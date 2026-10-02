#!/usr/bin/env python3
"""Start, stop, inspect or sign in to one consumer's Docker runner.

On Windows, run keeps the WSL 2 instance alive. A Windows Task Scheduler entry
may launch that same command at sign-in; Docker Desktop is not required.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nexkit.adapters import adapter  # noqa: E402
from nexkit.common import Blocked, read_json, read_regular_json, run, write_json  # noqa: E402
from nexkit.invocations import definitions, execution_config  # noqa: E402
from nexkit.pipelines import effective_config  # noqa: E402
from nexkit.policy import HASH, require  # noqa: E402
from nexkit.runner_host import (  # noqa: E402
    addresses,
    runner_identity,
    security_args,
    supervise_windows,
    wsl_command,
)


def selected(config, pipeline, invocation=None):
    cfg = effective_config(read_json(config), pipeline)
    if invocation:
        values = definitions(cfg)
        require(invocation in values, "Unknown invocation")
        cfg = execution_config({"config": cfg, "invocation": {"definition": values[invocation]}})
    return cfg


def supervise_wsl(name, state_path, *, stream=None):
    """A live Windows supervisor is required before starting this WSL service."""
    stream = stream or sys.stdin
    state = read_regular_json(state_path)
    require(state.get("kind") == "windows-wsl", "Expected a WSL runner host")
    service = name + ".service"
    previous = None
    try:
        while True:
            require(select.select([stream], [], [], 20)[0], "Windows supervisor heartbeat stopped")
            line = stream.readline(16385)
            if not line:
                return
            require(len(line) <= 16384, "Windows supervisor message is too large")
            message = json.loads(line)
            require(isinstance(message, dict), "Invalid Windows supervisor message")
            value = addresses(message.get("windows_host_ipv4"))
            if value != previous:
                # Stop first: a new interface must be protected before jobs run.
                run(["systemctl", "stop", service], timeout=50)
                state["windows_host_ipv4"] = value
                write_json(state_path, state)
                run(["systemctl", "start", service], timeout=50)
                previous = value
            require(
                run(["systemctl", "is-active", service], check=False).stdout.strip()
                in {"active", "activating"},
                "The consumer runner service stopped",
            )
    finally:
        run(["systemctl", "stop", service], timeout=50)


def operate(operation, cfg, *, supervised=False):
    name, directory, _ = runner_identity(cfg)
    root = Path(directory)
    require(
        sys.platform == "linux", "Manage the shared runner from Linux or through --wsl-distribution"
    )
    bound = read_regular_json(root / "runner.json")
    require(
        bound.get("repository") == cfg["repository"]
        and bound.get("default_branch") == cfg["default_branch"]
        and bound.get("isolation") == "container-v1"
        and bound.get("agent") == cfg["engine"]["name"],
        "The installed runner belongs to another consumer",
    )
    state = read_regular_json(root / "host.json")
    service = name + ".service"
    if operation == "run":
        require(
            supervised and state["kind"] == "windows-wsl",
            "On Windows use run from the Windows host; Linux hosts use start",
        )
        require(os.geteuid() == 0, "The WSL supervisor needs Linux administrator access")
        return supervise_wsl(name, root / "host.json")
    if operation == "start":
        require(state["kind"] == "linux", "Use run on the Windows host to keep WSL alive")
    if operation in {"start", "stop", "status"}:
        return subprocess.run(["sudo", "-n", "systemctl", operation, service]).returncode
    require(operation == "login", "Unknown runner operation")
    active = run(["systemctl", "is-active", service], check=False).stdout.strip()
    require(active not in {"active", "activating"}, "Stop the consumer runner before login")
    image = run(["docker", "container", "inspect", "--format", "{{.Image}}", name]).stdout.strip()
    require(
        image.startswith("sha256:")
        and HASH.fullmatch(image.removeprefix("sha256:"))
        and image == bound.get("image"),
        "The provisioned runner image changed or is unavailable",
    )
    installed_security = security_args(state, policy_directory=bound.get("policy_directory"))
    # Inspect the provisioned image without mounting the private login directory.
    integration = adapter(cfg["engine"])
    integration.probe(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            integration.executable,
            image,
        ],
    )
    command = integration.login_command(image, root, installed_security)
    return subprocess.run(command).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "start", "stop", "status", "login"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--invocation")
    parser.add_argument("--wsl-distribution", help="Explicit WSL 2 Ubuntu distribution")
    parser.add_argument("--supervise-wsl", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    cfg = selected(args.config, args.pipeline, args.invocation)
    if os.name == "nt":
        require(
            args.wsl_distribution and not args.supervise_wsl,
            "Choose --wsl-distribution on the Windows host",
        )
        command = wsl_command(
            args.wsl_distribution, Path(__file__), args.config, args.pipeline, args.invocation
        )
        command.append(args.operation)
        if args.operation == "run":
            command.append("--supervise-wsl")
            return supervise_windows(command)
        return subprocess.run(command).returncode
    require(not args.wsl_distribution, "Run Windows administration from the Windows host")
    return operate(args.operation, cfg, supervised=args.supervise_wsl)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Blocked, OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Runner administration blocked: {exc}") from exc
