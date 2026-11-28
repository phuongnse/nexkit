"""The engine's conformance suite (docs/engine.md#conformance).

Each file in tests/conformance/ is a case: the host's policies, the state it starts from (none,
or an example of `nexkit/protocol/v1/examples/state/` with fields set), and events in order,
each with the state, the `changed` flag and the kinds of actions it must give, or the error it
must be refused with. A step may also name fields of the state to `expect`, by dotted path, the
top-level fields that must be `absent`, and `policies` that the host sends from that step on.
Events name only what a case needs; the runner fills in the rest, each with an id of its own
unless the case gives one. A `note` is for people.

The runner sends each request to the program twice and checks that both answers are the same
text, checks every answer against the protocol, and after every call checks the outbox: every
action of the answer is pending, whole, an action leaves `pending` only with its result, its
cancel or its loss, a notice stays pending only while attention says it, and a fix round waits
to be issued (`rounds.due`) only in `fixing`. A state keeps a round's questions only while they
wait: in `awaitingApproval`, with attention naming them. It also checks NexKit's rule: each item
that is not final has a work action pending, a wait, a hold or attention. Across the cases, every
transition of the page's table is played, none that the page does not name for the case's
policies is, and a drop is played from every state that is not final.
"""

import copy
import io
import json
import re
import unittest
from pathlib import Path

from nexkit import protocol
from nexkit.engine import cli

HERE = Path(__file__).resolve().parent
CASES = HERE / "conformance"
DOC = HERE.parent / "docs" / "engine-states.md"
EXAMPLES = protocol.ROOT / "examples"
FIXTURES = json.loads((CASES / "fixtures.json").read_text())
FINAL = ("accepted", "noChange", "dropped")
# The actions that carry an item on: the others only tell, show or end.
WORK = (
    "runAgent",
    "runChecks",
    "publish",
    "accept",
    "createParts",
    "replaceChange",
    "updateChange",
    "wait",
)
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
    "textChanged": {"by": PERSON, "text": {"name": "body", "ref": "42"}},
    "commented": {"by": PERSON, "text": {"name": "comment", "ref": "9"}},
    "changesRequested": {"by": PERSON, "note": {"name": "comment", "ref": "9"}},
    "changeApproved": {"by": PERSON},
    "dropRequested": {"by": PERSON, "reason": ""},
    "changeObserved": {"conflicts": False, "merged": False, "empty": False},
    "stopped": {"outcome": "keyRefused", "text": "The provider refused the key."},
    "held": {"reason": "on-hold"},
    "reopened": {"by": PERSON},
    "actionLost": {"tokens": None, "seconds": None},
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
    "seconds": None,
}
RESULTS = {
    "@timeout": {**AGENT, "output": None, "timedOut": True},
    "@error": {**AGENT, "output": None, "error": "The agent stopped."},
    "@transient": {**AGENT, "output": None, "transient": True, "error": "Overloaded."},
    "@untrusted": {
        **AGENT,
        "output": None,
        "untrusted": True,
        "error": "An edit by someone without write access stopped it.",
    },
}
# The states a conflict leads to under each conflict policy (docs/engine-states.md#states).
CONFLICTS = {"person": ("ready",), "resolve": ("fixing", "ready"), "redo": ("working", "ready")}


def result_of(value):
    if isinstance(value, str) and value.startswith("@agent:"):
        return {**AGENT, "output": copy.deepcopy(FIXTURES[value.removeprefix("@agent:")])}
    if isinstance(value, str):
        return copy.deepcopy(RESULTS[value])
    return value


