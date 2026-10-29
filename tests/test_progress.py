import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from nexkit import progress
from nexkit.github import GitHub, GitHubError
from nexkit.maintain import (
    WAIT_INPUTS,
    _give_back,
    check_paused,
    maintain,
    resume_paused,
    wait_and_resume,
)
from nexkit.report import report
from nexkit.state import (
    empty_state,
    read_state,
    render_run,
    render_state,
    run_action,
    run_kinds,
)
from tests.support import FakeGitHub, make_config

HEAD = "c" * 40
NEW_HEAD = "e" * 40
RUN = "https://github.com/acme/app/actions/runs/77"


def decision(action, **kw):
    on_pull = action in ("fix", "review")
    base = {
        "action": action,
        "issue": 5,
        "pr": 6 if on_pull else None,
        "target": 6 if on_pull else 5,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": HEAD if on_pull else None,
        "note": "",
        "auto": False,
        "comment_id": 9,
    }
    return {**base, **kw}


def review_result(verdict="approve"):
    return {
        "status": "done",
        "tokens": {"input": 48_000, "cached": 40_000, "output": 2_100},
        "output": {"verdict": verdict, "summary": "Ok.", "criteria": [], "findings": []},
    }


class ProgressTests(unittest.TestCase):
    """A command shows its run from the start and its outcome where it was given."""

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha=HEAD)
        self.tmp = tempfile.TemporaryDirectory()
        self.artifacts = Path(self.tmp.name)
        env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "77"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, artifact, name, data):
        path = self.artifacts / artifact / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def finish(self, d, publish=None):
        needs = {"publish": {"outputs": {"result": json.dumps(publish)}}} if publish else {}
        return report(
            self.gh,
            d,
            make_config(),
            needs,
            self.artifacts,
            workflow_ref="acme/app/.github/workflows/nexkit.yml@refs/heads/main",
            default_branch="main",
        )

    def candidate(self, verdict="approve"):
        tokens = {"input": 52_000, "cached": 30_000, "output": 900}
        self.write(
            "nexkit-agent", "result.json", {"status": "done", "summary": "Done.", "tokens": tokens}
        )
        checks = [{"name": "test", "run": "t", "exit_code": 0, "passed": True, "output": ""}]
        self.write("nexkit-checks", "checks.json", checks)
        self.write("nexkit-review", "result.json", review_result(verdict))

    def bot_comments(self, number):
        return [c for c in self.gh.comments(number) if c["user"]["type"] == "Bot"]

    def run_as(self, run_id):
        return mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(run_id)})

    def test_issue_command_gets_its_own_comment(self):
        d = decision("plan")
        progress.start(self.gh, d)
        self.assertEqual(self.gh.reactions, [(9, "eyes")])
        comments = self.bot_comments(5)
        self.assertEqual(len(comments), 1)
        self.assertIn("### NexKit `/nexkit plan`", comments[0]["body"])
        self.assertIn(f"⏳ [running]({RUN})", comments[0]["body"])

        self.write("nexkit-agent", "result.json", {"status": "done"})
        outcome = self.finish(d, {"published": True, "comment": "https://x/plan"})
        self.assertEqual(outcome["outcome"], "planned")
        comments = self.gh.run_comments(5)
        self.assertEqual(len(comments), 1)
        self.assertIn(f"✅ [run log]({RUN})\n\n[Plan](https://x/plan) posted.", comments[0]["body"])
        self.assertEqual(
            comments[0]["run"],
            {"url": RUN, "command": "plan", "status": "success", "outcome": "planned"},
        )
        self.assertEqual(self.gh.reactions, [(9, "eyes"), (9, "rocket")])

    def test_failed_issue_command(self):
        d = decision("implement")
        progress.start(self.gh, d)
        self.write(
            "nexkit-agent", "result.json", {"status": "error", "error": "Setup failed:\nlog"}
        )
        self.assertEqual(self.finish(d)["outcome"], "agent_error")
        comments = self.bot_comments(5)
        self.assertEqual(len(comments), 1)
        self.assertIn("### NexKit `/nexkit go`\n\n❌ [run log]", comments[0]["body"])
        self.assertIn("NexKit stopped during implement: Setup failed:\nlog", comments[0]["body"])
        self.assertEqual(self.gh.reactions[-1], (9, "confused"))

    def test_each_issue_run_has_its_own_comment(self):
        old_status = "### NexKit\n\n- ✅ run log `/nexkit plan`\n\n<!-- nexkit:status e30= -->"
        self.gh.comment(5, old_status)  # v1.1.0
        first = decision("plan")
        with self.run_as(77):
            progress.start(self.gh, first)
            self.write("nexkit-agent", "result.json", {"status": "done"})
            self.finish(first, {"published": True, "comment": "https://x/plan"})
        first_body = self.gh.run_comments(5)[0]["body"]
        self.gh.human_comment(5, "/nexkit go")
        second = decision("implement", comment_id=None)  # started by a dispatch
        with self.run_as(78):
            progress.start(self.gh, second)
            self.assertEqual(self.gh.run_comments(5)[0]["body"], first_body)
            self.assertIn("⏳", self.gh.comments(5)[-1]["body"])  # right after the command
            self.candidate()
            outcome = self.finish(second, {"published": True, "pr": 6, "head": HEAD})
        self.assertEqual(outcome["outcome"], "ready")
        comments = self.gh.run_comments(5)
        self.assertEqual(
            [(c["run"]["command"], c["run"]["status"]) for c in comments],
            [("plan", "success"), ("go", "success")],
        )
        self.assertEqual(comments[0]["body"], first_body)
        self.assertIn("Pull request #6: ✅ Checks passed", comments[1]["body"])
        self.assertEqual(self.gh.comments(5)[0]["body"], old_status)

    def test_report_posts_a_new_comment_when_the_run_comment_is_gone(self):
        d = decision("plan")
        progress.start(self.gh, d)
        self.gh.issue_comments[5].clear()
        self.write("nexkit-agent", "result.json", {"status": "done"})
        self.finish(d, {"published": True, "comment": "https://x/plan"})
        comments = self.gh.run_comments(5)
        self.assertEqual([c["run"]["status"] for c in comments], ["success"])

    def test_a_rerun_keeps_its_comment(self):
        progress.start(self.gh, decision("plan"))
        progress.start(self.gh, decision("plan"))
        self.assertEqual(len(self.gh.run_comments(5)), 1)

    def test_a_rerun_on_another_commit_drops_the_checks_that_passed_before(self):
        progress.start(self.gh, decision("fix"))
        [comment] = self.gh.run_comments(6)
        passed = {"test": "0123456789abcdef"}
        self.gh.update_comment(comment["id"], render_run({**comment["run"], "passed": passed}))
        # The same round run again on its commit keeps them; on another commit it does not.
        progress.start(self.gh, decision("fix"))
        self.assertEqual(self.gh.run_comments(6)[0]["run"]["passed"], passed)
        progress.start(self.gh, decision("fix", head=NEW_HEAD))
        self.assertNotIn("passed", self.gh.run_comments(6)[0]["run"])

    def test_pull_request_round_is_shown_while_running(self):
        d = decision("fix")
        progress.start(self.gh, d)
        self.assertEqual(self.gh.reactions, [(9, "eyes")])
        self.assertEqual(
            {(s["sha"], s["context"], s["state"], s["target_url"]) for s in self.gh.statuses},
            {(HEAD, "nexkit/checks", "pending", RUN), (HEAD, "nexkit/review", "pending", RUN)},
        )
        [running] = self.gh.run_comments(6)
        self.assertIn("### NexKit round 1: fix\n\n⏳ [running]", running["body"])
        self.assertIn("| ccccccc | N/A | N/A | N/A |", running["body"])

        self.candidate()
        outcome = self.finish(d, {"published": True, "pr": 6, "head": NEW_HEAD})
        self.assertEqual(outcome["outcome"], "ready")
        [done] = self.gh.run_comments(6)  # the running comment was completed
        self.assertEqual(done["id"], running["id"])
        self.assertEqual(done["run"]["status"], "success")
        self.assertIn(f"✅ [run log]({RUN})", done["body"])
        self.assertIn("| eeeeeee | passed | approve | 100k in (70k cached), 3k out |", done["body"])
        self.assertIn("ready for a human decision", done["body"])
        self.assertEqual(len(self.gh.comments_matching(6, "ready for a human decision")), 1)
        latest = {(s["sha"], s["context"]): s["state"] for s in self.gh.statuses}
        self.assertEqual(latest[(NEW_HEAD, "nexkit/review")], "success")
        self.assertNotEqual(latest[(HEAD, "nexkit/checks")], "pending")
        self.assertEqual(self.gh.reactions[-1], (9, "rocket"))
        self.assertFalse(self.gh.run_comments(5))

    def test_each_round_has_its_own_comment(self):
        self.candidate()
        with self.run_as(77):
            self.finish(decision("implement"), {"published": True, "pr": 6, "head": HEAD})
        [first] = self.gh.run_comments(6)
        self.gh.human_comment(6, "/nexkit review")
        with self.run_as(78):
            progress.start(self.gh, decision("review"))
            self.assertIn("round 2: review\n\n⏳", self.gh.comments(6)[-1]["body"])
            self.finish(decision("review"))
        comments = self.gh.run_comments(6)
        self.assertEqual([c["run"]["round"] for c in comments], [1, 2])
        self.assertEqual(comments[0]["body"], first["body"])
        self.assertEqual(comments[1]["run"]["status"], "success")
        self.assertEqual(len(self.gh.comments_matching(6, "nexkit:state")), 1)

    def test_round_without_its_comment_is_posted_at_the_end(self):
        d = decision("fix")
        progress.start(self.gh, d)
        self.gh.issue_comments[6].clear()
        self.candidate()
        self.finish(d, {"published": True, "pr": 6, "head": NEW_HEAD})
        [done] = self.gh.run_comments(6)
        self.assertEqual((done["run"]["round"], done["run"]["status"]), (1, "success"))

    def test_failed_round_restores_the_previous_statuses(self):
        state = empty_state(5)
        failing = [{"name": "test", "run": "t", "exit_code": 1, "passed": False, "output": "E"}]
        state["feedback"] = {"head": HEAD, "checks": failing, "review": None}
        self.gh.comment(6, render_state(state))
        d = decision("fix", comment_id=None, auto=True)
        progress.start(self.gh, d)
        self.write("nexkit-agent", "result.json", {"status": "blocked", "error": "Unclear"})
        self.assertEqual(self.finish(d)["outcome"], "agent_blocked")
        latest = {s["context"]: s["state"] for s in self.gh.statuses if s["sha"] == HEAD}
        self.assertEqual(latest, {"nexkit/checks": "failure", "nexkit/review": "error"})
        [stopped] = self.gh.run_comments(6)
        self.assertEqual(
            (stopped["run"]["trigger"], stopped["run"]["status"]), ("fix (auto)", "failure")
        )
        self.assertEqual((stopped["run"]["action"], stopped["run"]["kinds"]), ("fix", ["auto"]))
        self.assertIn("Unclear", stopped["body"])
        self.assertEqual(len(self.gh.comments(6)), 2)  # state and round, no extra comment
        self.assertEqual(self.gh.reactions, [])  # dispatched rounds have no comment
        self.assertFalse(self.gh.dispatches)

    def test_a_review_stored_before_criteria_had_a_status_keeps_its_verdict(self):
        # A review from before 4.5.0 approved with a boolean `met: false`. Restoring its
        # statuses keeps that verdict: a stored review is never judged again.
        state = empty_state(5)
        passed = [{"name": "test", "run": "t", "exit_code": 0, "passed": True, "output": ""}]
        criterion = {"criterion": "five runs", "met": False, "test": "", "evidence": "None."}
        review = {"verdict": "approve", "summary": "OK", "criteria": [criterion], "findings": []}
        state["feedback"] = {"head": HEAD, "checks": passed, "review": review}
        self.gh.comment(6, render_state(state))
        d = decision("fix", comment_id=None, auto=True)
        progress.start(self.gh, d)
        self.write("nexkit-agent", "result.json", {"status": "blocked", "error": "Unclear"})
        self.assertEqual(self.finish(d)["outcome"], "agent_blocked")
        latest = {
            s["context"]: (s["state"], s["description"])
            for s in self.gh.statuses
            if s["sha"] == HEAD
        }
        self.assertEqual(
            latest,
            {
                "nexkit/checks": ("success", "Checks passed"),
                "nexkit/review": ("success", "AI review approved"),
            },
        )

    def test_pull_request_from_v1_1_keeps_working(self):
        failing = [{"name": "test", "run": "t", "exit_code": 1, "passed": False, "output": "E"}]
        old = {
            "version": 1,
            "issue": 5,
            "auto_fixes": 1,
            "rounds": [
                {"round": 1, "trigger": "implement", "status": "success", "head": "a" * 40},
                {"round": 2, "trigger": "fix (auto)", "status": "success", "head": HEAD},
            ],
            "feedback": {"head": HEAD, "checks": failing, "review": None},
        }
        encoded = base64.b64encode(json.dumps(old).encode()).decode()
        old_body = f"### NexKit\n\n| Round |\n\n<!-- nexkit:state {encoded} -->"
        self.gh.comment(6, old_body)
        d = decision("fix", comment_id=None, auto=True)
        progress.start(self.gh, d)
        self.assertIn("### NexKit round 3: fix (auto)", self.gh.comments(6)[-1]["body"])
        self.candidate("request_changes")
        self.assertEqual(
            self.finish(d, {"published": True, "pr": 6, "head": NEW_HEAD})["outcome"], "auto_fix"
        )
        self.assertEqual(self.gh.comments(6)[0]["body"], old_body)
        _, state = read_state(self.gh.comments(6))
        self.assertNotIn("rounds", state)
        self.assertEqual((state["version"], state["auto_fixes"]), (2, 2))
        self.assertEqual(state["feedback"]["head"], NEW_HEAD)
        self.assertEqual(state["fixes"][0]["round"], 3)

    def test_progress_failures_do_not_stop_the_run(self):
        self.gh.set_status = mock.Mock(side_effect=GitHubError(403, "forbidden"))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            progress.start(self.gh, decision("review"))
        self.assertIn("forbidden", stderr.getvalue())


class PausedRunTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        patcher = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def paused(self, number, run_id, at="2026-10-11T04:40:00Z"):
        resume = {"command": "plan", "number": number, "note": "", "auto": "resume", "at": at}
        run = {"url": f"https://x/runs/{run_id}", "command": "plan", "status": "paused"}
        return self.gh.comment(number, render_run({**run, "resume": resume}, "⏸️ Paused."))

    def test_trigger_labels(self):
        self.assertEqual(progress.trigger({"action": "fix", "auto": True}), "fix (auto)")
        self.assertEqual(
            progress.trigger({"action": "fix", "auto": True, "conflicts": True, "resumed": True}),
            "fix (auto, conflicts, resumed)",
        )
        self.assertEqual(
            progress.trigger({"action": "review", "resumed": True}), "review (resumed)"
        )

    def test_a_rounds_action_and_kinds_are_read_from_its_fields_before_its_trigger(self):
        old = {"round": 2, "trigger": "fix (auto, conflicts)"}
        self.assertEqual((run_action(old), run_kinds(old)), ("fix", {"auto", "conflicts"}))
        self.assertEqual(
            (run_action({"trigger": "implement"}), run_kinds({})), ("implement", set())
        )
        # The fields win over text written any other way.
        new = {"round": 2, "trigger": "fix: auto; conflicts", "action": "fix", "kinds": ["auto"]}
        self.assertEqual((run_action(new), run_kinds(new)), ("fix", {"auto"}))
        self.assertEqual(run_kinds({**old, "kinds": []}), set())
        # An automatic fix round that NexKit stopped gives its round back, read from them.
        self.gh.comment(6, render_state({"version": 2, "issue": 5, "auto_fixes": 1}))
        _give_back(self.gh, 6, self.gh.comments(6), {**old, "kinds": []})
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 1)
        _give_back(self.gh, 6, self.gh.comments(6), new)
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 0)

    def test_a_new_command_replaces_a_paused_one(self):
        self.paused(5, 1)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "2"}):
            progress.start(self.gh, decision("plan"))
        first, second = self.gh.run_comments(5)
        self.assertEqual(first["run"]["status"], "continued")
        self.assertNotIn("resume", first["run"])
        self.assertIn("⏭️ [continued](https://x/runs/1)", first["body"])
        self.assertNotIn("⏸️ [paused]", first["body"])
        self.assertIn("⏸️ Paused.\n\nReplaced by a later command.", first["body"])
        self.assertEqual(second["run"]["status"], "running")

    def test_resume_starts_due_commands_once(self):
        now = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)
        due = self.paused(5, 1)
        self.gh.add_issue(7)
        self.paused(7, 2, at="2026-10-11T06:00:00Z")
        self.gh.dispatch_runs["nexkit.yml"] = "https://x/runs/3"
        lines, upcoming = resume_paused(self.gh, "nexkit.yml", "main", now)
        self.assertEqual(lines, ["Resumed /nexkit plan on #5."])
        self.assertEqual(upcoming, datetime(2026, 10, 11, 6, 0, tzinfo=UTC))
        self.assertEqual(
            self.gh.dispatches,
            [
                {
                    "workflow": "nexkit.yml",
                    "ref": "main",
                    "inputs": {"command": "plan", "number": "5", "note": "", "auto": "resume"},
                }
            ],
        )
        [run] = self.gh.run_comments(5)
        self.assertNotIn("resume", run["run"])
        self.assertEqual(run["run"]["status"], "continued")
        self.assertIn("Resumed `/nexkit plan` in [a new run](https://x/runs/3)", due["body"])
        self.assertNotIn("⏸️ [paused]", due["body"])
        self.assertEqual(resume_paused(self.gh, "nexkit.yml", "main", now)[0], [])  # once
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_a_failed_resume_is_tried_again(self):
        now = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)
        self.paused(5, 1)
        self.gh.dispatch_errors["nexkit.yml"] = GitHubError(500, "Server error")
        [line], _ = resume_paused(self.gh, "nexkit.yml", "main", now)
        self.assertIn("Could not resume /nexkit plan on #5", line)
        self.assertIn("resume", self.gh.run_comments(5)[0]["run"])

    def test_the_schedule_resumes_only_when_enabled(self):
        self.paused(5, 1, at="2026-01-01T00:00:00Z")
        task = {"action": "maintain", "task": "schedule"}
        maintain(self.gh, task, make_config(usage_limit={"resume": False}), workflow="n.yml")
        self.assertFalse(self.gh.dispatches)
        # Without a wait, the schedule resumes due commands itself.
        cfg = make_config(usage_limit={"wait_minutes": 0})
        maintain(self.gh, task, cfg, workflow="n.yml", ref="main")
        self.assertEqual(self.gh.dispatches[-1]["inputs"]["command"], "plan")

    def test_the_schedule_hands_due_commands_to_the_waiting_run(self):
        now = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
        self.paused(5, 1)  # due at 04:40, within the wait
        lines = check_paused(self.gh, "nexkit.yml", "main", 330, now)
        self.assertEqual(lines, ["Started a run that resumes paused commands as they come due."])
        self.assertEqual(self.gh.dispatches[-1]["inputs"], WAIT_INPUTS)
        self.assertEqual(self.gh.run_comments(5)[0]["run"]["status"], "paused")
        # A resume beyond the wait is left to a later check.
        self.assertEqual(check_paused(self.gh, "nexkit.yml", "main", 60, now), [])
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_the_waiting_run_resumes_commands_as_they_come_due(self):
        clock = [datetime(2026, 10, 11, 4, 15, tzinfo=UTC)]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            clock[0] += timedelta(seconds=seconds)
            if len(slept) == 1:
                # A run that pauses while this one waits is resumed too.
                self.gh.add_issue(7)
                self.paused(7, 2, at="2026-10-11T04:50:00Z")

        lines = wait_and_resume(
            self.gh, "nexkit.yml", "main", 330, now=lambda tz: clock[0], sleep=sleep
        )
        self.assertEqual(lines, [])  # nothing was due at first
        self.paused(5, 1)  # due at 04:40
        lines = wait_and_resume(
            self.gh, "nexkit.yml", "main", 330, now=lambda tz: clock[0], sleep=sleep
        )
        self.assertEqual(lines, ["Resumed /nexkit plan on #5.", "Resumed /nexkit plan on #7."])
        self.assertEqual(slept, [600, 600, 300, 600])  # looks again at least every ten minutes
        self.assertEqual(clock[0], datetime(2026, 10, 11, 4, 50, tzinfo=UTC))

    def test_the_waiting_run_leaves_later_resumes_to_the_schedule(self):
        now = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
        self.paused(5, 1)  # due at 04:40
        sleep = mock.Mock()
        lines = wait_and_resume(self.gh, "nexkit.yml", "main", 60, now=lambda tz: now, sleep=sleep)
        self.assertEqual(lines, [])
        sleep.assert_not_called()
        self.assertFalse(self.gh.dispatches)

    def test_a_pull_request_command_continues_the_issues_paused_go(self):
        self.gh.add_pull(6, 5)
        go = {"url": "https://x/runs/1", "command": "go", "status": "paused"}
        self.gh.comment(5, render_run(go, "Pull request #6: ⏸️ Paused."))
        self.paused(5, 2)  # a paused plan on the issue keeps its own record
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "3"}):
            progress.start(self.gh, decision("review", comment_id=None))
        first, second = self.gh.run_comments(5)
        self.assertEqual(first["run"]["status"], "continued")
        self.assertIn("Continued in pull request #6.", first["body"])
        self.assertEqual(second["run"]["status"], "paused")
        self.assertIn("resume", second["run"])


