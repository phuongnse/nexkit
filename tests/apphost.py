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
from datetime import datetime
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
TIME = "%Y-%m-%dT%H:%M:%SZ"
STEPS = 500  # the most events one run sends, so that a scenario cannot loop
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
    the run spends, `{"limit": <reset time>}` stops at a usage limit, `transient` and `timeout`
    stop with a transient error and a timeout, and `hold` keeps the run open with no result. It
    fails when a script runs dry, so that a scenario cannot loop."""

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
        if entry == "transient":
            return {**AGENT, "output": None, "transient": True, "error": "Overloaded."}
        if entry == "timeout":
            return {**AGENT, "output": None, "timedOut": True}
        if isinstance(entry, dict) and "limit" in entry:
            limit = {"message": "The account's usage limit.", "reset": entry["limit"]}
            return {**AGENT, "output": None, "limit": limit}
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
    parent: str | None = None
    depth: int = 0
    needs: list = field(default_factory=list)  # the ids of the parts this one waits for
    started: bool = False
    running: dict = field(default_factory=dict)
    queue: collections.deque = field(default_factory=collections.deque)


class Host:
    """The application: its rows, its validation scripts and its person's helpers."""

    def __init__(self, engine, runner, checks=(), policies=None):
        self.engine = engine
        self.policies = {**POLICIES, **(policies or {})}
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
        self.now = datetime.strptime(CLOCK, TIME)  # the host's clock
        self.timers = {}  # wait action id -> (item, action)
        self.ticks = []  # (item, wait, time) of each tick sent
        self.withdrawn = []  # the ids of the drafts withdrawn
        self.replaced = {}  # item -> how many drafts were made again

    # The engine's step, and what follows from it.

    def send(self, item, event):
        row = self.rows[item]
        self.counter += 1
        event = {
            "protocol": 1,
            "id": f"{item}:e{self.counter}",
            "item": item,
            "at": self.now.strftime(TIME),
            **event,
        }
        old = row.state
        answer = self.engine.call(
            "step", {"state": old, "event": event, "policies": copy.deepcopy(self.policies)}
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
            raise AssertionError(f"no handler for {kind}")
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
        for _ in range(STEPS):
            row = next((row for row in self.rows.values() if row.queue), None)
            if row is None and not self.timers:
                return
            if row is None:
                self.tick()
                continue
            self.send(row.id, row.queue.popleft())
        raise AssertionError(f"more than {STEPS} events")

    def tick(self):
        """Nothing else is queued: the clock moves to the earliest wait, which ends with a tick
        and then its `actionFinished`."""
        wait_id = min(self.timers, key=lambda key: self.timers[key][1]["until"])
        item, action = self.timers.pop(wait_id)
        until = datetime.strptime(action["until"], TIME)
        self.now = max(self.now, until)
        self.ticks.append((item, action["wait"], self.now.strftime(TIME)))
        self.rows[item].queue.append({"kind": "tick", "wait": action["wait"]})
        self.finish(self.rows[item], action, {})

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
                "policies": copy.deepcopy(self.policies),
                "words": self.words(row),
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

    def words(self, row):
        """The application's words for its request and its draft, which a prompt uses in place
        of GitHub's: fixed text and the ids of its own records."""
        return {
            "item": f"request {row.id}",
            "itemHeading": f"Request {row.id}",
            "itemKind": "request",
            "itemKinds": "requests",
            "change": f"draft {row.draft['id']}" if row.draft else "",
            "changeKind": "draft",
            "source": "in the application",
            "readers": "in the application",
            "approve": "*Approve*",
            "fixRound": "*Request changes*",
            "commands": "the application's actions",
        }

    def plan_text(self, state):
        plan = state.get("plan")
        return f"The plan {plan['text']['ref']}." if plan and plan["approved"] else None

    def do_runChecks(self, row, action):
        if not self.validation:
            raise AssertionError("no validation left")
        ok = self.validation.pop(0)
        self.order.append(("checks", ok))
        if ok in ("behind", "conflict"):
            return self.observe(row, action, ok)
        self.checks_log.append((row.id, ok))
        check = {
            "name": "validate",
            "passed": ok,
            "skipped": False,
            "signature": None if ok else "fail validate",
            "knownBaseFailure": False,
        }
        return {"checks": [check]}

    def observe(self, row, action, what):
        """The release moved while the checks ran: the run stays open, and the host observes the
        draft. The engine's `cancel` then ends the run."""
        draft = row.draft
        draft["behind" if what == "behind" else "conflicts"] = True
        row.running[action["id"]] = Held()
        event = self.observation(draft)
        event["kind"] = "changeObserved"
        row.queue.append(event)

    def observation(self, draft):
        observed = {
            "changeId": draft["id"],
            "version": f"r{draft['revision']}",
            "conflicts": draft["conflicts"],
            "merged": False,
            "empty": False,
        }
        if draft["behind"]:
            observed["behind"] = True
        return observed

    def do_publish(self, row, action):
        if row.draft:
            row.draft["revision"] += 1
            row.draft["conflicts"] = row.draft["behind"] = False
        else:
            row.draft = {"id": f"d-{row.id}", "revision": 1, "conflicts": False, "behind": False}
        return self.changed(row)

    def changed(self, row):
        draft = row.draft
        change = {"id": draft["id"], "version": f"r{draft['revision']}"}
        return {"change": change, "refused": None, "observed": self.observation(draft)}

    def do_updateChange(self, row, action):
        """A new revision on the latest release."""
        row.draft["revision"] += 1
        row.draft["behind"] = False
        return self.changed(row)

    def do_replaceChange(self, row, action):
        """A new draft: the change is made again."""
        number = self.replaced[row.id] = self.replaced.get(row.id, 0) + 1
        draft = {"id": f"d-{row.id}-{number}", "revision": 1, "conflicts": False, "behind": False}
        row.draft = draft
        result = self.changed(row)
        del result["refused"]
        return result

    def do_withdrawChange(self, row, action):
        self.withdrawn.append(action["changeId"])
        row.draft = None
        return {}

    def do_wait(self, row, action):
        self.timers[action["id"]] = (row.id, action)

    def do_accept(self, row, action):
        return {"accepted": True, "refusal": None, "text": ""}

    def do_cancel(self, row, action):
        row.running.pop(action["action"], None)
        self.timers.pop(action["action"], None)
        return {"tokens": None, "seconds": None}

    def do_createParts(self, row, action):
        """New requests that start when created, each held back until the parts it needs ended."""
        child = {part["key"]: f"{row.id}.{part['key']}" for part in action["parts"]}
        for part in action["parts"]:
            needs = [child[key] for key in part["needs"]]
            body = f"The text {part['body']['ref']}."
            self.create(child[part["key"]], part["title"], body, row, needs, part["body"])
        for part in action["parts"]:
            self.start(child[part["key"]])
        return {"parts": [{"key": key, "item": item} for key, item in child.items()]}

    def start(self, item):
        """A part starts once each part it needs has closed, whatever its end."""
        row = self.rows[item]
        if row.started or any(not self.rows[need].closed for need in row.needs):
            return
        row.started = True
        row.queue.append(
            {"kind": "requested", "what": "start", "by": None, "note": "", "plan": None}
        )

    def do_close(self, row, action):
        row.closed = True
        parent = row.parent and self.rows[row.parent]
        if parent:
            parent.queue.append({"kind": "partEnded", "part": row.id, "state": row.state["state"]})
            for sibling in self.rows.values():
                if sibling.parent == row.parent:
                    self.start(sibling.id)
        return {}

    def do_needPerson(self, row, action):
        self.notices.append((row.id, action["kind"], action))
        return {}

    do_show = do_needPerson

    # The person, who sends the request and decides on it.

    def create(self, item, title, body, parent=None, needs=(), text=None):
        """A new request, as a person sends it or as a part of another request."""
        depth = parent.depth + 1 if parent else 0
        self.rows[item] = Row(item, title, body, parent=parent and parent.id, depth=depth)
        self.rows[item].needs = list(needs)
        event = {
            "kind": "created",
            "itemKind": "change",
            "title": title,
            "text": text or {"name": "body", "ref": item},
            "actors": {"plan": "engine", "change": "engine", "review": "engine"},
            "plan": None,
            "change": None,
        }
        if depth:
            event["depth"] = depth
        self.rows[item].queue.append(event)

    def submit(self, item, title, body):
        self.create(item, title, body)
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
