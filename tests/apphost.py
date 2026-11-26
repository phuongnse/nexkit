"""An application host for tests (docs/engine-hosts.md#hosts).

It is an application as the docs' column *An application* describes one. It reaches the engine only
over HTTP, as `describe`, `step` and `prompt` calls to a `nexkit-engine serve` process, and imports
only the standard library: `tests/test_apphost.py` fails when it imports anything else, `nexkit`
and the other test modules included. The few helpers it needs from `tests/test_conformance.py`
and `tests/test_serve.py` are copied here, and the agents' results are read from
`tests/conformance/fixtures.json` as a file.

Each request has a row that holds its state document. The host handles one event at a time for a
request, oldest first. It carries out only the actions that the state it stored has pending, and
answers each with an `actionFinished` event. After every step it checks that each action it carried
out was pending, and that each request that is not final has work, a wait, a hold or attention.
"""

import collections
import contextlib
import copy
import http.client
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = json.loads(
    (Path(__file__).resolve().parent / "conformance" / "fixtures.json").read_text()
)
FINAL = ("accepted", "noChange", "dropped")
# The actions that carry a request on: the others only tell, show or end.
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
CLOCK = "2026-11-23T15:00:00Z"
# The application's policies (docs/engine-hosts.md#what-a-host-decides): a person approves the
# plan, the review follows only passing checks, the engine accepts what passed, and a request
# has a token and time budget.
POLICIES = {
    "planApproval": "person",
    "answers": "never",
    "reviewAfterFailingChecks": "onlyAfterPassing",
    "accept": "autoWhenPassed",
    "fixRounds": 3,
    "whenRoundsRunOut": "person",
    "conflicts": "redo",
    "conflictRounds": 3,
    "splits": True,
    "parentEnd": "whenPartAccepted",
    "afterFailure": "person",
    "budgets": {
        "plan": {"seconds": 600},
        "implement": {"seconds": 1800},
        "fix": {"seconds": 1800},
        "review": {"seconds": 600},
        "item": {"tokens": 200000, "seconds": 7200},
    },
}
# What a runner of the kind model with tools gives for one session that ended with a result.
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
USUAL_TOKENS = {"input": 1000, "cached": 0, "output": 200}
HOLD = "hold"


@contextlib.contextmanager
def serve():
    """A `nexkit-engine serve` process on a free port of this machine; yields its port."""
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "nexkit-engine"), "serve", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        line = process.stdout.readline()
        if not line.startswith("nexkit-engine serves http://127.0.0.1:"):
            raise AssertionError(f"the server did not start: {line!r}")
        yield int(line.rsplit(":", 1)[1])
    finally:
        process.terminate()
        process.wait(timeout=10)
        process.stdout.close()


class Engine:
    """The engine's calls over HTTP."""

    def __init__(self, port):
        self.port = port
        answer = self.call("describe", {"protocol": 1})
        for name in ("step", "prompt"):
            assert name in answer["calls"], f"the engine has no {name} call: {answer['calls']}"

    def call(self, name, body):
        body = {"protocol": 1, "call": name, **body}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("POST", f"/{name}", body=json.dumps(body).encode())
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        if response.status != 200:
            raise AssertionError(f"{name}: HTTP {response.status}: {data.decode()}")
        return json.loads(data)


class Held:
    """What a runner gives for a run that stays open: no result, until a cancel ends it."""


class ScriptedRunner:
    """A runner of the kind model with tools, played from a script. `scripts` maps (item,
    stage) to the entries its runs take in turn: `@<fixture>` names an agent result of
    `tests/conformance/fixtures.json`, a dict with a `fixture` and `tokens` overrides the tokens
    the run spends, and `hold` keeps the run open with no result. It fails when a script runs dry,
    so that a scenario cannot loop."""

    def __init__(self, scripts):
        self.scripts = {key: list(entries) for key, entries in scripts.items()}

    def run(self, item, stage, prompt):
        assert isinstance(prompt, str) and prompt.strip(), f"no prompt for {item} {stage}"
        entries = self.scripts.get((item, stage))
        if not entries:
            raise AssertionError(f"no script left for {item} {stage}")
        entry = entries.pop(0)
        if entry == HOLD:
            return Held()
        if isinstance(entry, str):
            entry = {"fixture": entry.removeprefix("@")}
        tokens = entry.get("tokens", USUAL_TOKENS)
        output = copy.deepcopy(FIXTURES[entry["fixture"]])
        return {**AGENT, "output": output, "tokens": tokens, "seconds": entry.get("seconds", 30)}


@dataclass
class Row:
    """A request in the application's database."""

    id: str
    title: str
    body: str
    state: dict | None = None
    draft: dict | None = None
    closed: bool = False
    running: dict = field(default_factory=dict)
    queue: collections.deque = field(default_factory=collections.deque)


