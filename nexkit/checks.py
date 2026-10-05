"""Run the configured setup and check commands. These jobs hold no secrets."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

OUTPUT_TAIL = 12_000


def _run(command, cwd, timeout_seconds):
    started = time.monotonic()
    proc = subprocess.Popen(
        ["bash", "-o", "pipefail", "-c", command],
        cwd=cwd,
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


def setup(cfg, cwd):
    """Run setup commands in order. Return None, or a failed result for the first failure."""
    for command in cfg["setup"]:
        print(f"$ {command}", flush=True)
        code, output, seconds = _run(command, cwd, SETUP_SECONDS)
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


def run_checks(cfg, cwd, out_dir=None):
    """Run setup, then every check. A setup failure is reported as a failed check."""
    failed_setup = setup(cfg, cwd)
    results = [failed_setup] if failed_setup else []
    for check in [] if failed_setup else cfg["checks"]:
        print(f"::group::check {check['name']}: {check['run']}", flush=True)
        code, output, seconds = _run(check["run"], cwd, check["timeout_minutes"] * 60)
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
