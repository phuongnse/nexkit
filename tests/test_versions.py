import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import nexkit
from nexkit.runtimes import RUNTIMES

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "agent_version", ROOT / "scripts" / "agent_version.py"
)
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)
# What each runtime's release URL answers.
RELEASES = {"claude-code": "{}\n", "codex": json.dumps({"name": "@openai/codex", "version": "{}"})}


class AgentVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.init = Path(self.tmp.name) / "__init__.py"
        shutil.copy(ROOT / "nexkit" / "__init__.py", self.init)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_each_pin(self):
        self.assertEqual(versions.current("claude-code"), nexkit.CLAUDE_CODE)
        self.assertEqual(versions.current("codex"), nexkit.CODEX)
        self.assertEqual(set(RELEASES), set(RUNTIMES))

    def test_update_only_moves_forward(self):
        for runtime, answer in RELEASES.items():
            with self.subTest(runtime=runtime):
                shutil.copy(ROOT / "nexkit" / "__init__.py", self.init)
                pinned = versions.current(runtime, self.init)
                major, minor, patch = map(int, pinned.split("."))
                newer = f"{major}.{minor}.{patch + 1}"
                older = f"{major}.{minor}.{patch - 1}" if patch else f"{major}.{minor - 1}.99"

                def fetch(version, answer=answer):
                    return lambda url: answer.replace("{}", version)

                self.assertIsNone(versions.update(runtime, self.init, fetch(pinned)))
                self.assertIsNone(versions.update(runtime, self.init, fetch(older)))
                self.assertEqual(versions.current(runtime, self.init), pinned)
                self.assertEqual(versions.update(runtime, self.init, fetch(newer)), newer)
                self.assertEqual(versions.current(runtime, self.init), newer)
                self.assertIn(f'{RUNTIMES[runtime].pin} = "{newer}"', self.init.read_text())
                for other in set(RUNTIMES) - {runtime}:
                    self.assertEqual(versions.current(other, self.init), versions.current(other))

    def test_rejects_non_versions(self):
        with self.assertRaises(ValueError):
            versions.set_version("codex", "latest", self.init)
        with self.assertRaises(ValueError):
            versions.stable("codex", lambda url: '{"version": "0.163.0-alpha.1"}')
        self.assertTrue(versions.newer("2.10.0", "2.9.9"))


if __name__ == "__main__":
    unittest.main()
