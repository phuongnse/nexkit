"""Shared native contracts. These tests run on both Linux and Windows."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nexkit.checks import execute, verify
from nexkit.common import Blocked, consumer_path, read_json, run, safe_path, write_json
from nexkit.release import build_release
from nexkit.step_io import INPUT, RESULT, transfer


class PortableCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nexkit native ")
        self.root = Path(self.temporary.name) / "project space \u00e9"
        self.root.mkdir()
        self.addCleanup(self.temporary.cleanup)

    def test_unicode_paths_arguments_environment_and_output(self):
        marker = "\u0110\u01b0\u1eddng d\u1eabn \U0001f600"
        script = (
            "import json, os, sys, tempfile; from pathlib import Path; "
            "assert 'GH_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ; "
            "assert 'CODEX_HOME' not in os.environ and 'PYTHONPATH' not in os.environ; "
            "assert Path(tempfile.gettempdir()).resolve().is_relative_to(Path.home().resolve()); "
            "print(json.dumps({'arg':sys.argv[1], 'cwd':str(Path.cwd())},ensure_ascii=False))"
        )
        with patch.dict(
            os.environ,
            {
                "GH_TOKEN": "test-only",
                "OPENAI_API_KEY": "test-only",
                "CODEX_HOME": "private",
                "PYTHONPATH": str(self.root),
            },
        ):
            result = execute([sys.executable, "-c", script, marker], self.root, 10)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(json.loads(result["log"]), {"arg": marker, "cwd": str(self.root)})

    def test_controller_queries_receive_eof_or_the_explicit_request(self):
        command = [sys.executable, "-I", "-c", "import sys; print(repr(sys.stdin.read()))"]
        self.assertEqual(run(command, timeout=5).stdout.strip(), "''")
        self.assertEqual(
            run(command, data="literal request", timeout=5).stdout.strip(), "'literal request'"
        )

    def test_process_tree_is_stopped_after_success_and_timeout(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                marker = self.root / f"survivor-{timeout}"
                child = (
                    "import time; from pathlib import Path; time.sleep(2); "
                    f"Path({str(marker)!r}).write_text('escaped')"
                )
                script = (
                    f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); "
                    + ("time.sleep(60)" if timeout else "time.sleep(0.1)")
                )
                result = execute([sys.executable, "-c", script], self.root, 1 if timeout else 10)
                self.assertEqual(result["timed_out"], timeout, result)
                self.assertEqual(result["exit_code"], 124 if timeout else 0, result)
                time.sleep(2.1)
                self.assertFalse(marker.exists(), "A command descendant survived collection")

    def test_binary_logs_do_not_crash_collection(self):
        result = execute(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff'*25000)"],
            self.root,
            10,
        )
        self.assertEqual(result["exit_code"], 0, result)
        self.assertLessEqual(len(result["log"]), 24000)

    def test_checks_and_release_use_the_configured_command_home(self):
        home = self.root.parent / "accepted home"
        home.mkdir()
        marker = home / "setup-ready"
        cfg = {
            "environment": {
                "setup": [
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path.home().joinpath('setup-ready').write_text('ready')",
                    ]
                ]
            },
            "limits": {"command_seconds": 10},
            "checks": [
                {
                    "name": name,
                    "kind": name,
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path.home().joinpath('setup-ready').read_text() == 'ready'",
                    ],
                    "timeout_seconds": 10,
                }
                for name in ("test", "e2e")
            ],
            "release": {
                "build": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path.home().joinpath('setup-ready').read_text() == 'ready'; Path('package.txt').write_text('built')",
                ],
                "artifacts": ["package.txt"],
            },
        }
        context = {
            "config": cfg,
            "release": {"commit": "b" * 40, "config": "c" * 64, "version": "1.0.0"},
            "run_key": "100.1",
        }
        with patch.dict(os.environ, {"NEXKIT_COMMAND_HOME": str(home)}):
            self.assertTrue(verify(cfg, self.root)["passed"])
            self.assertEqual(marker.read_text(), "ready")
            manifest = build_release(context, self.root, self.root.parent / "collected")
        self.assertTrue(manifest["verification"]["passed"])
        self.assertEqual(manifest["artifacts"][0]["name"], "package.txt")
        self.assertEqual((self.root.parent / "collected/package.txt").read_text(), "built")

    def test_step_files_are_utf8_and_results_are_bounded(self):
        value = {"description": "\u0110\u01b0\u1eddng d\u1eabn"}
        transfer("prepare", self.root, value)
        self.assertEqual(read_json(self.root / INPUT), value)
        self.assertFalse(transfer("result-exists", self.root))
        write_json(self.root / RESULT, {"status": "done", "summary": "Checked \u00e9"})
        self.assertEqual(transfer("read-result", self.root)["value"]["summary"], "Checked \u00e9")
        (self.root / RESULT).write_bytes(b" " * 48001)
        with self.assertRaisesRegex(Blocked, "48 KB"):
            transfer("read-result", self.root)

    def test_isolated_helper_uses_utf8_bytes_under_an_explicit_non_utf8_locale(self):
        helper = Path(__file__).resolve().parents[1] / "nexkit/step_io.py"
        driver = (
            "import runpy,sys; "
            "sys.stdin.reconfigure(encoding='cp1252',errors='surrogateescape'); "
            "sys.stdout.reconfigure(encoding='cp1252',errors='strict'); "
            "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
        )
        value = {"text": "\u0110\u01b0\u1eddng \U0001f600 " * 2000}
        prepared = subprocess.run(
            [sys.executable, "-I", "-c", driver, str(helper), "prepare", str(self.root)],
            input=json.dumps(value, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertIsNone(json.loads(prepared.stdout))
        self.assertEqual(read_json(self.root / INPUT), value)
        (self.root / RESULT).write_bytes((self.root / INPUT).read_bytes())
        collected = subprocess.run(
            [sys.executable, "-I", "-c", driver, str(helper), "read-result", str(self.root)],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(collected.returncode, 0, collected.stderr)
        self.assertEqual(json.loads(collected.stdout)["value"], value)

    def test_step_result_links_and_directories_are_rejected(self):
        outside = self.root.parent / "outside.json"
        write_json(outside, {"private": True})
        result = self.root / RESULT
        result.symlink_to(outside)
        with self.assertRaises(Blocked):
            transfer("read-result", self.root)
        result.unlink()
        os.link(outside, result)
        with self.assertRaises(Blocked):
            transfer("read-result", self.root)
        result.unlink()
        result.mkdir()
        with self.assertRaises(Blocked):
            transfer("read-result", self.root)

    def test_reserved_input_and_workspace_links_are_rejected(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        link = self.root.parent / "link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(Blocked):
            transfer("prepare", link, {})
        (self.root / INPUT).symlink_to(outside / "private")
        with self.assertRaises(Blocked):
            transfer("prepare", self.root, {})
        self.assertFalse((outside / "private").exists())

    def test_repository_names_have_the_same_meaning_on_both_platforms(self):
        for name in (
            "C:/outside",
            "file:stream",
            "NUL",
            "folder/COM1.txt",
            "a/../b",
            ".GIT/config",
            "a.",
            "a ",
        ):
            with self.subTest(name=name), self.assertRaises(Blocked):
                safe_path(name)
        self.assertEqual(safe_path("src/valid name.py"), "src/valid name.py")

    @unittest.skipUnless(os.name == "nt", "Windows junction boundary")
    def test_windows_junction_cannot_supply_a_workspace_or_consumer_path(self):
        target = self.root.parent / "outside"
        target.mkdir()
        junction = self.root / "junction"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
            check=True,
            capture_output=True,
        )
        with self.assertRaises(Blocked):
            consumer_path(self.root, "junction/private.json")
        with self.assertRaises(Blocked):
            transfer("prepare", junction, {})
        self.assertEqual(list(target.iterdir()), [])
