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

    def test_commands_never_see_agent_credentials(self):
        cfg = make_config(checks=[{"name": "env", "run": "env"}])
        secrets = {
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-for-checks-1",
            "ANTHROPIC_API_KEY": "key-for-checks-2",
            "PLAIN_NAMED_ENDPOINT": "endpoint-for-checks-3",
        }
        # Agent secrets by any name: the job lists them.
        names = {"NEXKIT_AGENT_SECRETS": '["PLAIN_NAMED_ENDPOINT"]'}
        with mock.patch.dict(os.environ, {**secrets, **names, "KEEP_ME": "1"}):
            output = checks.run_checks(cfg, self.root)[0]["output"]
        self.assertIn("KEEP_ME=1", output)
        for value in secrets.values():
            self.assertNotIn(value, output)

    def test_setup_failure_is_a_failed_check(self):
        cfg = make_config(setup=["exit 3"], checks=[{"name": "unit", "run": "true"}])
        results = checks.run_checks(cfg, self.root)
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]["name"], results[0]["exit_code"]), ("setup", 3))

    def test_timeout(self):
        code, output, _ = checks._run("sleep 30", self.root, 1)
        self.assertEqual(code, 124)
        self.assertIn("stopped the command", output)


class CommitCase(unittest.TestCase):
    """A NexKit branch that forked from the base branch at `self.fork`."""

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


class BaseCommitTests(CommitCase):
    """NEXKIT_BASE_SHA: the merge base of the checked commit and the base branch."""

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
            known = checks.run_checks(cfg, repo, commits={checks.BASE_SHA: self.fork})
            unknown = checks.run_checks(cfg, repo)
        self.assertEqual(known[0]["output"].split(), [self.fork, self.fork])
        self.assertEqual(unknown[0]["output"].split(), ["unset", "unset"])


class VerifiedCommitTests(CommitCase):
    """NEXKIT_VERIFIED_SHA: the last commit of the pull request whose checks all passed."""

    def find(self, repo, verified):
        decision = {"base": "main", "verified_sha": verified}
        return self.quietly(checks.find_commits, repo, decision)

    def test_is_the_decision_commit_when_the_checkout_has_it(self):
        passed = self.repos.push_commit("dev", "nexkit/issue-5", {"c.txt": "c\n"}, "C")
        repo = self.repos.clone("verify")
        git(repo, "checkout", "-q", "nexkit/issue-5")
        commits, log = self.find(repo, passed)
        self.assertEqual(commits, {checks.BASE_SHA: self.fork, checks.VERIFIED_SHA: passed})
        self.assertIn(f"NEXKIT_VERIFIED_SHA={passed}", log)

    def test_is_unset_without_an_earlier_passing_round(self):
        repo = self.repos.clone("verify")
        commits, log = self.find(repo, None)
        self.assertIsNone(commits[checks.VERIFIED_SHA])
        self.assertIn(
            "NEXKIT_VERIFIED_SHA is not set: no earlier round of this pull request passed its "
            "checks",
            log,
        )

    def test_is_unset_when_the_checkout_lacks_the_commit(self):
        repo = self.repos.clone("verify")
        commits, log = self.find(repo, "f" * 40)
        self.assertIsNone(commits[checks.VERIFIED_SHA])
        self.assertIn(
            "NEXKIT_VERIFIED_SHA is not set: the checkout does not have fffffff, the last "
            "commit whose checks passed",
            log,
        )

    def test_setup_and_every_check_see_it(self):
        repo = self.repos.clone("checks")
        cfg = make_config(
            setup=['echo "${NEXKIT_VERIFIED_SHA-unset}" > setup.txt'],
            checks=[
                {"name": name, "run": 'cat setup.txt; echo "${NEXKIT_VERIFIED_SHA-unset}"'}
                for name in ("unit", "integration")
            ],
        )
        with mock.patch.dict(os.environ, {"NEXKIT_VERIFIED_SHA": "stale"}):
            known = checks.run_checks(cfg, repo, commits={checks.VERIFIED_SHA: self.fork})
            unknown = checks.run_checks(cfg, repo, commits={checks.VERIFIED_SHA: None})
        self.assertEqual([r["output"].split() for r in known], [[self.fork, self.fork]] * 2)
        self.assertEqual([r["output"].split() for r in unknown], [["unset", "unset"]] * 2)


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
