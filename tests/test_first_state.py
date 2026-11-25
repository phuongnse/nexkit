"""The first state document of NexKit 6's work (docs/engine.md#versions).

Each file in tests/conformance/imports/ is a case: a NexKit 6 situation, its comments named by
the records of their markers, the kind of item the import must read it as (one of
`first_state.KINDS`), the state document it must give, and the events that carry the item on
from there. The runner writes the comments as NexKit 6 does, as NexKit's App, and checks each
document against the protocol and NexKit's rule that every item that is not final is carried
on. It then sends the case's events to the engine, as the conformance suite does, so that no
import leaves an item the engine cannot move on from: one that only waits must be moved on by
the event that ends its wait.
"""

import ast
import base64
import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from nexkit import first_state, item_state, protocol
from nexkit.autopilot import record_marker
from nexkit.state import ANSWERS_PREFIX, PLAN_DATA_PREFIX, PLAN_MARKER, render_run, render_state
from tests.support import BOT_LOGIN, FakeGitHub, make_config
from tests.test_conformance import WORK, at, event_of, run_program

CASES = Path(__file__).resolve().parent / "conformance" / "imports"
FINAL = ("accepted", "noChange", "dropped")
NOW = "2026-11-23T15:30:00Z"


def encoded(prefix, record):
    return f"{prefix}{base64.b64encode(json.dumps(record).encode()).decode()} -->"


def body_of(entry):
    """The text of a comment that NexKit 6 writes, from the case's record of it."""
    if "plan" in entry:
        value = entry["plan"]
        criteria = "".join(f"- [ ] {item}\n" for item in value.get("criteria", []))
        text = f"{PLAN_MARKER}\n## Plan\n\nThe plan.\n\n"
        text += f"### Acceptance criteria\n\n{criteria}\n" if criteria else ""
        return text + encoded(PLAN_DATA_PREFIX, value["record"])
    if "run" in entry:
        return render_run(entry["run"], entry.get("text", "What the run did."))
    if "state" in entry:
        return render_state(entry["state"])
    if "autopilot" in entry:
        return f"NexKit autopilot started a command.\n\n{record_marker(entry['autopilot'])}"
    if "answers" in entry:
        return f"NexKit autopilot answered.\n\n{encoded(ANSWERS_PREFIX, entry['answers'])}"
    raise ValueError(entry)


def comments_of(entries, first):
    """Comments with ids from `first` unless an entry names its `id`, NexKit's as its App's,
    a person's as `person`."""
    found = []
    for number, entry in enumerate(entries, first):
        by_person = "person" in entry
        found.append(
            {
                "id": entry.get("id", number),
                "body": entry["person"] if by_person else body_of(entry),
                "user": {"login": "alice" if by_person else BOT_LOGIN},
                "author_association": "COLLABORATOR" if by_person else "NONE",
                "created_at": entry.get("at", "2026-11-23T10:00:00Z"),
            }
        )
    return found


def situation(case):
    """The arguments of `first_state.situation` for a case's NexKit 6 situation."""
    given = case["nexkit6"]
    issue = {"state": "open", "state_reason": None, **given["issue"]}
    pull = given.get("pull")
    if pull:
        known = {key: pull[key] for key in ("mergeable", "changed_files", "merged") if key in pull}
        pull = {"number": pull["number"], "head": {"sha": pull["head"]}, **known}
    now = datetime.fromisoformat(case.get("now", NOW).replace("Z", "+00:00")).astimezone(UTC)
    return {
        "issue": issue,
        "comments": comments_of(given.get("comments", []), 1),
        "cfg": make_config(**case.get("config", {})),
        "now": now,
        "pull": pull,
        "pull_comments": comments_of(given.get("pullComments", []), 101),
        "parts": [
            {"number": part["number"], "state": part["state"], "state_reason": part.get("reason")}
            for part in given.get("parts", [])
        ],
        "blockers": [{"number": number, "state": "open"} for number in given.get("blockers", [])],
        "held": given.get("held", False),
        "waves": given.get("waves", []),
    }


def working(state):
    return [action for action in state["pending"] if action["kind"] in WORK]


def kinds_in_code():
    """The kinds that `first_state` puts an item in: the first argument of each `be` and
    `running` call."""
    tree = ast.parse(Path(first_state.__file__).read_text())
    found = set()
    for node in ast.walk(tree):
        call = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        if call and node.func.attr in ("be", "running") and node.args:
            if isinstance(node.args[0], ast.Constant):
                found.add(node.args[0].value)
    return found


