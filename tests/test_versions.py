import importlib.util
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
RELEASES = {"claude-code": "{}\n"}


class AgentVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.init = Path(self.tmp.name) / "__init__.py"
        shutil.copy(ROOT / "nexkit" / "__init__.py", self.init)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_each_pin(self):
        self.assertEqual(versions.current("claude-code"), nexkit.CLAUDE_CODE)
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
            versions.set_version("claude-code", "latest", self.init)
        with self.assertRaises(ValueError):
            versions.stable("claude-code", lambda url: "2.2.0-beta.1\n")
        self.assertTrue(versions.newer("2.10.0", "2.9.9"))


digest_spec = importlib.util.spec_from_file_location(
    "image_digest", ROOT / "scripts" / "image_digest.py"
)
digests = importlib.util.module_from_spec(digest_spec)
digest_spec.loader.exec_module(digests)


class ImageDigestTests(unittest.TestCase):
    """update-image.yml proposes PYTHON_IMAGE at its tag's current digest, and only then
    (#328)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.init = Path(self.tmp.name) / "__init__.py"
        shutil.copy(ROOT / "nexkit" / "__init__.py", self.init)

    def test_reads_the_pin(self):
        tag, digest = digests.current()
        self.assertEqual(f"{tag}@{digest}", nexkit.PYTHON_IMAGE)
        self.assertEqual(digests.current(self.init), (tag, digest))

    def test_update_changes_only_the_digest_and_only_when_it_moved(self):
        before = self.init.read_text()
        tag, pinned = digests.current(self.init)
        asked = []

        def lookup(digest):
            return lambda name: asked.append(name) or digest

        self.assertIsNone(digests.update(self.init, lookup(pinned)))
        self.assertEqual(self.init.read_text(), before)
        self.assertEqual(asked, [tag])

        moved = "sha256:" + "a" * 64
        self.assertEqual(digests.update(self.init, lookup(moved)), f"{tag}@{moved}")
        after = self.init.read_text()
        self.assertEqual(after, before.replace(pinned, moved))
        self.assertEqual(digests.current(self.init), (tag, moved))
        # Nothing else moved: the runtime pins and the line around the image stay.
        self.assertEqual(versions.current("claude-code", self.init), nexkit.CLAUDE_CODE)
        self.assertEqual(len(after.splitlines()), len(before.splitlines()))
        self.assertIsNone(digests.update(self.init, lookup(moved)))

    def test_refuses_what_is_not_a_digest(self):
        before = self.init.read_text()
        for answer in ("", None, "sha256:abc", "sha512:" + "a" * 128, "latest"):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                digests.update(self.init, lambda name, answer=answer: answer)
        self.assertEqual(self.init.read_text(), before)
        self.init.write_text(before.replace(nexkit.PYTHON_IMAGE, "python:3.12-slim"))
        with self.assertRaises(ValueError):
            digests.current(self.init)

    def test_official_images_live_under_library(self):
        self.assertEqual(digests._repository("python"), "library/python")
        self.assertEqual(digests._repository("acme/python"), "acme/python")


if __name__ == "__main__":
    unittest.main()
