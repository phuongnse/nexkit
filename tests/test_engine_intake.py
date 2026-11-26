"""The engine's rules that a single step shows best: what holds a state's work back from
starting again (docs/engine-states.md#states), and what the state keeps of results and events within
its size (docs/engine.md#the-state-document)."""

import copy
import json
import unittest

from nexkit import engine, protocol
from nexkit.protocol.size import LONGEST, MOST
from tests.test_conformance import AGENT, FIXTURES, event_of

EXAMPLES = protocol.ROOT / "examples" / "state"


def example(name, **fields):
    state = json.loads((EXAMPLES / f"{name}.json").read_text())
    state.update(copy.deepcopy(fields))
    return state


def wait(number, kind, on=None):
    return {"id": f"w7.{number}", "for": kind, "until": None, "on": on}


def step(state, short, policies=None):
    event = {**event_of(short, state, 1), "item": state["item"]}
    request = {"protocol": 1, "call": "step", "state": state, "event": event}
    answer = engine.answer({**request, "policies": policies or {}})
    assert "error" not in answer, answer
    return answer["state"], [action["kind"] for action in answer["actions"]]


TICK = {"kind": "tick", "wait": "w7.1"}


class RestartTest(unittest.TestCase):
    def test_a_blocker_does_not_hold_a_round_back(self):
        state = example(
            "fixing", pending=[], waits=[wait(1, "usageLimit"), wait(2, "blocker", "41")]
        )
        self.assertEqual(step(state, TICK)[1], ["runAgent"])

    def test_a_wait_action_is_not_work_that_runs(self):
        # The accepting example has the wait action that asks for the tick still pending.
        self.assertEqual(step(example("accepting"), TICK)[1], ["accept"])

    def test_a_hold_attention_or_work_that_runs_holds_the_round_back(self):
        held = example("fixing", pending=[], waits=[wait(1, "requiredChecks")], hold=True)
        attention = {"reason": "failed", "text": "A person looks.", "ref": None}
        told = example("fixing", pending=[], waits=[wait(1, "usageLimit")], attention=attention)
        running = example("fixing", waits=[wait(1, "usageLimit")])
        for state in (held, told, running):
            new, actions = step(state, TICK)
            self.assertEqual((new["state"], actions), ("fixing", []))

    def test_an_accept_that_starts_again_follows_a_conflict_observed_meanwhile(self):
        observed = {**example("accepting")["observed"], "conflicts": True}
        state = example("accepting", pending=[], waits=[], hold=True, observed=observed)
        new, actions = step(state, {"kind": "released", "label": "on-hold"})
        self.assertEqual((new["state"], actions), ("ready", ["needPerson"]))
        self.assertEqual(new["attention"]["reason"], "acceptRefused")

    def test_a_request_to_start_starts_nothing_that_is_held_back(self):
        attention = {"reason": "failed", "text": "A person looks.", "ref": None}
        for fields in ({"attention": attention}, {"hold": True}, {}):
            pending = [] if fields else None
            state = example("checking", waits=[wait(1, "start")], **fields)
            if pending is not None:
                state["pending"] = pending
            new, actions = step(state, {"kind": "requested", "what": "start"})
            self.assertEqual(actions, [], fields)
            self.assertEqual(new["waits"], state["waits"], fields)


def finished(state, output):
    return {"kind": "actionFinished", "action": "runAgent", "result": {**AGENT, "output": output}}


class IntakeTest(unittest.TestCase):
    def test_a_review_past_the_lists_keeps_what_decides_first(self):
        review = copy.deepcopy(FIXTURES["review"])
        criterion = review["criteria"][0]
        statuses = ["met"] * 60 + ["unverifiable"]
        review["criteria"] = [{**criterion, "status": status} for status in statuses]
        finding = {"severity": "suggestion", "file": "a.py", "line": 1, "body": "x" * 1000}
        review["findings"] = [finding] * 40 + [{**finding, "severity": "blocking"}]
        review["verdict"] = "request_changes"
        state = example("reviewing")
        new, _ = step(state, finished(state, review))
        rounds = new["rounds"]
        self.assertEqual(len(rounds["criteria"]), MOST["criteria"])
        self.assertEqual(rounds["criteria"][0], "unverifiable")
        self.assertEqual(len(rounds["findings"]), MOST["findings"])
        self.assertEqual(rounds["findings"][0]["severity"], "blocking")
        self.assertEqual(len(rounds["findings"][1]["text"]), LONGEST["finding"] + 3)

    def test_more_parts_than_a_split_keeps_are_cut(self):
        plan = {"text": {"name": "plan", "ref": "a6.1"}}
        state = example("split")
        action = {"id": "a7.1", "kind": "createParts", "next": None, "parts": []}
        state = {
            **state,
            "pending": [action],
            "plan": {**example("awaitingApproval")["plan"], **plan},
        }
        result = {"parts": [{"key": n, "item": str(100 + n)} for n in range(1, 13)]}
        short = {"kind": "actionFinished", "action": "a7.1", "result": result}
        new, _ = step(state, short)
        self.assertEqual(len(new["parts"]), MOST["split"])

    def test_a_reason_for_a_person_is_clipped(self):
        state = example("fixing")
        new, _ = step(state, {"kind": "stopped", "text": "y" * 5000})
        self.assertEqual(len(new["attention"]["text"]), LONGEST["reason"] + 3)

    def test_a_closing_option_past_the_options_kept_is_none(self):
        output = copy.deepcopy(FIXTURES["planQuestions"])
        output["questions"][0].update(options=[f"Option {n}" for n in range(9)], closes=8)
        state = example("planning")
        new, _ = step(state, finished(state, output))
        self.assertEqual(new["plan"]["questions"][0]["closes"], 0)


if __name__ == "__main__":
    unittest.main()
