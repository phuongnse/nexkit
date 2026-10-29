"""Run the configured setup and check commands. These jobs hold no secrets."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .gitutil import GitError, git, has_commit
from .redact import Redactor
from .runlog import fence, one_line, write_summary
from .runtimes import NAMES_VAR, secret_names
from .state import COMMIT, check_fingerprint

OUTPUT_TAIL = 12_000
BASE_SHA = "NEXKIT_BASE_SHA"
VERIFIED_SHA = "NEXKIT_VERIFIED_SHA"
# Set in each check's own environment: the last commit on which that check passed.
CHECK_VERIFIED_SHA = "NEXKIT_CHECK_VERIFIED_SHA"
# The commit that a `verify` job checked, next to its `checks.json`, so a round that NexKit
# stops in its checks records only checks that passed on the pull request's head.
CHECKED_FILE = "checked.json"
# The files that failing checks name in `results` go into the round's `nexkit-checks`
# artifact under `results/<check>/`, at most this many bytes per round.
RESULTS_DIR = "results"
RESULTS_LIMIT = 50_000_000
# How much of a file decides whether it is text, which is redacted, or binary.
SNIFF = 8192
# A line of a check's output that reports a failure, for `failure_signature`.
FAILURE_LINE = re.compile(r"fail|error|✘|✗", re.IGNORECASE)
# What differs between two runs of the same failure, removed in this order: UUIDs, hex ids,
# durations, then any other number.
VOLATILE = (
    re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE),
    re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE),
    re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b", re.IGNORECASE),
    re.compile(
        r"\d+(?:\.\d+)?\s*(?:ms|s|secs?|seconds?|m|mins?|minutes?|h|hours?)\b", re.IGNORECASE
    ),
    re.compile(r"\d+"),
)
SIGNATURE_LIMIT = 200
# A test reporter names a test by its path of describe blocks, such as
# "OrderHistory > pages through more than twenty orders"; the last segment is the test.
TEST_SEGMENT = re.compile(r"\s+(?:›|>)\s+")
# The arguments of a test named as code, such as `Totals(count: 2)`, and the line and
# column after a file, such as `orders.test.ts:12:3`.
TEST_ARGUMENTS = re.compile(r"\s*\(.*\)\Z")
TEST_LINE = re.compile(r"(?::\d+){1,2}\Z")
# A JUnit `classname` that names the test's file rather than its class.
TEST_FILE = re.compile(r"[/\\]|\.(?:[cm]?[jt]sx?|py|rb|go|java|kt|cs|fs|vb|php|rs|swift|exs?)\Z")
# The most failing tests a check records from its JUnit files. A check with more records
# none, and is compared as a check that names no test (see `autopilot.same_failure`).
FAILED_TESTS_LIMIT = 200


def environment(commits=None):
    """The environment for repository commands: without any agent credential, and with
    NEXKIT_BASE_SHA and NEXKIT_VERIFIED_SHA only when they are known. `commits` maps
    those names to commits, or to None."""
    drop = {BASE_SHA, VERIFIED_SHA, CHECK_VERIFIED_SHA, NAMES_VAR, *secret_names()}
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


def check_commit(repo, check, heads):
    """The last commit of the pull request on which `check` passed with its command, for
    NEXKIT_CHECK_VERIFIED_SHA, when the checkout has it, else None. `heads` is the
    decision's `check_heads`, which `route` read from NexKit's own round comments."""
    found = (heads or {}).get(check["name"])
    sha = found.get(check_fingerprint(check["run"])) if isinstance(found, dict) else None
    name = check["name"]
    if not (isinstance(sha, str) and COMMIT.fullmatch(sha)):
        print(
            f"{CHECK_VERIFIED_SHA} is not set for `{name}`: no earlier round of this pull "
            "request passed it with this command, so it should run in full.",
            flush=True,
        )
        return None
    if not has_commit(repo, sha):
        print(
            f"{CHECK_VERIFIED_SHA} is not set for `{name}`: the checkout does not have "
            f"{sha[:7]}, the last commit on which it passed, so it should run in full.",
            flush=True,
        )
        return None
    print(f"{CHECK_VERIFIED_SHA}={sha} for `{name}`", flush=True)
    return sha


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


def failure_signature(output):
    """What tells one failure of a check from another: the first line of its output that
    reports a failure (FAIL, Error, failed, AssertionError, ✘), or else its last line,
    without ids, durations, numbers and runs of whitespace, which differ between runs."""
    lines = [line for line in (output or "").splitlines() if line.strip()]
    line = next((x for x in lines if FAILURE_LINE.search(x)), lines[-1] if lines else "")
    for pattern in VOLATILE:
        line = pattern.sub("", line)
    return " ".join(line.split())[:SIGNATURE_LIMIT]


def failing_signatures(results):
    """{check name: failure signature} for each failed check of `results`."""
    return {
        str(result.get("name")): failure_signature(result.get("output"))
        for result in results or []
        if isinstance(result, dict) and not result.get("passed")
    }


def test_name(title, file=""):
    """The name a failing test is compared by, the same for a JUnit test case and a test
    reporter's error annotation of that test. A test named as code, such as
    `Shop.Tests.OrderTests.Totals(count: 2)`, is its class and method: `ordertests.totals`.
    Any other is the last segment of its title, after its describe blocks, behind the name
    of its file without folders, line or column, or behind nothing when the file is not
    known: `orders.test.ts::adds tax`, `::adds tax`. Case and runs of whitespace do not
    count. "" when there is no title."""
    title = " ".join(str(title or "").split())
    last = TEST_SEGMENT.split(title)[-1] if title else ""
    code = TEST_ARGUMENTS.sub("", last)
    if code and " " not in code and "." in code.strip("."):
        return ".".join(code.strip(".").split(".")[-2:]).casefold()
    if not last:
        return ""
    path = TEST_LINE.sub("", str(file or "").strip().replace("\\", "/"))
    return f"{posixpath.basename(path)}::{last}".casefold()


def same_test(one, other):
    """Whether two names from `test_name` name the same test: the same name, in the same
    file when both say which."""
    if one == other:
        return True
    if "::" not in one or "::" not in other:
        return False
    file, _, name = one.partition("::")
    other_file, _, other_name = other.partition("::")
    return name == other_name and not (file and other_file)


def junit_failures(data):
    """The names (see `test_name`) of the test cases that failed or erred in a JUnit XML
    report, or None when `data` is not one. A case's `classname` is its class, joined to
    its name, unless it names a file, as JavaScript reporters do; a `file` attribute names
    the file."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    found = []
    for case in root.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        name, owner = case.get("name") or "", case.get("classname") or ""
        file = case.get("file") or ""
        if TEST_FILE.search(owner):
            file = file or owner
        elif owner and " " not in TEST_ARGUMENTS.sub("", name) and not name.startswith(f"{owner}."):
            name = f"{owner}.{name}"
        found.append(test_name(name, file))
    return [name for name in dict.fromkeys(found) if name]


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


