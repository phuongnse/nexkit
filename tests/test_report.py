import contextlib
import io
import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit import cli
from nexkit.github import GitHubError
from nexkit.maintain import WAIT_INPUTS
from nexkit.progress import trigger
from nexkit.report import criterion_status, report, review_body, workflow_file
from nexkit.route import route
from nexkit.state import read_state, render_state
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


TOKENS = {"input": 1000, "cached": 800, "output": 200}


def review_result(verdict="approve", findings=()):
    return {
        "status": "done",
        "tokens": {"input": 300, "cached": 100, "output": 50},
        "output": {
            "verdict": verdict,
            "summary": "Looks right.",
            "criteria": [
                {
                    "criterion": "add works",
                    "status": "met",
                    "test": "test_add",
                    "evidence": "test_calc.py:5 asserts add(2, 3) == 5",
                }
            ],
            "findings": list(findings),
        },
    }


LIMIT = {
    "status": "paused",
    "error": "Claude hit the account's usage limit: You've hit your session limit",
    "limit": "You've hit your session limit · resets 4:40am (UTC)",
    "resume_at": "2026-10-11T04:40:00Z",
    "reset_known": True,
}


class ReportCase(unittest.TestCase):
    """A NexKit pull request and helpers to run `report` on it."""

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
        self.write("nexkit-agent", "result.json", {"status": "done", "tokens": TOKENS})
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

    def run_report(self, d=None, cfg=None, published=True, head=HEAD, needs_review=None):
        needs = {"review": {"result": needs_review}} if needs_review else {}
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


