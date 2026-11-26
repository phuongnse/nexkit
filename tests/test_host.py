"""The GitHub host's calls of the engine: what follows a plan is the engine's answer, which
autopilot and `route` carry out in NexKit 6's words, storing no state
(docs/engine.md#getting-there)."""

import base64
import copy
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase, mock

from nexkit import autopilot, engine, host, item_state
from nexkit import report as reports
from nexkit.checks import recorded
from nexkit.github import GitHubError
from nexkit.maintain import resume_paused
from nexkit.protocol.size import MOST
from nexkit.report import NO_CHANGE_PLAN, TOO_LARGE, needs_person, report
from nexkit.route import route
from nexkit.state import (
    PLAN_MARKER,
    answers_marker,
    empty_state,
    plan_data_record,
    questions,
    read_state,
    render_run,
    render_state,
)
from tests.support import BOT_LOGIN, FakeGitHub, make_config
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

ROOT = Path(__file__).resolve().parent.parent


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
                self.assertEqual(route(self.gh, "issue_comment", event)["task"], "close_answer")
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
        self.assertEqual(pretended["by"], {"id": "bob", "role": "reader"})
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
            self.assertEqual(route(self.gh, "issue_comment", event)["action"], "plan")
        self.assertEqual(route(self.gh, "issue_comment", event)["task"], "close_answer")


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
        issue = self.issue(5)
        event = host._event(
            "requested", 5, None, "start-5", what="start", by=None, note="", plan=None, title=""
        )
        blocker = {"id": "w1.2", "for": "blocker", "until": None, "on": "7"}
        for name, change in {
            "hold": {"hold": True},
            "blocker": {"waits": [*host.new_document(issue, {})["waits"], blocker]},
        }.items():
            with self.subTest(name):
                doc = {**host.new_document(issue, {}), **change}
                answer = host.step(doc, event, host.policies(self.cfg))
                self.assertEqual(answer["actions"], [])
                self.assertEqual(answer["state"]["state"], "new")


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


class ClosingCommandTest(HostCase):
    def setUp(self):
        super().setUp()
        self.issue(5)
        self.asked = self.plan(5, {"questions": [{**ASKED, "recommended": 0}]})

    def decide(self, body):
        return route(self.gh, "issue_comment", comment_event(5, body))

    def test_a_writer_s_closing_answer_to_the_plan_is_the_engine_s_to_close(self):
        decision = self.decide("/nexkit plan Close the issue")
        found = (decision["task"], decision["answer"])
        self.assertEqual(found, ("close_answer", "Close the issue"))
        self.assertEqual(decision["asked"], self.asked["html_url"])

    def test_an_open_pull_request_keeps_the_issue_for_a_person(self):
        self.gh.add_pull(6, 5)
        self.assertEqual(self.decide("/nexkit plan Close the issue")["action"], "plan")

    def test_another_note_goes_to_its_round(self):
        self.assertEqual(self.decide("/nexkit go Keep it")["action"], "implement")


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

    def test_lost_work_starts_again_only_with_autopilot(self):
        off = self.cfg | {"autopilot": {**self.cfg["autopilot"], "enabled": False}}
        self.assertEqual(host.policies(off)["lostWaits"], [])
        self.assertEqual(host.policies(self.cfg)["lostWaits"], [0, 30, 60, 120])


