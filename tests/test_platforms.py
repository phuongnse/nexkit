"""Runner labels describe the shared Linux runtime, including on Windows hosts."""

import os
import unittest
from unittest.mock import patch

from nexkit.common import Blocked
from nexkit.platforms import execution_runner, require_native, runner_os


class PlatformTests(unittest.TestCase):
    def test_hosted_and_container_runners_use_linux_labels(self):
        for runner in ("ubuntu-22.04", "ubuntu-24.04", "ubuntu-26.04"):
            with self.subTest(runner=runner):
                self.assertEqual(runner_os(runner), "linux")
                self.assertEqual(execution_runner({"environment": {"runner": runner}}), runner)
        self.assertEqual(runner_os(["self-hosted", "linux", "x64", "my-project"]), "linux")

    def test_native_windows_and_ambiguous_runners_are_rejected(self):
        for value in (
            "windows-2025",
            "windows-latest",
            "ubuntu-latest",
            "ubuntu-24.04-arm",
            "ubuntu-24.04\n",
            "macos-15",
            None,
            {"group": "all"},
            ["self-hosted", "linux", "x64"],
            ["self-hosted", "windows", "x64", "my-project"],
            ["self-hosted", "windows", "linux", "x64", "my-project"],
            ["self-hosted", "linux", "arm64", "x64"],
            ["self-hosted", "linux", "x64", "x64"],
        ):
            with self.subTest(value=value), self.assertRaises(Blocked):
                runner_os(value)
        with self.assertRaises(Blocked):
            runner_os(["self-hosted", "linux", "x64", "my-project"], hosted_only=True)

    def test_native_windows_cannot_run_a_managed_linux_job(self):
        with (
            patch("nexkit.platforms.sys.platform", "win32"),
            patch("nexkit.platforms.platform.machine", return_value="AMD64"),
        ):
            with self.assertRaisesRegex(Blocked, "Linux container"):
                require_native("ubuntu-24.04")

    def test_managed_jobs_require_the_python_runtime_capability(self):
        with patch("nexkit.platforms.sys.version_info", (3, 10, 12)):
            with self.assertRaisesRegex(Blocked, "Python 3.11"):
                require_native("ubuntu-22.04")

    def test_linux_container_in_wsl_uses_linux_not_windows_labels(self):
        with (
            patch("nexkit.platforms.sys.platform", "linux"),
            patch("nexkit.platforms.platform.machine", return_value="x86_64"),
        ):
            with patch.dict(os.environ, {"RUNNER_OS": "Linux"}):
                self.assertEqual(
                    require_native(["self-hosted", "linux", "x64", "project"]), "linux"
                )
            with patch.dict(os.environ, {"RUNNER_OS": "Windows"}):
                with self.assertRaisesRegex(Blocked, "differs"):
                    require_native("ubuntu-24.04")
            with (
                patch("nexkit.platforms.platform.machine", return_value="ARM64"),
                self.assertRaises(Blocked),
            ):
                require_native("ubuntu-24.04")
