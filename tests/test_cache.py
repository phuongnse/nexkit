import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit import cache, cli
from tests.support import make_config

GRADLE = {
    "name": "gradle",
    "paths": ["~/.gradle/caches", "~/.gradle/wrapper"],
    "key_files": ["android/gradle/libs.versions.toml", "./android/*.kts", "!android/build/**"],
}
NPM = {"name": "npm", "paths": ["~/.npm"], "key_files": ["web/package-lock.json"]}
CRATES = {"name": "crates", "paths": ["~/.cargo/registry"], "key_files": ["core/Cargo.lock"]}


def restore_step(outcome="success", key="", matched="", hit=""):
    """A restore step in the `steps` context, as actions/cache/restore leaves it."""
    outputs = {"cache-primary-key": key} if key else {}
    if matched:
        outputs.update({"cache-matched-key": matched, "cache-hit": hit or "false"})
    return {"outcome": outcome, "conclusion": "success", "outputs": outputs}


class CacheTests(unittest.TestCase):
    def test_plan_names_each_entry_for_its_restore_step(self):
        outputs = cache.plan(make_config(cache=[GRADLE, NPM]), "repo", "Linux")
        self.assertEqual(outputs["count"], "2")
        self.assertEqual(outputs["name1"], "gradle")
        self.assertEqual(outputs["paths1"], "~/.gradle/caches\n~/.gradle/wrapper")
        self.assertEqual(outputs["prefix1"], "nexkit-Linux-gradle-")
        # hashFiles reads patterns from the workspace, where the checkout is `repo/`.
        self.assertEqual(
            outputs["files1"],
            "repo/android/gradle/libs.versions.toml\nrepo/android/*.kts\n!repo/android/build/**",
        )
        self.assertEqual(outputs["prefix2"], "nexkit-Linux-npm-")
        self.assertNotIn("name3", outputs)
        self.assertEqual(cache.plan(make_config(), "repo", "Linux"), {"count": "0"})

    def test_report_says_which_entries_were_restored(self):
        gradle = "nexkit-Linux-gradle-a"
        steps = {
            "plan": {"outcome": "success", "outputs": {"count": "3"}},
            "cache1": restore_step(key=gradle, matched=gradle, hit="true"),
            "cache2": restore_step(key="nexkit-Linux-npm-b", matched="nexkit-Linux-npm-old"),
            "cache3": restore_step(key="nexkit-Linux-crates-c"),
        }
        lines = cache.report(make_config(cache=[GRADLE, NPM, CRATES]), steps)
        self.assertEqual(
            lines[:4],
            [
                "gradle: restored nexkit-Linux-gradle-a",
                "npm: restored nexkit-Linux-npm-old, an older cache than nexkit-Linux-npm-b",
                "crates: missed, no cache for nexkit-Linux-crates-c or an older key",
                "Caches: 2 restored, 1 missed. Setup installs what is missing.",
            ],
        )
        self.assertIn(cache.DOCS, lines[4])

    def test_a_failed_restore_counts_as_a_miss(self):
        steps = {"cache1": restore_step(outcome="failure")}
        lines = cache.report(make_config(cache=[NPM]), steps)
        self.assertEqual(lines[0], "npm: not restored, the restore step failed")
        self.assertIn("0 restored, 1 missed", lines[1])
        # Every entry restored: no pointer to the docs.
        steps = {"cache1": restore_step(key="k", matched="k", hit="true")}
        self.assertEqual(len(cache.report(make_config(cache=[NPM]), steps)), 2)


class CacheCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / "output.txt"
        self.env = {
            "NEXKIT_CONFIG": json.dumps(make_config(cache=[NPM])),
            "GITHUB_OUTPUT": str(self.output),
            "RUNNER_OS": "Linux",
        }

    def run_cli(self, argv, **env):
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, **env}), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(argv), 0)
        return stdout.getvalue()

    def test_plan_writes_step_outputs(self):
        self.assertIn("Caches to restore: npm", self.run_cli(["cache", "--repo", "repo"]))
        pattern = r"^(\w+)<<(EOF_\w+)\n(.*?)\n\2$"
        found = re.findall(pattern, self.output.read_text(), re.M | re.S)
        outputs = {key: value for key, _, value in found}
        self.assertEqual(outputs["prefix1"], "nexkit-Linux-npm-")
        self.assertEqual(outputs["files1"], "repo/web/package-lock.json")

    def test_report_reads_the_restore_steps(self):
        steps = {"cache1": restore_step(key="nexkit-Linux-npm-a")}
        printed = self.run_cli(["cache", "--report"], NEXKIT_CACHE_STEPS=json.dumps(steps))
        self.assertIn("npm: missed, no cache for nexkit-Linux-npm-a", printed)


if __name__ == "__main__":
    unittest.main()
