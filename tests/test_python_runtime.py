"""Controller runtime contracts; loader mutations are explicitly simulated."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexkit.common import Blocked
from nexkit.python_runtime import matches, prepare, probe


def result(*, version=None, code=0, error=""):
    identity = (sys.executable, version or sys.version, sys.prefix, sys.base_prefix)
    return subprocess.CompletedProcess([], code, repr(identity), error)


class PythonRuntimeTests(unittest.TestCase):
    def test_probe_uses_the_actual_interpreter_and_no_loader_or_credentials_environment(self):
        with patch("nexkit.python_runtime.run", return_value=result()) as run:
            probe()
        argv = run.call_args.args[0]
        self.assertEqual(
            argv[:6], ["sudo", "-n", "env", "-i", "PATH=/usr/bin:/bin", sys.executable]
        )
        self.assertIn("-I", argv)

    def test_a_successful_process_using_another_library_is_not_the_same_runtime(self):
        self.assertFalse(matches(result(version="another Python library")))
        self.assertFalse(matches(result(code=127)))
        self.assertTrue(matches(result()))

    def test_matching_runtime_does_not_change_the_loader(self):
        with (
            patch("nexkit.python_runtime.sys.platform", "linux"),
            patch("nexkit.python_runtime.probe", return_value=result()),
            patch("nexkit.python_runtime.run") as run,
        ):
            self.assertTrue(prepare()["sudo_clean_environment"])
        run.assert_not_called()

    def test_permission_failure_cannot_authorize_a_loader_change(self):
        with (
            patch("nexkit.python_runtime.sys.platform", "linux"),
            patch(
                "nexkit.python_runtime.probe",
                return_value=result(code=1, error="sudo: a password is required"),
            ),
            patch("nexkit.python_runtime.run") as run,
            self.assertRaises(Blocked),
        ):
            prepare()
        run.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Linux library ownership and permissions")
    def test_relocated_runtime_uses_its_own_library_when_build_prefix_is_stale(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            root = Path(temporary)
            library = root / "relocated" / "lib"
            library.parent.mkdir(mode=0o755)
            library.mkdir(mode=0o755)
            name = "libpython-fixture.so"
            (library / name).write_text("Harmless relocated runtime")
            (library / name).chmod(0o644)
            stale = root / "original" / "lib"
            values = {"LIBDIR": str(stale), "LDLIBRARY": name, "Py_ENABLE_SHARED": 1}
            configured = []

            def loader_command(command):
                if command[2] == "install":
                    configured.append(Path(command[-2]).read_text())

            for existing_build in (False, True):
                if existing_build:
                    stale.mkdir(parents=True)
                    (stale / name).write_text("Harmless different runtime")
                with (
                    self.subTest(existing_build=existing_build),
                    patch("nexkit.python_runtime.sys.base_prefix", str(library.parent)),
                    patch(
                        "nexkit.python_runtime.probe",
                        side_effect=[result(code=127, error="libpython is missing"), result()],
                    ),
                    patch("nexkit.python_runtime.sysconfig.get_config_var", side_effect=values.get),
                    patch("nexkit.python_runtime.run", side_effect=loader_command),
                ):
                    prepare()
                self.assertEqual(configured[-1], str(library) + "\n")

    @unittest.skipUnless(os.name == "posix", "Linux library ownership and permissions")
    def test_shared_library_repair_rechecks_identity_and_rejects_consumer_writes(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            library = Path(temporary)
            shared = library / "libpython-fixture.so"
            shared.write_text("Harmless simulated library")
            shared.chmod(0o644)
            values = {"LIBDIR": str(library), "LDLIBRARY": shared.name, "Py_ENABLE_SHARED": 1}
            for initial in (
                result(code=127, error="libpython3.12.so.1.0: cannot open shared object file"),
                result(version="another Python library"),
            ):
                with (
                    self.subTest(initial=initial.returncode),
                    patch(
                        "nexkit.python_runtime.probe", side_effect=[initial, result()]
                    ) as checked,
                    patch("nexkit.python_runtime.sysconfig.get_config_var", side_effect=values.get),
                    patch("nexkit.python_runtime.run") as run,
                ):
                    prepare()
                    self.assertEqual(checked.call_count, 2)
                    self.assertEqual(run.call_args.args[0], ["sudo", "-n", "/sbin/ldconfig"])
            for exposed in (shared, library):
                original = exposed.stat().st_mode & 0o777
                exposed.chmod(0o777)
                try:
                    with (
                        self.subTest(exposed=exposed.name),
                        patch(
                            "nexkit.python_runtime.probe",
                            return_value=result(code=127, error="libpython is missing"),
                        ),
                        patch(
                            "nexkit.python_runtime.sysconfig.get_config_var", side_effect=values.get
                        ),
                        patch("nexkit.python_runtime.run") as run,
                        self.assertRaises(Blocked),
                    ):
                        prepare()
                    run.assert_not_called()
                finally:
                    exposed.chmod(original)
