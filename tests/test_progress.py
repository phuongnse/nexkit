import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from nexkit import progress
from nexkit.github import GitHub, GitHubError
from nexkit.report import report
from nexkit.state import empty_state, read_state, render_state
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
        "cost": 0.25,
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
        self.write("nexkit-agent", "result.json", {"status": "done", "summary": "Done."})
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
        self.assertEqual(comments[0]["run"], {"url": RUN, "command": "plan", "status": "success"})
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
        self.assertIn("| ccccccc | - | - | - |", running["body"])

        self.candidate()
        outcome = self.finish(d, {"published": True, "pr": 6, "head": NEW_HEAD})
        self.assertEqual(outcome["outcome"], "ready")
        [done] = self.gh.run_comments(6)  # the running comment was completed
        self.assertEqual(done["id"], running["id"])
        self.assertEqual(done["run"]["status"], "success")
        self.assertIn(f"✅ [run log]({RUN})", done["body"])
        self.assertIn("| eeeeeee | passed | approve | $0.25 |", done["body"])
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
        self.assertIn("Unclear", stopped["body"])
        self.assertEqual(len(self.gh.comments(6)), 2)  # state and round, no extra comment
        self.assertEqual(self.gh.reactions, [])  # dispatched rounds have no comment
        self.assertFalse(self.gh.dispatches)

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


if __name__ == "__main__":
    unittest.main()
