"""The states of a work item and what moves it between them, as docs/engine.md#states says.

`step` takes one state document, one event and the host's policies, and answers the new state
and the actions the host carries out. It is pure: it reads no network, no clock and no file.
The time is the event's, and the protocol's schemas come in as `check`, the function that tells
how a value breaks one of them.
"""

from __future__ import annotations

import copy

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
    "reviewAfterFailingChecks": "always",
    "accept": "person",
    "fixRounds": 2,
    "whenRoundsRunOut": "person",
    "conflicts": "person",
    "conflictRounds": 3,
    "splits": False,
}
# The states a conflict leads to under each conflict policy.
CONFLICTS = {"person": ("ready",), "resolve": ("fixing", "ready"), "redo": ("working",)}
# The states where nothing runs that a conflict must wait for: it acts there at once. Checks of
# a change that conflicts can no longer lead to an accept, so they stop for it too, when a
# conflict round can start (`Step.at_once`).
CONFLICT_AT_ONCE = ("ready", "accepting")
# The fields of a changeObserved event that the state keeps as its `observed`.
OBSERVED = ("changeId", "version", "conflicts", "merged", "empty")


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
        "observed": None,
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
        # The state keeps only the policies the engine knows (docs/engine.md#versions).
        self.given = {name: policies.get(name, value) for name, value in DEFAULTS.items()}
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

    def need(self, reason, text, ref=None):
        """Ask a person, about `ref` when the decision names what it is on."""
        self.s["attention"] = {"reason": reason, "text": text, "ref": ref}
        self.do("needPerson", reason=reason, text=text, ref=ref)

    def spend(self, source):
        """Add what an agent or a lost action spent to the item's `used`."""
        used = self.s["used"]
        tokens = source.get("tokens") or {}
        for key in ("input", "cached", "output"):
            used["tokens"][key] += tokens.get(key, 0)
        used["seconds"] += source.get("seconds") or 0
        used["usd"] += source.get("usd") or 0

    def run(self, stage):
        if self.review_only and stage != "review":
            raise EngineError("A review-only item is only checked and reviewed.")
        self.s["rounds"]["started"] += stage in ("implement", "fix")
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
        if self.conflicted():
            return
        self.move("failed" if self.p["whenRoundsRunOut"] == "failed" else "ready")
        self.need("roundsUsedUp", "The checks or the review still fail, and no fix round is left.")

    def ready(self):
        if self.conflicted():
            return
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

    def conflicted(self):
        """Apply the conflict policy when the item would go on to its checks or be ready and the
        latest observation says this version of the change conflicts (docs/engine.md#states).
        Whether it did."""
        observed = self.observed()
        if self.review_only or not observed or not observed["conflicts"]:
            return False
        self.on_conflict_found()
        return True

    def observed(self):
        """The latest observation, when it is of the change the item has."""
        observed, change = self.s["observed"], self.s["change"]
        if observed and change and observed["changeId"] == change["id"]:
            return observed
        return None

    def coming(self):
        """Whether a publish or a replace is on its way, which sets the change."""
        return any(a["kind"] in ("publish", "replaceChange") for a in self.s["pending"])

    def reconcile(self, whole=True):
        """Bring the item in line with the latest observation of its change; doing so again
        changes nothing (docs/engine.md#states). Not `whole`, only a merge or an emptied change
        acts. Whether it did anything."""
        observed = self.observed()
        if self.final() or self.coming() or not observed:
            return False
        if observed["merged"]:
            self.move("accepted")
            self.close("completed")
        elif observed["empty"]:
            self.move("noChange")
            self.close("completed")
        elif not whole:
            return False
        elif observed["version"] != self.version:
            # A person's push: what acts on the old version stops, and the new one is checked.
            self.s["change"] = {**self.s["change"], "version": observed["version"]}
            self.check()
        elif observed["conflicts"] and not self.review_only and self.at_once():
            if not self.on_conflict_found():
                return False
        else:
            return False
        return True

    def took_change(self, result):
        """A publish or a replace set the change. It changed the change outside the order of
        the item's events, so the observation its result carries, read when the host handled
        the result, replaces the latest one."""
        observed = result["observed"]
        self.s["change"] = result["change"]
        # One of another change stays in the slot unused: `observed` reads only the item's.
        self.s["observed"] = {key: observed[key] for key in OBSERVED} if observed else None

    def at_once(self):
        """Whether a conflict acts now: where nothing runs that it must wait for, and in checks
        when a conflict round can start, since those checks can no longer lead to an accept;
        NexKit 6 lets them end otherwise."""
        if self.state in CONFLICT_AT_ONCE:
            return True
        policy, rounds = self.p["conflicts"], self.s["rounds"]
        round_left = policy == "resolve" and rounds["conflicts"] < self.p["conflictRounds"]
        return self.state == "checking" and (policy == "redo" or round_left)

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
            attention = self.s["attention"] or {}
            if self.state == "ready" and (attention.get("reason"), attention.get("ref")) == (
                "acceptRefused",
                self.version,
            ):
                # A person was told already: reconciling again changes nothing.
                return False
            self.move("ready")
            self.need(
                "acceptRefused", "The change conflicts with what it was made on.", self.version
            )
        return True

    def timed_out(self):
        """An implement or fix agent ran out of time. With splits on, as autopilot does, the
        item asks once for a plan that splits it; otherwise, or the second time, a person
        decides (`autopilot._stopped`)."""
        if self.p["splits"] and "split" not in self.s["asked"]:
            # NexKit 6 asks after a fix round too. An item with a change is never split, so its
            # new plan goes on as one change on it, with the plan revised.
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
            self.plan_waits()
        else:
            self.wait("start")
        return True

    def on_requested(self, event):
        what, state, change = event["what"], self.state, self.s["change"]
        if what == "start":
            # Only an item that waits to start starts, and a hold keeps it waiting.
            if state != "new" or self.s["hold"] or self.review_only or not self.waiting("start"):
                return False
            self.start_plan()
        elif what in ("plan", "planFromScratch"):
            if self.review_only or state not in ("new", "awaitingApproval", "failed"):
                return False
            self.start_plan()
        elif what == "go":
            plan = (self.s["plan"] or {}).get("text", {}).get("ref")
            if self.review_only or state not in ("new", "awaitingApproval"):
                return False
            if event["plan"] != plan:
                # A go approves the plan it names: one for another plan arrives late.
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

    def splits(self, plan):
        """Whether `plan` is split into its parts: only an item with no change is, since a
        split would leave the change with nothing to end it."""
        return plan["tooLarge"] and plan["split"] and self.p["splits"] and not self.s["change"]

    def on_planApproved(self, event):
        plan = self.s["plan"]
        if (
            self.state != "awaitingApproval"
            or plan is None
            or plan["questions"]
            or event["plan"] != plan["text"]["ref"]
        ):
            return False
        plan["approved"] = True
        if self.splits(plan):
            self.start_split()
        else:
            self.start_change()
        return True

    def on_answered(self, event):
        attention = self.s["attention"] or {}
        if (
            self.state != "awaitingApproval"
            or attention.get("reason") != "question"
            or attention.get("ref") != event["asked"]
        ):
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
            or event["changeId"] != (self.s["change"] or {}).get("id")
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
        if not self.review_only:
            # A person's own pull request stays theirs: NexKit 6 never closes it.
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
        return True

    def on_partEnded(self, event):
        parts = self.s["parts"]
        match = [part for part in parts if part["item"] == event["part"]]
        if self.state != "split" or (match and match[0]["state"] is not None):
            return False
        if not match:
            # A part may end before the result that names the parts arrives: keep its end.
            if not any(action["kind"] == "createParts" for action in self.s["pending"]):
                return False
            parts.append({"item": event["part"], "state": event["state"]})
            return True
        match[0]["state"] = event["state"]
        self.parts_ended()
        return True

    def parts_ended(self):
        parts = self.s["parts"]
        if not all(part["state"] for part in parts):
            return
        self.s["waits"] = [wait for wait in self.s["waits"] if wait["for"] != "parts"]
        if any(part["state"] == "accepted" for part in parts):
            self.move("accepted")
            self.close("completed")
        else:
            self.need("splitIncomplete", "No part of the split was completed.")

    def on_reopened(self, event):
        if self.state in ("accepted", "noChange"):
            # Its change went in or became empty: the work starts again without it, and with
            # its rounds counted afresh.
            self.s["change"] = None
            self.s["observed"] = None
            self.s["rounds"] = blank(self.s["item"])["rounds"]
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
        return True

    def on_changeObserved(self, event):
        observed = {key: event[key] for key in OBSERVED}
        change = self.s["change"]
        # Only the item's change: one a publish or a replace sets comes with its result.
        if not change or observed["changeId"] != change["id"] or observed == self.s["observed"]:
            return False
        self.s["observed"] = observed
        return True

    def on_textChanged(self, event):
        return False

    def on_commented(self, event):
        return False

    def on_blockerClosed(self, event):
        return False

    def pending(self, number):
        return next((item for item in self.s["pending"] if item["id"] == number), None)

    def on_actionLost(self, event):
        action = self.pending(event["action"])
        if action is None:
            return False
        self.same_version(action, event)
        self.s["pending"].remove(action)
        self.spend(event)
        attention = self.s["attention"] or {}
        told = (attention.get("reason"), attention.get("text"), attention.get("ref"))
        if action["kind"] == "needPerson" and told != (
            action["reason"],
            action["text"],
            action["ref"],
        ):
            # What the person was to be told no longer stands.
            return True
        if action["kind"] in AGAIN:
            fields = {key: value for key, value in action.items() if key not in ("id", "next")}
            self.do(fields.pop("kind"), **fields)
        else:
            self.move("failed")
            self.need("outage", "An action the host started never reported.")
        return True

    def same_version(self, action, event):
        """A result echoes the version its action acts on, or null for one with none: one that
        names another cannot be this action's
        (docs/engine.md#events-and-actions-arrive-more-than-once)."""
        if event["changeVersion"] != action.get("changeVersion"):
            raise EngineError(
                f"The result of {action['id']} is for the change version "
                f"{event['changeVersion']!r}, but the action acts on "
                f"{action.get('changeVersion')!r}."
            )

    def on_actionFinished(self, event):
        action = self.pending(event["action"])
        if action is None:
            return False
        self.same_version(action, event)
        self.s["pending"].remove(action)
        handler = getattr(self, f"finished_{action['kind']}", None)
        if handler:
            handler(event["result"], action)
        return True

    # Results.

    def take_plan(self, output, text):
        questions = [
            {key: question[key] for key in ("question", "options", "recommended", "closes")}
            if "closes" in question
            else {key: question[key] for key in ("question", "options", "recommended")}
            for question in output.get("questions", [])
        ]
        split = [
            {"key": number, "title": part["title"], "needs": part["depends_on"]}
            for number, part in enumerate(output.get("split", []), 1)
        ]
        self.s["plan"] = {
            "text": text,
            "questions": questions,
            "criteria": len(output.get("acceptance_criteria", [])),
            "tooLarge": bool(output.get("too_large")),
            "parts": len(split),
            "noChange": bool(filled(output.get("no_change"))),
            "protectedPaths": list(output.get("protected_paths", [])),
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
        elif plan["questions"] or plan["protectedPaths"]:
            self.plan_waits()
        elif self.p["planApproval"] == "autoWhenClean" and plan["tooLarge"]:
            if self.splits(plan):
                plan["approved"] = True
                self.start_split()
            else:
                self.move("awaitingApproval")
                self.need(
                    "approval",
                    "The plan is too large, and a person decides how to go on.",
                    plan["text"]["ref"],
                )
        elif self.p["planApproval"] == "autoWhenClean":
            plan["approved"] = True
            self.start_change()
        else:
            self.plan_waits()

    def plan_waits(self):
        """The plan waits for a person: for answers, a hand-made change or its approval."""
        plan = self.s["plan"]
        ref = plan["text"]["ref"]
        self.move("awaitingApproval")
        if plan["questions"]:
            self.need("question", "The plan has questions for a person.", ref)
        elif plan["protectedPaths"]:
            self.need("handMadeChange", "The change needs paths that only a person changes.", ref)
        else:
            self.need("approval", "The plan waits for its approval.", ref)

    def finished_implement(self, output, number):
        self.finished_change(output, number, "implement")

    def finished_fix(self, output, number):
        self.finished_change(output, number, "fix")

    def finished_change(self, output, number, stage):
        if output["status"] == "blocked":
            if output.get("questions"):
                self.move("awaitingApproval")
                self.need("question", "The round has questions for a person.", number)
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
        rounds["criteria"] = [item["status"] for item in output["criteria"]]
        rounds["checklist"] = [item["answer"] for item in output.get("checklist", [])]
        rounds["findings"] = [
            {"severity": item["severity"], "text": item["body"][:300]}
            for item in output["findings"]
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
        self.took_change(result)
        if not self.reconcile():
            self.check()

    def check(self):
        if self.conflicted():
            return
        self.move("checking")
        self.do("runChecks", changeVersion=self.version)

    def finished_runChecks(self, result, action):
        failing = [
            check for check in result["checks"] if not check["passed"] and not check["skipped"]
        ]
        self.s["rounds"]["signatures"] = [check["signature"] or check["name"] for check in failing]
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
        self.took_change(result)
        if self.reconcile():
            return
        self.run("implement")

    def finished_createParts(self, result, action):
        ended = {part["item"]: part["state"] for part in self.s["parts"]}
        self.s["parts"] = [
            {"item": part["item"], "state": ended.get(part["item"])} for part in result["parts"]
        ]
        self.wait("parts")
        self.parts_ended()

    def finished_cancel(self, result, action):
        # The cancelled action's late result changes nothing, so its spending comes here.
        self.spend(result)


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
        # What waited for an earlier event, such as a push observed while the item was stopped.
        caught_up = work.reconcile()
        changed = handler(event) or caught_up
    if not changed:
        return before, [], False
    # An event that stops the item, or fails it for a lost action, keeps its attention: a new
    # version observed meanwhile waits for the next event (docs/engine.md#states).
    halted = event["kind"] == "stopped" or (
        event["kind"] == "actionLost" and (work.s["attention"] or {}).get("reason") == "outage"
    )
    work.reconcile(whole=not halted)
    if work.review_only:
        _never_writes(work.actions)
    work.s["revision"] += 1
    work.s["recentEvents"] = [*work.s["recentEvents"], event["id"]][-RECENT:]
    return work.s, work.actions, True


def _never_writes(actions):
    """A review-only item is never planned, changed, fixed or accepted (docs/engine.md#states)."""
    for action in actions:
        if action["kind"] in WRITES or action.get("stage", "review") != "review":
            raise EngineError(f"A review-only item never gets {action['kind']}.")