class ReactionTests(unittest.TestCase):
    def test_failed_reaction_is_reported(self):
        gh = GitHub(repository="acme/app", token="t")
        stderr = io.StringIO()
        with (
            mock.patch.object(gh, "request", side_effect=GitHubError(403, "denied")),
            redirect_stderr(stderr),
        ):
            gh.react(3)
        self.assertIn("could not add the eyes reaction", stderr.getvalue())
        self.assertIn("denied", stderr.getvalue())


class DispatchTests(unittest.TestCase):
    def test_dispatch_asks_for_the_run_only_when_needed(self):
        gh = GitHub(repository="acme/app", token="t")
        with mock.patch.object(gh, "request", return_value=None) as request:
            gh.dispatch("nexkit.yml", "main", {"command": "fix"})
            gh.dispatch("ci.yml", "main", {}, run_details=True)
        path = "/repos/acme/app/actions/workflows/{}/dispatches"
        self.assertEqual(
            request.call_args_list,
            [
                mock.call(
                    "POST", path.format("nexkit.yml"), {"ref": "main", "inputs": {"command": "fix"}}
                ),
                mock.call(
                    "POST",
                    path.format("ci.yml"),
                    {"ref": "main", "inputs": {}, "return_run_details": True},
                ),
            ],
        )


