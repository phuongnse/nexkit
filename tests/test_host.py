"""The GitHub host's calls of the engine: what follows a plan is the engine's answer, which
autopilot and `route` carry out in NexKit 6's words. Most decisions store no state; those after
a plan, for a first plan, after a vouch and about a pull request's rounds store the answer in
the item's group (docs/engine.md#getting-there)."""

import base64
import contextlib
import copy
import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase, mock

from nexkit import agent, autopilot, cli, engine, first_state, host, item_state, maintain, progress
from nexkit import config as configuration
from nexkit import report as reports
from nexkit.checks import recorded
from nexkit.github import GitHubError
from nexkit.protocol.size import MOST
from nexkit.report import NO_CHANGE_PLAN, TOO_LARGE, needs_person, report
from nexkit.route import route
from nexkit.state import (
    PLAN_MARKER,
    answers_marker,
    empty_state,
    latest_plan,
    plan_data,
    plan_data_record,
    profile_record,
    questions,
    read_state,
    render_run,
    render_state,
    run_comments,
)
from tests.support import (
    BOT_LOGIN,
    FakeGitHub,
    make_config,
    play_resume,
    profile_config,
    store_fields,
    store_pending,
)
from tests.test_autopilot import AutopilotCase, pilot, plan_output
from tests.test_item_state import CREATED
from tests.test_item_state import step as engine_step
from tests.test_report import (
    HEAD,
    LIMIT,
    ONCE,
    TOKENS,
    WORKFLOW,
    ReportCase,
    decision,
    review_result,
)

# The engine's own answer, which a test that stands in for it calls.
ANSWER = engine.answer
ASKED = {
    "question": "Keep the cache?",
    "options": ["Keep it", "Drop it", "Close the issue"],
    "recommended": 1,
    "closes": 3,
}


def seed(gh, cfg, number, now=None):
    """Store the first state of item `number` as `nexkit route` does before the item's first
    event (`host.store_import`), so that a decision starts from the stored state, and give
    what `item_state.read` then finds: (comment, state) at revision 1."""
    assert host.store_import(gh, cfg, number, now=now), f"#{number} already has its state"
    return item_state.read(gh, number)


def restate(gh, number, **fields):
    """Store `fields` over the state of item `number` at the next revision, as a decision
    that came before would have left it, and give the state stored."""
    read = item_state.read(gh, number)
    doc = {**copy.deepcopy(read[1]), **fields, "revision": read[1]["revision"] + 1}
    assert item_state.write(gh, number, doc, read), f"#{number}'s state moved"
    return item_state.read(gh, number)[1]


def merging(gh, cfg, number=5, since=None, **fields):
    """Seed item `number` and store a merge that waits for required checks over it: `accepting`
    with its `requiredChecks` wait, and `checksSince` when `since` (a time) is given. `fields`
    replace stored fields, such as `observed` or `attention`."""
    doc = seed(gh, cfg, number)[1]
    version = doc["change"]["version"]
    wait = {"id": "w1.1", "for": "requiredChecks", "until": None, "on": None}
    stored = {"state": "accepting", "pending": [], "waits": [wait], "observed": None}
    if since is not None:
        stored["checksSince"] = {"version": version, "at": autopilot.iso(since)}
    return restate(gh, number, **{**stored, **fields})


def refused_merge(gh, cfg, number=5, **fields):
    """Seed item `number` and store a merge that GitHub refused, left to a person: `ready` with
    `acceptRefused`, and the observation of its head. `fields` replace stored fields."""
    doc = seed(gh, cfg, number)[1]
    version = doc["change"]["version"]
    seen = {"changeId": doc["change"]["id"], "version": version, "conflicts": False}
    seen.update(merged=False, empty=False)
    text = first_state.SAYS["acceptRefused"]
    attention = {"reason": "acceptRefused", "text": text, "ref": None}
    stored = {"state": "ready", "pending": [], "attention": attention, "observed": seen}
    return restate(gh, number, **{**stored, **fields})


ROOT = Path(__file__).resolve().parent.parent


def runs_of(doc):
    """The stages of the agent runs that the outbox of the state `doc` holds."""
    return [a["stage"] for a in doc["pending"] if a["kind"] == "runAgent"]


def asked_step(answer):
    """The `step` requests that a stand-in for the engine's answer got, in order: a sweep's
    `sweep` request, which the engine gets beside them, has no event."""
    requests = [call.args[0] for call in answer.call_args_list]
    return [request for request in requests if request["call"] == "step"]


def comment_event(number, body, login="alice"):
    return {
        "action": "created",
        "issue": {"number": number},
        "comment": {
            "id": 77,
            "body": body,
            "user": {"login": login, "type": "User"},
            "author_association": "COLLABORATOR",
        },
    }


class HostCase(AutopilotCase):
    def plan(self, number, output=None, **run):
        """A plan run that posted a plan with the record of `output`."""
        self.run_comment(number, "plan", outcome="planned", **run)
        record = plan_data_record(output or {})
        return self.gh.comment(number, f"{PLAN_MARKER}\n## Plan\n\nAdd it.\n\n{record}")

    def answered(self, number):
        """Autopilot's answers comment, as it posts one before the plan they start."""
        record = {"answers": [{"question": ASKED["question"], "answer": "Keep it"}]}
        self.gh.comment(number, f"NexKit autopilot answers\n\n{answers_marker(record)}")

    def step(self, number, cfg=None):
        _, steps = autopilot.preview(self.gh, cfg or self.cfg)
        return {step["number"]: step for step in steps}[number]

    def requests(self, number, cfg=None):
        """The requests the engine gets while the sweep reads issue `number`."""
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            self.step(number, cfg)
        return asked_step(answer)


class AfterPlanTest(HostCase):
    def test_the_engine_hears_the_plan_s_result_or_the_comment_after_it(self):
        self.issue(5)
        self.plan(5, {"questions": [ASKED]})
        [request] = self.requests(5)
        self.assertEqual(request["event"]["kind"], "actionFinished")
        self.assertEqual(request["state"]["state"], "planning")
        self.assertEqual(request["state"]["title"], "Issue 5")
        self.assertEqual(request["policies"]["planApproval"], "autoWhenClean")
        self.gh.human_comment(5, "Keep it, but only for a day.")
        [request] = self.requests(5)
        self.assertEqual(request["event"]["kind"], "commented")
        self.assertEqual(request["state"]["state"], "awaitingApproval")
        self.gh.human_comment(5, "Close the issue")
        [request] = self.requests(5)
        self.assertEqual(request["event"]["kind"], "answered")
        self.assertEqual(request["event"]["answers"], [{"question": 1, "option": 3}])
        self.assertEqual(request["event"]["by"]["role"], "writer")
        step = self.step(5)
        self.assertEqual((step["state"], step["answer"]), ("close", "Close the issue"))

    def test_a_reader_s_closing_comment_plans_again_instead_of_closing(self):
        self.issue(5)
        self.plan(5, {"questions": [ASKED]})
        self.gh.human_comment(5, "Close the issue", login="bob")
        [request] = self.requests(5)
        self.assertEqual(request["event"]["kind"], "answered")
        self.assertEqual(request["event"]["by"]["role"], "reader")
        step = self.step(5)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))

    def test_answers_follow_the_answers_policy_once_in_a_row(self):
        self.issue(5)
        self.plan(5, {"questions": [ASKED]})
        self.assertEqual(self.step(5)["reason"], autopilot.QUESTIONS_REASON)
        cfg = pilot(answer="recommended")
        step = self.step(5, cfg)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertEqual(step["answers"], [{"question": ASKED["question"], "answer": "Keep it"}])
        self.answered(5)
        self.plan(5, {"questions": [ASKED]})
        self.assertEqual(self.step(5, cfg)["reason"], autopilot.REPEATED)
        self.assertIn("answers", self.requests(5, cfg)[0]["state"]["asked"])

    def test_a_plan_whose_recommended_answer_only_closes_needs_a_person_at_its_report(self):
        cfg = make_config(autopilot={"enabled": True, "answer": "recommended"}, notify=["olivia"])
        self.issue(5)
        output = plan_output(questions=[{**ASKED, "recommended": 3}])
        result = self.report_plan(cfg, output)
        self.assertIn("answer the plan's questions", result["attention"])
        self.assertNotIn("carries on", result["summary"])
        self.assertEqual(len(self.gh.comments_matching(5, "@olivia a person is needed")), 1)

    def test_the_sweep_waits_with_the_reason_the_plan_s_run_recorded(self):
        cfg = pilot(answer="recommended")
        closing = {"questions": [{**ASKED, "recommended": 3}]}
        self.issue(5)
        self.plan(5, closing, attention="answer the questions")
        step = self.step(5, cfg)
        self.assertEqual((step["state"], step["reason"]), ("waits", "answer the questions"))
        self.assertTrue(step["person"])
        # A run from before the report named it records none.
        self.issue(6)
        self.plan(6, closing)
        step = self.step(6, cfg)
        self.assertEqual((step["state"], step["reason"]), ("waits", autopilot.CLOSE_UNFOUNDED))
        self.assertTrue(step["person"])

    def test_a_plan_from_before_its_record_is_read_by_its_run(self):
        self.issue(5)
        self.planned(5, attention="answer the questions")
        self.assertEqual(self.step(5)["reason"], "answer the questions")
        self.issue(6)
        self.planned(6)
        self.assertEqual(self.step(6)["command"], "go")

    def test_the_parts_filed_are_those_the_engine_files(self):
        self.issue(5)
        split = [
            {"title": "First", "body": "One.", "depends_on": []},
            {"title": "Second", "body": "Two.", "depends_on": [1]},
        ]
        self.plan(5, {"too_large": True, "split": split})
        self.assertEqual(self.step(5)["parts"], split)
        # The parts are the ones the engine's createParts names, in its order.
        create = {"kind": "createParts", "parts": [{"key": 2, "title": "Second", "needs": []}]}
        answer = {"actions": [create], "state": {"asked": []}}
        with mock.patch.object(host, "step", return_value=answer):
            self.assertEqual(self.step(5)["parts"], [split[1]])

    SPLIT = [
        {"title": "First part", "body": "Do A.", "depends_on": []},
        {"title": "Second part", "body": "Do B.", "depends_on": [1]},
    ]

    def timed_out_plan(self):
        """An issue whose pull request's round ran out of time, and the too-large plan that
        autopilot asked for after it."""
        self.issue(5)
        self.plan(5)
        self.run_comment(5, "go", outcome="ready")
        self.gh.add_pull(9, 5)
        timed_out = {
            "round": 2,
            "trigger": "fix",
            "status": "failure",
            "url": "u",
            "outcome": "agent_error",
            "timed_out": True,
        }
        self.gh.comment(9, render_run(timed_out))
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])
        self.plan(5, {"too_large": True, "split": self.SPLIT})

    def test_the_plan_after_a_timed_out_round_goes_to_the_engine_with_its_pull_request(self):
        self.timed_out_plan()
        [request] = self.requests(5)
        self.assertIn("supersede", request["state"]["asked"])
        head = self.gh.pulls[9]["head"]["sha"]
        self.assertEqual(request["state"]["change"], {"id": "9", "version": head})

    def test_a_split_of_a_pull_request_s_issue_needs_the_engine_to_withdraw_the_change(self):
        self.timed_out_plan()
        create = {
            "kind": "createParts",
            "parts": [
                {"key": 1, "title": "First part", "needs": []},
                {"key": 2, "title": "Second part", "needs": [1]},
            ],
        }
        answer = {"actions": [create], "state": {"asked": []}}
        with mock.patch.object(host, "step", return_value=answer):
            step = self.step(5)
        self.assertEqual(step["state"], "waits")
        self.assertIn("refused to decide", step["reason"])
        self.assertIn("without withdrawing pull request #9", step["reason"])
        withdraw = {"kind": "withdrawChange", "changeId": "9", "reason": "The parts carry it."}
        # Another change than the issue's pull request is not carried out.
        answer = {"actions": [create, {**withdraw, "changeId": "10"}], "state": {"asked": []}}
        with mock.patch.object(host, "step", return_value=answer):
            self.assertIn("refused to decide", self.step(5)["reason"])
        answer = {"actions": [create, withdraw], "state": {"asked": []}}
        with mock.patch.object(host, "step", return_value=answer):
            step = self.step(5)
            self.assertEqual((step["state"], step["pull"]), ("split", 9))
            self.sweep()
        self.assertEqual(self.gh.pulls[9]["state"], "closed")
        self.assertTrue(self.gh.comments_matching(9, "Superseded by"))

    def test_each_reason_the_engine_waits_for_is_said_in_nexkit_6_s_words(self):
        self.issue(5)
        self.plan(5)
        for reason, text in (
            ("approval", TOO_LARGE),
            ("askedAgain", autopilot.REPEATED),
            ("closeRecommended", autopilot.CLOSE_UNFOUNDED),
        ):
            with self.subTest(reason=reason):
                need = {"kind": "needPerson", "reason": reason}
                answer = {"actions": [need], "state": {"asked": []}}
                with mock.patch.object(host, "step", return_value=answer):
                    self.assertEqual(self.step(5)["reason"], text)
        # A reason the host has no words for is refused, for that issue alone.
        answer = {"actions": [{"kind": "needPerson", "reason": "accept"}], "state": {"asked": []}}
        with mock.patch.object(host, "step", return_value=answer):
            self.assertIn("refused to decide", self.step(5)["reason"])

    def test_a_start_past_max_parallel_waits_and_nothing_is_stored(self):
        self.issue(5)
        self.issue(6)
        self.plan(5)
        self.plan(6)
        cfg = pilot(max_parallel=1)
        self.assertEqual(self.step(5, cfg)["command"], "go")
        step = self.step(6, cfg)
        self.assertEqual((step["state"], step["person"]), ("waits", False))
        self.assertIn("max_parallel", step["reason"])
        self.sweep(cfg)
        self.assertEqual(self.started(), [("go", 5)])
        for number in (5, 6):
            self.assertEqual(item_state.read(self.gh, number), (None, None))


class EventIdTest(HostCase):
    def test_a_source_gives_the_same_id_each_time_and_no_two_share_one(self):
        comment, review, event = {"id": 7}, {"id": 7, "state": "approved"}, {"id": 9}
        for kind, source in (("comment", comment), ("review", review), ("issue-event", event)):
            self.assertEqual(host.event_id(kind, dict(source)), host.event_id(kind, dict(source)))
        self.assertEqual(host.event_id("dispatch", 123), "dispatch-123")
        self.assertEqual(host.event_id("run", 123), host.event_id("run", 123))
        self.assertNotEqual(host.event_id("comment", comment), host.event_id("review", review))
        self.assertNotEqual(host.event_id("comment", comment), host.event_id("comment", {"id": 8}))
        self.assertNotEqual(host.event_id("dispatch", 123), host.event_id("run", 123))

    def test_only_a_positive_number_of_a_known_kind_names_an_event(self):
        for kind, source in (
            ("label", 1),
            ("comment", {"id": "7"}),
            ("comment", {}),
            ("comment", 0),
            ("comment", True),
            ("comment", -3),
        ):
            with self.assertRaises(ValueError, msg=(kind, source)):
                host.event_id(kind, source)

    def test_the_same_comment_sent_again_on_the_stored_state_changes_nothing(self):
        self.issue(5)
        self.plan(5, {"questions": [ASKED]})
        self.gh.human_comment(5, "Keep it, but only for a day.")
        [request] = self.requests(5)
        event = request["event"]
        self.assertEqual(event["kind"], "commented")
        self.assertRegex(event["id"], r"^comment-\d+$")
        self.assertEqual(self.requests(5)[0]["event"]["id"], event["id"])
        stored = {**request["state"], "revision": 1}
        self.assertTrue(item_state.write(self.gh, 5, stored, (None, None)))

        def answer(state):
            found = host.step(state, event, request["policies"])
            return found["state"], found["actions"], found["changed"]

        first = []
        item_state.apply(self.gh, 5, lambda state: first.append(answer(state)) or first[-1])
        self.assertTrue(first[0][2])
        again = []
        item_state.apply(self.gh, 5, lambda state: again.append(answer(state)) or again[-1])
        self.assertEqual((again[0][1], again[0][2]), ([], False))

    def test_a_closing_command_is_named_by_its_comment(self):
        self.issue(5)
        self.plan(5, {"questions": [{**ASKED, "recommended": 0}]})
        ids = []
        for number in (77, 78):
            event = comment_event(5, "/nexkit plan Close the issue")
            event["comment"]["id"] = number
            with mock.patch.object(host, "step", wraps=host.step) as step:
                self.assertEqual(
                    route(self.gh, "issue_comment", event, cfg=self.cfg)["task"], "close_answer"
                )
            ids.append(step.call_args.args[1]["id"])
        self.assertEqual(ids, ["comment-77", "comment-78"])

    def test_the_report_job_and_the_sweep_name_one_result_by_its_run(self):
        self.issue(5)
        url = "https://github.com/acme/app/actions/runs/123"
        self.run_comment(5, "plan", status="running", url=url)
        cfg = pilot(answer="recommended")
        decision = {"issue": 5, "pr": None, "action": "plan"}
        agent = {"output": {"questions": [ASKED]}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}):
            with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
                autopilot.carries_on(self.gh, cfg, decision, "planned", agent)
        [reported] = [call.args[0]["event"]["id"] for call in answer.call_args_list]
        self.plan(5, {"questions": [ASKED]}, url=url)
        [swept] = [request["event"]["id"] for request in self.requests(5)]
        self.assertEqual((reported, swept), ("run-123", "run-123"))

    def test_a_run_outside_github_actions_has_no_source(self):
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(host._run_event_id(), "run-local")
            self.assertEqual(host._run_event_id("https://example.com/x"), "run-local")


LATER = "2026-01-02T00:00:00Z"