class AfterGoTest(HostCase):
    """What follows a `/nexkit go` that stopped as blocked, with questions, protected paths or
    nothing to answer, is the engine's answer, in NexKit 6's words."""

    def blocked(self, number, **fields):
        """A plan, then a `/nexkit go` that stopped as blocked with nothing to answer."""
        self.issue(number)
        self.plan(number)
        run = {"outcome": "agent_blocked", "attention": "It needs a secret.", **fields}
        return self.run_comment(number, "go", status="failure", **run)

    def since_stop(self, number):
        """NexKit 6's own reason for the issue's latest `/nexkit go`."""
        issue = self.gh.issue(number)
        comments = self.gh.comments(number)
        [stop] = [c for c in comments if c.get("id") == self.stop_id]
        return autopilot._since_stop(self.gh, issue, comments, stop)

    def stopped(self, number, asked=(), **fields):
        """A plan, then a `/nexkit go` that stopped as blocked with the questions `asked`, as
        the round's marker keeps them."""
        self.issue(number)
        self.plan(number)
        found = questions([dict(item) for item in asked])
        run = {"outcome": "agent_blocked", "questions": found, **fields}
        return self.run_comment(number, "go", status="failure", attention="Answer them.", **run)

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
        [request] = self.requests(5)
        self.assertEqual(request["event"]["kind"], "answered")
        self.assertEqual(request["event"]["answers"], [{"question": 1, "option": 3}])
        step = self.step(5)
        self.assertEqual(
            (step["state"], step["answer"], step["by"]), ("close", "Close the issue", "alice")
        )

    def test_answers_follow_the_answers_policy_once_in_a_row(self):
        cfg = pilot(answer="recommended")
        self.stopped(5, [ASKED])
        step = self.step(5, cfg)
        self.assertEqual((step["command"], step["target"]), ("go", 5))
        self.assertEqual(step["answers"], [{"question": ASKED["question"], "answer": "Keep it"}])
        self.answered(5)
        self.run_comment(
            5, "go", status="failure", outcome="agent_blocked", questions=questions([ASKED])
        )
        self.assertEqual(self.step(5, cfg)["reason"], autopilot.REPEATED)

    def test_a_comment_on_questions_asked_again_lets_the_engine_answer_them_again(self):
        cfg = pilot(answer="recommended")
        self.stopped(5, [ASKED])
        self.answered(5)
        self.run_comment(
            5, "go", status="failure", outcome="agent_blocked", questions=questions([ASKED])
        )
        self.assertEqual(self.step(5, cfg)["reason"], autopilot.REPEATED)
        self.gh.human_comment(5, "Keep it, the other one changed.")
        # A person commented since the answers: the round's result is no longer asked again.
        [request] = self.requests(5, cfg)
        self.assertNotIn("answers", request["state"]["asked"])
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
        comment = self.blocked(5)
        self.stop_id = comment["id"]
        self.gh.human_comment(5, "The secret is set now.")
        kinds = [request["event"]["kind"] for request in self.requests(5)]
        self.assertEqual(kinds, ["actionFinished", "commented"])
        step = self.step(5)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertIn(
            "collaborators commented after the stopped `/nexkit go`, so autopilot plans again",
            step["reason"],
        )
        self.assertEqual(step["reason"], self.since_stop(5))

    def test_a_go_blocked_with_nothing_to_answer_plans_again_after_a_blocker_closes(self):
        comment = self.blocked(5)
        self.stop_id = comment["id"]
        comment["created_at"] = "2026-01-02T00:00:00Z"
        self.issue(7, state="closed", reason="completed")
        self.gh.issues[7]["closed_at"] = "2026-01-03T00:00:00Z"
        self.gh.blockers[5] = [7]
        step = self.step(5)
        self.assertEqual(step["command"], "plan")
        self.assertIn("#7, which blocked it, closed after the stopped", step["reason"])
        self.assertEqual(step["reason"], self.since_stop(5))

    def test_a_blocker_that_closed_before_the_round_leaves_it_waiting(self):
        comment = self.blocked(5)
        self.stop_id = comment["id"]
        comment["created_at"] = "2026-01-04T00:00:00Z"
        self.issue(7, state="closed", reason="completed")
        self.gh.issues[7]["closed_at"] = "2026-01-03T00:00:00Z"
        self.gh.blockers[5] = [7]
        step = self.step(5)
        self.assertEqual((step["state"], step["reason"]), ("waits", "It needs a secret."))
        self.assertIsNone(self.since_stop(5))

    def test_a_reason_the_host_has_no_words_for_is_refused_for_that_issue_alone(self):
        self.stopped(5, [ASKED])
        answer = {"actions": [{"kind": "needPerson", "reason": "accept"}], "state": {}}
        with mock.patch.object(host, "step", return_value=answer):
            self.assertIn("refused to decide", self.step(5)["reason"])

    def test_the_round_carries_on_as_the_engine_answers_it(self):
        cfg = pilot(answer="recommended")
        self.issue(5)
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
        self.answered(5)
        self.run_comment(5, "go", status="running")
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
        return route(self.gh, "issue_comment", comment_event(5, body))

    def test_a_writer_s_closing_answer_to_a_round_is_the_engine_s_to_close(self):
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            decision = self.decide("/nexkit go 3")
        self.assertEqual(
            (decision["task"], decision["answer"]), ("close_answer", "Close the issue")
        )
        [call] = answer.call_args_list
        self.assertEqual(call.args[0]["event"]["kind"], "answered")
        self.assertEqual(call.args[0]["state"]["questions"]["ref"], str(self.asked["id"]))

    def test_an_open_pull_request_keeps_the_issue_for_a_person(self):
        self.gh.add_pull(6, 5)
        self.assertEqual(self.decide("/nexkit plan Close the issue")["action"], "plan")


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
        [call] = answer.call_args_list
        request = call.args[0]
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
        [request] = asked_step(answer)
        self.assertTrue(request["event"]["result"]["timedOut"])
        self.assertEqual((step["command"], step["remember"]), ("plan", {"split_asked": True}))
        self.assertEqual(step["note"], autopilot.SPLIT_NOTE)
        with mock.patch.object(host, "step", return_value={"actions": [], "state": {}}):
            self.assertIn("refused to decide", self.step(5)["reason"])

    def test_a_part_whose_parent_asked_for_a_split_waits_for_a_person(self):
        self.issue(4)
        record = {"split_asked": True, "command": "plan", "basis": [0, 0]}
        self.gh.comment(4, "NexKit autopilot record\n\n" + autopilot.record_marker(record))
        self.issue(5, body="<!-- nexkit:part 4 -->\nPart.", user=BOT_LOGIN)
        self.gh.parents[5] = 4
        self.plan(5)
        self.run_comment(5, "go", status="failure", outcome="agent_error", timed_out=True)
        [request] = self.requests(5)
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
        self.assertEqual(host.required_failed(cfg, 5, 6, "c" * 40, 1, None), "auto_fix")
        self.assertEqual(host.required_failed(cfg, 5, 6, "c" * 40, 1, wait), "ci_waiting")
        self.assertEqual(host.required_failed(cfg, 5, 6, "c" * 40, 2, wait), "needs_human")

    def test_required_checks_read_the_base_failure_only_when_a_fix_round_is_left(self):
        head = "c" * 40
        cfg = make_config(merge={"auto": True}, fix={"max_auto_rounds": 2})
        # Pull request 6 has used both automatic fix rounds, pull request 7 only one.
        self.gh.add_pull(7, 5, head_sha=head)
        for pull, used, read in ((6, 2, False), (7, 1, True)):
            with self.subTest(used=used):
                state = {"version": 2, "issue": 5, "auto_fixes": used, "feedback": None}
                self.gh.comment(pull, render_state(state))
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
        head = "c" * 40
        self.assertEqual(host.stopped_counts(5, 6, head, 2, True, True), 1)
        self.assertEqual(host.stopped_counts(5, 6, head, 2, True, True, checking=True), 1)
        # A person's round counts nothing, and an agent that stopped by itself keeps its count.
        self.assertEqual(host.stopped_counts(5, 6, head, 2, False, True), 2)
        self.assertEqual(host.stopped_counts(5, 6, head, 2, True, False), 2)