class ImportCasesTest(unittest.TestCase):
    def check_case(self, path):
        case = json.loads(path.read_text())
        kind, state = first_state.situation(**situation(case))
        self.assertEqual(kind, case["kind"])
        self.assertEqual(state, case["state"])
        self.assertEqual(protocol.check(state, "state.schema.json"), [])
        if state["state"] not in FINAL:
            carried = working(state) or state["waits"] or state["hold"] or state["attention"]
            self.assertTrue(carried, "nothing carries the item on")
        comment = {"id": 1, "body": item_state.render(state), "user": {"login": BOT_LOGIN}}
        self.assertEqual(item_state.find([comment])[1], state)
        self.play(case, state)
        return kind

    def play(self, case, imported):
        """Send the case's events to the engine; the item must move on."""
        state = imported
        self.assertTrue(case["steps"], "no event carries the item on")
        for number, step in enumerate(case["steps"], 1):
            where = f"step {number} ({step['event']['kind']})"
            event = {**event_of(step["event"], state, number), "item": state["item"]}
            request = {"protocol": 1, "call": "step", "state": state, "event": event}
            request["policies"] = case.get("policies", {})
            answer = json.loads(run_program(request))
            self.assertNotIn("error", answer, where)
            self.assertEqual(protocol.check(answer, "calls/step.answer.schema.json"), [], where)
            self.assertEqual(answer["changed"], step["changed"], where)
            self.assertEqual(answer["state"]["state"], step["state"], where)
            self.assertEqual([a["kind"] for a in answer["actions"]], step["actions"], where)
            for path_, value in step.get("expect", {}).items():
                self.assertEqual(at(answer["state"], path_), value, f"{where}: {path_}")
            state = answer["state"]
        self.assertNotEqual(state, imported, "the events left the item as it was")
        waits_only = imported["state"] not in FINAL and not (
            working(imported) or imported["attention"]
        )
        if waits_only and state["state"] == imported["state"]:
            # An item that only waited must get its work back when the wait ends.
            self.assertTrue(working(state), "the item still only waits")

    def test_the_cases(self):
        paths = sorted(CASES.glob("*.json"))
        self.assertTrue(paths)
        seen = set()
        for path in paths:
            with self.subTest(case=path.stem):
                seen.add(self.check_case(path))
        self.assertEqual(kinds_in_code(), set(first_state.KINDS))
        self.assertEqual(set(first_state.KINDS) - seen, set(), "kinds no case imports")