class TakeTest(TestCase):
    """A run takes its item's events from their sources, after the state comment's cursor
    (docs/engine-hosts.md#hosts)."""

    CFG = make_config(autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}})

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(42)
        self.gh.add_pull(6, 42)
        change = {"id": "6", "version": HEAD}
        state, _, _ = engine_step(None, {**CREATED, "change": change})
        self.assertTrue(item_state.write(self.gh, 42, state, (None, None)))

    def state(self):
        return item_state.read(self.gh, 42)[1]

    def cursor(self):
        return item_state.since(item_state.read(self.gh, 42)[0])

    def review(self, login, state, at=LATER, commit=HEAD):
        review = {
            "id": self.gh._id(),
            "user": {"login": login, "type": "User"},
            "state": state,
            "commit_id": commit,
            "submitted_at": at,
            "body": "Looks right.",
        }
        self.gh.pr_reviews[6].append(review)
        return review

    def taken_events(self):
        """The events that `take` sends to the engine, which stands still, and the cursor."""
        events = []

        def step(state, event, found_policies):
            events.append(event)
            return {"state": state, "actions": [], "changed": False}

        with mock.patch.object(host, "step", step):
            self.assertEqual(host.take(self.gh, self.CFG, 42), [])
        return events

    def test_a_writer_s_command_a_remark_a_review_and_a_hold_label_each_give_an_event(self):
        gh = self.gh
        plan = gh.comment(42, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
        gh.comment(42, "A comment of NexKit's own, which is no event")
        asked = gh.human_comment(42, "/nexkit plan Keep it short", at=LATER)
        gone = gh.human_comment(42, "/nexkit go", at=LATER)
        remark = gh.human_comment(42, "Why not a cache?", login="bob", at=LATER)
        pretend = gh.human_comment(42, "/nexkit go", login="bob", at=LATER)
        fix = gh.human_comment(6, "/nexkit fix the test", at=LATER)
        robot = gh.human_comment(42, "Tests are green", login="some-app[bot]", at=LATER)
        robot["user"]["type"] = "Bot"
        approved = self.review("alice", "APPROVED")
        asked_changes = self.review("alice", "CHANGES_REQUESTED")
        self.review("bob", "APPROVED")
        self.review("alice", "COMMENTED")
        held = gh.label_event(42, "on-hold", "labeled", at=LATER)
        freed = gh.label_event(42, "on-hold", "unlabeled", at=LATER)
        gh.label_event(42, "bug", "labeled", at=LATER)
        events = self.taken_events()
        shown = [(e["kind"], e["id"]) for e in events]
        self.assertEqual(
            shown,
            [
                ("requested", f"comment-{asked['id']}"),
                ("requested", f"comment-{gone['id']}"),
                ("commented", f"comment-{remark['id']}"),
                ("commented", f"comment-{pretend['id']}"),
                ("requested", f"comment-{fix['id']}"),
                ("changeApproved", f"review-{approved['id']}"),
                ("changesRequested", f"review-{asked_changes['id']}"),
                ("held", f"issue-event-{held['id']}"),
                ("released", f"issue-event-{freed['id']}"),
            ],
        )
        by_id = {e["id"]: e for e in events}
        one = by_id[f"comment-{asked['id']}"]
        self.assertEqual((one["what"], one["note"], one["plan"]), ("plan", "Keep it short", None))
        self.assertEqual(one["by"], {"id": "alice", "role": "writer"})
        # A `go` names the plan that came before it, which is what the person saw.
        self.assertEqual(by_id[f"comment-{gone['id']}"]["plan"], str(plan["id"]))
        self.assertEqual(by_id[f"comment-{fix['id']}"]["what"], "fix")
        pretended = by_id[f"comment-{pretend['id']}"]
        # A command that was not taken as one is no collaborator's comment for the engine.
        self.assertEqual(pretended["by"], {"id": "bob", "role": "outside"})
        self.assertEqual(by_id[f"comment-{remark['id']}"]["by"], {"id": "bob", "role": "reader"})
        self.assertEqual(pretended["text"], {"name": "comment", "ref": str(pretend["id"])})
        sure = by_id[f"review-{approved['id']}"]
        self.assertEqual((sure["changeId"], sure["changeVersion"]), ("6", HEAD))
        note = by_id[f"review-{asked_changes['id']}"]["note"]
        self.assertEqual(note, {"name": "review", "ref": str(asked_changes["id"])})
        self.assertEqual(by_id[f"issue-event-{held['id']}"]["label"], "on-hold")
        # Each is the engine's to check, and each carries its time from the source.
        self.assertEqual({e["at"] for e in events}, {LATER})

    def test_a_command_is_answered_and_the_cursor_names_the_source_taken(self):
        asked = self.gh.human_comment(42, "/nexkit plan", at=LATER)
        [(event, actions)] = host.take(self.gh, self.CFG, 42)
        self.assertEqual(event["id"], f"comment-{asked['id']}")
        self.assertEqual([a["kind"] for a in actions], ["runAgent"])
        self.assertEqual(self.state()["state"], "planning")
        self.assertEqual(self.cursor(), (LATER, f"comment-{asked['id']}"))

    def test_a_command_on_a_closed_issue_is_passed_over_and_a_remark_still_counts(self):
        self.gh.close_issue(42)
        asked = self.gh.human_comment(42, "/nexkit plan", at=LATER)
        remark = self.gh.human_comment(42, "Why not a cache?", login="bob", at=LATER)
        before = self.state()
        events = []
        real = host.step

        def spy(state, event, found_policies):
            events.append(event["id"])
            return real(state, event, found_policies)

        with mock.patch.object(host, "step", spy):
            taken = host.take(self.gh, self.CFG, 42)
        # The command never reaches the engine, so the stored state has no stage of it; the
        # remark is taken as before, and the cursor is past both.
        self.assertEqual(events, [f"comment-{remark['id']}"])
        self.assertEqual(taken, [])
        self.assertNotIn(f"comment-{asked['id']}", self.state()["recentEvents"])
        self.assertNotEqual(self.state()["state"], "planning")
        self.assertEqual(self.state()["state"], before["state"])
        self.assertEqual(self.cursor(), (LATER, f"comment-{remark['id']}"))

    def test_a_command_and_a_review_on_a_closed_pull_request_are_passed_over(self):
        self.gh.pulls[6]["state"] = self.gh.issues[6]["state"] = "closed"
        self.gh.human_comment(6, "/nexkit fix", at=LATER)
        asked = self.review("alice", "CHANGES_REQUESTED")
        before = self.state()
        self.assertEqual(self.taken_events(), [])
        after = self.state()
        self.assertEqual((after["state"], after["pending"]), (before["state"], before["pending"]))
        # Both sources are behind the cursor.
        passed = item_state.order((LATER, f"review-{asked['id']}"))
        self.assertGreaterEqual(item_state.order(self.cursor()), passed)
        # The issue is open: a command on it is sent to the engine as before.
        asked = self.gh.human_comment(42, "/nexkit plan", at="2026-01-09T00:00:00Z")
        [event] = self.taken_events()
        self.assertEqual(event["id"], f"comment-{asked['id']}")

    def test_the_stored_run_names_the_model_of_the_plan_s_profile(self):
        cfg = profile_config()
        self.gh.add_issue(43)
        ready = Path(host.protocol.ROOT, "examples", "state", "ready.json")
        state = {**json.loads(ready.read_text()), "item": "43"}
        self.assertTrue(item_state.write(self.gh, 43, state, (None, None)))
        record = profile_record({"profile": "hard", "chosen_by": "triage"})
        self.gh.comment(43, f"{PLAN_MARKER}\n{record}\n## Plan\n\nAdd it.")
        self.gh.human_comment(43, "/nexkit fix", at=LATER)
        [(_, actions)] = host.take(self.gh, cfg, 43)
        self.assertEqual([a["kind"] for a in actions], ["runAgent"])
        pending = item_state.read(self.gh, 43)[1]["pending"]
        [stored] = [a for a in pending if a["kind"] == "runAgent"]
        expected = agent.action_for("fix", configuration.with_profile(cfg, "hard"))
        self.assertEqual((stored["agent"], stored["model"]), (expected["agent"], "opus"))
        self.assertNotEqual(stored["model"], agent.action_for("implement", cfg)["model"])

    def test_taking_again_after_the_events_were_taken_changes_nothing(self):
        self.gh.human_comment(42, "/nexkit plan", at=LATER)
        self.gh.human_comment(42, "Thanks", login="bob", at=LATER)
        self.gh.label_event(42, "on-hold", "labeled", at=LATER)
        self.assertEqual(len(host.take(self.gh, self.CFG, 42)), 2)
        before = copy.deepcopy(self.gh.issue_comments[42])
        with mock.patch.object(host.engine, "answer") as answer:
            with mock.patch.object(self.gh, "update_comment") as update:
                self.assertEqual(host.take(self.gh, self.CFG, 42), [])
        answer.assert_not_called()
        update.assert_not_called()
        self.assertEqual(self.gh.issue_comments[42], before)

    def test_another_writer_s_write_keeps_the_cursor(self):
        asked = self.gh.human_comment(42, "/nexkit plan", at=LATER)
        host.take(self.gh, self.CFG, 42)
        read = item_state.read(self.gh, 42)
        other, _, _ = engine_step(read[1], {**CREATED, "kind": "held", "id": "h1", "label": "x"})
        self.assertTrue(item_state.write(self.gh, 42, other, read))
        self.assertTrue(self.state()["hold"])
        self.assertEqual(self.cursor(), (LATER, f"comment-{asked['id']}"))
        self.assertEqual(host.take(self.gh, self.CFG, 42), [])

    def test_a_source_whose_answer_changed_nothing_is_not_answered_after_the_state_moved(self):
        # A remark on an item that is not waiting for a plan changes nothing.
        remark = self.gh.human_comment(42, "Why not a cache?", login="bob", at=LATER)
        self.assertEqual(host.take(self.gh, self.CFG, 42), [])
        self.assertEqual(self.cursor(), (LATER, f"comment-{remark['id']}"))
        revision = self.state()["revision"]
        # The state moves on, as a plan that waits for a person would take the remark.
        read = item_state.read(self.gh, 42)
        other, _, _ = engine_step(read[1], {**CREATED, "kind": "held", "id": "h1", "label": "x"})
        self.assertTrue(item_state.write(self.gh, 42, other, read))
        calls = []
        with mock.patch.object(host, "step", side_effect=lambda *a: calls.append(a)):
            self.assertEqual(host.take(self.gh, self.CFG, 42), [])
        self.assertEqual(calls, [])
        self.assertEqual(self.state()["revision"], revision + 1)

    def test_what_is_older_than_the_state_comment_counts_as_taken(self):
        # The import that built the first document read a command that came before it, so
        # of the comment's own second only a later comment is taken.
        gh = FakeGitHub()
        gh.add_issue(42)
        second = "2026-01-01T00:00:00Z"
        before = gh.human_comment(42, "/nexkit plan", at=second)
        state, _, _ = engine_step(None, CREATED)
        self.assertTrue(item_state.write(gh, 42, state, (None, None)))
        after = gh.human_comment(42, "/nexkit plan", at=second)
        # Listed late, though from before: it is not taken either.
        gh.human_comment(42, "/nexkit plan", at="2025-12-31T23:59:59Z")
        [(event, _)] = host.take(gh, self.CFG, 42)
        self.assertEqual(event["id"], f"comment-{after['id']}")
        self.assertLess(before["id"], after["id"])

    def test_a_work_item_with_no_state_comment_has_nothing_to_take(self):
        gh = FakeGitHub()
        gh.human_comment(7, "/nexkit plan", at=LATER)
        self.assertEqual(host.take(gh, self.CFG, 7), [])

    def test_a_cursor_only_counts_on_the_line_before_the_state_marker(self):
        asked = self.gh.human_comment(42, "/nexkit plan", at=LATER)
        host.take(self.gh, self.CFG, 42)
        [state_comment] = [c for c in self.gh.comments(42) if "nexkit:item-since" in c["body"]]
        line = f"{item_state.SINCE_PREFIX}{LATER} comment-{asked['id']} -->"
        self.assertIn(f"{line}\n{item_state.PREFIX}", state_comment["body"])
        for body in (
            state_comment["body"].replace(f"{line}\n", f"{line}\n\n"),
            f"{line}\n" + state_comment["body"].replace(f"{line}\n", ""),
            state_comment["body"]
            .replace(f"{line}\n", "")
            .replace("### NexKit state", f"### NexKit state\n\n{line}"),
        ):
            changed = {**state_comment, "body": body}
            self.assertIsNone(item_state.since(changed), body)
        edited = {**state_comment, "editors": ["mallory"]}
        self.assertIsNone(item_state.since(edited))


class StoreImportTest(TestCase):
    """The first run of an open item stores its state, imported from NexKit 6's markers
    (docs/engine-hosts.md#hosts)."""

    CFG = make_config(autopilot={"enabled": True})
    NOW = datetime(2026, 1, 3, tzinfo=UTC)

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.add_issue(42)

    def states(self):
        return [c for c in self.gh.comments(42) if c["body"].startswith("### NexKit state")]

    def test_an_open_item_with_no_state_comment_stores_its_import_at_revision_1(self):
        self.assertTrue(host.store_import(self.gh, self.CFG, 42, now=self.NOW))
        [comment] = self.states()
        doc = item_state.read(self.gh, 42)[1]
        self.assertEqual(doc, first_state.import_item(self.gh, self.CFG, 42, now=self.NOW))
        self.assertEqual(doc["revision"], 1)
        # The import keeps no cursor: `take` answers what came after the comment.
        self.assertIsNone(item_state.since(comment))
        # A second run finds it and stores nothing.
        self.assertFalse(host.store_import(self.gh, self.CFG, 42, now=self.NOW))
        self.assertEqual(len(self.states()), 1)

    def test_an_open_pull_request_of_the_issue_is_the_change_of_the_import(self):
        self.gh.add_pull(6, 42, head_sha=HEAD)
        self.gh.add_pull(7, 42, head_sha=HEAD, branch="nexkit/issue-42", repo="fork/app")
        self.assertTrue(host.store_import(self.gh, self.CFG, 42, now=self.NOW))
        doc = item_state.read(self.gh, 42)[1]
        self.assertEqual(doc["change"]["id"], "6")
        self.assertEqual(doc, first_state.import_item(self.gh, self.CFG, 42, 6, now=self.NOW))

    def test_a_person_s_pull_request_is_imported_as_its_own_item(self):
        self.gh.add_pull(9, 9, branch="feature")
        self.assertTrue(host.store_import(self.gh, self.CFG, 9, now=self.NOW))
        self.assertEqual(item_state.read(self.gh, 9)[1]["item"], "9")

    def test_a_closed_item_stores_nothing(self):
        self.gh.issues[42]["state"] = "closed"
        self.assertFalse(host.store_import(self.gh, self.CFG, 42, now=self.NOW))
        self.assertEqual(self.gh.comments(42), [])

    def test_a_state_comment_that_someone_else_changed_stores_and_takes_nothing(self):
        self.assertTrue(host.store_import(self.gh, self.CFG, 42, now=self.NOW))
        [comment] = self.states()
        comment["editors"] = [BOT_LOGIN, "mallory"]
        calls = []
        with (
            mock.patch.object(self.gh, "comment", side_effect=lambda *a: calls.append(a)),
            mock.patch.object(self.gh, "update_comment", side_effect=lambda *a: calls.append(a)),
        ):
            for call in (host.store_import, host.take):
                with self.subTest(call=call.__name__):
                    with self.assertRaises(host.Refused) as raised:
                        call(self.gh, self.CFG, 42)
                    self.assertIn("@mallory changed a comment of NexKit's", str(raised.exception))
        self.assertEqual(calls, [])
        self.assertEqual(len(self.states()), 1)

    def test_a_deleted_state_comment_is_imported_again_by_the_next_run(self):
        event = {"issue": {"number": 42}, "comment": {"id": 1, "body": "/nexkit plan"}}
        with mock.patch("sys.stdout"):
            cli._take(self.gh, self.CFG, "issue_comment", event)
            [comment] = self.states()
            self.gh.issue_comments[42].remove(comment)
            cli._take(self.gh, self.CFG, "issue_comment", event)
        [again] = self.states()
        self.assertNotEqual(again["id"], comment["id"])
        self.assertEqual(item_state.read(self.gh, 42)[1]["revision"], 1)

    def test_a_run_that_cannot_store_the_import_warns_and_goes_on(self):
        event = {"issue": {"number": 42}, "comment": {"id": 1, "body": "/nexkit plan"}}
        with (
            mock.patch.object(first_state, "import_item", side_effect=item_state.StateError("x")),
            mock.patch("sys.stdout") as out,
        ):
            cli._take(self.gh, self.CFG, "issue_comment", event)
        self.assertIn("::warning::", "".join(c.args[0] for c in out.write.call_args_list))
        self.assertEqual(self.states(), [])


class RefusedTest(HostCase):
    def setUp(self):
        super().setUp()
        for number in (5, 6):
            self.issue(number)
            self.plan(number)

    def test_an_unreadable_state_comment_holds_up_nothing(self):
        # A state document of a later schema, as an engine rolled back would find.
        doc = {"schemaVersion": 2, "revision": 3, "item": "5", "newField": 1}
        data = base64.b64encode(json.dumps(doc).encode()).decode()
        self.gh.comment(5, f"### NexKit state\n\n{item_state.PREFIX}{data} -->")
        self.sweep()
        self.assertEqual(self.started(), [("go", 5), ("go", 6)])

    def test_an_issue_the_engine_refuses_waits_alone(self):
        refused = {"protocol": 1, "error": "The state document passes the limit."}

        def answer(request):
            item = (request.get("event") or {}).get("item")
            return refused if item == "5" else ANSWER(request)

        decision = {"issue": 5, "pr": None, "action": "plan"}
        agent = {"output": {"questions": [ASKED]}}
        cfg = pilot(answer="recommended")
        self.run_comment(5, "plan", status="running")
        self.assertTrue(autopilot.carries_on(self.gh, cfg, decision, "planned", agent))
        with mock.patch.object(host.engine, "answer", side_effect=answer):
            self.assertFalse(autopilot.carries_on(self.gh, cfg, decision, "planned", agent))
            self.plan(5)
            step = self.step(5)
            self.assertEqual(step["state"], "waits")
            self.assertTrue(step["person"])
            self.assertIn("passes the limit", step["reason"])
            self.sweep()
            self.assertEqual(self.started(), [("go", 6)])
            self.plan(5, {"questions": [{**ASKED, "recommended": 0}]})
            event = comment_event(5, "/nexkit plan Close the issue")
            # An engine that refuses the item starts nothing for a person's command either: the
            # person gets a reply that says so.
            refusal = route(self.gh, "issue_comment", event, cfg=self.cfg)
            self.assertEqual((refusal["action"], refusal["reply_to"]), ("none", 5))
            self.assertIn("passes the limit", refusal["reason"])
        self.assertEqual(
            route(self.gh, "issue_comment", event, cfg=self.cfg)["task"], "close_answer"
        )


class DecideTest(HostCase):
    """A decision after a plan that runs in the item's group stores the engine's answer at the
    next revision, on the stored state document, and one that does not only previews it
    (`host.decide`)."""

    PLAN_RUN = {"url": "https://github.com/acme/app/actions/runs/1", "command": "plan"}
    START = {"basis": "b", "target": 5, "note": "", "remember": {}}

    def setUp(self):
        super().setUp()
        self.cfg = pilot(answer="recommended")
        self.issue(5)
        self.plan(5, {"questions": [ASKED]})
        seed(self.gh, self.cfg, 5)

    def read(self, number=5):
        return item_state.read(self.gh, number)

    def after_plan(self, number=5, **options):
        comments = self.gh.comments(number)
        plan = latest_plan(comments)
        issue = self.gh.issues[number]
        return host.after_plan(
            self.gh, self.cfg, issue, plan, self.PLAN_RUN, {}, self.START, comments, **options
        )

    def spied(self):
        """A stand-in for `host.step` that calls it and keeps its requests and answers."""
        seen, real = [], host.step

        def spy(state, event, found_policies):
            found = real(state, event, found_policies)
            seen.append((state, event, found))
            return found

        return seen, spy

    def decision(self):
        return {"issue": 5, "pr": None, "action": "plan"}

    def carried_on(self, **options):
        return host.plan_carried_on(
            self.gh, self.cfg, self.gh.issues[5], self.decision(), {"questions": [ASKED]}, **options
        )

    def test_after_plan_stores_the_engine_s_answer_at_the_next_revision(self):
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            step = self.after_plan(write=True)
        self.assertEqual(step["state"], "start")
        [(sent, event, found)] = seen
        self.assertEqual((sent["revision"], found["state"]["revision"]), (1, 2))
        self.assertEqual(self.read()[1], found["state"])
        # The plan's result is the answer of the plan run that the outbox holds.
        self.assertEqual(event["kind"], "actionFinished")
        self.assertEqual(event["action"], first_state.RUN_ID)

    def test_a_seeded_import_is_at_revision_1_and_after_plan_moves_it_to_2(self):
        # The state comment that setUp seeded goes, and `seed` stores the import again.
        self.gh.issue_comments[5] = [
            c for c in self.gh.comments(5) if item_state.PREFIX not in c["body"]
        ]
        self.assertEqual(self.read()[1], None)
        self.assertEqual(seed(self.gh, self.cfg, 5)[1]["revision"], 1)
        self.assertEqual(self.after_plan(write=True)["state"], "start")
        self.assertEqual(self.read()[1]["revision"], 2)

    def test_plan_carried_on_stores_the_engine_s_answer_at_the_next_revision(self):
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            self.assertTrue(self.carried_on(write=True))
        [(sent, _, found)] = seen
        self.assertEqual((sent["revision"], found["state"]["revision"]), (1, 2))
        self.assertEqual(self.read()[1], found["state"])

    def test_without_write_nothing_is_stored(self):
        before = copy.deepcopy(self.gh.comments(5))
        self.after_plan()
        self.carried_on()
        self.assertEqual(self.gh.comments(5), before)
        self.assertEqual(self.read()[1]["revision"], 1)

    def test_the_second_send_of_the_same_run_is_not_a_duplicate(self):
        # `report` stores the answer to the plan's run, and the step run sends the result of the
        # same run again: the engine would start nothing, so that one is decided on the markers.
        self.assertTrue(self.carried_on(write=True))
        stored = self.read()[1]
        self.assertIn(host._run_event_id(), stored["recentEvents"])
        self.assertEqual(self.after_plan(write=True)["state"], "start")
        self.assertEqual(self.read()[1]["revision"], 3)

    def test_a_write_that_lost_is_decided_again_after_a_wait_and_stored_next(self):
        seen, spy = self.spied()

        def racing(state, event, found_policies):
            found = spy(state, event, found_policies)
            if len(seen) == 1:
                comment, doc = self.read()
                other = {**doc, "revision": doc["revision"] + 1}
                self.assertTrue(item_state.write(self.gh, 5, other, (comment, doc)))
            return found

        with (
            mock.patch.object(host, "step", racing),
            mock.patch.object(host.time, "sleep") as sleep,
        ):
            self.assertEqual(self.after_plan(write=True)["state"], "start")
        self.assertEqual([sent["revision"] for sent, _, _ in seen], [1, 2])
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(self.read()[1]["revision"], 3)

    def test_writes_that_all_lose_warn_and_still_give_the_answer(self):
        real = host.step

        def racing(state, event, found_policies):
            found = real(state, event, found_policies)
            comment, doc = self.read()
            other = {**doc, "revision": doc["revision"] + 1}
            item_state.write(self.gh, 5, other, (comment, doc))
            return found

        out = io.StringIO()
        with mock.patch.object(host, "step", racing), mock.patch.object(host.time, "sleep"):
            with contextlib.redirect_stdout(out):
                step = self.after_plan(write=True)
        self.assertEqual(step["state"], "start")
        self.assertIn("::warning::", out.getvalue())
        # Each lost try was another write: the answer itself was never stored.
        self.assertEqual(self.read()[1]["revision"], 1 + item_state.TRIES)

    def test_with_no_state_comment_a_decision_posts_none(self):
        self.issue(6)
        self.plan(6)
        before = copy.deepcopy(self.gh.comments(6))
        step = self.after_plan(6, write=True)
        self.assertEqual(step["state"], "start")
        self.assertEqual(self.gh.comments(6), before)
        self.assertEqual(self.read(6), (None, None))

    def test_an_edited_state_comment_refuses_every_decision(self):
        comment, _ = self.read()
        comment["editors"] = [BOT_LOGIN, "mallory"]
        for options in ({}, {"write": True}):
            with self.assertRaises(host.Refused):
                self.after_plan(**options)
        with self.assertRaises(host.Refused):
            self.carried_on(write=True)
        with self.assertRaises(host.Refused):
            host.plan_waits_for({}, self.gh, self.cfg, 5)

    def test_what_a_plan_waits_for_is_read_on_the_stored_state_and_never_stored(self):
        output = {"questions": [ASKED]}
        before = copy.deepcopy(self.gh.comments(5))
        waits = host.plan_waits_for(output, self.gh, self.cfg, 5)
        self.assertEqual(waits, host.plan_waits_for(output))
        self.assertEqual(waits, "question")
        self.assertEqual(self.gh.comments(5), before)

    def test_a_state_comment_that_cannot_be_read_holds_up_nothing_and_is_not_written_over(self):
        comment, _ = self.read()
        comment["body"] = f"### NexKit state\n\n{item_state.PREFIX}{'QQ=='} -->"
        before = copy.deepcopy(self.gh.comments(5))
        self.assertEqual(self.after_plan(write=True)["state"], "start")
        self.assertEqual(self.gh.comments(5), before)

    def test_pending_id_names_the_action_that_a_result_answers(self):
        doc = host._planning(host._document({"number": 5}, {}), 0, False)
        self.assertEqual(host.pending_id(doc, "runAgent", "plan"), first_state.RUN_ID)
        doc["pending"][0]["id"] = "a3.1"
        self.assertEqual(host.pending_id(doc, "runAgent"), "a3.1")
        self.assertEqual(host.pending_id(doc, "runAgent", "plan"), "a3.1")
        self.assertEqual(host.pending_id(doc, "runAgent", "fix"), first_state.RUN_ID)
        self.assertEqual(host.pending_id(doc, "runChecks"), first_state.RUN_ID)

    def test_with_no_built_document_the_stored_state_alone_decides(self):
        state = copy.deepcopy(self.read()[1])
        self.assertIs(host.resync(state, None, {"id": "x"}), state)
        self.assertIsNone(host.resync(None, None, {"id": "x"}))
        event = {
            "protocol": 1,
            "kind": "commented",
            "id": "comment-9",
            "item": "5",
            "at": "2026-01-02T00:00:00Z",
            "by": {"id": "bob", "role": "reader"},
            "text": {"name": "comment", "ref": "9"},
        }
        stored, _ = host.decide(self.gh, self.cfg, 5, event, None, write=True)
        self.assertEqual(stored["recentEvents"][-1], "comment-9")
        self.assertEqual(self.read()[1]["recentEvents"][-1], "comment-9")
        self.assertEqual(stored["state"], self.read()[1]["state"])
        # An item with no state comment has the import in memory, and a state that cannot be
        # read has nothing to decide on.
        with mock.patch.object(host, "stored", return_value=(None, None)):
            with self.assertRaises(host.Refused):
                host.decide(self.gh, self.cfg, 5, {**event, "id": "comment-10"}, None)

    def test_the_stored_state_decides_only_where_it_agrees_with_the_markers(self):
        built = host._planning(host._document({"number": 5}, {}), 0, False)
        event = {"id": "run-1"}
        state = copy.deepcopy(built)
        state.update(revision=4, recentEvents=["comment-9"])
        state["pending"][0].update(id="a4.1", next="x")
        # It agrees, apart from its revision, its events seen and the ids of its outbox.
        self.assertIs(host.resync(state, built, event), state)
        # An event it holds is decided on the markers, at its revision, with no duplicate.
        state["recentEvents"] = ["comment-9", "run-1"]
        base = host.resync(state, built, event)
        self.assertEqual((base["revision"], base["recentEvents"]), (4, ["comment-9"]))
        self.assertEqual(host._core(base), host._core(built))
        # So is a state that says something else, or none.
        state["recentEvents"] = []
        state["state"] = "awaitingApproval"
        self.assertEqual(host.resync(state, built, event)["state"], "planning")
        self.assertEqual(host.resync(state, built, event)["revision"], 4)
        self.assertIs(host.resync(None, built, event), built)

    def test_a_stage_result_names_the_pending_action_of_the_stored_outbox(self):
        comment, doc = self.read()
        built = host._planning(copy.deepcopy(doc), 0, False)
        stored = copy.deepcopy(built)
        stored.update(revision=2)
        stored["pending"][0]["id"] = "a2.1"
        self.assertTrue(item_state.write(self.gh, 5, stored, (comment, doc)))
        plan = latest_plan(self.gh.comments(5))
        event = host._result(5, plan, plan_data(plan), self.PLAN_RUN)
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            host.decide(self.gh, self.cfg, 5, event, built, action=("runAgent", "plan"), write=True)
        [(sent, _, found)] = seen
        # The stored document agrees with the markers, so it is what the result is decided on.
        self.assertEqual((sent["revision"], sent["pending"][0]["id"]), (2, "a2.1"))
        self.assertEqual(seen[0][1]["action"], "a2.1")
        self.assertNotEqual(seen[0][1]["action"], first_state.RUN_ID)
        self.assertEqual(self.read()[1], found["state"])

    def test_a_tick_names_the_wait_of_the_document_it_is_decided_on(self):
        doc = {"waits": [{"id": "w2.1", "for": "usageLimit"}, {"id": "w2.2", "for": "time"}]}
        self.assertEqual(host.wait_id(doc, "usageLimit"), "w2.1")
        self.assertEqual(host.wait_id(doc, "time"), "w2.2")
        self.assertEqual(host.wait_id({"waits": []}, "time"), "w1.1")
        self.assertEqual(host.wait_id({}, "usageLimit"), "w1.1")

    def test_events_sent_in_a_row_give_one_state_at_the_next_revision(self):
        built = host._planning(host._document({"number": 5}, {}), 0, False)
        events = [
            host._event("held", 5, None, "held-5", label="on-hold"),
            host._event("tick", 5, None, "tick-5", wait="w1.1"),
        ]
        seen, spy = self.spied()
        before = copy.deepcopy(self.gh.comments(5))
        with mock.patch.object(host, "step", spy):
            state, answers = host.decide_all(
                self.gh, self.cfg, 5, events, built, wait="time", until=lambda actions: False
            )
        self.assertEqual([sent["kind"] for sent, _ in answers], ["held", "tick"])
        self.assertEqual(self.gh.comments(5), before)
        # The second event is sent on the state that the first gave.
        self.assertEqual(seen[1][0], seen[0][2]["state"])
        self.assertEqual(seen[1][1]["wait"], "w1.1")
        self.assertEqual(state["revision"], 2)
        # The row stops after the event whose actions satisfy `until`.
        with mock.patch.object(host, "step", spy):
            _, answers = host.decide_all(
                self.gh, self.cfg, 5, events, built, until=lambda actions: True
            )
        self.assertEqual([sent["kind"] for sent, _ in answers], ["held"])

    def test_with_no_configuration_the_markers_decide_and_nothing_is_read(self):
        built = host._planning(host._document({"number": 5}, {}), 0, False)
        event = host._event("held", 5, None, "held-5", label="on-hold")
        gh = mock.Mock()
        state, answers = host.decide_all(gh, None, 5, [event], built, found_policies={})
        self.assertEqual(state["revision"], 2)
        self.assertEqual(len(answers), 1)
        self.assertEqual(gh.mock_calls, [])

    def test_the_stored_state_that_holds_any_event_of_a_row_is_decided_on_the_markers(self):
        built = host._planning(host._document({"number": 5}, {}), 0, False)
        state = copy.deepcopy(built)
        state.update(revision=4, recentEvents=["comment-9"])
        events = [{"id": "held-5"}, {"id": "resume-5"}]
        self.assertIs(host.resync(state, built, events), state)
        state["recentEvents"] = ["comment-9", "held-5"]
        base = host.resync(state, built, events)
        self.assertEqual((base["revision"], base["recentEvents"]), (4, ["comment-9"]))
        state["recentEvents"] = ["resume-5", "held-5", "comment-9"]
        self.assertEqual(host.resync(state, built, events)["recentEvents"], ["comment-9"])


class FallbackTest(HostCase):
    """`host.on_fallback` hears each decision that `resync` took from the markers' document, once,
    with the function that asked, and nothing else changes in what the decision gives."""

    def setUp(self):
        super().setUp()
        self.cfg = pilot(answer="recommended")
        self.issue(5)
        self.plan(5, {"questions": [ASKED]})
        self.state = seed(self.gh, self.cfg, 5)[1]
        self.heard = []

    def event(self, name):
        return host._event(
            "commented",
            5,
            None,
            name,
            by=host._person("bob", "reader"),
            text={"name": "comment", "ref": name},
        )

    def decide(self, name, built, **options):
        with mock.patch.object(host, "on_fallback", lambda *heard: self.heard.append(heard)):
            return host.decide(self.gh, self.cfg, 5, self.event(name), built, write=True, **options)

    def test_a_built_document_that_agrees_with_the_stored_state_is_not_heard(self):
        self.decide("c-1", copy.deepcopy(self.state))
        self.assertEqual(self.heard, [])

    def test_a_stored_state_that_differs_from_the_built_document_is_heard_once(self):
        built = copy.deepcopy(self.state)
        built["title"] = "Another title"
        with mock.patch.object(host, "on_fallback", lambda *heard: self.heard.append(heard)):
            host.decide(self.gh, self.cfg, 5, self.event("c-1"), built, write=True)
        # The function that called `decide` is the one a later part converts.
        [(origin, why)] = self.heard
        self.assertEqual(why, "differs")
        self.assertEqual(origin, f"{__name__}.{type(self).__name__}.{self._testMethodName}")

    def test_an_event_that_the_stored_state_holds_already_is_heard_as_seen(self):
        self.decide("c-1", copy.deepcopy(self.state))
        self.decide("c-1", copy.deepcopy(self.state))
        self.assertEqual([why for _, why in self.heard], ["seen"])

    def test_a_decision_with_no_configuration_is_heard_as_no_state(self):
        with mock.patch.object(host, "on_fallback", lambda *heard: self.heard.append(heard)):
            host.decide(self.gh, None, 5, self.event("c-1"), copy.deepcopy(self.state))
        self.assertEqual([why for _, why in self.heard], ["no state"])

    def test_no_hook_changes_nothing_in_the_answer(self):
        built = copy.deepcopy(self.state)
        built["title"] = "Another title"
        with mock.patch.object(host, "on_fallback", None):
            quiet = host.decide(self.gh, self.cfg, 5, self.event("c-1"), built, write=True)
        self.assertEqual(self.heard, [])
        self.assertEqual(quiet[0]["revision"], self.state["revision"] + 1)

    def test_a_write_that_lost_and_is_decided_again_is_heard_once(self):
        real, seen = host.step, []

        def racing(state, event, found_policies):
            found = real(state, event, found_policies)
            seen.append(state["revision"])
            if len(seen) == 1:
                comment, doc = item_state.read(self.gh, 5)
                other = {**doc, "revision": doc["revision"] + 1}
                self.assertTrue(item_state.write(self.gh, 5, other, (comment, doc)))
            return found

        built = copy.deepcopy(self.state)
        built["title"] = "Another title"
        with mock.patch.object(host, "step", racing), mock.patch.object(host.time, "sleep"):
            self.decide("c-1", built)
        self.assertEqual(seen, [1, 2])
        self.assertEqual(len(self.heard), 1)


class SweepTest(HostCase):
    """The start steps of `autopilot.preview` go through the engine's `sweep`, which orders and
    limits them, and the host words its answer as NexKit 6 did."""

    def setUp(self):
        super().setUp()
        self.gh.add_milestone(1)
        self.gh.add_milestone(2)
        self.cfg = pilot(
            scope=None,
            max_parallel=3,
            priority=["urgent", "later"],
            labels={"journey": {"wave": 1}, "docs": {"wave": 2}},
        )

    def sweeps(self, answer):
        return [call.args[0] for call in answer.call_args_list if call.args[0]["call"] == "sweep"]

    def test_the_engine_gets_each_issue_with_its_labels_milestone_wave_and_awaited_pull_requests(
        self,
    ):
        self.issue(5, milestone=2, labels=["urgent"])
        self.issue(6, labels=["docs"])
        self.issue(7, milestone=None)
        self.issue(8, labels=["journey"])
        starts = [
            autopilot._step(issue, "start", "no plan yet") for issue in self.gh.issues.values()
        ]
        issues = {number: issue for number, issue in self.gh.issues.items()}
        where = autopilot.scope(self.gh, self.cfg)
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            started = host.sweep(self.cfg, where, starts, issues, 2, {8: [10, 11]})
        [request] = self.sweeps(answer)
        self.assertEqual(
            request["items"],
            [
                {
                    "item": "5",
                    "labels": ["urgent"],
                    "milestone": 1,
                    "wave": 0,
                    "awaited": False,
                    "account": None,
                },
                {
                    "item": "6",
                    "labels": ["docs"],
                    "milestone": 0,
                    "wave": 2,
                    "awaited": False,
                    "account": None,
                },
                {
                    "item": "7",
                    "labels": [],
                    "milestone": None,
                    "wave": 0,
                    "awaited": False,
                    "account": None,
                },
                {
                    "item": "8",
                    "labels": ["journey"],
                    "milestone": 0,
                    "wave": 1,
                    "awaited": True,
                    "account": None,
                },
            ],
        )
        self.assertEqual((request["running"], request["limited"]), (2, []))
        self.assertEqual(request["policies"]["maxParallel"], 3)
        self.assertEqual(request["policies"]["priority"], ["urgent", "later"])
        self.assertEqual(request["policies"]["splits"], host.policies(self.cfg)["splits"])
        # One place is left: the urgent issue takes it, and the awaited one, which comes before
        # the issue of a higher wave, starts beyond the limit.
        self.assertEqual([one["number"] for one in started], [5, 8])
        self.assertEqual(starts[0]["state"], "start")
        self.assertIn(
            "; pull requests #10, #11 wait for it, so it starts beyond max_parallel",
            starts[3]["reason"],
        )
        for one in (starts[1], starts[2]):
            self.assertEqual(
                (one["state"], one["person"], one["reason"]),
                ("waits", False, "4 issues are in progress (max_parallel)"),
            )

    def test_a_refused_sweep_leaves_each_start_waiting_for_a_person_and_the_others_as_they_were(
        self,
    ):
        self.issue(5)
        self.issue(6)
        self.issue(7, labels=["on-hold"])
        self.gh.add_issue(8, milestone=1)
        self.gh.blockers[8] = [5]
        cfg = {
            **self.cfg,
            "autopilot": {**self.cfg["autopilot"], "labels": {"on-hold": {"hold": True}}},
        }
        before = {s["number"]: s for s in autopilot.preview(self.gh, cfg)[1]}

        def answer(request):
            if request["call"] == "sweep":
                return {"protocol": 1, "error": "no sweep today"}
            return ANSWER(request)

        with mock.patch.object(host.engine, "answer", side_effect=answer):
            steps = {s["number"]: s for s in autopilot.preview(self.gh, cfg)[1]}
        self.assertEqual((before[5]["state"], before[6]["state"]), ("start", "start"))
        for number in (5, 6):
            self.assertEqual((steps[number]["state"], steps[number]["person"]), ("waits", True))
            self.assertIn("engine refused", steps[number]["reason"])
            self.assertIn("no sweep today", steps[number]["reason"])
        for number in (7, 8):
            self.assertEqual(steps[number], before[number])


class FirstPlanTest(HostCase):
    """The first plan of a new issue starts when the engine answers with a plan run."""

    HOLD = make_config(autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}})

    def test_the_engine_hears_a_start_and_the_plan_runs_on_its_plan_run(self):
        self.issue(5)
        [request] = self.requests(5)
        event, doc = request["event"], request["state"]
        self.assertEqual((event["kind"], event["what"], event["by"]), ("requested", "start", None))
        self.assertEqual(doc["state"], "new")
        self.assertEqual(doc["title"], "Issue 5")
        self.assertEqual([wait["for"] for wait in doc["waits"]], ["start"])
        self.assertFalse(doc["hold"])
        step = self.step(5)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertEqual(step["reason"], "no plan yet")
        self.sweep()
        self.assertEqual(self.started(), [("plan", 5)])

    def test_a_failed_plan_without_a_plan_starts_through_the_engine_too(self):
        self.issue(5)
        self.run_comment(5, "plan", status="failure", outcome="agent_error")
        [request] = self.requests(5)
        self.assertEqual(request["event"]["what"], "start")
        self.assertEqual(self.step(5)["state"], "start")

    def test_an_engine_that_refuses_or_starts_no_plan_leaves_the_issue_to_a_person(self):
        self.issue(5)
        self.issue(6)
        answers = {
            "refuses": {"protocol": 1, "error": "The state document passes the limit."},
            "no action": {"protocol": 1, "actions": [], "state": {}},
            "other stage": {
                "protocol": 1,
                "actions": [{"kind": "runAgent", "stage": "implement"}],
                "state": {},
            },
        }
        for name, found in answers.items():

            def answer(request, found=found):
                is_five = request["call"] == "step" and request["event"]["item"] == "5"
                return found if is_five else ANSWER(request)

            with self.subTest(name), mock.patch.object(host.engine, "answer", side_effect=answer):
                step = self.step(5)
                self.assertEqual(step["state"], "waits")
                self.assertTrue(step["person"])
                self.assertIn("engine refused", step["reason"])
                self.sweep()
                self.assertNotIn(5, [number for _, number in self.started()])
        # The other issue carries on.
        self.assertEqual(self.started(), [("plan", 6)])

    def test_a_held_or_blocked_new_issue_sends_the_engine_no_start(self):
        self.issue(5, labels=["on-hold"])
        self.issue(6)
        self.issue(7)
        self.gh.blockers[6] = [7]
        held = self.step(5, self.HOLD)
        self.assertEqual(held["state"], "held")
        self.assertEqual(self.step(6)["state"], "waits")
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            autopilot.preview(self.gh, self.HOLD)
        asked = [request["event"]["item"] for request in asked_step(answer)]
        self.assertEqual(asked, ["7"])

    def test_a_document_with_a_hold_or_a_blocker_wait_gets_no_action(self):
        self.issue(5)
        new = first_state.import_item(self.gh, self.cfg, 5, None)
        event = host._event(
            "requested", 5, None, "start-5-1", what="start", by=None, note="", plan=None, title=""
        )
        blocker = {"id": "w1.2", "for": "blocker", "until": None, "on": "7"}
        for name, change in {
            "hold": {"hold": True},
            "blocker": {"waits": [*new["waits"], blocker]},
        }.items():
            with self.subTest(name):
                doc = {**new, **change}
                answer = host.step(doc, event, host.policies(self.cfg))
                self.assertEqual(answer["actions"], [])
                self.assertEqual(answer["state"]["state"], "new")

    def first_plan(self, issue, write=False):
        return host.first_plan(self.gh, self.cfg, issue, {}, {}, write=write)

    def test_the_engine_gets_the_stored_state_and_the_start_stores_its_plan_run(self):
        issue = self.issue(5)
        before = seed(self.gh, self.cfg, 5)[1]
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            step = self.first_plan(issue, write=True)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        [request] = asked_step(answer)
        self.assertEqual(request["state"], before)
        self.assertEqual(request["event"]["id"], "start-5-1")
        self.assertEqual(request["event"]["title"], before["title"])
        stored = item_state.read(self.gh, 5)[1]
        self.assertEqual(stored["revision"], 2)
        self.assertEqual(runs_of(stored), ["plan"])

    def test_a_start_decides_on_the_import_when_nothing_is_stored_and_stores_nothing(self):
        issue = self.issue(5)
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            self.assertEqual(self.first_plan(issue)["state"], "start")
        [request] = asked_step(answer)
        self.assertEqual(request["state"], first_state.import_item(self.gh, self.cfg, 5, None))
        self.assertIsNone(item_state.read(self.gh, 5)[1])

    def test_a_plan_that_failed_before_the_engine_heard_it_starts_again_without_the_engine(self):
        issue = self.issue(5)
        seed(self.gh, self.cfg, 5)
        self.first_plan(issue, write=True)
        stored = item_state.read(self.gh, 5)[1]
        self.run_comment(5, "plan", status="failure", outcome="agent_error")
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            step = self.first_plan(issue, write=True)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertEqual(asked_step(answer), [])
        self.assertEqual(item_state.read(self.gh, 5)[1], stored)

    def test_a_start_after_a_stored_start_is_named_after_the_revision(self):
        issue = self.issue(5)
        seed(self.gh, self.cfg, 5)
        self.first_plan(issue, write=True)
        restate(self.gh, 5, pending=[], waits=[])
        names = []

        def answer(request):
            if request["call"] == "step":
                names.append(request["event"]["id"])
            return ANSWER(request)

        with mock.patch.object(host.engine, "answer", side_effect=answer):
            with self.assertRaises(host.Refused):
                self.first_plan(issue)
        self.assertEqual(names, ["start-5-3"])

    def test_a_first_plan_with_no_configuration_is_refused(self):
        issue = self.issue(5)
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            with self.assertRaises(host.Refused):
                host.first_plan(self.gh, None, issue, {}, {})
        self.assertEqual(asked_step(answer), [])


