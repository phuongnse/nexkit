import contextlib
import io
import itertools
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from nexkit import cli, host, item_state, progress
from nexkit.github import GitHubError
from nexkit.maintain import WAIT_INPUTS
from nexkit.progress import trigger
from nexkit.report import (
    after_merge,
    apply_criteria,
    criterion_status,
    linked_line,
    place,
    report,
    retry_line,
    review_body,
    review_failure,
    shortened_line,
    workflow_file,
)
from nexkit.route import route
from nexkit.state import (
    TEXT_LIMIT,
    check_fingerprint,
    empty_state,
    read_state,
    render_run,
    render_state,
)
from nexkit.usage import parse_iso, shown
from tests.support import (
    FIX_OUTPUT,
    IMPLEMENT_OUTPUT,
    FakeGitHub,
    interpret_claude,
    make_config,
    prompt_texts,
    store_pending,
)

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
# A single run again at once after a transient error, as before `retry.wait_minutes`.
ONCE = make_config(retry={"wait_minutes": [0]})


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
        self.started_run = None  # the run that `reopened` started, which its report is
        env = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, artifact, name, data):
        path = self.artifacts / artifact / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def candidate(self, checks_pass=True, review=None, output=None):
        agent = {"status": "done", "tokens": TOKENS}
        if output:
            # What the publish job stores in the item's state before the checks run.
            agent["output"] = output
        self.write("nexkit-agent", "result.json", agent)
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

    def run_report(
        self,
        d=None,
        cfg=None,
        published=True,
        head=HEAD,
        needs_review=None,
        start=False,
        stored=False,
    ):
        """With `start`, the stage is started first, as the run of a stage that stops is: a stop
        is decided on the stored state, which holds the run that is on its way. With `stored`,
        the item's state is stored just after, as a run in its group does (`host.store_import`),
        so that the stored state holds the round on its way, and whether it is automatic."""
        needs = {"review": {"result": needs_review}} if needs_review else {}
        if published:
            needs["publish"] = {
                "outputs": {"result": json.dumps({"published": True, "pr": 6, "head": head})}
            }
        os.environ["GITHUB_RUN_ID"] = self.started_run or str(next(self.run_ids))
        self.started_run = None
        if start or stored:
            progress.start(self.gh, d or decision())
        if published or stored:
            # A run in the item's group stores the item's first state before it reports
            # (`cli._take`): a merge answers the `accept` that it holds. A stage that stops
            # starts first instead (`start`), so that its stored state holds its run.
            host.store_import(self.gh, cfg or make_config(), 5)
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

    def reopened(self, cfg, d=None):
        """A person reopens the merged pull request and its issue for another round, which starts
        its run: the item's state is gone from GitHub with the old round, and the next run in its
        group imports the item again from the markers (`host.store_import`), which holds the
        round on its way."""
        self.gh.reopen(6, 5)
        for comment in list(self.gh.issue_comments[5]):
            if item_state.PREFIX in comment["body"]:
                self.gh.issue_comments[5].remove(comment)
        # The new round's run has its own id, which its report keeps: a run that starts twice
        # under one id keeps its first comment.
        self.started_run = str(next(self.run_ids))
        os.environ["GITHUB_RUN_ID"] = self.started_run
        progress.start(self.gh, d or decision())
        host.store_import(self.gh, cfg, 5)


class EditedIssueTests(ReportCase):
    def setUp(self):
        super().setUp()
        self.gh.issues[5]["user"] = {"login": "bob"}
        self.gh.human_comment(5, "/nexkit trust", "alice")
        self.gh.edits[5] = [("bob", "2026-01-02T00:00:00Z")]
        self.auto = make_config(merge={"auto": True})

    def test_an_approved_pull_request_of_an_edited_issue_is_not_merged(self):
        self.candidate()
        outcome = self.run_report(cfg=self.auto)
        self.assertEqual(outcome["outcome"], "merge_untrusted")
        self.assertEqual(self.gh.merged, [])
        self.assertIn("comment `/nexkit trust`", self.gh.run_comments(6)[-1]["body"])
        self.assertIn("/nexkit trust", outcome["attention"])

    def test_a_round_that_stopped_on_the_edit_gives_its_automatic_round_back(self):
        state = {**empty_state(5), "auto_fixes": 1}
        self.gh.comment(6, render_state(state))
        self.write(
            "nexkit-agent", "result.json", {"status": "untrusted", "error": "@bob edited it"}
        )
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        outcome = self.run_report(d, published=False, stored=True)
        self.assertEqual(outcome["outcome"], "agent_untrusted")
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 0)
        self.assertIn("/nexkit trust", outcome["attention"])
        stored = item_state.read(self.gh, 5)[1]
        self.assertEqual(stored["state"], "failed")
        self.assertEqual(stored["attention"]["reason"], "untrusted")


class MergeGuardTests(ReportCase):
    """The checks NexKit makes before it merges go to the engine as a refusal only a person
    clears, and the outcome stays NexKit 6's (`report._guarded`)."""

    def setUp(self):
        super().setUp()
        self.cfg = make_config(merge={"auto": True})
        self.target = {"pr": 6, "head": HEAD, "issue": 5, "base": "main"}
        # The merge answers the `accept` that the stored state holds, before the tests close or
        # edit the issue.
        store_pending(self.gh, self.cfg, 5, 6, HEAD)

    def merge(self):
        from nexkit import host
        from nexkit.report import merge_pull

        with mock.patch.object(host, "step", wraps=host.step) as step:
            outcome, text = merge_pull(self.gh, self.cfg, self.target, {}, "", WORKFLOW, "main")
        events = [call.args[1] for call in step.call_args_list]
        return outcome, text, events

    def edited(self):
        self.gh.issues[5]["user"] = {"login": "bob"}
        self.gh.human_comment(5, "/nexkit trust", "alice")
        self.gh.edits[5] = [("bob", "2026-01-02T00:00:00Z")]

    def unreadable(self):
        return mock.patch.object(self.gh, "issue", side_effect=GitHubError(502, "down"))

    def assertNeedsPerson(self, events, refusal="needsPerson"):
        [event] = events
        self.assertEqual(event["kind"], "actionFinished")
        result = event["result"]
        self.assertEqual((result["accepted"], result["refusal"]), (False, refusal))
        self.assertEqual(self.gh.merged, [])

    def test_a_closed_issue_sends_a_refusal_only_a_person_clears(self):
        self.gh.issues[5].update(state="closed", state_reason="not_planned")
        outcome, _, events = self.merge()
        self.assertEqual(outcome, "merge_issue_closed")
        self.assertNeedsPerson(events)

    def test_an_issue_edited_after_a_vouch_sends_an_untrusted_refusal(self):
        self.edited()
        outcome, _, events = self.merge()
        self.assertEqual(outcome, "merge_untrusted")
        self.assertNeedsPerson(events, "untrusted")
        self.assertIn("/nexkit trust", events[0]["result"]["text"])

    def test_an_unreadable_issue_sends_a_refusal_only_a_person_clears(self):
        with self.unreadable():
            outcome, _, events = self.merge()
        self.assertEqual(outcome, "merge_refused")
        self.assertNeedsPerson(events)

    def test_a_closed_issue_keeps_its_words(self):
        self.gh.issues[5].update(state="closed", state_reason="not_planned")
        _, text, _ = self.merge()
        self.assertIn("NexKit did not merge it: #5 is closed.", text)

    def test_an_unreadable_issue_keeps_what_a_person_does(self):
        with self.unreadable():
            _, text, _ = self.merge()
        self.assertIn("it could not read #5", text)
        self.assertEqual(
            self.target["found"]["refused"][0],
            "comment `/nexkit review` on the pull request to try the merge again",
        )

    def test_a_guard_the_engine_refuses_to_decide_is_a_refused_merge(self):
        from nexkit import host

        self.gh.issues[5].update(state="closed", state_reason="not_planned")
        from nexkit.report import merge_pull

        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            outcome, text = merge_pull(self.gh, self.cfg, self.target, {}, "", WORKFLOW, "main")
        self.assertEqual(outcome, "merge_refused")
        self.assertIn("refused to decide its next step", text)
        self.assertIn("refused", self.target["found"])

    def test_a_merge_that_passes_the_guards_tells_the_engine_only_that_it_went_through(self):
        outcome, _, events = self.merge()
        self.assertEqual(outcome, "merged")
        # The `accept` that the state holds is answered, and then the `close` that it asks for,
        # once GitHub shows the issue closed.
        [event, closed] = events
        self.assertEqual((event["kind"], event["action"]), ("actionFinished", "a2.1"))
        self.assertEqual(event["result"], {"accepted": True, "refusal": None, "text": ""})
        self.assertEqual(closed["id"], "closed-5-a3.1")