def event_of(short, state, number):
    """The whole event that `short` names, for `state`, as step `number` of its case. An
    action may be named by its kind, for the latest pending action of that kind."""
    kind = short["kind"]
    event = {"protocol": 1, "kind": kind, "id": f"e{number}", "item": "42"}
    event["at"] = "2026-11-23T15:01:11Z"
    event.update(copy.deepcopy(DEFAULTS.get(kind, {})))
    event.update({key: value for key, value in short.items() if key != "kind"})
    version = (state or {}).get("change") and state["change"]["version"]
    if kind in ("actionFinished", "actionLost"):
        pending = [item for item in state["pending"] if item["kind"] == event["action"]]
        if not ACTION_ID.match(event["action"]):
            event["action"] = pending[-1]["id"] if pending else "a0.0"
        action = next((a for a in state["pending"] if a["id"] == event["action"]), None)
        # A result echoes its action's own version, null for one with none.
        event.setdefault("changeVersion", action.get("changeVersion") if action else version)
    if kind == "actionFinished":
        event["result"] = result_of(event["result"])
        kinds = {item["id"]: item["kind"] for item in (state or {}).get("pending", [])}
        if kinds.get(event["action"]) in ("publish", "replaceChange", "updateChange"):
            # The host reads the change it set when it handles the result: unless the case says
            # otherwise, as the result gives it.
            change = event["result"].get("change")
            event["result"].setdefault("observed", change and observation(change))
    change_id = ((state or {}).get("change") or {}).get("id") or "none"
    if kind == "changeApproved":
        event.setdefault("changeId", change_id)
        event.setdefault("changeVersion", version)
    if kind == "changeObserved":
        # The change as the host reads it now: unless the case says otherwise, as it stands.
        event.setdefault("changeId", change_id)
        event.setdefault("version", version or "none")
    plan = ((state or {}).get("plan") or {}).get("text", {}).get("ref")
    if kind == "planApproved":
        event.setdefault("plan", plan or "none")
    if kind == "requested":
        # A go names the item's plan; other requests name none.
        event.setdefault("plan", plan if event["what"] == "go" else None)
    if kind == "answered":
        # The questions are those attention names, whatever the plan.
        event.setdefault("asked", ((state or {}).get("attention") or {}).get("ref") or "none")
    return event


def observation(change):
    """A change as a host reads it with nothing else to say: no conflict, merge or emptiness."""
    return {
        "changeId": change["id"],
        "version": change["version"],
        "conflicts": False,
        "merged": False,
        "empty": False,
    }


def start_of(case):
    """The state a case starts from: none, or an example state with the fields it sets."""
    start = case.get("state")
    if start is None:
        return None
    state = json.loads((EXAMPLES / "state" / f"{start['example']}.json").read_text())
    state.update(copy.deepcopy(start.get("set", {})))
    return state


def at(value, path):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


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


def allowed(policies, had_change):
    """The table's transitions, and those the page's rules below it allow, under `policies`:
    from every state that is not final, a drop and a stop; and, when the item has a change, a
    change that went in, became empty or was pushed to, a review asked for, and a conflict as
    the conflict policy says."""
    conflicts = {**_defaults(), **policies}["conflicts"]
    pairs = table_transitions()
    for name in _states():
        if name in FINAL:
            continue
        pairs |= {(name, "dropped"), (name, "failed")}
        if had_change and name != "split":
            pairs |= {(name, target) for target in ("accepted", "noChange", "checking")}
            pairs |= {(name, target) for target in CONFLICTS[conflicts]}
    return pairs


def policy_shapes(properties):
    """What each policy of `properties` needs played, by its name: the tuples that must be
    covered, and a function from a value to the tuples that value plays. It refuses a policy of
    a shape it does not know, so a new kind of policy cannot slip past the check."""
    return {name: _shape(name, schema) for name, schema in properties.items()}


def _shape(name, schema):
    kind = schema.get("type")
    if "enum" in schema or kind == "boolean":

        def play(value):
            return {("policy", name, value)}

        values = schema["enum"] if "enum" in schema else (True, False)
        return {("policy", name, value) for value in values}, play
    if kind == "integer" and "minimum" in schema:

        def play(value):
            return {("policy", name, "minimum" if value == schema["minimum"] else "above minimum")}

        return {("policy", name, "minimum"), ("policy", name, "above minimum")}, play
    if kind == "array":

        def play(value):
            return {("policy", name, "not empty" if value else "empty")}

        return {("policy", name, "empty"), ("policy", name, "not empty")}, play
    if kind == "object" and "properties" in schema:

        def play(value):
            return {
                ("policy", name, key)
                for key, sub in value.items()
                if isinstance(sub, dict) and any(item is not None for item in sub.values())
            }

        return {("policy", name, key) for key in schema["properties"]}, play
    raise ValueError(f"policy {name}: a schema shape the conformance test does not know")