class CarriesOnTest(HostCase):
    def carries_on(self, number, output, cfg):
        decision = {"issue": number, "pr": None, "action": "plan"}
        agent = {"output": output}
        return autopilot.carries_on(self.gh, cfg, decision, "planned", agent)

    def test_answers_need_a_person_when_asked_again_or_when_one_only_closes(self):
        cfg = pilot(answer="recommended")
        self.issue(5)
        closing = {"questions": [{**ASKED, "recommended": 3}]}
        self.run_comment(5, "plan", status="running")
        # An answer that only closes the issue needs a person, whom `report` mentions.
        self.assertFalse(self.carries_on(5, closing, cfg))
        self.assertTrue(self.carries_on(5, {"questions": [ASKED]}, cfg))
        self.plan(5, {"questions": [ASKED]})
        self.answered(5)
        self.run_comment(5, "plan", status="running")
        self.assertFalse(self.carries_on(5, closing, cfg))
        self.assertFalse(self.carries_on(5, {"questions": [ASKED]}, cfg))
        protected = {"protected_paths": [".github/workflows/ci.yml"]}
        self.assertFalse(self.carries_on(5, protected, cfg))

    def test_a_plan_that_finds_no_change_carries_on_before_its_size(self):
        self.issue(1)
        for child, parent in ((2, 1), (3, 2), (4, 3)):
            self.issue(child, user="nexkit-acme[bot]", body=f"<!-- nexkit:part {parent} -->\nDo.")
            self.gh.parents[child] = parent
        self.run_comment(4, "plan", status="running")
        too_large = {"too_large": True, "split": []}
        # At the deepest level a plan that is too large waits for a person.
        self.assertFalse(self.carries_on(4, too_large, self.cfg))
        finding = {**too_large, "no_change": "Run 7 passed."}
        self.assertTrue(self.carries_on(4, finding, self.cfg))


def opened(gh, pull, head=HEAD):
    """The round that the implement run which opened the NexKit pull request `pull` posts. With
    none, the item is in its checks and the engine starts no command on it."""
    gh.round_comment(
        pull,
        1,
        head,
        "passed",
        command="go",
        trigger="implement",
        outcome="ready",
        verdict="approve",
    )


class ClosingCommandTest(HostCase):
    def setUp(self):
        super().setUp()
        self.issue(5)
        self.asked = self.plan(5, {"questions": [{**ASKED, "recommended": 0}]})
        # As `nexkit route` stores the first state before it routes the item's first event.
        seed(self.gh, self.cfg, 5)

    def decide(self, body, comment=None, taken=None):
        event = comment_event(5, body)
        if comment:
            event["comment"]["id"] = comment["id"]
        return route(self.gh, "issue_comment", event, cfg=self.cfg, taken=taken)

    def revision(self):
        return item_state.read(self.gh, 5)[1]["revision"]

    def test_a_writer_s_closing_answer_to_the_plan_is_the_engine_s_to_close(self):
        decision = self.decide("/nexkit plan Close the issue")
        found = (decision["task"], decision["answer"])
        self.assertEqual(found, ("close_answer", "Close the issue"))
        self.assertEqual(decision["asked"], self.asked["html_url"])

    def test_a_closing_go_that_take_stored_is_carried_out_from_its_answer(self):
        said = self.gh.human_comment(5, "/nexkit go Close the issue", at=LATER)
        taken = host.take(self.gh, self.cfg, 5)
        # The engine closes only on an answer: `take` sends the command as one.
        self.assertEqual([event["kind"] for event, _ in taken], ["answered"])
        self.assertEqual(self.revision(), 2)
        with mock.patch.object(host.engine, "answer", side_effect=AssertionError("asked again")):
            decision = self.decide("/nexkit go Close the issue", said, taken)
        found = (decision["task"], decision["answer"], decision["asked"])
        self.assertEqual(found, ("close_answer", "Close the issue", self.asked["html_url"]))
        self.assertEqual(decision["comment_id"], said["id"])
        self.assertEqual(self.revision(), 2)

    def test_a_closing_go_with_no_taken_event_only_previews_the_answer(self):
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            decision = self.decide("/nexkit go Close the issue")
        self.assertEqual(decision["task"], "close_answer")
        self.assertEqual([r["event"]["kind"] for r in asked_step(answer)], ["answered"])
        self.assertEqual(self.revision(), 1)

    def test_a_closing_command_whose_run_was_replaced_is_carried_out_by_the_run_that_ran(self):
        closing = self.gh.human_comment(5, "/nexkit go Close the issue", at=LATER)
        later = self.gh.human_comment(5, "Thanks.", login="bob", at=LATER)
        taken = host.take(self.gh, self.cfg, 5)
        self.assertEqual([event["kind"] for event, _ in taken][0], "answered")
        # The run of the later comment is the one that ran: it carries out the earlier command.
        decision = self.decide("Thanks.", later, taken)
        self.assertEqual(decision["task"], "close_answer")
        self.assertEqual(decision["comment_id"], closing["id"])
        self.assertEqual(decision["actor"], "alice")

    def test_a_plan_is_decided_on_the_stored_state_not_on_one_built_from_markers(self):
        read = item_state.read(self.gh, 5)
        changed = {**read[1], "revision": 2, "title": "Stored, not built from markers"}
        self.assertTrue(item_state.write(self.gh, 5, changed, read))
        stored = item_state.read(self.gh, 5)[1]
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            self.decide("/nexkit plan Keep it")
        [request] = asked_step(answer)
        self.assertEqual(request["state"], stored)
        self.assertEqual(request["state"]["title"], "Stored, not built from markers")

    def test_an_open_pull_request_keeps_the_issue_for_a_person(self):
        gh = self.gh = FakeGitHub()
        self.issue(5)
        self.plan(5, {"questions": [{**ASKED, "recommended": 0}]})
        gh.add_pull(6, 5)
        opened(gh, 6)
        seed(gh, self.cfg, 5)
        decision = self.decide("/nexkit plan Close the issue")
        self.assertEqual(decision["action"], "none")
        self.assertIn("Pull request #6 is already open for this issue", decision["reason"])

    def test_another_note_goes_to_its_round(self):
        self.assertEqual(self.decide("/nexkit go Keep it")["action"], "implement")


class NoConfigurationTest(HostCase):
    """A command is decided with the configuration's policies: without them `command` and
    `closing_command` raise `Refused` and the engine is not asked. `route._decide` gives the
    configuration error before it comes to that."""

    def test_command_refuses_without_the_configuration(self):
        self.issue(5)
        with mock.patch.object(host.engine, "answer") as answer:
            with self.assertRaises(host.Refused):
                host.command(None, 5, "plan", gh=self.gh, source_id=7, by="alice")
            with self.assertRaises(host.Refused):
                host.command(make_config(), 5, "plan", source_id=7)
        answer.assert_not_called()

    def test_closing_command_refuses_without_the_configuration(self):
        self.issue(5)
        self.plan(5, {"questions": [{**ASKED, "recommended": 0}]})
        with mock.patch.object(host.engine, "answer") as answer:
            with self.assertRaises(host.Refused):
                host.closing_command(
                    self.gh,
                    None,
                    5,
                    "Close the issue",
                    self.gh.comments(5),
                    7,
                    branch="nexkit/issue-5",
                    by="alice",
                )
        answer.assert_not_called()


class PoliciesTest(HostCase):
    def test_the_policies_follow_the_configuration(self):
        found = host.policies(pilot(answer="recommended"))
        self.assertEqual(
            found,
            {
                "planApproval": "autoWhenClean",
                "reviewAfterFailingChecks": "always",
                "accept": "person",
                "fixRounds": 2,
                "whenRoundsRunOut": "person",
                "conflicts": "person",
                "conflictRounds": 3,
                "answers": "recommended",
                "afterFailure": "replan",
                "splits": True,
                "splitParts": 10,
                "splitDepth": 3,
                "retryWaits": [15, 30, 60],
                "lostWaits": [0, 30, 60, 120],
                "budgets": {
                    "plan": {"seconds": 15 * 60},
                    "implement": {"seconds": 45 * 60},
                    "fix": {"seconds": 45 * 60},
                    "review": {"seconds": 20 * 60},
                },
                "agents": {
                    stage: {
                        key: agent.action_for(stage, pilot())[key]
                        for key in ("agent", "model", "effort")
                    }
                    for stage in ("plan", "implement", "fix", "review")
                },
            },
        )
        found = host.policies(self.cfg | {"autopilot": {**self.cfg["autopilot"], "enabled": False}})
        found = (found["planApproval"], found["answers"], found["afterFailure"], found["splits"])
        self.assertEqual(found, ("person", "never", "person", False))
        cfg = make_config(
            merge={"auto": True},
            fix={"max_auto_rounds": 5},
            conflicts={"auto_resolve": True, "max_rounds": 4},
        )
        found = host.policies(cfg)
        found = [found[name] for name in ("accept", "fixRounds", "conflicts", "conflictRounds")]
        self.assertEqual(found, ["autoWhenPassed", 5, "resolve", 4])

    def test_each_stage_s_seconds_are_what_the_runner_gets(self):
        cfg = profile_config(stages={"implement": {"timeout_minutes": 50}})
        found = host.policies(cfg)["budgets"]
        self.assertEqual(
            set(found), {"triage", "plan", "implement", "fix", "review"}, "no `item` key"
        )
        self.assertEqual(found["implement"], {"seconds": 3000})
        self.assertEqual(found["fix"], found["implement"])
        for stage, budget in found.items():
            self.assertEqual(budget, {"seconds": agent.action_for(stage, cfg)["budget"]["seconds"]})

    def stage_runs(self, found_policies):
        """The `runAgent` that the engine answers for each stage it runs, by stage: a plan from
        a new item, an implement round from its go, a fix round from a ready change, and a
        review after the checks passed."""
        runs = {}
        passed = {"name": "test", "passed": True, "skipped": False}
        checks = {"checks": [{**passed, "signature": None, "knownBaseFailure": False}]}
        starts = (
            ("plan", "new", {"kind": "requested", "what": "plan"}),
            ("implement", "new", {"kind": "requested", "what": "go"}),
            ("fix", "ready", {"kind": "requested", "what": "fix"}),
            ("review", "checking", {"kind": "actionFinished", "action": "runChecks"}),
        )
        for stage, state, event in starts:
            example = host.protocol.ROOT / "examples" / "state" / f"{state}.json"
            doc = json.loads(example.read_text())
            event = {
                "protocol": 1,
                "id": "e1",
                "item": "42",
                "at": "2026-11-23T15:01:11Z",
                "by": {"id": "alice", "role": "writer"},
                "note": "",
                "plan": None,
                **event,
            }
            if event["kind"] == "actionFinished":
                pending = next(one for one in doc["pending"] if one["kind"] == event["action"])
                event |= {"action": pending["id"], "changeVersion": None, "result": checks}
                event["changeVersion"] = pending.get("changeVersion")
            found = host.step(doc, event, found_policies)
            [runs[stage]] = [one for one in found["actions"] if one["kind"] == "runAgent"]
            self.assertEqual(runs[stage]["stage"], stage)
        return runs

    def assertNames(self, run, expected):
        found = {key: run[key] for key in ("agent", "model", "effort")}
        self.assertEqual(found, {key: expected[key] for key in found})

    def test_each_stage_s_agent_is_what_its_runner_runs(self):
        cfg = make_config()
        found = host.policies(cfg)
        for stage, run in self.stage_runs(found).items():
            with self.subTest(stage=stage):
                self.assertNames(run, agent.action_for(stage, cfg))
                self.assertNames(run, found["agents"][stage])
        self.assertNotIn("triage", found["agents"], "no profiles, no triage stage")

    def test_with_a_profile_each_stage_names_what_the_profile_s_runner_runs(self):
        cfg = profile_config()
        triage = {key: agent.action_for("triage", cfg)[key] for key in ("agent", "model", "effort")}
        for name in cfg["profiles"]:
            with self.subTest(profile=name):
                found = host.policies(cfg, name)
                self.assertEqual(found["agents"]["triage"], triage, "no profile changes triage")
                self.assertEqual(
                    found["agents"]["plan"], {"agent": None, "model": None, "effort": None}
                )
                variant = configuration.with_profile(cfg, name)
                runs = self.stage_runs(found)
                self.assertNames(runs["plan"], {"agent": None, "model": None, "effort": None})
                for stage in ("implement", "fix", "review"):
                    self.assertNames(runs[stage], agent.action_for(stage, variant))
        # The profiles differ, so the test tells them apart.
        models = {
            host.policies(cfg, name)["agents"]["implement"]["model"] for name in cfg["profiles"]
        }
        self.assertEqual(len(models), 2)
        # A name that is no profile leaves the configuration as it is.
        self.assertEqual(host.policies(cfg, "gone")["agents"], host.policies(cfg)["agents"])

    def test_a_profile_s_time_is_the_runner_s_time(self):
        cfg = profile_config()
        found = host.policies(cfg, "hard")["budgets"]
        variant = configuration.with_profile(cfg, "hard")
        for stage in ("implement", "fix", "review"):
            expected = agent.action_for(stage, variant)["budget"]["seconds"]
            self.assertEqual(found[stage], {"seconds": expected})

    def test_the_item_s_profile_is_its_latest_plan_s_or_the_default(self):
        cfg = profile_config()
        self.assertIsNone(host.item_profile(make_config(), []))
        self.assertEqual(host.item_profile(cfg, []), "standard")

        def plan(name):
            record = profile_record({"profile": name, "chosen_by": "triage"})
            return {
                "id": 1,
                "user": {"login": BOT_LOGIN, "type": "Bot"},
                "body": f"{PLAN_MARKER}\n{record}\n## Plan\n\nAdd it.",
            }

        self.assertEqual(host.item_profile(cfg, [plan("hard")]), "hard")
        self.assertIsNone(host.item_profile(cfg, [plan("gone")]))

    def test_lost_work_starts_again_only_with_autopilot(self):
        off = self.cfg | {"autopilot": {**self.cfg["autopilot"], "enabled": False}}
        self.assertEqual(host.policies(off)["lostWaits"], [])
        self.assertEqual(host.policies(self.cfg)["lostWaits"], [0, 30, 60, 120])


class AfterGoTest(HostCase):
    """What follows a `/nexkit go` that stopped as blocked, with questions, protected paths or
    nothing to answer, is the engine's answer, in NexKit 6's words."""

    def stored(self, number, cfg=None, **run):
        """A `/nexkit go` that was running when the item's state was stored, as
        `nexkit route` stores it before the item's first event, and then stopped with the marker
        `run`. The stored state waits for the implement run, as it does until `report` stores
        the result. Its run comment, which is the same comment, is returned."""
        self.runs += 1
        url = f"https://github.com/acme/app/actions/runs/{self.runs}"
        started = {"url": url, "command": "go", "status": "running"}
        comment = self.gh.comment(number, render_run(started, "Running."))
        host.store_import(self.gh, cfg or self.cfg, number)
        ended = {"url": url, "command": "go", "status": "failure", **run}
        return self.gh.update_comment(comment["id"], render_run(ended, "Done."))

    def blocked(self, number, cfg=None, **fields):
        """A plan, then a `/nexkit go` that stopped as blocked with nothing to answer."""
        self.issue(number)
        self.plan(number)
        run = {"outcome": "agent_blocked", "attention": "It needs a secret.", **fields}
        return self.stored(number, cfg, **run)

    def failed(self, number, cfg=None, **fields):
        """A plan, then a `/nexkit go` that failed on an agent error with no pull request."""
        self.issue(number)
        self.plan(number)
        run = {"outcome": "agent_error", "attention": "The agent crashed.", **fields}
        return self.stored(number, cfg, **run)

    def stopped(self, number, asked=(), cfg=None, **fields):
        """A plan, then a `/nexkit go` that stopped as blocked with the questions `asked`, as
        the round's marker keeps them."""
        self.issue(number)
        self.plan(number)
        found = questions([dict(item) for item in asked])
        run = {"outcome": "agent_blocked", "questions": found, "attention": "Answer them."}
        return self.stored(number, cfg, **{**run, **fields})

    def after_go(self, number, cfg=..., write=False):
        """`host.after_go` for the item's latest `/nexkit go`, as the sweep reads it, with the
        configuration `cfg`, which is the test's own by default."""
        cfg = self.cfg if cfg is ... else cfg
        comments = self.gh.comments(number)
        comment, run = [(c, r) for c, r in run_comments(comments) if "command" in r][-1]
        issue = self.gh.issues[number]
        found = autopilot.find_record(comments)[1]
        start = {"basis": "b", "target": number, "note": "", "remember": {}}
        return host.after_go(self.gh, cfg, issue, comment, run, found, start, comments, write=write)

    def answering(self, number, asked=(), cfg=None, **fields):
        """A plan, a `/nexkit go` that was blocked, autopilot's answers to it, and the
        `/nexkit go` they started, which stored its state while it ran: the state asks for
        `answers`. That round then stopped as blocked with the questions `asked`."""
        self.issue(number)
        self.plan(number)
        first = questions([dict(item) for item in asked])
        self.run_comment(number, "go", status="failure", outcome="agent_blocked", questions=first)
        self.answered(number)
        run = {"outcome": "agent_blocked", "questions": first, "attention": "Answer them."}
        return self.stored(number, cfg, **{**run, **fields})

    def test_the_engine_hears_the_round_s_result_then_a_comment_after_it(self):
        self.stopped(5, [{**ASKED, "recommended": 0}])
        [request] = self.requests(5)
        self.assertEqual(request["event"]["kind"], "actionFinished")
        self.assertEqual(request["state"]["state"], "working")
        self.assertEqual(request["event"]["result"]["output"]["questions"][0]["closes"], 3)
        self.assertEqual(self.step(5)["reason"], "Answer them.")
        self.gh.human_comment(5, "Keep it, but only for a day.")
        kinds = [request["event"]["kind"] for request in self.requests(5)]
        self.assertEqual(kinds, ["actionFinished", "commented"])
        step = self.step(5)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertIn("collaborators commented after the stopped", step["reason"])

    def test_a_writer_s_closing_comment_is_an_answer_and_a_reader_s_a_comment(self):
        self.stopped(5, [ASKED])
        self.gh.human_comment(5, "close the issue.", login="bob")
        self.assertEqual(
            self.requests(5, pilot(answer="recommended"))[-1]["event"]["kind"], "actionFinished"
        )
        step = self.step(5, pilot(answer="recommended"))
        self.assertEqual((step["state"], step["command"]), ("start", "go"))
        self.gh.human_comment(5, "Close the issue")
        requests = self.requests(5)
        self.assertEqual([r["event"]["kind"] for r in requests], ["actionFinished", "answered"])
        # The answer names the questions that the engine gave the round's result.
        asked = requests[0]["state"]["pending"][0]["id"]
        self.assertEqual(requests[1]["event"]["asked"], asked)
        self.assertEqual(requests[1]["event"]["answers"], [{"question": 1, "option": 3}])
        step = self.step(5)
        self.assertEqual(
            (step["state"], step["answer"], step["by"]), ("close", "Close the issue", "alice")
        )

    def test_answers_follow_the_answers_policy_once_in_a_row(self):
        cfg = pilot(answer="recommended")
        self.stopped(5, [ASKED], cfg)
        step = self.step(5, cfg)
        self.assertEqual((step["command"], step["target"]), ("go", 5))
        self.assertEqual(step["answers"], [{"question": ASKED["question"], "answer": "Keep it"}])
        self.answering(6, [ASKED], cfg)
        self.assertEqual(self.step(6, cfg)["reason"], autopilot.REPEATED)

    def test_a_comment_on_questions_asked_again_lets_the_engine_answer_them_again(self):
        cfg = pilot(answer="recommended")
        self.answering(5, [ASKED], cfg)
        self.assertEqual(self.step(5, cfg)["reason"], autopilot.REPEATED)
        self.gh.human_comment(5, "Keep it, the other one changed.")
        # A person commented since the answers: the engine answers the questions again.
        first, second = self.requests(5, cfg)
        self.assertIn("answers", first["state"]["asked"])
        self.assertEqual(second["event"]["kind"], "commented")
        self.assertNotIn("answers", second["state"]["asked"])
        step = self.step(5, cfg)
        self.assertEqual((step["state"], step["command"]), ("start", "go"))

    def test_each_answer_the_engine_gives_is_carried_out_in_nexkit_6_s_words(self):
        cfg = pilot(answer="recommended")
        replans = {**ASKED, "question": "Split it?", "recommended": 2, "replans": [2]}
        self.stopped(5, [replans])
        step = self.step(5, cfg)
        self.assertEqual((step["command"], step["replan"]), ("plan", True))
        self.assertTrue(step["note"].startswith(autopilot.REPLAN_NOTE))
        closing = {**ASKED, "recommended": 3}
        self.stopped(6, [closing], no_change="Run 7 passed.")
        step = self.step(6, cfg)
        self.assertEqual((step["state"], step["finding"]), ("close", "Run 7 passed."))
        self.stopped(7, [closing])
        self.assertEqual(self.step(7, cfg)["reason"], autopilot.CLOSE_UNFOUNDED)
        self.stopped(8, [ASKED], protected_paths=["a.yml"])
        self.assertEqual(self.step(8, cfg)["reason"], autopilot.protected_reason(["a.yml"]))

    def test_a_blocker_that_closed_after_the_round_plans_again(self):
        comment = self.stopped(5, [{**ASKED, "recommended": 0}])
        comment["created_at"] = "2026-01-02T00:00:00Z"
        self.issue(7, state="closed", reason="not_planned")
        self.gh.issues[7]["closed_at"] = "2026-01-03T00:00:00Z"
        self.gh.blockers[5] = [7]
        [_, request] = self.requests(5)
        self.assertEqual((request["event"]["blocker"], request["event"]["completed"]), ("7", False))
        step = self.step(5)
        self.assertEqual(step["command"], "plan")
        self.assertIn("#7, which blocked it, closed after the stopped", step["reason"])

    def test_a_go_blocked_with_nothing_to_answer_waits_with_the_run_s_attention(self):
        self.blocked(5)
        [request] = self.requests(5)
        self.assertEqual(request["event"]["kind"], "actionFinished")
        self.assertEqual(request["policies"]["afterFailure"], "replan")
        step = self.step(5)
        self.assertEqual((step["state"], step["reason"]), ("waits", "It needs a secret."))
        self.blocked(6, attention=None)
        self.assertEqual(self.step(6)["reason"], autopilot.failed_reason("go"))

    def test_a_go_blocked_with_nothing_to_answer_plans_again_after_a_comment(self):
        self.blocked(5)
        self.gh.human_comment(5, "The secret is set now.")
        kinds = [request["event"]["kind"] for request in self.requests(5)]
        self.assertEqual(kinds, ["actionFinished", "commented"])
        step = self.step(5)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertIn(
            "collaborators commented after the stopped `/nexkit go`, so autopilot plans again",
            step["reason"],
        )

    def test_a_go_blocked_with_nothing_to_answer_plans_again_after_a_blocker_closes(self):
        comment = self.blocked(5)
        comment["created_at"] = "2026-01-02T00:00:00Z"
        self.issue(7, state="closed", reason="completed")
        self.gh.issues[7]["closed_at"] = "2026-01-03T00:00:00Z"
        self.gh.blockers[5] = [7]
        step = self.step(5)
        self.assertEqual(step["command"], "plan")
        self.assertIn("#7, which blocked it, closed after the stopped", step["reason"])

    def test_a_blocker_that_closed_before_the_round_leaves_it_waiting(self):
        comment = self.blocked(5)
        comment["created_at"] = "2026-01-04T00:00:00Z"
        self.issue(7, state="closed", reason="completed")
        self.gh.issues[7]["closed_at"] = "2026-01-03T00:00:00Z"
        self.gh.blockers[5] = [7]
        step = self.step(5)
        self.assertEqual((step["state"], step["reason"]), ("waits", "It needs a secret."))

    def test_a_go_that_failed_on_an_error_is_sent_as_an_agent_error(self):
        self.failed(5)
        [request] = self.requests(5)
        event = request["event"]
        self.assertEqual(event["kind"], "actionFinished")
        self.assertEqual(request["state"]["state"], "working")
        self.assertEqual(event["result"]["error"], "The agent crashed.")
        self.assertIsNone(event["result"]["output"])
        step = self.step(5)
        self.assertEqual((step["state"], step["reason"]), ("waits", "The agent crashed."))
        self.failed(6, attention=None)
        self.assertEqual(self.step(6)["reason"], autopilot.failed_reason("go"))

    def test_a_go_that_failed_on_an_error_plans_again_after_a_comment_or_a_blocker_closes(self):
        self.failed(5)
        self.gh.human_comment(5, "The secret is set now.")
        kinds = [request["event"]["kind"] for request in self.requests(5)]
        self.assertEqual(kinds, ["actionFinished", "commented"])
        step = self.step(5)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertIn(
            "collaborators commented after the stopped `/nexkit go`, so autopilot plans again",
            step["reason"],
        )
        comment = self.failed(6)
        comment["created_at"] = "2026-01-02T00:00:00Z"
        self.issue(7, state="closed", reason="completed")
        self.gh.issues[7]["closed_at"] = "2026-01-03T00:00:00Z"
        self.gh.blockers[6] = [7]
        step = self.step(6)
        self.assertEqual(step["command"], "plan")
        self.assertIn("#7, which blocked it, closed after the stopped", step["reason"])

    def imported(self, number, ends, cfg=None, **run):
        """A plan, a `/nexkit go` that stopped with the marker `run`, and the state that the
        import stores for it, which `ends` adds to before the item's first event: a run that
        started before the upgrade, or an issue that no run of the group has seen."""
        self.issue(number)
        self.plan(number)
        ends()
        self.runs += 1
        url = f"https://github.com/acme/app/actions/runs/{self.runs}"
        self.gh.comment(
            number, render_run({"url": url, "command": "go", "status": "failure", **run})
        )
        host.store_import(self.gh, cfg or self.cfg, number)
        return item_state.read(self.gh, number)[1]

    def spy(self):
        """The kinds of event that `step` gets, in a list, and the spy that fills it."""
        sent, real = [], host.step

        def spy(state, event, found_policies):
            sent.append(event["kind"])
            self.events.append(event)
            return real(state, event, found_policies)

        self.events = []
        return sent, spy

    def test_the_import_of_a_stopped_round_holds_its_result_as_seen(self):
        stored = self.imported(5, lambda: None, outcome="agent_blocked", attention="Secret.")
        self.assertEqual(stored["recentEvents"], [f"run-{self.runs}"])
        stored = self.imported(6, lambda: None, outcome="agent_error", attention="Crashed.")
        self.assertEqual(stored["recentEvents"], [f"run-{self.runs}"])

    def test_a_collaborator_s_comment_that_take_stored_after_a_failed_round_starts_the_plan(self):
        stored = self.imported(5, lambda: None, outcome="agent_error", attention="Crashed.")
        self.assertEqual(
            (stored["state"], stored["attention"]["reason"]), ("failed", "roundFailed")
        )
        comment = self.gh.human_comment(5, "The secret is set now.")
        self.assertEqual([e["kind"] for e, _ in host.take(self.gh, self.cfg, 5)], ["commented"])
        taken = item_state.read(self.gh, 5)[1]
        self.assertEqual(runs_of(taken), ["plan"])
        self.assertIn(f"comment-{comment['id']}", taken["recentEvents"])
        sent, spy = self.spy()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5, write=True)
        # The comment is stored: the engine hears nothing, and the step is the plan it holds.
        self.assertEqual(sent, [])
        self.assertEqual(item_state.read(self.gh, 5)[1], taken)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertIn("collaborators commented after the stopped `/nexkit go`", step["reason"])

    def test_an_outsider_s_comment_after_a_failed_round_leaves_it_failed(self):
        self.imported(5, lambda: None, outcome="agent_error", attention="Crashed.")
        self.gh.human_comment(5, "Plan it again.", login="eve", association="NONE")
        self.gh.human_comment(5, "/nexkit plan", login="bob")
        sent, spy = self.spy()
        with mock.patch.object(host, "step", spy):
            self.assertEqual(host.take(self.gh, self.cfg, 5), [])
        # Each is the engine's to ignore, since neither is a collaborator's remark.
        self.assertEqual([e["by"]["role"] for e in self.events], ["outside", "outside"])
        taken = item_state.read(self.gh, 5)[1]
        self.assertEqual((taken["state"], taken["pending"]), ("failed", []))
        sent, spy = self.spy()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5, write=True)
        self.assertEqual(sent, [])
        self.assertEqual((step["state"], step["reason"]), ("waits", "Crashed."))

    def test_a_blocked_round_asked_again_is_answered_again_after_a_comment_that_take_stored(self):
        cfg = pilot(answer="recommended")
        first = questions([ASKED])

        def answered():
            self.run_comment(5, "go", status="failure", outcome="agent_blocked", questions=first)
            self.answered(5)

        stored = self.imported(
            5, answered, cfg, outcome="agent_blocked", questions=first, attention="Answer them."
        )
        self.assertEqual(stored["attention"]["reason"], "askedAgain")
        self.gh.human_comment(5, "Keep it, the other one changed.")
        host.take(self.gh, cfg, 5)
        taken = item_state.read(self.gh, 5)[1]
        self.assertEqual(runs_of(taken), ["implement"])
        sent, spy = self.spy()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5, cfg, write=True)
        self.assertEqual(sent, [])
        self.assertEqual((step["state"], step["command"], step["target"]), ("start", "go", 5))
        self.assertEqual(step["answers"], [{"question": ASKED["question"], "answer": "Keep it"}])

    def test_an_imported_round_that_autopilot_s_answers_restart_is_started_by_a_request(self):
        cfg = pilot(answer="recommended")
        stored = self.imported(
            5, lambda: None, cfg, outcome="agent_blocked", questions=questions([ASKED])
        )
        # The import leaves no run pending for the answers' round, which only a request starts.
        self.assertEqual((stored["state"], stored["pending"]), ("working", []))
        sent, spy = self.spy()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5, cfg, write=True)
        self.assertEqual(sent, ["requested"])
        self.assertEqual((step["state"], step["command"], step["target"]), ("start", "go", 5))
        self.assertEqual(step["answers"], [{"question": ASKED["question"], "answer": "Keep it"}])
        doc = item_state.read(self.gh, 5)[1]
        self.assertEqual(runs_of(doc), ["implement"])
        self.assertEqual(doc["revision"], stored["revision"] + 1)
        # The next decision carries out what the state holds and sends nothing.
        sent.clear()
        with mock.patch.object(host, "step", spy):
            self.assertEqual(self.after_go(5, cfg, write=True)["command"], "go")
        self.assertEqual(sent, [])

    def test_a_writer_s_closing_answer_names_the_questions_that_the_stored_state_holds(self):
        stored = self.imported(
            5,
            lambda: None,
            outcome="agent_blocked",
            questions=questions([ASKED]),
            attention="Answer them.",
        )
        self.gh.human_comment(5, "Close the issue")
        sent, spy = self.spy()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5)
        self.assertEqual(sent, ["answered"])
        self.assertEqual((step["state"], step["by"]), ("close", "alice"))
        run = [c for c in self.gh.comments(5) if "actions/runs" in c["body"]][-1]
        self.assertEqual(stored["questions"]["ref"], str(run["id"]))

    def test_a_writer_s_closing_answer_that_take_stored_as_a_comment_is_still_an_answer(self):
        self.stopped(5, [ASKED])
        self.gh.human_comment(5, "Close the issue", at=LATER)
        # The run of the item's group takes the comment first, as a plain comment of the state.
        host.take(self.gh, self.cfg, 5)
        sent, spy = self.spy()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5, write=True)
        self.assertEqual(sent, ["actionFinished", "answered"])
        self.assertEqual((step["state"], step["by"]), ("close", "alice"))
        # What the state holds is carried out again and sends nothing.
        sent.clear()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5, write=True)
        self.assertEqual((sent, step["state"]), ([], "close"))

    def test_a_writer_s_closing_answer_that_take_stored_on_stored_questions_is_still_an_answer(
        self,
    ):
        comment = self.stopped(5, [ASKED])
        agent = {"status": "blocked", "output": {"questions": [{**ASKED, "closes": 3}]}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(self.runs)}):
            host.round_carried_on(self.gh, self.cfg, self.gh.issues[5], comment, agent, write=True)
        self.gh.human_comment(5, "Close the issue", at=LATER)
        ref = item_state.read(self.gh, 5)[1]["questions"]["ref"]
        [(taken, _)] = host.take(self.gh, self.cfg, 5)
        # The writer's pick on the questions that the state holds is stored as an answer.
        self.assertEqual((taken["kind"], taken["asked"]), ("answered", ref))
        sent, spy = self.spy()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5, write=True)
        self.assertEqual(sent, [])
        self.assertEqual((step["state"], step["by"]), ("close", "alice"))

    def test_a_reader_s_comment_that_take_stores_on_stored_questions_stays_a_comment(self):
        comment = self.stopped(5, [ASKED])
        agent = {"status": "blocked", "output": {"questions": [{**ASKED, "closes": 3}]}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(self.runs)}):
            host.round_carried_on(self.gh, self.cfg, self.gh.issues[5], comment, agent, write=True)
        self.gh.human_comment(5, "Close the issue", login="bob", at=LATER)
        [(taken, _)] = host.take(self.gh, self.cfg, 5)
        self.assertEqual((taken["kind"], taken["by"]["role"]), ("commented", "reader"))

    def test_a_blocker_s_close_that_the_stored_state_holds_is_not_sent_again(self):
        stored = self.imported(5, lambda: None, outcome="agent_error", attention="Crashed.")
        comment = [c for c in self.gh.comments(5) if "actions/runs" in c["body"]][-1]
        comment["created_at"] = "2026-01-02T00:00:00Z"
        self.issue(7, state="closed", reason="completed")
        self.gh.issues[7]["closed_at"] = "2026-01-03T00:00:00Z"
        self.gh.blockers[5] = [7]
        sent, spy = self.spy()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5, write=True)
        self.assertEqual((sent, step["command"]), (["blockerClosed"], "plan"))
        self.assertIn("#7, which blocked it, closed after the stopped", step["reason"])
        stored = item_state.read(self.gh, 5)[1]
        self.assertIn("blocker-7", stored["recentEvents"])
        sent.clear()
        with mock.patch.object(host, "step", spy):
            step = self.after_go(5, write=True)
        self.assertEqual((sent, step["command"]), ([], "plan"))
        self.assertIn("#7, which blocked it, closed after the stopped", step["reason"])
        self.assertEqual(item_state.read(self.gh, 5)[1], stored)

    def test_a_decision_with_no_configuration_or_no_state_is_refused(self):
        self.imported(5, lambda: None, outcome="agent_error", attention="Crashed.")
        with self.assertRaises(host.Refused):
            self.after_go(5, None)
        with mock.patch.object(host, "stored", return_value=(None, None)):
            with self.assertRaises(host.Refused):
                self.after_go(5)

    def test_a_reason_the_host_has_no_words_for_is_refused_for_that_issue_alone(self):
        self.stopped(5, [ASKED])
        answer = {"actions": [{"kind": "needPerson", "reason": "accept"}], "state": {}}
        with mock.patch.object(host, "step", return_value=answer):
            self.assertIn("refused to decide", self.step(5)["reason"])

    def test_the_round_carries_on_as_the_engine_answers_it(self):
        cfg = pilot(answer="recommended")
        self.issue(5)
        self.stored(5, cfg, outcome="agent_blocked")
        decision = {"issue": 5, "pr": None, "action": "implement"}

        def carries_on(*asked, **output):
            agent = {"status": "blocked", "stage": "implement"}
            agent["output"] = {"questions": list(asked), **output}
            return autopilot.carries_on(self.gh, cfg, decision, "agent_blocked", agent)

        self.assertTrue(carries_on(ASKED))
        # A wait for a person, the closing answer included, is mentioned by `report`.
        self.assertFalse(carries_on({**ASKED, "recommended": 3}))
        replan = {**ASKED, "recommended": 2, "closes": 0, "replans": [2]}
        self.assertFalse(carries_on({**ASKED, "recommended": 3}, replan))
        self.assertFalse(carries_on({**ASKED, "recommended": 0}))
        self.assertFalse(carries_on(ASKED, protected_paths=["a.yml"]))
        # The round that autopilot's answers started asks again: only a person's comment helps.
        self.answering(6, [ASKED], cfg)
        decision = {"issue": 6, "pr": None, "action": "implement"}
        self.assertFalse(carries_on(ASKED))
        self.assertFalse(carries_on({**ASKED, "recommended": 3}))
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            self.assertFalse(
                autopilot.carries_on(self.gh, cfg, decision, "agent_blocked", {"output": {}})
            )