class AcceptAnswerTest(ReportCase):
    """The answer of GitHub's merge is the engine's: `accept_answer` carries out what it
    answers for the merge's result, whatever the refusal's kind."""

    HEAD = "c" * 40
    AT = "2026-11-23T15:01:11Z"

    def answer(self, result, cfg=None, used=0, **fields):
        cfg = cfg or make_config(merge={"auto": True}, fix={"max_auto_rounds": 2})
        refusal = result if isinstance(result, dict) else {"accepted": False, "refusal": result}
        refusal = {"text": "Refused.", **refusal}
        return host.accept_answer(cfg, 5, 6, self.HEAD, used, refusal, at=self.AT, **fields)

    def test_a_merge_that_went_through_is_accepted(self):
        end, state = self.answer({"accepted": True, "refusal": None})
        self.assertEqual((end, state["state"]), ("accepted", "accepted"))

    def test_required_checks_that_have_not_passed_wait_and_keep_their_first_time(self):
        end, state = self.answer("waitingForChecks")
        self.assertEqual((end, state["state"]), ("waiting", "accepting"))
        self.assertEqual(state["checksSince"], {"version": self.HEAD, "at": self.AT})
        self.assertEqual([wait["for"] for wait in state["waits"]], ["requiredChecks"])
        since = "2026-11-23T10:00:00Z"
        end, state = self.answer("waitingForChecks", since=since)
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
                with mock.patch.object(host, "step", wraps=host.step) as step:
                    end, state = self.answer("behind", resolve, updates=used, conflicts=0)
                rounds = step.call_args.args[0]["rounds"]
                self.assertEqual(rounds.get("updates", 0), used)
                self.assertEqual(rounds["conflicts"], 0)
                if used < 2:
                    self.assertEqual(end, "update")
                else:
                    reason = state["attention"]["reason"]
                    self.assertEqual((end, reason), ("person", "acceptRefused"))
        # Conflict rounds used up leave the updates their own count.
        self.assertEqual(self.answer("behind", resolve, conflicts=2)[0], "update")

    def test_an_update_that_failed_is_a_person_s(self):
        resolve = make_config(merge={"auto": True}, conflicts={"auto_resolve": True})
        with mock.patch.object(host, "step", wraps=host.step) as step:
            end = host.update_failed(resolve, 5, 6, self.HEAD, 0, "It failed.", 1)
        self.assertEqual(end, "person")
        state, event = step.call_args.args[:2]
        self.assertEqual(state["rounds"]["updates"], 1)
        failed = {"change": None, "refused": "It failed.", "observed": None}
        self.assertEqual(event["result"], failed)
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                host.update_failed(resolve, 5, 6, self.HEAD, 0, "It failed.")

    def test_a_merge_that_was_not_accepted_fails_the_item(self):
        end, state = self.answer("notAccepted")
        self.assertEqual((end, state["state"]), ("person", "failed"))
        self.assertEqual(state["attention"]["reason"], "failed")

    def test_a_required_check_that_failed_follows_the_rounds_left(self):
        wait = {"accepted": False, "refusal": "checkFailed", "baseFailure": "77"}
        self.assertEqual(self.answer("checkFailed", used=1)[0], "fix")
        self.assertEqual(self.answer(wait, used=1)[0], "base_failure")
        end, state = self.answer("checkFailed", used=2)
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
                with mock.patch.object(host, "step", wraps=host.step) as step:
                    end, state = self.answer("conflict", resolve, conflicts=used)
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


