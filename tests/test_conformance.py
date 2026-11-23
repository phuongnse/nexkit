"""The engine's conformance suite (docs/engine.md#conformance).

Each file in tests/conformance/ is a case: the host's policies and events in order, each with
the state and the kinds of actions it must give, and where a case names them, the reason of
its attention, what the engine asked for, and the earlier parts each part of a split needs.
Events name only what a case needs; the runner fills in the rest. Every answer is checked
against the protocol, and after every call each work item that is not final must have an
action that runs, a wait, a hold or attention.
"""

import copy
import json
import re
import unittest
from pathlib import Path

from nexkit import engine, protocol

HERE = Path(__file__).resolve().parent
CASES = HERE / "conformance"
DOC = HERE.parent / "docs" / "engine.md"
FIXTURES = json.loads((CASES / "fixtures.json").read_text())
FINAL = ("accepted", "noChange", "dropped")
PERSON = {"id": "octocat", "role": "decides"}
ACTION_ID = re.compile(r"^a\d+\.\d+$")
DEFAULTS = {
    "created": {
        "itemKind": "change",
        "title": "A work item",
        "text": {"name": "body", "ref": "42"},
        "actors": {"plan": "engine", "change": "engine", "review": "engine"},
        "plan": None,
        "change": None,
    },
    "requested": {"by": PERSON, "note": ""},
    "planApproved": {"by": PERSON},
    "answered": {"by": PERSON, "answers": [{"question": 1, "option": 1}], "note": ""},
    "commented": {"by": PERSON, "text": {"name": "comment", "ref": "9"}},
    "changesRequested": {"by": PERSON, "note": {"name": "comment", "ref": "9"}},
    "changeApproved": {"by": PERSON},
    "dropRequested": {"by": PERSON, "reason": ""},
    "changeAccepted": {"by": PERSON},
    "changeEmptied": {"landedIn": None},
    "changePushed": {"by": PERSON},
    "conflict": {"files": []},
    "stopped": {"outcome": "keyRefused", "text": "The provider refused the key."},
    "held": {"label": "on-hold"},
    "reopened": {"by": PERSON},
    "actionLost": {"tokens": None},
}
AGENT = {
    "tokens": None,
    "limit": None,
    "transient": False,
    "noResult": False,
    "error": None,
    "session": None,
    "changeVersion": None,
    "timedOut": False,
}
RESULTS = {
    "@timeout": {**AGENT, "output": None, "timedOut": True},
    "@error": {**AGENT, "output": None, "error": "The agent stopped."},
}


def result_of(value):
    if isinstance(value, str) and value.startswith("@agent:"):
        return {**AGENT, "output": copy.deepcopy(FIXTURES[value.removeprefix("@agent:")])}
    if isinstance(value, str):
        return copy.deepcopy(RESULTS[value])
    return value


def event_of(short, state):
    kind = short["kind"]
    event = {"protocol": 1, "kind": kind, "item": "42", "at": "2026-11-23T15:01:11Z"}
    event.update(copy.deepcopy(DEFAULTS.get(kind, {})))
    event.update({key: value for key, value in short.items() if key != "kind"})
    version = (state or {}).get("change") and state["change"]["version"]
    if kind in ("actionFinished", "actionLost"):
        if not ACTION_ID.match(event["action"]):
            ids = [item["id"] for item in state["pending"] if item["kind"] == event["action"]]
            event["action"] = ids[-1] if ids else "a0.0"
        event.setdefault("changeVersion", version)
    if kind == "actionFinished":
        event["result"] = result_of(event["result"])
    if kind in ("changeAccepted", "changeEmptied", "changeApproved"):
        event.setdefault("changeVersion", version)
    return event


def table_transitions():
    """Each (state, next state) that the page's table of states names."""
    text = DOC.read_text(encoding="utf-8").split("\n## States\n", 1)[1].split("\n- ", 1)[0]
    pairs = set()
    for line in text.splitlines():
        cells = line.split("|")
        if len(cells) > 3 and cells[1].strip().startswith("`"):
            state = cells[1].strip().strip("`")
            pairs |= {(state, name) for name in re.findall(r"`(\w+)`", cells[3])}
    return pairs


