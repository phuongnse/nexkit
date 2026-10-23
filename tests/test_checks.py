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


TOKEN = "ghp_" + "a" * 36


class ResultFilesTests(unittest.TestCase):
    """A failing check keeps the files it names in `results` for the next fix round."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.out = self.root / "out"
        self.summary = self.root / "summary.md"
        env = mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def files(self, files):
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)

    def run_checks(self, checks_cfg):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            results = checks.run_checks(make_config(checks=checks_cfg), self.repo, self.out)
        return results, stdout.getvalue()

    def test_a_failing_check_keeps_its_result_files_redacted(self):
        binary = b"PK\x03\x04\x00\x00" + TOKEN.encode() + b"\xff\xfe"
        self.files(
            {
                "test-results/e2e/trace.zip": binary,
                "test-results/e2e/server.log": f"login with {TOKEN}\nselect failed\n",
                "logs/app.log": "started\n",
                "junit.xml": "<testsuite/>",
            }
        )
        results, log = self.run_checks(
            [
                {"name": "e2e", "run": "exit 1", "results": ["test-results", "logs/app.log"]},
                {"name": "unit", "run": "true", "results": ["junit.xml"]},
                {"name": "lint", "run": "exit 2"},
            ]
        )
        kept = self.out / checks.RESULTS_DIR / "e2e"
        log_file = (kept / "test-results/e2e/server.log").read_text()
        self.assertEqual(log_file, "login with ***\nselect failed\n")
        # Binary files are kept as they are.
        self.assertEqual((kept / "test-results/e2e/trace.zip").read_bytes(), binary)
        self.assertEqual((kept / "logs/app.log").read_text(), "started\n")
        # A passing check keeps nothing, and a check without `results` keeps nothing.
        self.assertEqual(sorted(p.name for p in (self.out / checks.RESULTS_DIR).iterdir()), ["e2e"])
        saved = json.loads((self.out / "checks.json").read_text())
        self.assertEqual(saved, results)
        self.assertEqual(saved[0]["results"], ["test-results", "logs/app.log"])
        self.assertEqual(saved[0]["results_left_out"], 0)
        self.assertNotIn("results", saved[1])
        self.assertNotIn("results", saved[2])
        self.assertIn("e2e: kept 2 of 2 files of `test-results`.", log)
        self.assertIn("Kept 3 files", self.summary.read_text())

    def test_a_passing_round_keeps_nothing(self):
        self.files({"junit.xml": "<testsuite/>"})
        self.run_checks([{"name": "unit", "run": "true", "results": ["junit.xml"]}])
        self.assertFalse((self.out / checks.RESULTS_DIR).exists())
        self.assertFalse(self.summary.exists())

    def test_a_missing_path_is_skipped(self):
        self.files({"logs/app.log": "started\n"})
        results, log = self.run_checks(
            [{"name": "e2e", "run": "exit 1", "results": ["test-results", "logs"]}]
        )
        self.assertEqual(results[0]["results"], ["logs"])
        self.assertTrue((self.out / checks.RESULTS_DIR / "e2e/logs/app.log").is_file())
        self.assertFalse((self.out / checks.RESULTS_DIR / "e2e/test-results").exists())
        self.assertIn("e2e: `test-results` was not found; skipped.", log)
        summary = self.summary.read_text()
        self.assertIn("Kept 1 file (0.0 MB) of the failing checks", summary)
        self.assertIn("**Not found**\n\n```\ne2e: test-results\n```", summary)

    def test_links_that_leave_the_checkout_are_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("not for the artifact\n")
        self.files({"logs/app.log": "started\n"})
        (self.repo / "logs/elsewhere").symlink_to(outside)
        (self.repo / "logs/secret.txt").symlink_to(outside / "secret.txt")
        (self.repo / "linked").symlink_to(outside)
        results, _ = self.run_checks(
            [{"name": "e2e", "run": "exit 1", "results": ["logs", "linked"]}]
        )
        kept = sorted(
            p.relative_to(self.out).as_posix()
            for p in (self.out / checks.RESULTS_DIR).rglob("*")
            if p.is_file()
        )
        self.assertEqual(kept, ["results/e2e/logs/app.log"])
        self.assertEqual(results[0]["results"], ["logs"])

    def test_the_size_limit_leaves_files_out_with_a_note_in_the_run_summary(self):
        self.files(
            {
                "reports/a.txt": "123456",
                "reports/b.txt": "1234567",
                "reports/c.txt": "123",
                "logs/z.txt": "1",
            }
        )
        with mock.patch.object(checks, "RESULTS_LIMIT", 10):
            results, _ = self.run_checks(
                [
                    {"name": "e2e", "run": "exit 1", "results": ["reports"]},
                    {"name": "api", "run": "exit 1", "results": ["logs"]},
                ]
            )
        kept = self.out / checks.RESULTS_DIR
        # In check order, then path order: b.txt does not fit after a.txt; c.txt and z.txt do.
        self.assertTrue((kept / "e2e/reports/a.txt").is_file())
        self.assertFalse((kept / "e2e/reports/b.txt").exists())
        self.assertTrue((kept / "e2e/reports/c.txt").is_file())
        self.assertTrue((kept / "api/logs/z.txt").is_file())
        self.assertEqual([r["results_left_out"] for r in results], [1, 0])
        summary = self.summary.read_text()
        self.assertIn("**Left out** (1): a round keeps at most 0.0 MB", summary)
        self.assertIn("e2e: reports/b.txt (0.0 MB)", summary)


class FailureSignatureTests(unittest.TestCase):
    """The same failure has the same signature in two runs; another failure does not."""

    def test_the_first_failing_line_without_what_changes_between_runs(self):
        first = (
            "Running 42 tests using 4 workers\n"
            "  ✓ 1 [chromium] › login.spec.ts:10:5 › signs in (1.2s)\n"
            "  ✘ 2 [chromium] › desk.spec.ts:42:5 › picks an option (12.3s)\n"
            "  1 failed\n"
        )
        again = first.replace("12.3s", "9.81s").replace("42 tests", "43 tests")
        signature = checks.failure_signature(first)
        self.assertEqual(signature, "✘ [chromium] › desk.spec.ts:: › picks an option ()")
        self.assertEqual(checks.failure_signature(again), signature)
        other = first.replace("picks an option", "saves the ticket")
        self.assertNotEqual(checks.failure_signature(other), signature)

    def test_ids_and_durations_do_not_count(self):
        first = "Error: request 3f2a9c1e7 failed after 1500ms (trace 0x7ffe12)\n"
        again = "Error: request 9b8c7d6e5 failed after 2 s (trace 0xdeadbeef)\n"
        uuid = "Error: request 123e4567-e89b-12d3-a456-426614174000 failed after 1500ms\n"
        self.assertEqual(checks.failure_signature(first), "Error: request failed after (trace )")
        self.assertEqual(checks.failure_signature(again), checks.failure_signature(first))
        self.assertEqual(checks.failure_signature(uuid), "Error: request failed after")
        # Ordinary words made of hex letters stay.
        words = "FAIL: added a bad face"
        self.assertEqual(checks.failure_signature(words), words)

    def test_failure_words_in_any_case(self):
        for line in ("FAIL test_add", "AssertionError: -1 != 5", "2 tests failed", "npm error"):
            output = f"collected 3 items\n{line}\nsummary at 10:42\n"
            self.assertEqual(checks.failure_signature(output), checks.failure_signature(line))

    def test_without_a_failing_line_the_last_line_counts(self):
        output = "building...\n  step 3 of 7   timed out\n\n"
        self.assertEqual(checks.failure_signature(output), "step of timed out")
        self.assertEqual(checks.failure_signature(""), "")
        self.assertEqual(checks.failure_signature(None), "")

    def test_only_failing_checks_have_a_signature(self):
        results = [
            {"name": "unit", "passed": True, "output": "ok"},
            {"name": "e2e", "passed": False, "output": "FAIL picks an option"},
        ]
        self.assertEqual(checks.failing_signatures(results), {"e2e": "FAIL picks an option"})
        self.assertEqual(checks.failing_signatures(None), {})


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