class ChecksWaitedTest(ReportCase):
    """A sweep's tick of a merge that waits for required checks: the six hours are the
    engine's."""

    def waited(self, hours):
        since = datetime(2026, 11, 23, 15, 0, 0, tzinfo=UTC)
        cfg = make_config(merge={"auto": True})
        return host.checks_waited(cfg, 5, 6, "c" * 40, since, since + hours)

    def test_the_merge_is_tried_again_for_six_hours_and_then_a_person_looks(self):
        self.assertEqual(self.waited(timedelta(hours=1)), "merge")
        self.assertEqual(self.waited(timedelta(hours=6)), "merge")
        self.assertEqual(self.waited(timedelta(hours=6, seconds=1)), "waits")

    def test_a_branch_behind_its_base_keeps_its_merge_when_conflicts_are_resolved(self):
        since = datetime(2026, 11, 23, 15, 0, 0, tzinfo=UTC)
        late = since + timedelta(hours=7)
        for resolve, expected in ((True, "merge"), (False, "waits")):
            with self.subTest(resolve=resolve):
                conflicts = {"auto_resolve": resolve}
                cfg = make_config(merge={"auto": True}, conflicts=conflicts)
                with mock.patch.object(host, "step", wraps=host.step) as step:
                    end = host.checks_waited(cfg, 5, 6, "c" * 40, since, late, behind=True)
                self.assertEqual(end, expected)
                self.assertTrue(step.call_args.args[0]["observed"]["behind"])
        with mock.patch.object(host, "step", wraps=host.step) as step:
            host.checks_waited(cfg, 5, 6, "c" * 40, since, late)
        self.assertNotIn("behind", step.call_args.args[0]["observed"])

    def test_a_tick_the_engine_refuses_is_refused(self):
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                self.waited(timedelta(hours=1))


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
        self.run_report(d=d)
        return read_state(self.gh.comments(6))[1]["feedback"].get("repeated")

    def fix(self, **kw):
        return decision("fix", pr=6, target=6, head=HEAD, **kw)

    def test_checks_past_the_thirtieth_that_fail_the_same_way_are_found(self):
        repeated = self.round_with(range(40), (32, 33, 34, 35), self.fix(auto=True))
        self.assertEqual(repeated, ["c032", "c033", "c034", "c035"])
        event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": "true"}}
        found = route(self.gh, "workflow_dispatch", event, event["inputs"])
        self.assertEqual(found["diagnose"], ["c032", "c033", "c034", "c035"])

    def test_a_diagnosis_round_looks_into_the_first_ten_of_more(self):
        repeated = self.round_with(range(40), range(5, 40), self.fix(auto=True))
        self.assertEqual(repeated, [f"c{n:03}" for n in range(5, 40)])
        event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": "true"}}
        found = route(self.gh, "workflow_dispatch", event, event["inputs"])
        self.assertEqual(found["diagnose"], [f"c{n:03}" for n in range(5, 15)])

    def test_a_person_s_fix_round_s_checks_compare_too(self):
        repeated = self.round_with((3,), (3,), self.fix())
        self.assertEqual(repeated, ["c003"])

    def route_diagnoses(self):
        event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": "true"}}
        return route(self.gh, "workflow_dispatch", event, event["inputs"]).get("diagnose")

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
        self.assertEqual(
            host.conflicting(cfg, decision("fix", head="b"), 6, HEAD, None, 1), "round"
        )
        self.assertEqual(
            host.conflicting(cfg, decision("fix", head="b"), 6, HEAD, None, 2), "used_up"
        )
        self.assertEqual(host.conflicting(cfg, decision(), 6, HEAD, "on-hold", 2), "held")
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
        ends = host.conflicts_released
        self.assertEqual(ends(cfg, 5, 6, run, HEAD, True, 1), "round")
        self.assertEqual(ends(cfg, 5, 6, run, HEAD, True, 2), "waits")
        self.assertEqual(ends(cfg, 5, 6, run, HEAD, False, 2), "review")
        # A head that moved is checked, and the round after it finds any conflict.
        self.assertEqual(ends(cfg, 5, 6, run, "d" * 40, True, 0), "review")
        off = make_config(conflicts={"auto_resolve": False})
        self.assertEqual(ends(off, 5, 6, run, HEAD, True, 0), "waits")