class PersonsPullRequestTests(ReportCase):
    def setUp(self):
        super().setUp()
        self.auto = make_config(merge={"auto": True}, conflicts={"auto_resolve": False})

    def person(self, **kw):
        fields = {"pr": 6, "issue": 6, "target": 6, "head": HEAD, "person": True}
        return decision("review", branch="upgrade/nexkit", **{**fields, **kw})

    def report_person(self, cfg=None, **kw):
        return self.run_report(self.person(**kw), cfg or self.auto, published=False, start=True)

    def test_an_approved_pull_request_gets_both_statuses_and_stays_as_it_is(self):
        self.candidate()
        outcome = self.report_person()
        self.assertEqual(outcome["outcome"], "person_reviewed")
        # The run's own pending statuses came first.
        last = {(s["context"], s["state"]) for s in self.gh.statuses[-2:]}
        self.assertEqual(last, {("nexkit/checks", "success"), ("nexkit/review", "success")})
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.gh.updated, [])
        self.assertEqual(self.gh.dispatches, [])

    def test_failing_checks_start_no_fix_round_and_mention_nobody(self):
        self.candidate(checks_pass=False)
        outcome = self.report_person()
        self.assertEqual(outcome["outcome"], "person_reviewed")
        self.assertFalse(outcome.get("attention"))
        self.assertIn(("nexkit/checks", "failure"), self.statuses())
        self.assertNotIn("fix", [d["inputs"]["command"] for d in self.gh.dispatches])
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["status"], "success")
        self.assertIn("a person makes any change", row["body"])

    def test_a_configuration_nexkit_would_refuse_fails_the_checks_and_says_why(self):
        self.candidate()  # its checks passed and its review approved
        error = 'evidence.artifacts: `*` may only end a name, such as "test-results-e2e-*"'
        outcome = self.report_person(config_error=error)
        self.assertEqual(outcome["outcome"], "person_reviewed")
        checks = [s for s in self.gh.statuses if s["context"] == "nexkit/checks"][-1]
        self.assertEqual(checks["state"], "failure")
        self.assertEqual(checks["description"], f"NexKit would refuse .nexkit/config.json: {error}")
        self.assertIn(("nexkit/review", "success"), self.statuses())
        [row] = self.gh.run_comments(6)
        self.assertIn(f"NexKit would refuse the `.nexkit/config.json` of `{HEAD[:7]}`", row["body"])
        self.assertIn(error, row["body"])
        self.assertIn("Checks or review need attention", row["body"])
        self.assertEqual(row["run"]["checks"], "failed")
        [review] = self.gh.created_reviews
        self.assertIn("❌ `.nexkit/config.json`", review["body"])
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.gh.dispatches, [])

    def test_a_long_configuration_error_is_cut_to_fit_the_description(self):
        self.candidate(checks_pass=False)
        error = "Configuration keys were renamed: " + "; ".join(["'a' is now 'b'"] * 20)
        self.report_person(config_error=error)
        checks = [s for s in self.gh.statuses if s["context"] == "nexkit/checks"][-1]
        self.assertEqual(len(checks["description"]), 140)
        self.assertTrue(checks["description"].endswith("…"))
        self.assertIn(error, self.gh.run_comments(6)[-1]["body"])

    def test_a_configuration_error_holds_while_the_review_waits_for_the_usage_limit(self):
        self.candidate(review={"status": "paused", "error": "limit", "resets_at": None})
        self.report_person(config_error="Configuration must be a JSON object")
        self.assertEqual(self.gh.statuses[-1]["context"], "nexkit/checks")
        self.assertEqual(self.gh.statuses[-1]["state"], "failure")

    def test_a_review_that_requests_changes_starts_no_fix_round(self):
        finding = {"severity": "blocking", "file": "a.yml", "line": 1, "body": "It breaks."}
        self.candidate(review=review_result("request_changes", [finding]))
        self.assertEqual(self.report_person()["outcome"], "person_reviewed")
        self.assertEqual(self.gh.dispatches, [])
        self.assertIn(("nexkit/review", "failure"), self.statuses())

    def test_the_review_comment_says_who_makes_any_change(self):
        self.candidate()
        self.report_person()
        [review] = self.gh.created_reviews
        self.assertIn("a person makes any change", review["body"])

    def test_a_review_of_a_nexkit_pull_request_does_not_say_so(self):
        self.candidate()
        self.run_report(decision("review", head=HEAD), published=False)
        [review] = self.gh.created_reviews
        self.assertNotIn("a person makes any change", review["body"])

    def test_a_review_that_stopped_is_shown_and_tried_once_more_like_any_other(self):
        self.candidate(review=False)
        needs = {"review": {"result": "failure"}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "9"}):
            progress.start(self.gh, self.person())
            found = report(
                self.gh,
                self.person(),
                self.auto,
                needs,
                self.artifacts,
                workflow_ref=WORKFLOW,
                default_branch="main",
            )
        # It runs again after the first wait, which the waiting run sees to.
        self.assertEqual(found["outcome"], "retry_waiting")
        [start] = self.gh.dispatches
        self.assertEqual(start["inputs"], WAIT_INPUTS)
        [row] = [c["run"] for c in self.gh.run_comments(6)]
        self.assertEqual(row["status"], "paused")
        self.assertEqual(row["resume"]["command"], "review")
        self.assertIn(("nexkit/review", "pending"), self.statuses())
        self.assertEqual(self.gh.merged, [])

    def test_a_lost_shards_job_runs_the_round_once_more(self):
        from nexkit.report import _transient

        needs = {"review": {"result": "skipped"}, "shards": {"result": "failure"}}
        self.assertEqual(
            _transient({}, None, {}, needs, "review"), "the shards job ended without a result"
        )

    def test_the_round_shows_whose_pull_request_it_is(self):
        self.assertEqual(trigger(self.person()), "review (person)")

    def test_the_state_comment_keeps_the_review_and_offers_no_fix_round(self):
        self.candidate()
        self.report_person()
        [state] = self.gh.comments_matching(6, "nexkit:state")
        self.assertIn("A person's pull request", state["body"])
        self.assertNotIn("/nexkit fix", state["body"])
        self.assertEqual(read_state(self.gh.comments(6))[1]["last_review"]["head"], HEAD)


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
        # The implement round is on its way, which the stored state holds, and runs only while no
        # NexKit pull request is open.
        self.gh.pulls[6]["state"] = self.gh.issues[6]["state"] = "closed"
        progress.start(self.gh, decision())
        return report(
            self.gh,
            decision(),
            cfg or make_config(),
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )

    def test_a_round_whose_issue_closed_meanwhile_says_nothing_was_published(self):
        self.gh.close_issue(5, "not_planned")
        self.write("nexkit-agent", "result.json", {"status": "done", "output": {}})
        needs = {
            "publish": {
                "outputs": {"result": json.dumps({"published": False, "issue_closed": True})}
            }
        }
        cfg = make_config(notify=["olivia"])
        outcome = report(
            self.gh,
            decision(),
            cfg,
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        self.assertEqual((outcome["outcome"], outcome.get("attention")), ("issue_closed", None))
        [row] = self.gh.run_comments(5)
        self.assertEqual(row["run"]["status"], "success")
        self.assertIn(
            "#5 closed while the implement round ran, so NexKit published nothing.", row["body"]
        )
        self.assertEqual(self.gh.issues[5]["state_reason"], "not_planned")
        self.assertFalse(self.gh.comments_matching(5, "@olivia"))

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
        self.assertEqual(
            outcome["attention"],
            "close the issue yourself. No change is needed, but GitHub refused NexKit's close: "
            "`GitHub API 403: Forbidden`",
        )
        self.assertEqual(self.gh.run_comments(5)[-1]["run"]["status"], "failure")
        self.assertIn("could not close #5", outcome["summary"])

    def test_a_round_that_finds_no_change_closes_nothing_when_the_engine_refuses(self):
        with mock.patch.object(host, "decide", side_effect=host.Refused("no")):
            outcome = self.no_change(make_config(close_parent_issues=True))
        self.assertEqual(outcome["outcome"], "close_failed")
        self.assertIn("NexKit's engine refused to decide its next step: `no`", outcome["attention"])
        self.assertIn("did not close #5", outcome["summary"])
        self.assertIn("Fixed by 8b9cb87.", outcome["summary"])
        self.assertEqual(self.gh.issues[5]["state"], "open")
        self.assertFalse(self.gh.comments_matching(5, "NexKit closed #5"))
        self.assertEqual(self.gh.run_comments(5)[-1]["run"]["status"], "failure")

    def test_a_round_that_finds_no_change_sends_the_agent_s_finding_to_the_engine(self):
        with mock.patch.object(host, "no_change_found", return_value="noChange") as found:
            self.no_change()
        self.assertEqual(found.call_args.args[3], "Fixed by 8b9cb87.")
        self.assertTrue(found.call_args.kwargs["write"])
        self.assertEqual(self.gh.issues[5]["state_reason"], "completed")

    def test_a_fix_round_whose_base_holds_its_change_closes_nothing_when_the_engine_refuses(self):
        state = {"version": 2, "issue": 5, "auto_fixes": 1, "feedback": None}
        self.gh.comment(6, render_state(state))
        found = {
            "stage": "fix",
            "status": "already_on_base",
            "summary": "",
            "error": "`main` already holds the change of this pull request.",
            "merge_base": "c" * 40,
            "on_base": "b" * 40,
        }
        self.write("nexkit-agent", "result.json", found)
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        with mock.patch.object(host, "on_base_found", side_effect=host.Refused("no")):
            outcome = self.run_report(d=d, published=False, stored=True)
        self.assertEqual(outcome["outcome"], "on_base_failed")
        self.assertIn("NexKit's engine refused to decide", outcome["attention"])
        self.assertEqual(self.gh.pulls[6]["state"], "open")
        self.assertEqual(self.gh.issues[5]["state"], "open")
        self.assertFalse(self.gh.deleted)
        self.assertFalse(self.gh.merged)
        [row] = self.gh.run_comments(6)
        self.assertEqual(
            (row["run"]["status"], row["run"]["outcome"], row["run"]["on_base"]),
            ("failure", "on_base_failed", "b" * 40),
        )
        self.assertIn(f"brought by {'b' * 40}, but NexKit's engine refused to decide", row["body"])
        # Its count comes back, as when GitHub refuses the close.
        self.assertEqual(read_state(self.gh.comments(6))[1]["auto_fixes"], 0)

    def test_a_changed_round_with_a_no_change_note_publishes_and_keeps_the_issue_open(self):
        self.candidate()
        note = "Nothing skipped. Every planned change was made."
        output = {"status": "done", "summary": "Fixed add.", "no_change": ""}
        self.write(
            "nexkit-agent",
            "result.json",
            {"status": "done", "tokens": TOKENS, "output": output, "no_change_note": note},
        )
        outcome = self.run_report()
        self.assertEqual(outcome["outcome"], "ready")
        body = self.gh.run_comments(6)[-1]["body"]
        self.assertIn(f"**Note from the implement agent:** {note}", body)
        self.assertIn("so NexKit published the change and keeps the issue open", body)
        self.assertEqual(self.gh.issues[5]["state"], "open")
        self.assertFalse(self.gh.comments_matching(5, "No change is needed"))

    def test_agent_failures_are_explained_on_the_issue(self):
        # The last run again after a job that failed as a whole: a person decides.
        outcome = self.run_report(d=decision(retry=3), published=False, start=True)
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertEqual(
            outcome["attention"],
            "post the command again once the cause has passed. The run stopped on a transient "
            "error 4 times, the last after every wait of `retry.wait_minutes`: `the agent job "
            "ended without a result`",
        )
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

    def test_a_transient_error_runs_the_command_once_more_at_once_with_a_single_wait_of_0(self):
        cfg = make_config(notify=["olivia"], retry={"wait_minutes": [0]})
        error = "Claude stopped (success). API Error: 529 overloaded"
        self.write(
            "nexkit-agent", "result.json", {"status": "error", "error": error, "transient": True}
        )
        d = decision(note="Use the cache.")
        outcome = self.run_report(d=d, cfg=cfg, published=False, start=True)
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
        retry = decision(note="Use the cache.", retry=1)
        self.write(
            "nexkit-agent",
            "result.json",
            {"status": "error", "error": "Connection error", "transient": True},
        )
        outcome = self.run_report(d=retry, cfg=cfg, published=False, start=True)
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertIn(
            "Transient errors so far:\n\n1. Claude stopped (success). API Error: 529 overloaded"
            "\n2. Connection error",
            outcome["summary"],
        )
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertTrue(self.gh.comments_matching(5, "@olivia a person is needed"))

    def test_a_transient_error_runs_again_after_growing_waits_then_waits_for_a_person(self):
        cfg = make_config(notify=["olivia"])
        errors = ["API Error: 529 overloaded", "Connection error", "API Error: 500", "ETIMEDOUT"]
        for tries, (error, wait) in enumerate(zip(errors, (15, 30, 60, None), strict=True)):
            self.write(
                "nexkit-agent",
                "result.json",
                {"status": "error", "error": error, "transient": True},
            )
            before = datetime.now(UTC)
            d = decision(note="Use the cache.", **({"retry": tries} if tries else {}))
            outcome = self.run_report(d=d, cfg=cfg, published=False, start=True)
            run = self.gh.run_comments(5)[-1]
            if wait is None:
                break
            # Paused until the wait has passed; only the run that waits for it starts now.
            self.assertEqual(outcome["outcome"], "retry_waiting", tries)
            self.assertIsNone(outcome.get("attention"))
            self.assertEqual(self.gh.dispatches[-1]["inputs"], WAIT_INPUTS)
            self.assertEqual(run["run"]["status"], "paused")
            resume = run["run"]["resume"]
            auto = "retry" if not tries else f"retry:{tries + 1}"
            self.assertEqual(
                {k: resume[k] for k in ("command", "number", "note", "auto", "retry")},
                {
                    "command": "go",
                    "number": "5",
                    "note": "Use the cache.",
                    "auto": auto,
                    "retry": tries + 1,
                },
            )
            at = parse_iso(resume["at"])
            self.assertLessEqual(abs(at - before - timedelta(minutes=wait)), timedelta(minutes=1))
            self.assertIn(
                f"again after {shown(resume['at'])}: run again {tries + 1} of 3", run["body"]
            )
            self.assertIn("A new command here replaces this.", run["body"])
            listed = "Transient errors so far:" in run["body"]
            self.assertEqual(listed, tries > 0)
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertEqual(run["run"]["status"], "failure")
        self.assertNotIn("resume", run["run"])
        numbered = "\n".join(f"{i}. {e}" for i, e in enumerate(errors, 1))
        self.assertIn(f"Transient errors so far:\n\n{numbered}", run["body"])
        self.assertIn("stopped on a transient error 4 times", outcome["attention"])
        self.assertTrue(self.gh.comments_matching(5, "@olivia a person is needed"))
        self.assertEqual({d["inputs"]["command"] for d in self.gh.dispatches}, {"resume"})

    def test_a_run_again_that_passes_goes_on_as_usual_and_shows_its_number(self):
        self.candidate()
        outcome = self.run_report(d=decision(retry=2))
        self.assertEqual(outcome["outcome"], "ready")
        [row] = [c for c in self.gh.run_comments(6)]
        self.assertTrue(row["body"].startswith("### NexKit round 1: implement (retry 2)"))
        self.assertEqual(trigger(decision("fix", auto=True, retry=2)), "fix (auto, retry 2)")
        progress_comment = render_run(
            {"url": "u", "command": "go", "status": "running", "retry": 2}
        )
        self.assertIn("### NexKit `/nexkit go` (retry 2)", progress_comment)

    def test_a_stop_on_cloudflares_challenge_page_runs_again_but_a_refused_key_does_not(self):
        def stop(text, request_id=None):
            said = {
                "type": "assistant",
                "api_error_status": 403,
                "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]},
            }
            if request_id:
                said["request_id"] = request_id
            result = {"subtype": "success", "is_error": True, "result": text}
            return interpret_claude("implement", result, returncode=1, stream=[said])

        cloudflare = "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare"
        self.write("nexkit-agent", "result.json", stop(cloudflare))
        outcome = self.run_report(published=False, start=True)
        self.assertEqual(outcome["outcome"], "retry_waiting")
        self.assertIsNone(outcome.get("attention"))
        run = self.gh.run_comments(5)[-1]
        self.assertEqual(run["run"]["status"], "paused")
        self.assertIn(
            f"again after {shown(run['run']['resume']['at'])}: run again 1 of 3", run["body"]
        )
        self.assertNotIn("remove its cause", run["body"])
        refused = "Failed to authenticate. API Error: 403 Your API key does not have permission"
        self.write("nexkit-agent", "result.json", stop(refused, "req_1"))
        outcome = self.run_report(published=False, start=True)
        self.assertEqual(outcome["outcome"], "agent_error")
        self.assertIn("remove its cause", outcome["attention"])
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_without_waits_a_transient_error_waits_for_a_person_at_once(self):
        self.write(
            "nexkit-agent",
            "result.json",
            {"status": "error", "error": "API 529", "transient": True},
        )
        cfg = make_config(retry={"wait_minutes": []})
        outcome = self.run_report(cfg=cfg, published=False, start=True)
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertIn("with `retry.wait_minutes` empty NexKit does not run", outcome["attention"])
        self.assertNotIn("Transient errors so far", outcome["summary"])
        self.assertFalse(self.gh.dispatches)

    def test_a_round_without_a_result_that_fits_runs_once_more(self):
        error = (
            "Claude stopped (error_max_structured_output_retries). NexKit asked the same "
            "session for its result alone, and it gave none that fits either."
        )
        result = {"status": "error", "error": error, "transient": True, "unfit": "no testing"}
        result["asked_again"] = True
        self.write("nexkit-agent", "result.json", result)
        cfg = make_config(retry={"wait_minutes": [0]})
        first = decision(note="Keep it small.")
        outcome = self.run_report(d=first, cfg=cfg, published=False, start=True)
        self.assertEqual(outcome["outcome"], "retry")
        self.assertIn("even when asked for it alone", outcome["summary"])
        self.assertNotIn("from the service, the network", outcome["summary"])
        [dispatch] = self.gh.dispatches
        self.assertEqual(
            dispatch["inputs"],
            {"command": "go", "number": "5", "note": "Keep it small.", "auto": "retry"},
        )
        # A session that NexKit could not ask: only that there was none.
        self.assertIn("gave no result that fits. Another run", retry_line({"unfit": "x"}))
        # A second such stop waits for a person.
        retry = decision(note="Keep it small.", retry=1)
        outcome = self.run_report(d=retry, cfg=cfg, published=False, start=True)
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_a_publish_that_failed_on_github_every_try_runs_once_more(self):
        self.write("nexkit-agent", "result.json", {"status": "done", "output": {}})
        error = "git push --quiet origin failed: The requested URL returned error: 500"
        result = {"published": False, "error": error, "transient": True}
        needs = {"publish": {"outputs": {"result": json.dumps(result)}}}
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        progress.start(self.gh, decision())
        outcome = report(
            self.gh,
            decision(),
            ONCE,
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        self.assertEqual(outcome["outcome"], "retry")
        self.assertIn("NexKit could not publish the implement result", outcome["summary"])
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["command"], "go")
        # A refusal that another try cannot change waits for a person.
        result = {"published": False, "error": "protected branch hook declined"}
        needs = {"publish": {"outputs": {"result": json.dumps(result)}}}
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        progress.start(self.gh, decision())
        outcome = report(
            self.gh,
            decision(),
            ONCE,
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        self.assertEqual(outcome["outcome"], "publish_failed")
        self.assertEqual(len(self.gh.dispatches), 1)
        # A publish job that ended without its outcome, such as out of time, runs once more.
        needs = {"publish": {"result": "failure", "outputs": {}}}
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        progress.start(self.gh, decision())
        outcome = report(
            self.gh,
            decision(),
            ONCE,
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        self.assertEqual(outcome["outcome"], "retry")
        self.assertEqual(len(self.gh.dispatches), 2)

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
            progress.start(self.gh, fix)
            outcome = report(
                self.gh,
                fix,
                ONCE,
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
        self.assertEqual(trigger(decision("fix", auto=True, retry=True)), "fix (auto, retry 1)")

    def test_a_review_that_never_ran_is_retried_only_after_a_failed_verify_job(self):
        self.candidate(review=False, output=FIX_OUTPUT)
        fix = decision("fix", pr=6, target=6, head=HEAD)
        # With parallel checks, the combine job that joins them counts as theirs.
        cases = (
            ("success", "skipped", "review_failed"),
            ("success", "failure", "retry"),
            ("failure", "skipped", "retry"),
        )
        for verify, combine, expected in cases:
            self.reopened(ONCE, fix)
            os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
            published = {"published": True, "pr": 6, "head": HEAD}
            needs = {
                "publish": {"outputs": {"result": json.dumps(published)}},
                "verify": {"result": verify},
                "combine": {"result": combine},
                "review": {"result": "skipped"},
            }
            outcome = report(
                self.gh,
                fix,
                ONCE,
                needs,
                self.artifacts,
                workflow_ref=WORKFLOW,
                default_branch="main",
            )
            self.assertEqual(outcome["outcome"], expected)

    def test_only_the_review_the_decision_names_carries_over(self):
        self.candidate(review={"stage": "review", "status": "carried", "carried": "a" * 40})
        review = decision("review", pr=6, target=6, head=HEAD)
        # Without `carry_from`, a carried result is no review at all.
        outcome = self.run_report(d=review, published=False)
        self.assertEqual(outcome["outcome"], "review_failed")
        self.assertNotIn("carried over", outcome["summary"])
        # With it, the round is ready on the earlier approval, without a new PR review.
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        self.write("nexkit-review", "carry.json", {"from": "a" * 40, "carried": True, "why": ""})
        carry = {**review, "carry_from": "a" * 40}
        self.reopened(make_config(), carry)  # the new review starts its run
        outcome = self.run_report(d=carry, published=False)
        self.assertEqual(outcome["outcome"], "ready")
        self.assertIn("The AI review of `aaaaaaa` carried over", outcome["summary"])
        self.assertFalse(self.gh.created_reviews)
        self.assertIn(("nexkit/review", "success"), self.statuses())

    def test_a_review_that_failed_on_a_transient_error_runs_once_more(self):
        self.candidate(review={"status": "error", "error": "API Error: 503", "transient": True})
        fix = decision("fix", pr=6, target=6, head=HEAD, autopilot=True)
        outcome = self.run_report(d=fix, cfg=ONCE)
        self.assertEqual(outcome["outcome"], "retry")
        [dispatch] = self.gh.dispatches
        self.assertEqual(
            dispatch["inputs"],
            {"command": "review", "number": "6", "note": "", "auto": "autopilot,retry"},
        )

    def test_a_review_stopped_by_cloudflare_runs_again_without_asking_for_new_secrets(self):
        cloudflare = "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare"
        for transient, secrets in ((True, False), (False, True)):
            reviewed = {"status": "error", "error": cloudflare}
            if transient:
                reviewed["transient"] = True
            _, message = review_failure(reviewed)
            self.assertEqual("secrets are fixed" in message, secrets, message)
        self.candidate(review={"status": "error", "error": cloudflare, "transient": True})
        outcome = self.run_report(d=decision("fix", pr=6, target=6, head=HEAD))
        self.assertEqual(outcome["outcome"], "retry_waiting")
        self.assertNotIn("secrets", self.gh.run_comments(6)[-1]["body"])

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
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(self.gh.issues[1]["state"], "open")  # the setting is off
        self.reopened(make_config(merge={"auto": True}, close_parent_issues=True))
        self.run_report(cfg=make_config(merge={"auto": True}, close_parent_issues=True))
        self.assertEqual(self.gh.issues[1]["state"], "closed")
        row = self.gh.run_comments(6)[-1]
        self.assertIn("Closed #5 as completed.\nClosed #1 as completed", row["body"])

    def test_a_pull_request_a_person_merged_or_closed_first_mentions_nobody(self):
        self.candidate()
        cfg = make_config(merge={"auto": True}, notify=["owner"])
        for merged, line in (
            (True, "It was already merged by `olivia`, so NexKit had nothing to merge."),
            (False, "It was already closed, so NexKit did not merge it."),
        ):
            with self.subTest(merged=merged):
                self.gh = FakeGitHub()
                self.gh.add_issue(5)
                self.gh.add_pull(6, 5, head_sha=HEAD)
                # The item's state was stored while its issue was open.
                host.store_import(self.gh, cfg, 5)
                # GitHub closed the issue through the pull request's `Closes` line.
                self.gh.issues[5].update(state="closed", state_reason="completed")
                self.gh.pulls[6].update(state="closed", merged=merged)
                if merged:
                    self.gh.pulls[6]["merged_by"] = {"login": "olivia"}
                outcome = self.run_report(cfg=cfg)
                self.assertEqual(outcome["outcome"], "pull_closed")
                self.assertIsNone(outcome["attention"])
                self.assertEqual((self.gh.merged, self.gh.dispatches), ([], []))
                self.assertFalse(self.gh.comments_matching(6, "@owner"))
                [row] = self.gh.run_comments(6)
                self.assertEqual(row["run"]["status"], "success")
                self.assertIn(line, row["body"])
                self.assertIn(f"✅ Checks and AI review passed on `{HEAD[:7]}`.", row["body"])
                self.assertNotIn("#5 is closed", row["body"])
                self.assertNotIn("ready for a human decision", row["body"])

    def test_a_pull_request_whose_issue_closed_is_not_merged(self):
        self.candidate()
        merge = {"auto": True, "after_workflows": ["ci.yml"]}
        cfg = make_config(merge=merge, notify=["olivia"])
        # The item's state was stored while its issue was open.
        host.store_import(self.gh, cfg, 5)
        self.gh.issues[5].update(state="closed", state_reason="not_planned")
        outcome = self.run_report(cfg=cfg)
        self.assertEqual(outcome["outcome"], "merge_issue_closed")
        self.assertEqual(
            outcome["attention"],
            "merge the pull request or close it. Its issue closed before NexKit merged it, and "
            "NexKit leaves the pull request of a closed issue to a person",
        )
        self.assertEqual((self.gh.merged, self.gh.dispatches), ([], []))
        self.assertEqual(self.gh.issues[5]["state_reason"], "not_planned")
        self.assertEqual(self.gh.pulls[6]["state"], "open")
        [mention] = self.gh.comments_matching(6, "@olivia")
        self.assertIn("merge the pull request or close it", mention["body"])
        self.assertFalse(self.gh.comments_matching(5, "NexKit merged"))
        [row] = self.gh.run_comments(6)
        self.assertIn(
            "NexKit did not merge it: #5 is closed. Merge the pull request or close it yourself.",
            row["body"],
        )
        self.assertTrue(self.gh.comments_matching(6, "@olivia"))

    def test_failing_to_close_the_issue_keeps_the_merge(self):
        self.candidate()
        self.gh.close_errors[5] = GitHubError(403, "Resource not accessible by integration")
        outcome = self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(outcome["outcome"], "merged")
        self.assertEqual(self.gh.merged, [(6, HEAD, "squash")])
        [row] = self.gh.run_comments(6)
        self.assertIn("Could not close #5: GitHub API 403", row["body"])
        self.assertEqual(row["run"]["status"], "success")

    def test_an_issue_github_closes_at_the_same_moment_counts_as_closed(self):
        self.gh.add_issue(1, title="Epic")
        self.gh.add_issue(2, state="closed", reason="completed")
        self.gh.parents.update({2: 1, 5: 1})
        self.candidate()
        self.gh.closed_by_other.add(5)
        cfg = make_config(merge={"auto": True}, close_parent_issues=True, notify=["owner"])
        outcome = self.run_report(cfg=cfg)
        self.assertEqual((outcome["outcome"], outcome["attention"]), ("merged", None))
        self.assertEqual(self.gh.issues[5]["state"], "closed")
        # The parent closes in this round, as after a close by NexKit.
        self.assertEqual(self.gh.issues[1]["state"], "closed")
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["status"], "success")
        self.assertIn(
            "#5 is closed: it closed at the same moment as NexKit's close.\nClosed #1 as completed",
            row["body"],
        )
        self.assertNotIn("Could not close", row["body"])
        self.assertNotIn("Close it yourself", row["body"])
        self.assertFalse(self.gh.comments_matching(6, "@owner"))

    def test_a_failed_close_of_an_issue_that_stays_open_still_asks_a_person(self):
        self.candidate()
        # A 422 alike, but the issue is still open on a second read.
        self.gh.close_errors[5] = GitHubError(422, "Validation Failed")
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(self.gh.issues[5]["state"], "open")
        [row] = self.gh.run_comments(6)
        self.assertIn(
            "Could not close #5: GitHub API 422: Validation Failed. Close it yourself.", row["body"]
        )

    def test_auto_merge_deletes_its_branch_when_the_repository_deletes_merged_branches(self):
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(self.gh.deleted, [])  # the setting is off
        self.gh.delete_on_merge = True
        self.reopened(make_config(merge={"auto": True}))
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(self.gh.deleted, ["nexkit/issue-5"])
        row = self.gh.run_comments(6)[-1]
        self.assertIn("Closed #5 as completed.\nDeleted branch `nexkit/issue-5`.", row["body"])

    def test_auto_merge_deletes_only_its_own_branch(self):
        self.gh.delete_on_merge = True
        own = dict(self.gh.pulls[6]["head"])
        for head in ({"ref": "feature/add"}, {"repo": {"full_name": "fork/app"}}):
            self.candidate(output=IMPLEMENT_OUTPUT)
            self.gh.pulls[6]["head"] = {**own, **head}
            self.reopened(make_config(merge={"auto": True}))
            self.assertEqual(
                self.run_report(cfg=make_config(merge={"auto": True}))["outcome"], "merged"
            )
            self.assertNotIn("branch", self.gh.run_comments(6)[-1]["body"].split("merged")[-1])
        self.assertEqual(self.gh.deleted, [])
        # A pull request that targets the branch would close with it.
        self.gh.pulls[6]["head"] = own
        self.candidate(output=IMPLEMENT_OUTPUT)
        self.gh.add_pull(7, 8)
        self.gh.pulls[7]["base"] = {"ref": "nexkit/issue-5"}
        self.reopened(make_config(merge={"auto": True}))
        self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(self.gh.deleted, [])
        row = self.gh.run_comments(6)[-1]
        self.assertIn("Kept branch `nexkit/issue-5`, the base branch of #7.", row["body"])

    def test_failing_to_delete_the_branch_keeps_the_merge_and_the_close(self):
        self.gh.delete_on_merge = True
        self.gh.delete_error = GitHubError(403, "Resource not accessible by integration")
        self.candidate()
        outcome = self.run_report(cfg=make_config(merge={"auto": True}))
        self.assertEqual(outcome["outcome"], "merged")
        self.assertEqual(self.gh.merged, [(6, HEAD, "squash")])
        self.assertEqual(self.gh.issues[5]["state_reason"], "completed")
        [row] = self.gh.run_comments(6)
        self.assertIn(
            "Could not delete branch `nexkit/issue-5`: GitHub API 403: Resource not accessible "
            "by integration. It stays; delete it yourself.",
            row["body"],
        )
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

    def test_a_merge_refused_for_pending_required_checks_is_the_engine_s_to_wait_for(self):
        from nexkit import host
        from nexkit.report import merge_pull

        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.gh.required = {"e2e"}
        cfg = make_config(merge={"auto": True}, autopilot={"enabled": True})
        target = {"pr": 6, "head": HEAD, "issue": 5, "base": "main"}
        pending = GitHubError(405, 'Required status check "e2e" is expected.')
        store_pending(self.gh, cfg, 5, 6, HEAD)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.gh.merge_error = [pending]
            row = {}
            outcome, _ = merge_pull(self.gh, cfg, target, row, "", WORKFLOW, "main")
            self.assertEqual(outcome, "merge_waiting")
            [(state, event, _)] = [call.args for call in step.call_args_list]
            self.assertEqual(event["kind"], "actionFinished")
            self.assertEqual(
                (event["result"]["accepted"], event["result"]["refusal"]),
                (False, "waitingForChecks"),
            )
            self.assertEqual([action["kind"] for action in state["pending"]], ["accept"])
            # `waiting_since` is the engine's `checksSince`, which is the time of its event.
            self.assertEqual(row["waiting_since"], event["at"])
            self.assertLess(
                datetime.now(UTC) - parse_iso(row["waiting_since"]), timedelta(minutes=1)
            )
            # A second refusal of the same head, of the accept that the sweep's tick started,
            # keeps the first time the checks had not passed.
            step.reset_mock()
            self.gh.merge_error = [pending]
            first = "2026-11-23T10:00:00Z"
            store_pending(
                self.gh,
                cfg,
                5,
                6,
                HEAD,
                action_id="a3.1",
                checksSince={"version": HEAD, "at": first},
            )
            row = {}
            outcome, _ = merge_pull(self.gh, cfg, target, row, "", WORKFLOW, "main")
            self.assertEqual(outcome, "merge_waiting")
            [(state, *_)] = [call.args for call in step.call_args_list]
            self.assertEqual(state["checksSince"], {"version": HEAD, "at": first})
            self.assertEqual(row["waiting_since"], first)
            self.assertEqual(row["waiting_since"], first)

    def test_a_refusal_the_engine_cannot_wait_for_goes_to_a_person(self):
        from nexkit import host
        from nexkit.report import merge_pull

        self.gh.add_milestone(1)
        self.gh.issues[5]["milestone"] = {"number": 1}
        self.gh.required = {"e2e"}
        cfg = make_config(merge={"auto": True}, autopilot={"enabled": True})
        target = {"pr": 6, "head": HEAD, "issue": 5, "base": "main"}
        self.gh.merge_error = [GitHubError(405, 'Required status check "e2e" is expected.')]
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            row = {}
            outcome, text = merge_pull(self.gh, cfg, target, row, "", WORKFLOW, "main")
        self.assertEqual(outcome, "merge_refused")
        self.assertIn("refused to decide its next step", text)
        self.assertNotIn("waiting_since", row)
        self.assertIn("refused", target["found"])

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

    def ci_run(self, sha, event="push", status="completed", repo="acme/app"):
        """A run of `ci.yml` on `main`, newer than those before it."""
        runs = self.gh.workflow_history.setdefault("ci.yml", [])
        run_id = 70 + len(runs)
        runs.append(
            {
                "id": run_id,
                "head_branch": "main",
                "head_repository": {"full_name": repo},
                "event": event,
                "head_sha": sha,
                "status": status,
                "conclusion": "success" if status == "completed" else None,
                "html_url": f"https://github.com/acme/app/actions/runs/{run_id}",
            }
        )

    def merge_into_main(self, every):
        """What `after_merge` does after one more commit reached `main`."""
        self.gh.history["main"].append(f"{len(self.gh.history['main']) + 1:040x}")
        return after_merge(self.gh, [{"workflow": "ci.yml", "every": every}], "main")

    def started(self):
        return len([d for d in self.gh.dispatches if d["workflow"] == "ci.yml"])

    def test_a_workflow_with_every_starts_once_its_commits_reach_it(self):
        self.candidate()
        self.gh.history["main"] = ["0" * 40, "1" * 40]
        self.ci_run("0" * 40, event="workflow_dispatch")
        merge = {"auto": True, "after_workflows": ["e2e.yml", {"workflow": "ci.yml", "every": 3}]}
        outcome = self.run_report(cfg=make_config(merge=merge))
        self.assertEqual(outcome["outcome"], "merged")
        # The merge is the second commit since the run: `e2e.yml` starts, `ci.yml` waits.
        self.assertEqual([d["workflow"] for d in self.gh.dispatches], ["e2e.yml"])
        [row] = self.gh.run_comments(6)
        self.assertIn(
            "- `ci.yml` waits: 2 of 3 commits since its "
            "[latest run](https://github.com/acme/app/actions/runs/70).",
            row["body"],
        )
        self.assertEqual([r["workflow"] for r in row["run"]["after_merge"]], ["e2e.yml"])
        # The third starts it.
        self.assertIn("- Started [`ci.yml`]", self.merge_into_main(3))
        self.assertEqual(self.started(), 1)

    def test_a_persons_push_that_ran_the_workflow_resets_the_count(self):
        self.gh.history["main"] = ["0" * 40]
        self.ci_run("0" * 40, event="workflow_dispatch")
        self.merge_into_main(3)
        self.merge_into_main(3)
        # A person pushes to `main`, which runs `ci.yml`.
        self.gh.history["main"].append("p" * 40)
        self.ci_run("p" * 40)
        self.assertIn("1 of 3 commits", self.merge_into_main(3))
        self.assertIn("2 of 3 commits", self.merge_into_main(3))
        self.assertEqual(self.started(), 0)
        self.merge_into_main(3)
        self.assertEqual(self.started(), 1)

    def test_a_run_that_has_not_finished_holds_back_a_second_start(self):
        self.gh.history["main"] = ["0" * 40]
        self.ci_run("0" * 40)
        for _ in range(2):
            self.merge_into_main(2)
        self.assertEqual(self.started(), 1)
        head = self.gh.history["main"][-1]
        # The run it started is queued, then in progress: no merge starts another.
        for status in ("queued", "in_progress"):
            self.ci_run(head, event="workflow_dispatch", status=status)
            for _ in range(3):
                self.assertIn(
                    "- `ci.yml` waits: a run on `main` has not finished yet.",
                    self.merge_into_main(2),
                )
            self.gh.workflow_history["ci.yml"].pop()
        self.assertEqual(self.started(), 1)
        # Once it ended, the commits since its head start it again.
        self.ci_run(head, event="workflow_dispatch")
        self.merge_into_main(2)
        self.assertEqual(self.started(), 2)

    def test_a_failed_start_is_tried_again_at_the_next_merge(self):
        self.gh.history["main"] = ["0" * 40]
        self.ci_run("0" * 40)
        self.gh.dispatch_errors["ci.yml"] = GitHubError(503, "Service Unavailable")
        self.merge_into_main(2)
        self.assertIn("Could not start `ci.yml`", self.merge_into_main(2))
        del self.gh.dispatch_errors["ci.yml"]
        self.assertIn("- Started [`ci.yml`]", self.merge_into_main(2))

    def test_a_workflow_starts_when_in_doubt(self):
        self.gh.history["main"] = ["0" * 40]
        # No run on `main`: a run of a fork's pull request from its own `main` is none.
        self.ci_run("9" * 40, event="pull_request", repo="fork/app")
        self.assertIn("- Started [`ci.yml`]", self.merge_into_main(5))
        # The latest run's head is no longer on `main`, such as after a force push.
        self.ci_run("9" * 40)
        self.assertIn("- Started [`ci.yml`]", self.merge_into_main(5))
        # GitHub could not compare it.
        compare = self.gh.compare
        self.gh.compare = mock.Mock(side_effect=GitHubError(404, "Not Found"))
        self.assertIn("- Started [`ci.yml`]", self.merge_into_main(5))
        self.gh.compare = compare
        self.assertEqual(self.started(), 3)

    def test_a_workflow_without_every_starts_after_every_merge(self):
        self.gh.history["main"] = ["0" * 40]
        self.ci_run("0" * 40, status="in_progress")
        for _ in range(2):
            self.merge_into_main(1)
        self.assertEqual(self.started(), 2)

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
        self.assertIn(
            "could not verify, then approve the pull request, which NexKit then merges", mention
        )
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

    def checklist_review(self, *answers):
        review = review_result()
        review["output"]["checklist"] = [
            {"item": f"Rule {n}", "answer": answer, "evidence": f"file:{n}"}
            for n, answer in enumerate(answers, 1)
        ]
        return review

    def test_a_checklist_item_not_met_requests_changes_and_starts_a_fix_round(self):
        self.candidate(review=self.checklist_review("met", "not_met", "not_applicable"))
        outcome = self.run_report(cfg=make_config(fix={"max_auto_rounds": 1}))
        self.assertEqual(outcome["outcome"], "auto_fix")
        self.assertEqual(self.gh.dispatches[-1]["inputs"]["command"], "fix")
        [body] = [review["body"] for review in self.gh.created_reviews]
        self.assertIn("### NexKit review: changes requested", body)
        self.assertIn("NexKit requests changes because a checklist item is not met.", body)
        self.assertIn("🛑 Blocking: Checklist item not met: Rule 2. file:2", body)
        self.assertIn("- ✅ met: Rule 1", body)
        self.assertIn("- ❌ not met: Rule 2", body)
        self.assertIn("- ➖ not applicable: Rule 3", body)
        self.assertIn("- ❌ not met: Rule 2: file:2", body.split("<details>")[1])

    def test_an_item_a_person_decides_holds_the_merge_and_starts_no_fix_round(self):
        given = self.checklist_review("met", "not_met")
        given["output"]["verdict"] = "request_changes"
        given["output"]["checklist"][1]["person"] = True
        self.candidate(review=given)
        cfg = make_config(merge={"auto": True}, notify=["owner"])
        outcome = self.run_report(cfg=cfg)
        self.assertEqual(outcome["outcome"], "ready")
        self.assertEqual((self.gh.merged, self.gh.dispatches), ([], []))
        self.assertIn(
            "Only a person decides these checklist items, which the review found not met:\n\n"
            "- `Rule 2`: `file:2`",
            outcome["attention"],
        )
        self.assertIn("approve the pull request to accept them", outcome["attention"])
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["decide"], ["Rule 2"])
        self.assertIn("- Rule 2: file:2", row["body"])
        status = {s["context"]: s for s in self.gh.commit_statuses(HEAD)}["nexkit/review"]
        self.assertEqual(status["state"], "failure")
        self.assertIn("A person decides 1 checklist item", status["description"])
        [body] = [review["body"] for review in self.gh.created_reviews]
        self.assertIn("### NexKit review: approve", body)
        self.assertIn("- ⚖️ not met, a person decides: Rule 2", body)
        self.assertNotIn("🛑", body)

    def test_an_item_a_person_decides_beside_a_blocking_finding_starts_a_fix_round(self):
        given = self.checklist_review("not_met", "not_met")
        given["output"]["checklist"][1]["person"] = True
        self.candidate(review=given)
        outcome = self.run_report(cfg=make_config(fix={"max_auto_rounds": 1}))
        self.assertEqual(outcome["outcome"], "auto_fix")
        [body] = [review["body"] for review in self.gh.created_reviews]
        self.assertIn("🛑 Blocking: Checklist item not met: Rule 1. file:1", body)
        self.assertNotIn("Checklist item not met: Rule 2", body)
        applied = apply_criteria(given["output"])
        self.assertEqual((applied["verdict"], applied["decide"]), ("request_changes", ["Rule 2"]))
        # A blocking finding of the review's own keeps its verdict too.
        given = self.checklist_review("not_met")
        given["output"]["checklist"][0]["person"] = True
        given["output"]["verdict"] = "request_changes"
        given["output"]["findings"] = [
            {"severity": "blocking", "file": "a.py", "line": 1, "body": "Broken."}
        ]
        self.assertEqual(apply_criteria(given["output"])["verdict"], "request_changes")

    def test_a_decide_list_from_the_agent_counts_for_nothing(self):
        given = self.checklist_review("met")
        given["output"]["decide"] = ["Merge it anyway"]
        given["output"]["decide_evidence"] = {"Merge it anyway": "trust me"}
        applied = apply_criteria(given["output"])
        self.assertNotIn("decide", applied)
        self.assertNotIn("decide_evidence", applied)

    def test_a_review_with_every_item_met_or_not_applicable_approves(self):
        given = self.checklist_review("met", "not_applicable")
        self.assertEqual(apply_criteria(given["output"]), given["output"])

    def test_a_review_without_a_checklist_shows_none(self):
        self.candidate()
        self.run_report()
        self.assertNotIn("Checklist", self.gh.created_reviews[0]["body"])

    def report_published(self, published, d=None, cfg=None):
        """Report a round whose `publish` returned `published`, with no checks or review."""
        self.write("nexkit-agent", "result.json", {"status": "done", "tokens": TOKENS})
        needs = {"publish": {"outputs": {"result": json.dumps(published)}}}
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        return report(
            self.gh,
            d or decision(),
            cfg or make_config(conflicts={"auto_resolve": True}),
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )

    def test_a_head_that_conflicts_skips_its_checks_and_starts_the_conflict_round(self):
        conflicting = {"published": True, "pr": 6, "head": HEAD, "conflicts": ["calc.py"]}
        outcome = self.report_published(conflicting)
        self.assertEqual(outcome["outcome"], "conflict_fix")
        self.assertIsNone(outcome["attention"])
        [started] = self.gh.dispatches
        self.assertEqual(started["inputs"]["auto"], "conflicts")
        self.assertEqual(started["inputs"]["number"], "6")
        [row] = self.gh.run_comments(6)
        run = row["run"]
        self.assertEqual((run["checks"], run["verdict"]), ("not run", "not run"))
        self.assertEqual(run["conflicts_started"], "f" * 40)
        self.assertIn(
            "`ccccccc` conflicts with `main` in `calc.py`, so NexKit skipped its checks and AI "
            "review",
            row["body"],
        )
        self.assertIn("Started an automatic round on #6 to merge `main`.", row["body"])
        self.assertEqual(self.statuses(), {("nexkit/checks", "error"), ("nexkit/review", "error")})
        self.assertIn("conflicts with main", self.gh.statuses[0]["description"])
        self.assertEqual(self.gh.created_reviews, [])
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["feedback"], {"head": HEAD, "checks": None, "review": None})

    def test_a_head_that_conflicts_after_the_conflict_rounds_are_used_up_waits(self):
        earlier = {
            "round": 1,
            "trigger": "fix (auto, conflicts)",
            "conflicts": True,
            "base_sha": "b" * 40,
            "status": "success",
            "outcome": "ready",
            "url": "https://github.com/acme/app/actions/runs/90",
            "head": "a" * 40,
        }
        self.gh.comment(6, render_run(earlier, "Merged."))
        cfg = make_config(conflicts={"auto_resolve": True, "max_rounds": 1}, notify=["olivia"])
        conflicting = {"published": True, "pr": 6, "head": HEAD, "conflicts": ["calc.py"]}
        fix = decision("fix", pr=6, target=6, head="a" * 40, auto=True)
        outcome = self.report_published(conflicting, d=fix, cfg=cfg)
        self.assertEqual(outcome["outcome"], "conflicting")
        self.assertIn("NexKit started no round to resolve it", outcome["attention"])
        self.assertFalse([d for d in self.gh.dispatches if d["inputs"]["command"] == "fix"])
        latest = self.gh.run_comments(6)[-1]
        self.assertIn("its 1 automatic conflict rounds are used up", latest["body"])
        self.assertEqual(latest["run"]["status"], "success")
        self.assertTrue(self.gh.comments_matching(6, "@olivia a person is needed"))

    def test_a_round_brought_up_to_date_by_publish_says_so(self):
        self.candidate()
        updated = {"published": True, "pr": 6, "head": HEAD, "updated": "b" * 40}
        needs = {"publish": {"outputs": {"result": json.dumps(updated)}}}
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        cfg = make_config()
        report(
            self.gh,
            decision(),
            cfg,
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        [row] = self.gh.run_comments(6)
        self.assertIn(
            "Merged `main` (`bbbbbbb`) after this round's commit: its rules want pull requests "
            "up to date",
            row["body"],
        )
        self.assertEqual(row["run"]["checks"], "passed")

    def test_the_round_records_each_check_that_passed_on_its_head(self):
        self.candidate(review=review_result("request_changes"))
        unit = {"name": "unit", "run": "make unit", "exit_code": 0, "passed": True, "output": ""}
        e2e = {"name": "e2e", "run": "make e2e", "exit_code": 1, "passed": False, "output": "x"}
        self.write("nexkit-checks", "checks.json", [unit, e2e])
        self.run_report()
        [row] = self.gh.run_comments(6)
        self.assertEqual((row["run"]["head"], row["run"]["checks"]), (HEAD, "failed"))
        self.assertEqual(row["run"]["passed"], {"unit": check_fingerprint("make unit")})
        # A round with no check that passed records none.
        self.write("nexkit-checks", "checks.json", [e2e])
        self.run_report(decision("fix", pr=6, target=6, head=HEAD))
        self.assertNotIn("passed", self.gh.run_comments(6)[-1]["run"])

    def test_a_check_that_route_skipped_passes_and_is_recorded_on_the_new_head(self):
        cfg = make_config(
            checks=[
                {"name": "unit", "run": "make unit"},
                {"name": "e2e", "run": "make e2e", "unaffected_by": ["docs/**"]},
            ]
        )
        since = "a" * 40
        unit = {"name": "unit", "run": "make unit", "exit_code": 0, "passed": True, "output": ""}
        self.write("nexkit-checks", "checks.json", [unit])
        self.write("nexkit-review", "result.json", review_result())
        d = decision(
            "review",
            pr=6,
            target=6,
            head=HEAD,
            auto=True,
            setup="0" * 16,
            skipped_checks=[{"name": "e2e", "since": since}],
        )
        self.assertEqual(self.run_report(d=d, cfg=cfg, published=False)["outcome"], "ready")
        [row] = self.gh.run_comments(6)
        self.assertIn("`e2e` skipped: no path it reads changed since `aaaaaaa`.", row["body"])
        self.assertEqual(row["run"]["checks"], "passed")
        # The next round compares with this head, with the same command and setup.
        self.assertEqual(
            row["run"]["passed"],
            {"unit": check_fingerprint("make unit"), "e2e": check_fingerprint("make e2e")},
        )
        self.assertEqual(row["run"]["setup"], "0" * 16)
        [created] = self.gh.created_reviews
        self.assertIn("**Checks**: ✅ `unit`, ⏭️ `e2e`", created["body"])
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["feedback"]["checks"][1]["skipped"], since)

    def test_a_round_whose_checks_route_all_skipped_reports_without_a_verify_job(self):
        cfg = make_config(checks=[{"name": "e2e", "run": "make e2e", "unaffected_by": ["**"]}])
        self.write("nexkit-review", "result.json", review_result())
        skipped = [{"name": "e2e", "since": "a" * 40}]
        d = decision("review", pr=6, target=6, head=HEAD, auto=True, skipped_checks=skipped)
        self.assertEqual(self.run_report(d=d, cfg=cfg, published=False)["outcome"], "ready")
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["checks"], "passed")
        self.assertEqual(row["run"]["passed"], {"e2e": check_fingerprint("make e2e")})
        self.assertIn(("nexkit/checks", "success"), self.statuses())

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
        agent = {
            "status": "done",
            "summary": "Wrapped the popup.",
            "output": {**FIX_OUTPUT, "cause": cause},
        }
        self.write("nexkit-agent", "result.json", agent)
        self.failing(output)
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        return self.run_report(d=d, cfg=make_config(fix={"max_auto_rounds": 5}), head=head)

    def next_round(self):
        inputs = self.gh.dispatches[-1]["inputs"]
        cfg = make_config(fix={"max_auto_rounds": 5})
        return route(self.gh, "workflow_dispatch", {"sender": {"login": "nexkit"}}, inputs, cfg=cfg)

    def many_failing(self, number):
        """Six failing e2e shards, each with an output as long as NexKit stores, whose
        failure line comes first and changes its numbers each round, as a run's would."""
        output = f"  ✘ {number} desk.spec.ts:42 › picks an option ({number}.3s)\n"
        output += "teardown ok\n" * 990 + "1 failed"
        check = {"run": "scripts/e2e.sh", "exit_code": 1, "passed": False, "output": output}
        shards = [{**check, "name": f"e2e-{n}"} for n in range(1, 7)]
        self.write("nexkit-checks", "checks.json", shards)

    def state_comment(self):
        [body] = [c["body"] for c in self.gh.comments(6) if "<!-- nexkit:state " in c["body"]]
        return body

    def test_many_failing_checks_and_a_long_review_keep_the_state_within_the_limit(self):
        findings = [
            {"severity": "suggestion", "file": "a.py", "line": n, "body": "Rename it. " * 140}
            for n in range(1, 31)
        ]
        findings.append({"severity": "blocking", "file": "b.py", "line": 7, "body": "Off. " * 300})
        self.candidate(checks_pass=False, review=review_result("request_changes", findings))
        self.many_failing(1)
        self.run_report(cfg=make_config(fix={"max_auto_rounds": 5}))
        self.assertLessEqual(len(self.state_comment()), TEXT_LIMIT)
        _, state = read_state(self.gh.comments(6))
        shards = state["feedback"]["checks"]
        self.assertEqual(len(shards), 6)
        for shard in shards:
            self.assertTrue(shard["output_cut"])
            self.assertTrue(shard["output"].endswith("teardown ok\nteardown ok\n1 failed"))
            self.assertEqual(shard["signature"], "✘ desk.spec.ts: › picks an option ()")
        blocking = state["feedback"]["review"]["findings"][-1]
        self.assertEqual(blocking["body"], "Off. " * 300)  # whole: suggestions go first

        # The next fix round still sees each shard's failure line and real end.
        fix = {"action": "fix", "issue": 5, "pr": 6, "head": HEAD, "note": ""}
        feedback = prompt_texts(self.gh, fix)["feedback"]
        self.assertEqual(feedback.count("✘ 1 desk.spec.ts:42 › picks an option (1.3s)"), 6)
        self.assertEqual(feedback.count("teardown ok\n1 failed\n```"), 6)
        self.assertIn("Off. Off.", feedback)

        # Failing the same way after a fix compares by the recorded signatures, not by
        # what the state comment kept of the output.
        agent = {"status": "done", "summary": "Fixed.", "output": {**FIX_OUTPUT, "cause": ""}}
        self.write("nexkit-agent", "result.json", agent)
        self.many_failing(2)
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        self.run_report(d=d, cfg=make_config(fix={"max_auto_rounds": 5}), head="e" * 40)
        self.assertLessEqual(len(self.state_comment()), TEXT_LIMIT)
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["feedback"]["repeated"], [f"e2e-{n}" for n in range(1, 7)])

    def test_a_state_that_fits_is_saved_as_it_is(self):
        self.candidate(checks_pass=False, review=review_result("request_changes"))
        self.failing("  ✘ 1 desk.spec.ts:42 › picks an option (12.3s)\n" + "ok\n" * 3_000)
        self.run_report()
        _, state = read_state(self.gh.comments(6))
        [check] = state["feedback"]["checks"]
        self.assertNotIn("output_cut", check)
        self.assertEqual(
            len(check["output"]), len("  ✘ 1 desk.spec.ts:42 › picks an option (12.3s)\n") + 9_000
        )

    def test_passed_checks_output_leaves_room_for_the_blocking_finding(self):
        # 13 passed checks with as much output as NexKit stores, as in a repository with
        # 14 check jobs: their output is not recorded, so nothing a prompt reads is cut.
        passed = [
            {"name": f"unit-{n}", "run": "make unit", "exit_code": 0, "passed": True}
            for n in range(1, 14)
        ]
        passed = [{**check, "output": "ok\n" * 4_000} for check in passed]
        output = "  ✘ 1 desk.spec.ts:42 › picks an option (1.3s)\n"
        output += "teardown ok\n" * 990 + "1 failed"
        failing = {
            "name": "integration-1",
            "run": "make integration",
            "exit_code": 1,
            "passed": False,
            "output": output,
        }
        body = "The total leaves out refunds, so the invoice is too high. " * 26
        finding = {"severity": "blocking", "file": "b.py", "line": 7, "body": body}
        self.candidate(checks_pass=False, review=review_result("request_changes", [finding]))
        self.write("nexkit-checks", "checks.json", [*passed, failing])
        self.run_report(cfg=make_config(fix={"max_auto_rounds": 5}))
        self.assertLessEqual(len(self.state_comment()), TEXT_LIMIT)
        _, state = read_state(self.gh.comments(6))
        checks = state["feedback"]["checks"]
        self.assertEqual(len(checks), 14)
        for check in checks[:13]:
            self.assertEqual(
                check, {"name": check["name"], "run": "make unit", "exit_code": 0, "passed": True}
            )
        self.assertEqual(checks[13]["output"], output)
        self.assertNotIn("output_cut", checks[13])
        self.assertEqual(state["feedback"]["review"]["findings"][0]["body"], body)

        fix = {"action": "fix", "issue": 5, "pr": 6, "head": HEAD, "note": ""}
        feedback = prompt_texts(self.gh, fix)["feedback"]
        # The review NexKit records for the next round keeps a finding's first 1,500 characters.
        self.assertIn(body[:1_500].strip(), feedback)
        self.assertIn("✘ 1 desk.spec.ts:42 › picks an option (1.3s)", feedback)
        self.assertIn("teardown ok\n1 failed\n```", feedback)
        self.assertNotIn("unit-1", feedback)

    def test_a_check_that_passed_after_a_fix_is_told_apart_from_one_that_failed_again(self):
        def checks(unit_passed, e2e_line):
            unit = {"name": "unit", "run": "make unit", "passed": unit_passed}
            if unit_passed:
                unit.update(exit_code=0, output="ok\n")
            else:
                unit.update(exit_code=1, output="  ✘ 1 calc.test.ts:3 › adds (0.2s)\n")
            e2e = {"name": "e2e", "run": "scripts/e2e.sh", "exit_code": 1, "passed": False}
            self.write("nexkit-checks", "checks.json", [unit, {**e2e, "output": e2e_line}])

        self.candidate(checks_pass=False, review=review_result("request_changes"))
        checks(False, "  ✘ 1 desk.spec.ts:42 › picks an option (12.3s)\n")
        self.run_report(cfg=make_config(fix={"max_auto_rounds": 5}))

        # The fix made unit pass; e2e failed the same way.
        agent = {"status": "done", "summary": "Fixed.", "output": {**FIX_OUTPUT, "cause": ""}}
        self.write("nexkit-agent", "result.json", agent)
        checks(True, "  ✘ 3 desk.spec.ts:42 › picks an option (9.1s)\n")
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        self.run_report(d=d, cfg=make_config(fix={"max_auto_rounds": 5}), head="e" * 40)
        _, state = read_state(self.gh.comments(6))
        self.assertEqual(state["feedback"]["repeated"], ["e2e"])
        unit = state["feedback"]["checks"][0]
        self.assertTrue(unit["passed"])
        self.assertNotIn("output", unit)
        self.assertNotIn("signature", unit)
        signature = "✘ desk.spec.ts: › picks an option ()"
        self.assertEqual(state["fixes"][-1]["failing"], [{"name": "e2e", "signature": signature}])

        # After the next fix, unit fails as it did first: it passed in between, so it is
        # a new failure, not one that failed the same way after a fix.
        checks(False, "  ✘ 1 desk.spec.ts:50 › saves the ticket (1.0s)\n")
        self.run_report(d=d, cfg=make_config(fix={"max_auto_rounds": 5}), head="f" * 40)
        _, state = read_state(self.gh.comments(6))
        self.assertNotIn("repeated", state["feedback"])

    def test_a_check_that_fails_the_same_way_twice_gets_a_diagnosis_round(self):
        self.candidate(checks_pass=False, review=review_result("request_changes"))
        # The stored state that the implement run left, as every run of the item's group stores.
        self.assertTrue(host.store_import(self.gh, make_config(), 5))
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

    def test_a_long_review_keeps_its_blocking_findings_and_counts_the_rest(self):
        hints = [
            {"severity": "suggestion", "file": "", "line": 0, "body": f"Hint {n}"}
            for n in range(1, 36)
        ]
        blocking = {"severity": "blocking", "file": "calc.py", "line": 2, "body": "Wrong sign"}
        self.candidate(review=review_result("request_changes", [*hints, blocking]))
        self.run_report()
        _, state = read_state(self.gh.comments(6))
        last = state["last_review"]
        # Blocking first, then the suggestions in the reviewer's order, up to 30.
        bodies = [f["body"] for f in last["findings"]]
        self.assertEqual(bodies, ["Wrong sign", *(f"Hint {n}" for n in range(1, 30))])
        self.assertEqual(last["left_out"], 6)

        # The review of the next fix round is told how many were left out.
        decision = {"action": "fix", "issue": 5, "pr": 6, "head": "e" * 40, "note": ""}
        fix_result = {"status": "done", "summary": "Fixed the sign.", "output": {"cause": ""}}
        text = prompt_texts(self.gh, decision, "review", fix_result)["previous_round"]
        self.assertIn("1. blocking, `calc.py:2`: Wrong sign", text)
        self.assertIn("That review had 6 more findings, left out here", text)
        # The fix agent's own prompt reads the whole review, so it lists the finding too.
        feedback = prompt_texts(self.gh, decision, "fix")["feedback"]
        self.assertIn("`calc.py:2`: Wrong sign", feedback)

    def test_a_review_of_at_most_30_findings_is_recorded_whole_in_its_order(self):
        findings = [
            {"severity": "suggestion", "file": "", "line": 0, "body": f"Hint {n}"}
            for n in range(1, 30)
        ]
        findings.append({"severity": "blocking", "file": "calc.py", "line": 2, "body": "Sign"})
        self.candidate(review=review_result("request_changes", findings))
        self.run_report()
        _, state = read_state(self.gh.comments(6))
        last = state["last_review"]
        self.assertEqual([f["body"] for f in last["findings"]], [f["body"] for f in findings])
        self.assertNotIn("left_out", last)

    def test_a_blocked_run_comment_shows_its_blocker(self):
        reason = "The plan forbids a known.py entry"
        self.write(
            "nexkit-agent",
            "result.json",
            {"status": "blocked", "error": reason, "output": {"blocker": reason, "questions": []}},
        )
        outcome = self.run_report(published=False)
        self.assertEqual(outcome["outcome"], "agent_blocked")
        self.assertTrue(self.gh.comments_matching(5, f"NexKit stopped during implement: {reason}"))

    def test_a_blocked_run_with_questions_and_no_blocker_lists_the_decisions(self):
        self.write(
            "nexkit-agent",
            "result.json",
            {
                "status": "blocked",
                "error": "The agent reported it was blocked.",
                "output": {"blocker": "", "questions": [{"question": "Which port?"}]},
            },
        )
        self.run_report(published=False)
        self.assertTrue(self.gh.comments_matching(5, "Decisions needed"))
        self.assertTrue(self.gh.comments_matching(5, "Which port?"))

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

    def test_a_conflict_with_a_location_renders_the_comment_of_one_with_a_file(self):
        shapes = {
            "old": {"file": "Program.cs", "pr_change": "Moves the endpoints above the middleware."},
            "new": {
                "location": "Program.cs",
                "this_change": "Moves the endpoints above the middleware.",
            },
        }
        rows = {}
        for name, names in shapes.items():
            open_conflict = {
                **names,
                "base_change": "Adds request validation before the endpoints.",
                "question": "Should validation run before this pull request's endpoint?",
                "options": ["Keep validation first", "Keep the endpoints first"],
                "recommended": 1,
            }
            self.write(
                "nexkit-agent",
                "result.json",
                {
                    "status": "blocked",
                    "error": "A conflict needs a decision.",
                    "start_base": "b" * 40,
                    "conflicts": ["Program.cs"],
                    "output": {"status": "blocked", "open_conflicts": [open_conflict]},
                },
            )
            self.run_report(d=decision("fix", pr=6, target=6, head=HEAD), published=False)
            rows[name] = self.gh.run_comments(6)[-1]
            self.gh.issue_comments[6].clear()
            self.run_ids = itertools.count(1)
        self.assertEqual(rows["old"]["body"], rows["new"]["body"])
        self.assertEqual(rows["old"]["run"]["questions"], rows["new"]["run"]["questions"])
        [asked] = rows["new"]["run"]["questions"]
        self.assertTrue(asked["question"].startswith("`Program.cs`: Should validation run"))

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
        outcome = self.run_report(d=d, published=False, start=True)
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
        # Only the 👀 of the start stays: no 🚀 or 😕 until it finishes.
        self.assertEqual(self.gh.reactions, [(3, "eyes")])
        # The reset is within usage_limit.wait_minutes: a run waits for it.
        [wait] = self.gh.dispatches
        self.assertEqual(wait["inputs"], WAIT_INPUTS)

    def test_a_fix_round_at_the_usage_limit_keeps_the_statuses_pending(self):
        self.write("nexkit-agent", "result.json", {**LIMIT, "reset_known": False})
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        outcome = self.run_report(d=d, published=False, start=True)
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

    def test_a_review_run_again_at_the_usage_limit_resumes_as_that_run_again(self):
        self.candidate(review=LIMIT)
        cases = (
            (decision("review", pr=6, target=6, head=HEAD, autopilot=True, retry=2), "autopilot"),
            (decision("fix", pr=6, target=6, head=HEAD, auto=True, retry=2), ""),
        )
        for d, kinds in cases:
            self.run_report(d=d, published=d["action"] != "review")
            resume = self.gh.run_comments(6)[-1]["run"]["resume"]
            # A fix round's review resumes as `/nexkit review`, not as an automatic fix.
            expected = ",".join(filter(None, (kinds, "resume", "retry:2")))
            self.assertEqual((resume["command"], resume["auto"]), ("review", expected))

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
        self.assertEqual(self.run_report(d=d, published=False, start=True)["outcome"], "paused")
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


