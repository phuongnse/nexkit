import contextlib
import io
import json
import os
import re
import shutil
import signal
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from nexkit import checks
from nexkit.state import check_fingerprint
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

    # Writes, then starts a process in its own session that holds stdout.
    LEAVES = "echo before; setsid sleep 30 & echo pid=$!"

    def leftover(self, output):
        """The pid of the process a command left running, from its `pid=` line; killed
        after the test."""
        pid = int(re.search(r"pid=(\d+)", output).group(1))
        self.addCleanup(self.kill, pid)
        return pid

    def kill(self, pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    def timed(self, run, *args):
        with mock.patch.object(checks, "STOP_GRACE_SECONDS", 1):
            started = time.monotonic()
            result = run(*args)
        return result, time.monotonic() - started

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_a_timeout_returns_while_a_process_outside_the_group_holds_the_output(self):
        (code, output, _), elapsed = self.timed(
            checks._run, f"{self.LEAVES}; sleep 60", self.root, 1
        )
        self.leftover(output)
        self.assertEqual(code, 124)
        self.assertLess(elapsed, 10)
        self.assertIn("before", output)  # written before the time-out, kept
        self.assertIn("[NexKit stopped the command after 1 seconds", output)
        self.assertIn("outside its process group still held its output", output)

    def test_a_setup_that_times_out_that_way_fails_as_setup(self):
        cfg = make_config(setup=[f"{self.LEAVES}; sleep 60"], checks=[])
        cfg["limits"]["setup_timeout_minutes"] = 1 / 60
        with contextlib.redirect_stdout(io.StringIO()):
            result, elapsed = self.timed(checks.setup, cfg, self.root)
        self.leftover(result["output"])
        self.assertLess(elapsed, 10)
        self.assertEqual((result["name"], result["exit_code"]), ("setup", 124))
        self.assertFalse(result["passed"])
        self.assertEqual(result["run"], cfg["setup"][0])
        self.assertIn("before", result["output"])
        self.assertIn("NexKit stopped the command", result["output"])

    def test_a_command_ends_with_its_shell_and_leaves_what_it_started_running(self):
        for command, code in ((self.LEAVES, 0), ("sleep 30 & echo pid=$!; exit 3", 3)):
            (found, output, _), elapsed = self.timed(checks._run, command, self.root, 60)
            pid = self.leftover(output)
            self.assertEqual(found, code)
            self.assertLess(elapsed, 10)
            self.assertIn(f"[The command exited with code {code}, but a process it", output)
            self.assertTrue(self.alive(pid))

    def test_a_process_left_running_can_go_on_writing(self):
        ticks = "(while true; do echo tick; sleep 0.1; done) & echo pid=$!"
        (code, output, _), _ = self.timed(checks._run, ticks, self.root, 60)
        pid = self.leftover(output)
        self.assertEqual(code, 0)
        time.sleep(1)  # NexKit still reads its pipe, so it gets no broken pipe
        self.assertTrue(self.alive(pid))

    def test_output_written_soon_after_the_shell_exits_is_kept(self):
        code, output, _ = checks._run("(sleep 0.3; echo late) & echo now", self.root, 60)
        self.assertEqual((code, output), (0, "now\nlate\n"))

    def test_output_is_text_with_universal_newlines(self):
        code, output, _ = checks._run("printf 'a\\r\\nb\\rc\\xff\\n'", self.root, 60)
        self.assertEqual((code, output), (0, "a\nb\nc\ufffd\n"))

    def test_a_setup_that_leaves_a_service_running_passes_and_the_checks_see_it(self):
        cfg = make_config(
            setup=[f"{self.LEAVES} > service.pid"],
            checks=[{"name": "unit", "run": 'kill -0 "$(cut -d= -f2 service.pid)"'}],
        )
        with contextlib.redirect_stdout(io.StringIO()):
            (results, elapsed) = self.timed(checks.run_checks, cfg, self.root)
        self.leftover((self.root / "service.pid").read_text())
        self.assertLess(elapsed, 10)
        self.assertEqual([(r["name"], r["passed"]) for r in results], [("unit", True)])

    def test_a_setup_whose_service_holds_its_output_passes(self):
        cfg = make_config(setup=[self.LEAVES], checks=[{"name": "unit", "run": "true"}])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            (results, elapsed) = self.timed(checks.run_checks, cfg, self.root)
        self.leftover(out.getvalue())
        self.assertLess(elapsed, 10)
        self.assertEqual([(r["name"], r["passed"]) for r in results], [("unit", True)])


class ParallelChecksTests(unittest.TestCase):
    """With `parallel_checks`, each group, and each check without one, runs in its own
    `verify` job, and `combine` joins their results as one job would have left them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(
            parallel_checks=True,
            setup=["echo ready > setup.txt"],
            checks=[
                {"name": "build", "run": "echo built", "group": "fast"},
                {
                    "name": "e2e",
                    "run": "echo e2e failed; mkdir -p out-e2e; echo log > out-e2e/log.txt; false",
                    "results": ["out-e2e"],
                },
                {"name": "lint", "run": "echo linted", "group": "fast"},
                {"name": "unit", "run": "echo unit failed; false"},
            ],
        )

    def run_job(self, group):
        repo, out = self.root / f"repo-{group}", self.root / "parts" / checks.artifact_name(group)
        repo.mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            return checks.run_checks(self.cfg, repo, out, group=group)

    def test_each_group_and_each_other_check_has_a_job(self):
        jobs = checks.check_jobs(self.cfg)
        self.assertEqual(
            {group: [c["name"] for c in items] for group, items in jobs.items()},
            {"fast": ["build", "lint"], "e2e": ["e2e"], "unit": ["unit"]},
        )
        self.assertEqual([e["group"] for e in checks.check_matrix(self.cfg)["include"]], list(jobs))

    def test_without_the_setting_every_check_runs_in_one_job(self):
        cfg = make_config(checks=[{"name": "a", "run": "true"}, {"name": "b", "run": "true"}])
        self.assertEqual(checks.check_jobs(cfg), {})
        self.assertEqual(checks.check_matrix(cfg), {"include": [{"group": "", "timeout": 70}]})
        self.assertEqual(checks.artifact_name(""), "nexkit-checks")
        # One job for all the checks is the same as no parallel jobs.
        one = make_config(
            parallel_checks=True,
            checks=[
                {"name": "a", "run": "true", "group": "g"},
                {"name": "b", "run": "true", "group": "g"},
            ],
        )
        self.assertEqual(checks.check_jobs(one), {})

    def test_a_job_runs_setup_and_only_its_own_checks(self):
        results = self.run_job("fast")
        self.assertEqual([r["name"] for r in results], ["build", "lint"])
        self.assertTrue(all(r["passed"] for r in results))
        with self.assertRaisesRegex(ValueError, "No check job"):
            checks.run_checks(self.cfg, self.root, group="nope")

    def test_combined_results_follow_the_order_of_checks_and_keep_result_files(self):
        for group in ("unit", "e2e", "fast"):  # in any order they finish
            self.run_job(group)
        out = self.root / "out"
        combined = checks.combine(self.cfg, self.root / "parts", out)
        self.assertEqual([r["name"] for r in combined], ["build", "e2e", "lint", "unit"])
        self.assertEqual([r["passed"] for r in combined], [True, False, True, False])
        self.assertEqual(json.loads((out / "checks.json").read_text()), combined)
        self.assertEqual(combined[1]["results"], ["out-e2e"])
        self.assertEqual((out / "results/e2e/out-e2e/log.txt").read_text(), "log\n")

    def test_a_failed_setup_comes_first_once_and_its_jobs_checks_are_left_out(self):
        # Setup fails only in the job of `fast`, such as on a registry that was down.
        self.cfg["setup"] = ['case "$PWD" in */repo-fast) exit 3;; esac']
        for group in ("e2e", "fast", "unit"):
            self.run_job(group)
        combined = checks.combine(self.cfg, self.root / "parts", self.root / "out")
        self.assertEqual([r["name"] for r in combined], ["setup", "e2e", "unit"])
        self.assertEqual(combined[0]["exit_code"], 3)

    def test_a_job_that_left_no_result_fails_its_checks(self):
        self.run_job("fast")
        self.run_job("unit")
        combined = checks.combine(self.cfg, self.root / "parts", self.root / "out")
        self.assertEqual([r["name"] for r in combined], ["build", "e2e", "lint", "unit"])
        self.assertFalse(combined[1]["passed"])
        self.assertIn("The check job `e2e` ended without a result.", combined[1]["output"])

    def test_entries_that_are_not_results_are_left_out(self):
        for group in ("fast", "unit"):
            self.run_job(group)
        part = self.root / "parts" / checks.artifact_name("e2e")
        part.mkdir(parents=True)
        (part / "checks.json").write_text('["e2e", {"name": "e2e", "passed": true}]')
        combined = checks.combine(self.cfg, self.root / "parts", self.root / "out")
        self.assertEqual([r["name"] for r in combined], ["build", "e2e", "lint", "unit"])

    def test_the_one_artifact_that_was_downloaded_by_itself_belongs_to_its_job(self):
        # A pattern that matches one artifact extracts it without its folder.
        self.run_job("e2e")
        part = self.root / "parts" / checks.artifact_name("e2e")
        for item in part.iterdir():
            item.rename(self.root / "parts" / item.name)
        part.rmdir()
        combined = checks.combine(self.cfg, self.root / "parts", self.root / "out")
        self.assertEqual([r["name"] for r in combined], ["build", "e2e", "lint", "unit"])
        self.assertIn("e2e failed", combined[1]["output"])
        self.assertIn("ended without a result", combined[0]["output"])
        self.assertTrue((self.root / "out/results/e2e/out-e2e/log.txt").is_file())

    def test_a_job_whose_checks_route_all_skipped_is_left_out(self):
        since = "a" * 40
        skipped = [{"name": "e2e", "since": since}, {"name": "build", "since": since}]
        # `fast` still runs `lint`, so only the job of `e2e` is left out.
        matrix = checks.check_matrix(self.cfg, skipped)
        self.assertEqual([e["group"] for e in matrix["include"]], ["fast", "unit"])
        self.assertFalse(checks.all_skipped(self.cfg, skipped))
        # A job that runs does not run its skipped checks either.
        repo, out = self.root / "repo-fast", self.root / "parts" / checks.artifact_name("fast")
        repo.mkdir(parents=True)
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            results = checks.run_checks(self.cfg, repo, out, group="fast", skipped=skipped)
        self.assertEqual(
            [(r["name"], r["passed"]) for r in results], [("build", True), ("lint", True)]
        )
        self.assertEqual(results[0]["skipped"], since)
        self.assertNotIn("echo built", log.getvalue())
        self.assertIn("`build` skipped: no path it reads changed since `aaaaaaa`.", log.getvalue())
        self.run_job("unit")
        # The job that did not run passes its checks, rather than leaving no result.
        combined = checks.combine(self.cfg, self.root / "parts", self.root / "out", skipped)
        self.assertEqual(
            [(r["name"], r["passed"], r.get("skipped")) for r in combined],
            [
                ("build", True, since),
                ("e2e", True, since),
                ("lint", True, None),
                ("unit", False, None),
            ],
        )

    def test_when_route_skipped_every_check_no_verify_job_runs(self):
        skipped = [{"name": c["name"], "since": "b" * 40} for c in self.cfg["checks"]]
        self.assertTrue(checks.all_skipped(self.cfg, skipped))
        # The matrix keeps every job, so it is never empty; the job does not run.
        self.assertEqual(checks.check_matrix(self.cfg, skipped), checks.check_matrix(self.cfg))
        results = checks.with_skipped(self.cfg, None, skipped)
        self.assertEqual([r["name"] for r in results], ["build", "e2e", "lint", "unit"])
        self.assertTrue(all(r["passed"] and r["skipped"] == "b" * 40 for r in results))
        self.assertEqual(
            results[1]["output"], "`e2e` skipped: no path it reads changed since `bbbbbbb`."
        )

    def test_one_job_that_ran_and_its_setup_failed_keeps_its_result(self):
        # Only the job of `unit` runs, and its setup fails; the skipped jobs do not claim
        # the one artifact, which the download puts in `parts` itself.
        self.cfg["setup"] = ["exit 3"]
        skipped = [{"name": n, "since": "a" * 40} for n in ("build", "e2e", "lint")]
        self.run_job("unit")
        part = self.root / "parts" / checks.artifact_name("unit")
        for item in part.iterdir():
            item.rename(self.root / "parts" / item.name)
        part.rmdir()
        combined = checks.combine(self.cfg, self.root / "parts", self.root / "out", skipped)
        self.assertEqual([r["name"] for r in combined], ["setup", "build", "e2e", "lint"])
        self.assertEqual(combined[0]["exit_code"], 3)

    def test_each_job_keeps_its_share_of_the_result_files_limit(self):
        with (
            mock.patch.object(checks, "RESULTS_LIMIT", 9),
            mock.patch.object(checks, "JOB_RESULTS_FLOOR", 2),
            mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.root / "summary.md")}),
        ):
            results = self.run_job("e2e")
        # Three jobs share 9 bytes, more than the floor: the 4-byte log fits in 3 no more.
        self.assertEqual(results[0]["results_left_out"], 1)
        summary = (self.root / "summary.md").read_text()
        self.assertIn("each of the round's 3 check jobs keeps at most", summary)


class CheckJobTimesTests(unittest.TestCase):
    """With `parallel_checks`, `combine` writes each check job's time to the run summary,
    and the job the run waited for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parts = Path(self.tmp.name) / "parts"
        self.cfg = make_config(
            parallel_checks=True,
            checks=[
                {"name": "unit", "run": "true"},
                {"name": "e2e", "run": "true"},
                {"name": "lint", "run": "true"},
            ],
        )
        # A second attempt: its own start counts, not the first attempt's.
        self.run = {
            "run_started_at": "2026-11-12T05:46:47Z",
            "created_at": "2026-11-12T05:00:00Z",
        }
        self.jobs = [
            self.job("route", "05:58:38", "05:59:00"),
            self.job("verify (unit)", "05:59:12", "06:08:08"),
            self.job("verify (e2e)", "05:59:12", "06:14:25"),
            self.job("verify (lint)", "05:59:12", "06:05:01"),
            self.job("review", "06:14:25", "06:14:25", "skipped"),
        ]
        self.now = datetime(2026, 11, 12, 6, 15, 17, tzinfo=UTC)

    def job(self, name, start, end, conclusion="success"):
        return {
            "name": f"nexkit / {name}",
            "status": "completed",
            "conclusion": conclusion,
            "started_at": f"2026-11-12T{start}Z",
            "completed_at": f"2026-11-12T{end}Z",
        }

    def part(self, group, results):
        folder = self.parts / checks.artifact_name(group)
        folder.mkdir(parents=True)
        (folder / "checks.json").write_text(json.dumps(results))

    def check(self, name, passed=True, seconds=1.0):
        return {"name": name, "run": "true", "passed": passed, "seconds": seconds}

    def summary(self, **kwargs):
        options = {"run": self.run, "jobs": self.jobs, "now": self.now, **kwargs}
        return checks.jobs_summary(self.cfg, self.parts, **options)

    def rows(self, text):
        return [line for line in text.splitlines() if line.startswith("| `")]

    def test_one_row_per_job_longest_first_with_its_checks_time(self):
        self.part("unit", [self.check("unit", seconds=410.7)])
        self.part("e2e", [self.check("e2e", seconds=900)])
        self.part("lint", [self.check("lint", seconds=30)])
        self.assertEqual(
            self.rows(self.summary()),
            [
                "| `e2e` (ended last: the run waited for it) | passed | 15m 13s | 15m 00s |",
                "| `unit` | passed | 8m 56s | 6m 50s |",
                "| `lint` | passed | 5m 49s | 30s |",
            ],
        )

    def test_the_line_above_gives_the_run_its_wait_and_the_check_jobs_span(self):
        for group in ("unit", "e2e", "lint"):
            self.part(group, [self.check(group)])
        text = self.summary()
        self.assertIn(
            "The run so far: **28m 30s**; "
            "waited **11m 51s** before its first job started; "
            "check jobs: **15m 13s** from the first start to the last end.",
            text,
        )
        # The skipped review, which never started, does not count.
        self.assertLess(text.index("The run so far"), text.index("| Check job |"))

    def test_a_failed_cancelled_or_resultless_job_is_still_listed(self):
        self.jobs[1]["conclusion"] = "cancelled"
        self.jobs[3]["conclusion"] = "failure"
        self.part("unit", [self.check("unit", passed=False, seconds=None)])
        self.part("e2e", [self.check("e2e", passed=False)])
        # `lint` uploaded no part.
        rows = self.rows(self.summary())
        self.assertEqual(
            rows,
            [
                "| `e2e` (ended last: the run waited for it) | 1 of 1 failed | 15m 13s | 1s |",
                "| `unit` | cancelled, 1 of 1 failed | 8m 56s | N/A |",
                "| `lint` | failed, no result | 5m 49s | N/A |",
            ],
        )

    def test_a_failed_setup_says_so(self):
        setup = {"name": "setup", "passed": False, "seconds": 12}
        for group in ("unit", "e2e", "lint"):
            self.part(group, [setup] if group == "unit" else [self.check(group)])
        self.assertIn("| `unit` | setup failed | 8m 56s | N/A |", self.summary())

    def test_without_the_actions_api_the_table_has_no_times(self):
        for group in ("unit", "e2e"):
            self.part(group, [self.check(group, seconds=5)])
        text = self.summary(run=None, jobs=None, error="403 Resource not accessible")
        self.assertNotIn("The run so far", text)
        self.assertIn("No job times: the Actions API answered 403 Resource not accessible", text)
        self.assertEqual(
            self.rows(text),
            [
                "| `unit` | passed | N/A | 5s |",
                "| `e2e` | passed | N/A | 5s |",
                "| `lint` | no result | N/A | N/A |",
            ],
        )

    def test_a_job_whose_checks_route_all_skipped_has_no_row(self):
        self.part("unit", [self.check("unit")])
        self.part("e2e", [self.check("e2e")])
        skipped = [{"name": "lint", "since": "a" * 40}]
        text = self.summary(skipped=skipped)
        self.assertEqual(
            [row.split(" | ")[0] for row in self.rows(text)],
            ["| `e2e` (ended last: the run waited for it)", "| `unit`"],
        )

    def test_without_parallel_checks_there_is_no_section(self):
        self.cfg["parallel_checks"] = False
        self.assertEqual(self.summary(), "")

    def test_combine_checks_writes_the_section_to_the_run_summary(self):
        from nexkit import cli

        for group in ("unit", "e2e", "lint"):
            self.part(group, [self.check(group)])
        summary = Path(self.tmp.name) / "summary.md"
        gh = mock.Mock()
        gh.run_attempt.return_value = self.run
        gh.every_attempt_job.return_value = self.jobs
        env = {
            "NEXKIT_CONFIG": json.dumps(self.cfg),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": "43163637643",
            "GITHUB_RUN_ATTEMPT": "2",
        }
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(cli, "_gh", return_value=gh),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            os.environ.pop("NEXKIT_DECISION", None)
            os.environ.pop("NEXKIT_SHARDS", None)
            out = f"{self.tmp.name}/out"
            cli.main(["combine-checks", "--parts", str(self.parts), "--out", out])
        gh.run_attempt.assert_called_once_with("43163637643", "2")
        gh.every_attempt_job.assert_called_once_with("43163637643", "2")
        text = summary.read_text()
        self.assertIn("### NexKit check jobs", text)
        self.assertIn("| `e2e` (ended last: the run waited for it) | passed | 15m 13s | 1s |", text)


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

    def trace(self, path, size):
        """A binary result file of `size` bytes, such as a Playwright trace."""
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(b"PK\x03\x04\x00")
            handle.truncate(size)

    def keep(self, jobs, checks_cfg, passed=()):
        cfg = make_config(checks=checks_cfg)
        results = [{"name": c["name"], "passed": c["name"] in passed} for c in checks_cfg]
        with contextlib.redirect_stdout(io.StringIO()):
            lines = checks.keep_results(cfg, results, self.repo, self.out, jobs=jobs)
        return results, "\n".join(lines)

    def test_each_check_job_keeps_the_larger_of_its_share_and_the_floor(self):
        self.assertEqual(checks.results_limit(1), 50_000_000)
        self.assertEqual(checks.results_limit(2), 25_000_000)
        self.assertEqual(checks.results_limit(13), 25_000_000)
        self.assertEqual(checks.results_limit(17), 25_000_000)
        # The floor never lets a job keep more than the round would with one job.
        with mock.patch.object(checks, "JOB_RESULTS_FLOOR", 80_000_000):
            self.assertEqual(checks.results_limit(3), 50_000_000)

    def test_the_one_failing_job_of_thirteen_keeps_its_trace(self):
        self.trace("test-results/trace.zip", 20_000_000)
        results, summary = self.keep(
            13, [{"name": "e2e-3", "run": "x", "results": ["test-results"]}]
        )
        kept = self.out / checks.RESULTS_DIR / "e2e-3/test-results/trace.zip"
        self.assertEqual(kept.stat().st_size, 20_000_000)
        self.assertEqual(results[0]["results_left_out"], 0)
        self.assertIn("Kept 1 file (20.0 MB)", summary)

    def test_one_job_keeps_up_to_50_mb_and_each_of_two_up_to_25_mb(self):
        self.trace("big/trace.zip", 40_000_000)
        self.trace("small/trace.zip", 20_000_000)
        cfg = [
            {"name": "e2e", "run": "x", "results": ["big"]},
            {"name": "api", "run": "x", "results": ["small"]},
        ]
        results, _ = self.keep(1, cfg[:1])
        self.assertEqual(results[0]["results_left_out"], 0)
        shutil.rmtree(self.out)
        results, _ = self.keep(2, cfg)
        self.assertEqual([r["results_left_out"] for r in results], [1, 0])
        self.assertTrue((self.out / checks.RESULTS_DIR / "api/small/trace.zip").is_file())

    def test_files_past_a_jobs_budget_are_left_out_and_listed(self):
        self.trace("test-results/a.zip", 20_000_000)
        self.trace("test-results/b.zip", 10_000_000)
        results, summary = self.keep(
            13, [{"name": "e2e-1", "run": "x", "results": ["test-results"]}]
        )
        self.assertEqual(results[0]["results_left_out"], 1)
        self.assertFalse((self.out / checks.RESULTS_DIR / "e2e-1/test-results/b.zip").exists())
        self.assertIn(
            "**Left out** (1): each of the round's 13 check jobs keeps at most 25.0 MB", summary
        )
        self.assertIn("e2e-1: test-results/b.zip (10.0 MB)", summary)


class FailedTestsTests(unittest.TestCase):
    """A failing check records the tests its JUnit result files name as failed, by names
    that match a test reporter's error annotations of the same tests."""

    JUNIT = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="web">
    <testcase classname="src/orders.test.ts" name="Orders &gt; adds tax">
      <failure message="expected 2">AssertionError</failure>
    </testcase>
    <testcase classname="src/orders.test.ts" name="Orders &gt; rounds"/>
    <testcase classname="src/cart.test.ts" name="skipped"><skipped/></testcase>
  </testsuite>
  <testsuite name="Shop.Tests">
    <testcase classname="Shop.Tests.OrderTests" name="Totals(count: 2)">
      <error message="boom"/>
    </testcase>
    <testcase classname="Shop.Tests.OrderTests" name="Shop.Tests.OrderTests.Refunds">
      <failure/>
    </testcase>
    <testcase classname="e2e" name="checkout pays" file="tests/checkout.spec.ts">
      <failure/>
    </testcase>
  </testsuite>
</testsuites>
"""

    def test_names_match_across_junit_and_annotations(self):
        names = checks.junit_failures(self.JUNIT.encode())
        self.assertEqual(
            names,
            [
                "orders.test.ts::adds tax",
                "ordertests.totals",
                "ordertests.refunds",
                "checkout.spec.ts::checkout pays",
            ],
        )
        # What test reporters title their annotations, with the file they point at.
        annotated = [
            checks.test_name("Orders › adds   tax", "web/src/orders.test.ts"),
            checks.test_name("Shop.Tests.OrderTests.Totals", "tests/OrderTests.cs"),
            checks.test_name("OrderTests.Refunds(id: 7)", ""),
            checks.test_name(
                "[chromium] › checkout.spec.ts:4:1 › checkout pays", "e2e/checkout.spec.ts:4"
            ),
        ]
        self.assertEqual(annotated, names)
        # A title without a file matches the same title in any file, but not another title.
        self.assertTrue(checks.same_test("::adds tax", "orders.test.ts::adds tax"))
        self.assertFalse(checks.same_test("cart.test.ts::adds tax", "orders.test.ts::adds tax"))
        self.assertFalse(checks.same_test("::adds tax", "::rounds"))
        self.assertEqual(checks.test_name("", "a.ts"), "")
        self.assertIsNone(checks.junit_failures(b"<html/>"))
        self.assertIsNone(checks.junit_failures(b"not xml"))

    def test_a_failing_check_records_its_failed_tests(self):
        repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        out = repo / "out"
        (repo / "reports").mkdir()
        (repo / "reports/junit.xml").write_text(self.JUNIT)
        (repo / "reports/notes.xml").write_text("<notes/>")
        (repo / "other.log").write_text("x")
        cfg = make_config(
            checks=[
                {"name": "unit", "run": "exit 1", "results": ["reports"]},
                {"name": "e2e", "run": "exit 1", "results": ["other.log"]},
            ]
        )
        summary = repo / "summary.md"
        with (
            mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            unit, e2e = checks.run_checks(cfg, repo, out)
        self.assertEqual(unit["failed_tests"], checks.junit_failures(self.JUNIT.encode()))
        # Without a JUnit file there is nothing to compare by but the command.
        self.assertNotIn("failed_tests", e2e)
        # So many failures record none.
        with mock.patch.object(checks, "FAILED_TESTS_LIMIT", 3):
            results = [{"name": "unit", "passed": False}]
            checks.keep_results(cfg, results, repo, repo / "out2")
        self.assertEqual(results[0]["failed_tests"], [])


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


class OutputTextTests(unittest.TestCase):
    """What a check's result keeps of its output, and what a prompt shows of it."""

    def test_a_short_output_shows_whole_without_a_file(self):
        kept = []
        text = checks.output_text({"output": "E1 bad\n"}, 100, 5, kept.append)
        self.assertEqual(text, "Output:\n\n```\nE1 bad\n```")
        self.assertEqual(kept, [])

    def test_a_long_output_shows_its_failure_lines_and_its_end_from_a_line(self):
        # As an older NexKit saved it: without `failure_lines`.
        output = (
            "Error: ``` the first\n"
            + "".join(f"teardown {n} ok\n" for n in range(300))
            + "".join(f"FAIL test {n} {'x' * 400}\n" for n in range(3))
            + "".join(f"teardown {n} ok\n" for n in range(300, 600))
            + "Tests: 3 failed\n"
        )
        kept = []
        text = checks.output_text(
            {"output": output}, 200, 2, lambda whole: kept.append(whole) or "/t/x.txt"
        )
        self.assertEqual(kept, [output.rstrip()])
        lines, end = text.split("End of output")
        self.assertIn("(the first 2):", lines)
        first = ("FAIL test 0 " + "x" * 400)[: checks.FAILURE_LINE_WIDTH]
        self.assertIn(f"````\nError: ``` the first\n{first} […]\n````", lines)
        self.assertNotIn("FAIL test 1", text)
        self.assertNotIn("teardown", lines)
        self.assertTrue(end.endswith("teardown 599 ok\nTests: 3 failed\n```"))
        self.assertIn(
            f"of {len(output.rstrip()):,} characters; the whole output is in `/t/x.txt`", end
        )
        # The end starts at a line, and only the lines before it are listed.
        self.assertIn("```\nteardown 589 ok\n", end)
        self.assertNotIn("Tests: 3 failed", lines)

    def test_without_a_file_or_a_failure_line_only_the_end_shows(self):
        text = checks.output_text({"output": "x" * 300}, 100, 5)
        self.assertEqual(
            text, f"End of output (the last 100 of 300 characters):\n\n```\n{'x' * 100}\n```"
        )

    def test_a_result_keeps_the_failure_lines_before_the_end_it_keeps(self):
        output = (
            "".join(f"FAIL early {n}\n" for n in range(25)) + "teardown ok\n" * 2_000 + "1 failed"
        )
        record = checks.kept_output(output)
        self.assertEqual(record["output"], output[-checks.OUTPUT_TAIL :])
        self.assertEqual(record["failure_lines"], [f"FAIL early {n}" for n in range(20)])
        self.assertEqual(checks.kept_output("FAIL\nok"), {"output": "FAIL\nok"})

    def test_recorded_failure_lines_come_first_and_count_toward_the_limit(self):
        output = "FAIL late\n" + "teardown ok\n" * 1_000 + "1 failed"
        check = {"output": output, "failure_lines": ["FAIL early"]}
        kept = []
        text = checks.output_text(check, 100, 5, lambda whole: kept.append(whole) or "/t/x.txt")
        self.assertIn("(all):\n\n```\nFAIL early\nFAIL late\n```", text)
        self.assertIn(
            f"the output NexKit kept (its last {len(output):,} characters, with the lines "
            "before them that report a failure) is in `/t/x.txt`",
            text,
        )
        [whole] = kept
        self.assertTrue(whole.startswith("Lines before the output that NexKit kept"))
        self.assertIn("FAIL early", whole)
        self.assertTrue(whole.endswith(output))
        text = checks.output_text(check, 100, 1)
        self.assertIn("(the first 1):\n\n```\nFAIL early\n```", text)

    def test_an_output_the_state_comment_shortened_says_so(self):
        check = {"output": "teardown ok\n1 failed", "output_cut": True, "failure_lines": ["FAIL a"]}
        text = checks.output_text(check, 100, 5, lambda _: "/t/x")
        self.assertIn("(the first 1):\n\n```\nFAIL a\n```", text)
        self.assertIn("the output NexKit kept (its last 20 characters, with the lines", text)
        self.assertNotIn("whole output", text)
        text = checks.output_text({"output": "", "output_cut": True}, 100, 5)
        self.assertIn("left its output out of the state comment", text)

    def test_a_kept_tail_is_not_called_the_whole_output(self):
        text = checks.output_text({"output": "x" * checks.OUTPUT_TAIL}, 100, 5, lambda _: "/t/x")
        self.assertIn("the output NexKit kept (its last 12,000 characters) is in `/t/x`", text)


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


class CheckVerifiedCommitTests(CommitCase):
    """NEXKIT_CHECK_VERIFIED_SHA: the last commit of the pull request on which one check
    passed with its command."""

    def test_each_check_sees_its_own_commit_and_the_log_says_why_one_has_none(self):
        repo = self.repos.clone("checks")
        git(repo, "checkout", "-q", "nexkit/issue-5")
        seen = 'echo "${NEXKIT_CHECK_VERIFIED_SHA-unset}"'
        cfg = make_config(
            setup=[f"{seen} > setup.txt"],
            checks=[
                {"name": "unit", "run": f"cat setup.txt; {seen}"},
                {"name": "e2e", "run": f"cat setup.txt; {seen} # e2e"},
                {"name": "lint", "run": f"cat setup.txt; {seen} # lint"},
                {"name": "docs", "run": f"cat setup.txt; {seen} # docs"},
            ],
        )
        heads = {
            "unit": {check_fingerprint(cfg["checks"][0]["run"]): self.fork},
            # Passed with another command: no commit for this one.
            "e2e": {check_fingerprint("make e2e"): self.fork},
            # The checkout does not have it.
            "lint": {check_fingerprint(cfg["checks"][2]["run"]): "f" * 40},
        }
        out = Path(repo) / "out"
        with mock.patch.dict(os.environ, {"NEXKIT_CHECK_VERIFIED_SHA": "stale"}):
            results, log = self.quietly(
                lambda: checks.run_checks(cfg, repo, out_dir=out, heads=heads)
            )
        self.assertEqual(
            [r["output"].split() for r in results],
            [["unset", self.fork], ["unset", "unset"], ["unset", "unset"], ["unset", "unset"]],
        )
        self.assertIn(f"NEXKIT_CHECK_VERIFIED_SHA={self.fork} for `unit`", log)
        self.assertIn(
            "NEXKIT_CHECK_VERIFIED_SHA is not set for `e2e`: no earlier round of this pull "
            "request passed it with this command and setup, so it should run in full.",
            log,
        )
        self.assertIn(
            "NEXKIT_CHECK_VERIFIED_SHA is not set for `lint`: the checkout does not have "
            "fffffff, the last commit on which it passed",
            log,
        )
        self.assertIn("NEXKIT_CHECK_VERIFIED_SHA is not set for `docs`: no earlier round", log)
        # The job records the commit it checked, for a round that NexKit stops.
        head = git(repo, "rev-parse", "HEAD")
        self.assertEqual(json.loads((out / checks.CHECKED_FILE).read_text()), {"head": head})

    def test_a_check_that_skips_work_still_passes(self):
        repo = self.repos.clone("checks")
        git(repo, "checkout", "-q", "nexkit/issue-5")
        skip = (
            'since="${NEXKIT_CHECK_VERIFIED_SHA:-}"; '
            '[ -n "$since" ] && ! git diff --name-only "$since" | grep -q "^b/" '
            "&& { echo skipped; exit 0; }; echo ran; exit 1"
        )
        cfg = make_config(checks=[{"name": "b", "run": skip}])
        heads = {"b": {check_fingerprint(skip): self.fork}}
        [result], _ = self.quietly(lambda: checks.run_checks(cfg, repo, heads=heads))
        self.assertEqual((result["passed"], result["output"].strip()), (True, "skipped"))
        [result], _ = self.quietly(lambda: checks.run_checks(cfg, repo))
        self.assertEqual((result["passed"], result["output"].strip()), (False, "ran"))


class SkippedChecksTests(unittest.TestCase):
    """A check that route skipped, because no path it reads changed since it passed,
    passes without running, in one job too."""

    def setUp(self):
        self.cfg = make_config(
            checks=[
                {"name": "unit", "run": "echo unit"},
                {"name": "e2e", "run": "echo e2e; false", "unaffected_by": ["docs/**"]},
            ]
        )
        self.skipped = [{"name": "e2e", "since": "c" * 40}]

    def test_the_one_job_runs_only_the_checks_route_did_not_skip(self):
        self.assertEqual(checks.check_matrix(self.cfg, self.skipped), checks.check_matrix(self.cfg))
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            results = checks.run_checks(self.cfg, tmp, skipped=self.skipped)
        self.assertEqual(
            [(r["name"], r["passed"]) for r in results], [("unit", True), ("e2e", True)]
        )
        self.assertEqual((results[1]["seconds"], results[1]["run"]), (0, "echo e2e; false"))

    def test_results_get_the_skipped_checks_they_lack_in_the_order_of_checks(self):
        unit = {"name": "unit", "run": "echo unit", "passed": False, "exit_code": 1}
        merged = checks.with_skipped(self.cfg, [unit], self.skipped)
        self.assertEqual([r["name"] for r in merged], ["unit", "e2e"])
        self.assertEqual(merged[0], unit)
        # Once there, a skipped result is not added again.
        self.assertEqual(checks.with_skipped(self.cfg, merged, self.skipped), merged)
        setup = {"name": "setup", "run": "npm ci", "passed": False, "exit_code": 2}
        self.assertEqual(
            [r["name"] for r in checks.with_skipped(self.cfg, [setup], self.skipped)],
            ["setup", "e2e"],
        )
        # Checks that did not run, or left no result, stay unknown unless all were skipped.
        self.assertIsNone(checks.with_skipped(self.cfg, None, self.skipped))
        self.assertIsNone(checks.with_skipped(self.cfg, None, []))
        self.assertEqual(checks.with_skipped(self.cfg, [unit], None), [unit])


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


SHARDED = {
    "name": "e2e",
    "run": 'echo "shard $NEXKIT_SHARD"',
    "shards": {"count": "cat count.txt", "max": 4},
}


class ShardTests(unittest.TestCase):
    """A check that runs in as many shards as its `count` prints."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cfg = make_config(
            parallel_checks=True, checks=[{"name": "lint", "run": "true"}, SHARDED]
        )

    def count(self, text):
        (self.root / "count.txt").write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            return checks.count_shards(self.cfg, self.root)

    def test_each_shard_is_a_check_of_its_own_with_its_shard(self):
        counts = self.count("Measured 41 specs.\n3\n")
        self.assertEqual(counts, {"e2e": 3})
        expanded = checks.expand(self.cfg, counts)
        self.assertEqual(
            [(c["name"], c.get("shard")) for c in expanded["checks"]],
            [("lint", None), ("e2e-1", "1/3"), ("e2e-2", "2/3"), ("e2e-3", "3/3")],
        )
        self.assertNotIn("shards", expanded["checks"][1])
        # One job each, in the matrix the `shards` job gives verify.
        matrix = checks.check_matrix(expanded)
        self.assertEqual(
            [entry["group"] for entry in matrix["include"]], ["lint", "e2e-1", "e2e-2", "e2e-3"]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            results = checks.run_checks(expanded, self.root, group="e2e-2")
        [result] = results
        self.assertEqual((result["name"], result["shard"]), ("e2e-2", "2/3"))
        self.assertIn("shard 2/3", result["output"])
        self.assertTrue(result["passed"])
        # A fix round's prompt shows how to run that shard alone.
        self.assertEqual(
            checks.shown_command(result), 'NEXKIT_SHARD=2/3 echo "shard $NEXKIT_SHARD"'
        )

    def test_a_count_that_fails_or_is_out_of_range_fails_the_check_and_runs_no_shard(self):
        for text, why in (
            ("7\n", "not a whole number from 1 to 4"),
            ("0\n", "not a whole number from 1 to 4"),
            ("three\n", "'three' last"),
            ("", "''"),
        ):
            counts = self.count(text)
            self.assertIn(why, counts["e2e"]["error"], text)
        (self.root / "count.txt").unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            counts = checks.count_shards(self.cfg, self.root)
        self.assertIn("failed with exit code 1", counts["e2e"]["error"])
        self.assertIn("count.txt", counts["e2e"]["error"])
        expanded = checks.expand(self.cfg, counts)
        self.assertEqual([c["name"] for c in expanded["checks"]], ["lint", "e2e"])
        (self.root / "ran.txt").unlink(missing_ok=True)
        cfg = {**expanded, "setup": ["touch ran.txt"]}
        with contextlib.redirect_stdout(io.StringIO()):
            [result] = checks.run_checks(cfg, self.root, group="e2e")
        self.assertFalse(result["passed"])
        self.assertEqual(result["run"], "cat count.txt")
        self.assertIn("so no shard ran", result["output"])
        # Nothing ran in its job, setup neither.
        self.assertFalse((self.root / "ran.txt").exists())

    def test_a_long_count_output_is_cut_and_a_count_out_of_range_runs_nothing(self):
        counts = self.count("x" * 200_000)
        self.assertLess(len(counts["e2e"]["error"]), checks.COUNT_TAIL + 500)
        # Counts the `shards` job did not make, such as one a process it left behind wrote.
        for number in (0, 5):
            [check] = checks.expand(
                make_config(parallel_checks=True, checks=[SHARDED]), {"e2e": number}
            )["checks"]
            self.assertIn("not 1 to its `max`", check["count_error"])

    def test_count_cannot_set_the_jobs_outputs(self):
        (self.root / "count.txt").write_text("2\n")
        cfg = make_config(
            parallel_checks=True,
            checks=[
                {**SHARDED, "shards": {"count": 'echo "${GITHUB_OUTPUT:-none}"; echo 2', "max": 4}}
            ],
        )
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": "/tmp/out"}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                checks.count_shards(cfg, self.root)
        self.assertIn("none", out.getvalue())
        self.assertNotIn("/tmp/out", out.getvalue())

    def test_a_skipped_shard_keeps_its_fingerprint(self):
        check = {"name": "e2e-2", "run": "x", "shard": "2/3"}
        result = checks.skipped_result(check, "a" * 40)
        self.assertEqual(state_passed([result])["e2e-2"], check_fingerprint("x", "2/3"))

    def test_a_shard_gets_its_verified_commit_only_with_the_same_count(self):
        result = {"name": "e2e-1", "run": SHARDED["run"], "passed": True, "shard": "1/3"}
        recorded = state_passed([result])
        self.assertEqual(recorded["e2e-1"], check_fingerprint(SHARDED["run"], "1/3"))
        self.assertNotEqual(recorded["e2e-1"], check_fingerprint(SHARDED["run"], "1/4"))
        self.assertNotEqual(recorded["e2e-1"], check_fingerprint(SHARDED["run"]))

    def test_an_automatic_review_skips_a_shard_whose_paths_did_not_change(self):
        repos = GitRepos()
        self.addCleanup(repos._tmp.cleanup)
        work = repos.clone("work")
        since = git(work, "rev-parse", "HEAD")
        (work / "docs.md").write_text("words\n")
        git(work, "add", "-A")
        head = repos.commit(work, "docs")
        check = {**SHARDED, "unaffected_by": ["*.md"]}
        cfg = checks.expand(make_config(parallel_checks=True, checks=[check]), {"e2e": 2})
        heads = {"e2e-1": {check_fingerprint(check["run"], "1/2"): since}}
        decision = {"action": "review", "auto": True, "check_heads": heads}
        self.assertEqual(
            checks.skip_shards(cfg, decision, work, head), [{"name": "e2e-1", "since": since}]
        )
        # Not for a person's review, nor for another count.
        self.assertEqual(checks.skip_shards(cfg, {**decision, "auto": False}, work, head), [])
        other = checks.expand(make_config(parallel_checks=True, checks=[check]), {"e2e": 3})
        self.assertEqual(checks.skip_shards(other, decision, work, head), [])


def state_passed(results):
    from nexkit.state import passed_checks

    return passed_checks(results)