class DiagnosisTest(HostCase):
    """The fix round that `route` starts diagnoses what the engine names."""

    def test_only_an_automatic_round_diagnoses_the_repeated_failures(self):
        self.assertEqual(host.diagnosis(5, 6, "c" * 40, True, ["test"]), ["test"])
        self.assertEqual(host.diagnosis(5, 6, "c" * 40, False, ["test"]), [])
        self.assertEqual(host.diagnosis(5, 6, "c" * 40, True, []), [])

    def test_route_takes_the_diagnosis_from_the_engine(self):
        self.gh.add_pull(6, 5, head_sha="c" * 40)
        state = {**empty_state(5), "feedback": {"head": "c" * 40, "repeated": ["test"]}}
        self.gh.comment(6, render_state(state))
        found = {}
        for auto in ("true", "conflicts", "false"):
            event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": auto}}
            with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
                found[auto] = route(self.gh, "workflow_dispatch", event, event["inputs"])
            self.assertEqual(len(answer.call_args_list), 1)
        self.assertEqual(found["true"]["diagnose"], ["test"])
        self.assertNotIn("diagnose", found["conflicts"])
        self.assertNotIn("diagnose", found["false"])
        with mock.patch.object(host, "diagnosis", side_effect=host.Refused("No.")):
            event = {"inputs": {"command": "fix", "number": "6", "note": "", "auto": "true"}}
            self.assertNotIn(
                "diagnose", route(self.gh, "workflow_dispatch", event, event["inputs"])
            )


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
            outcome = self.run_report(d=d or decision("plan"), cfg=cfg, published=False)
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
            outcome = self.run_report(d=decision("plan"), cfg=ONCE, published=False)
        self.assertEqual(outcome["outcome"], "retry")
        [call] = answer.call_args_list
        self.assertTrue(call.args[0]["event"]["result"]["transient"])

    def publish_stop(self, d, cfg):
        self.write("nexkit-agent", "result.json", {"status": "done", "output": {}})
        error = "git push --quiet origin failed: The requested URL returned error: 500"
        result = {"published": False, "error": error, "transient": True}
        needs = {"publish": {"outputs": {"result": json.dumps(result)}}}
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

    def test_a_hold_is_sent_first_and_changes_nothing_the_engine_answers(self):
        self.hold()
        for result, outcome in ((LIMIT, "paused"), (self.TRANSIENT, "retry")):
            with self.subTest(outcome=outcome):
                found, requests, _ = self.stop(result, cfg=self.HOLD)
                self.assertEqual(found["outcome"], outcome)
                self.assertEqual([r["event"]["kind"] for r in requests], ["held", "actionFinished"])
                self.assertEqual(requests[0]["event"]["label"], "on-hold")

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