class RoundClosingCommandTest(HostCase):
    def setUp(self):
        super().setUp()
        self.issue(5)
        self.plan(5)
        run = {"outcome": "agent_blocked", "questions": questions([{**ASKED, "recommended": 0}])}
        self.asked = self.run_comment(5, "go", status="failure", **run)

    def decide(self, body):
        return route(self.gh, "issue_comment", comment_event(5, body), cfg=self.cfg)

    def test_a_writer_s_closing_answer_to_a_round_is_the_engine_s_to_close(self):
        # As `nexkit route` stores the first state before it routes the item's first event.
        seed(self.gh, self.cfg, 5)
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            decision = self.decide("/nexkit go 3")
        self.assertEqual(
            (decision["task"], decision["answer"]), ("close_answer", "Close the issue")
        )
        # The engine is asked only the answer that only closes the issue, and nothing is stored:
        # no `take` stored the command in this run.
        kinds = [call.args[0]["event"]["kind"] for call in answer.call_args_list]
        self.assertEqual(kinds, ["answered"])
        call = answer.call_args_list[-1]
        self.assertEqual(call.args[0]["state"]["questions"]["ref"], str(self.asked["id"]))
        self.assertEqual(item_state.read(self.gh, 5)[1]["revision"], 1)

    def test_an_open_pull_request_keeps_the_issue_for_a_person(self):
        self.gh.add_pull(6, 5)
        opened(self.gh, 6)
        seed(self.gh, self.cfg, 5)
        decision = self.decide("/nexkit plan Close the issue")
        self.assertEqual(decision["action"], "none")
        self.assertIn("Pull request #6 is already open for this issue", decision["reason"])


class PlanWaitsForTest(HostCase):
    def test_a_plan_without_autopilot_waits_for_what_the_engine_names(self):
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            reason = needs_person(
                "planned", self.cfg, plan={"questions": [ASKED], "too_large": True}
            )
        self.assertEqual(reason, TOO_LARGE)
        [call] = answer.call_args_list
        self.assertEqual(call.args[0]["policies"], {})
        found = {"questions": [ASKED], "protected_paths": ["a.yml"]}
        self.assertEqual(
            needs_person("planned", self.cfg, plan=found), autopilot.protected_reason(["a.yml"])
        )
        with mock.patch.object(host, "plan_waits_for", return_value="approval"):
            self.assertIsNone(needs_person("planned", self.cfg, plan={"questions": [ASKED]}))

    def test_a_plan_that_finds_no_change_or_asks_blank_questions_names_a_person_s_step(self):
        found = {"questions": [], "no_change": "The base branch has it."}
        self.assertEqual(needs_person("planned", self.cfg, plan=found), NO_CHANGE_PLAN)
        # Questions that name no question still ask, as NexKit 6 reads them.
        reason = needs_person("planned", self.cfg, plan={"questions": [{"question": ""}]})
        self.assertIn("/nexkit plan <answers>", reason)


class AfterRoundTest(ReportCase):
    """What follows a round's checks and review on a NexKit pull request is the engine's
    answer, which `report` carries out in NexKit 6's words."""

    def test_the_engine_hears_the_checks_then_the_review(self):
        self.candidate(checks_pass=False)
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            outcome = self.run_report()
        self.assertEqual(outcome["outcome"], "auto_fix")
        first, second = [call.args[0] for call in answer.call_args_list]
        self.assertEqual(first["state"]["state"], "checking")
        self.assertEqual(first["event"]["result"]["checks"][0]["passed"], False)
        self.assertEqual(second["event"]["result"]["output"]["verdict"], "approve")
        self.assertEqual(second["policies"]["fixRounds"], 2)
        self.assertEqual(self.gh.dispatches[-1]["inputs"]["command"], "fix")

    def test_each_end_the_engine_answers_is_carried_out(self):
        for ends, expected in (
            ("needs_human", "needs_human"),
            ("held_fix", "held"),
            ("auto_fix", "auto_fix"),
            ("ready", "ready"),
        ):
            with self.subTest(ends=ends):
                self.candidate(checks_pass=False)
                with mock.patch.object(host, "after_round", return_value=ends):
                    self.assertEqual(self.run_report()["outcome"], expected)

    def test_a_blocking_finding_of_a_review_that_approves_starts_no_fix_round(self):
        finding = {"severity": "blocking", "file": "a.py", "line": 3, "body": "Wrong."}
        self.candidate(review=review_result(findings=[finding]))
        self.assertEqual(self.run_report()["outcome"], "ready")

    def test_a_round_whose_next_step_the_engine_refuses_waits_for_a_person(self):
        self.candidate(checks_pass=False)
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            outcome = self.run_report()
        self.assertEqual(outcome["outcome"], "needs_human")
        self.assertIn("refused to decide", outcome["attention"])
        self.assertNotIn("no automatic fix rounds remain", outcome["summary"])
        self.assertEqual(self.gh.dispatches, [])


class StoppedTest(HostCase):
    """What follows a fix round that stopped as blocked, and a round that ran out of time, is
    the engine's answer, in NexKit 6's words."""

    def fix_stopped(self, number, pull, asked=(), **fields):
        """A plan, a pull request, and its fix round that stopped with the questions `asked`."""
        self.issue(number)
        self.plan(number)
        self.gh.add_pull(pull, number, head_sha="c" * 40)
        self.run_comment(number, "go", outcome="pr_opened")
        found = questions([dict(item) for item in asked])
        row = {"round": 2, "trigger": "fix", "url": "https://github.com/acme/app/actions/runs/9"}
        row["head"] = "c" * 40
        row.update(status="failure", action="fix", outcome="agent_blocked", questions=found)
        self.gh.comment(pull, render_run({**row, **fields}, "Stopped."))

    def test_the_engine_answers_a_fix_round_on_its_change(self):
        cfg = pilot(answer="recommended")
        self.fix_stopped(5, 6, [{**ASKED, "closes": 0}])
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            step = self.step(5, cfg)
        # The import of the item, which places this round too, asks the engine first.
        request = answer.call_args_list[-1].args[0]
        self.assertEqual(
            (request["state"]["state"], request["state"]["change"]["id"]), ("fixing", "6")
        )
        self.assertEqual((step["state"], step["command"], step["target"]), ("start", "fix", 6))
        self.assertEqual(step["answers"], [{"question": ASKED["question"], "answer": "Keep it"}])

    def test_each_wait_after_a_fix_round_is_said_in_nexkit_6_s_words(self):
        cfg = pilot(answer="recommended")
        replans = {**ASKED, "closes": 0, "recommended": 2, "replans": [2]}
        self.fix_stopped(5, 6, [replans])
        self.assertEqual(self.step(5, cfg)["reason"], autopilot.REPLAN_IN_PULL)
        self.fix_stopped(7, 8, [{**ASKED, "recommended": 3}])
        self.assertEqual(self.step(7, cfg)["reason"], autopilot.CLOSE_UNFOUNDED)
        self.fix_stopped(9, 10, [ASKED], protected_paths=["a.yml"])
        self.assertEqual(self.step(9, cfg)["reason"], autopilot.protected_reason(["a.yml"]))
        self.fix_stopped(11, 12, [{**ASKED, "recommended": 0}], attention="Answer them.")
        self.assertEqual(self.step(11, cfg)["reason"], "Answer them.")

    def test_a_round_that_ran_out_of_time_asks_once_for_a_split(self):
        self.issue(5)
        self.plan(5)
        self.run_comment(5, "go", status="failure", outcome="agent_error", timed_out=True)
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            step = self.step(5)
        # The import of the item, which places this round too, asks the engine first.
        request = asked_step(answer)[-1]
        self.assertTrue(request["event"]["result"]["timedOut"])
        self.assertEqual((step["command"], step["remember"]), ("plan", {"split_asked": True}))
        self.assertEqual(step["note"], autopilot.SPLIT_NOTE)
        real, stub = host.step, {"actions": [], "state": {}}
        # The import of the item, which places this round too, asks first and gets the real one.
        answers = [lambda *args: real(*args), lambda *args: stub]
        with mock.patch.object(host, "step", side_effect=lambda *a: answers.pop(0)(*a)):
            self.assertIn("refused to decide", self.step(5)["reason"])

    def test_a_part_whose_parent_asked_for_a_split_waits_for_a_person(self):
        self.issue(4)
        record = {"split_asked": True, "command": "plan", "basis": [0, 0]}
        self.gh.comment(4, "NexKit autopilot record\n\n" + autopilot.record_marker(record))
        self.issue(5, body="<!-- nexkit:part 4 -->\nPart.", user=BOT_LOGIN)
        self.gh.parents[5] = 4
        self.plan(5)
        self.run_comment(5, "go", status="failure", outcome="agent_error", timed_out=True)
        request = self.requests(5)[-1]
        self.assertIn("split", request["state"]["asked"])
        self.assertIn("after #4 asked for a split", self.step(5)["reason"])

    def test_the_round_carries_on_as_the_engine_answers_it(self):
        cfg = pilot(answer="recommended")
        self.issue(5)
        self.gh.add_pull(6, 5)
        fix = {"issue": 5, "pr": 6, "action": "fix", "head": "c" * 40}

        def carries_on(decision, outcome, **agent):
            return autopilot.carries_on(self.gh, cfg, decision, outcome, agent)

        output = {"questions": [{**ASKED, "closes": 0}]}
        self.assertTrue(carries_on(fix, "agent_blocked", status="blocked", output=output))
        replans = {"questions": [{**ASKED, "closes": 0, "recommended": 2, "replans": [2]}]}
        self.assertFalse(carries_on(fix, "agent_blocked", status="blocked", output=replans))
        closes = {"questions": [{**ASKED, "recommended": 3}]}
        self.assertFalse(carries_on(fix, "agent_blocked", status="blocked", output=closes))
        # One answer only closes the issue and another needs a new plan: a person decides.
        both = {"questions": closes["questions"] + replans["questions"]}
        self.assertFalse(carries_on(fix, "agent_blocked", status="blocked", output=both))
        self.assertTrue(carries_on(fix, "agent_error", timed_out=True))
        go = {"issue": 5, "pr": None, "action": "implement"}
        self.assertTrue(carries_on(go, "agent_error", timed_out=True))
        # After a `/nexkit go` too, a wait for a person is one `report` mentions.
        self.assertFalse(carries_on(go, "agent_blocked", status="blocked", output=both))
        self.assertFalse(carries_on(go, "agent_blocked", status="blocked", output=closes))
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            self.assertFalse(carries_on(go, "agent_error", timed_out=True))


class WaitEndTest(HostCase):
    """The end of a round's wait for a hold label or for the base branch's failure is the
    engine's answer, which the sweep carries out in NexKit 6's words: the fix round that
    waited is counted only when it starts."""

    def waiting(self, used, **fields):
        """Issue 5's pull request 6, its automatic fix rounds `used`, and a round that waits."""
        self.issue(5)
        self.plan(5)
        self.run_comment(5, "go", outcome="pr_opened")
        self.gh.add_pull(6, 5, head_sha="c" * 40)
        state = {"version": 2, "issue": 5, "auto_fixes": used, "feedback": None}
        if fields.get("ci_wait"):
            state["ci_wait"] = fields["ci_wait"]
        self.gh.comment(6, render_state(state))
        self.gh.round_comment(6, 1, "c" * 40, "failed", action="fix", kinds=["auto"], **fields)

    def test_a_held_fix_round_is_counted_only_when_it_runs(self):
        self.waiting(1, outcome="held", held_back="fix")
        [request] = self.requests(5)
        self.assertEqual(request["event"]["kind"], "released")
        state = request["state"]
        self.assertEqual((state["rounds"]["counted"], state["rounds"]["due"]), (1, True))
        step = self.step(5)
        self.assertEqual((step["state"], step["command"], step["count"]), ("start", "fix", True))
        # `fix.max_auto_rounds` lowered while it waited: a person decides, as NexKit 6 does.
        step = self.step(5, pilot_with(fix={"max_auto_rounds": 1}))
        self.assertEqual(step["state"], "waits")
        self.assertIn("it has no automatic fix rounds left", step["reason"])

    def test_a_head_pushed_while_the_label_held_it_is_reviewed(self):
        self.waiting(2, outcome="held", held_back="fix")
        self.gh.pulls[6]["head"]["sha"] = "d" * 40
        kinds = [request["event"]["kind"] for request in self.requests(5)]
        self.assertEqual(kinds, ["changeObserved", "released"])
        step = self.step(5)
        self.assertEqual((step["state"], step["command"]), ("start", "review"))

    def test_the_base_failure_wait_ends_as_the_engine_answers(self):
        wait = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}
        self.waiting(0, outcome="ci_waiting", ci_wait=wait)
        with mock.patch.object(autopilot, "ci_wait_over", return_value="#77 is closed"):
            [request] = self.requests(5)
            self.assertEqual(
                (request["event"]["kind"], request["event"]["blocker"]),
                (
                    "blockerClosed",
                    "77",
                ),
            )
            step = self.step(5)
            self.assertEqual((step["state"], step["command"]), ("start", "fix"))
            step = self.step(5, pilot_with(fix={"max_auto_rounds": 0}))
            self.assertEqual(step["state"], "waits")
            self.assertIn("#77 is closed, but it has no automatic fix rounds left", step["reason"])
        # While the wait lasts, nothing ends, and the engine hears nothing.
        with mock.patch.object(autopilot, "ci_wait_over", return_value=None):
            self.assertEqual(self.requests(5), [])

    def test_the_end_of_each_wait_reads_fix_max_auto_rounds_as_it_is_then(self):
        wait = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}
        waits = {
            "a hold": {"outcome": "held", "held_back": "fix"},
            "a base failure": {"outcome": "ci_waiting", "ci_wait": wait},
        }
        for name, fields in waits.items():
            with (
                self.subTest(wait=name),
                mock.patch.object(autopilot, "ci_wait_over", return_value="#77 is closed"),
            ):
                self.setUp()
                self.waiting(2, **fields)
                # Two rounds are counted, and the limit is two: a person decides.
                step = self.step(5)
                self.assertEqual(step["state"], "waits")
                self.assertIn("no automatic fix rounds left", step["reason"])
                # Raised while it waited: the round starts, counted, with the limit read now.
                raised = pilot_with(fix={"max_auto_rounds": 3})
                step = self.step(5, raised)
                started = (step["state"], step["command"], step["count"])
                self.assertEqual(started, ("start", "fix", True))
                self.assertEqual(self.requests(5, raised)[-1]["policies"]["fixRounds"], 3)
                # Lowered again: the round waits for a person once more.
                step = self.step(5, pilot_with(fix={"max_auto_rounds": 1}))
                self.assertEqual(step["state"], "waits")
                self.assertIn("no automatic fix rounds left", step["reason"])

    def test_a_wait_whose_end_the_engine_refuses_waits_for_a_person(self):
        self.waiting(0, outcome="held", held_back="fix")
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            step = self.step(5)
        self.assertEqual(step["state"], "waits")
        self.assertIn("refused to decide", step["reason"])


def pilot_with(**settings):
    return make_config(autopilot={"enabled": True}, **settings)


class CountTest(ReportCase):
    """Which rounds count is the engine's: required checks that fail at the merge start a
    counted round or wait, and an automatic fix round that NexKit stopped gives its count
    back."""

    def test_required_checks_that_fail_at_the_merge_follow_the_engine(self):
        cfg = make_config(merge={"auto": True}, fix={"max_auto_rounds": 2})
        wait = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}
        # The rounds used are the stored `rounds.counted`.
        store_pending(self.gh, cfg, 5, 6, "c" * 40, rounds={"counted": 1})
        self.assertEqual(host.required_failed(self.gh, cfg, 5, 6, None), "auto_fix")
        self.assertEqual(host.required_failed(self.gh, cfg, 5, 6, wait), "ci_waiting")
        store_fields(self.gh, 5, rounds={"counted": 2})
        self.assertEqual(host.required_failed(self.gh, cfg, 5, 6, wait), "needs_human")

    def test_required_checks_read_the_base_failure_only_when_a_fix_round_is_left(self):
        head = "c" * 40
        cfg = make_config(merge={"auto": True}, fix={"max_auto_rounds": 2})
        # Pull request 6 has used both automatic fix rounds, pull request 7 only one.
        self.gh.add_pull(7, 5, head_sha=head)
        for pull, used, read in ((6, 2, False), (7, 1, True)):
            with self.subTest(used=used):
                state = {"version": 2, "issue": 5, "auto_fixes": used, "feedback": None}
                self.gh.comment(pull, render_state(state))
                store_pending(self.gh, cfg, 5, pull, head, rounds={"counted": used})
                target = {"issue": 5, "pr": pull, "head": head, "base": "main"}
                with (
                    mock.patch.object(autopilot, "failed_checks", return_value=[]),
                    mock.patch.object(autopilot, "ci_wait", return_value=None) as seen,
                ):
                    outcome, _ = reports._required_failed(
                        self.gh, cfg, target, {}, "Done.", ["test"], "nexkit.yml", "main"
                    )
                self.assertEqual(seen.called, read)
                self.assertEqual(outcome, "auto_fix" if read else "needs_human")

    def test_a_round_nexkit_stopped_gives_its_count_back_only_when_automatic(self):
        head, cfg = "c" * 40, make_config()
        counts = host.stopped_counts
        first = seed(self.gh, cfg, 5)[1]

        def stored(state="fixing", pending="fix", **rounds):
            """Store a round on its way: two automatic fix rounds used, the round's own pending
            run, and `rounds` over the stored ones."""
            run = {"id": "a1.1", "next": None, "kind": "runChecks", "changeVersion": head}
            runs = {None: [], "fix": [first_state.agent_run("fix", head)], "checks": [run]}
            rounds = {**first["rounds"], "counted": 2, "automatic": True, **rounds}
            rounds = {key: value for key, value in rounds.items() if value is not None}
            change = {"id": "6", "version": head}
            return restate(
                self.gh, 5, state=state, change=change, pending=runs[pending], rounds=rounds
            )

        # The host stops an automatic round that is on its way, in its agent or its checks.
        stored()
        self.assertEqual(counts(self.gh, cfg, 5, 6, head, True), 1)
        stored("checking", "checks")
        self.assertEqual(counts(self.gh, cfg, 5, 6, head, True), 1)
        # A person's round counts nothing: the stored state does not mark it as automatic.
        stored(automatic=None)
        self.assertEqual(counts(self.gh, cfg, 5, 6, head, True), 2)
        # With no pending run of the round there is nothing to stop, whatever a caller says.
        stored("ready", None)
        self.assertEqual(counts(self.gh, cfg, 5, 6, head, True), 2)
        # An agent that stopped by itself keeps its count, even for an automatic round.
        stored()
        self.assertEqual(counts(self.gh, cfg, 5, 6, head, False), 2)


class AcceptAnswerTest(ReportCase):
    """The answer of GitHub's merge is the engine's: `accept_answer` carries out what it
    answers for the merge's result, whatever the refusal's kind."""

    HEAD = "c" * 40
    AT = "2026-11-23T15:01:11Z"

    def answer(self, result, cfg=None, seed=None, **fields):
        """`accept_answer` for the refusal `result`, on a stored state that holds a pending
        `accept` of `HEAD` and, over it, the fields of `seed` (`store_pending`)."""
        cfg = cfg or make_config(merge={"auto": True}, fix={"max_auto_rounds": 2})
        refusal = result if isinstance(result, dict) else {"accepted": False, "refusal": result}
        refusal = {"text": "Refused.", **refusal}
        store_pending(self.gh, cfg, 5, 6, self.HEAD, **(seed or {}))
        return host.accept_answer(self.gh, cfg, 5, 6, refusal, at=self.AT, **fields)

    def test_a_merge_that_went_through_is_accepted(self):
        end, state = self.answer({"accepted": True, "refusal": None})
        self.assertEqual((end, state["state"]), ("accepted", "accepted"))

    def test_required_checks_that_have_not_passed_wait_and_keep_their_first_time(self):
        end, state = self.answer("waitingForChecks")
        self.assertEqual((end, state["state"]), ("waiting", "accepting"))
        self.assertEqual(state["checksSince"], {"version": self.HEAD, "at": self.AT})
        self.assertEqual([wait["for"] for wait in state["waits"]], ["requiredChecks"])
        # The first time is the stored one.
        since = "2026-11-23T10:00:00Z"
        seed = {"checksSince": {"version": self.HEAD, "at": since}}
        end, state = self.answer("waitingForChecks", seed=seed)
        self.assertEqual((end, state["checksSince"]["at"]), ("waiting", since))

    def test_a_merge_the_engine_cannot_try_again_waits_for_a_person(self):
        for kind in ("transient", "behind", "headMoved", "needsPerson"):
            with self.subTest(kind=kind):
                end, state = self.answer(kind)
                self.assertEqual((end, state["state"]), ("person", "ready"))
                self.assertEqual(state["attention"]["reason"], "acceptRefused")
                self.assertNotIn("checksSince", state)

    def test_a_branch_behind_its_base_follows_the_conflict_policy(self):
        resolve = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        person = make_config(merge={"auto": True}, conflicts={"auto_resolve": False})
        end, state = self.answer("behind", resolve)
        self.assertEqual((end, state["state"]), ("update", "accepting"))
        self.assertEqual([action["kind"] for action in state["pending"]], ["updateChange"])
        self.assertEqual(state["pending"][0]["changeVersion"], self.HEAD)
        end, state = self.answer("behind", person)
        self.assertEqual((end, state["attention"]["reason"]), ("person", "acceptRefused"))
        self.assertEqual(state["attention"]["text"], "Refused.")

    def test_updates_count_apart_from_conflict_rounds_against_the_limit(self):
        conflicts = {"auto_resolve": True, "max_rounds": 2}
        resolve = make_config(merge={"auto": True}, conflicts=conflicts)
        for used in (0, 1, 2, 3):
            with self.subTest(used=used):
                seed = {"rounds": {"updates": used, "conflicts": 0}}
                with mock.patch.object(host, "step", wraps=host.step) as step:
                    end, state = self.answer("behind", resolve, seed)
                rounds = step.call_args.args[0]["rounds"]
                self.assertEqual(rounds.get("updates", 0), used)
                self.assertEqual(rounds["conflicts"], 0)
                if used < 2:
                    self.assertEqual(end, "update")
                else:
                    reason = state["attention"]["reason"]
                    self.assertEqual((end, reason), ("person", "acceptRefused"))
        # Conflict rounds used up leave the updates their own count.
        seed = {"rounds": {"updates": 0, "conflicts": 2}}
        self.assertEqual(self.answer("behind", resolve, seed)[0], "update")

    def test_an_update_that_failed_answers_the_pending_update_and_is_a_person_s(self):
        resolve = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        store_pending(self.gh, resolve, 5, 6, self.HEAD, "updateChange", rounds={"updates": 1})
        with mock.patch.object(host, "step", wraps=host.step) as step:
            end = host.update_failed(self.gh, resolve, 5, 6, "It failed.", write=True)
        self.assertEqual(end, "person")
        state, event = step.call_args.args[:2]
        self.assertEqual(state["rounds"]["updates"], 1)
        failed = {"change": None, "refused": "It failed.", "observed": None}
        self.assertEqual(event["result"], failed)
        # The result names the pending update, by its id and its change version.
        self.assertEqual((event["action"], event["changeVersion"]), ("a2.1", self.HEAD))
        self.assertEqual(event["id"], "update-6-a2.1")
        stored = item_state.read(self.gh, 5)[1]
        self.assertEqual(stored["state"], "ready")
        self.assertNotIn("updateChange", [action["kind"] for action in stored["pending"]])
        self.assertEqual(stored["attention"]["reason"], "acceptRefused")
        store_pending(self.gh, resolve, 5, 6, self.HEAD, "updateChange")
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                host.update_failed(self.gh, resolve, 5, 6, "It failed.")

    def test_a_merge_that_was_not_accepted_fails_the_item(self):
        end, state = self.answer("notAccepted")
        self.assertEqual((end, state["state"]), ("person", "failed"))
        self.assertEqual(state["attention"]["reason"], "failed")

    def test_a_required_check_that_failed_follows_the_rounds_left(self):
        wait = {"accepted": False, "refusal": "checkFailed", "baseFailure": "77"}
        once, twice = {"rounds": {"counted": 1}}, {"rounds": {"counted": 2}}
        self.assertEqual(self.answer("checkFailed", seed=once)[0], "fix")
        self.assertEqual(self.answer(wait, seed=once)[0], "base_failure")
        end, state = self.answer("checkFailed", seed=twice)
        self.assertEqual((end, state["attention"]["reason"]), ("person", "roundsUsedUp"))

    def test_a_conflict_follows_the_conflict_policy(self):
        resolve = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        person = make_config(merge={"auto": True}, conflicts={"auto_resolve": False})
        self.assertEqual(self.answer("conflict", resolve)[0], "fix")
        end, state = self.answer("conflict", person)
        self.assertEqual((end, state["attention"]["reason"]), ("person", "acceptRefused"))

    def test_a_conflict_counts_the_conflict_rounds_used_against_the_limit(self):
        conflicts = {"auto_resolve": True, "max_rounds": 2}
        resolve = make_config(merge={"auto": True}, conflicts=conflicts)
        for used in (0, 1, 2, 3):
            with self.subTest(used=used):
                seed = {"rounds": {"conflicts": used}}
                with mock.patch.object(host, "step", wraps=host.step) as step:
                    end, state = self.answer("conflict", resolve, seed)
                self.assertEqual(step.call_args.args[0]["rounds"]["conflicts"], used)
                if used < 2:
                    self.assertEqual(end, "fix")
                else:
                    reason = state["attention"]["reason"]
                    self.assertEqual((end, reason), ("person", "acceptRefused"))

    def test_the_events_before_the_answer_are_heard_first_at_the_given_time(self):
        seen = {"changeId": "6", "version": self.HEAD, "conflicts": False}
        seen.update(merged=False, empty=False)
        event = host._event("changeObserved", 5, "2026-11-23T14:00:00Z", "head-6", **seen)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            end, _ = self.answer("waitingForChecks", before=[event])
        self.assertEqual(end, "waiting")
        sent = [call.args[1] for call in step.call_args_list]
        self.assertEqual([event["kind"] for event in sent], ["changeObserved", "actionFinished"])
        self.assertEqual({event["at"] for event in sent}, {self.AT})

    def test_a_newer_head_observed_before_a_moved_head_is_checked(self):
        newer = "d" * 40
        end, state = self.answer("headMoved", before=[host.head_observed(5, 6, newer)])
        self.assertEqual((end, state["state"]), ("review", "checking"))
        self.assertEqual(state["change"]["version"], newer)
        self.assertIsNone(state["attention"])

    def test_a_moved_head_with_no_newer_one_observed_waits_for_a_person(self):
        same = host.head_observed(5, 6, self.HEAD)
        for before in ([], [same]):
            with self.subTest(before=len(before)):
                end, state = self.answer("headMoved", before=before)
                self.assertEqual((end, state["state"]), ("person", "ready"))
                self.assertEqual(state["attention"]["reason"], "acceptRefused")

    def test_an_answer_the_host_cannot_carry_out_is_refused(self):
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                self.answer("waitingForChecks")
        answer = {"state": {**engine_state(), "state": "failed", "waits": []}, "actions": []}
        with mock.patch.object(host, "step", return_value=answer):
            with self.assertRaises(host.Refused):
                self.answer("waitingForChecks")


