#!/usr/bin/env python3
"""Verify the shared Docker runtime with fake credentials and a simulated model.

This does not register a GitHub runner, log in, call a real model or publish.
The bridge, firewall chain and AppArmor test profile are scoped to this run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nexkit.adapters.codex import CodexAdapter  # noqa: E402
from nexkit.common import run  # noqa: E402
from nexkit.policy import VERSION  # noqa: E402
from nexkit.runner_host import (  # noqa: E402
    host_kind,
    inspect_host,
    windows_addresses,
    wsl_path,
    wsl_prefix,
)
from scripts.install_codex import VERSION as CLI_REFERENCE  # noqa: E402

COMMANDS = r"""
set -eu
cp -a /source /kit
chmod -R a+rX,go-w /kit
python3 -m venv /opt/nexkit-test-python
/opt/nexkit-test-python/bin/python -m pip install --quiet -r /kit/requirements-dev.txt
cd /kit
NEXKIT_TEST_ACTIONS_RUNTIME=1 /opt/nexkit-test-python/bin/python -m unittest tests.test_actions_runtime.ActionsRuntimeTests -v
python3 -m nexkit.adapters.codex_subscription --probe
NEXKIT_TEST_CONTAINER_CLI=1 /opt/nexkit-test-python/bin/python -m unittest tests.test_codex_loop.ContainerCodexLoopTests -v
useradd --create-home --shell /bin/bash nexkit-agent
useradd --create-home --shell /bin/bash nexkit-test-controller
printf '%s\n' 'nexkit-test-controller ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/nexkit-test-controller
chmod 440 /etc/sudoers.d/nexkit-test-controller
sudo -u nexkit-test-controller env NEXKIT_TEST_ISOLATION=1 /opt/nexkit-test-python/bin/python -m unittest tests.test_step_isolation -v
rm /etc/sudoers.d/nexkit-test-controller
userdel --remove nexkit-test-controller
userdel --remove nexkit-agent
NEXKIT_TEST_AGENT_WORKSPACE=1 /opt/nexkit-test-python/bin/python -m unittest tests.test_agent_workspace.LinuxAgentWorkspaceTests -v
"""


def snapshot(destination):
    for name in (
        "bin",
        "nexkit",
        "plugins",
        "schemas",
        "actions",
        "runner",
        ".github",
        "docs",
        "scripts",
        "tests",
    ):
        shutil.copytree(
            ROOT / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    for name in (
        "README.md",
        "CHANGELOG.md",
        "AGENTS.md",
        "pyproject.toml",
        "requirements-dev.txt",
    ):
        shutil.copyfile(ROOT / name, destination / name)


def verify(extra, windows_listener=None, *, codex_version=CLI_REFERENCE):
    state = inspect_host(host_kind())
    state["windows_host_ipv4"] = extra
    image = "nexkit-container-acceptance:codex-" + codex_version
    name = f"nexkit-acceptance-{os.getpid()}"
    chain = f"NEXKIT-TEST-{os.getpid()}"
    bridge = None
    with tempfile.TemporaryDirectory(prefix="nexkit-docker-") as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        snapshot(source)
        subprocess.run(
            [
                "docker",
                "build",
                "--tag",
                image,
                "--build-arg",
                "CODEX_VERSION=" + codex_version,
                str(source / "runner"),
            ],
            check=True,
        )
        capabilities = CodexAdapter().probe(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "codex", image],
        )
        state_path = root / "host.json"
        state_path.write_text(json.dumps(state))
        state_path.chmod(0o600)
        # The policy reader requires a root-owned file, including in this probe.
        run(["sudo", "-n", "chown", "root:root", state_path])
        profile_name = name
        profile = root / "apparmor.profile"
        profile.write_text(
            (source / "runner/apparmor.profile").read_text().replace("nexkit-runner", profile_name)
        )
        seccomp = source / "runner/seccomp.json"
        run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--opt",
                "com.docker.network.bridge.enable_icc=false",
                name,
            ]
        )
        try:
            network = json.loads(run(["docker", "network", "inspect", name]).stdout)[0]
            bridge = network["Options"].get(
                "com.docker.network.bridge.name", "br-" + network["Id"][:12]
            )
            subprocess.run(
                [
                    "sudo",
                    "-n",
                    sys.executable,
                    str(source / "runner/host_network.py"),
                    "--state",
                    str(state_path),
                    "--network",
                    name,
                    "--chain",
                    chain,
                    "--apparmor-profile",
                    str(profile),
                ],
                check=True,
            )
            docker = [
                "docker",
                "run",
                "--rm",
                "--network",
                name,
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
                "seccomp=" + str(seccomp),
            ]
            if state["apparmor"]:
                docker.extend(["--security-opt", "apparmor=" + profile_name])
            with socket.create_server(("0.0.0.0", 0)) as listener:
                port = listener.getsockname()[1]
                thread = threading.Thread(target=lambda: listener.accept()[0].close(), daemon=True)
                thread.start()
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
                thread.join(timeout=2)
                targets = [(network["IPAM"]["Config"][0]["Gateway"], port)]
                if windows_listener:
                    targets.extend(
                        (address, windows_listener)
                        for address in extra
                        if not address.startswith("127.")
                    )
                probe = (
                    "import socket,sys; socket.create_connection(('1.1.1.1',443),timeout=10).close(); "
                    "print('PUBLIC_NETWORK_REACHABLE'); "
                    f"targets={targets!r}\n"
                    "for address,port in targets:\n"
                    "    try: socket.create_connection((address,port),timeout=2).close()\n"
                    "    except OSError: pass\n"
                    "    else: sys.exit('Runner reached a protected host listener')\n"
                    "print('HOST_LISTENERS_DENIED')"
                )
                subprocess.run(
                    [*docker, "--name", name + "-network", image, "python3", "-c", probe],
                    check=True,
                    timeout=60,
                )
            subprocess.run(
                [
                    *docker,
                    "--name",
                    name + "-runtime",
                    "--mount",
                    f"type=bind,src={source},dst=/source,readonly",
                    image,
                    "/bin/bash",
                    "-c",
                    COMMANDS,
                ],
                check=True,
                timeout=900,
            )
            print(
                json.dumps(
                    {
                        "host": state["kind"],
                        "apparmor": state["apparmor"],
                        "kernel": state["kernel"],
                        "docker_version": state["docker_version"],
                        "agent_capabilities": capabilities,
                        "docker_desktop_required": False,
                        "real_container_commands": True,
                        "workflow_setup_python_tested": True,
                        "workflow_workspace_action_tested": True,
                        "readonly_roles_tested": ["request", "task", "review"],
                        "nested_controller_directory_tested": True,
                        "model_calls": 0,
                        "model_responses_simulated": True,
                        "github_registration_tested": False,
                        "credentials_copied": False,
                    }
                )
            )
        finally:
            # Stop containers before removing their firewall rules, including
            # when a timed-out Docker client left its container running.
            for container in (name + "-network", name + "-runtime"):
                run(["docker", "rm", "--force", container], check=False)
            if bridge:
                for rule in (
                    ("DOCKER-USER", ["-i", bridge, "-j", chain]),
                    ("INPUT", ["-i", bridge, "-j", "REJECT"]),
                ):
                    run(["sudo", "-n", "iptables", "-w", "5", "-D", rule[0], *rule[1]], check=False)
                run(["sudo", "-n", "iptables", "-w", "5", "-F", chain], check=False)
                run(["sudo", "-n", "iptables", "-w", "5", "-X", chain], check=False)
            run(["docker", "network", "rm", name], check=False)
            if state["apparmor"]:
                run(["sudo", "-n", "apparmor_parser", "-R", profile], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wsl-distribution")
    parser.add_argument("--windows-host-ipv4", action="append", default=[])
    parser.add_argument("--windows-listener", type=int)
    parser.add_argument(
        "--codex-version", default=CLI_REFERENCE, help="Exact CLI release to exercise"
    )
    args = parser.parse_args()
    if not VERSION.fullmatch(args.codex_version):
        parser.error("Select an exact Codex release version")
    if os.name == "nt":
        prefix = wsl_prefix(args.wsl_distribution)
        script = wsl_path(prefix, Path(__file__))
        with socket.create_server(("0.0.0.0", 0)) as listener:
            socket.create_connection(("127.0.0.1", listener.getsockname()[1]), timeout=2).close()
            print("WINDOWS_HOST_LISTENER_PRESENT", flush=True)
            command = [*prefix, "python3", script, "--codex-version", args.codex_version]
            for address in windows_addresses():
                command.extend(["--windows-host-ipv4", address])
            command.extend(["--windows-listener", str(listener.getsockname()[1])])
            return subprocess.run(command).returncode
    verify(args.windows_host_ipv4, args.windows_listener, codex_version=args.codex_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
