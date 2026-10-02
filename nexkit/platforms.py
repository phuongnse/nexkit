"""Managed jobs use Linux; Windows hosts provide it through WSL 2 and Docker."""

from __future__ import annotations

import os
import platform
import re
import sys

from .common import Blocked


def runner_os(runner, *, hosted_only=False):
    if isinstance(runner, str) and re.fullmatch(r"ubuntu-[0-9]{2}\.[0-9]{2}", runner):
        return "linux"
    if hosted_only or not isinstance(runner, list):
        raise Blocked(
            "Select an explicit Ubuntu release runner for managed jobs; Windows hosts use Linux containers through WSL 2"
        )
    if not (
        4 <= len(runner) <= 8
        and all(
            isinstance(label, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", label)
            for label in runner
        )
        and len(set(runner)) == len(runner)
        and {"self-hosted", "linux", "x64"} <= set(runner)
        and not {"windows", "macos", "arm", "arm64"} & set(runner)
    ):
        raise Blocked("Use self-hosted, linux, x64 and a project-specific runner label")
    return "linux"


def execution_runner(cfg):
    runner = cfg["environment"]["runner"]
    runner_os(runner, hosted_only=True)
    return runner


def require_native(runner):
    if sys.version_info < (3, 11):
        raise Blocked("Managed jobs require Python 3.11 or newer")
    expected = runner_os(runner)
    actual = {"linux": "linux", "win32": "windows"}.get(sys.platform)
    if actual != expected or platform.machine().casefold() not in ("x86_64", "amd64"):
        raise Blocked(
            f"This managed job requires {expected}/x64; on Windows run it inside the Linux container"
        )
    declared = os.environ.get("RUNNER_OS")
    if declared and declared.casefold() != actual:
        raise Blocked("Actions runner OS differs from the Python execution platform")
    return actual