class ResumeTest(ReportCase):
    """`maintain.resume_paused` sends the tick of each due resume to the engine, a hold on the
    issue included, and starts the command when the engine runs it."""

    NOW = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)

    def paused(self, command="plan", at="2026-10-11T04:40:00Z", **resume):
        record = {"command": command, "number": 5, "note": "", "auto": "resume", "at": at}
        run = {"url": "https://x/runs/1", "command": command, "status": "paused"}
        run["resume"] = {**record, **resume}
        return self.gh.comment(5, render_run(run, "⏸️ Paused."))

    def resume(self, cfg=None):
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
            lines, _ = resume_paused(self.gh, "nexkit.yml", "main", self.NOW, cfg)
        return lines, [call.args[0] for call in answer.call_args_list]

    def test_a_run_paused_under_a_hold_starts_again_at_its_time(self):
        self.gh.issues[5].update(labels=[{"name": "on-hold"}], milestone={"number": 1})
        self.gh.add_milestone(1)
        cfg = pilot(labels={"on-hold": {"hold": True}})
        self.paused()
        lines, requests = self.resume(cfg)
        self.assertEqual(lines, ["Resumed /nexkit plan on #5."])
        self.assertEqual([r["event"]["kind"] for r in requests], ["held", "tick"])
        self.assertEqual(requests[1]["state"]["waits"][0]["for"], "usageLimit")
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
                self.gh.dispatches.clear()
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
    with, or checks: `change_seen` sends it and never fakes the merge's result."""

    HEAD = "c" * 40

    def seen(self, merged=False, empty=False, version=None, waiting=False, cfg=None):
        cfg = cfg or make_config(merge={"auto": True})
        seen = {"version": version or self.HEAD, "merged": merged, "empty": empty}
        return host.change_seen(cfg, 5, 6, self.HEAD, 0, seen, waiting=waiting)

    def test_a_pull_request_a_person_merged_ends_the_item_as_accepted(self):
        self.assertEqual(self.seen(merged=True), "accepted")
        self.assertEqual(self.seen(merged=True, waiting=True), "accepted")

    def test_a_change_the_base_branch_holds_ends_the_item_without_a_change(self):
        self.assertEqual(self.seen(empty=True), "noChange")
        self.assertEqual(self.seen(empty=True, waiting=True), "noChange")

    def test_a_push_while_the_merge_waits_is_checked(self):
        self.assertEqual(self.seen(version="d" * 40), "review")
        self.assertEqual(self.seen(version="d" * 40, waiting=True), "review")

    def test_the_merge_sends_an_observation_and_no_result(self):
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.seen(merged=True)
        [event] = [call.args[1] for call in step.call_args_list]
        self.assertEqual(event["kind"], "changeObserved")
        self.assertEqual(
            (event["changeId"], event["merged"], event["conflicts"]), ("6", True, False)
        )

    def test_an_observation_that_ends_nothing_and_changes_nothing_is_refused(self):
        with self.assertRaises(host.Refused):
            self.seen()
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                self.seen(merged=True)


