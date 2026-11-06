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
from nexkit.config import MAX_CACHES
from tests.support import make_config, with_claude

ROOT = Path(__file__).resolve().parent.parent

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


def steps_of(path):
    """{step id: (`uses`, its text)} of a composite action."""
    found = {}
    for step in (ROOT / path).read_text().split("\n    - ")[1:]:
        name = re.search(r"^id: (\S+)", step, re.M)
        uses = re.search(r"uses: (\S+)", step)
        if name and uses:
            found[name.group(1)] = (uses.group(1), step)
    return found


def inputs_of(step):
    """The `with:` of a step, which decides its cache's key and version."""
    return dict(re.findall(r"^\s+(path|key|restore-keys): (.*)$", step, re.M))


class SavingTests(unittest.TestCase):
    def test_the_action_plans_the_key_and_paths_nexkit_restores(self):
        cfg = make_config(cache=[GRADLE, NPM, CRATES])
        restored = cache.plan(cfg, "repo", "Linux")
        saved = cache.plan(cfg, ".", "Linux")
        self.assertEqual(saved["count"], "3")
        for number in (1, 2, 3):
            for field in ("name", "paths", "prefix"):
                self.assertEqual(saved[f"{field}{number}"], restored[f"{field}{number}"])
            # The same files, from a checkout at the workspace instead of in `repo/`.
            self.assertEqual(
                saved[f"files{number}"].split("\n"),
                [line.replace("repo/", "", 1) for line in restored[f"files{number}"].split("\n")],
            )
        self.assertEqual(
            saved["files1"],
            "android/gradle/libs.versions.toml\nandroid/*.kts\n!android/build/**",
        )
        # In a folder, as NexKit's own jobs have it.
        self.assertEqual(cache.plan(cfg, "code", "Linux")["files2"], "code/web/package-lock.json")

    def test_each_step_of_the_action_uses_the_key_of_the_restore_step(self):
        restored = steps_of(".github/actions/restore-caches/action.yml")
        saved = steps_of("cache/action.yml")
        for number in range(1, MAX_CACHES + 1):
            wanted = inputs_of(restored[f"cache{number}"][1])
            self.assertEqual(len(wanted), 3)
            self.assertEqual(inputs_of(saved[f"save{number}"][1]), wanted)
            self.assertEqual(inputs_of(saved[f"cache{number}"][1]), wanted)
            self.assertTrue(saved[f"save{number}"][0].startswith("actions/cache@"))
            self.assertTrue(saved[f"cache{number}"][0].startswith("actions/cache/restore@"))
            # The same cache action, so the same version of a cache.
            self.assertEqual(
                saved[f"save{number}"][0].split("@")[1], restored[f"cache{number}"][0].split("@")[1]
            )

    def test_a_failed_cache_never_fails_the_run(self):
        text = (ROOT / "cache/action.yml").read_text()
        steps = text.split("\n    - ")[1:]
        self.assertEqual(len(steps), 2 + 2 * MAX_CACHES)
        for step in steps:
            self.assertIn("continue-on-error: true", step)

    def test_only_a_step_for_the_base_branch_saves(self):
        saved = steps_of("cache/action.yml")
        for number in range(1, MAX_CACHES + 1):
            self.assertIn("steps.plan.outputs.save == 'true'", saved[f"save{number}"][1])
            self.assertIn("steps.plan.outputs.save != 'true'", saved[f"cache{number}"][1])

    def test_it_saves_only_on_the_base_branch(self):
        self.assertTrue(cache.saves("push", "main", "main"))
        self.assertTrue(cache.saves("workflow_dispatch", "main", "main"))
        self.assertFalse(cache.saves("push", "feature", "main"))
        self.assertFalse(cache.saves("pull_request", "main", "main"))
        self.assertFalse(cache.saves("pull_request_target", "main", "main"))
        # A run that does not say which branch is the base one only restores.
        self.assertFalse(cache.saves("schedule", "main", ""))


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

    def test_the_action_reads_the_entries_from_the_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / ".nexkit/config.json"
            path.parent.mkdir()
            raw = with_claude({"cache": [NPM]})
            path.write_text(json.dumps(raw))
            argv = ["cache", "--save", "--repo", root, "--base", "main"]
            env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "main", "NEXKIT_CONFIG": ""}
            printed = self.run_cli(argv, **env)
            self.assertIn("Saves them at the end of the job: yes", printed)
            self.assertIn("Caches: npm", printed)
            outputs = self.outputs()
            self.assertEqual(outputs["save"], "true")
            self.assertEqual(outputs["prefix1"], "nexkit-Linux-npm-")
            self.assertEqual(outputs["paths1"], "~/.npm")
            self.output.write_text("")
            env["GITHUB_REF_NAME"] = "feature"
            self.assertIn("no, it only restores", self.run_cli(argv, **env))
            self.assertEqual(self.outputs()["save"], "false")

    def outputs(self):
        pattern = r"^(\w+)<<(EOF_\w+)\n(.*?)\n\2$"
        found = re.findall(pattern, self.output.read_text(), re.M | re.S)
        return {key: value for key, _, value in found}

    def test_report_reads_the_restore_steps(self):
        steps = {"cache1": restore_step(key="nexkit-Linux-npm-a")}
        printed = self.run_cli(["cache", "--report"], NEXKIT_CACHE_STEPS=json.dumps(steps))
        self.assertIn("npm: missed, no cache for nexkit-Linux-npm-a", printed)


if __name__ == "__main__":
    unittest.main()