def kinds(name):
    """The `kind` of each schema that the `anyOf` of schema file `name` refers to."""
    return {
        protocol.load(_relative(name, ref["$ref"]))["properties"]["kind"]["const"]
        for ref in protocol.load(name)["anyOf"]
    }


def _relative(name, ref):
    return str(Path(name).parent / ref) if "/" in name else ref


def required():
    """Everything the cases must play, read from the protocol's schemas: each policy value,
    each event kind and each action kind."""
    shapes = policy_shapes(protocol.load("policies.schema.json")["properties"])
    values = set().union(*(need for need, _ in shapes.values()))
    values |= {("event", kind) for kind in kinds("event.schema.json")}
    return values | {("action", kind) for kind in kinds("action.schema.json")}


def played(request, answer):
    """What one accepted step plays: each policy, the missing ones as their defaults, the
    event's kind and the kind of each action it answers."""
    shapes = policy_shapes(protocol.load("policies.schema.json")["properties"])
    policies = {**_defaults(), **request["policies"]}
    values = set()
    for name, value in policies.items():
        # A name the schema does not list, such as `start`, is nothing the schema asks for.
        if name in shapes:
            values |= shapes[name][1](value)
    values.add(("event", request["event"]["kind"]))
    return values | {("action", action["kind"]) for action in answer["actions"]}


def run_program(request):
    """The program's answer to `request`, as text. It reads standard input as bytes."""
    out = io.StringIO()
    cli.main(["step"], io.BytesIO(json.dumps(request).encode()), out)
    return out.getvalue()


def cases():
    return sorted(path for path in CASES.glob("*.json") if path.name != "fixtures.json")


def play(case):
    """Each step of a case with the state it starts from and the request the suite sends,
    which the program answers with `text`. The next step starts from the answer's state,
    unless the step expects an error."""
    policies = case["policies"]
    state = start_of(case)
    for number, step in enumerate(case["steps"], 1):
        policies = step.get("policies", policies)
        request = {
            "protocol": 1,
            "call": "step",
            "state": state,
            "event": event_of(step["event"], state, number),
            "policies": policies,
        }
        text = run_program(request)
        yield number, step, request, text
        if "error" not in step:
            state = json.loads(text)["state"]


def every_request():
    """Every request the suite sends, case by case."""
    for path in cases():
        for _, _, request, _ in play(json.loads(path.read_text())):
            yield request