class PartEndedTest(ReportCase):
    """A part that NexKit merged ends its parent only as the engine decides, under `parentEnd`:
    `close_parent_issues` on gives `whenPartAccepted`, off gives `person`."""

    def subs(self, *states):
        """The sub-issues of the parent 1: issue 5, and one for each of `states`."""
        found = [{"number": 5, "state": "closed", "state_reason": "completed"}]
        for number, (state, reason) in enumerate(states, 20):
            found.append({"number": number, "state": state, "state_reason": reason})
        return found

    def ended(self, *states, close=True, subs=None):
        cfg = make_config(close_parent_issues=close)
        return host.part_ended(cfg, 1, subs or self.subs(*states), 5, "accepted")

    def test_the_parent_closes_when_every_part_ended_and_one_was_completed(self):
        self.assertEqual(self.ended(), "close")
        self.assertEqual(self.ended(("closed", "not_planned")), "close")
        self.assertEqual(self.ended(("closed", "completed")), "close")

    def test_the_parent_stays_while_a_part_is_open(self):
        self.assertEqual(self.ended(("open", None)), "stays")

    def test_the_parent_stays_when_no_part_was_completed(self):
        subs = [{"number": 5, "state": "closed", "state_reason": "completed"}]
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.assertEqual(host.part_ended(make_config(), 1, subs, 7, "dropped"), "stays")
        self.assertEqual(step.call_args.args[1]["part"], "7")

    def test_without_close_parent_issues_the_engine_gets_person_and_the_parent_stays(self):
        with mock.patch.object(host, "step", wraps=host.step) as step:
            self.assertEqual(self.ended(close=False), "stays")
            self.assertEqual(step.call_args.args[2]["parentEnd"], "person")
            self.assertEqual(self.ended(close=True), "close")
            self.assertEqual(step.call_args.args[2]["parentEnd"], "whenPartAccepted")

    def test_a_list_that_misses_the_part_still_counts_it(self):
        self.assertEqual(self.ended(subs=[]), "close")

    def test_a_parent_with_more_parts_than_a_state_holds_is_refused(self):
        many = [
            {"number": n, "state": "closed", "state_reason": "completed"}
            for n in range(5, 5 + MOST["split"] + 1)
        ]
        with self.assertRaises(host.Refused):
            self.ended(subs=many)
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                self.ended()

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
        self.assertEqual(seen.call_args.args[5], {"version": HEAD, "merged": True, "empty": False})
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
        self.assertTrue(seen.call_args.args[5]["merged"])

    def test_a_change_the_base_branch_holds_is_closed_only_when_the_engine_ends_it(self):
        self.gh.on_base.add(HEAD)
        with mock.patch.object(host, "change_seen", wraps=host.change_seen) as seen:
            outcome = self.merge()
        self.assertEqual(outcome["outcome"], "already_on_base")
        self.assertEqual(seen.call_args.args[5], {"version": HEAD, "merged": False, "empty": True})
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
        self.assertEqual(ended.call_args.args[1:], (1, ended.call_args.args[2], 5, "accepted"))
        self.gh.reopen(6, 5)
        self.gh.pulls[6]["state"] = "open"
        with mock.patch.object(host, "part_ended", return_value="close"):
            self.assertEqual(self.merge(cfg)["outcome"], "merged")
        self.assertEqual(self.gh.issues[1]["state"], "closed")

    def test_without_close_parent_issues_the_engine_gets_person_and_the_parent_stays(self):
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

    def test_a_parent_with_more_sub_issues_than_a_state_holds_keeps_nexkit_6s_path(self):
        cfg = make_config(merge={"auto": True}, close_parent_issues=True)
        self.parent(*[(n, "completed") for n in range(20, 20 + MOST["split"])])
        with mock.patch.object(host, "part_ended") as ended:
            self.assertEqual(self.merge(cfg)["outcome"], "merged")
        ended.assert_not_called()
        self.assertEqual(self.gh.issues[1]["state"], "closed")


class MergeStepTest(ReportCase):
    """The sweep's tick of a merge that waits for required checks reviews a head that a person
    pushed meanwhile only when the engine answers that it checks it."""

    def step(self):
        self.gh.pulls[6]["head"]["sha"] = "d" * 40
        comment = {"id": 77}
        run = {"head": HEAD, "waiting_since": autopilot.iso(datetime.now(UTC))}
        cfg = make_config(merge={"auto": True})
        now = datetime.now(UTC)
        return autopilot._merge_step(self.gh, cfg, {"number": 5}, 6, comment, run, {}, now)

    def test_a_head_pushed_while_the_merge_waited_is_reviewed_when_the_engine_checks_it(self):
        with mock.patch.object(host, "change_seen", wraps=host.change_seen) as seen:
            step = self.step()
        self.assertEqual((step["state"], step["command"]), ("start", "review"))
        self.assertEqual(seen.call_args.args[5]["version"], "d" * 40)
        self.assertTrue(seen.call_args.kwargs["waiting"])

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

    def lost(self, command="plan", target=5, tries=0, now=None, at=None, **kwargs):
        record = {"command": command, "target": target, "at": at or self.AT, "tries": tries}
        record.update(kwargs.pop("record", {}))
        fields = {"command": command, "target": target, "note": "", "remember": {}}
        fields.update(kwargs.pop("fields", {}))
        now = now or self.NOW
        with mock.patch.object(host, "step", wraps=host.step) as sent:
            found = host.lost_again(self.CFG, self.ISSUE, record, now, self.WHAT, fields, **kwargs)
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
        found = autopilot._again(self.CFG, self.ISSUE, {}, self.NOW, self.WHAT, fields)
        self.assertEqual(found["state"], "waits")
        self.assertIn("refused to decide", found["reason"])