def allowed():
    """The table's transitions, and those the page's rules below it allow from many states:
    from a state with a change, a change that went in, became empty or was pushed to, and a
    conflict; from every state that is not final, a drop and a stop; and a reopened item."""
    states = _states()
    extra = {(name, "new") for name in FINAL}
    for name in states:
        if name in FINAL:
            continue
        extra |= {(name, "dropped"), (name, "failed")}
        if name not in ("new", "planning", "split"):
            extra |= {(name, target) for target in ("accepted", "noChange", "checking")}
            extra |= {(name, target) for target in ("fixing", "working", "ready")}
    return table_transitions() | extra


class ConformanceTest(unittest.TestCase):
    def run_case(self, path, seen, snapshots):
        case = json.loads(path.read_text())
        state = None
        for number, step in enumerate(case["steps"], 1):
            where = f"{path.stem} step {number} ({step['event']['kind']})"
            request = {
                "protocol": 1,
                "call": "step",
                "state": state,
                "event": event_of(step["event"], state),
                "policies": case["policies"],
            }
            answer = engine.answer(copy.deepcopy(request))
            self.assertNotIn("error", answer, where)
            self.assertEqual(protocol.check(answer, "calls/step.answer.schema.json"), [], where)
            for action in answer["actions"]:
                schema = f"actions/{action['kind']}.schema.json"
                self.assertEqual(protocol.check(action, schema), [], where)
            self.assertEqual(answer["changed"], step["changed"], where)
            new = answer["state"]
            if answer["changed"]:
                self.assertEqual(new["revision"], (state or {"revision": 0})["revision"] + 1, where)
                pending = {item["id"] for item in new["pending"]}
                self.assertTrue({action["id"] for action in answer["actions"]} <= pending, where)
            else:
                self.assertEqual(new, state, where)
                self.assertEqual(answer["actions"], [], where)
            self.assertEqual(new["state"], step["state"], where)
            if "actions" in step:
                self.assertEqual([a["kind"] for a in answer["actions"]], step["actions"], where)
            if "attention" in step:
                reason = (new["attention"] or {}).get("reason")
                self.assertEqual(reason, step["attention"], where)
            if "asked" in step:
                self.assertEqual(new["asked"], step["asked"], where)
            if "needs" in step:
                [parts] = [a["parts"] for a in answer["actions"] if a["kind"] == "createParts"]
                self.assertEqual([part["needs"] for part in parts], step["needs"], where)
            if new["state"] not in FINAL:
                carried = new["pending"] or new["waits"] or new["hold"] or new["attention"]
                self.assertTrue(carried, f"{where}: nothing carries the item on")
            if state is not None and new["state"] != state["state"]:
                seen.add((state["state"], new["state"]))
            snapshots.setdefault(new["state"], (new, case["policies"]))
            state = new

    def test_the_cases(self):
        seen, snapshots = set(), {}
        paths = sorted(path for path in CASES.glob("*.json") if path.name != "fixtures.json")
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(case=path.stem):
                self.run_case(path, seen, snapshots)
        self.assertEqual(table_transitions() - seen, set(), "transitions no case plays")
        self.assertEqual(seen - allowed(), set(), "transitions the page does not name")
        for name, (state, policies) in snapshots.items():
            if name in FINAL:
                continue
            with self.subTest(drop=name):
                request = {
                    "protocol": 1,
                    "call": "step",
                    "state": state,
                    "event": event_of({"kind": "dropRequested"}, state),
                    "policies": policies,
                }
                self.assertEqual(engine.answer(request)["state"]["state"], "dropped")
        self.assertEqual(set(snapshots) - set(FINAL), set(_states()) - set(FINAL))


def _states():
    return protocol.load("common.schema.json")["$defs"]["state"]["enum"]


if __name__ == "__main__":
    unittest.main()
