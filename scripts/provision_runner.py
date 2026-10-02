#!/usr/bin/env python3
"""Provision one Linux container runner on Ubuntu or a Windows WSL 2 host.

Preview is the default. This is setup glue around Docker, systemd and GitHub's
official runner registration, not an agent runtime or a task dispatcher.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nexkit import __version__  # noqa: E402
from nexkit.adapters import adapter  # noqa: E402
from nexkit.common import Blocked, read_json  # noqa: E402
from nexkit.invocations import definitions, execution_config  # noqa: E402
from nexkit.pipelines import effective_config  # noqa: E402
from nexkit.policy import HASH, agent_runner, require  # noqa: E402
from nexkit.runner_host import (  # noqa: E402
    addresses,
    host_kind,
    image_build,
    inspect_host,
    runner_identity,
    security_args,
    windows_addresses,
    wsl_command,
)


def run(args, *, data=None):
    return subprocess.run(args, input=data, text=True, capture_output=True, check=True).stdout


def root_file(path, data, mode="644"):
    with tempfile.NamedTemporaryFile(mode="w") as staged:
        staged.write(data)
        staged.flush()
        run(
            [
                "sudo",
                "install",
                "-D",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                mode,
                staged.name,
                str(path),
            ]
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--pipeline", required=True, help="Configured pipeline whose runner is being provisioned"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--image", help="Use a reviewed local runner image instead of building defaults"
    )
    parser.add_argument(
        "--check-host", action="store_true", help="Inspect Docker and host prerequisites"
    )
    parser.add_argument("--wsl-distribution", help="Explicit WSL 2 Ubuntu distribution on Windows")
    parser.add_argument("--host", choices=("linux", "windows-wsl"), help=argparse.SUPPRESS)
    parser.add_argument("--windows-host-ipv4", action="append", help=argparse.SUPPRESS)
    parser.add_argument(
        "--invocation", help="Select an invocation's runner override within the pipeline"
    )
    args = parser.parse_args()
    cfg = effective_config(read_json(args.config), args.pipeline)
    if args.invocation:
        values = definitions(cfg)
        require(args.invocation in values, "Unknown invocation")
        cfg = execution_config(
            {"config": cfg, "invocation": {"definition": values[args.invocation]}}
        )
    kind = args.host or host_kind()
    labels = agent_runner(cfg)
    name, directory, label = runner_identity(cfg)
    root = Path(directory)
    image, build = image_build(cfg)
    image = args.image or image
    binding = {
        "repository": cfg["repository"],
        "default_branch": cfg["default_branch"],
        "auth": "chatgpt",
        "isolation": "container-v1",
        "agent": cfg["engine"]["name"],
        "policy_directory": f"/opt/nexkit-runner/{__version__}",
        "workflows": [Path(path).name for path in cfg["binding"]["agent_workflows"]],
    }
    require(
        binding["workflows"],
        "Explicitly accept the workflows allowed to use this credential-bearing runner",
    )
    plan = {
        "repository": cfg["repository"],
        "container": name,
        "image": image,
        "image_build": None if args.image else build,
        "invocation": args.invocation,
        "agent_runner": labels,
        "allowed_workflows": binding["workflows"],
        "login": "A separate Codex ChatGPT login must be completed inside this consumer runner",
        "host": kind,
        "wsl_distribution": args.wsl_distribution,
        "host_state": directory,
        "policy_files": f"/opt/nexkit-runner/{__version__}",
        "lifecycle": "systemd" if kind == "linux" else "Windows supervisor keeps WSL alive",
        "docker_desktop_required": False,
        "network": "Dedicated bridge; host/private/link-local access blocked",
        "credentials_copied": False,
        "applied": args.apply,
    }
    if not args.apply and not args.check_host:
        print(json.dumps(plan, indent=2))
        return
    if os.name == "nt":
        require(args.wsl_distribution, "Choose --wsl-distribution on Windows")
        command = wsl_command(
            args.wsl_distribution, Path(__file__), args.config, args.pipeline, args.invocation
        )
        command.extend(["--host", "windows-wsl"])
        if args.image:
            command.extend(["--image", args.image])
        for address in windows_addresses():
            command.extend(["--windows-host-ipv4", address])
        command.append("--apply" if args.apply else "--check-host")
        raise SystemExit(subprocess.run(command).returncode)
    require(not args.wsl_distribution, "Use --wsl-distribution from the Windows host")
    state = inspect_host(kind)
    if kind == "windows-wsl":
        state["windows_host_ipv4"] = addresses(args.windows_host_ipv4)
    else:
        require(not args.windows_host_ipv4, "Windows addresses apply only to a WSL host")
    plan["host_checks"] = state
    if not args.apply:
        print(json.dumps(plan, indent=2))
        return
    # Reprovisioning an existing runner must be a deliberate update with cleanup;
    # never silently replace its registration or persistent account session.
    existing = subprocess.run(["docker", "container", "inspect", name], capture_output=True)
    require(
        existing.returncode != 0,
        "Runner container already exists; inspect it before an explicit update",
    )
    # Build before registration or login; preview exposes these exact arguments.
    if not args.image:
        run(build)
    image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip()
    require(
        image_id.startswith("sha256:") and HASH.fullmatch(image_id.removeprefix("sha256:")),
        "Docker did not return an immutable runner image identity",
    )
    binding["image"] = image_id
    plan["image_id"] = image_id
    integration = adapter(cfg["engine"])
    capabilities = integration.probe(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            integration.executable,
            image_id,
        ],
    )
    binding["capabilities"] = capabilities
    plan["capabilities"] = capabilities
    network = subprocess.run(
        ["docker", "network", "inspect", "nexkit-runners"], capture_output=True
    )
    if network.returncode:
        run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--opt",
                "com.docker.network.bridge.enable_icc=false",
                "nexkit-runners",
            ]
        )
    policy = Path("/opt/nexkit-runner", __version__)
    for filename in ("apparmor.profile", "host_network.py", "seccomp.json"):
        root_file(policy / filename, (ROOT / "runner" / filename).read_text())
    root_file(root / "host.json", json.dumps(state, indent=2) + "\n")
    run(
        [
            "sudo",
            "python3",
            str(policy / "host_network.py"),
            "--state",
            str(root / "host.json"),
            "--apparmor-profile",
            str(policy / "apparmor.profile"),
        ]
    )
    root_file(root / "runner.json", json.dumps(binding, indent=2) + "\n")
    run(["sudo", "install", "-d", "-o", "1101", "-g", "1101", "-m", "700", str(root / "codex")])
    # The repo administrator's long-lived GitHub credential stays on the host.
    token = json.loads(
        run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{cfg['repository']}/actions/runners/registration-token",
            ]
        )
    )["token"]
    args_run = [
        "docker",
        "run",
        "--interactive",
        "--name",
        name,
        "--network",
        "nexkit-runners",
        "--restart",
        "no",
        "--cap-drop",
        "NET_RAW",
        "--memory",
        "2g",
        "--cpus",
        "2",
        "--pids-limit",
        "512",
        "--sysctl",
        "net.ipv6.conf.all.disable_ipv6=1",
        *security_args(state, policy_directory=str(policy)),
        "--mount",
        f"type=bind,src={root / 'runner.json'},dst=/etc/nexkit/runner.json,readonly",
        "--mount",
        f"type=bind,src={root / 'codex'},dst=/var/lib/nexkit/codex",
        image_id,
    ]
    # Docker stdin holds only the short-lived registration token and is closed
    # by the entrypoint before the listener starts. Never print token/command data.
    with tempfile.TemporaryFile(mode="w+") as bootstrap_log:
        process = subprocess.Popen(
            args_run, stdin=subprocess.PIPE, stdout=bootstrap_log, stderr=bootstrap_log, text=True
        )
        process.stdin.write(json.dumps({"registration_token": token, "label": label}) + "\n")
        process.stdin.close()
        del token
        registered = False
        for _ in range(30):
            check = subprocess.run(
                ["docker", "exec", name, "test", "-f", "/opt/actions-runner/.runner"],
                capture_output=True,
            )
            if check.returncode == 0:
                registered = True
                break
            if process.poll() is not None:
                break
            time.sleep(1)
        subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True)
        process.wait(timeout=15)
        require(
            registered,
            "Runner bootstrap failed; no account credential was supplied. Inspect the scoped container before retrying",
        )
    # Every restart installs the scoped network policy before the listener can
    # accept jobs. Docker's own auto-start is deliberately disabled.
    unit = f"""[Unit]
Description=NexKit repository runner {cfg["repository"]}
Requires=docker.service
After=docker.service

[Service]
Type=simple
ExecStartPre=/usr/bin/python3 {policy / "host_network.py"} --state {root / "host.json"} --apparmor-profile {policy / "apparmor.profile"}
ExecStart=/usr/bin/docker start --attach {name}
ExecStop=/usr/bin/docker stop --time 30 {name}
Restart=always
RestartSec=5
SuccessExitStatus=143
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
"""
    root_file(Path("/etc/systemd/system", name + ".service"), unit)
    run(["sudo", "systemctl", "daemon-reload"])
    if kind == "linux":
        run(["sudo", "systemctl", "enable", "--now", name + ".service"])
    else:
        plan["next_action"] = (
            "Use scripts/manage_runner.py run from Windows to start and supervise this runner"
        )
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Runner provisioning blocked: {exc}") from exc