def engine_state():
    return host._pull_doc(None, 5, 6, "c" * 40, "accepting", 0)


class ApprovedTest(TestCase):
    """A writer's approval is the engine's to answer: `host.approved` gives a merge, an
    acceptance of the items a person decides, or nothing, from the round's outcome and
    `merge.auto`, on the item's stored state."""

    HEAD = "c" * 40
    OTHER = "d" * 40

    def start(self, outcome, auto, decide=False):
        """Item 5 with the round of pull request 6 that ended as `outcome` posted and the item's
        state stored, as the item's group stores it before the approval: (gh, cfg, run)."""
        cfg = make_config(
            merge={"auto": auto}, review={"checklist": [{"text": "Rule 1", "person": True}]}
        )
        run = {"outcome": outcome, "head": self.HEAD, "status": "success"}
        row = {
            "round": 1,
            "trigger": "implement (autopilot)",
            "checks": "passed",
            "verdict": "approve",
            "url": "https://github.com/acme/app/actions/runs/3",
            **run,
        }
        if decide:
            row["decide"] = ["Rule 1"]
            run["decide"] = [1]
        gh = FakeGitHub()
        gh.add_issue(5)
        gh.add_pull(6, 5, head_sha=self.HEAD)
        gh.comment(6, render_run(row, "Done."))
        seed(gh, cfg, 5)
        return gh, cfg, run

    def answer(self, outcome, auto, commit=None, decide=False, **options):
        gh, cfg, run = self.start(outcome, auto, decide)
        return host.approved(gh, cfg, 5, 6, run, commit, "olivia", **options)

    def test_an_approval_after_a_refusal_or_a_reopen_merges_only_with_merge_auto(self):
        for outcome in ("merge_refused", autopilot.PULL_CLOSED):
            for commit in (None, self.HEAD, self.OTHER):
                with self.subTest(outcome=outcome, commit=commit):
                    self.assertEqual(self.answer(outcome, True, commit), "merge")
                    self.assertEqual(self.answer(outcome, False, commit), "none")

    def test_an_approval_of_a_ready_round_merges_only_on_its_head_and_with_merge_auto(self):
        self.assertEqual(self.answer("ready", True), "merge")
        self.assertEqual(self.answer("ready", True, self.HEAD), "merge")
        self.assertEqual(self.answer("ready", True, self.OTHER), "none")
        self.assertEqual(self.answer("ready", False, self.HEAD), "none")

    def test_an_approval_of_the_items_a_person_decides_accepts_them_on_the_head_only(self):
        self.assertEqual(self.answer("ready", True, self.HEAD, decide=True), "merge")
        self.assertEqual(self.answer("ready", False, self.HEAD, decide=True), "accept")
        self.assertEqual(self.answer("ready", False, None, decide=True), "accept")
        self.assertEqual(self.answer("ready", False, self.OTHER, decide=True), "none")
        self.assertEqual(self.answer("ready", True, self.OTHER, decide=True), "none")

    def test_an_engine_that_refuses_gives_no_answer(self):
        with mock.patch.object(engine, "answer", return_value={"error": "no"}):
            with self.assertRaises(host.Refused):
                self.answer("merge_refused", True)

    def test_the_engine_gets_the_stored_state_and_the_approval_stores_its_answer(self):
        gh, cfg, run = self.start("ready", True)
        before = item_state.read(gh, 5)[1]
        with mock.patch.object(engine, "answer", wraps=ANSWER) as answer:
            found = host.approved(gh, cfg, 5, 6, run, None, "olivia", write=True, review=9000)
        self.assertEqual(found, "merge")
        [request] = asked_step(answer)
        self.assertEqual(request["state"], before)
        self.assertEqual(request["event"]["id"], "review-9000")
        self.assertEqual(request["event"]["kind"], "changeApproved")
        stored = item_state.read(gh, 5)[1]
        self.assertEqual(stored["revision"], 2)
        self.assertIn("review-9000", stored["recentEvents"])
        self.assertEqual([action["kind"] for action in stored["pending"]], ["accept"])

    def test_an_approval_with_no_review_is_named_after_the_pull_request_and_its_version(self):
        gh, cfg, run = self.start("ready", True)
        with mock.patch.object(engine, "answer", wraps=ANSWER) as answer:
            host.approved(gh, cfg, 5, 6, run, self.HEAD, "olivia")
        [request] = asked_step(answer)
        self.assertEqual(request["event"]["id"], f"approval-6-{self.HEAD}")
        self.assertEqual(item_state.read(gh, 5)[1]["revision"], 1)

    def test_an_approval_that_stored_state_took_carries_out_its_answer_without_the_engine(self):
        gh, cfg, run = self.start("ready", True)
        host.approved(gh, cfg, 5, 6, run, None, "olivia", write=True, review=9000)
        with mock.patch.object(engine, "answer", wraps=ANSWER) as answer:
            found = host.approved(gh, cfg, 5, 6, run, None, "olivia", write=True, review=9000)
        self.assertEqual(found, "merge")
        self.assertEqual(asked_step(answer), [])
        self.assertEqual(item_state.read(gh, 5)[1]["revision"], 2)

    def test_the_accept_that_stored_state_took_accepts_the_items_a_person_decides(self):
        gh, cfg, run = self.start("ready", False, decide=True)
        self.assertEqual(
            host.approved(gh, cfg, 5, 6, run, None, "olivia", write=True, review=9000), "accept"
        )
        stored = item_state.read(gh, 5)[1]
        self.assertEqual(stored["attention"]["reason"], "accept")
        with mock.patch.object(engine, "answer", wraps=ANSWER) as answer:
            found = host.approved(gh, cfg, 5, 6, run, None, "olivia", write=True, review=9000)
            # The items are not a person's to decide in a round that has none.
            plain = {**run, "decide": None}
            plain = host.approved(gh, cfg, 5, 6, plain, None, "olivia", review=9000)
        self.assertEqual((found, plain), ("accept", "none"))
        self.assertEqual(asked_step(answer), [])

    def test_an_approval_that_stored_state_took_with_nothing_left_to_carry_out_is_none(self):
        gh, cfg, run = self.start("ready", True)
        doc = item_state.read(gh, 5)[1]
        restate(gh, 5, recentEvents=[*doc["recentEvents"], "review-9000"])
        with mock.patch.object(engine, "answer", wraps=ANSWER) as answer:
            found = host.approved(gh, cfg, 5, 6, run, None, "olivia", write=True, review=9000)
        self.assertEqual(found, "none")
        self.assertEqual(asked_step(answer), [])

    def test_an_approval_with_no_configuration_or_no_state_is_refused(self):
        gh, cfg, run = self.start("ready", True)
        with mock.patch.object(engine, "answer", wraps=ANSWER) as answer:
            with self.assertRaises(host.Refused):
                host.approved(gh, None, 5, 6, run, None, "olivia", review=9000)
            with mock.patch.object(host, "stored", return_value=(None, None)):
                with self.assertRaises(host.Refused):
                    host.approved(gh, cfg, 5, 6, run, None, "olivia", review=9000)
        self.assertEqual(asked_step(answer), [])


class MergeDecideTest(ReportCase):
    """A merge decision that runs in the item's group stores the engine's answer at the next
    revision, and one that does not only previews it (`host.decide`)."""

    HEAD = "c" * 40
    MOVED = "d" * 40
    AT = "2026-11-23T15:01:11Z"
    CFG = make_config(
        merge={"auto": True}, fix={"max_auto_rounds": 2}, conflicts={"auto_resolve": True}
    )
    WAITS = [{"id": "w1.1", "for": "requiredChecks", "until": None, "on": None}]

    def setUp(self):
        super().setUp()
        seed(self.gh, self.CFG, 5)

    def read(self):
        return item_state.read(self.gh, 5)[1]

    def refused(self, kind):
        return {"accepted": False, "refusal": kind, "text": "Refused."}

    def spied(self):
        """A stand-in for `host.step` that calls it and keeps its requests."""
        seen, real = [], host.step

        def spy(state, event, found_policies):
            found = real(state, event, found_policies)
            seen.append((state, event, found))
            return found

        return seen, spy

    def store(self, kind="accept", **fields):
        """The state of item 5 as the engine's answer leaves it before a merge's result: its
        pull request 6 on `HEAD`, with a `kind` action pending (`store_pending`)."""
        return store_pending(self.gh, self.CFG, 5, 6, self.HEAD, kind, **fields)

    def test_a_branch_behind_its_base_stores_its_update_and_the_updates_used(self):
        base = self.store(rounds={"updates": 1})["revision"]
        refusal = self.refused("behind")
        end, state = host.accept_answer(self.gh, self.CFG, 5, 6, refusal, write=True)
        self.assertEqual(end, "update")
        stored = self.read()
        self.assertEqual(stored, state)
        self.assertEqual(stored["revision"], base + 1)
        self.assertEqual([action["kind"] for action in stored["pending"]], ["updateChange"])
        # The update counts once it is done, so the stored count is what was used before it.
        self.assertEqual(stored["rounds"]["updates"], 1)

    def test_a_branch_whose_updates_are_used_up_stores_the_wait_for_a_person(self):
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True, "max_rounds": 2})
        self.store(rounds={"updates": 2})
        end, _ = host.accept_answer(self.gh, cfg, 5, 6, self.refused("behind"), write=True)
        self.assertEqual(end, "person")
        stored = self.read()
        self.assertEqual(
            (stored["state"], stored["attention"]["reason"]), ("ready", "acceptRefused")
        )
        self.assertEqual(stored["rounds"]["updates"], 2)

    def test_a_conflict_stores_the_conflict_rounds_the_engine_counts(self):
        base = self.store(rounds={"conflicts": 1})["revision"]
        end, state = host.accept_answer(
            self.gh, self.CFG, 5, 6, self.refused("conflict"), write=True
        )
        self.assertEqual(end, "fix")
        stored = self.read()
        self.assertEqual(stored, state)
        self.assertEqual(stored["revision"], base + 1)
        self.assertEqual(stored["rounds"]["conflicts"], 2)
        self.assertEqual([action["stage"] for action in stored["pending"]], ["fix"])

    def test_a_conflict_with_no_round_left_stores_the_wait_for_a_person(self):
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True, "max_rounds": 1})
        self.store(rounds={"conflicts": 1})
        end, _ = host.accept_answer(self.gh, cfg, 5, 6, self.refused("conflict"), write=True)
        self.assertEqual(end, "person")
        stored = self.read()
        self.assertEqual(
            (stored["state"], stored["attention"]["reason"]), ("ready", "acceptRefused")
        )
        self.assertEqual(stored["rounds"]["conflicts"], 1)

    def test_conflict_rounds_at_the_limit_hand_the_merge_to_a_person(self):
        cfg = make_config(merge={"auto": True}, conflicts={"auto_resolve": True, "max_rounds": 2})
        self.store(rounds={"conflicts": 2})
        end, state = host.accept_answer(self.gh, cfg, 5, 6, self.refused("conflict"), write=True)
        self.assertEqual((end, state["state"]), ("person", "ready"))
        self.assertEqual(state["attention"]["reason"], "acceptRefused")
        self.assertEqual(self.read()["rounds"]["conflicts"], 2)

    def test_the_result_names_the_pending_accept_and_a_second_refusal_is_its_own_answer(self):
        cfg = make_config(merge={"auto": True})
        self.store(action_id="a2.1")
        seen, spy = self.spied()
        waiting = self.refused("waitingForChecks")
        with mock.patch.object(host, "step", spy):
            first = host.accept_answer(self.gh, cfg, 5, 6, waiting, at=self.AT, write=True)
            since = self.read()["checksSince"]
            # The sweep's tick asks for another `accept` of the same commit, which GitHub
            # refuses again: its result is not a repeat of the first.
            self.store(action_id="a3.1", checksSince=since)
            second = host.accept_answer(self.gh, cfg, 5, 6, waiting, at=self.AT, write=True)
        self.assertEqual((first[0], second[0]), ("waiting", "waiting"))
        first_event, second_event = [event for _, event, _ in seen]
        self.assertEqual((first_event["action"], second_event["action"]), ("a2.1", "a3.1"))
        self.assertEqual(
            (first_event["id"], second_event["id"]), ("accept-6-a2.1", "accept-6-a3.1")
        )
        self.assertEqual(first_event["changeVersion"], self.HEAD)
        self.assertEqual(self.read()["checksSince"], since)

    def test_a_result_with_no_pending_accept_is_refused_and_stores_nothing(self):
        base = self.store(kind="updateChange")
        before = copy.deepcopy(self.gh.comments(5))
        for answer in (
            lambda: host.accept_answer(
                self.gh, self.CFG, 5, 6, self.refused("conflict"), write=True
            ),
            lambda: host.required_failed(self.gh, self.CFG, 5, 6, None, write=True),
        ):
            with self.assertRaises(host.Refused) as refused:
                answer()
            self.assertIn("no pending accept", str(refused.exception))
        self.assertEqual(self.gh.comments(5), before)
        self.assertEqual(self.read(), base)
        # An item with no state comment has none either, and an update needs its own action.
        self.store()
        with self.assertRaises(host.Refused) as refused:
            host.update_failed(self.gh, self.CFG, 5, 6, "It failed.", write=True)
        self.assertIn("no pending updateChange", str(refused.exception))

    def test_a_head_that_moved_is_stored_before_the_merge_s_result_is_decided_on_it(self):
        base = self.store()["revision"]
        before = [host.head_observed(5, 6, self.MOVED)]
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            end, state = host.accept_answer(
                self.gh,
                self.CFG,
                5,
                6,
                self.refused("headMoved"),
                before=before,
                at=self.AT,
                write=True,
            )
        self.assertEqual(end, "review")
        # The observation comes first and the result is decided on the state after it, in one
        # row, which is stored as a single write. The result names the `accept` that was
        # pending before the observation cancelled it, which the engine ignores.
        [(first, observed, found), (second, result, _)] = seen
        self.assertEqual(first["revision"], base)
        self.assertEqual(observed["kind"], "changeObserved")
        self.assertEqual(second, found["state"])
        self.assertEqual((result["kind"], result["action"]), ("actionFinished", "a2.1"))
        stored = self.read()
        self.assertEqual(stored["revision"], base + 1)
        self.assertEqual(stored["change"]["version"], self.MOVED)
        self.assertIn(observed["id"], stored["recentEvents"])
        self.assertEqual(state["change"]["version"], self.MOVED)

    def test_a_merge_decision_without_write_stores_nothing(self):
        cfg, now = self.CFG, datetime(2026, 11, 23, 15, 0, 0, tzinfo=UTC)
        seen = {"version": self.HEAD, "merged": False, "empty": True}
        accept = {"id": "a2.1", "next": None, "kind": "accept", "by": "engine"}
        update = {"id": "a2.2", "next": None, "kind": "updateChange"}
        both = [
            {**accept, "changeVersion": self.HEAD},
            {**update, "changeVersion": self.HEAD},
        ]
        base = self.store(pending=both, waits=self.WAITS, observed=None)["revision"]
        before = copy.deepcopy(self.gh.comments(5))
        answers = [
            lambda: host.required_failed(self.gh, cfg, 5, 6, None),
            lambda: host.accept_answer(self.gh, cfg, 5, 6, self.refused("behind")),
            lambda: host.accept_answer(
                self.gh,
                cfg,
                5,
                6,
                self.refused("headMoved"),
                before=[host.head_observed(5, 6, self.MOVED)],
            ),
            lambda: host.update_failed(self.gh, cfg, 5, 6, "It failed."),
        ]
        for answer in answers:
            answer()
        self.assertEqual(self.gh.comments(5), before)
        self.assertEqual(self.read()["revision"], base)
        # The merge waits for required checks, with nothing pending.
        base = restate(self.gh, 5, pending=[])["revision"]
        before = copy.deepcopy(self.gh.comments(5))
        host.checks_waited(self.gh, cfg, 5, 6, self.HEAD, now)
        host.refusal_cleared(self.gh, cfg, 5, 6, self.HEAD, True)
        host.change_seen(self.gh, cfg, 5, 6, seen)
        self.assertEqual(self.gh.comments(5), before)
        self.assertEqual(self.read()["revision"], base)

    def test_each_merge_decision_stores_one_answer_at_the_next_revision(self):
        cfg, now = self.CFG, datetime(2026, 11, 23, 15, 0, 0, tzinfo=UTC)
        seen = {"version": self.HEAD, "merged": False, "empty": True}
        answers = {
            "required_failed": (
                "accept",
                lambda: host.required_failed(self.gh, cfg, 5, 6, None, write=True),
            ),
            "checks_waited": (
                "waits",
                lambda: host.checks_waited(self.gh, cfg, 5, 6, self.HEAD, now, write=True),
            ),
            "update_failed": (
                "updateChange",
                lambda: host.update_failed(self.gh, cfg, 5, 6, "It failed.", write=True),
            ),
            "change_seen": (
                None,
                lambda: host.change_seen(self.gh, cfg, 5, 6, seen, write=True),
            ),
        }
        for name, (kind, answer) in answers.items():
            with self.subTest(name):
                self.gh.issue_comments[5] = [
                    c for c in self.gh.comments(5) if not c["body"].startswith("### NexKit state")
                ]
                seed(self.gh, cfg, 5)
                if kind == "waits":
                    restate(
                        self.gh, 5, state="accepting", pending=[], observed=None, waits=self.WAITS
                    )
                elif kind:
                    self.store(kind)
                base = self.read()["revision"]
                answer()
                self.assertEqual(self.read()["revision"], base + 1)

    def test_a_tick_names_the_wait_of_the_document_it_is_decided_on(self):
        self.store()
        self.assertEqual(host.wait_id(self.read(), "requiredChecks"), "w1.1")
        refusal = self.refused("waitingForChecks")
        host.accept_answer(self.gh, self.CFG, 5, 6, refusal, at=self.AT, write=True)
        waiting = self.read()
        named = host.wait_id(waiting, "requiredChecks")
        self.assertTrue(named and named != "w1.1")
        self.assertEqual(host.wait_id(waiting, "baseFailure"), "w1.1")
        event = host._event("tick", 5, self.AT, "merge-6-x", wait="w1.1")
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            host.decide(self.gh, self.CFG, 5, event, waiting, pull=6, wait="requiredChecks")
            host.decide(self.gh, self.CFG, 5, event, waiting, pull=6)
        # With `wait`, the tick names the stored document's wait, and without it, its own.
        [(_, with_wait, _), (_, without, _)] = seen
        self.assertEqual(with_wait["wait"], named)
        self.assertEqual(without["wait"], "w1.1")


class ChecksWaitedTest(ReportCase):
    """A sweep's tick of a merge that waits for required checks: the six hours are the
    engine's, counted from the stored `checksSince`."""

    HEAD = "c" * 40
    SINCE = datetime(2026, 11, 23, 15, 0, 0, tzinfo=UTC)

    def cfg(self, resolve=False):
        return make_config(merge={"auto": True}, conflicts={"auto_resolve": resolve})

    def waited(self, hours, **fields):
        merging(self.gh, self.cfg(), since=self.SINCE, **fields)
        return host.checks_waited(self.gh, self.cfg(), 5, 6, self.HEAD, self.SINCE + hours)

    def test_the_merge_is_tried_again_for_six_hours_and_then_a_person_looks(self):
        self.assertEqual(self.waited(timedelta(hours=1)), "merge")
        self.gh.issue_comments[5] = []
        self.assertEqual(self.waited(timedelta(hours=6)), "merge")
        self.gh.issue_comments[5] = []
        self.assertEqual(self.waited(timedelta(hours=6, seconds=1)), "waits")

    def test_a_branch_behind_its_base_keeps_its_merge_when_conflicts_are_resolved(self):
        late = self.SINCE + timedelta(hours=7)
        for resolve, expected in ((True, "merge"), (False, "waits")):
            with self.subTest(resolve=resolve):
                self.gh.issue_comments[5] = []
                cfg = self.cfg(resolve)
                stored = merging(self.gh, cfg, since=self.SINCE)
                # The stored state does not see the branch as behind.
                self.assertIsNone(stored["observed"])
                with mock.patch.object(host, "step", wraps=host.step) as step:
                    end = host.checks_waited(self.gh, cfg, 5, 6, self.HEAD, late, behind=True)
                self.assertEqual(end, expected)
                [observed, tick] = [call.args[1] for call in step.call_args_list]
                self.assertEqual((observed["kind"], observed["behind"]), ("changeObserved", True))
                self.assertEqual(tick["kind"], "tick")
                self.assertTrue(step.call_args.args[0]["observed"]["behind"] or not resolve)
        self.gh.issue_comments[5] = []
        merging(self.gh, self.cfg(True), since=self.SINCE)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            host.checks_waited(self.gh, self.cfg(True), 5, 6, self.HEAD, late)
        self.assertEqual([call.args[1]["kind"] for call in step.call_args_list], ["tick"])
        self.assertNotIn("behind", step.call_args.args[0]["observed"] or {})

    def test_the_start_of_the_wait_is_the_stored_one(self):
        # Within six hours of the stored start, whatever a round's comment says.
        self.assertEqual(self.waited(timedelta(hours=5, minutes=59)), "merge")
        self.gh.issue_comments[5] = []
        # With no start stored the merge is tried, and the engine stores none for a tick.
        merging(self.gh, self.cfg())
        now = self.SINCE + timedelta(days=2)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            end = host.checks_waited(self.gh, self.cfg(), 5, 6, self.HEAD, now)
        self.assertEqual(end, "merge")
        self.assertNotIn("checksSince", step.call_args.args[0])

    def test_a_merge_left_to_a_person_waits_on_every_later_sweep(self):
        cfg = self.cfg()
        late = self.SINCE + timedelta(hours=7)
        merging(self.gh, cfg, since=self.SINCE)
        self.assertEqual(
            host.checks_waited(self.gh, cfg, 5, 6, self.HEAD, late, write=True), "waits"
        )
        stored = item_state.read(self.gh, 5)[1]
        self.assertEqual(
            (stored["state"], stored["attention"]["reason"]), ("ready", "acceptRefused")
        )
        for sweep in (1, 2):
            later = late + timedelta(minutes=30 * sweep)
            end = host.checks_waited(self.gh, cfg, 5, 6, self.HEAD, later, write=True)
            self.assertEqual(end, "waits")
        self.assertEqual(item_state.read(self.gh, 5)[1]["state"], "ready")

    def test_a_tick_is_decided_with_no_attention_to_wait_for(self):
        # A merge whose stored state is ready for a person without `acceptRefused` is not waited
        # for: the engine's answer is refused.
        cfg = self.cfg()
        refused_merge(self.gh, cfg, attention={"reason": "accept", "text": "x", "ref": None})
        with self.assertRaises(host.Refused):
            host.checks_waited(self.gh, cfg, 5, 6, self.HEAD, self.SINCE)

    def test_each_sweep_s_tick_is_new_to_the_engine_after_a_write_between_them(self):
        cfg = self.cfg()
        merging(self.gh, cfg, since=self.SINCE)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            first = host.checks_waited(self.gh, cfg, 5, 6, self.HEAD, self.SINCE, write=True)
            # The merge GitHub refused again for checks that wait stores the wait that the next
            # sweep's tick names.
            refusal = {"accepted": False, "refusal": "waitingForChecks", "text": "Wait."}
            at = autopilot.iso(self.SINCE)
            end, _ = host.accept_answer(self.gh, cfg, 5, 6, refusal, at=at, write=True)
            second = host.checks_waited(self.gh, cfg, 5, 6, self.HEAD, self.SINCE, write=True)
        self.assertEqual((first, end, second), ("merge", "waiting", "merge"))
        ticks = [
            call.args[1]["id"] for call in step.call_args_list if call.args[1]["kind"] == "tick"
        ]
        self.assertEqual(len(ticks), 2)
        self.assertEqual(len(set(ticks)), 2)
        self.assertTrue(all(name.startswith(f"merge-6-{self.HEAD}-r") for name in ticks))

    def test_a_tick_the_engine_refuses_is_refused(self):
        merging(self.gh, self.cfg(), since=self.SINCE)
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                host.checks_waited(self.gh, self.cfg(), 5, 6, self.HEAD, self.SINCE)


class RefusalClearedTest(ReportCase):
    """A sweep's look at a merge that GitHub refused: the engine accepts it again only for a
    head that GitHub would accept now and did not refuse while it would, which the stored
    state holds as `refusedAcceptable`."""

    HEAD = "c" * 40

    def cleared(self, acceptable, refused_clean=False, auto=True):
        cfg = make_config(merge={"auto": auto})
        fields = {"refusedAcceptable": self.HEAD} if refused_clean else {}
        refused_merge(self.gh, cfg, **fields)
        return host.refusal_cleared(self.gh, cfg, 5, 6, self.HEAD, acceptable)

    def test_an_acceptable_head_is_merged_whatever_merge_auto_says(self):
        for auto in (True, False):
            with self.subTest(auto=auto):
                self.gh.issue_comments[5] = []
                self.assertEqual(self.cleared(True, auto=auto), "merge")

    def test_a_head_that_would_not_be_accepted_waits(self):
        self.assertEqual(self.cleared(False), "waits")

    def test_a_head_refused_while_acceptable_is_not_tried_again(self):
        self.assertEqual(self.cleared(True, refused_clean=True), "waits")

    def test_the_stored_state_alone_says_which_head_was_refused_while_acceptable(self):
        cfg = make_config(merge={"auto": True})
        refused_merge(self.gh, cfg, refusedAcceptable="d" * 40)
        self.assertEqual(host.refusal_cleared(self.gh, cfg, 5, 6, self.HEAD, True), "merge")

    def test_each_sweep_s_observation_is_new_to_the_engine_after_a_write_between_them(self):
        cfg = make_config(merge={"auto": True})
        refused_merge(self.gh, cfg)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            first = host.refusal_cleared(self.gh, cfg, 5, 6, self.HEAD, True, write=True)
            # GitHub refused the merge again, which leaves it to a person once more.
            text = first_state.SAYS["acceptRefused"]
            attention = {"reason": "acceptRefused", "text": text, "ref": None}
            restate(self.gh, 5, state="ready", pending=[], attention=attention)
            second = host.refusal_cleared(self.gh, cfg, 5, 6, self.HEAD, True, write=True)
        self.assertEqual((first, second), ("merge", "merge"))
        names = [call.args[1]["id"] for call in step.call_args_list]
        self.assertEqual(len(set(names)), len(names))

    def test_an_answer_the_engine_refuses_is_refused(self):
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                self.cleared(True)