class ReportTests(ReportCase):
    def no_change(self, cfg=None):
        output = {
            "status": "done",
            "summary": "Nothing to change.",
            "no_change": "Fixed by 8b9cb87.",
        }
        self.write(
            "nexkit-agent", "result.json", {"status": "done", "no_change": True, "output": output}
        )
        needs = {
            "publish": {"outputs": {"result": json.dumps({"published": False, "no_change": True})}}
        }
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        return report(
            self.gh,
            decision(),
            cfg or make_config(),
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )

    def test_a_round_that_finds_no_change_needed_closes_the_issue_with_the_finding(self):
        self.gh.add_issue(3)
        self.gh.parents[5] = 3
        outcome = self.no_change(make_config(close_parent_issues=True))
        self.assertEqual((outcome["outcome"], outcome.get("attention")), ("no_change", None))
        self.assertEqual(self.gh.issues[5]["state_reason"], "completed")
        [row] = self.gh.run_comments(5)
        self.assertEqual(row["run"]["status"], "success")
        self.assertIn("No change is needed, so NexKit closed #5 as completed.", row["body"])
        self.assertIn("Fixed by 8b9cb87.", row["body"])
        self.assertEqual(self.gh.issues[3]["state"], "closed")  # its only sub-issue closed
        # When the issue cannot be closed, a person closes it.
        self.gh.issues[5]["state"] = "open"
        self.gh.close_errors[5] = GitHubError(403, "Forbidden")
        outcome = self.no_change()
        self.assertEqual(outcome["outcome"], "close_failed")
        self.assertIn("could not close the issue; close it yourself", outcome["attention"])
        self.assertEqual(self.gh.run_comments(5)[-1]["run"]["status"], "failure")
        self.assertIn("could not close #5", outcome["summary"])

    def test_agent_failures_are_explained_on_the_issue(self):
        # The try after a job that failed as a whole: a person decides.
        outcome = self.run_report(d=decision(retry=True), published=False)
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertEqual(outcome["attention"], "the run stopped on a transient error twice")
        self.assertTrue(self.gh.comments_matching(5, "could not run"))
        self.write(
            "nexkit-agent",
            "result.json",
            {"status": "blocked", "error": "Need API key"},
        )
        outcome = self.run_report(published=False)
        self.assertEqual(outcome["outcome"], "agent_blocked")
        self.assertTrue(self.gh.comments_matching(5, "Need API key"))
        self.assertFalse(self.gh.dispatches)

    def test_a_transient_error_runs_the_command_once_more(self):
        cfg = make_config(notify=["olivia"])
        error = "Claude stopped (success). API Error: 529 overloaded"
        self.write(
            "nexkit-agent", "result.json", {"status": "error", "error": error, "transient": True}
        )
        outcome = self.run_report(d=decision(note="Use the cache."), cfg=cfg, published=False)
        self.assertEqual(outcome["outcome"], "retry")
        self.assertIsNone(outcome.get("attention"))
        [dispatch] = self.gh.dispatches
        self.assertEqual(
            dispatch["inputs"],
            {"command": "go", "number": "5", "note": "Use the cache.", "auto": "retry"},
        )
        [run] = [c["run"] for c in self.gh.run_comments(5)]
        self.assertEqual(run["status"], "continued")
        self.assertIn("API Error: 529", run["transient_error"])
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))
        # The retry stops the same way: a person decides, with both errors in the comment.
        retry = decision(note="Use the cache.", retry=True)
        self.write(
            "nexkit-agent",
            "result.json",
            {"status": "error", "error": "Connection error", "transient": True},
        )
        outcome = self.run_report(d=retry, cfg=cfg, published=False)
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertIn(
            "The try before stopped on a transient error too: Claude stopped", outcome["summary"]
        )
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertTrue(self.gh.comments_matching(5, "@olivia a person is needed"))

    def test_errors_that_another_try_cannot_fix_run_nothing_again(self):
        for result in (
            {"status": "error", "error": "Claude stopped (error_max_turns)."},
            {
                "status": "error",
                "error": "Claude did not finish within 30 minutes.",
                "timed_out": True,
            },
            {"status": "blocked", "error": "Which port?"},
            {
                "status": "error",
                "error": "The diagnosis round changed code without naming the cause",
            },
        ):
            self.write("nexkit-agent", "result.json", result)
            self.assertNotEqual(self.run_report(published=False)["outcome"], "retry")
        self.assertFalse(self.gh.dispatches)
        # A job a person cancelled is not started again either.
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        (self.artifacts / "nexkit-agent" / "result.json").unlink()
        outcome = report(
            self.gh,
            decision(),
            make_config(),
            {"agent": {"result": "cancelled"}},
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        self.assertEqual(outcome["outcome"], "agent_failed")
        self.assertFalse(self.gh.dispatches)

    def test_an_automatic_round_whose_branch_moved_runs_once_more(self):
        self.write("nexkit-agent", "result.json", {"status": "done", "tokens": TOKENS})
        moved = {"published": False, "error": "The branch moved from a to b.", "moved": True}
        needs = {"publish": {"outputs": {"result": json.dumps(moved)}}}
        state = {"version": 2, "issue": 5, "auto_fixes": 1, "feedback": None}
        self.gh.comment(6, render_state(state))
        for auto, expected in ((True, "retry"), (False, "publish_failed")):
            os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
            fix = decision("fix", pr=6, target=6, head=HEAD, auto=auto)
            outcome = report(
                self.gh,
                fix,
                make_config(),
                needs,
                self.artifacts,
                workflow_ref=WORKFLOW,
                default_branch="main",
            )
            self.assertEqual(outcome["outcome"], expected)
        [dispatch] = self.gh.dispatches
        self.assertEqual(
            dispatch["inputs"], {"command": "fix", "number": "6", "note": "", "auto": "true,retry"}
        )
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 1)
        # The retried round keeps the label, and the conflict rounds never count it.
        self.assertEqual(trigger(decision("fix", auto=True, retry=True)), "fix (auto, retry)")

    def test_a_review_that_never_ran_is_retried_only_after_a_failed_verify_job(self):
        self.candidate(review=False)
        fix = decision("fix", pr=6, target=6, head=HEAD)
        for verify, expected in (("success", "review_failed"), ("failure", "retry")):
            os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
            published = {"published": True, "pr": 6, "head": HEAD}
            needs = {
                "publish": {"outputs": {"result": json.dumps(published)}},
                "verify": {"result": verify},
                "review": {"result": "skipped"},
            }
            outcome = report(
                self.gh,
                fix,
                make_config(),
                needs,
                self.artifacts,
                workflow_ref=WORKFLOW,
                default_branch="main",
            )
            self.assertEqual(outcome["outcome"], expected)

    def test_a_review_that_failed_on_a_transient_error_runs_once_more(self):
        self.candidate(review={"status": "error", "error": "API Error: 503", "transient": True})
        outcome = self.run_report(d=decision("fix", pr=6, target=6, head=HEAD, autopilot=True))
        self.assertEqual(outcome["outcome"], "retry")
        [dispatch] = self.gh.dispatches
        self.assertEqual(
            dispatch["inputs"],
            {"command": "review", "number": "6", "note": "", "auto": "autopilot,retry"},
        )

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
        # The round counts the tokens of both agents.
        both = {"input": 1300, "cached": 900, "output": 250}
        self.assertEqual((row["run"]["tokens"], row["run"]["verdict"]), (both, "approve"))
        self.assertIn("| 1.3k in (900 cached), 250 out |", row["body"])
        self.assertFalse(self.gh.dispatches)
        self.assertFalse(self.gh.merged)

    def test_a_round_without_every_count_shows_no_total(self):
        review = review_result()
        del review["tokens"]
        self.candidate(review=review)
        self.run_report()
        [row] = self.gh.run_comments(6)
        self.assertIsNone(row["run"]["tokens"])
        self.assertIn("| approve | N/A |", row["body"])

    def test_auto_merge(self):
        self.candidate()
        self.assertEqual(
            self.run_report(cfg=make_config(merge={"auto": True}))["outcome"], "merged"
        )
        self.assertEqual(self.gh.merged, [(6, HEAD, "squash")])

    def test_a_pull_request_whose_change_is_already_on_its_base_branch_closes(self):
        self.candidate()
        self.gh.on_base.add(HEAD)
        outcome = self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(outcome["outcome"], "already_on_base")
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.gh.pulls[6]["state"], "closed")
        self.assertEqual(self.gh.issues[5]["state_reason"], "completed")
        [row] = self.gh.run_comments(6)
        self.assertEqual(
            (row["run"]["status"], row["run"]["outcome"]), ("success", "already_on_base")
        )
        self.assertIn("`main` already holds the change of this pull request, so", row["body"])
        self.assertIn("NexKit closed it without merging.", row["body"])
        self.assertIn("Closed #5 as completed.", row["body"])

    def test_auto_merge_closes_the_issue(self):
        self.candidate()
        self.run_report(cfg=make_config(merge={"auto": True}))
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
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(self.gh.issues[1]["state"], "open")  # the setting is off
        self.gh.issues[5]["state"] = "open"
        self.run_report(cfg=make_config(merge={"auto": True}, close_parent_issues=True))
        self.assertEqual(self.gh.issues[1]["state"], "closed")
        row = self.gh.run_comments(6)[-1]
        self.assertIn("Closed #5 as completed.\nClosed #1 as completed", row["body"])

    def test_an_issue_that_is_already_closed_stays_as_it_is(self):
        self.candidate()
        self.gh.issues[5].update(state="closed", state_reason="not_planned")
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(self.gh.issues[5]["state_reason"], "not_planned")
        self.assertFalse(self.gh.comments_matching(5, "NexKit merged"))
        [row] = self.gh.run_comments(6)
        self.assertNotIn("Closed #5", row["body"])

    def test_failing_to_close_the_issue_keeps_the_merge(self):
        self.candidate()
        self.gh.close_errors[5] = GitHubError(403, "Resource not accessible by integration")
        outcome = self.run_report(cfg=make_config(merge={"auto": True}))
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
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(self.gh.issues[5]["state"], "open")

    def test_auto_merge_blocked_by_branch_protection(self):
        self.candidate()
        self.gh.merge_error = GitHubError(405, "Required approving review")
        outcome = self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(outcome["outcome"], "merge_refused")
        self.assertTrue(self.gh.comments_matching(6, "Required approving review"))
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["status"], "success")
        self.assertFalse(self.gh.dispatches)

    def test_auto_merge_starts_the_base_branch_workflows(self):
        self.candidate()
        run = "https://github.com/acme/app/actions/runs/77"
        self.gh.dispatch_runs["ci.yml"] = run
        cfg = make_config(merge={"auto": True, "after_workflows": ["ci.yml", "e2e.yml"]})
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
        self.assertEqual(
            row["run"]["after_merge"],
            [
                {"workflow": "ci.yml", "run_id": 1, "url": run},
                {"workflow": "e2e.yml", "run_id": None, "url": row["run"]["after_merge"][1]["url"]},
            ],
        )
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
        cfg = make_config(merge={"auto": True, "after_workflows": ["ci.yml", "e2e.yml"]})
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
        self.run_report(cfg=make_config(merge={"after_workflows": ["ci.yml"]}))
        self.assertFalse(self.gh.merged)
        self.assertFalse(self.gh.dispatches)

    def criterion(self, status, name="floats add"):
        review = review_result()
        review["output"]["criteria"].append(
            {"criterion": name, "status": status, "test": "", "evidence": "No float test."}
        )
        return review

    def test_an_unmet_criterion_requests_changes_whatever_the_verdict(self):
        self.candidate(review=self.criterion("unmet"))
        outcome = self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(outcome["outcome"], "auto_fix")
        self.assertFalse(self.gh.merged)
        self.assertIn(("nexkit/review", "failure"), self.statuses())
        self.assertEqual(self.gh.dispatches[0]["inputs"]["command"], "fix")
        # The fix round and the next review get the criterion as a blocking finding.
        _, state = read_state(self.gh.comments(6))
        [finding] = state["feedback"]["review"]["findings"]
        self.assertEqual(finding["severity"], "blocking")
        self.assertEqual(
            finding["body"], "Acceptance criterion not met: floats add. No float test."
        )
        self.assertEqual(state["last_review"]["verdict"], "request_changes")
        body = self.gh.created_reviews[0]["body"]
        self.assertIn("### NexKit review: changes requested", body)
        self.assertIn("because an acceptance criterion is not met", body)
        self.assertIn("- ❌ floats add (no test)", body)

    def test_an_unverifiable_criterion_leaves_the_merge_to_a_person(self):
        self.candidate(review=self.criterion("unverifiable", "passes five runs in a row"))
        cfg = make_config(merge={"auto": True}, notify=["owner"])
        outcome = self.run_report(cfg=cfg)
        self.assertEqual(outcome["outcome"], "ready")
        self.assertFalse(self.gh.merged)
        self.assertFalse(self.gh.dispatches)
        [review] = [s for s in self.gh.statuses if s["context"] == "nexkit/review"]
        self.assertEqual(review["state"], "success")
        self.assertIn("could not verify", review["description"])
        [row] = self.gh.run_comments(6)
        self.assertIn("NexKit does not merge it by itself.", row["body"])
        self.assertIn("Confirm them before merging:\n\n- passes five runs in a row", row["body"])
        self.assertIn("could not verify", row["run"]["attention"])
        [mention] = [c["body"] for c in self.gh.comments(6) if c["body"].startswith("@owner")]
        self.assertIn("confirm them before merging", mention)
        body = self.gh.created_reviews[0]["body"]
        self.assertIn("- ❔ passes five runs in a row (no test)", body)
        self.assertEqual(self.gh.issues[5]["state"], "open")

    def test_an_unverifiable_criterion_without_auto_merge_is_listed(self):
        self.candidate(review=self.criterion("unverifiable"))
        self.assertEqual(self.run_report()["outcome"], "ready")
        [row] = self.gh.run_comments(6)
        self.assertNotIn("does not merge it by itself", row["body"])
        self.assertIn("- floats add", row["body"])

    def test_an_unverifiable_criterion_of_a_rejected_review_changes_nothing(self):
        finding = {"severity": "blocking", "file": "calc.py", "line": 2, "body": "Wrong sign"}
        review = self.criterion("unverifiable")
        review["output"].update(verdict="request_changes", findings=[finding])
        self.candidate(review=review)
        self.assertEqual(self.run_report()["outcome"], "auto_fix")
        self.assertNotIn("could not verify", self.gh.created_reviews[0]["body"])

    def test_a_stored_review_from_before_criteria_had_a_status(self):
        self.assertEqual(criterion_status({"met": True}), "met")
        self.assertEqual(criterion_status({"met": False}), "unmet")
        self.assertEqual(criterion_status({"status": "unverifiable"}), "unverifiable")

    def test_failures_schedule_bounded_automatic_fixes(self):
        finding = {"severity": "blocking", "file": "calc.py", "line": 2, "body": "Wrong sign"}
        self.candidate(checks_pass=False, review=review_result("request_changes", [finding]))
        cfg = make_config(fix={"max_auto_rounds": 1})
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

    def test_a_round_held_up_by_workflow_changes_gives_its_automatic_round_back(self):
        cfg = make_config(fix={"max_auto_rounds": 1}, notify=["olivia"])
        self.candidate(checks_pass=False)
        self.assertEqual(self.run_report(cfg=cfg)["outcome"], "auto_fix")
        fix = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        # Stopped before its agent: the merge it must make changes workflow files.
        self.write(
            "nexkit-agent",
            "result.json",
            {"status": "needs_merge", "error": "Merging `main` brings changes to `ci.yml`."},
        )
        outcome = self.run_report(d=fix, cfg=cfg, published=False)
        self.assertEqual(outcome["outcome"], "needs_merge")
        self.assertIn("merge it into the pull request yourself", outcome["attention"])
        self.assertTrue(self.gh.comments_matching(6, "@olivia a person is needed"))
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["auto_fixes"], 0)
        # Refused at publish for the same reason: given back as well.
        state_id, state = read_state(self.gh.comments(6))
        self.gh.update_comment(state_id, render_state({**state, "auto_fixes": 1}))
        self.write("nexkit-agent", "result.json", {"status": "done", "tokens": TOKENS})
        refused = {"published": False, "error": "Merging `main` brings…", "workflows": True}
        needs = {"publish": {"outputs": {"result": json.dumps(refused)}}}
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        outcome = report(
            self.gh, fix, cfg, needs, self.artifacts, workflow_ref=WORKFLOW, default_branch="main"
        )
        self.assertEqual(outcome["outcome"], "needs_merge")
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 0)
        # So the next failing round starts its automatic fix round.
        self.candidate(checks_pass=False)
        self.gh.dispatches.clear()
        self.assertEqual(self.run_report(d=fix, cfg=cfg)["outcome"], "auto_fix")
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_a_round_that_left_out_the_base_merge_says_so(self):
        self.candidate()
        agent = {"status": "done", "tokens": TOKENS, "merge_held": [".github/workflows/ci.yml"]}
        self.write("nexkit-agent", "result.json", agent)
        self.run_report(d=decision("fix", pr=6, target=6, head=HEAD, auto=True))
        [row] = self.gh.run_comments(6)
        self.assertIn("Did not merge `main`, which changed `.github/workflows/ci.yml`", row["body"])

    def test_state_names_the_run_that_holds_the_checks_result_files(self):
        self.candidate(checks_pass=False, review=review_result("request_changes"))
        self.run_report()
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["feedback"]["run_id"], 1)
        self.assertFalse(state["feedback"]["checks"][0]["passed"])

    def failing(self, output):
        check = {"name": "e2e", "run": "scripts/e2e.sh", "exit_code": 1, "passed": False}
        self.write("nexkit-checks", "checks.json", [{**check, "output": output}])

    def fix_round(self, output, head, cause=""):
        agent = {"status": "done", "summary": "Wrapped the popup.", "output": {"cause": cause}}
        self.write("nexkit-agent", "result.json", agent)
        self.failing(output)
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        return self.run_report(d=d, cfg=make_config(fix={"max_auto_rounds": 5}), head=head)

    def next_round(self):
        inputs = self.gh.dispatches[-1]["inputs"]
        return route(self.gh, "workflow_dispatch", {"sender": {"login": "nexkit"}}, inputs)

    def test_a_check_that_fails_the_same_way_twice_gets_a_diagnosis_round(self):
        self.candidate(checks_pass=False, review=review_result("request_changes"))
        self.failing("Running 3 tests\n  ✘ 1 desk.spec.ts:42 › picks an option (12.3s)\n")
        self.run_report(cfg=make_config(fix={"max_auto_rounds": 5}))
        self.assertNotIn("diagnose", self.next_round())  # no fix round has run yet

        again = "Running 4 tests\n  ✘ 3 desk.spec.ts:42 › picks an option (9.1s)\n"
        self.fix_round(again, "e" * 40)
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["feedback"]["repeated"], ["e2e"])
        signature = "✘ desk.spec.ts: › picks an option ()"
        self.assertEqual(state["fixes"][-1]["failing"], [{"name": "e2e", "signature": signature}])
        diagnosis = self.next_round()
        self.assertEqual(diagnosis["diagnose"], ["e2e"])
        self.assertEqual(trigger(diagnosis), "fix (auto, diagnosis)")

        # The diagnosis round found the cause; the check now fails another way.
        cause = "The popup renders outside the modal, so the click misses it (trace.zip)."
        self.fix_round("  ✘ 1 desk.spec.ts:50 › saves the ticket (1.0s)\n", "f" * 40, cause)
        _, state = read_state(self.gh.comments(6))
        self.assertNotIn("repeated", state["feedback"])
        self.assertEqual(state["fixes"][-1]["cause"], cause)
        self.assertNotIn("diagnose", self.next_round())
        text = self.gh.run_comments(6)[-1]["body"]
        self.assertIn(f"**Cause found by the fix agent:** {cause}", text)

    def test_missing_review(self):
        self.candidate(review=False)
        outcome = self.run_report(needs_review="cancelled")
        self.assertEqual(outcome["outcome"], "review_failed")
        self.assertIn("the review job left no result", outcome["summary"])
        self.assertIn(("nexkit/review", "error"), self.statuses())
        self.assertFalse(self.gh.dispatches)

    def test_a_review_that_raised_names_the_exception(self):
        self.candidate(review=False)
        out = self.artifacts / "nexkit-review"
        self.write("nexkit-review", "context.json", {})
        env = {
            "NEXKIT_DECISION": json.dumps(decision("review", pr=6, target=6, head=HEAD)),
            "NEXKIT_CONFIG": json.dumps(make_config()),
            "GITHUB_STEP_SUMMARY": str(self.artifacts / "summary.md"),
        }
        error = UnicodeDecodeError("utf-8", b"\xc5", 0, 1, "invalid continuation byte")
        args = ["agent", "--stage", "review", "--repo", self.tmp.name, "--out", str(out)]
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("nexkit.agent.run_stage", side_effect=error),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(cli.main(args), 0)
        self.assertIn("::stop-commands::", stdout.getvalue())
        self.assertIn("Traceback", stdout.getvalue())
        outcome = self.run_report(d=decision("review", pr=6, target=6, head=HEAD))
        self.assertEqual(outcome["outcome"], "review_failed")
        round_comment = self.gh.run_comments(6)[-1]["body"]
        self.assertIn("NexKit stopped on UnicodeDecodeError: 'utf-8' codec", round_comment)
        self.assertNotIn("left no result", round_comment)

    def review_status(self):
        return [s["description"] for s in self.gh.statuses if s["context"] == "nexkit/review"]

    def test_a_review_that_stopped_says_why(self):
        error = (
            "Claude stopped (error_during_execution). API Error: 401 authentication_error: "
            "Invalid bearer token."
        )
        self.candidate(review={"status": "error", "error": error})
        outcome = self.run_report()
        self.assertEqual(outcome["outcome"], "review_failed")
        self.assertIn(f"The AI review did not complete: {error}", outcome["summary"])
        self.assertIn("docs/troubleshooting.md", outcome["summary"])
        self.assertNotIn("to try again", outcome["summary"])
        round_comment = self.gh.run_comments(6)[-1]["body"]
        self.assertIn("401 authentication_error", round_comment)
        [status] = self.review_status()
        self.assertTrue(
            status.startswith("AI review did not complete: Claude stopped (error_during")
        )
        self.assertLessEqual(len(status), 140)

    def test_a_review_that_timed_out_can_be_tried_again(self):
        self.candidate(
            review={"status": "error", "error": "Claude did not finish within 25 minutes."}
        )
        outcome = self.run_report()
        self.assertIn(
            "did not complete: Claude did not finish within 25 minutes.", outcome["summary"]
        )
        self.assertIn("Comment `/nexkit review` to try again.", outcome["summary"])
        self.assertEqual(
            self.review_status(),
            ["AI review did not complete: Claude did not finish within 25 minutes."],
        )

    def test_a_review_without_a_verdict_says_so(self):
        self.candidate(review={"status": "done", "output": None})
        outcome = self.run_report()
        self.assertEqual(outcome["outcome"], "review_failed")
        self.assertIn("the review agent returned no verdict", outcome["summary"])

    def test_the_issue_hears_why_the_review_stopped(self):
        self.candidate(
            review={
                "status": "error",
                "error": "Claude stopped (error_during_execution). API Error: 403 Forbidden",
            }
        )
        outcome = self.run_report()
        self.assertTrue(outcome["summary"].startswith("Pull request #6: The AI review did"))
        self.assertIn("403 Forbidden", outcome["summary"])

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
                    "failing": [],  # every check passed after it
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
                "start_base": "b" * 40,
                "conflicts": ["Program.cs", "Shop.Server.csproj"],
            },
        )
        d = decision("fix", pr=6, target=6, head="a" * 40)
        self.assertEqual(self.run_report(d=d)["outcome"], "ready")
        [row] = self.gh.run_comments(6)
        self.assertIn(
            "Merged `main` (`bbbbbbb`) into the branch and resolved conflicts in "
            "`Program.cs`, `Shop.Server.csproj`.",
            row["body"],
        )
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["fixes"][-1]["conflicts"], ["Program.cs", "Shop.Server.csproj"])

    def test_round_comment_says_a_moved_base_was_merged(self):
        self.candidate()
        merged = {"status": "done", "summary": "", "start_base": "b" * 40, "conflicts": []}
        self.write("nexkit-agent", "result.json", merged)
        d = decision("fix", pr=6, target=6, head="a" * 40, auto=True)
        self.run_report(d=d)
        [row] = self.gh.run_comments(6)
        self.assertIn(
            "Merged `main` (`bbbbbbb`) into the branch, which was behind it.", row["body"]
        )
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["fixes"][-1]["conflicts"], [])

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
            {"criterion": "floats add", "status": "unmet", "test": "", "evidence": "No float test."}
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

    def test_a_plan_at_the_usage_limit_pauses_with_its_resume_record(self):
        self.write("nexkit-agent", "result.json", LIMIT)
        d = decision("plan", note="Keep the old endpoint", comment_id=3)
        outcome = self.run_report(d=d, published=False)
        self.assertEqual(outcome["outcome"], "paused")
        [run] = self.gh.run_comments(5)
        self.assertEqual(run["run"]["status"], "paused")
        self.assertEqual(
            run["run"]["resume"],
            {
                "command": "plan",
                "number": 5,
                "note": "Keep the old endpoint",
                "auto": "resume",
                "at": "2026-10-11T04:40:00Z",
            },
        )
        self.assertIn("⏸️ [paused]", run["body"])
        self.assertIn("You've hit your session limit · resets 4:40am (UTC)", run["body"])
        self.assertIn(
            "NexKit runs `/nexkit plan` with the same note again after it resets, at "
            "04:40 UTC on 2026-10-11.",
            run["body"],
        )
        self.assertEqual(self.gh.reactions, [])  # keeps 👀 until it finishes
        # The reset is within usage_limit.wait_minutes: a run waits for it.
        [wait] = self.gh.dispatches
        self.assertEqual(wait["inputs"], WAIT_INPUTS)

    def test_a_fix_round_at_the_usage_limit_keeps_the_statuses_pending(self):
        self.write("nexkit-agent", "result.json", {**LIMIT, "reset_known": False})
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        outcome = self.run_report(d=d, published=False)
        self.assertEqual(outcome["outcome"], "paused")
        self.assertEqual(
            self.statuses(), {("nexkit/checks", "pending"), ("nexkit/review", "pending")}
        )
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["status"], "paused")
        self.assertEqual(row["run"]["resume"]["auto"], "true,resume")
        self.assertIn("could not read when it resets", row["body"])
        _, state = read_state(self.gh.comments(6))
        self.assertIsNone(state)  # a paused round uses no automatic fix round

    def test_a_review_at_the_usage_limit_resumes_only_the_review(self):
        self.candidate(review=LIMIT)
        outcome = self.run_report()
        self.assertEqual(outcome["outcome"], "paused")
        self.assertEqual(
            self.statuses(), {("nexkit/checks", "pending"), ("nexkit/review", "pending")}
        )
        self.assertFalse(self.gh.created_reviews)
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["verdict"], "paused")
        self.assertEqual(
            {k: row["run"]["resume"][k] for k in ("command", "number", "auto")},
            {"command": "review", "number": 6, "auto": "resume"},
        )
        # The issue's `/nexkit go` comment shows the pause, but only the round resumes.
        [command] = self.gh.run_comments(5)
        self.assertEqual(command["run"]["status"], "paused")
        self.assertNotIn("resume", command["run"])
        self.assertEqual([d["inputs"] for d in self.gh.dispatches], [WAIT_INPUTS])

    def test_a_reset_beyond_the_wait_is_left_to_the_schedule(self):
        for usage_limit, at in (({"wait_minutes": 0}, None), ({}, "2099-01-01T00:00:00Z")):
            self.gh.dispatches.clear()
            result = {**LIMIT, "resume_at": at or LIMIT["resume_at"]}
            self.write("nexkit-agent", "result.json", result)
            d = decision("plan", comment_id=3)
            cfg = make_config(usage_limit=usage_limit)
            outcome = self.run_report(d=d, cfg=cfg, published=False)
            self.assertEqual(outcome["outcome"], "paused")
            self.assertFalse(self.gh.dispatches, usage_limit)

    def test_a_resumed_run_with_an_unknown_reset_does_not_resume_again(self):
        self.write("nexkit-agent", "result.json", {**LIMIT, "reset_known": False})
        d = decision("fix", pr=6, target=6, head=HEAD, resumed=True)
        self.assertEqual(self.run_report(d=d, published=False)["outcome"], "paused")
        [row] = self.gh.run_comments(6)
        self.assertNotIn("resume", row["run"])
        self.assertIn("Comment `/nexkit fix` after the reset", row["body"])
        self.assertIn("usage limit", row["run"]["attention"])

    def test_without_resume_the_comment_says_when_to_continue(self):
        self.write("nexkit-agent", "result.json", LIMIT)
        cfg = make_config(usage_limit={"resume": False})
        self.run_report(d=decision("implement"), cfg=cfg, published=False)
        [run] = self.gh.run_comments(5)
        self.assertNotIn("resume", run["run"])
        self.assertIn(
            "It resets at 04:40 UTC on 2026-10-11. Comment `/nexkit go` after the reset",
            run["body"],
        )

    def test_workflow_file(self):
        self.assertEqual(workflow_file(WORKFLOW), "nexkit.yml")


if __name__ == "__main__":
    unittest.main()
