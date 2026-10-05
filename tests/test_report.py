import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit.github import GitHubError
from nexkit.report import report, review_body, workflow_file
from nexkit.state import read_state
from tests.support import FakeGitHub, make_config

HEAD = "c" * 40
WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"


def decision(action="implement", **kw):
    base = {
        "action": action,
        "issue": 5,
        "pr": None,
        "target": 5,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": None,
        "note": "",
        "auto": False,
    }
    return {**base, **kw}


def review_result(verdict="approve", findings=()):
    return {
        "status": "done",
        "cost": 0.25,
        "output": {
            "verdict": verdict,
            "summary": "Looks right.",
            "criteria": [{"criterion": "add works", "met": True, "evidence": "test_add"}],
            "findings": list(findings),
        },
    }


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha=HEAD)
        self.tmp = tempfile.TemporaryDirectory()
        self.artifacts = Path(self.tmp.name)
        # Each report is a separate workflow run, whatever the environment running the tests.
        self.run_ids = itertools.count(1)
        env = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, artifact, name, data):
        path = self.artifacts / artifact / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def candidate(self, checks_pass=True, review=None):
        self.write("nexkit-agent", "result.json", {"status": "done", "cost": 1.0})
        self.write(
            "nexkit-checks",
            "checks.json",
            [
                {
                    "name": "test",
                    "run": "make test",
                    "exit_code": 0 if checks_pass else 1,
                    "passed": checks_pass,
                    "output": "" if checks_pass else "AssertionError",
                }
            ],
        )
        if review is not False:
            self.write("nexkit-review", "result.json", review or review_result())

    def run_report(self, d=None, cfg=None, published=True, head=HEAD):
        needs = {}
        if published:
            needs["publish"] = {
                "outputs": {"result": json.dumps({"published": True, "pr": 6, "head": head})}
            }
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        return report(
            self.gh,
            d or decision(),
            cfg or make_config(),
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )

    def statuses(self):
        return {(s["context"], s["state"]) for s in self.gh.statuses}

    def test_agent_failures_are_explained_on_the_issue(self):
        outcome = self.run_report(published=False)
        self.assertEqual(outcome["outcome"], "agent_failed")
        self.assertTrue(self.gh.comments_matching(5, "could not run"))
        self.write(
            "nexkit-agent",
            "result.json",
            {"status": "blocked", "error": "Need API key", "cost": 0.3},
        )
        outcome = self.run_report(published=False)
        self.assertEqual(outcome["outcome"], "agent_blocked")
        self.assertTrue(self.gh.comments_matching(5, "Need API key"))
        self.assertFalse(self.gh.dispatches)

    def test_publish_failure(self):
        self.write("nexkit-agent", "result.json", {"status": "done"})
        needs = {
            "publish": {
                "outputs": {"result": json.dumps({"published": False, "error": "protected"})}
            }
        }
        outcome = report(
            self.gh,
            decision(),
            make_config(),
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        self.assertEqual(outcome["outcome"], "publish_failed")
        self.assertTrue(self.gh.comments_matching(5, "protected"))

    def test_ready_candidate(self):
        self.candidate()
        outcome = self.run_report()
        self.assertEqual(outcome["outcome"], "ready")
        self.assertEqual(
            self.statuses(), {("nexkit/checks", "success"), ("nexkit/review", "success")}
        )
        self.assertEqual(self.gh.created_reviews[0]["commit_id"], HEAD)
        self.assertIn("✅ add works", self.gh.created_reviews[0]["body"])
        self.assertTrue(self.gh.comments_matching(6, "ready for a human decision"))
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["rounds"][0]["cost"], 1.25)
        self.assertEqual(state["rounds"][0]["verdict"], "approve")
        self.assertFalse(self.gh.dispatches)
        self.assertFalse(self.gh.merged)

    def test_auto_merge(self):
        self.candidate()
        self.assertEqual(self.run_report(cfg=make_config(auto_merge=True))["outcome"], "merged")
        self.assertEqual(self.gh.merged, [(6, HEAD, "squash")])

    def test_auto_merge_blocked_by_branch_protection(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Required approving review")
        self.assertEqual(self.run_report(cfg=make_config(auto_merge=True))["outcome"], "ready")
        self.assertTrue(self.gh.comments_matching(6, "Required approving review"))

    def test_failures_schedule_bounded_automatic_fixes(self):
        finding = {"severity": "blocking", "file": "calc.py", "line": 2, "body": "Wrong sign"}
        self.candidate(checks_pass=False, review=review_result("request_changes", [finding]))
        cfg = make_config(max_auto_fixes=1)
        first = self.run_report(cfg=cfg)
        self.assertEqual(first["outcome"], "auto_fix")
        self.assertEqual(
            self.gh.dispatches,
            [
                {
                    "workflow": "nexkit.yml",
                    "ref": "main",
                    "inputs": {"command": "fix", "number": "6", "note": "", "auto": "true"},
                }
            ],
        )
        self.assertIn(("nexkit/checks", "failure"), self.statuses())
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["auto_fixes"], 1)
        self.assertEqual(state["feedback"]["review"]["findings"][0]["body"], "Wrong sign")

        second = self.run_report(d=decision("fix", pr=6, target=6, head=HEAD, auto=True), cfg=cfg)
        self.assertEqual(second["outcome"], "needs_human")
        self.assertEqual(len(self.gh.dispatches), 1)
        state_comments = [c for c in self.gh.comments(6) if "nexkit:state" in c["body"]]
        self.assertEqual(len(state_comments), 1)  # updated in place
        _, state = read_state(self.gh.comments(6))
        self.assertEqual([r["trigger"] for r in state["rounds"]], ["implement", "fix (auto)"])

    def test_missing_review(self):
        self.candidate(review=False)
        self.assertEqual(self.run_report()["outcome"], "review_failed")
        self.assertIn(("nexkit/review", "error"), self.statuses())
        self.assertFalse(self.gh.dispatches)

    def test_review_command_does_not_need_an_agent(self):
        self.candidate()
        (self.artifacts / "nexkit-agent" / "result.json").unlink()
        d = decision("review", pr=6, target=6, head=HEAD)
        self.assertEqual(self.run_report(d=d, published=False)["outcome"], "ready")

    def test_state_keeps_the_previous_round_for_the_next_review(self):
        blocking = {"severity": "blocking", "file": "calc.py", "line": 2, "body": "Wrong sign"}
        hint = {"severity": "suggestion", "file": "", "line": 0, "body": "Add docs"}
        self.candidate(review=review_result("request_changes", [blocking, hint]))
        self.run_report()
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["last_review"]["head"], HEAD)
        self.assertEqual(state["last_review"]["round"], 1)
        self.assertEqual(
            [f["body"] for f in state["last_review"]["findings"]], ["Wrong sign", "Add docs"]
        )
        self.assertNotIn("fixes", state)

        new_head = "e" * 40
        self.write("nexkit-agent", "result.json", {"status": "done", "summary": "Kept docs."})
        (self.artifacts / "nexkit-review" / "result.json").unlink()  # the review failed
        d = decision("fix", pr=6, target=6, head=HEAD, note="Also handle floats")
        self.run_report(d=d, head=new_head)
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["last_review"]["head"], HEAD)  # unchanged without a review
        self.assertEqual(
            state["fixes"],
            [
                {
                    "round": 2,
                    "head": new_head,
                    "auto": False,
                    "summary": "Kept docs.",
                    "note": "Also handle floats",
                }
            ],
        )

    def test_review_body_shows_previous_findings(self):
        review = review_result()
        review["output"]["previous_findings"] = [
            {"finding": "Wrong sign", "resolution": "resolved", "evidence": "calc.py:2 adds"},
            {"finding": "Add docs", "resolution": "rejection_accepted", "evidence": "Internal"},
            {"finding": "Floats", "resolution": "unresolved", "evidence": "Still int()"},
        ]
        body = review_body(review, [])
        self.assertIn("**Previous findings**", body)
        self.assertIn("- ✅ resolved: Wrong sign: calc.py:2 adds", body)
        self.assertIn("- 🤝 rejection accepted: Add docs: Internal", body)
        self.assertIn("- 🛑 unresolved: Floats: Still int()", body)
        self.assertNotIn("Previous findings", review_body(review_result(), []))

    def test_workflow_file(self):
        self.assertEqual(workflow_file(WORKFLOW), "nexkit.yml")


if __name__ == "__main__":
    unittest.main()
