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
from nexkit.state import empty_state, read_state, read_status, render_state
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

    def test_issue_command_edits_one_status_comment(self):
        d = decision("plan")
        progress.start(self.gh, d)
        self.assertEqual(self.gh.reactions, [(9, "eyes")])
        comments = self.bot_comments(5)
        self.assertEqual(len(comments), 1)
        self.assertIn(f"⏳ [running]({RUN}) `/nexkit plan`", comments[0]["body"])

        self.write("nexkit-agent", "result.json", {"status": "done"})
        outcome = self.finish(d, {"published": True, "comment": "https://x/plan"})
        self.assertEqual(outcome["outcome"], "planned")
        comments = self.bot_comments(5)
        self.assertEqual(len(comments), 1)
        self.assertIn(
            f"✅ [run log]({RUN}) `/nexkit plan`: [Plan](https://x/plan)", comments[0]["body"]
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
        self.assertIn("❌ [run log]", comments[0]["body"])
        self.assertIn(
            "`/nexkit go`: NexKit stopped during implement: Setup failed:\n  log",
            comments[0]["body"],
        )
        self.assertEqual(self.gh.reactions[-1], (9, "confused"))

    def test_issue_keeps_one_comment_across_commands(self):
        progress.start(self.gh, decision("plan"))
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "78"}):
            d = decision("implement")
            progress.start(self.gh, d)
            self.candidate()
            outcome = self.finish(d, {"published": True, "pr": 6, "head": HEAD})
        self.assertEqual(outcome["outcome"], "ready")
        comments = self.bot_comments(5)
        self.assertEqual(len(comments), 1)
        _, status = read_status(comments)
        self.assertEqual([r["command"] for r in status["runs"]], ["plan", "go"])
        self.assertEqual([r["status"] for r in status["runs"]], ["running", "success"])
        self.assertIn("Pull request #6: ✅ Checks passed", status["runs"][1]["text"])

    def test_pull_request_round_is_shown_while_running(self):
        d = decision("fix")
        progress.start(self.gh, d)
        self.assertEqual(self.gh.reactions, [(9, "eyes")])
        self.assertEqual(
            {(s["sha"], s["context"], s["state"], s["target_url"]) for s in self.gh.statuses},
            {(HEAD, "nexkit/checks", "pending", RUN), (HEAD, "nexkit/review", "pending", RUN)},
        )
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["rounds"][0]["status"], "running")
        self.assertIn(f"⏳ [running]({RUN})", self.bot_comments(6)[0]["body"])

        self.candidate()
        outcome = self.finish(d, {"published": True, "pr": 6, "head": NEW_HEAD})
        self.assertEqual(outcome["outcome"], "ready")
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(len(state["rounds"]), 1)  # the running row was completed
        self.assertEqual(state["rounds"][0]["status"], "success")
        self.assertEqual(state["rounds"][0]["head"], NEW_HEAD)
        latest = {(s["sha"], s["context"]): s["state"] for s in self.gh.statuses}
        self.assertEqual(latest[(NEW_HEAD, "nexkit/review")], "success")
        self.assertNotEqual(latest[(HEAD, "nexkit/checks")], "pending")
        self.assertEqual(self.gh.reactions[-1], (9, "rocket"))
        self.assertEqual(len(self.gh.comments_matching(5, "nexkit:status")), 0)

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
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(
            [(r["trigger"], r["status"]) for r in state["rounds"]], [("fix (auto)", "failure")]
        )
        self.assertTrue(self.gh.comments_matching(6, "Unclear"))
        self.assertEqual(self.gh.reactions, [])  # dispatched rounds have no comment
        self.assertFalse(self.gh.dispatches)

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


if __name__ == "__main__":
    unittest.main()