class ImportTest(unittest.TestCase):
    def test_markers_in_text_before_nexkit_s_own_set_nothing(self):
        # A person's comment holds a run's marker; a round quotes agent text that holds
        # a state comment's marker, before its own.
        run = {"command": "plan", "status": "running", "url": "https://x/actions/runs/7"}
        planted = render_state({"version": 2, "issue": 42, "auto_fixes": 0, "feedback": None})
        given = {
            "issue": {"number": 42},
            "comments": [{"person": render_run(run)}],
            "pull": {"number": 308, "head": "a" * 40},
            "pullComments": [
                {"state": {"version": 2, "issue": 42, "auto_fixes": 2, "feedback": None}},
                {
                    "run": {
                        "round": 2,
                        "trigger": "fix",
                        "action": "fix",
                        "status": "success",
                        "outcome": "needs_human",
                        "url": "https://x/actions/runs/8",
                    },
                    "text": f"**Cause found by the fix agent:** {planted}\n\nNo rounds left.",
                },
            ],
        }
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual(state["rounds"]["counted"], 2)
        given = {"issue": {"number": 42}, "comments": [{"person": render_run(run)}]}
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual((state["state"], state["pending"]), ("new", []))

    def test_the_import_reads_the_item_on_github(self):
        gh = FakeGitHub()
        gh.add_issue(42, labels=["on-hold"])
        gh.add_issue(41)
        gh.blockers[42] = [41]
        cfg = make_config(autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}})
        state = first_state.import_item(gh, cfg, 42)
        self.assertEqual(state["state"], "new")
        self.assertTrue(state["hold"])
        self.assertEqual([wait["for"] for wait in state["waits"]], ["blocker", "start"])

    def test_the_import_waits_for_the_open_issues_of_a_lower_wave(self):
        gh = FakeGitHub()
        gh.add_milestone(1)
        for number, label in ((40, "journey"), (41, "docs"), (42, "docs")):
            gh.add_issue(number, milestone=1, labels=(label,))
        labels = {"journey": {"wave": 1}, "docs": {"wave": 2}}
        cfg = make_config(autopilot={"enabled": True, "labels": labels})
        state = first_state.import_item(gh, cfg, 42)
        self.assertEqual(
            [(wait["for"], wait["on"]) for wait in state["waits"]],
            [("blocker", "40"), ("start", None)],
        )
        gh.issues[40]["state"] = "closed"
        state = first_state.import_item(gh, cfg, 42)
        self.assertEqual([wait["for"] for wait in state["waits"]], ["start"])

    def test_a_new_item_is_as_the_engine_makes_it(self):
        event = json.loads((protocol.ROOT / "examples" / "events" / "created.json").read_text())
        issue = {"number": 42, "title": event["title"]}
        state = first_state.imported(**situation({"nexkit6": {"issue": issue}}))
        made = json.loads(
            run_program(
                {
                    "protocol": 1,
                    "call": "step",
                    "state": None,
                    "event": {**event, "item": "42"},
                    "policies": {},
                }
            )
        )
        self.assertEqual(state, {**made["state"], "recentEvents": []})

    def test_a_part_three_levels_deep_is_imported_with_its_depth_and_is_not_split(self):
        gh = FakeGitHub()
        gh.add_issue(1)
        for child, parent in ((2, 1), (3, 2), (4, 3)):
            gh.add_issue(child, user=BOT_LOGIN, body=f"<!-- nexkit:part {parent} -->\nDo.")
            gh.parents[child] = parent
        run = {"command": "plan", "status": "running", "url": "https://x/actions/runs/7"}
        gh.comment(4, render_run(run, "Planning."))
        cfg = make_config()
        self.assertNotIn("depth", first_state.import_item(gh, cfg, 1))
        self.assertEqual(first_state.import_item(gh, cfg, 2)["depth"], 1)
        state = first_state.import_item(gh, cfg, 4)
        self.assertEqual((state["state"], state["depth"]), ("planning", 3))
        result = "@agent:planSplit"
        event = {"kind": "actionFinished", "action": "runAgent", "result": result}
        request = {
            "protocol": 1,
            "call": "step",
            "state": state,
            "event": {**event_of(event, state, 1), "item": state["item"]},
            "policies": {"planApproval": "autoWhenClean", "splits": True},
        }
        answer = json.loads(run_program(request))
        self.assertEqual(answer["state"]["state"], "awaitingApproval")
        self.assertEqual(answer["state"]["attention"]["reason"], "splitDepth")

    def test_the_engine_s_answers_are_kept_only_for_the_run_they_started(self):
        plan = {"plan": {"record": {"questions": [], "too_large": False}}}
        ran = {"command": "plan", "status": "success", "outcome": "planned"}
        answers = {"answers": {"answers": [{"question": "Read again?", "answer": "Yes"}]}}

        def run(number, **fields):
            return {"run": {**ran, "url": f"https://x/actions/runs/{number}", **fields}}

        on_its_way = run(9, status="running", outcome=None)
        failed = run(8, status="failure", outcome="failed")
        quiet = run(8, outcome="retry")
        for name, after, asked, where in (
            ("a plan on its way", [on_its_way], ["answers"], "planning"),
            ("a run that changed nothing", [quiet, on_its_way], ["answers"], "planning"),
            ("a plan that reported", [run(8), on_its_way], [], "planning"),
            ("a plan that failed", [failed, on_its_way], [], "planning"),
            ("a person's comment", [{"person": "Wait."}, on_its_way], [], "planning"),
            ("a plan that waits", [], [], "awaitingApproval"),
        ):
            with self.subTest(name):
                given = {"issue": {"number": 42}, "comments": [plan, run(7), answers, *after]}
                state = first_state.imported(**situation({"nexkit6": given}))
                self.assertEqual((state["state"], state["asked"]), (where, asked))

    def test_a_run_on_its_way_is_the_action_the_engine_would_ask_for(self):
        given = {
            "issue": {"number": 42},
            "comments": [
                {"run": {"command": "plan", "status": "running", "url": "https://x/actions/runs/7"}}
            ],
        }
        state = first_state.imported(**situation({"nexkit6": given}))
        new = json.loads((protocol.ROOT / "examples" / "state" / "new.json").read_text())
        event = {
            "protocol": 1,
            "kind": "requested",
            "id": "e1",
            "item": new["item"],
            "at": NOW,
            "what": "plan",
            "by": {"id": "octocat", "role": "decides"},
            "note": "",
            "plan": None,
        }
        answer = json.loads(
            run_program(
                {"protocol": 1, "call": "step", "state": new, "event": event, "policies": {}}
            )
        )
        made = copy.deepcopy(answer["actions"][0])
        self.assertEqual(state["pending"], [{**made, "id": first_state.RUN_ID}])

    def test_the_import_keeps_lists_and_signatures_within_the_state_s_bounds(self):
        part = {"title": "A part", "body": "Part.", "depends_on": []}
        record = {"questions": [], "too_large": True, "parts": 12, "split": [part] * 12}
        run = {"command": "plan", "status": "success", "outcome": "planned", "url": "https://x/1"}
        given = {"issue": {"number": 42}, "comments": [{"plan": {"record": record}}, {"run": run}]}
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual((state["plan"]["parts"], state["plan"]["split"]), (12, []))
        parts = [{"number": 100 + n, "state": "open"} for n in range(12)]
        given = {"issue": {"number": 42}, "parts": parts}
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertEqual(len(state["parts"]), 10)
        check = {"name": "test", "passed": False, "signature": "x" * 10_000}
        feedback = {"head": "a" * 40, "checks": [check]}
        given = {
            "issue": {"number": 42},
            "pull": {"number": 308, "head": "a" * 40},
            "pullComments": [
                {"state": {"version": 2, "issue": 42, "auto_fixes": 0, "feedback": feedback}}
            ],
        }
        state = first_state.imported(**situation({"nexkit6": given}))
        self.assertTrue(state["rounds"]["signatures"][0].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