class RoundEndTest(ReportCase):
    """`report` sends every round's checks and review to the engine: a person's own pull
    request, a review that paused, failed or stopped on a transient error, and a round whose
    change conflicts,
    and keeps the checks the engine finds repeated for the next automatic fix round."""

    def answers(self, **kw):
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            outcome = self.run_report(**kw)
        return outcome, [call.args[0] for call in answer.call_args_list]

    def fix(self, **kw):
        return decision("fix", pr=6, target=6, head=HEAD, **kw)

    def test_a_person_s_own_pull_request_is_a_review_only_item(self):
        self.gh.add_pull(7, 0, head_sha=HEAD, branch="feature/x")
        self.candidate(checks_pass=False)
        d = decision("review", issue=7, pr=7, target=7, head=HEAD, person=True)
        outcome, calls = self.answers(d=d, published=False)
        self.assertEqual(outcome["outcome"], "person_reviewed")
        self.assertEqual([c["state"]["itemKind"] for c in calls], ["reviewOnly"] * 2)
        self.assertEqual(self.gh.dispatches, [])

    def test_a_paused_review_waits_for_the_reset_the_host_gives_or_for_a_person(self):
        for resume, reset in ((True, "2026-10-11T04:40:00Z"), (False, None)):
            with self.subTest(resume=resume):
                self.candidate(review=LIMIT)
                cfg = make_config(usage_limit={"resume": resume})
                outcome, calls = self.answers(d=self.fix(), cfg=cfg)
                self.assertEqual(outcome["outcome"], "paused")
                self.assertEqual(calls[-1]["event"]["result"]["limit"]["reset"], reset)

    def test_a_failed_review_waits_for_a_person(self):
        self.candidate(review={"status": "error", "error": "Review crashed", "tokens": TOKENS})
        outcome, calls = self.answers(d=self.fix())
        self.assertEqual(outcome["outcome"], "review_failed")
        self.assertEqual(calls[-1]["event"]["result"]["error"], "Review crashed")
        self.assertFalse(calls[-1]["event"]["result"]["transient"])

    TRANSIENT = {"status": "error", "error": "Overloaded", "transient": True}
    WAITS = make_config(retry={"wait_minutes": [0, 15]})

    def stopped(self, retry, cfg=None, **kw):
        self.candidate(review={**self.TRANSIENT, "tokens": TOKENS})
        d = self.fix(retry=retry)
        self.answered = []

        def answer(request):
            self.answered.append(ANSWER(request))
            return self.answered[-1]

        with mock.patch.object(host.engine, "answer", side_effect=answer) as engine_answer:
            outcome = self.run_report(d=d, cfg=cfg or self.WAITS)
        return outcome, [call.args[0] for call in engine_answer.call_args_list]

    def test_a_review_that_stopped_on_a_transient_error_runs_again_as_the_engine_answers(self):
        now = datetime.now(UTC)
        outcome, calls = self.stopped(0)
        self.assertEqual(outcome["outcome"], "retry")
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["command"], "review")
        self.assertEqual(dispatch["inputs"]["auto"], "retry")
        request = calls[-1]
        self.assertEqual(request["event"]["kind"], "actionFinished")
        result = request["event"]["result"]
        self.assertTrue(result["transient"])
        self.assertEqual(result["error"], "Overloaded")
        self.assertNotIn("transient", request["state"].get("again") or {})
        self.assertEqual(request["event"]["action"], request["state"]["pending"][0]["id"])
        self.assertEqual(request["state"]["pending"][0]["stage"], "review")
        # The checks and then the review: nothing is decided by the host.
        self.assertEqual(len(calls), 2)

        self.gh.dispatches.clear()
        outcome, calls = self.stopped(1)
        self.assertEqual(calls[-1]["state"]["again"], {"transient": 1})
        self.assertEqual(outcome["outcome"], "retry_waiting")
        # What starts it later is `maintain`'s run that waits for the time, not the review.
        self.assertEqual([d["inputs"]["command"] for d in self.gh.dispatches], ["resume"])
        [wait] = [a for a in self.answered[-1]["actions"] if a["kind"] == "wait"]
        self.assertEqual(outcome["resume_at"], wait["until"])
        at = datetime.strptime(wait["until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.assertLess(abs(at - now - timedelta(minutes=15)), timedelta(minutes=1))

    def test_a_review_that_stopped_after_the_last_wait_goes_to_a_person(self):
        outcome, calls = self.stopped(2)
        self.assertEqual(calls[-1]["state"]["again"], {"transient": 2})
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertEqual(self.gh.dispatches, [])
        self.assertNotIn("resume", outcome)
        self.assertIsNotNone(outcome["attention"])

    def test_a_review_that_ended_without_a_result_is_a_transient_stop_too(self):
        self.candidate(review=self.TRANSIENT)
        (self.artifacts / "nexkit-review" / "result.json").unlink()
        with mock.patch.object(host.engine, "answer", side_effect=lambda r: ANSWER(r)) as answer:
            outcome = self.run_report(d=self.fix(), cfg=ONCE)
        self.assertEqual(outcome["outcome"], "retry")
        self.assertTrue(answer.call_args.args[0]["event"]["result"]["transient"])

    def test_a_review_the_engine_refuses_to_run_again_waits_for_a_person(self):
        self.candidate(review={**self.TRANSIENT, "tokens": TOKENS})
        with mock.patch.object(host, "_transient_answer", side_effect=host.Refused("No.")):
            outcome = self.run_report(d=self.fix(), cfg=ONCE)
        self.assertEqual(self.gh.dispatches, [])
        self.assertIn("refused", outcome["attention"])
        self.assertNotIn(outcome["outcome"], ("retry", "retry_waiting", "retry_failed"))

    def test_a_review_whose_run_again_could_not_start_waits_for_a_person(self):
        self.candidate(review={**self.TRANSIENT, "tokens": TOKENS})
        with mock.patch.object(self.gh, "dispatch", side_effect=GitHubError(500, "no")):
            outcome = self.run_report(d=self.fix(), cfg=ONCE)
        self.assertEqual(outcome["outcome"], "review_failed")
        self.assertEqual(self.gh.dispatches, [])

    def test_a_refused_review_end_asks_a_person_why(self):
        self.candidate(review=LIMIT)
        with mock.patch.object(host, "_review_end", side_effect=host.Refused("No.")):
            outcome = self.run_report(d=self.fix())
        self.assertIn("refused", outcome["attention"])

    def test_checks_that_failed_the_same_way_after_a_fix_round_are_kept_for_diagnosis(self):
        state = {
            **empty_state(5),
            "feedback": {
                "head": "b" * 40,
                "review": None,
                "checks": [{"name": "test", "passed": False, "signature": "AssertionError"}],
            },
        }
        self.gh.comment(6, render_state(state))
        self.candidate(checks_pass=False)
        self.run_report(d=self.fix(auto=True, autopilot="true"))
        _, found = read_state(self.gh.comments(6))
        self.assertEqual(found["feedback"]["repeated"], ["test"])
        self.assertIn("run_id", found["feedback"])
        # A review round's checks are no fix round's.
        self.candidate(checks_pass=False)
        self.run_report(d=decision("review", pr=6, target=6, head=HEAD))
        _, found = read_state(self.gh.comments(6))
        self.assertNotIn("repeated", found["feedback"])


class ManyChecksTest(ReportCase):
    """The checks that repeat are found however many checks a pull request runs, and a
    diagnosis round looks into the first ten, as in NexKit 6."""

    def checks(self, failing, count=40):
        return [
            {
                "name": f"c{n:03}",
                "run": f"make c{n}",
                "exit_code": 1 if n in failing else 0,
                "passed": n not in failing,
                "output": f"FAIL test_{chr(97 + n % 26)}{chr(97 + n // 26)}"
                if n in failing
                else "",
            }
            for n in range(count)
        ]

    def round_with(self, before, after, d, count=40):
        state = {**empty_state(5), "feedback": {"head": "b" * 40, "review": None}}
        state["feedback"]["checks"] = recorded(self.checks(before, count))
        self.gh.comment(6, render_state(state))
        self.write("nexkit-agent", "result.json", {"status": "done", "tokens": TOKENS})
        self.write("nexkit-checks", "checks.json", self.checks(after, count))
        self.write("nexkit-review", "result.json", review_result())
        # The stored state that the implement run left: the pull request in its checks. The
        # fix round that this round starts is stored by its report, which the dispatch carries out.
        seed(self.gh, make_config(), 5)
        self.run_report(d=d)
        return read_state(self.gh.comments(6))[1]["feedback"].get("repeated")

    def fix(self, **kw):
        return decision("fix", pr=6, target=6, head=HEAD, **kw)

    def test_checks_past_the_thirtieth_that_fail_the_same_way_are_found(self):
        repeated = self.round_with(range(40), (32, 33, 34, 35), self.fix(auto=True))
        self.assertEqual(repeated, ["c032", "c033", "c034", "c035"])
        event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": "true"}}
        found = route(self.gh, "workflow_dispatch", event, event["inputs"], cfg=make_config())
        self.assertEqual(found["diagnose"], ["c032", "c033", "c034", "c035"])

    def test_a_diagnosis_round_looks_into_the_first_ten_of_more(self):
        repeated = self.round_with(range(40), range(5, 40), self.fix(auto=True))
        self.assertEqual(repeated, [f"c{n:03}" for n in range(5, 40)])
        event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": "true"}}
        found = route(self.gh, "workflow_dispatch", event, event["inputs"], cfg=make_config())
        self.assertEqual(found["diagnose"], [f"c{n:03}" for n in range(5, 15)])

    def test_a_person_s_fix_round_s_checks_compare_too(self):
        repeated = self.round_with((3,), (3,), self.fix())
        self.assertEqual(repeated, ["c003"])

    def route_diagnoses(self):
        event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": "true"}}
        return route(self.gh, "workflow_dispatch", event, event["inputs"], cfg=make_config()).get(
            "diagnose"
        )

    def test_a_check_past_the_300th_failing_result_is_not_compared(self):
        # The state compares with the first 300 failing results, so c305 is not compared.
        repeated = self.round_with(range(320), (305,), self.fix(auto=True), count=320)
        self.assertFalse(repeated)
        self.assertFalse(self.route_diagnoses())

    def test_at_most_150_repeated_checks_are_kept(self):
        repeated = self.round_with(range(320), range(320), self.fix(auto=True), count=320)
        self.assertEqual(repeated, [f"c{n:03}" for n in range(MOST["repeated"])])
        self.assertEqual(self.route_diagnoses(), [f"c{n:03}" for n in range(10)])

    def test_the_configuration_names_the_bounds(self):
        text = (ROOT / "docs" / "checks.md").read_text(encoding="utf-8")
        text = text.split("\n## Checks\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"{MOST['failing']} failing", text)
        self.assertIn(f"{MOST['repeated']} checks", text)


class ConflictingTest(ReportCase):
    """A round whose change conflicts with its base branch, and a hold on its conflict
    round, follow the engine."""

    def conflicting(self, cfg, used=0):
        self.write("nexkit-agent", "result.json", {"status": "done", "tokens": TOKENS})
        result = {"published": True, "pr": 6, "head": HEAD, "conflicts": ["a.py"]}
        needs = {"publish": {"outputs": {"result": json.dumps(result)}}}
        for n in range(used):
            self.gh.round_comment(6, n + 1, "b" * 40, "failed", conflicts=True, base_sha="d" * 40)
        d = decision("fix", pr=6, target=6, head="b" * 40)
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            found = report(
                self.gh, d, cfg, needs, self.artifacts, workflow_ref=WORKFLOW, default_branch="main"
            )
        return found, [call.args[0] for call in answer.call_args_list]

    def test_the_engine_starts_the_conflict_round_or_leaves_it_to_a_person(self):
        cfg = make_config(conflicts={"auto_resolve": True, "max_rounds": 2})
        conflicting, gh = host.conflicting, self.gh
        # The conflict rounds used are the item's own: the round comments that its import counts.
        self.conflict_round(1)
        self.assertEqual(conflicting(gh, cfg, decision("fix", head="b"), 6, HEAD, None, 1), "round")
        self.conflict_round(2)
        self.assertEqual(
            conflicting(gh, cfg, decision("fix", head="b"), 6, HEAD, None, 2), "used_up"
        )
        self.assertEqual(conflicting(gh, cfg, decision(), 6, HEAD, "on-hold", 2), "held")

    def conflict_round(self, number):
        self.gh.round_comment(6, number, "b" * 40, "failed", conflicts=True, base_sha="d" * 40)

    def test_a_report_after_the_conflict_rounds_are_used_up_starts_none(self):
        cfg = make_config(conflicts={"auto_resolve": True, "max_rounds": 2})
        found, calls = self.conflicting(cfg, used=2)
        self.assertEqual(found["outcome"], "conflicting")
        self.assertEqual(calls[-1]["state"]["rounds"]["conflicts"], 2)
        self.assertNotIn("fix", [x["inputs"]["command"] for x in self.gh.dispatches])

    def test_a_refused_conflict_asks_a_person_why(self):
        cfg = make_config(conflicts={"auto_resolve": True})
        with mock.patch.object(host, "conflicting", side_effect=host.Refused("No.")):
            found, _ = self.conflicting(cfg)
        self.assertEqual(found["outcome"], "needs_human")
        self.assertIn("refused", found["attention"])

    def test_a_held_conflict_round_released_follows_the_engine(self):
        cfg = make_config(conflicts={"auto_resolve": True, "max_rounds": 2})
        run = {"head": HEAD, "held_back": "conflicts"}
        released, gh = host.conflicts_released, self.gh
        self.conflict_round(1)
        self.assertEqual(released(gh, cfg, 5, 6, run, HEAD, True, 1), "round")
        self.conflict_round(2)
        self.assertEqual(released(gh, cfg, 5, 6, run, HEAD, True, 2), "waits")
        self.assertEqual(released(gh, cfg, 5, 6, run, HEAD, False, 2), "review")
        # A head that moved is checked, and the round after it finds any conflict.
        self.assertEqual(released(gh, cfg, 5, 6, run, "d" * 40, True, 0), "review")
        off = make_config(conflicts={"auto_resolve": False})
        self.assertEqual(released(gh, off, 5, 6, run, HEAD, True, 0), "waits")


class DiagnosisTest(HostCase):
    """The fix round that `route` starts diagnoses what the engine names."""

    def test_only_an_automatic_round_diagnoses_the_repeated_failures(self):
        head = "c" * 40
        self.assertEqual(host.diagnosis(None, None, 5, 6, head, True, ["test"]), ["test"])
        self.assertEqual(host.diagnosis(None, None, 5, 6, head, False, ["test"]), [])
        self.assertEqual(host.diagnosis(None, None, 5, 6, head, True, []), [])
        # On the item's stored state, with its configuration, the answer is the same.
        self.issue(5)
        self.gh.add_pull(6, 5, head_sha=head)
        cfg = make_config()
        self.assertEqual(host.diagnosis(self.gh, cfg, 5, 6, head, True, ["test"]), ["test"])
        self.assertEqual(host.diagnosis(self.gh, cfg, 5, 6, head, False, ["test"]), [])

    def test_route_takes_the_diagnosis_from_the_engine(self):
        self.issue(5)
        self.gh.add_pull(6, 5, head_sha="c" * 40)
        opened(self.gh, 6, "c" * 40)
        state = {**empty_state(5), "feedback": {"head": "c" * 40, "repeated": ["test"]}}
        self.gh.comment(6, render_state(state))
        found = {}
        for auto in ("true", "conflicts", "false"):
            event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": auto}}
            with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
                found[auto] = route(
                    self.gh, "workflow_dispatch", event, event["inputs"], cfg=self.cfg
                )
            # The command is the engine's to start. Only an automatic round then asks what it
            # diagnoses: a person's round and a conflict round diagnose nothing. A conflict
            # round is first counted from the observation that the head conflicts.
            kinds = [call.args[0]["event"]["kind"] for call in answer.call_args_list]
            self.assertEqual(len(kinds), 2 if auto == "true" else 1, kinds)
            self.assertEqual(kinds[0], "changeObserved" if auto == "conflicts" else "requested")
        self.assertEqual(found["true"]["diagnose"], ["test"])
        self.assertNotIn("diagnose", found["conflicts"])
        self.assertNotIn("diagnose", found["false"])
        with mock.patch.object(host, "diagnosis", side_effect=host.Refused("No.")):
            event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": "true"}}
            self.assertNotIn(
                "diagnose",
                route(self.gh, "workflow_dispatch", event, event["inputs"], cfg=self.cfg),
            )


class RoundPublishedTest(HostCase):
    """The end of an implement or fix round reaches the stored state before its checks do
    (`host.round_published`), only while that state waits for the round's agent."""

    NEW = "e" * 40
    RED = [{"name": "test", "passed": False, "output": "AssertionError"}]
    REVIEW = ("done", {"verdict": "approve", "criteria": [], "findings": []})
    OUTPUT = {"status": "done", "summary": "Fixed.", "checks_run": []}

    def setUp(self):
        super().setUp()
        self.cfg = make_config(fix={"max_auto_rounds": 3}, conflicts={"auto_resolve": True})
        self.issue(5)
        self.gh.add_pull(6, 5, head_sha=HEAD)
        self.found = {"action": "fix", "issue": 5, "pr": 6, "target": 6, "head": HEAD}

    def read(self):
        return item_state.read(self.gh, 5)

    def publish(self, head, conflicts=False, found=None, agent=None):
        agent = {"status": "done", "output": self.OUTPUT} if agent is None else agent
        args = (self.gh, self.cfg, found or self.found, 6, head, agent, None, conflicts)
        return host.round_published(*args)

    def fail_the_checks(self):
        """The round's checks fail, which stores an automatic fix round with its agent pending."""
        args = (self.gh, self.cfg, self.found, 6, HEAD, 0, self.RED, None, self.REVIEW)
        self.assertEqual(host.round_ended(*args, write=True)[0], "auto_fix")
        doc = self.read()[1]
        self.assertEqual(doc["state"], "fixing")
        self.assertEqual(runs_of(doc), ["fix"])

    def test_a_round_that_the_import_placed_in_its_checks_sends_nothing(self):
        seed(self.gh, self.cfg, 5)
        before = self.read()[1]
        self.assertEqual(before["state"], "checking")
        self.assertEqual([a["kind"] for a in before["pending"]], ["runChecks"])
        with mock.patch.object(host, "step", side_effect=AssertionError("step was called")):
            self.assertFalse(self.publish(HEAD))
            self.assertFalse(self.publish(HEAD, conflicts=True))
        self.assertEqual(self.read()[1], before)
        self.assertEqual(self.read()[1]["revision"], 1)

    def test_a_round_without_a_checked_result_or_a_state_sends_nothing(self):
        with mock.patch.object(host, "step", side_effect=AssertionError("step was called")):
            # No state comment: the import is only in memory.
            self.assertFalse(self.publish(HEAD))
        seed(self.gh, self.cfg, 5)
        with mock.patch.object(host, "step", side_effect=AssertionError("step was called")):
            for agent in (None, {"status": "done"}, {"status": "done", "output": "text"}):
                self.assertFalse(self.publish(HEAD, agent=agent or {}))
            self.assertFalse(host.round_published(self.gh, None, self.found, 6, HEAD, {}, None, 0))
            review = {**self.found, "action": "review"}
            self.assertFalse(self.publish(HEAD, found=review))
        self.assertEqual(self.read()[1]["revision"], 1)

    def test_a_fix_round_stores_its_results_in_one_write_and_a_report_that_runs_again_none(self):
        seed(self.gh, self.cfg, 5)
        self.fail_the_checks()
        before = self.read()[1]
        self.assertTrue(self.publish(self.NEW))
        doc = self.read()[1]
        self.assertEqual(doc["revision"], before["revision"] + 1)
        self.assertEqual(doc["state"], "checking")
        self.assertEqual(doc["change"], {"id": "6", "version": self.NEW})
        self.assertEqual([a["kind"] for a in doc["pending"]], ["runChecks"])
        self.assertEqual(doc["pending"][0]["changeVersion"], self.NEW)
        self.assertEqual(doc["rounds"]["fixed"], self.NEW)
        self.assertNotIn("recheck", doc["rounds"])
        # The same report again: nothing waits for the agent, so nothing is sent.
        with mock.patch.object(host, "step", side_effect=AssertionError("step was called")):
            self.assertFalse(self.publish(self.NEW))
        self.assertEqual(self.read()[1], doc)

    def test_a_fix_round_that_changed_nothing_stores_the_head_it_checks_again(self):
        seed(self.gh, self.cfg, 5)
        self.fail_the_checks()
        self.assertTrue(self.publish(HEAD))
        rounds = self.read()[1]["rounds"]
        self.assertEqual((rounds["recheck"], rounds["fixed"]), (HEAD, HEAD))

    def test_a_conflicting_commit_stores_only_the_agent_result_and_conflicting_the_publish(self):
        seed(self.gh, self.cfg, 5)
        self.fail_the_checks()
        before = self.read()[1]
        self.assertFalse(self.publish(self.NEW, conflicts=True))
        doc = self.read()[1]
        self.assertEqual(doc["revision"], before["revision"] + 1)
        self.assertEqual([a["kind"] for a in doc["pending"]], ["publish"])
        self.assertEqual(doc["pending"][0]["stage"], "fix")
        self.assertFalse(any(name.startswith("publish-") for name in doc["recentEvents"]))
        # `conflicting` then sends the publish result, which the stored state still waits for.
        args = (self.gh, self.cfg, self.found, 6, self.NEW, None, 0)
        self.assertEqual(host.conflicting(*args, write=True), "round")
        seen = self.read()[1]["recentEvents"]
        self.assertTrue(any(name.startswith("publish-6-") for name in seen))


class DispatchedTest(HostCase):
    """NexKit's own dispatch of a command carries out the action that the stored state has
    pending, and is otherwise decided as a new request named by its run
    (`host.command`, `host.diagnosis`)."""

    HEAD = "c" * 40

    def setUp(self):
        super().setUp()
        self.cfg = make_config(fix={"max_auto_rounds": 3})
        self.issue(5)
        self.gh.add_pull(6, 5, head_sha=self.HEAD)
        # The pull request's NexKit 6 state comment repeats `test`, which a stored round that
        # diagnoses `lint` must not follow.
        state = {**empty_state(5), "feedback": {"head": self.HEAD, "repeated": ["test"]}}
        self.gh.comment(6, render_state(state))
        opened(self.gh, 6, self.HEAD)
        seed(self.gh, self.cfg, 5)

    def pend(self, action, **fields):
        """Store the state again with `action`, changed by `fields`, as its only pending
        action, at the next revision."""
        comment, doc = item_state.read(self.gh, 5)
        found = {**doc, "revision": doc["revision"] + 1, "pending": [{**action, **fields}]}
        self.assertTrue(item_state.write(self.gh, 5, found, (comment, doc)))
        return found

    def fix_round(self, **fields):
        return self.pend(first_state.agent_run("fix", self.HEAD), **{"id": "a2.1", **fields})

    def dispatch(self, command, auto="true", run="1001", on_pull=True, **options):
        number = "6" if on_pull else "5"
        inputs = {"command": command, "number": number, "note": "", "auto": auto}
        event = {"inputs": inputs, "sender": {"login": "nexkit[bot]"}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": run}):
            return route(self.gh, "workflow_dispatch", event, inputs, cfg=self.cfg, **options)

    def test_an_automatic_fix_round_diagnoses_the_checks_of_its_pending_action(self):
        stored = self.fix_round(diagnose=["lint"])
        with mock.patch.object(host, "step", side_effect=AssertionError("asked the engine")):
            found = self.dispatch("fix")
            again = self.dispatch("fix", run="1002")
        for each in (found, again):
            self.assertEqual(each["action"], "fix")
            self.assertEqual(each["diagnose"], ["lint"])
        # Nothing was stored: the dispatch carries the action out and the engine is not asked.
        self.assertEqual(item_state.read(self.gh, 5)[1], stored)

    def test_a_person_and_a_conflict_round_diagnose_nothing_from_the_pending_action(self):
        self.fix_round(diagnose=["lint"])
        for auto in ("conflicts", "false"):
            self.assertNotIn("diagnose", self.dispatch("fix", auto=auto))

    def test_diagnosis_reads_the_pending_action_alone(self):
        action = {**first_state.agent_run("fix", self.HEAD), "diagnose": ["lint"]}
        with mock.patch.object(host, "step", side_effect=AssertionError("asked the engine")):
            diagnosis = host.diagnosis(self.gh, self.cfg, 5, 6, self.HEAD, True, ["test"], action)
            self.assertEqual(diagnosis, ["lint"])
            self.assertEqual(
                host.diagnosis(self.gh, self.cfg, 5, 6, self.HEAD, False, [], action), []
            )
            empty = {**action, "diagnose": []}
            self.assertEqual(
                host.diagnosis(self.gh, self.cfg, 5, 6, self.HEAD, True, ["test"], empty), []
            )

    def test_a_pending_action_of_a_persons_command_is_not_the_automatic_rounds(self):
        # A round that a person's command stored and `route` refused leaves its action
        # pending. An automatic dispatch still starts `fix`, and diagnoses as NexKit 6 does.
        self.fix_round(by="person", diagnose=[])
        found = self.dispatch("fix")
        self.assertEqual((found["action"], found["diagnose"]), ("fix", ["test"]))

    def test_every_dispatched_command_carries_out_its_pending_action(self):
        checks = {"id": "a2.1", "next": None, "kind": "runChecks", "changeVersion": self.HEAD}
        for command, action in (
            ("fix", first_state.agent_run("fix", self.HEAD)),
            ("review", checks),
            ("go", first_state.agent_run("implement", self.HEAD)),
        ):
            with self.subTest(command):
                stored = self.pend(action, id="a2.1")
                with mock.patch.object(host, "step", side_effect=AssertionError("asked")):
                    found = self.dispatch(command, on_pull=command != "go")
                self.assertEqual(found["action"], {"go": "implement"}.get(command, command))
                self.assertEqual(item_state.read(self.gh, 5)[1], stored)

    def test_dispatches_with_nothing_pending_are_new_requests_named_by_their_run(self):
        self.gh.add_issue(7)
        seed(self.gh, self.cfg, 7)
        for run in ("1001", "1002"):
            comment, doc = item_state.read(self.gh, 7)
            # Nothing is pending, as after the plan of the run before.
            reset = {**doc, "revision": doc["revision"] + 1, "state": "new", "pending": []}
            self.assertTrue(item_state.write(self.gh, 7, reset, (comment, doc)))
            inputs = {"command": "plan", "number": "7", "note": "", "auto": "true"}
            event = {"inputs": inputs, "sender": {"login": "nexkit[bot]"}}
            with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": run}):
                found = route(self.gh, "workflow_dispatch", event, inputs, cfg=self.cfg)
            self.assertEqual(found["action"], "plan", run)
            self.assertIn(f"dispatch-{run}", item_state.read(self.gh, 7)[1]["recentEvents"])
        events = set(item_state.read(self.gh, 7)[1]["recentEvents"])
        self.assertTrue({"dispatch-1001", "dispatch-1002"} <= events)

    def test_a_dispatch_outside_actions_is_named_by_the_next_revision(self):
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "77"}):
            self.assertEqual(host._dispatch_id({"revision": 4}), "dispatch-77")
        for run in ("", "x1", "0", "-3"):
            with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": run}):
                self.assertEqual(host._dispatch_id(None), "dispatch-local-1", run)
                self.assertEqual(host._dispatch_id({"revision": 4}), "dispatch-local-5", run)

    def test_a_persons_dispatch_is_decided_as_a_new_request(self):
        # Nothing is pending: the round that opened the pull request is over.
        stored = item_state.read(self.gh, 5)[1]
        self.assertEqual((stored["state"], stored["pending"]), ("ready", []))
        with mock.patch.object(host, "step", wraps=host.step) as asked:
            found = self.dispatch("fix", auto="false")
        self.assertEqual(found["action"], "fix")
        self.assertEqual(asked.call_args_list[0].args[1]["kind"], "requested")
        self.assertEqual(asked.call_args_list[0].args[1]["id"], "dispatch-1001")
        self.assertNotIn("diagnose", found)
        self.assertGreaterEqual(item_state.read(self.gh, 5)[1]["revision"], stored["revision"])

    def test_the_imports_run_on_its_way_is_decided_as_a_new_request(self):
        self.fix_round(id=first_state.RUN_ID, diagnose=["lint"])
        self.assertIsNone(host._carried(item_state.read(self.gh, 5)[1], "fix"))
        with mock.patch.object(host, "step", wraps=host.step) as asked:
            self.dispatch("fix")
        self.assertEqual(asked.call_args_list[0].args[1]["kind"], "requested")

    def test_no_event_id_is_built_from_the_command(self):
        self.assertNotRegex(Path(host.__file__).read_text(), r'f"command-\{')


class RoundDecideTest(ReportCase):
    """The decisions about a pull request's rounds read the item's stored state document, and
    those that run in the item's group store the engine's answer at the next revision
    (`host.decide`). The counts are the stored ones, and a decision that falls back to the
    markers' document keeps them (`host.resync`)."""

    GOOD = {"verdict": "approve", "criteria": [], "findings": []}
    RED = [{"name": "test", "passed": False, "output": "AssertionError"}]
    GREEN = [{"name": "test", "passed": True}]

    def setUp(self):
        super().setUp()
        self.cfg = make_config(
            fix={"max_auto_rounds": 3},
            conflicts={"auto_resolve": True, "max_rounds": 2},
            autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}},
        )
        seed(self.gh, self.cfg, 5)

    def read(self):
        return item_state.read(self.gh, 5)

    def store(self, **rounds):
        """Store the state again at the next revision, with `rounds` changed."""
        comment, doc = self.read()
        found = {**doc, "revision": doc["revision"] + 1}
        found["rounds"] = {**doc["rounds"], **rounds}
        self.assertTrue(item_state.write(self.gh, 5, found, (comment, doc)))
        return found

    def spied(self):
        """A stand-in for `host.step` that keeps a copy of each request and answer."""
        seen, real = [], host.step

        def spy(state, event, found_policies):
            found = real(state, event, found_policies)
            seen.append((copy.deepcopy(state), event, found))
            return found

        return seen, spy

    def ended(self, **options):
        review = ("done", self.GOOD)
        d = decision("fix", pr=6, target=6, head="b" * 40)
        return host.round_ended(
            self.gh, self.cfg, d, 6, HEAD, 0, self.GREEN, None, review, **options
        )

    def test_a_rounds_checks_and_review_each_store_a_revision(self):
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            end, _ = self.ended(write=True)
        self.assertEqual(end, "ready")
        [(_, checks, _), (_, review, _)] = seen
        self.assertEqual((checks["kind"], review["kind"]), ("actionFinished",) * 2)
        _, doc = self.read()
        self.assertEqual(doc["revision"], 3)
        self.assertEqual(doc["recentEvents"][-2:], [f"checks-6-{HEAD}", f"round-review-6-{HEAD}"])
        # The review's result names the review run that the checks' answer started.
        self.assertEqual(seen[1][0]["revision"], 2)
        self.assertEqual(seen[1][2]["state"], doc)

    def test_without_write_a_rounds_end_stores_nothing(self):
        before = copy.deepcopy(self.gh.comments(5)), copy.deepcopy(self.gh.comments(6))
        self.assertEqual(self.ended()[0], "ready")
        self.assertEqual((self.gh.comments(5), self.gh.comments(6)), before)
        self.assertEqual(self.read()[1]["revision"], 1)

    def test_the_checks_result_names_the_runChecks_id_of_the_stored_outbox(self):
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            self.ended()
        # The stored state says what the markers' document says, but the id of its outbox.
        agreeing = copy.deepcopy(seen[0][0])
        agreeing["revision"] = 2
        agreeing["pending"][0]["id"] = "a7.1"
        self.assertTrue(item_state.write(self.gh, 5, agreeing, self.read()))
        seen.clear()
        with mock.patch.object(host, "step", spy):
            self.ended()
        self.assertEqual(seen[0][1]["action"], host.pending_id(self.read()[1], "runChecks"))
        self.assertEqual(seen[0][1]["action"], "a7.1")
        self.assertEqual(seen[0][0]["revision"], 2)

    def test_the_stored_count_alone_stops_the_automatic_fix_rounds(self):
        review = self.GOOD
        d = decision("fix", pr=6, target=6, head="b" * 40)

        def after(**options):
            args = (self.gh, self.cfg, d, 6, HEAD, 0, self.RED, review, False, None, None)
            return host.after_round(*args, **options)

        self.assertEqual(after(), "auto_fix")
        self.store(counted=3)
        self.assertEqual(after(), "needs_human")
        # NexKit 6's count is no floor: the stored one is current.
        self.store(counted=0)
        args = (self.gh, self.cfg, d, 6, HEAD, 3, self.RED, review, False, None, None)
        self.assertEqual(host.after_round(*args), "auto_fix")

    def test_nexkit_6_s_count_is_used_only_when_no_state_can_be_read(self):
        self.assertEqual(host._stored_count(None, 3, "counted"), 3)
        self.assertEqual(host._stored_count(None, -1, "counted"), 0)
        self.assertEqual(host._stored_count(self.read()[1], 3, "counted"), 0)

    def test_a_decision_that_falls_back_to_the_markers_keeps_the_stored_counts(self):
        self.store(conflicts=1, counted=2)
        # The markers' document of a round's end holds no conflict round: it differs from the
        # stored state, so the decision falls back to it.
        self.assertEqual(self.ended(write=True)[0], "ready")
        rounds = self.read()[1]["rounds"]
        self.assertEqual((rounds["conflicts"], rounds["counted"]), (1, 2))

    def test_the_fallback_raises_the_counts_that_the_markers_document_lacks(self):
        state = self.store(conflicts=1, counted=2, updates=3)
        event = {"id": "x", "kind": "stopped"}
        for built, want in (
            ({"counted": 0, "conflicts": 0}, (2, 1, 3)),
            ({"counted": 5, "conflicts": 0, "updates": 1}, (5, 1, 3)),
        ):
            found = host.resync(state, {"revision": 1, "recentEvents": [], "rounds": built}, event)
            rounds = found["rounds"]
            self.assertEqual((rounds["counted"], rounds["conflicts"], rounds["updates"]), want)
        # An optional count that neither document holds stays unwritten.
        bare = {"revision": 1, "recentEvents": [], "rounds": {"counted": 0, "conflicts": 0}}
        plain = copy.deepcopy(state)
        del plain["rounds"]["updates"]
        found = host.resync(plain, bare, event)
        self.assertNotIn("updates", found["rounds"])

    def conflicting(self, held=None, used=0, **options):
        args = (self.gh, self.cfg, decision("fix", head="b"), 6, HEAD, held, used)
        return host.conflicting(*args, **options)

    def test_a_conflict_stores_the_engine_s_answer_at_the_next_revision(self):
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            self.assertEqual(self.conflicting(write=True), "round")
        [(_, event, found)] = seen
        self.assertEqual(event["action"], host.pending_id(found["state"], "publish"))
        self.assertEqual(self.read()[1], found["state"])
        self.assertEqual(self.read()[1]["revision"], 2)

    def test_a_held_conflict_answers_held_whatever_the_stored_conflicts_are(self):
        self.store(conflicts=2)
        self.assertEqual(self.conflicting(held="on-hold"), "held")
        self.assertEqual(self.conflicting(held="on-hold", used=2), "held")
        self.assertEqual(self.conflicting(held="on-hold", write=True), "held")
        # Without the label the stored count is at its limit.
        self.store(conflicts=2)
        self.assertEqual(self.conflicting(), "used_up")

    def test_a_conflict_round_counts_from_the_stored_conflicts(self):
        self.store(conflicts=2)
        self.assertEqual(self.conflicting(used=0), "used_up")
        run = {"head": HEAD, "held_back": "conflicts"}
        args = (self.gh, self.cfg, 5, 6, run, HEAD, True, 0)
        self.assertEqual(host.conflicts_released(*args), "waits")
        self.assertEqual(
            host.base_moved(self.gh, self.cfg, 5, 6, HEAD, True, False, False, 0, 0), "waits"
        )

    def test_a_release_stores_one_revision_for_each_event(self):
        run = {"head": HEAD, "held_back": "fix"}
        self.store(counted=1)
        self.assertEqual(host.released(self.gh, self.cfg, 5, 6, run, HEAD, 1, write=True), "fix")
        _, doc = self.read()
        self.assertEqual((doc["revision"], doc["rounds"]["counted"]), (3, 2))
        self.assertEqual(doc["recentEvents"][-1], f"released-6-{HEAD}")
        # A head pushed meanwhile is a second event.
        moved = host.released(self.gh, self.cfg, 5, 6, run, "d" * 40, 1, write=True)
        self.assertEqual(moved, "review")
        self.assertEqual(self.read()[1]["revision"], 5)
        # Previewed, it stores nothing.
        self.assertEqual(host.released(self.gh, self.cfg, 5, 6, run, HEAD, 1), "fix")
        self.assertEqual(self.read()[1]["revision"], 5)

    def test_a_held_fix_round_released_counts_from_the_stored_count(self):
        run = {"head": HEAD, "held_back": "fix"}
        self.store(counted=3)
        self.assertEqual(host.released(self.gh, self.cfg, 5, 6, run, HEAD, 0), "used_up")

    def test_the_end_of_a_wait_for_the_base_branch_stores_the_answer(self):
        run = {"head": HEAD, "ci_wait": {"bug": 77, "workflow": "ci.yml", "branch": "main"}}
        args = (self.gh, self.cfg, 5, 6, run, 1)
        self.store(counted=1)
        self.assertEqual(host.base_passes(*args, write=True), "fix")
        _, doc = self.read()
        self.assertEqual((doc["revision"], doc["rounds"]["counted"]), (3, 2))
        self.store(counted=3)
        self.assertEqual(host.base_passes(*args), "used_up")

    def test_a_held_conflict_round_released_stores_both_events(self):
        run = {"head": HEAD, "held_back": "conflicts"}
        args = (self.gh, self.cfg, 5, 6, run, HEAD, True, 1)
        self.store(conflicts=1)
        self.assertEqual(host.conflicts_released(*args, write=True), "round")
        _, doc = self.read()
        self.assertGreater(doc["revision"], 1)
        self.assertEqual(doc["recentEvents"][-1], f"released-6-{HEAD}")
        # The round that started counts: the stored conflicts are at the limit now.
        self.assertEqual(doc["rounds"]["conflicts"], 2)
        before = copy.deepcopy(self.gh.comments(5))
        self.assertEqual(host.conflicts_released(*args), "waits")
        self.assertEqual(self.gh.comments(5), before)

    def test_a_stopped_round_stores_its_count_given_back(self):
        counts = host.stopped_counts
        args = (self.gh, self.cfg, 5, 6, HEAD, True)

        def store(counted):
            """Store an automatic fix round on its way with `counted` rounds used, a new round
            that no event of an earlier one is seen for."""
            doc = self.read()[1]
            rounds = {**doc["rounds"], "counted": counted, "automatic": True}
            pending = [first_state.agent_run("fix", HEAD)]
            change = {"id": "6", "version": HEAD}
            return restate(
                self.gh,
                5,
                state="fixing",
                change=change,
                pending=pending,
                rounds=rounds,
                recentEvents=[],
            )

        revision = store(2)["revision"]
        self.assertEqual(counts(*args, write=True), 1)
        _, doc = self.read()
        self.assertEqual((doc["revision"], doc["rounds"]["counted"]), (revision + 1, 1))
        # The stored count is the one that is given back.
        store(3)
        self.assertEqual(counts(*args), 2)
        # An agent that stopped by itself finishes its round and keeps its count.
        revision = store(3)["revision"]
        self.assertEqual(counts(*args[:-1], False, write=True), 3)
        self.assertGreater(self.read()[1]["revision"], revision)

    def test_a_base_branch_move_and_a_diagnosis_leave_the_state_as_it_is(self):
        before = copy.deepcopy(self.gh.comments(5)), copy.deepcopy(self.gh.comments(6))
        moved = host.base_moved(self.gh, self.cfg, 5, 6, HEAD, True, False, False, 0, 0)
        self.assertEqual(moved, "round")
        self.assertEqual(host.diagnosis(self.gh, self.cfg, 5, 6, HEAD, True, ["test"]), ["test"])
        self.assertEqual((self.gh.comments(5), self.gh.comments(6)), before)
        self.assertEqual(self.read()[1]["revision"], 1)

    def test_a_decision_on_a_state_that_someone_else_changed_is_refused(self):
        comment, _ = self.read()
        comment["editors"] = [BOT_LOGIN, "mallory"]
        with self.assertRaises(host.Refused):
            self.ended(write=True)
        with self.assertRaises(host.Refused):
            self.conflicting()

    def test_a_pull_request_with_no_state_comment_decides_as_before_and_stores_none(self):
        self.gh.add_issue(7)
        self.gh.add_pull(8, 7, head_sha=HEAD)
        before = copy.deepcopy(self.gh.comments(7))
        run = {"head": HEAD, "held_back": "fix"}
        self.assertEqual(host.released(self.gh, self.cfg, 7, 8, run, HEAD, 1, write=True), "fix")
        self.assertEqual(self.gh.comments(7), before)


