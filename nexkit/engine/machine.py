"""The states of a work item and what moves it between them, as docs/engine.md#states says.

`step` takes one state document, one event and the host's policies, and answers the new state
and the actions the host carries out. It is pure: it reads no network, no clock and no file.
The time is the event's, and the protocol's schemas come in as `check`, the function that tells
how a value breaks one of them.
"""

from __future__ import annotations

import copy

from ..protocol.size import MOST, STATE_LIMIT, clip, fit, signature
from ..schema import filled

FINAL = ("accepted", "noChange", "dropped")
# The state that runs each stage's agent.
RUNS = {"plan": "planning", "implement": "working", "fix": "fixing", "review": "reviewing"}
ACCESS = {"triage": "none", "plan": "read", "review": "read", "implement": "write", "fix": "write"}
INPUTS = {
    "plan": ["item", "discussion", "notes", "previousPlan", "linked", "evidence", "parts"],
    "implement": ["item", "discussion", "notes", "plan", "linked", "evidence", "parts"],
    "fix": ["item", "discussion", "notes", "plan", "rounds", "checks", "findings", "diff"],
    "review": ["item", "discussion", "notes", "plan", "rounds", "checks", "findings", "diff"],
}
# The actions that carry an item on (docs/engine.md#conformance). Each belongs to the state
# that asked for it, so the engine cancels the ones that run when the item leaves that state.
# The others stay pending until the host reports them done, whatever the state.
WORK = ("runAgent", "runChecks", "publish", "accept", "createParts", "replaceChange", "wait")
# The actions that the host carries out again under a new id when it lost them: they only
# tell, show or end, so doing one again changes nothing that one done already changed.
AGAIN = ("close", "cancel", "needPerson", "show", "wait")
# What a review-only item never gets: it is only checked and reviewed.
WRITES = ("publish", "replaceChange", "accept", "createParts")
# The states of a round, which keep the policies that the round started with.
ROUND = ("working", "fixing", "checking", "reviewing")
# The waits that a tick ends: those for a time.
TIMED = ("time", "usageLimit", "requiredChecks")
# How many event ids the state keeps, to take each event once.
RECENT = 20
# A host's policies when it leaves one out: NexKit 6's on GitHub without autopilot, as
# `policies.schema.json` gives each as its default (docs/engine.md#what-a-host-decides).
DEFAULTS = {
    "planApproval": "person",
    "answers": "never",
    "reviewAfterFailingChecks": "always",
    "accept": "person",
    "fixRounds": 2,
    "whenRoundsRunOut": "person",
    "conflicts": "person",
    "conflictRounds": 3,
    "splits": False,
    "afterFailure": "person",
}
# The states a conflict leads to under each conflict policy.
CONFLICTS = {"person": ("ready",), "resolve": ("fixing", "ready"), "redo": ("working",)}


class EngineError(ValueError):
    """A call the engine cannot answer, such as a first event that is not `created`."""


def blank(item):
    """The state document of a work item before its `created` event."""
    return {
        "schemaVersion": 1,
        "revision": 0,
        "item": item,
        "itemKind": "change",
        "state": "new",
        "change": None,
        "attention": None,
        "hold": False,
        "waits": [],
        "pending": [],
        "actors": {"plan": "engine", "change": "engine", "review": "engine"},
        "plan": None,
        "rounds": {
            "started": 0,
            "counted": 0,
            "conflicts": 0,
            "signatures": [],
            "findings": [],
            "criteria": [],
            "checklist": [],
        },
        "used": {
            "tokens": {"input": 0, "cached": 0, "output": 0},
            "seconds": 0,
            "usd": 0,
            "since": None,
        },
        "asked": [],
        "parts": [],
        "policies": {},
        "recentEvents": [],
    }


