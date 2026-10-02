"""Host planning and WSL lifecycle contracts; external tools are mocked here."""

import ctypes
import importlib.util
import io
import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nexkit import __version__, runner_host
from nexkit.adapters import adapter
from nexkit.common import Blocked, read_json, write_json
from tests.support import project, project_document

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manage_runner", ROOT / "scripts/manage_runner.py")
management = importlib.util.module_from_spec(spec)
spec.loader.exec_module(management)


def configuration():
    cfg = project()
    cfg["engine"]["auth"] = "chatgpt"
    cfg["environment"]["agent_runner"] = ["self-hosted", "linux", "x64", "project-runner"]
    return cfg


class RunnerHostTests(unittest.TestCase):
    def test_login_uses_the_provisioned_image_and_checks_cli_before_mounting_credentials(self):
        cfg = configuration()
        image = "sha256:" + "a" * 64
        bound = {
            "repository": cfg["repository"],
            "default_branch": cfg["default_branch"],
            "isolation": "container-v1",
            "agent": "codex",
            "policy_directory": "/opt/nexkit-runner/1.0.0",
            "image": image,
        }
        for compatible in (True, False):
            with (
                self.subTest(compatible=compatible),
                patch.object(management.sys, "platform", "linux"),
                patch.object(
                    management,
                    "read_regular_json",
                    side_effect=[bound, {"kind": "linux", "apparmor": False}],
                ),
                patch.object(
                    management,
                    "run",
                    side_effect=[
                        SimpleNamespace(stdout="inactive\n"),
                        SimpleNamespace(stdout=image),
                    ],
                ),
                patch.object(
                    adapter(cfg["engine"]),
                    "probe",
                    side_effect=None
                    if compatible
                    else Blocked("Missing required execution option"),
                ) as check_cli,
                patch.object(
                    management.subprocess, "run", return_value=SimpleNamespace(returncode=0)
                ) as login,
            ):
                if compatible:
                    self.assertEqual(management.operate("login", cfg), 0)
                    argv = login.call_args.args[0]
                    self.assertIn(image, argv)
                    self.assertNotIn("nexkit-runner:1.0.0", argv)
                    self.assertEqual(argv[-3:], ["codex", "login", "--device-auth"])
                    self.assertIn("seccomp=/opt/nexkit-runner/1.0.0/seccomp.json", argv)
                else:
                    with self.assertRaisesRegex(Blocked, "required execution option"):
                        management.operate("login", cfg)
                    login.assert_not_called()
                check_cli.assert_called_once_with(
                    ["docker", "run", "--rm", "--network", "none", "--entrypoint", "codex", image],
                )
                self.assertNotIn("--mount", check_cli.call_args.args[0])

    def test_login_rejects_a_changed_image_adapter_or_missing_sandbox_location(self):
        cfg = configuration()
        bound = {
            "repository": cfg["repository"],
            "default_branch": cfg["default_branch"],
            "isolation": "container-v1",
            "agent": "codex",
            "policy_directory": "/opt/nexkit-runner/1.0.0",
            "image": "sha256:" + "a" * 64,
        }
        for changed in (
            {"image": "sha256:" + "b" * 64},
            {"agent": "another"},
            {"image": None},
            {"agent": None},
            {"policy_directory": None},
            {"policy_directory": "/opt/nexkit-runner/../../etc"},
        ):
            with (
                self.subTest(changed=changed),
                patch.object(management.sys, "platform", "linux"),
                patch.object(
                    management,
                    "read_regular_json",
                    side_effect=[bound | changed, {"kind": "linux"}],
                ),
                patch.object(
                    management,
                    "run",
                    side_effect=[
                        SimpleNamespace(stdout="inactive"),
                        SimpleNamespace(stdout=bound["image"]),
                    ],
                ),
                patch.object(adapter(cfg["engine"]), "probe") as check_cli,
                patch.object(management.subprocess, "run") as login,
                self.assertRaises(Blocked),
            ):
                management.operate("login", cfg)
            check_cli.assert_not_called()
            login.assert_not_called()

    def test_optional_install_pin_only_selects_the_build_dependency(self):
        cfg = configuration()
        image, argv = runner_host.image_build(cfg)
        self.assertEqual(image, f"nexkit-runner:{__version__}-codex")
        self.assertNotIn("--build-arg", argv)
        cfg["engine"]["install"] = {"version": "1.2.3"}
        pinned_image, argv = runner_host.image_build(cfg)
        self.assertNotEqual(pinned_image, image)
        self.assertIn("CODEX_VERSION=1.2.3", argv)
        self.assertEqual(argv[-1], str(ROOT / "runner"))

    def test_one_container_identity_is_used_on_both_host_operating_systems(self):
        name, root, label = runner_host.runner_identity(configuration())
        self.assertTrue(name.startswith("nexkit-"))
        self.assertEqual(root, "/var/lib/nexkit-runners/" + name)
        self.assertEqual(label, "project-runner")
        changed = configuration()
        changed["repository"] = "owner/another"
        self.assertNotEqual(runner_host.runner_identity(changed)[0], name)

    def test_preview_requires_no_docker_wsl_login_or_machine_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "accepted project.json"
            write_json(config, project_document(configuration()))
            for host in ("linux", "windows-wsl"):
                result = subprocess.run(
                    [
                        os.sys.executable,
                        str(ROOT / "scripts/provision_runner.py"),
                        "--config",
                        str(config),
                        "--pipeline",
                        "maintenance",
                        "--host",
                        host,
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                value = json.loads(result.stdout)
                self.assertEqual(value["host"], host)
                self.assertEqual(value["agent_runner"][1], "linux")
                self.assertFalse(value["docker_desktop_required"])
                self.assertFalse(value["applied"])
                self.assertFalse(value["credentials_copied"])
                self.assertEqual(value["policy_files"], f"/opt/nexkit-runner/{__version__}")
                self.assertNotIn("codex_version", value)
                self.assertIn(value["image"], value["image_build"])

    def test_reviewed_image_preview_skips_building_default_dependencies(self):
        cfg = configuration()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "project.json"
            write_json(config, project_document(cfg))
            result = subprocess.run(
                [
                    os.sys.executable,
                    str(ROOT / "scripts/provision_runner.py"),
                    "--config",
                    str(config),
                    "--pipeline",
                    "maintenance",
                    "--image",
                    "reviewed-runner:custom",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        value = json.loads(result.stdout)
        self.assertEqual(value["image"], "reviewed-runner:custom")
        self.assertIsNone(value["image_build"])
        self.assertNotIn("codex_version", value)
        self.assertFalse(value["applied"])

    def test_windows_admin_uses_literal_wsl_arguments_and_validated_addresses(self):
        fake_os = SimpleNamespace(name="nt", environ={"SYSTEMROOT": "/windows"})
        with patch("nexkit.runner_host.os", fake_os):
            prefix = runner_host.wsl_prefix("Ubuntu-24.04")
            self.assertEqual(prefix[-3:], ["--user", "root", "--exec"])
            for value in ("", "--help", "Ubuntu;whoami", "Ubuntu\nother"):
                with self.assertRaises(Blocked):
                    runner_host.wsl_prefix(value)
            with patch(
                "nexkit.runner_host.run",
                return_value=SimpleNamespace(stdout="/mnt/c/project space/script.py\n"),
            ) as called:
                self.assertEqual(
                    runner_host.wsl_path(prefix, "project space/script.py"),
                    "/mnt/c/project space/script.py",
                )
                self.assertEqual(called.call_args.args[0][-4:-1], ["wslpath", "-a", "-u"])

            def query(buffer, size, ordered):
                value = struct.pack("<I", 2) + b"".join(
                    struct.pack("<4sIIIIHH", address, 0, 0, 0, 0, 0, 0)
                    for address in (b"\x7f\0\0\1", b"\xcb\0\x71\7")
                )
                ctypes.memmove(buffer, value, len(value))
                return 0

            with patch.object(
                ctypes,
                "WinDLL",
                return_value=SimpleNamespace(GetIpAddrTable=Mock(side_effect=query)),
                create=True,
            ):
                self.assertEqual(runner_host.windows_addresses(), ["127.0.0.1", "203.0.113.7"])

    def test_native_windows_daemon_or_another_docker_context_is_rejected(self):
        base = {
            "OSType": "linux",
            "Architecture": "x86_64",
            "KernelVersion": "accepted-kernel",
            "SecurityOptions": ["name=seccomp,profile=builtin"],
        }
        for changed in (
            {"OSType": "windows"},
            {"KernelVersion": "remote-kernel"},
            {"Architecture": "aarch64"},
            {"SecurityOptions": []},
            {"SecurityOptions": ["name=rootless", "name=seccomp"]},
        ):
            with (
                self.subTest(changed=changed),
                self.host_fixture(base | changed),
                self.assertRaises(Blocked),
            ):
                runner_host.inspect_host("linux")

    def test_host_checks_capabilities_without_requiring_one_ubuntu_version(self):
        info = {
            "OSType": "linux",
            "Architecture": "x86_64",
            "KernelVersion": "accepted-kernel",
            "SecurityOptions": ["name=seccomp,profile=builtin"],
        }
        for selected in ("22.04", "24.04", "26.04"):
            with (
                self.subTest(selected=selected),
                self.host_fixture(info),
                patch(
                    "nexkit.runner_host.platform.freedesktop_os_release",
                    return_value={"ID": "ubuntu", "VERSION_ID": selected},
                ),
            ):
                state = runner_host.inspect_host("linux")
                self.assertEqual(state["distribution"]["version"], selected)
        with (
            self.host_fixture(info),
            patch(
                "nexkit.runner_host.platform.freedesktop_os_release",
                return_value={"ID": "debian", "VERSION_ID": "13"},
            ),
            self.assertRaisesRegex(Blocked, "requires Ubuntu"),
        ):
            runner_host.inspect_host("linux")

    def host_fixture(self, info):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch("nexkit.runner_host.sys.platform", "linux"))
        stack.enter_context(patch("nexkit.runner_host.platform.machine", return_value="x86_64"))
        stack.enter_context(
            patch("nexkit.runner_host.platform.release", return_value="accepted-kernel")
        )
        stack.enter_context(
            patch(
                "nexkit.runner_host.platform.freedesktop_os_release",
                return_value={"ID": "ubuntu", "VERSION_ID": "24.04"},
            )
        )
        stack.enter_context(patch("nexkit.runner_host.host_kind", return_value="linux"))
        stack.enter_context(patch("nexkit.runner_host.local_engine"))
        stack.enter_context(patch("nexkit.runner_host.Path.read_text", return_value="systemd\n"))
        stack.enter_context(
            patch("nexkit.runner_host.run", return_value=SimpleNamespace(stdout=json.dumps(info)))
        )
        stack.enter_context(patch.dict(os.environ, {"DOCKER_HOST": ""}))
        return stack

    def test_system_engine_rejects_proxies_stopped_services_and_other_network_namespaces(self):
        for mode in ("local", "desktop-context", "desktop-proxy", "stopped", "other-namespace"):

            def command(argv):
                if argv[0] == "docker":
                    endpoint = (
                        "npipe://desktop"
                        if mode == "desktop-context"
                        else "unix:///run/docker.sock"
                    )
                    return SimpleNamespace(stdout=json.dumps(endpoint))
                if argv[0] == "systemctl":
                    return SimpleNamespace(stdout="0" if mode == "stopped" else "321")
                return SimpleNamespace(
                    stdout="1:2\n1:3\n" if mode == "other-namespace" else "1:2\n1:2\n"
                )

            with (
                self.subTest(mode=mode),
                patch.dict(os.environ, {"DOCKER_HOST": "", "DOCKER_CONTEXT": ""}),
                patch("nexkit.runner_host.run", side_effect=command),
                patch.object(
                    Path,
                    "resolve",
                    return_value=Path("/mnt/desktop.sock")
                    if mode == "desktop-proxy"
                    else Path("/run/docker.sock"),
                ),
            ):
                if mode == "local":
                    runner_host.local_engine()
                else:
                    with self.assertRaises(Blocked):
                        runner_host.local_engine()

    def test_kernel_support_selects_apparmor_without_removing_seccomp(self):
        info = {
            "OSType": "linux",
            "Architecture": "x86_64",
            "KernelVersion": "accepted-kernel",
            "SecurityOptions": ["name=seccomp,profile=builtin"],
        }
        for enabled in (False, True):
            with (
                self.subTest(enabled=enabled),
                self.host_fixture(
                    info
                    | {
                        "SecurityOptions": info["SecurityOptions"]
                        + (["name=apparmor"] if enabled else [])
                    }
                ),
            ):
                value = runner_host.inspect_host("linux")
                self.assertEqual(value["apparmor"], enabled)
                flags = runner_host.security_args(
                    value, policy_directory="/opt/nexkit-runner/1.0.0"
                )
                self.assertIn("seccomp=/opt/nexkit-runner/1.0.0/seccomp.json", flags)
                self.assertEqual("apparmor=nexkit-runner" in flags, enabled)

    def test_invalid_host_addresses_cannot_become_firewall_arguments(self):
        for value in ([], None, ["1.2.3.4;whoami"], ["::1"], [True], ["1.2.3.4"] * 257):
            with self.subTest(value=value), self.assertRaises(Blocked):
                runner_host.addresses(value)

    def test_wsl_supervisor_stops_on_eof_and_refreshes_before_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "host.json"
            write_json(path, {"schema": 1, "kind": "windows-wsl", "apparmor": False})
            messages = io.StringIO(
                '{"windows_host_ipv4":["203.0.113.7"]}\n{"windows_host_ipv4":["203.0.113.8"]}\n'
            )
            commands = []

            def command(argv, **kwargs):
                if argv[1] == "start":
                    commands.append((argv[1], read_json(path)["windows_host_ipv4"]))
                else:
                    commands.append((argv[1], None))
                return SimpleNamespace(stdout="active\n")

            with (
                patch.object(management.select, "select", return_value=([messages], [], [])),
                patch.object(management, "run", side_effect=command),
            ):
                management.supervise_wsl("consumer", path, stream=messages)
            self.assertEqual(
                [name for name, _ in commands],
                ["stop", "start", "is-active", "stop", "start", "is-active", "stop"],
            )
            self.assertEqual(
                [value for name, value in commands if name == "start"],
                [["203.0.113.7"], ["203.0.113.8"]],
            )

    def test_wsl_supervisor_missing_or_malformed_heartbeat_stops_the_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "host.json"
            write_json(path, {"schema": 1, "kind": "windows-wsl", "apparmor": False})
            for body, ready in (("{}\n", True), ("", False)):
                stream = io.StringIO(body)
                with (
                    self.subTest(body=body),
                    patch.object(
                        management.select,
                        "select",
                        return_value=([stream] if ready else [], [], []),
                    ),
                    patch.object(management, "run") as called,
                ):
                    with self.assertRaises((Blocked, KeyError)):
                        management.supervise_wsl("consumer", path, stream=stream)
                    self.assertEqual(
                        called.call_args.args[0], ["systemctl", "stop", "consumer.service"]
                    )
