"""The GitHub host's calls of the engine: what follows a plan is the engine's answer, which
autopilot and `route` carry out in NexKit 6's words, storing no state
(docs/engine.md#getting-there)."""

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

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
        return [call.args[0] for call in answer.call_args_list]


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
                "splits": True,
                "splitParts": 10,
                "splitDepth": 3,
                "retryWaits": [15, 30, 60],
            },
        )
        found = host.policies(self.cfg | {"autopilot": {**self.cfg["autopilot"], "enabled": False}})
        found = (found["planApproval"], found["answers"], found["splits"])
        self.assertEqual(found, ("person", "never", False))
        cfg = make_config(
            merge={"auto": True},
            fix={"max_auto_rounds": 5},
            conflicts={"auto_resolve": True, "max_rounds": 4},
        )
        found = host.policies(cfg)
        found = [found[name] for name in ("accept", "fixRounds", "conflicts", "conflictRounds")]
        self.assertEqual(found, ["autoWhenPassed", 5, "resolve", 4])


class AfterGoTest(HostCase):
    """What follows a `/nexkit go` that stopped as blocked with questions or protected paths
    is the engine's answer, in NexKit 6's words."""

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
        [call] = answer.call_args_list
        self.assertTrue(call.args[0]["event"]["result"]["timedOut"])
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

    def test_a_tick_the_engine_refuses_is_refused(self):
        with mock.patch.object(host, "step", side_effect=host.Refused("no")):
            with self.assertRaises(host.Refused):
                self.waited(timedelta(hours=1))


class RoundEndTest(ReportCase):
    """`report` sends every round's checks and review to the engine: a person's own pull
    request, a review that paused, failed or runs again, and a round whose change conflicts,
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

    def test_a_failed_review_waits_for_a_person_and_a_retried_one_is_the_host_s(self):
        self.candidate(review={"status": "error", "error": "Review crashed", "tokens": TOKENS})
        outcome, calls = self.answers(d=self.fix())
        self.assertEqual(outcome["outcome"], "review_failed")
        self.assertEqual(calls[-1]["event"]["result"]["error"], "Review crashed")
        transient = {"status": "error", "error": "Overloaded", "transient": True}
        self.candidate(review={**transient, "tokens": TOKENS})
        outcome, calls = self.answers(d=self.fix(), cfg=make_config(retry={"wait_minutes": [0]}))
        self.assertEqual(outcome["outcome"], "retry")
        # Only the checks: the review runs again, and its end is the next report's.
        self.assertEqual(len(calls), 1)

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
        text = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
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
    decides. A stop of publish is NexKit's own retry."""

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

    def test_a_stop_of_publish_is_nexkit_s_own_retry_and_asks_nothing(self):
        self.write("nexkit-agent", "result.json", {"status": "done", "output": {}})
        error = "git push --quiet origin failed: The requested URL returned error: 500"
        result = {"published": False, "error": error, "transient": True}
        needs = {"publish": {"outputs": {"result": json.dumps(result)}}}
        with mock.patch.object(host.engine, "answer", wraps=ANSWER) as answer:
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
        self.assertEqual(len(self.gh.dispatches), 1)
        answer.assert_not_called()

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