class ConformanceTest(unittest.TestCase):
    def run_case(self, path, seen, dropped, covered):
        case = json.loads(path.read_text())
        state = start_of(case)
        if state is not None:
            self.assertEqual(protocol.check(state, "state.schema.json"), [], path.stem)
            self.check_questions(state, f"{path.stem} start")
        open_ = {action["id"]: action for action in (state or {}).get("pending", [])}
        for number, step, request, text in play(case):
            where = f"{path.stem} step {number} ({step['event']['kind']})"
            state, event, policies = request["state"], request["event"], request["policies"]
            self.assertEqual(run_program(request), text, f"{where}: the same request")
            answer = json.loads(text)
            if "error" in step:
                self.assertIn(step["error"], answer.get("error", ""), where)
                self.assertEqual(protocol.check(answer, "calls/error.schema.json"), [], where)
                continue
            covered |= played(request, answer)
            self.assertNotIn("error", answer, where)
            self.assertEqual(protocol.check(answer, "calls/step.answer.schema.json"), [], where)
            # The engine names no following action, so a host carries out one action at a time.
            for action in answer["actions"]:
                self.assertIsNone(action["next"], f"{where}: {action['id']} names a next action")
            self.assertEqual(answer["changed"], step["changed"], where)
            new = answer["state"]
            if answer["changed"]:
                self.assertEqual(new["revision"], (state or {"revision": 0})["revision"] + 1, where)
            else:
                self.assertEqual(new, state, where)
                self.assertEqual(answer["actions"], [], where)
            self.assertEqual(new["state"], step["state"], where)
            if "actions" in step:
                self.assertEqual([a["kind"] for a in answer["actions"]], step["actions"], where)
            for path_, value in step.get("expect", {}).items():
                self.assertEqual(at(new, path_), value, f"{where}: {path_}")
            for field in step.get("absent", ()):
                self.assertNotIn(field, new, f"{where}: {field}")
            open_ = self.check_outbox(open_, event, answer, where)
            self.check_due(new, where)
            self.check_questions(new, where)
            # A close stays pending only while the item is final: a reopen takes it back.
            if new["state"] not in FINAL:
                closes = [action["id"] for action in new["pending"] if action["kind"] == "close"]
                self.assertEqual(closes, [], f"{where}: a close is pending on an open item")
            # A notice stays pending only while attention says it, so a host that carries out
            # every pending action never tells a person something that no longer stands.
            attention = new["attention"] or {}
            for action in new["pending"]:
                if action["kind"] == "needPerson":
                    told = (action["reason"], action["text"], action["ref"])
                    wants = (attention.get("reason"), attention.get("text"), attention.get("ref"))
                    self.assertEqual(told, wants, f"{where}: {action['id']} no longer stands")
            if new["state"] not in FINAL:
                carried = [action for action in new["pending"] if action["kind"] in WORK]
                carried = carried or new["waits"] or new["hold"] or new["attention"]
                self.assertTrue(carried, f"{where}: nothing carries the item on")
            if state is not None and new["state"] != state["state"]:
                pair = (state["state"], new["state"])
                # A change the step itself set, as a publish does, counts too.
                names = allowed(policies, bool(state["change"] or new["change"]))
                self.assertIn(pair, names, f"{where}: the page names no {pair}")
                seen.add(pair)
            if event["kind"] == "dropRequested" and answer["changed"]:
                dropped.add(state["state"])

    def check_outbox(self, open_, event, answer, where):
        """The pending actions after `answer`: each action it gives is pending, whole, and
        each that was open stays, or left with its result, its cancel or its loss."""
        pending = {action["id"]: action for action in answer["state"]["pending"]}
        given = {action["id"]: action for action in answer["actions"]}
        for number, action in given.items():
            self.assertEqual(pending.get(number), action, f"{where}: {number} is not pending")
        ended = {action["action"] for action in answer["actions"] if action["kind"] == "cancel"}
        if event["kind"] in ("actionFinished", "actionLost") and answer["changed"]:
            ended.add(event["action"])
        for number, action in open_.items():
            if number in ended:
                self.assertNotIn(number, pending, f"{where}: {number} ended and stays")
            else:
                self.assertEqual(pending.get(number), action, f"{where}: {number} went away")
        for number in pending:
            self.assertTrue(number in open_ or number in given, f"{where}: {number} came")
        return pending

    def check_due(self, state, where):
        """A fix round that waits to be issued waits only in `fixing`: a move to another state
        ends the wait."""
        if state["rounds"].get("due"):
            self.assertEqual(state["state"], "fixing", f"{where}: a fix round waits outside it")

    def check_questions(self, state, where):
        """A round's questions are kept only while they wait for answers: in `awaitingApproval`,
        with attention naming them by their ref."""
        questions = state.get("questions")
        if questions:
            self.assertEqual(
                state["state"], "awaitingApproval", f"{where}: questions kept outside it"
            )
            ref = (state.get("attention") or {}).get("ref")
            self.assertEqual(ref, questions["ref"], f"{where}: attention names other questions")

    def test_the_cases(self):
        seen, dropped, covered = set(), set(), set()
        paths = cases()
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(case=path.stem):
                self.run_case(path, seen, dropped, covered)
        self.assertEqual(table_transitions() - seen, set(), "transitions no case plays")
        open_states = set(_states()) - set(FINAL)
        self.assertEqual(open_states - dropped, set(), "states no case drops an item from")
        missing = sorted(required() - covered, key=str)
        self.assertEqual(missing, [], "values no case plays")

    def test_a_value_no_case_plays_is_found(self):
        # Leave out every request that sends `onlyAfterPassing`: the check names it.
        covered = set()
        for path in cases():
            for _, step, request, text in play(json.loads(path.read_text())):
                effective = {**_defaults(), **request["policies"]}
                if "error" not in step and effective["reviewAfterFailingChecks"] == "always":
                    covered |= played(request, json.loads(text))
        missing = required() - covered
        self.assertIn(("policy", "reviewAfterFailingChecks", "onlyAfterPassing"), missing)
        self.assertNotIn(("policy", "reviewAfterFailingChecks", "always"), missing)

    def test_what_the_schemas_require(self):
        values = required()
        for value in (
            ("event", "textChanged"),
            ("action", "withdrawChange"),
            ("policy", "budgets", "triage"),
            ("policy", "budgets", "item"),
            ("policy", "agents", "review"),
            ("policy", "retryWaits", "empty"),
            ("policy", "splits", True),
            ("policy", "fixRounds", "above minimum"),
        ):
            self.assertIn(value, values)

    def test_a_policy_of_an_unknown_shape_is_refused(self):
        with self.assertRaises(ValueError):
            policy_shapes({"title": {"type": "string"}})
        with self.assertRaises(ValueError):
            policy_shapes({"mode": {"type": "object"}})

    def test_a_runner_that_checks_nothing_would_fail(self):
        # The outbox check finds an action that went away with no result, cancel or loss.
        action = json.loads((EXAMPLES / "state" / "planning.json").read_text())["pending"][0]
        state = {"state": "planning", "pending": []}
        answer = {"state": state, "actions": [], "changed": True}
        with self.assertRaises(AssertionError):
            self.check_outbox({action["id"]: action}, {"kind": "tick"}, answer, "here")
        # A fix round that waits to be issued outside `fixing` is found.
        with self.assertRaises(AssertionError):
            self.check_due({"state": "checking", "rounds": {"due": True}}, "here")
        # Questions kept outside `awaitingApproval` are found, and so are questions that the
        # attention does not name.
        with self.assertRaises(AssertionError):
            self.check_questions(
                {"state": "planning", "attention": None, "questions": {"ref": "r1", "items": []}},
                "here",
            )
        with self.assertRaises(AssertionError):
            self.check_questions(
                {
                    "state": "awaitingApproval",
                    "attention": {"reason": "question", "text": "t", "ref": "r2"},
                    "questions": {"ref": "r1", "items": []},
                },
                "here",
            )
        # A transition the conflict policy does not lead to is not allowed.
        self.assertNotIn(("working", "fixing"), allowed({"conflicts": "person"}, True))
        self.assertIn(("working", "fixing"), allowed({"conflicts": "resolve"}, True))
        # A conflict that finds no round left goes to a person, under `redo` as under `resolve`.
        self.assertIn(("working", "ready"), allowed({"conflicts": "redo"}, True))
        # The table names the failed checks that go to a person, which no conflict explains.
        self.assertIn(("checking", "ready"), table_transitions())
        # The table names the states a stage fails from, and a person's review of a failed item.
        self.assertIn(("planning", "failed"), table_transitions())
        self.assertIn(("checking", "failed"), table_transitions())
        self.assertIn(("reviewing", "failed"), table_transitions())
        self.assertIn(("failed", "checking"), table_transitions())
        # Nor one that only an item with a change makes.
        self.assertNotIn(("planning", "accepted"), allowed({}, False))
        self.assertNotIn(("new", "ready"), allowed({}, False))


def _states():
    return protocol.load("common.schema.json")["$defs"]["state"]["enum"]


def _defaults():
    properties = protocol.load("policies.schema.json")["properties"]
    return {name: policy["default"] for name, policy in properties.items()}


if __name__ == "__main__":
    unittest.main()