class Step:
    def __init__(self, state, policies, check):
        self.s = state
        self.problems = check
        self.given = {**DEFAULTS, **policies}
        # A round that runs keeps the policies it started with (docs/engine.md#the-engine).
        running = state["state"] in ROUND and state["policies"]
        self.p = {**DEFAULTS, **state["policies"]} if running else self.given
        self.actions = []
        self.waits = 0

    # What every rule uses.

    @property
    def state(self):
        return self.s["state"]

    @property
    def version(self):
        return (self.s["change"] or {}).get("version")

    @property
    def review_only(self):
        return self.s["itemKind"] == "reviewOnly"

    def final(self):
        return self.state in FINAL

    def number(self, prefix, count):
        return f"{prefix}{self.s['revision'] + 1}.{count}"

    def move(self, name):
        """Go to `name`: cancel the work the old state asked for in earlier steps, and end its
        waits and attention. A round's state keeps the policies the round started with."""
        issued = {action["id"] for action in self.actions}
        for action in list(self.s["pending"]):
            if action["kind"] in WORK and action["id"] not in issued:
                self.cancel(action)
        if name not in ROUND:
            self.s["policies"] = {}
        elif self.state not in ROUND or not self.s["policies"]:
            self.s["policies"] = dict(self.given)
            self.p = self.given
        kept = () if name in FINAL else ("blocker",)
        self.s["waits"] = [wait for wait in self.s["waits"] if wait["for"] in kept]
        self.s["state"] = name
        self.s["attention"] = None

    def do(self, kind, **fields):
        action = {"id": self.number("a", len(self.actions) + 1), "kind": kind, "next": None}
        action.update(fields)
        self.actions.append(action)
        self.s["pending"].append(copy.deepcopy(action))
        return action

    def cancel(self, action):
        self.s["pending"].remove(action)
        self.do("cancel", action=action["id"])

    def wait(self, kind):
        self.waits += 1
        wait = {"id": self.number("w", self.waits), "for": kind, "until": None, "on": None}
        self.s["waits"].append(wait)

    def waiting(self, kind):
        return any(wait["for"] == kind for wait in self.s["waits"])

    def need(self, reason, text):
        text = clip(text, "reason")
        self.s["attention"] = {"reason": reason, "text": text}
        self.do("needPerson", reason=reason, text=text)

    def spend(self, source):
        """Add what an agent or a lost action spent to the item's `used`."""
        used = self.s["used"]
        tokens = source.get("tokens") or {}
        for key in ("input", "cached", "output"):
            used["tokens"][key] += tokens.get(key, 0)
        used["seconds"] += source.get("seconds") or 0
        used["usd"] += source.get("usd") or 0

    def run(self, stage, again=False):
        if self.review_only and stage != "review":
            raise EngineError("A review-only item is only checked and reviewed.")
        self.s["rounds"]["started"] += stage in ("implement", "fix") and not again
        self.do(
            "runAgent",
            stage=stage,
            changeVersion=self.version,
            access=ACCESS[stage],
            inputs=INPUTS[stage],
            result=f"results/{stage}.schema.json",
            agent=None,
            model=None,
            effort=None,
            budget={"tokens": None, "seconds": None, "usd": None},
            session=None,
            resume=False,
        )

    def close(self, outcome, finding=None):
        self.do("close", outcome=outcome, finding=finding)

    # Starting work.

    def start_plan(self):
        self.move("planning")
        self.run("plan")

    def start_change(self):
        if self.s["change"]:
            self.move("fixing")
            self.run("fix")
        else:
            self.move("working")
            self.run("implement")

    def start_split(self):
        self.move("split")
        plan = self.s["plan"]
        parts = [
            {
                "key": part["key"],
                "title": part["title"],
                "body": {"name": f"part-{part['key']}", "ref": plan["text"]["ref"]},
                "needs": part["needs"],
            }
            for part in plan["split"]
        ]
        self.do("createParts", parts=parts)

    def next_fix(self):
        rounds = self.s["rounds"]
        if rounds["counted"] < self.p["fixRounds"]:
            rounds["counted"] += 1
            self.move("fixing")
            self.run("fix")
            return
        self.move("failed" if self.p["whenRoundsRunOut"] == "failed" else "ready")
        self.need("roundsUsedUp", "The checks or the review still fail, and no fix round is left.")

    def ready(self):
        self.move("ready")
        if "unverifiable" in self.s["rounds"]["criteria"]:
            self.need(
                "unverifiableCriterion",
                "A person confirms the criteria the review could not verify.",
            )
        elif "not_met" in self.s["rounds"]["checklist"]:
            self.need("personChecklistItem", "A person decides a checklist item.")
        else:
            self.accept_or_ask()

    def accept_or_ask(self):
        if self.p["accept"] == "autoWhenPassed":
            self.move("accepting")
            self.do("accept", changeVersion=self.version, by="engine")
        else:
            self.need("accept", "The change passed, and a person decides its accept.")

    def on_conflict_found(self):
        policy, rounds = self.p["conflicts"], self.s["rounds"]
        if policy == "redo":
            self.move("working")
            self.do("replaceChange", reason="The change no longer applies to what it was made on.")
        elif policy == "resolve" and rounds["conflicts"] < self.p["conflictRounds"]:
            rounds["conflicts"] += 1
            self.move("fixing")
            self.run("fix")
        else:
            self.move("ready")
            self.need("acceptRefused", "The change conflicts with what it was made on.")

    def held_back(self, ignore=()):
        """Whether something holds back the work of the item's state: a hold, attention, a
        wait other than for a blocker or of the kinds in `ignore`, or its work that runs."""
        waits = [w for w in self.s["waits"] if w["for"] not in ("blocker", *ignore)]
        # A `wait` action only asks for the tick, which has come or will.
        running = any(a["kind"] in WORK and a["kind"] != "wait" for a in self.s["pending"])
        return bool(self.s["hold"] or self.s["attention"] or waits or running)

    def resume(self):
        """Start the work of the item's state again once nothing holds it back, as after a
        wait for a time, a usage limit, required checks or the base branch, or a hold
        (docs/engine.md#states). It is the same round, so it counts nothing again."""
        if self.held_back():
            return
        stage = next((stage for stage, name in RUNS.items() if name == self.state), None)
        if stage:
            self.run(stage, again=True)
        elif self.state == "checking":
            self.do("runChecks", changeVersion=self.version)
        elif self.state == "accepting":
            self.do("accept", changeVersion=self.version, by="engine")

    def timed_out(self):
        """An implement or fix agent ran out of time. With splits on, as autopilot does, the
        item asks once for a plan that splits it; otherwise, or the second time, a person
        decides (`autopilot._stopped`)."""
        if self.p["splits"] and "split" not in self.s["asked"]:
            self.s["asked"].append("split")
            self.start_plan()
        else:
            self.move("failed")
            self.need("failed", "The agent ran out of time.")

    # Events. Each answers False when it changes nothing.

    def on_created(self, event):
        self.s["itemKind"] = event["itemKind"]
        self.s["actors"] = event["actors"]
        self.s["change"] = event["change"]
        if self.review_only and not event["change"]:
            raise EngineError("A review-only item comes with its change.")
        if event["plan"] is not None and not self.review_only:
            self.take_plan(event["plan"], {"name": "plan", "ref": "created"})
            self.move("awaitingApproval")
            self.need("approval", "The plan waits for its approval.")
        else:
            self.wait("start")
        return True

    def on_requested(self, event):
        what, state, change = event["what"], self.state, self.s["change"]
        if what == "start":
            # Only an item that waits to start starts, and a hold or a blocker keeps it
            # waiting. One that waits in a later state, as an import leaves a round that a
            # hold kept back, starts that state's work again.
            if self.review_only or not self.waiting("start") or self.waiting("blocker"):
                return False
            if state == "new" and not self.s["hold"]:
                self.start_plan()
            elif state in FINAL or state == "new" or self.held_back(ignore=("start",)):
                return False
            else:
                self.s["waits"] = [wait for wait in self.s["waits"] if wait["for"] != "start"]
                self.resume()
        elif what in ("plan", "planFromScratch"):
            if self.review_only or state not in ("new", "awaitingApproval", "failed"):
                return False
            self.start_plan()
        elif what == "go":
            if self.review_only or state not in ("new", "awaitingApproval"):
                return False
            if self.s["plan"]:
                self.s["plan"]["approved"] = True
            self.start_change()
        elif what == "fix":
            if self.review_only or not change or state not in ("ready", "failed"):
                return False
            self.move("fixing")
            self.run("fix")
        elif what == "review":
            if not change or state not in ("new", "ready", "failed"):
                return False
            self.check()
        elif what == "retry" and state == "failed":
            if self.review_only:
                self.check()
            elif change:
                self.move("fixing")
                self.run("fix")
            else:
                self.start_plan()
        else:
            return False
        return True

    def on_planApproved(self, event):
        plan = self.s["plan"]
        if self.state != "awaitingApproval" or plan is None or plan["questions"]:
            return False
        plan["approved"] = True
        if plan["tooLarge"] and plan["split"] and self.p["splits"]:
            self.start_split()
        else:
            self.start_change()
        return True

    def on_answered(self, event):
        if self.state != "awaitingApproval":
            return False
        self.start_plan()
        return True

    def on_changesRequested(self, event):
        if self.state != "ready" or not self.s["change"] or self.review_only:
            return False
        self.move("fixing")
        self.run("fix")
        return True

    def on_changeApproved(self, event):
        attention = self.s["attention"] or {}
        if (
            self.state != "ready"
            or event["changeVersion"] != self.version
            or attention.get("reason") not in ("unverifiableCriterion", "personChecklistItem")
        ):
            return False
        self.s["rounds"]["criteria"] = [
            "met" if status == "unverifiable" else status for status in self.s["rounds"]["criteria"]
        ]
        self.s["rounds"]["checklist"] = [
            "met" if answer == "not_met" else answer for answer in self.s["rounds"]["checklist"]
        ]
        self.ready()
        return True

    def on_dropRequested(self, event):
        self.move("dropped")
        self.close("notPlanned")
        return True

    def on_stopped(self, event):
        if self.state == "failed":
            return False
        self.move("failed")
        self.need("failed", event["text"])
        return True

    def on_held(self, event):
        if self.s["hold"]:
            return False
        self.s["hold"] = True
        return True

    def on_released(self, event):
        if not self.s["hold"]:
            return False
        self.s["hold"] = False
        self.resume()
        return True

    def on_partEnded(self, event):
        parts = self.s["parts"]
        match = [part for part in parts if part["item"] == event["part"]]
        if self.state != "split" or not match or match[0]["state"] is not None:
            return False
        match[0]["state"] = event["state"]
        if all(part["state"] for part in parts):
            self.s["waits"] = [wait for wait in self.s["waits"] if wait["for"] != "parts"]
            if any(part["state"] == "accepted" for part in parts):
                self.move("accepted")
                self.close("completed")
            else:
                self.need("splitIncomplete", "No part of the split was completed.")
        return True

    def on_reopened(self, event):
        self.move("new")
        self.need("reopened", "The work item was opened again.")
        return True

    def on_tick(self, event):
        waits = [
            wait for wait in self.s["waits"] if wait["id"] == event["wait"] and wait["for"] in TIMED
        ]
        if not waits:
            return False
        self.s["waits"].remove(waits[0])
        self.resume()
        return True

    def on_conflict(self, event):
        if not self.s["change"] or event["changeVersion"] != self.version or self.review_only:
            return False
        self.on_conflict_found()
        return True

    def on_changeAccepted(self, event):
        if not self.s["change"]:
            return False
        self.move("accepted")
        self.close("completed")
        return True

    def on_changeEmptied(self, event):
        if not self.s["change"]:
            return False
        self.move("noChange")
        self.close("completed")
        return True

    def on_changePushed(self, event):
        if not self.s["change"] or event["changeVersion"] == self.version:
            return False
        self.s["change"] = {**self.s["change"], "version": event["changeVersion"]}
        self.check()
        return True

    def on_textChanged(self, event):
        return False

    def on_commented(self, event):
        return False

    def on_blockerClosed(self, event):
        """An item that blocks this one ended, or the bug issue of a failure that the base
        branch has: the waits on it end."""
        ends = ("blocker", "baseFailure")
        waits = [w for w in self.s["waits"] if w["for"] in ends and w["on"] == event["blocker"]]
        if not waits:
            return False
        self.s["waits"] = [wait for wait in self.s["waits"] if wait not in waits]
        self.resume()
        return True

    def pending(self, number):
        return next((item for item in self.s["pending"] if item["id"] == number), None)

    def on_actionLost(self, event):
        action = self.pending(event["action"])
        if action is None:
            return False
        self.s["pending"].remove(action)
        self.spend(event)
        if action["kind"] in AGAIN:
            fields = {key: value for key, value in action.items() if key not in ("id", "next")}
            self.do(fields.pop("kind"), **fields)
        else:
            self.move("failed")
            self.need("outage", "An action the host started never reported.")
        return True

    def on_actionFinished(self, event):
        action = self.pending(event["action"])
        if action is None:
            return False
        if "changeVersion" in action and event["changeVersion"] != action["changeVersion"]:
            # The action is pending on the version it acts on: a result that names another
            # cannot be this action's (docs/engine.md#events-and-actions-arrive-more-than-once).
            raise EngineError(
                f"The result of {action['id']} is for the change version "
                f"{event['changeVersion']!r}, but the action acts on {action['changeVersion']!r}."
            )
        self.s["pending"].remove(action)
        handler = getattr(self, f"finished_{action['kind']}", None)
        if handler:
            handler(event["result"], action)
        return True

    # Results.

    def take_plan(self, output, text):
        """Keep a plan as the state does: each list to MOST and each text to LONGEST
        (`protocol.size`). A split of more parts than MOST keeps none, and `parts` says how
        many it named, so a split is never filed cut; each part needs only earlier parts."""
        questions = [_question(item) for item in output.get("questions", [])[: MOST["questions"]]]
        named = output.get("split", [])
        split = [
            {
                "key": number,
                "title": clip(part["title"], "title"),
                "needs": sorted({need for need in part["depends_on"] if 0 < need < number}),
            }
            for number, part in enumerate(named if len(named) <= MOST["split"] else [], 1)
        ]
        paths = output.get("protected_paths", [])[: MOST["protectedPaths"]]
        self.s["plan"] = {
            "text": text,
            "questions": questions,
            "criteria": len(output.get("acceptance_criteria", [])),
            "tooLarge": bool(output.get("too_large")),
            "parts": len(named),
            "noChange": bool(filled(output.get("no_change"))),
            "protectedPaths": [clip(path, "path") for path in paths],
            "approved": False,
            "split": split,
        }

    def misfits(self, output, stage):
        """How an agent's output breaks its stage's result schema: agent output is untrusted."""
        found = self.problems(output, f"results/{stage}.schema.json", "result")
        checklist = output.get("checklist", []) if isinstance(output, dict) else []
        if stage == "review" and not isinstance(checklist, list):
            found.append("result.checklist must be an array")
        elif stage == "review":
            for index, entry in enumerate(checklist):
                name = "results/checklist.schema.json#/$defs/review"
                found += self.problems(entry, name, f"result.checklist[{index}]")
        return found

    def finished_runAgent(self, result, action):
        self.spend(result)
        stage = action["stage"]
        if self.state != RUNS[stage]:
            # `move` cancels the agent of a state the item leaves, so this is a state that a
            # host changed by hand.
            raise EngineError(f"{action['id']} runs {stage}, which {self.state} does not run.")
        if result["timedOut"] and stage in ("implement", "fix"):
            self.timed_out()
            return
        output = result["output"]
        found = [] if output is None else self.misfits(output, stage)
        if result["error"] or result["noResult"] or output is None or found:
            self.move("failed")
            text = "; ".join(found[:3])
            self.need("failed", result["error"] or f"The agent gave no result that fits. {text}")
            return
        getattr(self, f"finished_{stage}")(output, action["id"])

    def finished_plan(self, output, number):
        self.take_plan(output, {"name": "plan", "ref": number})
        plan = self.s["plan"]
        self.do("show", what="plan", text=plan["text"])
        if plan["noChange"]:
            self.move("noChange")
            self.close("completed", {"name": "noChange", "ref": number})
        elif plan["questions"]:
            self.move("awaitingApproval")
            self.need("question", "The plan has questions for a person.")
        elif plan["protectedPaths"]:
            self.move("awaitingApproval")
            self.need("handMadeChange", "The change needs paths that only a person changes.")
        elif self.p["planApproval"] == "autoWhenClean" and plan["tooLarge"]:
            if plan["split"] and self.p["splits"]:
                plan["approved"] = True
                self.start_split()
            else:
                self.move("awaitingApproval")
                self.need("approval", "The plan is too large, and a person decides how to go on.")
        elif self.p["planApproval"] == "autoWhenClean":
            plan["approved"] = True
            self.start_change()
        else:
            self.move("awaitingApproval")
            self.need("approval", "The plan waits for its approval.")

    def finished_implement(self, output, number):
        self.finished_change(output, number, "implement")

    def finished_fix(self, output, number):
        self.finished_change(output, number, "fix")

    def finished_change(self, output, number, stage):
        if output["status"] == "blocked":
            if output.get("questions"):
                self.move("awaitingApproval")
                self.need("question", "The round has questions for a person.")
            else:
                self.move("failed")
                self.need("blocked", filled(output.get("blocker")) or "The agent was blocked.")
        elif stage == "implement" and filled(output.get("no_change")) and not self.s["change"]:
            self.move("noChange")
            self.close("completed", {"name": "noChange", "ref": number})
        else:
            self.do("publish", stage=stage)

    def finished_review(self, output, number):
        rounds = self.s["rounds"]
        if self.review_only:
            # A person's own change is only checked and reviewed: it waits for its next request.
            self.move("ready")
            self.do("show", what="review", text={"name": "review", "ref": number})
            self.wait("start")
            return
        criteria = [item["status"] for item in output["criteria"]]
        checklist = [item["answer"] for item in output.get("checklist", [])]
        # Past MOST, what a decision reads first: an unmet or unverifiable criterion and an
        # item not met are kept, and the blocking findings come before the others.
        rounds["criteria"] = _most(criteria, "criteria", ("unmet", "unverifiable"))
        rounds["checklist"] = _most(checklist, "checklist", ("not_met",))
        findings = sorted(output["findings"], key=lambda item: item["severity"] != "blocking")
        rounds["findings"] = [
            {"severity": item["severity"], "text": clip(item["body"], "finding")}
            for item in findings[: MOST["findings"]]
        ]
        blocking = any(item["severity"] == "blocking" for item in rounds["findings"])
        if (
            output["verdict"] == "request_changes"
            or "unmet" in rounds["criteria"]
            or blocking
            or rounds["signatures"]
        ):
            self.next_fix()
        else:
            self.ready()

    def finished_publish(self, result, action):
        if result["change"] is None:
            self.move("failed")
            self.need("failed", result["refused"] or "The change could not be published.")
            return
        self.s["change"] = result["change"]
        self.check()

    def check(self):
        self.move("checking")
        self.do("runChecks", changeVersion=self.version)

    def finished_runChecks(self, result, action):
        failing = [
            check for check in result["checks"] if not check["passed"] and not check["skipped"]
        ]
        self.s["rounds"]["signatures"] = [
            signature(check["signature"] or check["name"]) for check in failing
        ][: MOST["signatures"]]
        if self.review_only or not failing:
            self.review()
        elif self.p["reviewAfterFailingChecks"] == "always":
            self.review()
        else:
            self.next_fix()

    def review(self):
        self.move("reviewing")
        self.run("review")

    def finished_accept(self, result, action):
        if result["accepted"]:
            self.move("accepted")
            self.close("completed")
        elif result["refusal"] == "conflict":
            self.on_conflict_found()
        elif result["refusal"] == "notAccepted":
            self.move("failed")
            self.need("failed", result["text"] or "The accept failed.")
        else:
            self.move("ready")
            self.need("acceptRefused", result["text"] or "The accept was refused.")

    def finished_replaceChange(self, result, action):
        self.s["change"] = result["change"]
        self.run("implement")

    def finished_createParts(self, result, action):
        parts = result["parts"][: MOST["split"]]
        self.s["parts"] = [{"item": part["item"], "state": None} for part in parts]
        self.wait("parts")