class HeldConflictReleasedTest(HostCase):
    """A held conflict round released waits for a person by the read the engine decided on,
    though GitHub says otherwise a moment later while it works the conflict out again."""

    def test_the_reason_comes_from_the_same_read(self):
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha="c" * 40)
        self.gh.pulls[6]["mergeable"] = True
        details = {**self.gh.pull(6), "mergeable": False}
        cfg = make_config(conflicts={"auto_resolve": False})
        run = {"head": "c" * 40, "held_back": "conflicts"}
        step = autopilot._conflicts_released(
            self.gh, cfg, self.gh.issue(5), 6, run, [], {}, details
        )
        self.assertEqual(step["state"], "waits")
        self.assertIn("auto_resolve", step["reason"])


class StageStopTest(ReportCase):
    """`report` sends a plan, implement or fix stage that stopped at the usage limit or on a
    transient error to the engine, and carries out when it runs again or that a person
    decides. A stop of publish goes as a transient stop of the agent."""

    TRANSIENT = {"status": "error", "error": "Overloaded", "transient": True}
    HOLD = make_config(
        autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}},
        retry={"wait_minutes": [0, 15]},
    )

    def hold(self):
        """Issue 5 is in autopilot's scope with a hold label on it."""
        self.gh.issues[5].update(labels=[{"name": "on-hold"}], milestone={"number": 1})
        self.gh.add_milestone(1)

    def stop(self, result, cfg=None, d=None):
        """(outcome, requests, answers) of the engine's `step` calls for a stage whose agent
        gave `result`."""
        self.write("nexkit-agent", "result.json", result)
        asked, answered = [], []

        def answer(request):
            asked.append(request)
            found = ANSWER(request)
            answered.append(found)
            return found

        with mock.patch.object(host.engine, "answer", side_effect=answer):
            outcome = self.run_report(d=d or decision("plan"), cfg=cfg, published=False, start=True)
        return outcome, asked, answered

    def test_a_limit_with_a_known_reset_waits_for_it(self):
        outcome, [request], [found] = self.stop(LIMIT)
        self.assertEqual(request["event"]["kind"], "actionFinished")
        self.assertEqual(request["event"]["result"]["limit"]["reset"], LIMIT["resume_at"])
        [wait] = [a for a in found["actions"] if a["kind"] == "wait"]
        self.assertEqual(outcome["resume"]["at"], wait["until"])
        self.assertEqual(outcome["resume_at"], wait["until"])
        self.assertIsNone(outcome["attention"])

    def test_a_run_resumed_blind_that_stops_again_gets_no_reset(self):
        blind = {**LIMIT, "reset_known": False}
        outcome, [request], [found] = self.stop(blind, d=decision("plan", resumed=True))
        self.assertIsNone(request["event"]["result"]["limit"]["reset"])
        self.assertEqual(found["state"]["attention"]["reason"], "failed")
        self.assertIsNone(outcome["resume"])
        self.assertIsNone(outcome["resume_at"])
        self.assertEqual(outcome["attention"], reports.PAUSED_REASON)
        # The first blind stop waits.
        outcome, [request], _ = self.stop(blind)
        self.assertEqual(outcome["resume"]["at"], LIMIT["resume_at"])

    def test_a_limit_that_is_not_resumed_waits_for_a_person(self):
        cfg = make_config(usage_limit={"resume": False})
        outcome, [request], _ = self.stop(LIMIT, cfg=cfg)
        self.assertIsNone(request["event"]["result"]["limit"]["reset"])
        self.assertIsNone(outcome["resume"])
        self.assertIn("It resets at", outcome["summary"])

    def test_each_wait_of_the_retry_settings_then_a_person(self):
        cfg = make_config(retry={"wait_minutes": [0, 15]})
        now = datetime.now(UTC)
        outcome, [request], _ = self.stop(self.TRANSIENT, cfg=cfg)
        self.assertEqual(outcome["outcome"], "retry")
        self.assertTrue(request["event"]["result"]["transient"])
        self.assertEqual(request["state"].get("again"), None)
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["auto"], "retry")

        # What starts the run again later is `maintain`'s run that waits for the time, not
        # the command.
        def commands():
            return [d for d in self.gh.dispatches if d["inputs"]["command"] != "resume"]

        outcome, [request], [found] = self.stop(self.TRANSIENT, cfg, decision("plan", retry=1))
        self.assertEqual(request["state"]["again"], {"transient": 1})
        self.assertTrue(request["event"]["result"]["transient"])
        [wait] = [a for a in found["actions"] if a["kind"] == "wait"]
        self.assertEqual(outcome["outcome"], "retry_waiting")
        self.assertEqual(outcome["resume"]["at"], wait["until"])
        self.assertEqual(outcome["resume"]["retry"], 2)
        at = datetime.strptime(wait["until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.assertLess(abs(at - now - timedelta(minutes=15)), timedelta(minutes=1))
        self.assertEqual(len(commands()), 1)
        outcome, [request], _ = self.stop(self.TRANSIENT, cfg, decision("plan", retry=2))
        self.assertEqual(request["state"]["again"], {"transient": 2})
        self.assertTrue(request["event"]["result"]["transient"])
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertEqual(len(commands()), 1)
        self.assertNotIn("resume", outcome)

    def test_a_job_that_ended_without_a_result_is_a_transient_stop_of_the_agent(self):
        self.write("nexkit-agent", "result.json", {})
        (self.artifacts / "nexkit-agent" / "result.json").unlink()
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            outcome = self.run_report(d=decision("plan"), cfg=ONCE, published=False, start=True)
        self.assertEqual(outcome["outcome"], "retry")
        [call] = answer.call_args_list
        self.assertTrue(call.args[0]["event"]["result"]["transient"])

    def publish_stop(self, d, cfg):
        self.write("nexkit-agent", "result.json", {"status": "done", "output": {}})
        error = "git push --quiet origin failed: The requested URL returned error: 500"
        result = {"published": False, "error": error, "transient": True}
        needs = {"publish": {"outputs": {"result": json.dumps(result)}}}
        progress.start(self.gh, d)
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            outcome = report(
                self.gh,
                d,
                cfg,
                needs,
                self.artifacts,
                workflow_ref=WORKFLOW,
                default_branch="main",
            )
        return outcome, [call.args[0] for call in answer.call_args_list], error

    def test_a_stop_of_publish_goes_to_the_engine_as_a_transient_stop(self):
        outcome, [request], error = self.publish_stop(decision(), ONCE)
        self.assertEqual(request["event"]["kind"], "actionFinished")
        self.assertTrue(request["event"]["result"]["transient"])
        self.assertEqual(request["event"]["result"]["error"], error)
        self.assertEqual(outcome["outcome"], "retry")
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_a_stop_of_publish_after_the_last_wait_goes_to_a_person(self):
        outcome, [request], _ = self.publish_stop(decision(retry=1), ONCE)
        self.assertEqual(request["state"]["again"], {"transient": 1})
        self.assertEqual(outcome["outcome"], "retry_failed")
        self.assertEqual(self.gh.dispatches, [])

    def test_a_hold_the_state_holds_already_is_not_sent_and_changes_nothing_the_engine_answers(
        self,
    ):
        self.hold()
        for result, outcome in ((LIMIT, "paused"), (self.TRANSIENT, "retry")):
            with self.subTest(outcome=outcome):
                found, requests, _ = self.stop(result, cfg=self.HOLD)
                self.assertEqual(found["outcome"], outcome)
                # The import of the labelled issue is held already, so no `held` goes first.
                self.assertEqual([r["event"]["kind"] for r in requests], ["actionFinished"])
                self.assertTrue(requests[0]["state"]["hold"])

    def test_a_stop_the_engine_refuses_waits_for_a_person(self):
        with mock.patch.object(host, "stage_limited", side_effect=host.Refused("No.")):
            outcome, _, _ = self.stop(LIMIT)
        self.assertEqual(outcome["outcome"], "paused")
        self.assertIsNone(outcome["resume"])
        self.assertIn("refused to decide", outcome["attention"])
        with mock.patch.object(host, "stage_transient", side_effect=host.Refused("No.")):
            outcome, _, _ = self.stop(self.TRANSIENT, cfg=ONCE)
        self.assertEqual(self.gh.dispatches, [])
        self.assertNotIn(outcome["outcome"], ("retry", "retry_waiting", "retry_failed"))
        self.assertIn("refused to decide", outcome["attention"])

    def test_a_fix_round_on_a_pull_request_keeps_its_change(self):
        d = decision("fix", pr=6, target=6, head=HEAD)
        _, [request], _ = self.stop(self.TRANSIENT, cfg=ONCE, d=d)
        self.assertEqual(request["state"]["change"], {"id": "6", "version": HEAD})
        self.assertEqual(request["state"]["state"], "fixing")

    def test_an_automatic_fix_round_that_waits_to_run_again_stays_in_its_wait(self):
        # `report` shows the round's stop too, which must not end the item that waits.
        self.gh.add_pull(6, 5, head_sha=HEAD)
        self.gh.comment(6, render_state({**empty_state(5), "auto_fixes": 1}))
        d = decision("fix", pr=6, target=6, head=HEAD, auto=True)
        cfg = make_config(retry={"wait_minutes": [15]})
        self.write("nexkit-agent", "result.json", self.TRANSIENT)
        progress.start(self.gh, d)
        seed(self.gh, cfg, 5)
        outcome = self.run_report(d=d, cfg=cfg, published=False)
        self.assertEqual(outcome["outcome"], "retry_waiting")
        doc = item_state.read(self.gh, 5)[1]
        self.assertEqual(doc["state"], "fixing")
        self.assertEqual([w["for"] for w in doc["waits"]], ["time"])
        self.assertIsNone(doc["attention"])


class ResumeTest(ReportCase):
    """The `step` that `maintain.resume_paused` sends for each due resume sends its tick to the
    engine, a hold on the issue included, and starts the command when the engine runs it."""

    NOW = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)

    def paused(self, command="plan", at="2026-10-11T04:40:00Z", **resume):
        record = {"command": command, "number": 5, "note": "", "auto": "resume", "at": at}
        run = {"url": "https://x/runs/1", "command": command, "status": "paused"}
        if command == "review":
            # A round that paused at its review, which the import keeps as `reviewing`.
            run["verdict"] = "paused"
        run["resume"] = {**record, **resume}
        comment = self.gh.comment(5, render_run(run, "⏸️ Paused."))
        # Paused shortly before `NOW`, so that the import does not find the pause stale.
        comment["created_at"] = "2026-10-11T04:30:00Z"
        return comment

    def resume(self, cfg=None):
        """The resume run's step, on the state that the item's first run stored before the
        pause, as of `NOW`."""
        cfg = cfg or make_config()
        host.store_import(self.gh, cfg, 5, now=self.NOW)
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            lines, _ = play_resume(self.gh, cfg, self.NOW)
        return lines, [call.args[0] for call in answer.call_args_list]

    def test_a_run_paused_under_a_hold_starts_again_at_its_time(self):
        self.gh.issues[5].update(labels=[{"name": "on-hold"}], milestone={"number": 1})
        self.gh.add_milestone(1)
        cfg = pilot(labels={"on-hold": {"hold": True}})
        self.paused()
        lines, requests = self.resume(cfg)
        self.assertEqual(lines, ["Resumed /nexkit plan on #5."])
        # The stored import already holds the label, so no `held` goes first.
        self.assertEqual([r["event"]["kind"] for r in requests], ["tick"])
        self.assertTrue(requests[0]["state"]["hold"])
        self.assertEqual(requests[0]["state"]["waits"][0]["for"], "usageLimit")
        [dispatch] = self.gh.dispatches
        self.assertEqual(dispatch["inputs"]["command"], "plan")

    def test_each_command_runs_its_stage_again(self):
        for command, state in (
            ("plan", "planning"),
            ("go", "working"),
            ("fix", "fixing"),
            ("review", "reviewing"),
        ):
            with self.subTest(command=command):
                self.setUp()
                self.paused(command)
                _, [request] = self.resume()
                self.assertEqual(request["state"]["state"], state)
                [dispatch] = self.gh.dispatches
                self.assertEqual(dispatch["inputs"]["command"], command)

    def test_a_run_waiting_after_a_transient_error_waits_for_a_time(self):
        self.paused(retry=2, auto="retry:2")
        _, [request] = self.resume()
        self.assertEqual(request["state"]["waits"][0]["for"], "time")
        self.assertEqual(request["state"]["again"], {"transient": 2})
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_a_command_nexkit_never_writes_waits_for_a_person(self):
        self.paused("close")
        lines, requests = self.resume()
        self.assertEqual(requests, [])
        self.assertEqual(self.gh.dispatches, [])
        self.assertIn("Did not resume /nexkit close on #5", lines[0])
        [run] = self.gh.run_comments(5)
        self.assertEqual(run["run"]["status"], "failure")
        self.assertNotIn("resume", run["run"])
        self.assertIn("refused to decide", run["run"]["attention"])


class ChangeSeenTest(ReportCase):
    """What the merge finds about a pull request is an observation the engine ends the item
    with, or checks: `change_seen` sends it and never fakes the merge's result. The stored
    state holds whether the merge waits for required checks or has its accept pending."""

    HEAD = "c" * 40

    def seen(self, merged=False, empty=False, version=None, waiting=False, cfg=None):
        cfg = cfg or make_config(merge={"auto": True})
        self.gh.issue_comments[5] = []
        if waiting:
            merging(self.gh, cfg)
        else:
            version_ = seed(self.gh, cfg, 5)[1]["change"]["version"]
            accept = {"id": "a1.1", "next": None, "kind": "accept", "by": "engine"}
            accept["changeVersion"] = version_
            restate(self.gh, 5, state="accepting", pending=[accept])
        seen = {"version": version or self.HEAD, "merged": merged, "empty": empty}
        return host.change_seen(self.gh, cfg, 5, 6, seen)

    def each(self, **kwargs):
        """The answers for a merge that waits and for one whose accept is pending."""
        return [self.seen(waiting=waiting, **kwargs) for waiting in (False, True)]

    def test_a_pull_request_a_person_merged_ends_the_item_as_accepted(self):
        self.assertEqual(self.each(merged=True), ["accepted", "accepted"])

    def test_a_change_the_base_branch_holds_ends_the_item_without_a_change(self):
        self.assertEqual(self.each(empty=True), ["noChange", "noChange"])

    def test_a_push_while_the_merge_waits_is_checked(self):
        self.assertEqual(self.each(version="d" * 40), ["review", "review"])

    def test_the_merge_sends_an_observation_and_no_result(self):
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.seen(merged=True)
        [event] = [call.args[1] for call in step.call_args_list]
        self.assertEqual(event["kind"], "changeObserved")
        self.assertEqual(
            (event["changeId"], event["merged"], event["conflicts"]), ("6", True, False)
        )
        self.assertRegex(event["id"], r"^seen-6-c{40}-r\d+$")

    def test_an_observation_that_ends_nothing_and_changes_nothing_is_refused(self):
        with self.assertRaises(host.Refused):
            self.seen()
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                self.seen(merged=True)


class NoChangeFoundTest(ReportCase):
    """An implement round that finds no change is needed, and a fix round whose base branch
    already holds its change, close only when the engine ends the item as `noChange`. Both
    decide on the item's stored state alone, which holds the round that is on its way."""

    def setUp(self):
        super().setUp()
        self.cfg = make_config()

    def start(self, action="implement"):
        """Start the round and store the item's state, as a run in its group does. An implement
        round runs only while no NexKit pull request is open, so pull request 6 is closed for
        it."""
        os.environ["GITHUB_RUN_ID"] = str(next(self.run_ids))
        if action == "implement":
            self.gh.pulls[6]["state"] = self.gh.issues[6]["state"] = "closed"
            d = decision()
        else:
            d = decision("fix", pr=6, target=6, head=HEAD)
        progress.start(self.gh, d)
        return seed(self.gh, self.cfg, 5)[1], d

    def implement(self):
        doc, _ = self.start()
        self.assertEqual(doc["state"], "working")
        self.assertEqual([a["kind"] for a in doc["pending"]], ["runAgent"])
        return doc

    def fix(self):
        doc, d = self.start("fix")
        self.assertEqual(doc["state"], "fixing")
        return doc, {"issue": 5, "pr": 6, "head": HEAD}

    def test_the_implement_round_ends_the_item_on_the_stored_state(self):
        doc = self.implement()
        with mock.patch.object(host, "step", wraps=host.step) as step:
            found = host.no_change_found(
                self.gh, self.cfg, {"issue": 5}, "Fixed by 8b9cb87.", write=True
            )
        self.assertEqual(found, "noChange")
        [call] = step.call_args_list
        self.assertEqual(call.args[0], doc)
        event = call.args[1]
        self.assertEqual(event["kind"], "actionFinished")
        self.assertEqual(event["result"]["output"]["no_change"], "Fixed by 8b9cb87.")
        stored = item_state.read(self.gh, 5)[1]
        self.assertEqual((stored["revision"], stored["state"]), (doc["revision"] + 1, "noChange"))
        self.assertIn("close", [action["kind"] for action in stored["pending"]])

    def test_the_fix_round_ends_the_item_on_the_stored_state(self):
        doc, decided = self.fix()
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.assertEqual(host.on_base_found(self.gh, self.cfg, decided, write=True), "noChange")
        [call] = step.call_args_list
        self.assertEqual(call.args[0], doc)
        self.assertEqual(call.args[0]["state"], "fixing")
        event = call.args[1]
        self.assertEqual(
            (event["kind"], event["changeId"], event["empty"], event["merged"]),
            ("changeObserved", "6", True, False),
        )
        stored = item_state.read(self.gh, 5)[1]
        self.assertEqual((stored["revision"], stored["state"]), (doc["revision"] + 1, "noChange"))
        self.assertIn("close", [action["kind"] for action in stored["pending"]])

    def test_the_implement_round_stores_the_engine_s_answer_only_with_write(self):
        self.implement()
        before = copy.deepcopy(self.gh.comments(5))
        found = host.no_change_found(self.gh, self.cfg, {"issue": 5}, "Fixed by 8b9cb87.")
        self.assertEqual(found, "noChange")
        self.assertEqual(self.gh.comments(5), before)

    def test_the_fix_round_stores_the_engine_s_answer_only_with_write(self):
        _, decided = self.fix()
        before = copy.deepcopy(self.gh.comments(5))
        self.assertEqual(host.on_base_found(self.gh, self.cfg, decided), "noChange")
        self.assertEqual(self.gh.comments(5), before)

    def test_no_configuration_is_refused_before_anything_is_written(self):
        self.implement()
        before = copy.deepcopy(self.gh.comments(5))
        with self.assertRaises(host.Refused):
            host.no_change_found(self.gh, None, {"issue": 5}, "Fixed.")
        with self.assertRaises(host.Refused):
            host.on_base_found(self.gh, None, {"issue": 5, "pr": 6, "head": HEAD})
        self.assertEqual(self.gh.comments(5), before)

    def test_an_answer_that_ends_nothing_is_refused(self):
        self.implement()
        # An empty finding is no finding: the engine publishes the round's change.
        with self.assertRaises(host.Refused):
            host.no_change_found(self.gh, self.cfg, {"issue": 5}, "")

    def test_an_answer_of_the_engine_that_is_no_end_is_refused(self):
        self.implement()
        for call in (
            lambda: host.no_change_found(self.gh, self.cfg, {"issue": 5}, "Fixed."),
            lambda: host.on_base_found(self.gh, self.cfg, {"issue": 5, "pr": 6, "head": HEAD}),
        ):
            with mock.patch.object(engine, "answer", return_value={"error": "no"}):
                with self.assertRaises(host.Refused):
                    call()


class PartEndedTest(ReportCase):
    """A part that NexKit merged ends its parent only as the engine decides it on the parent's
    stored state, under `parentEnd`: `close_parent_issues` on gives `whenPartAccepted`, off gives
    `person`. Each test stores the parent's state while its parts are open, as the run that
    split it did, and then closes parts on GitHub."""

    def setUp(self):
        super().setUp()
        self.gh.add_issue(1, title="Epic")
        self.cfg = make_config(close_parent_issues=True)

    def parts(self, *numbers):
        """Sub-issues of the parent 1: issue 5 and each of `numbers`, all open, and the
        parent's state stored as it is while they are."""
        for number in (5, *numbers):
            if number != 5:
                self.gh.add_issue(number)
            self.gh.parents[number] = 1
        return seed(self.gh, self.cfg, 1)[1]

    def close(self, number, reason="completed"):
        self.gh.issues[number].update(state="closed", state_reason=reason)

    def ended(self, part=5, state="accepted", cfg=None, write=False):
        subs = self.gh.sub_issues(1)
        return host.part_ended(self.gh, cfg or self.cfg, 1, subs, part, state, write=write)

    def test_the_parent_closes_when_every_part_ended_and_one_was_completed(self):
        self.parts(20, 21)
        for number, reason in ((5, "not_planned"), (20, "completed"), (21, "not_planned")):
            self.close(number, reason)
        before = item_state.read(self.gh, 1)
        comments = copy.deepcopy(self.gh.comments(1))
        # The end of the part that closed last answers for the earlier ones too, as a preview.
        self.assertEqual(self.ended(21, "dropped"), "close")
        self.assertEqual(item_state.read(self.gh, 1), before)
        self.assertEqual(self.gh.comments(1), comments)

    def test_the_end_of_an_earlier_part_that_never_reached_the_parent_still_counts(self):
        self.parts(20)
        self.close(5)
        self.close(20)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.assertEqual(self.ended(20), "close")
        sent = [call.args[1] for call in step.call_args_list]
        ends = [(e["kind"], e["part"]) for e in sent]
        self.assertEqual(ends, [("partEnded", "5"), ("partEnded", "20")])
        self.assertEqual(step.call_args_list[-1].args[2]["parentEnd"], "whenPartAccepted")

    def test_the_parent_stays_while_a_part_is_open(self):
        self.parts(20)
        self.close(5)
        self.assertEqual(self.ended(), "stays")

    def test_a_person_decides_when_no_part_was_completed(self):
        self.parts(20)
        self.close(5, "not_planned")
        self.close(20, "not_planned")
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.assertEqual(self.ended(20, "dropped"), "person")
        self.assertEqual(step.call_args.args[1]["part"], "20")

    def test_without_close_parent_issues_the_engine_gets_person_and_a_person_decides(self):
        self.parts(20)
        self.close(5)
        self.close(20)
        off = make_config(close_parent_issues=False)
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.assertEqual(self.ended(20, cfg=off), "person")
            self.assertEqual(step.call_args.args[2]["parentEnd"], "person")
            self.assertEqual(self.ended(20, cfg=self.cfg), "close")
            self.assertEqual(step.call_args.args[2]["parentEnd"], "whenPartAccepted")

    def test_a_list_that_misses_the_part_still_counts_it(self):
        self.parts()
        self.close(5)
        found = host.part_ended(self.gh, self.cfg, 1, [], 5, "accepted")
        self.assertEqual(found, "close")

    def test_record_part_end_stores_one_row_in_the_parents_state(self):
        self.parts(20)
        self.close(5)
        self.close(20)
        _, before = item_state.read(self.gh, 1)
        lines = maintain.record_part_end(self.gh, self.cfg, {"issue": 1, "part": 5})
        self.assertIn("close", lines[0])
        _, after = item_state.read(self.gh, 1)
        # Both ends, a sibling's and the part's own, are one write.
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(
            after["parts"],
            [{"item": "5", "state": "accepted"}, {"item": "20", "state": "accepted"}],
        )

    def test_a_sibling_s_end_after_the_parents_close_was_stored_and_done_still_closes(self):
        self.parts(20)
        self.close(5)
        maintain.record_part_end(self.gh, self.cfg, {"issue": 1, "part": 5})
        self.close(20)
        self.gh.issues[1].update(state="closed", state_reason="completed")
        maintain.record_part_end(self.gh, self.cfg, {"issue": 1, "part": 20})
        _, doc = item_state.read(self.gh, 1)
        self.assertEqual((doc["state"], doc["pending"]), ("accepted", []))
        before = item_state.read(self.gh, 1)
        self.assertEqual(self.ended(5), "close")
        lines = maintain.record_part_end(self.gh, self.cfg, {"issue": 1, "part": 5})
        self.assertIn("close", lines[0])
        self.assertEqual(item_state.read(self.gh, 1), before)

    def test_a_parent_with_an_eleventh_sub_issue_stays_while_it_is_open(self):
        numbers = range(20, 20 + MOST["split"])
        doc = self.parts(*numbers)
        self.assertEqual(len(doc["parts"]), MOST["split"])
        for number in (5, *numbers[:-1]):
            self.close(number)
        before = item_state.read(self.gh, 1)
        self.assertEqual(self.ended(), "stays")
        self.assertEqual(item_state.read(self.gh, 1), before)
        self.assertEqual(self.ended(write=True), "stays")
        self.assertEqual(item_state.read(self.gh, 1), before)
        self.assertEqual(maintain.close_parent(self.gh, self.cfg, 1, 5), ([], False))

    def test_a_parent_whose_only_completed_part_is_beyond_the_ones_a_state_holds_waits_for_a_person(
        self,
    ):
        numbers = range(20, 20 + MOST["split"])
        self.parts(*numbers)
        for number in (5, *numbers[:-1]):
            self.close(number, "not_planned")
        self.close(numbers[-1])
        self.assertEqual(self.ended(5, "dropped"), "person")

    def test_a_sub_issue_added_after_the_split_keeps_the_parent_open(self):
        self.parts(20)
        self.close(5)
        self.close(20)
        self.gh.add_issue(30)
        self.gh.parents[30] = 1
        before = item_state.read(self.gh, 1)
        self.assertEqual(self.ended(20), "stays")
        self.assertEqual(item_state.read(self.gh, 1), before)
        # Once it closes, the parent's own parts decide.
        self.close(30)
        self.assertEqual(self.ended(20), "close")

    def test_a_parent_stored_before_it_had_sub_issues_is_left_to_a_person(self):
        seed(self.gh, self.cfg, 1)
        for number in (5, 20):
            if number != 5:
                self.gh.add_issue(number)
            self.gh.parents[number] = 1
            self.close(number)
        before = item_state.read(self.gh, 1)
        with self.assertRaises(host.Refused) as refused:
            self.ended(20)
        self.assertIn("a person decides", str(refused.exception))
        lines, closed = maintain.close_parent(self.gh, self.cfg, 1, 20)
        self.assertFalse(closed)
        self.assertIn("refused to decide", lines[0])
        self.assertEqual(self.gh.issues[1]["state"], "open")
        self.assertEqual(item_state.read(self.gh, 1), before)

    def test_a_parent_waiting_for_its_parts_to_be_filed_closes_once_the_filed_parts_close(self):
        seed(self.gh, self.cfg, 1)
        body = "<!-- nexkit:part 1 -->\nDo it."
        for number in (20, 21):
            self.gh.add_issue(number, body=body, user=BOT_LOGIN)
            self.gh.parents[number] = 1
        making = [
            {"key": key, "title": f"Part {key}", "needs": [], "body": {"name": "n", "ref": "r"}}
            for key in (1, 2)
        ]
        action = {"id": "a1.1", "kind": "createParts", "next": None, "parts": making}
        wait = {"id": "w1.1", "for": "parts", "until": None, "on": None}
        restate(self.gh, 1, state="split", pending=[action], parts=[], waits=[wait])
        self.assertEqual(self.ended(20), "stays")
        self.close(20, "not_planned")
        self.close(21)
        self.assertEqual(self.ended(21, write=True), "close")
        _, doc = item_state.read(self.gh, 1)
        self.assertEqual(
            doc["parts"],
            [{"item": "20", "state": "dropped"}, {"item": "21", "state": "accepted"}],
        )
        self.assertEqual([a["kind"] for a in doc["pending"]], ["close"])

    def test_the_engine_may_refuse_to_decide(self):
        self.parts(20)
        self.close(5)
        self.close(20)
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                self.ended(20)

    def test_a_round_keeps_the_policies_it_started_with_without_the_parents_end(self):
        doc = host._pull_doc(make_config(), 5, 6, "c" * 40, "fixing", 0)
        self.assertNotIn("parentEnd", doc["policies"])
        self.assertNotIn("parentEnd", host.policies(make_config()))


class MergeFindsTest(ReportCase):
    """`merge_pull` follows the engine's answer to what it finds: a pull request that a person
    merged, a change the base branch holds, and the parent after NexKit's own merge."""

    AUTO = make_config(merge={"auto": True})

    def merge(self, cfg=None):
        self.candidate()
        return self.run_report(cfg=cfg or self.AUTO)

    def merged_by_person(self):
        self.gh.issues[5].update(state="closed", state_reason="completed")
        self.gh.pulls[6].update(state="closed", merged=True, merged_by={"login": "olivia"})

    def test_a_pull_request_a_person_merged_ends_the_round_as_the_engine_answers(self):
        self.merged_by_person()
        with mock.patch.object(host, "change_seen", wraps=host.change_seen) as seen:
            outcome = self.merge()
        self.assertEqual(outcome["outcome"], "pull_closed")
        self.assertEqual(seen.call_args.args[4], {"version": HEAD, "merged": True, "empty": False})
        self.merged_by_person()
        with mock.patch.object(host, "change_seen", side_effect=host.Refused("no")):
            outcome = self.merge()
        self.assertEqual(outcome["outcome"], "merge_refused")
        self.assertIn("refused to decide", outcome["attention"])

    def test_a_pull_request_closed_unmerged_is_not_sent_to_the_engine(self):
        self.gh.issues[5].update(state="closed", state_reason="not_planned")
        self.gh.pulls[6].update(state="closed", merged=False)
        with mock.patch.object(host, "change_seen") as seen:
            outcome = self.merge()
        self.assertEqual(outcome["outcome"], "pull_closed")
        seen.assert_not_called()

    def test_a_merge_found_while_github_refuses_is_the_engines_too(self):
        self.gh.merge_error = GitHubError(405, "Pull Request is not mergeable")
        self.gh.pulls[6].update(state="closed", merged=True, merged_by={"login": "olivia"})
        with mock.patch.object(host, "change_seen", wraps=host.change_seen) as seen:
            outcome = self.merge()
        self.assertEqual(outcome["outcome"], "pull_closed")
        self.assertTrue(seen.call_args.args[4]["merged"])

    def test_a_change_the_base_branch_holds_is_closed_only_when_the_engine_ends_it(self):
        self.gh.on_base.add(HEAD)
        with mock.patch.object(host, "change_seen", wraps=host.change_seen) as seen:
            outcome = self.merge()
        self.assertEqual(outcome["outcome"], "already_on_base")
        self.assertEqual(seen.call_args.args[4], {"version": HEAD, "merged": False, "empty": True})
        self.assertEqual(self.gh.pulls[6]["state"], "closed")
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.gh.add_pull(6, 5, head_sha=HEAD)
        self.gh.on_base.add(HEAD)
        with mock.patch.object(host, "change_seen", return_value="review"):
            outcome = self.merge()
        self.assertEqual(outcome["outcome"], "merge_refused")
        self.assertEqual(self.gh.pulls[6]["state"], "open")
        self.assertEqual(self.gh.issues[5]["state"], "open")

    def parent(self, *parts):
        self.gh.add_issue(1, title="Epic")
        self.gh.parents[5] = 1
        for number, reason in parts:
            self.gh.add_issue(number, state="closed", reason=reason)
            self.gh.parents[number] = 1

    def test_the_parent_closes_after_the_merge_only_when_the_engine_answers_close(self):
        cfg = make_config(merge={"auto": True}, close_parent_issues=True)
        self.parent((2, "completed"))
        with mock.patch.object(host, "part_ended", return_value="stays") as ended:
            self.assertEqual(self.merge(cfg)["outcome"], "merged")
        self.assertEqual(self.gh.issues[1]["state"], "open")
        self.assertEqual(ended.call_args.args[2:], (1, ended.call_args.args[3], 5, "accepted"))
        self.gh.reopen(6, 5)
        self.gh.pulls[6]["state"] = "open"
        with mock.patch.object(host, "part_ended", return_value="close"):
            self.assertEqual(self.merge(cfg)["outcome"], "merged")
        self.assertEqual(self.gh.issues[1]["state"], "closed")

    def test_without_close_parent_issues_the_engine_gets_person_and_a_person_decides(self):
        self.parent((2, "completed"))
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.assertEqual(self.merge()["outcome"], "merged")
        parents = [call.args for call in step.call_args_list if call.args[1]["kind"] == "partEnded"]
        self.assertEqual([args[2]["parentEnd"] for args in parents], ["person"])
        self.assertEqual(self.gh.issues[1]["state"], "open")

    def test_a_parent_that_is_closed_or_has_no_part_to_wait_for_is_left_alone(self):
        cfg = make_config(merge={"auto": True}, close_parent_issues=True)
        self.parent((2, "completed"))
        self.gh.issues[1]["state"] = "closed"
        with mock.patch.object(host, "part_ended") as ended:
            self.assertEqual(self.merge(cfg)["outcome"], "merged")
        ended.assert_not_called()

    def test_an_engine_that_refuses_the_parent_closes_none_and_says_so(self):
        cfg = make_config(merge={"auto": True}, close_parent_issues=True)
        self.parent((2, "completed"))
        with mock.patch.object(host, "part_ended", side_effect=host.Refused("no")):
            outcome = self.merge(cfg)
        self.assertEqual(outcome["outcome"], "merged")
        self.assertEqual(self.gh.issues[1]["state"], "open")
        [row] = self.gh.run_comments(6)
        self.assertIn("engine refused to decide", row["body"])

    def test_a_parent_with_more_sub_issues_than_a_state_holds_goes_through_the_engine_too(self):
        cfg = make_config(merge={"auto": True}, close_parent_issues=True)
        self.parent(*[(n, "completed") for n in range(20, 20 + MOST["split"])])
        with mock.patch.object(host, "part_ended", wraps=host.part_ended) as ended:
            self.assertEqual(self.merge(cfg)["outcome"], "merged")
        ended.assert_called_once()
        self.assertEqual(self.gh.issues[1]["state"], "closed")


class MergeStepTest(ReportCase):
    """The sweep's tick of a merge that waits for required checks reviews a head that a person
    pushed meanwhile only when the engine answers that it checks it."""

    def step(self):
        cfg = make_config(merge={"auto": True})
        now = datetime.now(UTC)
        # The merge waits for required checks in the stored state, and then a person pushes.
        merging(self.gh, cfg, since=now)
        self.gh.pulls[6]["head"]["sha"] = "d" * 40
        comment = {"id": 77}
        run = {"head": HEAD, "waiting_since": autopilot.iso(now)}
        return autopilot._merge_step(self.gh, cfg, {"number": 5}, 6, comment, run, {}, now)

    def test_a_head_pushed_while_the_merge_waited_is_reviewed_when_the_engine_checks_it(self):
        with mock.patch.object(host, "change_seen", wraps=host.change_seen) as seen:
            step = self.step()
        self.assertEqual((step["state"], step["command"]), ("start", "review"))
        self.assertEqual(seen.call_args.args[4]["version"], "d" * 40)

    def test_a_head_the_engine_refuses_waits_for_a_person(self):
        with mock.patch.object(host, "change_seen", side_effect=host.Refused("no")):
            step = self.step()
        self.assertEqual(step["state"], "waits")
        self.assertIn("refused to decide", step["reason"])


class LostAgainTest(TestCase):
    """A command that autopilot started and that was lost starts again, waits, or waits for a
    person as the engine decides, with NexKit 6's words, due times and counts."""

    CFG = make_config(autopilot={"enabled": True})
    NOW = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)
    AT = "2099-01-01T11:50:00Z"
    ISSUE = {"number": 5, "title": "Add it"}
    WHAT = "the run ended without a report"
    # Each command and the target of its start: the issue, or its pull request.
    COMMANDS = (("plan", 5), ("go", 5), ("fix", 6), ("review", 6))
    # The minutes after the last start that the n-th start waits.
    DUE = (0, 30, 60, 120)

    def github(self):
        """A repository with the issue and its pull request, which the item's state is read
        from."""
        gh = FakeGitHub()
        gh.add_issue(5, title="Add it")
        gh.add_pull(6, 5)
        return gh

    def lost(self, command="plan", target=5, tries=0, now=None, at=None, **kwargs):
        gh = kwargs.pop("gh", None) or self.github()
        record = {"command": command, "target": target, "at": at or self.AT, "tries": tries}
        record.update(kwargs.pop("record", {}))
        fields = {"command": command, "target": target, "note": "", "remember": {}}
        fields.update(kwargs.pop("fields", {}))
        now = now or self.NOW
        with mock.patch.object(host, "step", wraps=host.step) as sent:
            found = host.lost_again(
                gh, self.CFG, self.ISSUE, record, now, self.WHAT, fields, **kwargs
            )
        return found, [call.args for call in sent.call_args_list]

    def test_a_lost_command_starts_again_when_it_is_due(self):
        for command, target in self.COMMANDS:
            for tries in range(4):
                with self.subTest(command=command, tries=tries):
                    due = datetime(2099, 1, 1, 11, 50, tzinfo=UTC) + timedelta(
                        minutes=self.DUE[tries]
                    )
                    found, _ = self.lost(command, target, tries, now=due)
                    self.assertEqual((found["state"], found["command"]), ("start", command))
                    self.assertEqual(found["target"], target)
                    self.assertEqual(found["remember"]["tries"], tries + 1)
                    self.assertNotIn("once", found["remember"])
                    again = "again" if tries else "once more"
                    self.assertIn(f"autopilot starts `/nexkit {command}` {again}", found["reason"])

    def test_a_lost_command_waits_until_its_due_time(self):
        for command, target in self.COMMANDS:
            for tries in range(1, 4):
                with self.subTest(command=command, tries=tries):
                    due = datetime(2099, 1, 1, 11, 50, tzinfo=UTC) + timedelta(
                        minutes=self.DUE[tries]
                    )
                    early = due - timedelta(seconds=1)
                    found, requests = self.lost(command, target, tries, now=early)
                    self.assertEqual(found["state"], "active")
                    self.assertEqual([r[1]["kind"] for r in requests], ["actionLost"])
                    self.assertIn(f"again after {due:%H:%M} UTC", found["reason"])
                    self.assertIn(f"`/nexkit {command}`", found["reason"])

    def test_the_loss_is_sent_at_the_last_start_and_a_run_again_after_its_wait(self):
        _, requests = self.lost("fix", 6, 2, now=self.NOW + timedelta(hours=2))
        lost, tick = [event for _, event, _ in requests]
        self.assertEqual((lost["kind"], lost["at"]), ("actionLost", self.AT))
        self.assertEqual((lost["tokens"], lost["action"]), (None, "a1.1"))
        self.assertEqual(tick["kind"], "tick")
        self.assertEqual(tick["at"], autopilot.iso(self.NOW + timedelta(hours=2)))
        document = requests[0][0]
        self.assertEqual(document["state"], "fixing")
        self.assertEqual(document["again"], {"lost": 2})
        self.assertEqual(document["change"], {"id": "6", "version": "6"})

    def test_a_loss_without_a_usable_record_is_sent_at_the_sweeps_time(self):
        found, requests = self.lost("plan", 5, 0, record={"at": "soon"})
        self.assertEqual(found["state"], "start")
        self.assertEqual(requests[0][1]["at"], autopilot.iso(self.NOW))

    def test_a_review_runs_the_checks_again(self):
        _, requests = self.lost("review", 6, 1, now=self.NOW + timedelta(hours=1))
        document = requests[0][0]
        self.assertEqual(document["state"], "checking")
        self.assertEqual(document["pending"][0]["kind"], "runChecks")
        self.assertEqual(requests[0][1]["changeVersion"], "6")

    def test_what_a_start_carried_stays(self):
        fields = {"auto": "autopilot", "note": "Mind the cache.", "remember": {"split_asked": 1}}
        found, _ = self.lost("go", 5, 0, fields=fields)
        self.assertEqual(found["auto"], "autopilot")
        self.assertEqual(found["note"], "Mind the cache.")
        self.assertEqual(found["remember"], {"split_asked": 1, "tries": 1})
        stale = {"number": 5, "comment": 9}
        found, _ = self.lost("go", 5, 0, extra={"stale": stale})
        self.assertEqual(found["stale"], stale)

    def test_the_fifth_loss_waits_for_a_person(self):
        for tries in (4, 7):
            with self.subTest(tries=tries):
                found, _ = self.lost("go", 5, tries)
                self.assertEqual(found["state"], "waits")
                self.assertIn(f"{tries + 1} times", found["reason"])
                self.assertTrue(
                    found["reason"].startswith(
                        autopilot.OUTAGE_DO.format(command="`/nexkit go`")[:40]
                    )
                )
                self.assertIn("GitHub created no job for it", found["reason"])

    def test_a_run_whose_agent_ran_starts_once_more_only(self):
        found, requests = self.lost("fix", 6, 0, record={"once": True})
        self.assertEqual(found["state"], "start")
        self.assertEqual(found["remember"], {"tries": 1, "once": True})
        self.assertEqual(requests[0][0]["again"], {"once": True})
        found, _ = self.lost("fix", 6, 1, record={"once": True})
        self.assertEqual(found["state"], "waits")
        self.assertIn("after autopilot started it once more", found["reason"])
        self.assertTrue(found["reason"].startswith(autopilot.LOST_DO))

    def test_a_persons_command_waits_for_that_person(self):
        stale = {"number": 6, "comment": 9}
        found, requests = self.lost("fix", 6, 0, extra={"stale": stale}, person=True)
        self.assertEqual(found["state"], "waits")
        self.assertEqual(found["reason"], autopilot.person(autopilot.LOST_DO, self.WHAT))
        self.assertEqual(found["stale"], stale)
        self.assertEqual(requests[0][0]["pending"][0]["by"], "person")
        found, _ = self.lost("fix", 6, 0, person=True)
        self.assertIsNone(found)

    def test_a_hold_label_is_sent_and_a_held_restart_answers_held(self):
        found, requests = self.lost("plan", 5, 0, label="on-hold")
        self.assertEqual([r[1]["kind"] for r in requests], ["held", "actionLost"])
        self.assertEqual(found["state"], "held")
        self.assertIn("`on-hold`", found["reason"])
        due = self.NOW + timedelta(hours=1)
        found, requests = self.lost("plan", 5, 1, now=due, label="on-hold")
        self.assertEqual([r[1]["kind"] for r in requests], ["held", "actionLost", "tick"])
        self.assertEqual(found["state"], "held")

    def test_a_command_nexkit_never_writes_is_refused(self):
        with self.assertRaises(host.Refused):
            self.lost("close", 5, 0)

    def test_autopilot_waits_for_a_person_when_the_engine_refuses(self):
        fields = {"command": "close", "target": 5, "note": "", "remember": {}}
        found = autopilot._again(
            self.github(), self.CFG, self.ISSUE, {}, self.NOW, self.WHAT, fields
        )
        self.assertEqual(found["state"], "waits")
        self.assertIn("refused to decide", found["reason"])


