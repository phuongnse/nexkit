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
            "criteria": [
                {
                    "criterion": "add works",
                    "met": True,
                    "test": "test_add",
                    "evidence": "test_calc.py:5 asserts add(2, 3) == 5",
                }
            ],
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
        [row] = self.gh.run_comments(6)
        self.assertIn("ready for a human decision", row["body"])
        self.assertEqual((row["run"]["cost"], row["run"]["verdict"]), (1.25, "approve"))
        self.assertFalse(self.gh.dispatches)
        self.assertFalse(self.gh.merged)

    def test_auto_merge(self):
        self.candidate()
        self.assertEqual(self.run_report(cfg=make_config(auto_merge=True))["outcome"], "merged")
        self.assertEqual(self.gh.merged, [(6, HEAD, "squash")])

    def test_auto_merge_closes_the_issue(self):
        self.candidate()
        self.run_report(cfg=make_config(auto_merge=True))
        self.assertEqual(
            (self.gh.issues[5]["state"], self.gh.issues[5]["state_reason"]), ("closed", "completed")
        )
        [note] = self.gh.comments_matching(5, "NexKit merged #6")
        self.assertIn("d" * 40, note["body"])
        [row] = self.gh.run_comments(6)
        self.assertIn("Closed #5 as completed.", row["body"])

    def test_auto_merge_closes_the_parent_issue_with_close_parent_issues(self):
        self.gh.add_issue(1, title="Epic")
        self.gh.add_issue(2, state="closed", reason="completed")
        self.gh.parents.update({2: 1, 5: 1})
        self.candidate()
        self.run_report(cfg=make_config(auto_merge=True))
        self.assertEqual(self.gh.issues[1]["state"], "open")  # the setting is off
        self.gh.issues[5]["state"] = "open"
        self.run_report(cfg=make_config(auto_merge=True, close_parent_issues=True))
        self.assertEqual(self.gh.issues[1]["state"], "closed")
        row = self.gh.run_comments(6)[-1]
        self.assertIn("Closed #5 as completed.\nClosed #1 as completed", row["body"])

    def test_an_issue_that_is_already_closed_stays_as_it_is(self):
        self.candidate()
        self.gh.issues[5].update(state="closed", state_reason="not_planned")
        self.run_report(cfg=make_config(auto_merge=True))
        self.assertEqual(self.gh.issues[5]["state_reason"], "not_planned")
        self.assertFalse(self.gh.comments_matching(5, "NexKit merged"))
        [row] = self.gh.run_comments(6)
        self.assertNotIn("Closed #5", row["body"])

    def test_failing_to_close_the_issue_keeps_the_merge(self):
        self.candidate()
        self.gh.close_errors[5] = GitHubError(403, "Resource not accessible by integration")
        outcome = self.run_report(cfg=make_config(auto_merge=True))
        self.assertEqual(outcome["outcome"], "merged")
        self.assertEqual(self.gh.merged, [(6, HEAD, "squash")])
        [row] = self.gh.run_comments(6)
        self.assertIn("Could not close #5: GitHub API 403", row["body"])
        self.assertEqual(row["run"]["status"], "success")

    def test_issue_stays_open_without_an_automatic_merge(self):
        self.candidate()
        self.run_report()
        self.assertEqual(self.gh.issues[5]["state"], "open")
        self.gh.merge_error = GitHubError(405, "Required approving review")
        self.run_report(cfg=make_config(auto_merge=True))
        self.assertEqual(self.gh.issues[5]["state"], "open")

    def test_auto_merge_blocked_by_branch_protection(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Required approving review")
        self.assertEqual(self.run_report(cfg=make_config(auto_merge=True))["outcome"], "ready")
        self.assertTrue(self.gh.comments_matching(6, "Required approving review"))
        self.assertFalse(self.gh.dispatches)

    def test_auto_merge_starts_the_base_branch_workflows(self):
        self.candidate()
        run = "https://github.com/acme/app/actions/runs/77"
        self.gh.dispatch_runs["ci.yml"] = run
        cfg = make_config(auto_merge=True, after_merge_workflows=["ci.yml", "e2e.yml"])
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app"}):
            outcome = self.run_report(d=decision(base="develop"), cfg=cfg)
        self.assertEqual(outcome["outcome"], "merged")
        self.assertEqual(
            self.gh.dispatches,
            [
                {"workflow": "ci.yml", "ref": "develop", "inputs": {}},
                {"workflow": "e2e.yml", "ref": "develop", "inputs": {}},
            ],
        )
        [row] = self.gh.run_comments(6)
        self.assertIn("After the merge, on `develop`:", row["body"])
        self.assertIn(f"- Started [`ci.yml`]({run})", row["body"])
        # Without run details, the link lists the workflow's dispatched runs on the branch.
        url = "https://github.com/acme/app/actions/workflows/e2e.yml"
        self.assertIn(
            f"- Started [`e2e.yml`]({url}?query=branch%3Adevelop+event%3Aworkflow_dispatch)",
            row["body"],
        )

    def test_failed_dispatch_after_merge_keeps_the_merge(self):
        self.candidate()
        self.gh.dispatch_errors["ci.yml"] = GitHubError(
            422, "Workflow does not have 'workflow_dispatch' trigger"
        )
        cfg = make_config(auto_merge=True, after_merge_workflows=["ci.yml", "e2e.yml"])
        outcome = self.run_report(cfg=cfg)
        self.assertEqual(outcome["outcome"], "merged")
        self.assertEqual(self.gh.merged, [(6, HEAD, "squash")])
        self.assertEqual([d["workflow"] for d in self.gh.dispatches], ["e2e.yml"])
        [row] = self.gh.run_comments(6)
        self.assertIn(
            "- Could not start `ci.yml`: GitHub API 422: "
            "Workflow does not have 'workflow_dispatch' trigger",
            row["body"],
        )
        self.assertIn("- Started [`e2e.yml`]", row["body"])
        self.assertEqual(row["run"]["status"], "success")

    def test_no_workflows_start_without_auto_merge(self):
        self.candidate()
        self.run_report(cfg=make_config(after_merge_workflows=["ci.yml"]))
        self.assertFalse(self.gh.merged)
        self.assertFalse(self.gh.dispatches)

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
        self.assertIn("Automatic fix rounds used: 1.", state_comments[0]["body"])
        self.assertEqual(
            [(c["run"]["round"], c["run"]["trigger"]) for c in self.gh.run_comments(6)],
            [(1, "implement"), (2, "fix (auto)")],
        )

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

    def test_blocked_conflicts_are_listed_for_a_person(self):
        open_conflict = {
            "file": "Program.cs",
            "base_change": "Adds request validation before the endpoints.",
            "pr_change": "Moves the endpoints above the middleware.",
            "question": "Should validation run before this pull request's endpoint?",
        }
        self.write(
            "nexkit-agent",
            "result.json",
            {
                "status": "blocked",
                "error": "Two conflicts need a decision.",
                "cost": 0.4,
                "start_base": "b" * 40,
                "conflicts": ["Program.cs"],
                "output": {"status": "blocked", "open_conflicts": [open_conflict]},
            },
        )
        d = decision("fix", pr=6, target=6, head=HEAD)
        outcome = self.run_report(d=d, published=False)
        self.assertEqual(outcome["outcome"], "agent_blocked")
        self.assertFalse(self.gh.dispatches)
        [row] = self.gh.run_comments(6)
        body = row["body"]
        self.assertIn("Two conflicts need a decision.", body)
        self.assertIn(
            "**Conflicts with `main` that need your decision**\n\n- `Program.cs`\n"
            "  - `main`: Adds request validation before the endpoints.\n"
            "  - This pull request: Moves the endpoints above the middleware.\n"
            "  - **Question:** Should validation run before this pull request's endpoint?",
            body,
        )
        self.assertIn("`/nexkit fix` followed by your decisions", body)
        self.assertEqual(row["run"]["status"], "failure")

    def test_round_comment_says_the_base_was_merged(self):
        self.candidate()
        self.write(
            "nexkit-agent",
            "result.json",
            {
                "status": "done",
                "summary": "Kept both.",
                "cost": 1.0,
                "start_base": "b" * 40,
                "conflicts": ["Program.cs", "Axis.Server.csproj"],
            },
        )
        d = decision("fix", pr=6, target=6, head="a" * 40)
        self.assertEqual(self.run_report(d=d)["outcome"], "ready")
        [row] = self.gh.run_comments(6)
        self.assertIn(
            "Merged `main` (`bbbbbbb`) into the branch and resolved conflicts in "
            "`Program.cs`, `Axis.Server.csproj`.",
            row["body"],
        )
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["fixes"][-1]["conflicts"], ["Program.cs", "Axis.Server.csproj"])

    def test_review_body_shows_previous_findings(self):
        review = review_result()
        review["output"]["previous_findings"] = [
            {
                "finding": "Wrong sign",
                "severity": "blocking",
                "resolution": "resolved",
                "evidence": "calc.py:2 adds",
            },
            {
                "finding": "Add docs",
                "severity": "suggestion",
                "resolution": "rejection_accepted",
                "evidence": "Internal",
            },
            {
                "finding": "Floats",
                "severity": "blocking",
                "resolution": "unresolved",
                "evidence": "Still int()",
            },
        ]
        body = review_body(review, [])
        short, evidence = body.split("<details>", 1)
        self.assertIn("**Previous findings**", short)
        self.assertIn("- ✅ resolved: Wrong sign\n", short)
        self.assertIn("- 🤝 rejection accepted: Add docs\n", short)
        self.assertIn("- 🛑 Blocking, unresolved: Floats\n", short)
        self.assertIn("- ✅ resolved: Wrong sign: calc.py:2 adds", evidence)
        self.assertIn("- 🤝 rejection accepted: Add docs: Internal", evidence)
        self.assertIn("- 🛑 Blocking, unresolved: Floats: Still int()", evidence)
        self.assertNotIn("Previous findings", review_body(review_result(), []))

    def test_only_blocking_findings_get_the_stop_sign(self):
        review = review_result()
        review["output"]["previous_findings"] = [
            {"finding": "Null check", "severity": "blocking", "resolution": "unresolved"},
            {"finding": "Rename x", "severity": "suggestion", "resolution": "unresolved"},
            {"finding": "Add docs", "severity": "suggestion", "resolution": "resolved"},
        ]
        body = review_body(review, [])
        previous = body.split("**Previous findings**", 1)[1].split("**Acceptance criteria**")[0]
        lines = previous.strip().splitlines()
        self.assertEqual(
            lines,
            [
                "- 🛑 Blocking, unresolved: Null check",
                "- 💡 Suggestion, still open: Rename x",
                "- ✅ resolved: Add docs",
            ],
        )
        self.assertEqual(body.count("🛑"), 1)

    def test_review_body_puts_findings_first_and_evidence_last(self):
        findings = [
            {"severity": "suggestion", "file": "calc.py", "line": 0, "body": "Add a docstring."},
            {"severity": "blocking", "file": "calc.py", "line": 2, "body": "Floats are cut."},
        ]
        review = review_result("request_changes", findings)
        review["output"]["criteria"].append(
            {"criterion": "floats add", "met": False, "test": "", "evidence": "No float test."}
        )
        checks = [{"name": "test", "passed": True}]
        body = review_body(review, checks)
        order = [
            "### NexKit review: changes requested",
            "Looks right.",
            "**Things to look at**",
            "- 🛑 Blocking: Floats are cut. (`calc.py:2`)",
            "- 💡 Suggestion: Add a docstring. (`calc.py`)",
            "**Acceptance criteria**",
            "- ✅ add works (`test_add`)",
            "- ❌ floats add (no test)",
            "**Checks**: ✅ `test`",
            "<details><summary>Evidence</summary>",
            "- ✅ add works: test_calc.py:5 asserts add(2, 3) == 5",
            "- ❌ floats add: No float test.",
            "</details>",
        ]
        positions = [body.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("<details><summary>Evidence</summary>\n\n", body)
        short = body.split("<details>", 1)[0]
        self.assertNotIn("asserts", short)

    def test_review_body_without_findings_or_evidence(self):
        review = review_result()
        review["output"]["criteria"][0]["evidence"] = ""
        body = review_body(review, [])
        self.assertNotIn("Things to look at", body)
        self.assertNotIn("<details>", body)
        self.assertIn("- ✅ add works (`test_add`)", body)

    def test_workflow_file(self):
        self.assertEqual(workflow_file(WORKFLOW), "nexkit.yml")


if __name__ == "__main__":
    unittest.main()
