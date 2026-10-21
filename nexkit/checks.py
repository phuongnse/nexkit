"""Run the configured setup and check commands. These jobs hold no secrets."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .gitutil import GitError, git, has_commit
from .runtimes import NAMES_VAR, secret_names

OUTPUT_TAIL = 12_000
BASE_SHA = "NEXKIT_BASE_SHA"
VERIFIED_SHA = "NEXKIT_VERIFIED_SHA"


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


def run_checks(cfg, cwd, out_dir=None, commits=None):
    """Run setup, then every check. A setup failure is reported as a failed check."""
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
        (Path(out_dir) / "checks.json").write_text(json.dumps(results, indent=2))
    return results
