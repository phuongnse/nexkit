import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit import checks
from tests.support import GitRepos, git, make_config


class ChecksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_pass_and_fail(self):
        cfg = make_config(
            setup=["echo ready > setup.txt"],
            checks=[
                {"name": "unit", "run": "test -f setup.txt && echo fine"},
                {"name": "lint", "run": "echo bad; false | cat"},
            ],
        )
        results = checks.run_checks(cfg, self.root, self.root / "out")
        self.assertEqual([r["passed"] for r in results], [True, False])
        self.assertIn("fine", results[0]["output"])
        self.assertNotEqual(results[1]["exit_code"], 0)  # pipefail keeps the failure
        saved = json.loads((self.root / "out" / "checks.json").read_text())
        self.assertEqual(saved, results)

    def test_setup_failure_is_a_failed_check(self):
        cfg = make_config(setup=["exit 3"], checks=[{"name": "unit", "run": "true"}])
        results = checks.run_checks(cfg, self.root)
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]["name"], results[0]["exit_code"]), ("setup", 3))

    def test_timeout(self):
        code, output, _ = checks._run("sleep 30", self.root, 1)
        self.assertEqual(code, 124)
        self.assertIn("stopped the command", output)


class BaseCommitTests(unittest.TestCase):
    """NEXKIT_BASE_SHA: the merge base of the checked commit and the base branch."""

    def setUp(self):
        self.repos = GitRepos()
        self.fork = git(self.repos.root / "seed", "rev-parse", "HEAD")
        self.repos.push_commit("dev", "nexkit/issue-5", {"a.txt": "a\n"}, "A")
        # A base branch change after the merge base, which must not count as changed.
        self.repos.push_commit("dev", "main", {"b.txt": "b\n"}, "B")

    def tearDown(self):
        self.repos.cleanup()

    def quietly(self, function, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            return function(*args), stdout.getvalue()

    def test_finds_the_merge_base_in_a_full_checkout(self):
        repo = self.repos.clone("agent")
        git(repo, "checkout", "-q", "nexkit/issue-5")
        sha, log = self.quietly(checks.find_base, repo, "main")
        self.assertEqual(sha, self.fork)
        self.assertIn(f"NEXKIT_BASE_SHA={self.fork}", log)
        # The base branch's own later commit does not count as changed.
        self.assertEqual(git(repo, "diff", "--name-only", sha), "a.txt")
        sha, log = self.quietly(checks.find_base, repo, "missing")
        self.assertIsNone(sha)
        self.assertIn("NEXKIT_BASE_SHA is not set", log)

    def test_setup_and_checks_see_the_base_only_when_it_is_known(self):
        repo = self.repos.clone("checks")
        cfg = make_config(
            setup=['echo "${NEXKIT_BASE_SHA-unset}" > setup.txt'],
            checks=[{"name": "base", "run": 'cat setup.txt; echo "${NEXKIT_BASE_SHA-unset}"'}],
        )
        with mock.patch.dict(os.environ, {"NEXKIT_BASE_SHA": "stale"}):
            known = checks.run_checks(cfg, repo, base_sha=self.fork)
            unknown = checks.run_checks(cfg, repo)
        self.assertEqual(known[0]["output"].split(), [self.fork, self.fork])
        self.assertEqual(unknown[0]["output"].split(), ["unset", "unset"])


class SetupTimeoutTests(unittest.TestCase):
    def test_setup_uses_the_configured_timeout(self):
        cfg = make_config(setup=["true"], limits={"setup_timeout_minutes": 7})
        with (
            mock.patch.object(checks, "_run", return_value=(0, "", 0.1)) as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertIsNone(checks.setup(cfg, "."))
        self.assertEqual(run.call_args.args[2], 7 * 60)


if __name__ == "__main__":
    unittest.main()
