"""Run the configured setup and check commands. These jobs hold no secrets."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .gitutil import GitError, git, has_commit
from .redact import Redactor
from .runlog import fence, one_line, write_summary
from .runtimes import NAMES_VAR, secret_names

OUTPUT_TAIL = 12_000
BASE_SHA = "NEXKIT_BASE_SHA"
VERIFIED_SHA = "NEXKIT_VERIFIED_SHA"
# The files that failing checks name in `results` go into the round's `nexkit-checks`
# artifact under `results/<check>/`, at most this many bytes per round.
RESULTS_DIR = "results"
RESULTS_LIMIT = 50_000_000
# How much of a file decides whether it is text, which is redacted, or binary.
SNIFF = 8192


def environment(commits=None):
    """The environment for repository commands: without any agent credential, and with
    NEXKIT_BASE_SHA and NEXKIT_VERIFIED_SHA only when they are known. `commits` maps
    those names to commits, or to None."""
    drop = {BASE_SHA, VERIFIED_SHA, NAMES_VAR, *secret_names()}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update({name: sha for name, sha in (commits or {}).items() if sha})
    return env


def _known(repo, name, sha, why):
    """`sha` when the checkout has that commit, else None. Say which in the log."""
    if sha and has_commit(repo, sha):
        print(f"{name}={sha}", flush=True)
        return sha
    print(f"{name} is not set: {why}, so checks should run in full.", flush=True)
    return None


def known_base(repo, sha):
    """`sha` when the checkout has that commit, for NEXKIT_BASE_SHA, else None."""
    return _known(repo, BASE_SHA, sha, "the merge base with the base branch is unknown")


def find_base(repo, base):
    """The merge base of HEAD and `origin/<base>`, for NEXKIT_BASE_SHA. The checkout needs
    the history of both."""
    try:
        sha = git(repo, "merge-base", "HEAD", f"origin/{base}").strip()
    except GitError as exc:
        print(f"Could not find the merge base with `{base}`: {exc}", flush=True)
        sha = None
    return known_base(repo, sha)


def known_verified(repo, sha):
    """The decision's `verified_sha` when the checkout has that commit, for
    NEXKIT_VERIFIED_SHA, else None. `route` read it from NexKit's own round comments."""
    if not sha:
        print(
            f"{VERIFIED_SHA} is not set: no earlier round of this pull request passed its "
            "checks, so checks should run in full.",
            flush=True,
        )
        return None
    why = f"the checkout does not have {sha[:7]}, the last commit whose checks passed"
    return _known(repo, VERIFIED_SHA, sha, why)


def find_commits(repo, decision):
    """NEXKIT_BASE_SHA and NEXKIT_VERIFIED_SHA for a run's setup and checks."""
    return {
        BASE_SHA: find_base(repo, decision["base"]),
        VERIFIED_SHA: known_verified(repo, decision.get("verified_sha")),
    }


def _run(command, cwd, timeout_seconds, env=None):
    started = time.monotonic()
    proc = subprocess.Popen(
        ["bash", "-o", "pipefail", "-c", command],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout_seconds)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
        output += f"\n[NexKit stopped the command after {timeout_seconds} seconds]"
        code = 124
    return code, output, round(time.monotonic() - started, 1)


def setup(cfg, cwd, redact=str, commits=None):
    """Run setup commands in order. Return None, or a failed result for the first failure.

    In the agent job, pass a redactor: the output may still show other secrets.
    """
    env = environment(commits)
    for command in cfg["setup"]:
        print(f"$ {command}", flush=True)
        timeout = cfg["limits"]["setup_timeout_minutes"] * 60
        code, output, seconds = _run(command, cwd, timeout, env)
        output = redact(output)
        print(output[-OUTPUT_TAIL:], flush=True)
        if code != 0:
            return {
                "name": "setup",
                "run": command,
                "exit_code": code,
                "passed": False,
                "seconds": seconds,
                "output": output[-OUTPUT_TAIL:],
            }
    return None


def size_text(size):
    return f"{size / 1_000_000:.1f} MB"


