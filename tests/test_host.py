"""The GitHub host's calls of the engine: what follows a plan is the engine's answer, which
the sweep carries out and stores on the issue (docs/engine.md#getting-there)."""

import json

from nexkit import autopilot, first_state, host, item_state
from nexkit.route import route
from nexkit.state import PLAN_MARKER, plan_data_record
from tests.test_autopilot import AutopilotCase, pilot

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

    def stored(self, number):
        return item_state.find(self.gh.comments(number))[1]

    def step(self, number, cfg=None):
        _, steps = autopilot.preview(self.gh, cfg or self.cfg)
        return {step["number"]: step for step in steps}[number]


class AfterPlanTest(HostCase):
    def test_the_sweep_stores_the_engine_s_answer_once(self):
        self.issue(5)
        self.plan(5, {"questions": [{**ASKED, "recommended": 0}]})
        self.sweep()
        stored = self.stored(5)
        self.assertEqual(stored["state"], "awaitingApproval")
        self.assertEqual(stored["attention"]["reason"], "question")
        self.assertEqual(stored["revision"], 2)
        comment, _ = item_state.find(self.gh.comments(5))
        # The next sweep gives the same answer, so it writes nothing.
        self.sweep()
        again, _ = item_state.find(self.gh.comments(5))
        self.assertEqual(again, comment)
        self.assertNotIn("updated_at", again)

    def test_a_stored_state_is_answered_on_its_revision(self):
        self.issue(5)
        self.plan(5)
        doc = first_state.imported(self.gh.issue(5), self.gh.comments(5))
        doc["revision"] = 7
        self.gh.comment(5, item_state.render(doc))
        self.sweep()
        stored = self.stored(5)
        self.assertEqual((stored["state"], stored["revision"]), ("working", 8))
        self.assertEqual(self.started(), [("go", 5)])

    def test_status_reads_the_engine_s_answer_and_stores_nothing(self):
        self.issue(5)
        self.plan(5)
        self.assertEqual(self.step(5)["command"], "go")
        self.assertIsNone(self.stored(5))

    def test_the_engine_hears_the_plan_s_result_or_the_comment_after_it(self):
        self.issue(5)
        self.plan(5, {"questions": [ASKED]})
        request = self.step(5)["engine"]["request"]
        self.assertEqual(request["event"]["kind"], "actionFinished")
        self.assertEqual(request["state"]["state"], "planning")
        self.assertEqual(request["policies"]["planApproval"], "autoWhenClean")
        self.gh.human_comment(5, "Keep it, but only for a day.")
        request = self.step(5)["engine"]["request"]
        self.assertEqual(request["event"]["kind"], "commented")
        self.assertEqual(request["event"]["by"]["role"], "reader")
        self.gh.human_comment(5, "Close the issue")
        request = self.step(5)["engine"]["request"]
        self.assertEqual(request["event"]["kind"], "answered")
        self.assertEqual(request["event"]["answers"], [{"question": 1, "option": 3}])
        self.assertEqual(request["event"]["by"]["role"], "writer")
        self.assertEqual(request["state"]["state"], "awaitingApproval")

    def test_answers_follow_the_answers_policy(self):
        self.issue(5)
        self.plan(5, {"questions": [ASKED]})
        self.assertEqual(self.step(5)["state"], "waits")
        cfg = pilot(answer="recommended")
        step = self.step(5, cfg)
        self.assertEqual((step["state"], step["command"]), ("start", "plan"))
        self.assertEqual(step["answers"], [{"question": ASKED["question"], "answer": "Keep it"}])
        self.assertEqual(step["engine"]["request"]["policies"]["answers"], "recommended")

    def test_a_plan_from_before_its_record_is_read_by_its_run(self):
        self.issue(5)
        self.planned(5, attention="answer the questions")
        self.assertEqual(self.step(5)["reason"], "answer the questions")
        self.issue(6)
        self.planned(6)
        self.assertEqual(self.step(6)["command"], "go")

    def test_the_parts_filed_are_the_engine_s(self):
        self.issue(5)
        split = [
            {"title": "First", "body": "One.", "depends_on": []},
            {"title": "Second", "body": "Two.", "depends_on": [1]},
        ]
        self.plan(5, {"too_large": True, "split": split})
        step = self.step(5)
        self.assertEqual(step["state"], "split")
        self.assertEqual(
            step["parts"],
            [
                {"title": "First", "body": "One.", "depends_on": []},
                {"title": "Second", "body": "Two.", "depends_on": [1]},
            ],
        )

    def test_a_split_and_a_close_are_stored(self):
        self.issue(5)
        self.plan(5, {"no_change": "Run 7 passed since."})
        self.sweep()
        self.assertEqual(self.gh.issues[5]["state"], "closed")
        self.assertEqual(self.stored(5)["state"], "noChange")


class CarriesOnTest(HostCase):
    def test_a_plan_whose_answer_only_closes_counts_as_carried_on_as_in_nexkit_6(self):
        self.issue(5)
        self.run_comment(5, "plan", status="running")
        cfg = pilot(answer="recommended")
        decision = {"issue": 5, "pr": None}
        closing = {"questions": [{**ASKED, "recommended": 3}]}
        issue = self.gh.issue(5)
        self.assertTrue(host.plan_carried_on(self.gh, cfg, issue, decision, closing))
        protected = {"protected_paths": [".github/workflows/ci.yml"]}
        self.assertFalse(host.plan_carried_on(self.gh, cfg, issue, decision, protected))
        self.assertTrue(host.plan_carried_on(self.gh, cfg, issue, decision, {"questions": [ASKED]}))


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
            json.loads(json.dumps(found)),
            {
                "planApproval": "autoWhenClean",
                "answers": "recommended",
                "splits": True,
                "splitParts": 10,
                "splitDepth": 3,
            },
        )
        self.assertEqual(host.policies(self.cfg)["answers"], "never")
