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

    def runs(self, ref="v1.0.0", repository=KIT_REPOSITORY):
        path = pin.cache_root() / repository / ref / "runs.json"
        return json.loads(path.read_text()) if path.exists() else []

    def test_status_runs_the_pinned_release_fetched_once(self):
        self.pin_to("v1.0.0")
        argv = ["status", "--repo", str(self.repo), "--json"]
        code, err = self.main(*argv)
        self.assertEqual(7, code)
        self.assertIn("fetching NexKit v1.0.0", err)
        self.assertIn(f"pins NexKit v1.0.0, so it runs from {pin.cache_root()}", err)
        [run] = self.runs()
        self.assertEqual({"argv": argv, "own": "1", "cwd": os.getcwd(), "release": "1.0.0"}, run)
        # The second time, it runs the copy in the cache, with no fetch.
        self.sources[KIT_REPOSITORY] = str(self.tmp / "gone")
        code, err = self.main(*argv)
        self.assertEqual(7, code)
        self.assertNotIn("fetching", err)
        self.assertIn("pins NexKit v1.0.0", err)
        self.assertEqual(2, len(self.runs()))

    def test_doctor_runs_the_pinned_release_too(self):
        self.pin_to("v1.0.0")
        self.assertEqual(7, self.main("doctor", f"--repo={self.repo}")[0])
        self.assertEqual([["doctor", f"--repo={self.repo}"]], [r["argv"] for r in self.runs()])

    def test_a_branch_is_fetched_again_each_time(self):
        self.pin_to("main")
        with mock.patch.object(pin.subprocess, "run", wraps=subprocess.run) as run:
            self.assertEqual(7, self.main("status", "--repo", str(self.repo))[0])
            self.release("main 2")
            self.assertEqual(7, self.main("status", "--repo", str(self.repo))[0])
        ran = [c.args[0][1] for c in run.call_args_list if c.args[0][0] != "git"]
        self.assertEqual(2, len(ran))
        self.assertFalse(Path(ran[0]).exists())  # each copy goes when its run ends
        self.assertFalse((pin.cache_root() / KIT_REPOSITORY / "main").exists())

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

    def test_a_ref_that_git_would_take_as_an_option_is_refused(self):
        self.pin_to("--upload-pack=touch")
        code, err = self.main("status", "--repo", str(self.repo))
        self.assertEqual(2, code)
        self.assertIn("which is not a ref", err)

    def test_a_ref_that_cannot_be_fetched_says_how_to_run_this_release(self):
        self.pin_to("v9.9.9")
        code, err = self.main("status", "--repo", str(self.repo))
        self.assertEqual(2, code)
        self.assertIn("cannot fetch NexKit v9.9.9", err)
        self.assertIn("--own-release", err)
        self.assertEqual([], list((pin.cache_root() / KIT_REPOSITORY).iterdir()))


if __name__ == "__main__":
    unittest.main()