def _question(question):
    """A question as the state keeps it: its texts clipped, at most MOST["options"] options,
    and no number that names an option it left out."""
    options = [clip(option, "option") for option in question["options"][: MOST["options"]]]
    kept = {
        "question": clip(question["question"], "question"),
        "options": options,
        "recommended": question["recommended"] if question["recommended"] <= len(options) else 0,
    }
    if "closes" in question:
        kept["closes"] = question["closes"] if question["closes"] <= len(options) else 0
    return kept


def _most(answers, kind, first):
    """`answers` as the state keeps them: all of them, or past MOST[kind] the ones in
    `first` before the others, so that a decision reads the same."""
    if len(answers) <= MOST[kind]:
        return answers
    return sorted(answers, key=lambda answer: answer not in first)[: MOST[kind]]


# The events that a final item still takes: a reopen, and the results of what it asked for.
AFTER_END = ("reopened", "actionFinished", "actionLost")


def step(state, event, policies, check):
    """The new state and actions after `event`, and whether anything changed. `check(value,
    name, where)` tells how `value` breaks the protocol's schema `name`."""
    if state is None:
        if event["kind"] != "created":
            raise EngineError("A work item's first event must be created.")
        state = blank(event["item"])
        before = None
    else:
        if event["item"] != state["item"]:
            raise EngineError("The event is about another work item.")
        before = state
        state = copy.deepcopy(state)
    work = Step(state, policies, check)
    handler = getattr(work, f"on_{event['kind']}", None)
    if handler is None:
        raise EngineError(f"No rule for the event {event['kind']}.")
    if before is not None and (
        event["id"] in before["recentEvents"]
        or event["kind"] == "created"
        or (work.final() and event["kind"] not in AFTER_END)
        or (not work.final() and event["kind"] == "reopened")
    ):
        changed = False
    else:
        changed = handler(event)
    if not changed:
        return before, [], False
    if work.review_only:
        _never_writes(work.actions)
    work.s["revision"] += 1
    work.s["recentEvents"] = [*work.s["recentEvents"], event["id"]][-RECENT:]
    if fit(work.s) is None:
        raise EngineError(f"The state document passes the limit of {STATE_LIMIT} bytes.")
    return work.s, work.actions, True


def _never_writes(actions):
    """A review-only item is never planned, changed, fixed or accepted (docs/engine.md#states)."""
    for action in actions:
        if action["kind"] in WRITES or action.get("stage", "review") != "review":
            raise EngineError(f"A review-only item never gets {action['kind']}.")