class Host:
    """The application: its rows, its validation scripts and its person's helpers."""

    def __init__(self, engine, runner, checks=()):
        self.engine = engine
        self.runner = runner
        self.validation = list(checks)  # whether each run of the checks passes, in turn
        self.rows = {}
        self.notices = []  # (item, kind, action) of what the person was told
        self.carried = set()  # the kinds of the actions carried out, for a coverage test
        self.stages = []  # (item, stage) in the order the runs started
        self.checks_log = []  # (item, passed) in the order the checks ran
        self.order = []  # ("agent", stage) and ("checks", passed), as they ran across both lists
        self.states = []  # (item, state) after each step, for the tests to read
        self.counter = 0

    # The engine's step, and what follows from it.

    def send(self, item, event):
        row = self.rows[item]
        self.counter += 1
        event = {"protocol": 1, "id": f"{item}:e{self.counter}", "item": item, "at": CLOCK, **event}
        old = row.state
        answer = self.engine.call(
            "step", {"state": old, "event": event, "policies": copy.deepcopy(POLICIES)}
        )
        state = answer["state"]
        if answer["changed"]:
            assert state["revision"] == (old or {"revision": 0})["revision"] + 1, "revision"
        row.state = state
        self.states.append((item, copy.deepcopy(state)))
        self.check_rule(state)
        pending = {action["id"] for action in state["pending"]}
        for action in answer["actions"]:
            assert action["id"] in pending, f"{action['kind']} {action['id']} was not pending"
            self.carried.add(action["kind"])
            self.carry_out(row, action)
        return state

    def check_rule(self, state):
        """Each request that is not final has work, a wait, a hold or attention."""
        if state["state"] in FINAL:
            return
        work = any(action["kind"] in WORK for action in state["pending"])
        assert work or state["waits"] or state["hold"] or state["attention"], (
            f"{state['item']} in {state['state']} has nothing to do"
        )

    def carry_out(self, row, action):
        kind = action["kind"]
        handler = getattr(self, f"do_{kind}", None)
        if handler is None:
            raise AssertionError(f"not carried out in part 1: {kind}")
        result = handler(row, action)
        if result is not None:
            self.finish(row, action, result)

    def finish(self, row, action, result):
        row.queue.append(
            {
                "kind": "actionFinished",
                "action": action["id"],
                "changeVersion": action.get("changeVersion"),
                "result": result,
            }
        )

    def run(self):
        """Send the queued events, one at a time for a request, oldest first, until none is left."""
        while True:
            row = next((row for row in self.rows.values() if row.queue), None)
            if row is None:
                return
            self.send(row.id, row.queue.popleft())

    # The actions.

    def do_runAgent(self, row, action):
        stage = action["stage"]
        state = row.state
        inputs = {
            "item": {
                "id": row.id,
                "title": row.title,
                "body": row.body,
                "change": row.draft and row.draft["id"],
                "person": False,
            }
        }
        inputs["workspace"] = {
            "checks": [{"name": "validate", "run": "validate"}],
            "protectedPaths": [],
        }
        if stage in ("implement", "fix", "review"):
            inputs["plan"] = self.plan_text(state)
        if stage in ("fix", "review"):
            inputs["diff"] = {"base": "release", "text": "--- a\n+++ b\n"}
        inputs = {name: inputs[name] for name in action["inputs"] if name in inputs}
        answer = self.engine.call(
            "prompt",
            {
                "state": state,
                "stage": stage,
                "inputs": inputs,
                "policies": copy.deepcopy(POLICIES),
                "texts": None,
            },
        )
        self.stages.append((row.id, stage))
        self.order.append(("agent", stage))
        result = self.runner.run(row.id, stage, answer["text"])
        if isinstance(result, Held):
            row.running[action["id"]] = result
            return None
        return result

    def plan_text(self, state):
        plan = state.get("plan")
        return f"The plan {plan['text']['ref']}." if plan and plan["approved"] else None

    def do_runChecks(self, row, action):
        if not self.validation:
            raise AssertionError("no validation left")
        ok = self.validation.pop(0)
        self.checks_log.append((row.id, ok))
        self.order.append(("checks", ok))
        check = {
            "name": "validate",
            "passed": ok,
            "skipped": False,
            "signature": None if ok else "fail validate",
            "knownBaseFailure": False,
        }
        return {"checks": [check]}

    def do_publish(self, row, action):
        revision = (row.draft["revision"] if row.draft else 0) + 1
        row.draft = {"id": f"d-{row.id}", "revision": revision}
        change = {"id": row.draft["id"], "version": f"r{revision}"}
        observed = {
            "changeId": change["id"],
            "version": change["version"],
            "conflicts": False,
            "merged": False,
            "empty": False,
        }
        return {"change": change, "refused": None, "observed": observed}

    def do_accept(self, row, action):
        return {"accepted": True, "refusal": None, "text": ""}

    def do_cancel(self, row, action):
        row.running.pop(action["action"], None)
        return {"tokens": None, "seconds": None}

    def do_close(self, row, action):
        row.closed = True
        return {}

    def do_needPerson(self, row, action):
        self.notices.append((row.id, action["kind"], action))
        return {}

    do_show = do_needPerson

    # The person, who sends the request and decides on it.

    def submit(self, item, title, body):
        self.rows[item] = Row(item, title, body)
        self.rows[item].queue.append(
            {
                "kind": "created",
                "itemKind": "change",
                "title": title,
                "text": {"name": "body", "ref": item},
                "actors": {"plan": "engine", "change": "engine", "review": "engine"},
                "plan": None,
                "change": None,
            }
        )
        self.rows[item].queue.append(
            {"kind": "requested", "what": "plan", "by": PERSON, "note": "", "plan": None}
        )
        self.run()

    def person(self, item, event):
        self.rows[item].queue.append(event)
        self.run()

    def approve_plan(self, item):
        plan = self.rows[item].state["plan"]["text"]["ref"]
        self.person(item, {"kind": "planApproved", "by": PERSON, "plan": plan})

    def answer(self, item):
        asked = self.rows[item].state["attention"]["ref"]
        answers = [{"question": 1, "option": 1}]
        self.person(
            item, {"kind": "answered", "by": PERSON, "answers": answers, "note": "", "asked": asked}
        )

    def drop(self, item):
        self.person(item, {"kind": "dropRequested", "by": PERSON, "reason": ""})

    def stop(self, item):
        self.person(
            item,
            {"kind": "stopped", "outcome": "keyRefused", "text": "The provider refused the key."},
        )

    def state_of(self, item):
        return self.rows[item].state