def _is_text(data):
    """Whether a file is text, which is redacted, rather than binary, such as a zip or an
    image, which is kept as it is."""
    if b"\0" in data[:SNIFF]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _result_files(root, path):
    """The regular files at `path` in the checkout `root`, in path order, without following
    links, or None when the path does not exist or leads out of the checkout."""
    target = root / path
    if target.is_symlink() or not target.exists():
        return None
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if target.is_file():
        return [target]
    found = []
    for folder, _, names in os.walk(target):  # does not enter linked folders
        for name in names:
            file = Path(folder) / name
            if file.is_file() and not file.is_symlink():
                found.append(file)
    return sorted(found, key=lambda file: file.relative_to(root).as_posix())


def keep_results(cfg, results, cwd, out_dir, redact=None):
    """Copy the files that each failing check names in `results` into
    `out_dir/results/<check>/<path>`, for the next fix round. Text files are redacted;
    binary files are copied as they are. Missing paths are skipped. In check order, then
    path order, files that would take the round past RESULTS_LIMIT are left out.

    Each failing check's result gets `results`, the paths of which files were kept, and
    `results_left_out`, the number of its files left out. Returns the lines for the run
    summary, or [] when no failing check names result files."""
    redact = redact or Redactor()
    root = Path(cwd)
    named = {check["name"]: check.get("results") or [] for check in cfg["checks"]}
    used = kept = 0
    missing, left_out = [], []
    for result in results:
        paths = [] if result["passed"] else named.get(result["name"]) or []
        if not paths:
            continue
        result["results"], result["results_left_out"] = [], 0
        for path in paths:
            files = _result_files(root, path)
            if files is None:
                missing.append(f"{result['name']}: {path}")
                print(f"{result['name']}: `{path}` was not found; skipped.", flush=True)
                continue
            count = 0
            for file in files:
                relative = file.relative_to(root).as_posix()
                shown = f"{result['name']}: {one_line(relative)}"
                try:
                    size = file.stat().st_size
                    if used + size > RESULTS_LIMIT:
                        left_out.append(f"{shown} ({size_text(size)})")
                        result["results_left_out"] += 1
                        continue
                    data = file.read_bytes()
                except OSError as exc:  # removed or unreadable since it was listed
                    print(f"{shown} could not be read: {exc}", flush=True)
                    continue
                if _is_text(data):
                    data = redact(data.decode("utf-8")).encode("utf-8")
                target = Path(out_dir) / RESULTS_DIR / result["name"] / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                used += len(data)
                count += 1
            if count:
                result["results"].append(path)
            kept += count
            print(f"{result['name']}: kept {count} of {len(files)} files of `{path}`.", flush=True)
    if not missing and not left_out and not kept:
        return []
    lines = [
        "### NexKit checks: result files",
        "",
        f"Kept {kept} {'file' if kept == 1 else 'files'} ({size_text(used)}) of the failing "
        "checks for the next fix round.",
    ]
    limit = size_text(RESULTS_LIMIT)
    for title, items in (
        ("**Not found**", missing),
        (f"**Left out** ({len(left_out)}): a round keeps at most {limit}", left_out),
    ):
        if items:
            text = "\n".join(items)
            lines += ["", title, "", fence(text), text, fence(text)]
    return lines


def run_checks(cfg, cwd, out_dir=None, commits=None):
    """Run setup, then every check. A setup failure is reported as a failed check. With
    `out_dir`, the failing checks' result files are kept there too (see `keep_results`)."""
    failed_setup = setup(cfg, cwd, commits=commits)
    results = [failed_setup] if failed_setup else []
    env = environment(commits)
    for check in [] if failed_setup else cfg["checks"]:
        print(f"::group::check {check['name']}: {check['run']}", flush=True)
        code, output, seconds = _run(check["run"], cwd, check["timeout_minutes"] * 60, env)
        print(output[-OUTPUT_TAIL:], flush=True)
        print("::endgroup::", flush=True)
        results.append(
            {
                "name": check["name"],
                "run": check["run"],
                "exit_code": code,
                "passed": code == 0,
                "seconds": seconds,
                "output": output[-OUTPUT_TAIL:],
            }
        )
        print(f"{check['name']}: {'passed' if code == 0 else f'failed (exit {code})'}")
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        redact = Redactor()
        lines = keep_results(cfg, results, cwd, out_dir, redact)
        if lines:
            write_summary("\n".join(lines) + "\n", redact)
        (Path(out_dir) / "checks.json").write_text(json.dumps(results, indent=2))
    return results
