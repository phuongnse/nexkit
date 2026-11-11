"""`nexkit status` and `nexkit doctor` run the release the repository's workflow pins."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from nexkit import __version__, cli, pin
from nexkit.scaffold import KIT_REPOSITORY, WORKFLOW_PATH, workflow_text

# A release's bin/nexkit: records how it ran, beside itself, and exits with 7.
FAKE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    from pathlib import Path
    here = Path(__file__).resolve().parent.parent
    runs = here / "runs.json"
    done = json.loads(runs.read_text()) if runs.exists() else []
    done.append({"argv": sys.argv[1:], "own": os.environ.get("NEXKIT_OWN_RELEASE"),
                 "cwd": os.getcwd(), "release": (here / "RELEASE").read_text()})
    runs.write_text(json.dumps(done))
    sys.exit(7)
    """
)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class PinTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # NexKit's repository, with a release tag and a branch.
        self.kit = self.tmp / "kit"
        (self.kit / "bin").mkdir(parents=True)
        (self.kit / "bin" / "nexkit").write_text(FAKE)
        git(self.kit, "init", "-q", "-b", "main")
        git(self.kit, "config", "user.email", "t@example.com")
        git(self.kit, "config", "user.name", "t")
        self.release("1.0.0")
        git(self.kit, "tag", "v1.0.0")
        self.release("main 1")
        self.sources = {KIT_REPOSITORY: str(self.kit)}
        patches = [
            mock.patch.object(pin, "source", lambda repository: self.sources[repository]),
            mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache")}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for name in (pin.OWN, pin.TRUST):
            os.environ.pop(name, None)
        self.repo = self.tmp / "repo"

    def release(self, text):
        (self.kit / "RELEASE").write_text(text)
        git(self.kit, "add", "-A")
        git(self.kit, "commit", "-q", "-m", text)

    def pin_to(self, ref, repository=KIT_REPOSITORY):
        path = self.repo / WORKFLOW_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(workflow_text(repository, ref))

    def main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, err.getvalue()

    def runs(self, repository=KIT_REPOSITORY):
        """How each release in the cache from `repository` ran."""
        found = []
        for path in sorted((pin.cache_root() / repository).rglob("runs.json")):
            found += json.loads(path.read_text())
        return found

    def ran_this_release(self, *argv):
        with mock.patch.object(cli, f"cmd_{argv[0]}", return_value=0) as command:
            code, err = self.main(*argv)
        self.assertEqual(0, code)
        command.assert_called_once()
        return err

    def test_status_runs_the_pinned_release_fetched_once(self):
        self.pin_to("v1.0.0")
        argv = ["status", "--repo", str(self.repo), "--json"]
        code, err = self.main(*argv)
        self.assertEqual(7, code)
        self.assertIn("fetched NexKit v1.0.0", err)
        folder = pin.cache_root() / KIT_REPOSITORY / "v1.0.0"
        self.assertIn(f"pins NexKit v1.0.0, so it runs from {folder}", err)
        [run] = self.runs()
        self.assertEqual({"argv": argv, "own": "1", "cwd": os.getcwd(), "release": "1.0.0"}, run)
        # The second time, it runs the copy in the cache, with no fetch.
        self.sources[KIT_REPOSITORY] = str(self.tmp / "gone")
        code, err = self.main(*argv)
        self.assertEqual(7, code)
        self.assertNotIn("fetched", err)
        self.assertIn("pins NexKit v1.0.0", err)
        self.assertEqual(2, len(self.runs()))

    def test_doctor_runs_the_pinned_release_too(self):
        self.pin_to("v1.0.0")
        self.assertEqual(7, self.main("doctor", f"--repo={self.repo}")[0])
        self.assertEqual([["doctor", f"--repo={self.repo}"]], [r["argv"] for r in self.runs()])

    def test_the_checkout_around_the_current_directory_counts_and_the_last_repo(self):
        self.pin_to("v1.0.0")
        git(self.repo, "init", "-q")
        inside = self.repo / "src" / "deep"
        inside.mkdir(parents=True)
        here = os.getcwd()
        os.chdir(inside)
        self.addCleanup(os.chdir, here)
        self.assertEqual((KIT_REPOSITORY, "v1.0.0"), pin.pinned(["status"]))
        self.assertIsNone(pin.pinned(["status", "--repo", str(self.repo), "--repo", "/none"]))
        self.assertIsNotNone(pin.pinned(["status", "--repo", "/none", "--repo", str(self.repo)]))

    def test_a_branch_is_fetched_again_when_it_moved(self):
        self.pin_to("main")
        argv = ("status", "--repo", str(self.repo))
        code, err = self.main(*argv)
        self.assertEqual(7, code)
        self.assertIn("fetched NexKit main", err)
        code, err = self.main(*argv)
        self.assertNotIn("fetched", err)
        self.release("main 2")
        code, err = self.main(*argv)
        self.assertIn("fetched NexKit main", err)
        self.assertEqual(["main 2"], [r["release"] for r in self.runs()])
        # The copy of the older commit went; the newer stays for the docs it holds.
        [copy] = (pin.cache_root() / KIT_REPOSITORY / "heads" / "main").iterdir()
        self.assertEqual("main 2", (copy / "RELEASE").read_text())
        self.assertIn(str(copy), err)

    def test_only_tags_and_branches_are_fetched_never_a_pull_request_or_commit(self):
        # Anyone can open a pull request on NexKit's repository, whose ref and commits
        # GitHub serves as NexKit's.
        (self.kit / "RELEASE").write_text("an outsider's")
        git(self.kit, "commit", "-q", "-am", "outsider")
        outsider = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.kit, capture_output=True, text=True
        ).stdout.strip()
        git(self.kit, "update-ref", "refs/pull/1/head", outsider)
        git(self.kit, "reset", "-q", "--hard", "HEAD~1")
        self.pin_to("pull/1/head")
        code, err = self.main("status", "--repo", str(self.repo))
        self.assertEqual(2, code)
        self.assertIn("has no tag or branch pull/1/head", err)
        for ref in ("refs/pull/1/head", outsider, "--upload-pack=touch", "a/../b", "${{"):
            self.pin_to(ref)
            err = self.ran_this_release("status", "--repo", str(self.repo))
            self.assertIn("which is no release tag or branch, so this NexKit", err)
        self.assertEqual([], self.runs())

    def test_doctor_still_checks_a_workflow_whose_ref_is_an_expression(self):
        self.pin_to("${{ vars.NEXKIT_REF }}")
        self.ran_this_release("doctor", "--repo", str(self.repo))

    def test_this_release_runs_when_the_pin_is_its_own_version(self):
        self.pin_to(f"v{__version__}")
        self.assertIsNone(pin.pinned(["status", "--repo", str(self.repo)]))

    def test_this_release_runs_outside_a_repository_with_a_nexkit_workflow(self):
        self.assertIsNone(pin.pinned(["status", "--repo", str(self.repo)]))
        path = self.repo / ".github" / "workflows" / "nexkit.yml"
        path.parent.mkdir(parents=True)
        path.write_text("on: push\njobs: {}\n")
        self.assertIsNone(pin.pinned(["doctor", "--repo", str(self.repo)]))

    def test_this_release_runs_init_and_the_pipeline_commands(self):
        self.pin_to("v1.0.0")
        for command in ("init", "route", "agent", "publish", "report", "maintain"):
            self.assertIsNone(pin.pinned([command, "--repo", str(self.repo)]))
        self.assertIsNone(pin.pinned(["--version"]))

    def test_the_flag_and_the_environment_run_this_release(self):
        self.pin_to("v1.0.0")
        argv = ["status", "--repo", str(self.repo)]
        self.assertIsNone(pin.pinned([*argv, "--own-release"]))
        self.assertIsNone(pin.pinned([*argv, "--own"]))  # as argparse takes it
        with mock.patch.dict(os.environ, {pin.OWN: "1"}):
            self.assertIsNone(pin.pinned(argv))
        with mock.patch.dict(os.environ, {pin.OWN: "0"}):
            self.assertEqual((KIT_REPOSITORY, "v1.0.0"), pin.pinned(argv))
        # The real parser takes the flag.
        with mock.patch.object(cli, "cmd_doctor", return_value=0) as doctor:
            self.assertEqual(0, self.main("doctor", "--repo", str(self.repo), "--own-release")[0])
        self.assertTrue(doctor.call_args.args[0].own_release)
        self.assertEqual([], self.runs())

    def test_another_repository_runs_nothing_without_the_opt_in(self):
        self.pin_to("v1.0.0", repository="someone/fork")
        self.sources["someone/fork"] = str(self.kit)
        code, err = self.main("status", "--repo", str(self.repo))
        self.assertEqual(2, code)
        self.assertIn("from someone/fork at v1.0.0", err)
        self.assertIn(f"set {pin.TRUST}=someone/fork", err)
        self.assertIn("--own-release", err)
        self.assertFalse(pin.cache_root().exists())
        # Even at this release's own version: that tag of the fork is another repository's code.
        self.pin_to(f"v{__version__}", repository="someone/fork")
        self.assertEqual(2, self.main("status", "--repo", str(self.repo))[0])
        # With the opt-in, its code runs.
        self.pin_to("v1.0.0", repository="someone/fork")
        with mock.patch.dict(os.environ, {pin.TRUST: "Someone/Fork"}):
            self.assertEqual(7, self.main("status", "--repo", str(self.repo))[0])
        self.assertEqual(1, len(self.runs(repository="someone/fork")))

    def test_git_works_in_the_cache_whatever_repository_the_caller_names(self):
        self.pin_to("v1.0.0")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.repo / "nowhere")}):
            self.assertEqual(7, self.main("status", "--repo", str(self.repo))[0])

    def test_a_ref_that_cannot_be_fetched_says_how_to_run_this_release(self):
        self.pin_to("v9.9.9")
        code, err = self.main("status", "--repo", str(self.repo))
        self.assertEqual(2, code)
        self.assertIn("cannot fetch NexKit v9.9.9", err)
        self.assertIn("--own-release", err)
        self.assertEqual([], list((pin.cache_root() / KIT_REPOSITORY).iterdir()))


if __name__ == "__main__":
    unittest.main()
