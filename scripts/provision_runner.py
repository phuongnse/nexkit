#!/usr/bin/env python3
"""Provision a repo-scoped official Actions runner on an authorized Linux host.

Preview is the default. This is setup glue around Docker, systemd and GitHub's
official runner registration, not an agent runtime or a task dispatcher.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nexkit.common import read_json  # noqa: E402
from nexkit.policy import agent_runner, authentication, config, require  # noqa: E402


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
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    cfg = config(read_json(args.config))
    require(
        authentication(cfg) == "chatgpt", "Select the consumer's ChatGPT authentication explicitly"
    )
    labels = agent_runner(cfg)
    custom = [label for label in labels if label not in ("self-hosted", "linux", "x64")]
    require(len(custom) == 1, "This provisioning command takes one project-specific runner label")
    label = custom[0]
    name = "nexkit-" + hashlib.sha256(cfg["repository"].encode()).hexdigest()[:12]
    root = Path("/var/lib/nexkit-runners", name)
    binding = {
        "repository": cfg["repository"],
        "default_branch": cfg["default_branch"],
        "auth": "chatgpt",
        "isolation": "container-v1",
        "workflows": ["nexkit-delivery.yml", "nexkit-clarify.yml", "nexkit-runner-probe.yml"],
    }
    plan = {
        "repository": cfg["repository"],
        "container": name,
        "agent_runner": labels,
        "login": "A separate Codex ChatGPT login must be completed inside this consumer runner",
        "host_state": str(root),
        "network": "Dedicated bridge; host/private/link-local access blocked",
        "credentials_copied": False,
        "applied": args.apply,
    }
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
    for filename in ("apparmor.profile", "host_network.py", "seccomp.json"):
        root_file(Path("/opt/nexkit-runner", filename), (ROOT / "runner" / filename).read_text())
    run(["sudo", "python3", "/opt/nexkit-runner/host_network.py"])
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
        "--security-opt",
        "apparmor=nexkit-runner",
        "--security-opt",
        "seccomp=/opt/nexkit-runner/seccomp.json",
        "--mount",
        f"type=bind,src={root / 'runner.json'},dst=/etc/nexkit/runner.json,readonly",
        "--mount",
        f"type=bind,src={root / 'codex'},dst=/var/lib/nexkit/codex",
        "nexkit-runner:0.1.0-rc.1",
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
ExecStartPre=/usr/bin/python3 /opt/nexkit-runner/host_network.py
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
    run(["sudo", "systemctl", "enable", "--now", name + ".service"])
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