def keep_results(cfg, results, cwd, out_dir, redact=None, jobs=1):
    """Copy the files that each failing check names in `results` into
    `out_dir/results/<check>/<path>`, for the next fix round. Text files are redacted;
    binary files are copied as they are. Missing paths are skipped. In check order, then
    path order, files that would take the round past RESULTS_LIMIT are left out; when
    the round's checks run in several `jobs`, past this job's share of it.

    Each failing check's result gets `results`, the paths of which files were kept, and
    `results_left_out`, the number of its files left out. When kept files are JUnit XML
    reports, it also gets `failed_tests`, the names of their failed test cases (see
    `junit_failures`), which a round compares with the default branch's failing tests;
    none past FAILED_TESTS_LIMIT. Returns the lines for the run summary, or [] when no
    failing check names result files."""
    redact = redact or Redactor()
    limit = RESULTS_LIMIT // jobs
    root = Path(cwd)
    named = {check["name"]: check.get("results") or [] for check in cfg["checks"]}
    used = kept = 0
    missing, left_out = [], []
    for result in results:
        paths = [] if result["passed"] else named.get(result["name"]) or []
        if not paths:
            continue
        result["results"], result["results_left_out"] = [], 0
        tests = None
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
                    if used + size > limit:
                        left_out.append(f"{shown} ({size_text(size)})")
                        result["results_left_out"] += 1
                        continue
                    data = file.read_bytes()
                except OSError as exc:  # removed or unreadable since it was listed
                    print(f"{shown} could not be read: {exc}", flush=True)
                    continue
                if _is_text(data):
                    data = redact(data.decode("utf-8")).encode("utf-8")
                    failures = junit_failures(data) if file.suffix == ".xml" else None
                    if failures is not None:
                        tests = [*(tests or []), *failures]
                target = Path(out_dir) / RESULTS_DIR / result["name"] / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                used += len(data)
                count += 1
            if count:
                result["results"].append(path)
            kept += count
            print(f"{result['name']}: kept {count} of {len(files)} files of `{path}`.", flush=True)
        if tests is not None:
            tests = list(dict.fromkeys(tests))
            result["failed_tests"] = tests if len(tests) <= FAILED_TESTS_LIMIT else []
    if not missing and not left_out and not kept:
        return []
    lines = [
        "### NexKit checks: result files",
        "",
        f"Kept {kept} {'file' if kept == 1 else 'files'} ({size_text(used)}) of the failing "
        "checks for the next fix round.",
    ]
    most = f"a round keeps at most {size_text(limit)}"
    if jobs > 1:
        most = f"each of the round's {jobs} check jobs keeps at most {size_text(limit)}"
    for title, items in (
        ("**Not found**", missing),
        (f"**Left out** ({len(left_out)}): {most}", left_out),
    ):
        if items:
            text = "\n".join(items)
            lines += ["", title, "", fence(text), text, fence(text)]
    return lines