SHORTENED = [
    {"name": "the comment of `@alice` of 2026-11-01T00:00:00Z", "kept": 1000, "length": 39016}
]
SHORTENED_LINE = (
    "**Shortened for the agent:** these texts are longer than a prompt keeps, so its prompt "
    "had only their start and the agent read each whole from a file: the comment of "
    "`@alice` of 2026-11-01T00:00:00Z (1,000 of 39,016 characters)."
)


class ShortenedTextsTests(ReportCase):
    """The round's comment names the texts an agent's prompt shortened, so the person who
    answers the agent knows why it asks."""

    def test_a_blocked_round_names_the_shortened_texts(self):
        blocked = {"status": "blocked", "error": "Which file?", "shortened": SHORTENED}
        self.write("nexkit-agent", "result.json", blocked)
        outcome = self.run_report(published=False)
        self.assertEqual(outcome["outcome"], "agent_blocked")
        [run] = self.gh.run_comments(5)
        self.assertIn(SHORTENED_LINE, run["body"])

    def test_a_posted_plan_names_the_shortened_texts(self):
        self.write("nexkit-agent", "result.json", {"status": "done", "shortened": SHORTENED})
        plan = "https://github.com/acme/app/issues/5#issuecomment-1"
        published = json.dumps({"published": True, "comment": plan})
        needs = {"publish": {"outputs": {"result": published}}}
        os.environ["GITHUB_RUN_ID"] = "1"
        outcome = report(
            self.gh,
            decision("plan"),
            make_config(),
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        self.assertEqual(outcome["outcome"], "planned")
        self.assertIn(f"[Plan]({plan}) posted.\n\n{SHORTENED_LINE}", outcome["summary"])

    def test_a_pull_request_round_names_what_the_review_shortened(self):
        self.candidate()
        self.write("nexkit-review", "result.json", {**review_result(), "shortened": SHORTENED})
        self.run_report(decision("fix", pr=6, target=6, head=HEAD))
        self.assertIn(SHORTENED_LINE, self.gh.run_comments(6)[-1]["body"])

    def test_a_round_names_the_comments_its_prompt_left_out(self):
        left_out = {"name": "10 older comments of a discussion, of 2026-11-01T00:00:00Z"}
        shortened = [{**left_out, "left_out": 10}, *SHORTENED]
        blocked = {"status": "blocked", "error": "Which file?", "shortened": shortened}
        self.write("nexkit-agent", "result.json", blocked)
        self.run_report(published=False)
        [run] = self.gh.run_comments(5)
        self.assertIn(
            "**Shortened for the agent:** these texts are longer than a prompt keeps, so its "
            "prompt had only their start or left them out, and the agent read each whole "
            "from a file: 10 older comments of a discussion, of 2026-11-01T00:00:00Z (left "
            "out); the comment of `@alice` of 2026-11-01T00:00:00Z (1,000 of 39,016 "
            "characters).",
            run["body"],
        )

    def test_a_round_without_shortened_texts_says_nothing_of_them(self):
        self.candidate()
        self.run_report(decision("fix", pr=6, target=6, head=HEAD))
        self.assertNotIn("Shortened", self.gh.run_comments(6)[-1]["body"])
        self.assertEqual(shortened_line(None, {"shortened": [{"kept": 1}]}), "")


class LinkedTextsTests(ReportCase):
    """The round's comment names the texts NexKit read for the agents and the links it could
    not read, so a person sees what the plan and the change were made from."""

    def test_a_posted_plan_names_what_its_agent_read_and_did_not(self):
        linked = {"read": ["comment 12 on #2", "#3 with its discussion"], "skipped": []}
        linked["skipped"] = ["other/repo#9: not this repository"]
        result = {"status": "done", "linked": linked, "shortened": SHORTENED}
        self.write("nexkit-agent", "result.json", result)
        plan = "https://github.com/acme/app/issues/5#issuecomment-1"
        published = json.dumps({"published": True, "comment": plan})
        needs = {"publish": {"outputs": {"result": published}}}
        os.environ["GITHUB_RUN_ID"] = "1"
        outcome = report(
            self.gh,
            decision("plan"),
            make_config(),
            needs,
            self.artifacts,
            workflow_ref=WORKFLOW,
            default_branch="main",
        )
        self.assertIn(
            f"[Plan]({plan}) posted.\n\n"
            "**Read for the agent:** `comment 12 on #2`; `#3 with its discussion`.\n"
            "**Linked but not read:** `other/repo#9: not this repository`. The agents were told "
            "to ask rather than guess when the work depends on one of these.\n\n"
            f"{SHORTENED_LINE}",
            outcome["summary"],
        )

    def test_a_pull_request_round_names_what_the_review_read(self):
        self.candidate()
        linked = {"read": ["#3 with its discussion"], "skipped": []}
        self.write("nexkit-review", "result.json", {**review_result(), "linked": linked})
        self.run_report(decision("fix", pr=6, target=6, head=HEAD))
        body = self.gh.run_comments(6)[-1]["body"]
        self.assertIn("**Read for the agent:** `#3 with its discussion`.", body)
        self.assertNotIn("Linked but not read", body)
        self.assertEqual(linked_line(None, {"linked": "x"}, {"linked": {"read": [1]}}), "")


class FindingPlaceTests(unittest.TestCase):
    """A finding points at a `location`, which GitHub's comments and markers show as the
    `file` and `line` they always held."""

    def test_a_location_is_a_file_and_a_line_or_the_file_alone(self):
        self.assertEqual(place({"location": "nexkit/report.py:262"}), ("nexkit/report.py", 262))
        self.assertEqual(place({"location": "a.py"}), ("a.py", 0))
        self.assertEqual(place({"location": ""}), ("", 0))
        # A location in no file's form is shown whole, as the file of no line.
        self.assertEqual(
            place({"location": "form Customer › field Phone"}),
            (
                "form Customer › field Phone",
                0,
            ),
        )
        self.assertEqual(place({"file": "a.py", "line": 3}), ("a.py", 3))
        self.assertEqual(place({"file": None, "line": None}), ("", 0))

    def test_a_review_with_locations_gives_the_comment_and_the_marker_of_a_file_and_a_line(self):
        findings = [
            {"severity": "blocking", "location": "a.py:3", "body": "Wrong."},
            {"severity": "suggestion", "location": "b.py", "body": "Name it."},
        ]
        old = [
            {"severity": "blocking", "file": "a.py", "line": 3, "body": "Wrong."},
            {"severity": "suggestion", "file": "b.py", "line": 0, "body": "Name it."},
        ]
        new = {**review_result()["output"], "findings": findings}
        before = {**review_result()["output"], "findings": old}
        self.assertEqual(apply_criteria(new), apply_criteria(before))
        self.assertEqual(apply_criteria(new)["findings"], old)
        text = review_body({"output": new}, [])
        self.assertIn("Wrong. (`a.py:3`)", text)
        self.assertIn("Name it. (`b.py`)", text)
        self.assertEqual(text, review_body({"output": before}, []))


if __name__ == "__main__":
    unittest.main()
