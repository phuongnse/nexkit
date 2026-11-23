"""The first state document of NexKit 6's work (docs/engine.md#versions).

Each file in tests/conformance/imports/ is a case: a NexKit 6 situation, its comments named by
the records of their markers, and the state document the import must give. The runner
writes the comments as NexKit 6 does, as NexKit's App, and checks each document against the
protocol, NexKit's rule that every item that is not final is carried on, and the engine,
which must take it.
"""

import base64
import copy
import json
import unittest
from pathlib import Path

from nexkit import engine, first_state, item_state, protocol
from nexkit.autopilot import record_marker
from nexkit.state import ANSWERS_PREFIX, PLAN_DATA_PREFIX, PLAN_MARKER, render_run, render_state
from tests.support import BOT_LOGIN, FakeGitHub, make_config

CASES = Path(__file__).resolve().parent / "conformance" / "imports"
FINAL = ("accepted", "noChange", "dropped")
# Where the import must place each kind of open NexKit 6 item, by the cases' documents:
# (state, attention reason or the kind of a wait, or "pending" for a run on its way).
KINDS = {
    ("new", "start"),
    ("new", "blocker"),
    ("planning", "pending"),
    ("planning", "time"),
    ("awaitingApproval", "approval"),
    ("awaitingApproval", "question"),
    ("working", "pending"),
    ("working", "usageLimit"),
    ("checking", "pending"),
    ("fixing", "pending"),
    ("fixing", "time"),
    ("fixing", "baseFailure"),
    ("reviewing", "usageLimit"),
    ("ready", "accept"),
    ("ready", "unverifiableCriterion"),
    ("ready", "roundsUsedUp"),
    ("ready", "start"),
    ("accepting", "requiredChecks"),
    ("accepting", "hold"),
    ("split", "parts"),
    ("failed", "failed"),
    ("noChange", None),
    ("accepted", None),
    ("dropped", None),
}


def encoded(prefix, record):
    return f"{prefix}{base64.b64encode(json.dumps(record).encode()).decode()} -->"


def body_of(entry):
    """The text of a comment that NexKit 6 writes, from the case's record of it."""
    (kind, value), *_ = entry.items()
    if kind == "plan":
        criteria = "".join(f"- [ ] {item}\n" for item in value.get("criteria", []))
        text = f"{PLAN_MARKER}\n## Plan\n\nThe plan.\n\n"
        text += f"### Acceptance criteria\n\n{criteria}\n" if criteria else ""
        return text + encoded(PLAN_DATA_PREFIX, value["record"])
    if kind == "run":
        return render_run(value, "What the run did.")
    if kind == "state":
        return render_state(value)
    if kind == "autopilot":
        return f"NexKit autopilot started a command.\n\n{record_marker(value)}"
    if kind == "answers":
        return f"NexKit autopilot answered.\n\n{encoded(ANSWERS_PREFIX, value)}"
    raise ValueError(kind)


def comments_of(entries, first):
    """Comments with ids from `first`, NexKit's as its App's, a person's as `person`."""
    found = []
    for number, entry in enumerate(entries, first):
        by_person = "person" in entry
        found.append(
            {
                "id": number,
                "body": entry["person"] if by_person else body_of(entry),
                "user": {"login": "alice" if by_person else BOT_LOGIN},
                "author_association": "COLLABORATOR" if by_person else "NONE",
                "created_at": "2026-11-20T10:00:00Z",
            }
        )
    return found


def situation(case):
    """The arguments of `first_state.imported` for a case's NexKit 6 situation."""
    given = case["nexkit6"]
    issue = {"state": "open", "state_reason": None, **given["issue"]}
    pull = given.get("pull")
    if pull:
        pull = {"number": pull["number"], "head": {"sha": pull["head"]}}
    return {
        "issue": issue,
        "comments": comments_of(given.get("comments", []), 1),
        "pull": pull,
        "pull_comments": comments_of(given.get("pullComments", []), 101),
        "parts": [
            {"number": part["number"], "state": part["state"], "state_reason": part.get("reason")}
            for part in given.get("parts", [])
        ],
        "blockers": [{"number": number, "state": "open"} for number in given.get("blockers", [])],
        "held": given.get("held", False),
    }


def kinds_of(state):
    """What places a document among KINDS."""
    found = {(state["state"], wait["for"]) for wait in state["waits"]}
    if state["pending"]:
        found.add((state["state"], "pending"))
    if state["attention"]:
        found.add((state["state"], state["attention"]["reason"]))
    if state["hold"]:
        found.add((state["state"], "hold"))
    return found or {(state["state"], None)}


class ImportCasesTest(unittest.TestCase):
    def check_case(self, path):
        case = json.loads(path.read_text())
        state = first_state.imported(**situation(case))
        self.assertEqual(state, case["state"])
        self.assertEqual(protocol.check(state, "state.schema.json"), [])
        if state["state"] not in FINAL:
            carried = state["pending"] or state["waits"] or state["hold"] or state["attention"]
            self.assertTrue(carried, "nothing carries the item on")
        # The engine takes the document: a drop ends any item that is not final.
        event = {
            "protocol": 1,
            "kind": "dropRequested",
            "item": state["item"],
            "at": "2026-11-23T15:01:11Z",
            "by": {"id": "octocat", "role": "decides"},
            "reason": "",
        }
        request = {"protocol": 1, "call": "step", "state": state, "event": event, "policies": {}}
        answer = engine.answer(copy.deepcopy(request))
        self.assertNotIn("error", answer)
        self.assertEqual(
            answer["state"]["state"], "dropped" if state["state"] not in FINAL else state["state"]
        )
        # And the state comment keeps it as it is.
        comment = {"id": 1, "body": item_state.render(state), "user": {"login": BOT_LOGIN}}
        self.assertEqual(item_state.find([comment])[1], state)
        return kinds_of(state)

    def test_the_cases(self):
        paths = sorted(CASES.glob("*.json"))
        self.assertTrue(paths)
        seen = set()
        for path in paths:
            with self.subTest(case=path.stem):
                seen |= self.check_case(path)
        self.assertEqual(KINDS - seen, set(), "kinds of NexKit 6 items no case imports")


class ImportTest(unittest.TestCase):
    def test_a_person_s_comment_with_nexkit_s_markers_sets_nothing(self):
        run = {"command": "plan", "status": "running", "url": "https://x/actions/runs/7"}
        given = {"issue": {"number": 42}, "comments": [{"person": render_run(run)}]}
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual(state["state"], "new")
        self.assertEqual(state["pending"], [])

    def test_the_import_reads_the_item_on_github(self):
        gh = FakeGitHub()
        gh.add_issue(42, labels=["on-hold"])
        gh.add_issue(41)
        gh.blockers[42] = [41]
        cfg = make_config(autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}})
        state = first_state.import_item(gh, cfg, 42)
        self.assertEqual(state["state"], "new")
        self.assertTrue(state["hold"])
        self.assertEqual([wait["for"] for wait in state["waits"]], ["start", "blocker"])

    def test_a_new_item_is_as_the_engine_makes_it(self):
        state = first_state.imported(**situation({"nexkit6": {"issue": {"number": 42}}}))
        event = json.loads((protocol.ROOT / "examples" / "events" / "created.json").read_text())
        made, _, _ = engine.step(None, {**event, "item": "42"}, {})
        self.assertEqual(state, made)


if __name__ == "__main__":
    unittest.main()