def check_jobs(cfg):
    """{job: [check, ...]} for checks that run in parallel jobs, in the order of `checks`:
    with `parallel_checks`, the checks of one `group` share a job, and each other check
    has a job of its own, named after it. {} when the checks run in one job: with
    `parallel_checks` off, or when every check would run in the same job."""
    if not cfg.get("parallel_checks"):
        return {}
    jobs = {}
    for check in cfg["checks"]:
        jobs.setdefault(check.get("group") or check["name"], []).append(check)
    return jobs if len(jobs) > 1 else {}


def check_matrix(cfg):
    """The `verify` job's matrix: one entry per job of `check_jobs`, each with the job's
    name and time-out, or one entry with no name that runs every check, as one job."""
    setup = cfg["limits"]["setup_timeout_minutes"]

    def entry(group, checks):
        return {"group": group, "timeout": sum(c["timeout_minutes"] for c in checks) + setup + 10}

    jobs = check_jobs(cfg)
    if not jobs:
        return {"include": [entry("", cfg["checks"])]}
    return {"include": [entry(group, checks) for group, checks in jobs.items()]}


def _only(cfg, group):
    """`cfg` with only the checks of the job `group` of `check_jobs`; all of them for ""."""
    if not group:
        return cfg
    jobs = check_jobs(cfg)
    if group not in jobs:
        raise ValueError(f"No check job is named '{group}'")
    return {**cfg, "checks": jobs[group]}


def run_checks(cfg, cwd, out_dir=None, commits=None, group="", heads=None):
    """Run setup, then every check, or with `group` the checks of that job of
    `check_jobs`. A setup failure is reported as a failed check. Each check gets the last
    commit on which it passed, from `heads` (see `check_commit`). With `out_dir`, the
    failing checks' result files are kept there too (see `keep_results`), and the commit
    checked; each of several jobs keeps its share of RESULTS_LIMIT, so the round stays
    within it."""
    jobs = len(check_jobs(cfg)) if group else 1
    cfg = _only(cfg, group)
    failed_setup = setup(cfg, cwd, commits=commits)
    results = [failed_setup] if failed_setup else []
    env = environment(commits)
    for check in [] if failed_setup else cfg["checks"]:
        print(f"::group::check {check['name']}: {check['run']}", flush=True)
        sha = check_commit(cwd, check, heads)
        own = {**env, CHECK_VERIFIED_SHA: sha} if sha else env
        code, output, seconds = _run(check["run"], cwd, check["timeout_minutes"] * 60, own)
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
        lines = keep_results(cfg, results, cwd, out_dir, redact, jobs)
        if lines:
            write_summary("\n".join(lines) + "\n", redact)
        (Path(out_dir) / "checks.json").write_text(json.dumps(results, indent=2))
        try:
            head = git(cwd, "rev-parse", "HEAD").strip()
        except GitError:
            head = None
        (Path(out_dir) / CHECKED_FILE).write_text(json.dumps({"head": head}))
    return results


def artifact_name(group):
    """The artifact of the `verify` job that runs the checks of `group`: `nexkit-checks`
    for the one job that runs them all, which the other jobs read."""
    return f"nexkit-checks-{group}" if group else "nexkit-checks"


def _read_results(path):
    try:
        results = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(results, list):
        return None
    return [result for result in results if isinstance(result, dict)]


def combine(cfg, parts, out_dir):
    """Join the results of the parallel `verify` jobs, each downloaded to
    `parts/<artifact_name>/`, into `out_dir` as one job would have left them: one
    `checks.json` in the order of `checks`, and each job's `results/<check>/` files. A
    setup that failed comes first, once, and the checks of its job are left out, as after
    a setup failure in one job. A job that left no result fails each of its checks.
    Returns the results.

    When only one job left an artifact, the download puts it in `parts` itself; it
    belongs to the job whose checks it names."""
    parts, out = Path(parts), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    found, setups = {}, []
    flat = _read_results(parts / "checks.json")
    for group, checks in check_jobs(cfg).items():
        folder = parts / artifact_name(group)
        results = _read_results(folder / "checks.json")
        names = {"setup", *(check["name"] for check in checks)}
        if results is None and flat is not None and {r.get("name") for r in flat} <= names:
            folder, results, flat = parts, flat, None
        if results is None:
            print(f"The check job `{group}` left no result.", flush=True)
            results = [
                {
                    "name": check["name"],
                    "run": check["run"],
                    "exit_code": None,
                    "passed": False,
                    "seconds": None,
                    "output": f"The check job `{group}` ended without a result.",
                }
                for check in checks
            ]
        for result in results:
            if result.get("name") == "setup":
                setups.append(result)
            elif result.get("name") in names:
                found[result["name"]] = result
        if (folder / RESULTS_DIR).is_dir():
            shutil.copytree(
                folder / RESULTS_DIR, out / RESULTS_DIR, symlinks=True, dirs_exist_ok=True
            )
    combined = setups[:1] + [found[c["name"]] for c in cfg["checks"] if c["name"] in found]
    (out / "checks.json").write_text(json.dumps(combined, indent=2))
    return combined
