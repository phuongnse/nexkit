"""The GitHub host's calls of the engine: what follows a plan is the engine's answer, which
autopilot and `route` carry out in NexKit 6's words, storing no state
(docs/engine.md#getting-there)."""

import base64
import json
from unittest import mock

from nexkit import autopilot, engine, host, item_state
from nexkit.report import TOO_LARGE
from nexkit.route import route
from nexkit.state import PLAN_MARKER, answers_marker, plan_data_record
from tests.test_autopilot import AutopilotCase, pilot

# The engine's own answer, which a test that stands in for it calls.
ANSWER = engine.answer
ASKED = {
    "question": "Keep the cache?",
    "options": ["Keep it", "Drop it", "Close the issue"],
    "recommended": 1,
    "closes": 3,
}


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

    def test_answers_asked_again_need_a_person_even_when_one_only_closes(self):
        cfg = pilot(answer="recommended")
        self.issue(5)
        closing = {"questions": [{**ASKED, "recommended": 3}]}
        self.run_comment(5, "plan", status="running")
        # Autopilot's answers then wait for a person, whom the sweep mentions itself.
        self.assertTrue(self.carries_on(5, closing, cfg))
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
                "answers": "recommended",
                "splits": True,
                "splitParts": 10,
                "splitDepth": 3,
            },
        )
        found = host.policies(self.cfg | {"autopilot": {**self.cfg["autopilot"], "enabled": False}})
        found = (found["planApproval"], found["answers"], found["splits"])
        self.assertEqual(found, ("person", "never", False))
