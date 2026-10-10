"""Run the configured setup and check commands. These jobs hold no secrets."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .gitutil import GitError, git, has_commit

OUTPUT_TAIL = 12_000
BASE_SHA = "NEXKIT_BASE_SHA"


def environment(base_sha=None):
    """The environment for repository commands: NEXKIT_BASE_SHA is set only when known."""
    env = {k: v for k, v in os.environ.items() if k != BASE_SHA}
    if base_sha:
        env[BASE_SHA] = base_sha
    return env


def known_base(repo, sha):
    """`sha` when the checkout has that commit, else None. Say which in the log."""
    if has_commit(repo, sha):
        print(f"{BASE_SHA}={sha}", flush=True)
        return sha
    print(
        f"{BASE_SHA} is not set: the merge base with the base branch is unknown, "
        "so checks should run in full.",
        flush=True,
    )
    return None


def find_base(repo, base):
    """The merge base of HEAD and `origin/<base>`, for NEXKIT_BASE_SHA. The checkout needs
    the history of both."""
    try:
        sha = git(repo, "merge-base", "HEAD", f"origin/{base}").strip()
    except GitError as exc:
        print(f"Could not find the merge base with `{base}`: {exc}", flush=True)
        sha = None
    return known_base(repo, sha)


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


SETUP_SECONDS = 30 * 60


def setup(cfg, cwd, redact=str, base_sha=None):
    """Run setup commands in order. Return None, or a failed result for the first failure.

    In the agent job the Claude credential is in the environment, so pass a redactor there.
    """
    env = environment(base_sha)
    for command in cfg["setup"]:
        print(f"$ {command}", flush=True)
        code, output, seconds = _run(command, cwd, SETUP_SECONDS, env)
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


def run_checks(cfg, cwd, out_dir=None, base_sha=None):
    """Run setup, then every check. A setup failure is reported as a failed check."""
    failed_setup = setup(cfg, cwd, base_sha=base_sha)
    results = [failed_setup] if failed_setup else []
    env = environment(base_sha)
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
