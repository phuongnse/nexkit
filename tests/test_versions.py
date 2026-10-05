import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

import nexkit

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "claude_code_version", ROOT / "scripts" / "claude_code_version.py"
)
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)


class ClaudeCodeVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.init = Path(self.tmp.name) / "__init__.py"
        shutil.copy(ROOT / "nexkit" / "__init__.py", self.init)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_the_pin(self):
        self.assertEqual(versions.current(), nexkit.CLAUDE_CODE)

    def test_update_only_moves_forward(self):
        pinned = versions.current(self.init)
        major, minor, patch = map(int, pinned.split("."))
        newer = f"{major}.{minor}.{patch + 1}"
        older = f"{major}.{minor}.{patch - 1}" if patch else f"{major}.{minor - 1}.99"
        self.assertIsNone(versions.update(self.init, fetch=lambda _: pinned))
        self.assertIsNone(versions.update(self.init, fetch=lambda _: older))
        self.assertEqual(versions.current(self.init), pinned)
        self.assertEqual(versions.update(self.init, fetch=lambda _: newer), newer)
        self.assertEqual(versions.current(self.init), newer)
        self.assertIn(f'CLAUDE_CODE = "{newer}"', self.init.read_text())

    def test_rejects_non_versions(self):
        with self.assertRaises(ValueError):
            versions.set_version("latest", self.init)
        self.assertTrue(versions.newer("2.10.0", "2.9.9"))


if __name__ == "__main__":
    unittest.main()
