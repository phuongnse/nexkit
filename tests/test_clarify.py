import unittest
from copy import deepcopy

from nexkit.clarify import prepare, publish
from nexkit.cli import SPEC_MARKER
from nexkit.common import Blocked
from nexkit.policy import now, reserve, spec_hash
from tests.support import FakeGitHub, agent, approve


class RequirementGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.work["body"] = (
            "<!-- nexkit:request:original -->\n## Original request\n\n> Signed sum"
            + SPEC_MARKER
            + "Clarification pending."
        )
        self.discussion = []

    def api(self, path, method="GET", data=None, **kwargs):
        if path.endswith("/issues/1") and method == "PATCH":
            self.work.update(data, last_edited_at=now())
            return deepcopy(self.work)
        return super().api(path, method, data, **kwargs)

    def comment(self, number, body):
        self.messages.append(body)
        self.discussion.append(
            {
                "id": len(self.discussion) + 20,
                "body": body,
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "created_at": now(),
                "updated_at": now(),
            }
        )

    def answer(self, text, login="owner"):
        comment = approve(self.work, number=len(self.discussion) + 30, login=login)
        comment["body"] = text
        self.discussion.append(comment)
        return {"comment": comment}


def result(context, questions=None):
    value = agent("request")
    value.update(
        reply="The specification is ready for review. The CLI will handle signed integers.",
        specification="## Goal\n\nSum signed integers.\n\n## Acceptance\n\n- CLI -2 -3 prints -5.\n- No arguments print 0.",
        questions=questions or [],
        ready_for_approval=not questions,
    )
    return {
        "run_key": context["run_key"],
        "input": context["input"],
        "unchanged": True,
        "result": value,
    }


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        self.gh = RequirementGitHub()

    def prepare(self, key="100.1", event=None):
        return prepare(self.gh, 1, key, "a" * 40, event or {})

    def test_github_clarification_preserves_original_and_waits_without_implementation(self):
        context = self.prepare()
        self.assertTrue(context["ready"])
        outcome = publish(self.gh, context, result(context))
        self.assertEqual(outcome["status"], "awaiting_approval")
        self.assertIn("> Signed sum", self.gh.work["body"])
        self.assertIn("/nexkit approve ", self.gh.messages[-1])
        self.assertEqual(self.gh.merges, [])
        self.assertIsNone(self.gh.pr)
        again = self.prepare("101.1")
        self.assertFalse(again["ready"])
        self.assertEqual(self.gh.state["agent_calls"], 1)

    def test_human_answer_starts_fresh_bounded_session(self):
        first = self.prepare()
        publish(self.gh, first, result(first, ["Should no arguments print zero?"]))
        self.assertNotIn("/nexkit approve ", self.gh.messages[-1])
        event = self.gh.answer("Yes, print zero.")
        second = self.prepare("101.1", event)
        self.assertTrue(second["ready"])
        self.assertEqual(second["answers"][-1]["body"], "Yes, print zero.")
        self.assertEqual(self.gh.state["agent_calls"], 2)
        self.assertEqual(second["pending_questions"], ["Should no arguments print zero?"])
        publish(self.gh, second, result(second))

    def test_short_answer_keeps_the_previous_question_meaning(self):
        first = self.prepare()
        question = "Choose invalid-input behavior: A reports an error; B skips invalid values."
        publish(self.gh, first, result(first, [question]))
        second = self.prepare("101.1", self.gh.answer("B"))
        self.assertEqual(second["answers"][-1]["body"], "B")
        self.assertEqual(second["pending_questions"], [question])

    def test_approval_received_during_agent_run_prevents_spec_mutation(self):
        context = self.prepare()
        original = self.gh.work["body"]
        self.gh.discussion.append(approve(self.gh.work))
        with self.assertRaisesRegex(Blocked, "approved"):
            publish(self.gh, context, result(context))
        self.assertEqual(self.gh.work["body"], original)

    def test_new_answer_and_changed_source_output_are_rejected(self):
        context = self.prepare()
        self.gh.answer("Additional constraint")
        with self.assertRaisesRegex(Blocked, "New requirement answers"):
            publish(self.gh, context, result(context))
        context = self.prepare("101.1")
        invalid = result(context)
        invalid["unchanged"] = False
        with self.assertRaisesRegex(Blocked, "changed the source"):
            publish(self.gh, context, invalid)

    def test_unauthorized_comments_cannot_spend_model_budget(self):
        event = self.gh.answer("Change the product", login="outsider")
        self.assertFalse(self.prepare(event=event)["ready"])
        self.assertNotIn("agent_calls", self.gh.state)

    def test_attempt_budget_survives_events_and_runs(self):
        self.gh.cfg["limits"]["attempts"] = 1
        first = self.prepare()
        publish(self.gh, first, result(first, ["Choose behavior?"]))
        event = self.gh.answer("Chosen")
        second = self.prepare("101.1", event)
        self.assertFalse(second["ready"])
        self.assertIn("attempts exhausted", second["reason"])
        self.assertEqual(self.gh.state["agent_calls"], 1)

    def test_retry_recovers_missing_question_comment_without_new_model_call(self):
        context = self.prepare()
        original_comment = self.gh.comment
        self.gh.comment = lambda *_: (_ for _ in ()).throw(Blocked("Connection interrupted"))
        with self.assertRaises(Blocked):
            publish(self.gh, context, result(context, ["Choose behavior?"]))
        self.gh.comment = original_comment
        self.assertFalse(self.prepare("101.1")["ready"])
        self.assertIn("Choose behavior?", self.gh.messages[-1])
        self.assertEqual(self.gh.state["agent_calls"], 1)

    def test_reply_answers_a_question_without_editing_an_unchanged_spec(self):
        first = self.prepare()
        publish(self.gh, first, result(first))
        before = deepcopy(self.gh.work)
        second = self.prepare("101.1", self.gh.answer("Why use arbitrary precision?"))
        output = result(second)
        output["result"]["reply"] = "It preserves exact sums above the safe integer range."
        publish(self.gh, second, output)
        self.assertEqual(self.gh.work, before)
        self.assertIn(output["result"]["reply"], self.gh.messages[-1])
        self.assertIn("/nexkit approve " + spec_hash(before), self.gh.messages[-1])
        third = self.prepare("102.1", self.gh.answer("Can you give an example?"))
        self.assertEqual(third["previous_reply"], output["result"]["reply"])

    def test_missing_or_oversized_reply_cannot_publish(self):
        context = self.prepare()
        for reply in (None, "", " ", "x" * 6001):
            output = result(context)
            output["result"]["reply"] = reply
            with (
                self.subTest(reply_length=len(reply or "")),
                self.assertRaisesRegex(Blocked, "reply"),
            ):
                publish(self.gh, context, output)
        self.assertEqual(self.gh.messages, [])

    def test_no_conversation_cap_does_not_spend_delivery_budget(self):
        self.gh.cfg["clarification"] = {"agent_minutes": 10}
        self.gh.cfg["limits"].update(attempts=1, agent_calls=2)
        for index in range(8):
            event = self.gh.answer(f"Explain example {index}.") if index else {}
            context = self.prepare(f"{100 + index}.1", event)
            self.assertTrue(context["ready"])
            self.assertEqual(context["agent_minutes"], 10)
            publish(self.gh, context, result(context))
        self.assertEqual(self.gh.state["clarification"]["calls"], 8)
        state = reserve(self.gh.state, self.gh.cfg, "200.1")
        self.assertEqual(state["agent_calls"], 10)
        self.assertEqual(state["delivery_calls"], 2)
        with self.assertRaises(Blocked):
            reserve(state, self.gh.cfg, "201.1")

    def test_conversation_cap_is_independent_and_survives_retries(self):
        self.gh.cfg["clarification"] = {"agent_minutes": 5, "max_calls": 1}
        first = self.prepare()
        publish(self.gh, first, result(first))
        again = self.prepare("101.1", self.gh.answer("Another question"))
        self.assertFalse(again["ready"])
        self.assertIn("attempts exhausted", again["reason"])
        self.assertEqual(self.gh.state["agent_calls"], 1)
        self.assertEqual(reserve(self.gh.state, self.gh.cfg, "200.1")["delivery_calls"], 2)

    def test_uncapped_failed_input_needs_new_human_input_to_reserve_again(self):
        self.gh.cfg["clarification"] = {"agent_minutes": 5, "max_calls": None}
        self.assertTrue(self.prepare()["ready"])
        self.gh.state["clarification"]["status"] = "blocked"
        self.assertFalse(self.prepare("101.1")["ready"])
        self.assertEqual(self.gh.state["agent_calls"], 1)
        event = self.gh.answer("/nexkit resume")
        context = self.prepare("102.1", event)
        self.assertTrue(context["ready"])
        self.assertEqual(self.gh.state["agent_calls"], 2)
        self.assertFalse(self.prepare("103.1", event)["ready"])

    def test_bots_and_outsiders_cannot_trigger_uncapped_conversation(self):
        self.gh.cfg["clarification"] = {"agent_minutes": 5}
        outsider = self.gh.answer("Continue", login="outsider")
        self.assertFalse(self.prepare(event=outsider)["ready"])
        bot = self.gh.answer("Continue")
        bot["comment"]["user"]["type"] = "Bot"
        self.assertFalse(self.prepare(event=bot)["ready"])
        self.assertNotIn("agent_calls", self.gh.state)