class StoredStopsTest(HostCase):
    """A blocked or failed `/nexkit go`, a stopped or timed-out round, a usage limit, a transient
    error, a resume that comes due and a lost command store the engine's answer at the next
    revision when they run in the item's group, and only preview it otherwise."""

    NOW = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)
    START = {"basis": "b", "target": 5, "note": "", "remember": {}}
    GO = {"issue": 5, "pr": None, "action": "implement"}
    PLAN = {"issue": 5, "pr": None, "action": "plan"}

    def setUp(self):
        super().setUp()
        self.cfg = pilot(answer="recommended", labels={"on-hold": {"hold": True}})
        self.ends = None
        self.issue(5)
        self.plan(5)

    def stored(self):
        """The item has the state comment that `nexkit route` stores before its first event.
        A `/nexkit go` that `going` left running has stopped by then (`ends`)."""
        seed(self.gh, self.cfg, 5, now=self.NOW)
        if self.ends:
            self.ends()

    def started(self, **fields):
        """The plan run of issue 5 is on its way, as `progress.start` shows it: the stage
        that a usage limit or a transient error stops."""
        decision = {
            "action": "plan",
            "issue": 5,
            "pr": None,
            "target": 5,
            "branch": "nexkit/issue-5",
            "base": "main",
            "head": None,
            "note": "",
            "auto": False,
            "run_id": "7",
            **fields,
        }
        progress.start(self.gh, decision)

    def read(self):
        return item_state.read(self.gh, 5)

    def going(self, **run):
        """A `/nexkit go` that runs now and stops with the marker `run` once the item's state is
        stored, as it does in production: the stored state waits for the implement run. Its run
        comment is returned, and `ends` ends it."""
        self.runs += 1
        url = f"https://github.com/acme/app/actions/runs/{self.runs}"
        started = {"url": url, "command": "go", "status": "running"}
        comment = self.gh.comment(5, render_run(started, "Running."))
        ended = {"url": url, "command": "go", "status": "failure", **run}
        self.ends = lambda: self.gh.update_comment(comment["id"], render_run(ended, "Done."))
        return comment

    def blocked(self):
        run = {"outcome": "agent_blocked", "questions": questions([{**ASKED, "closes": 0}])}
        return self.going(attention="Answer them.", **run)

    def timed_out(self):
        return self.run_comment(5, "go", status="failure", outcome="agent_error", timed_out=True)

    def after_go(self, write=False):
        comments = self.gh.comments(5)
        comment, run = [(c, r) for c, r in run_comments(comments) if "command" in r][-1]
        record = autopilot.find_record(comments)[1]
        issue = self.gh.issues[5]
        return host.after_go(
            self.gh, self.cfg, issue, comment, run, record, self.START, comments, write=write
        )

    def stopped(self, write=False):
        comments = self.gh.comments(5)
        _, run = [(c, r) for c, r in run_comments(comments) if "command" in r][-1]
        record = autopilot.find_record(comments)[1]
        issue = self.gh.issues[5]
        return host.stopped(
            self.gh, self.cfg, issue, run, record, self.START, comments, write=write
        )

    def paused(self, command="plan", at="2026-10-11T04:40:00Z", **resume):
        record = {"command": command, "number": 5, "note": "", "auto": "resume", "at": at}
        run = {"url": "https://x/runs/1", "command": command, "status": "paused"}
        run["resume"] = {**record, **resume}
        comment = self.gh.comment(5, render_run(run, "⏸️ Paused."))
        # Paused shortly before `NOW`, so that the import does not find the pause stale.
        comment["created_at"] = "2026-10-11T04:30:00Z"
        return run

    def lost(self, write=False, now=None):
        record = {"command": "go", "target": 5, "at": "2026-10-11T04:50:00Z", "tries": 1}
        fields = {"command": "go", "target": 5, "note": "", "remember": {}}
        return host.lost_again(
            self.gh,
            self.cfg,
            self.gh.issues[5],
            record,
            now or self.NOW,
            "the run ended without a report",
            fields,
            write=write,
        )

    def calls(self):
        """(name, setup, call) of each decision, where `call(write)` carries it out."""
        agent = {"status": "blocked", "output": {"questions": [{**ASKED, "closes": 0}]}}
        timed_out = {"timed_out": True}
        return [
            ("after_go", self.blocked, self.after_go),
            (
                "round_carried_on",
                self.blocked,
                lambda write: host.round_carried_on(
                    self.gh, self.cfg, self.gh.issues[5], self.GO, agent, write=write
                ),
            ),
            ("stopped", self.timed_out, self.stopped),
            (
                "stop_carried_on",
                self.timed_out,
                lambda write: host.stop_carried_on(
                    self.gh, self.cfg, self.gh.issues[5], self.GO, timed_out, write=write
                ),
            ),
            (
                "stage_limited",
                self.started,
                lambda write: host.stage_limited(
                    self.gh, self.cfg, self.PLAN, "2026-10-11T04:40:00Z", write=write
                ),
            ),
            (
                "stage_transient",
                self.started,
                lambda write: host.stage_transient(
                    self.gh, self.cfg, self.PLAN, self.NOW, "Overloaded", write=write
                ),
            ),
            (
                "resumes",
                self.paused,
                lambda write: host.resumes(
                    self.gh, self.cfg, {"resume": self.resume_record()}, 5, self.NOW, write=write
                ),
            ),
            ("lost_again", lambda: None, self.lost),
        ]

    def resume_record(self):
        return {"command": "plan", "number": 5, "at": "2026-10-11T04:40:00Z"}

    def spied(self):
        seen, real = [], host.step

        def spy(state, event, found_policies):
            found = real(state, event, found_policies)
            seen.append(found)
            return found

        return seen, spy

    def test_each_decision_stores_the_engine_s_answer_at_the_next_revision(self):
        for name, setup, call in self.calls():
            with self.subTest(decision=name):
                self.setUp()
                setup()
                self.stored()
                before = self.read()[1]["revision"]
                seen, spy = self.spied()
                with mock.patch.object(host, "step", spy):
                    call(True)
                doc = self.read()[1]
                self.assertEqual(doc["revision"], before + 1)
                # The row of events is one write: the state after the last of them.
                self.assertEqual({**doc, "revision": 0}, {**seen[-1]["state"], "revision": 0})

    def test_without_write_the_comments_and_the_revision_stay(self):
        for name, setup, call in self.calls():
            with self.subTest(decision=name):
                self.setUp()
                setup()
                self.stored()
                before = copy.deepcopy(self.gh.comments(5))
                call(False)
                self.assertEqual(self.gh.comments(5), before)
                self.assertEqual(self.read()[1]["revision"], 1)

    def test_a_due_tick_of_a_lost_command_is_a_second_answer_stored_after_the_first(self):
        self.stored()
        before = self.read()[1]["revision"]
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            # Due: the loss is answered with a wait, and its tick, named by that wait, starts it.
            step = self.lost(write=True, now=self.NOW + timedelta(hours=5))
        self.assertEqual((step["state"], step["command"]), ("start", "go"))
        self.assertEqual([found["state"]["revision"] for found in seen], [before + 1, before + 2])
        doc = self.read()[1]
        self.assertEqual(doc, seen[-1]["state"])
        self.assertEqual([w for w in doc["waits"]], [])

    def test_a_decision_with_no_state_comment_stores_nothing(self):
        self.blocked()
        self.ends()
        before = copy.deepcopy(self.gh.comments(5))
        self.after_go(write=True)
        self.assertEqual(self.gh.comments(5), before)
        self.assertEqual(self.read(), (None, None))

    def test_a_comment_after_the_round_is_stored_with_its_result_as_one_answer(self):
        # A round with nothing to answer waits for a person, until the comment plans it again.
        self.going(outcome="agent_blocked", attention="Secret.")
        self.stored()
        self.gh.human_comment(5, "Keep it, but only for a day.")
        before = self.read()[1]["revision"]
        seen, spy = self.spied()
        with mock.patch.object(host, "step", spy):
            self.after_go(write=True)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.read()[1]["revision"], before + 1)

    def test_the_sweep_does_not_send_the_result_that_report_stored_again(self):
        self.blocked()
        self.stored()
        before = self.read()[1]
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(self.runs)}):
            self.assertTrue(
                host.round_carried_on(
                    self.gh, self.cfg, self.gh.issues[5], self.GO, self.agent(), write=True
                )
            )
        stored = self.read()[1]
        self.assertEqual(stored["revision"], before["revision"] + 1)
        self.assertIn(f"run-{self.runs}", stored["recentEvents"])
        sent = []
        real = host.step

        def spy(state, event, found_policies):
            sent.append(event["kind"])
            return real(state, event, found_policies)

        with mock.patch.object(host, "step", spy):
            step = self.after_go(write=True)
        # Nothing reaches the engine, and the step is the answer that `report` stored.
        self.assertEqual(sent, [])
        self.assertEqual(self.read()[1], stored)
        self.assertEqual((step["state"], step["command"]), ("start", "go"))
        self.assertEqual(step["answers"], [{"question": ASKED["question"], "answer": "Keep it"}])

    def agent(self):
        return {"status": "blocked", "output": {"questions": [{**ASKED, "closes": 0}]}}

    def test_an_edited_state_comment_refuses_each_decision(self):
        for name, setup, call in self.calls():
            with self.subTest(decision=name):
                self.setUp()
                setup()
                self.stored()
                comment, _ = self.read()
                comment["editors"] = [BOT_LOGIN, "mallory"]
                with self.assertRaises(host.Refused):
                    call(True)

    def test_a_run_paused_at_a_usage_limit_that_the_step_starts_clears_its_wait(self):
        self.started()
        self.stored()
        until = host.stage_limited(self.gh, self.cfg, self.PLAN, "2026-10-11T04:40:00Z", write=True)
        self.assertEqual(until, "2026-10-11T04:40:00Z")
        waiting = self.read()[1]
        [wait] = waiting["waits"]
        self.assertEqual(wait["for"], "usageLimit")
        # `report` shows the pause on the run's comment, with the record that resumes it.
        comment, run = [(c, r) for c, r in run_comments(self.gh.comments(5)) if "url" in r][-1]
        run = {**run, "status": "paused", "resume": {**self.resume_record(), "auto": "resume"}}
        self.gh.update_comment(comment["id"], render_run(run, "⏸️ Paused."))
        lines = maintain.resume_one(
            self.gh, self.cfg, 5, comment["id"], "nexkit.yml", "main", now=self.NOW
        )
        self.assertEqual(lines, ["Resumed /nexkit plan on #5."])
        doc = self.read()[1]
        self.assertEqual([w for w in doc["waits"] if w["for"] == "usageLimit"], [])
        # The wait is over; the engine's `wait` notice stays in the outbox, which only a
        # run of work reads. Exactly one agent run is on its way.
        runs = [(a["kind"], a["stage"]) for a in doc["pending"] if a["kind"] == "runAgent"]
        self.assertEqual(runs, [("runAgent", "plan")])
        self.assertEqual(doc["revision"], waiting["revision"] + 1)

    def test_a_third_transient_stop_counts_from_the_stored_runs_again(self):
        self.cfg = pilot(answer="recommended", labels={"on-hold": {"hold": True}})
        self.cfg["retry"]["wait_minutes"] = [15, 30]
        self.started(action="implement", retry=2)
        self.stored()
        self.assertEqual(self.read()[1]["again"], {"transient": 2})
        # The run's own input lost its count: the stored one decides.
        self.assertNotIn("retry", self.GO)
        found = host.stage_transient(self.gh, self.cfg, self.GO, self.NOW, "Overloaded", write=True)
        self.assertEqual(found, ("failed",))
        # An implement round with no change ends for a person as a round that failed.
        self.assertEqual(self.read()[1]["attention"]["reason"], "roundFailed")

    def test_a_resume_of_a_round_on_a_pull_request_decides_on_its_issue(self):
        # The paused round's comment is on the pull request, but the state is the issue's.
        self.cfg["retry"]["wait_minutes"] = [15]
        self.gh.add_pull(6, 5, head_sha=HEAD)
        self.started(action="fix", pr=6, target=6, head=HEAD)
        self.stored()
        fix = {"issue": 5, "pr": 6, "action": "fix", "head": HEAD}
        found = host.stage_transient(self.gh, self.cfg, fix, self.NOW, "Overloaded", write=True)
        self.assertEqual(found[0], "later")
        waiting = self.read()[1]
        self.assertEqual([w["for"] for w in waiting["waits"]], ["time"])
        record = {"command": "fix", "number": 6, "at": found[1], "retry": 1}
        self.assertTrue(
            host.resumes(self.gh, self.cfg, {"resume": record}, 6, self.NOW, write=True)
        )
        doc = self.read()[1]
        self.assertEqual(doc["waits"], [])
        runs = [(a["kind"], a["stage"]) for a in doc["pending"] if a["kind"] == "runAgent"]
        self.assertEqual(runs, [("runAgent", "fix")])

    def test_one_issue_resumes_twice_on_the_stored_state(self):
        # The second wait of an issue gets its own tick: the engine ignores an id that it holds.
        self.cfg["retry"]["wait_minutes"] = [15, 30]
        self.started()
        self.stored()
        stop = host.stage_transient
        first = stop(self.gh, self.cfg, self.PLAN, self.NOW, "Overloaded", write=True)
        self.assertEqual(first[0], "later")
        record = {"command": "plan", "number": 5, "at": first[1], "retry": 1}
        resume = {"resume": record}
        self.assertTrue(host.resumes(self.gh, self.cfg, resume, 5, self.NOW, write=True))
        self.assertEqual(self.read()[1]["waits"], [])
        later = self.NOW + timedelta(minutes=20)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "8"}):
            second = host.stage_transient(
                self.gh, self.cfg, {**self.PLAN, "retry": 1}, later, "Overloaded", write=True
            )
        self.assertEqual(second[0], "later")
        self.assertNotEqual(second[1], first[1])
        record = {"command": "plan", "number": 5, "at": second[1], "retry": 2}
        self.assertTrue(host.resumes(self.gh, self.cfg, {"resume": record}, 5, later, write=True))
        doc = self.read()[1]
        self.assertEqual(doc["waits"], [])
        runs = [(a["kind"], a["stage"]) for a in doc["pending"] if a["kind"] == "runAgent"]
        self.assertEqual(runs, [("runAgent", "plan")])

    def test_the_resume_run_without_a_configuration_raises_and_stores_nothing(self):
        self.stored()
        self.paused()
        before = copy.deepcopy(self.gh.comments(5))
        with self.assertRaises(host.Refused):
            host.resumes(self.gh, None, {"resume": self.resume_record()}, 5, self.NOW)
        self.assertEqual(self.gh.comments(5), before)

    def hold(self):
        self.gh.issues[5].update(labels=[{"name": "on-hold"}], milestone={"number": 1})
        self.gh.add_milestone(1)

    def hold_kinds(self, seeded_held):
        """The kinds of the events that a usage limit sends, when the item's state is stored
        with the hold label already on the issue, or before the label came."""
        self.started()
        if seeded_held:
            self.hold()
        self.stored()
        if not seeded_held:
            self.hold()
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            host.stage_limited(
                self.gh, self.cfg, self.PLAN, "2026-10-11T04:40:00Z", label="on-hold", write=True
            )
        return [call.args[0]["event"]["kind"] for call in answer.call_args_list]

    def test_a_stored_state_that_is_held_gets_no_held_event_before_the_stop(self):
        self.assertEqual(self.hold_kinds(True), ["actionFinished"])

    def test_a_state_stored_before_the_label_came_gets_held_first(self):
        self.assertEqual(self.hold_kinds(False), ["held", "actionFinished"])
        self.assertTrue(self.read()[1]["hold"])
