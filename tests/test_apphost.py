"""The application host of `tests/apphost.py`, played over HTTP against `nexkit-engine serve`.

It shows that a host other than GitHub carries a request from its plan to its end, or to a wait
for a person, through the protocol alone: no git, pull requests or workflow runs. Each scenario
asserts the request's final state and attention reason.
"""

import ast
import contextlib
import sys
import unittest
from pathlib import Path

from tests import apphost
from tests.apphost import Engine, Host, ScriptedRunner

ITEM = "r1"
TITLE = "Add a reminder"
BODY = "Remind a person a day before a draft is due."
PLAN = {(ITEM, "plan"): ["@plan"]}
IMPLEMENT = {(ITEM, "implement"): ["@implement"]}
REVIEW = {(ITEM, "review"): ["@review"]}
# The run that ends a request: the fixtures hold each stage's agent output.
SPENT = {"input": 190000, "cached": 0, "output": 20000}


class ApplicationHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = contextlib.ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.engine = Engine(cls.stack.enter_context(apphost.serve()))

    def host(self, scripts, checks=()):
        host = Host(self.engine, ScriptedRunner(scripts), checks)
        return host

    def start(self, scripts, checks=()):
        """A host whose request was sent, planned and approved by its person."""
        host = self.host(scripts, checks)
        host.submit(ITEM, TITLE, BODY)
        self.assertEqual(host.state_of(ITEM)["state"], "awaitingApproval")
        self.assertEqual(host.state_of(ITEM)["attention"]["reason"], "approval")
        host.approve_plan(ITEM)
        return host

    def assert_ended(self, host, state, reason=None):
        row = host.rows[ITEM]
        self.assertEqual(row.state["state"], state)
        self.assertEqual((row.state["attention"] or {}).get("reason"), reason)
        self.assertEqual(row.running, {})

    def test_a_plan_is_approved_and_its_change_is_accepted_by_itself(self):
        host = self.start({**PLAN, **IMPLEMENT, **REVIEW}, [True])
        self.assert_ended(host, "accepted")
        self.assertTrue(host.rows[ITEM].closed)
        self.assertTrue({"runAgent", "runChecks", "publish", "accept", "close"} <= host.carried)
        self.assertEqual(host.stages, [(ITEM, "plan"), (ITEM, "implement"), (ITEM, "review")])
        self.assertEqual(host.rows[ITEM].state["change"], {"id": "d-r1", "version": "r1"})

    def test_a_plans_questions_are_answered_by_the_person(self):
        host = self.host(
            {(ITEM, "plan"): ["@planQuestions", "@plan"], **IMPLEMENT, **REVIEW}, [True]
        )
        host.submit(ITEM, TITLE, BODY)
        waiting = host.state_of(ITEM)
        self.assertEqual(waiting["state"], "awaitingApproval")
        self.assertEqual(waiting["attention"]["reason"], "question")
        host.answer(ITEM)
        self.assertEqual(host.state_of(ITEM)["attention"]["reason"], "approval")
        host.approve_plan(ITEM)
        self.assert_ended(host, "accepted")
        self.assertEqual([stage for _, stage in host.stages].count("plan"), 2)

    def test_checks_fail_and_pass_after_a_fix_round_and_the_review_follows_them(self):
        scripts = {**PLAN, **IMPLEMENT, (ITEM, "fix"): ["@fix"], **REVIEW}
        host = self.start(scripts, [False, True])
        self.assert_ended(host, "accepted")
        self.assertEqual(host.checks_log, [(ITEM, False), (ITEM, True)])
        self.assertIn((ITEM, "fix"), host.stages)
        reviews = [number for number, (_, stage) in enumerate(host.stages) if stage == "review"]
        self.assertEqual(len(reviews), 1)
        # No review starts before the checks ran with every check passed.
        review = host.order.index(("agent", "review"))
        self.assertIn(("checks", True), host.order[:review])
        self.assertEqual(host.order[review - 1], ("checks", True))
        self.assertNotIn(("checks", False), host.order[review:])
        self.assertEqual(
            host.stages[: reviews[0]], [(ITEM, "plan"), (ITEM, "implement"), (ITEM, "fix")]
        )

    def test_the_fix_rounds_run_out(self):
        scripts = {**PLAN, **IMPLEMENT, (ITEM, "fix"): ["@fix"] * 3}
        host = self.start(scripts, [False] * 4)
        self.assert_ended(host, "ready", "roundsUsedUp")
        self.assertEqual([stage for _, stage in host.stages].count("fix"), 3)
        self.assertNotIn("review", [stage for _, stage in host.stages])
        self.assertFalse(host.rows[ITEM].closed)
        self.assertIn("needPerson", host.carried)

    def test_a_host_stop_fails_the_request_for_a_person(self):
        host = self.start({**PLAN, (ITEM, "implement"): ["hold"]})
        self.assertEqual(host.state_of(ITEM)["state"], "working")
        self.assertEqual(len(host.rows[ITEM].running), 1)
        host.stop(ITEM)
        self.assert_ended(host, "failed", "failed")
        self.assertTrue({"cancel", "needPerson"} <= host.carried)

    def test_the_tokens_of_a_request_are_spent(self):
        implement = {(ITEM, "implement"): [{"fixture": "implement", "tokens": SPENT}]}
        # The engine decides a budget where a run would start: the review does not.
        host = self.start({**PLAN, **implement}, [True])
        self.assert_ended(host, "failed", "failed")
        state = host.state_of(ITEM)
        self.assertEqual(state["attention"]["text"], "The work item spent its token budget.")
        self.assertEqual(state["change"], {"id": "d-r1", "version": "r1"})
        self.assertIn("needPerson", host.carried)
        self.assertNotIn("review", [stage for _, stage in host.stages])

    def test_a_drop_while_the_request_works_cancels_the_run(self):
        host = self.start({**PLAN, (ITEM, "implement"): ["hold"]})
        self.assertEqual(len(host.rows[ITEM].running), 1)
        host.drop(ITEM)
        self.assert_ended(host, "dropped")
        self.assertTrue({"cancel", "close"} <= host.carried)
        self.assertTrue(host.rows[ITEM].closed)

    def test_the_host_records_the_kinds_it_carried_out(self):
        host = self.start({**PLAN, **IMPLEMENT, **REVIEW}, [True])
        self.assertIsInstance(host.carried, set)
        self.assertEqual(
            host.carried,
            {"runAgent", "runChecks", "publish", "accept", "close", "show", "needPerson"},
        )
        # Each step stored a state that passed the check for a request that is not final.
        self.assertTrue(host.states)

    def test_a_request_with_nothing_to_do_is_refused(self):
        host = self.host({})
        state = {
            "item": ITEM,
            "state": "working",
            "pending": [],
            "waits": [],
            "hold": False,
            "attention": None,
        }
        with self.assertRaises(AssertionError):
            host.check_rule(state)
        host.check_rule({**state, "state": "accepted"})
        host.check_rule({**state, "hold": True})

    def test_an_action_that_was_not_pending_is_refused(self):
        class Liar:
            """An engine whose first action names an id that is not pending."""

            def call(self, name, body):
                answer = engine.call(name, body)
                for action in answer.get("actions", []):
                    action["id"] = "a0.0"
                return answer

        engine = self.engine
        host = Host(Liar(), ScriptedRunner(PLAN))
        with self.assertRaises(AssertionError) as raised:
            host.submit(ITEM, TITLE, BODY)
        self.assertIn("was not pending", str(raised.exception))

    def test_imports_only_the_standard_library(self):
        tree = ast.parse(Path(apphost.__file__).read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "a relative import")
                imported.append(node.module)
        self.assertTrue(imported)
        for name in imported:
            self.assertIn(name.split(".")[0], sys.stdlib_module_names, name)

    def test_the_import_guard_refuses_what_is_not_the_standard_library(self):
        for line in ("import nexkit", "from tests import support", "from nexkit.engine import cli"):
            with self.subTest(line=line):
                node = ast.parse(line).body[0]
                name = node.names[0].name if isinstance(node, ast.Import) else node.module
                self.assertNotIn(name.split(".")[0], sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