class UpToDateRuleTests(unittest.TestCase):
    def answers(self, rules, protection):
        """A client whose branch rules API returns `rules` and whose classic protection
        returns `protection`, or raises it when it is an error."""
        gh = GitHub(repository="acme/app", token="t")

        def request(method, path, body=None, params=None):
            if "/rules/branches/" in path:
                return rules if params.get("page") == 1 else []
            if isinstance(protection, Exception):
                raise protection
            return protection

        patch = mock.patch.object(gh, "request", side_effect=request)
        patch.start()
        self.addCleanup(patch.stop)
        return gh

    def test_a_ruleset_or_classic_protection_can_want_branches_up_to_date(self):
        strict = {"type": "required_status_checks"}
        strict["parameters"] = {"strict_required_status_checks_policy": True}
        loose = {"type": "required_status_checks", "parameters": {}}
        self.assertTrue(self.answers([strict], GitHubError(404, "")).wants_up_to_date("main"))
        self.assertTrue(self.answers([loose], {"strict": True}).wants_up_to_date("main"))
        self.assertFalse(self.answers([loose], {"strict": False}).wants_up_to_date("main"))
        # The Actions token may not read classic protection: the rule counts as off.
        for status in (403, 404):
            gh = self.answers([], GitHubError(status, "Resource not accessible"))
            self.assertFalse(gh.wants_up_to_date("main"))
        with self.assertRaises(GitHubError):
            self.answers([], GitHubError(500, "down")).wants_up_to_date("main")


class LatestClosedIssueTests(unittest.TestCase):
    def test_the_issue_that_closed_last_not_a_pull_request_or_the_latest_updated(self):
        gh = GitHub(repository="acme/app", token="t")
        found = [
            {"number": 4, "closed_at": "2026-10-20T00:00:00Z"},  # commented on since
            {"number": 9, "closed_at": "2026-10-24T00:00:00Z", "pull_request": {}},
            {"number": 5, "closed_at": "2026-10-22T00:00:00Z"},
        ]
        with mock.patch.object(gh, "request", return_value=found) as request:
            self.assertEqual(gh.latest_closed_issue(1)["number"], 5)
        params = request.call_args.kwargs["params"]
        self.assertEqual((params["state"], params["milestone"]), ("closed", "1"))
        with mock.patch.object(gh, "request", return_value=[]):
            self.assertIsNone(gh.latest_closed_issue(1))


if __name__ == "__main__":
    unittest.main()
