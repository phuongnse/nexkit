"""The states of a work item and what moves it between them, as docs/engine.md#states says.

`Step` takes one state document and one event, and answers the new state and the actions
the host carries out. It reads no network, no clock and no file outside this package: the
time is the event's, and everything else comes with the call.
"""

from __future__ import annotations

import copy

from ..schema import filled

FINAL = ("accepted", "noChange", "dropped")
# The stage an agent runs in each state that runs one.
STAGE_OF = {"planning": "plan", "working": "implement", "fixing": "fix", "reviewing": "review"}
ACCESS = {"triage": "none", "plan": "read", "review": "read", "implement": "write", "fix": "write"}
INPUTS = {
    "plan": ["item", "discussion", "notes", "previousPlan", "linked", "evidence", "parts"],
    "implement": ["item", "discussion", "notes", "plan", "linked", "evidence", "parts"],
    "fix": ["item", "discussion", "notes", "plan", "rounds", "checks", "findings", "diff"],
    "review": ["item", "discussion", "notes", "plan", "rounds", "checks", "findings", "diff"],
}
# Actions that act on one version of the change: their result is stale once it moved.
BOUND = ("runChecks", "accept")
BOUND_STAGES = ("fix", "review")
# A host's policies when it leaves one out: NexKit 6's on GitHub
# (docs/engine.md#what-a-host-decides).
DEFAULTS = {
    "planApproval": "person",
    "answers": "never",
    "reviewAfterFailingChecks": "always",
    "accept": "person",
    "fixRounds": 2,
    "conflicts": "person",
    "conflictRounds": 3,
    "splits": True,
    "afterFailure": "person",
}


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
        "used": {"tokens": {"input": 0, "cached": 0, "output": 0}, "since": None},
        "asked": [],
        "parts": [],
        "policies": {},
    }


class Step:
    def __init__(self, state, policies):
        self.s = state
        self.p = {**DEFAULTS, **policies}
        self.actions = []

    # What every rule uses.

    @property
    def state(self):
        return self.s["state"]

    @property
    def version(self):
        return (self.s["change"] or {}).get("version")

    def final(self):
        return self.state in FINAL

    def move(self, name):
        self.s["state"] = name
        self.s["attention"] = None
        self.s["waits"] = [wait for wait in self.s["waits"] if wait["for"] != "start"]

    def do(self, kind, **fields):
        number = f"a{self.s['revision'] + 1}.{len(self.actions) + 1}"
        action = {"id": number, "kind": kind, "next": None, **fields}
        self.actions.append(action)
        bound = kind in BOUND or (kind == "runAgent" and fields["stage"] in BOUND_STAGES)
        self.s["pending"].append(
            {"id": number, "kind": kind, "changeVersion": self.version if bound else None}
        )
        return action

    def need(self, reason, text):
        self.s["attention"] = {"reason": reason, "text": text}
        self.do("needPerson", reason=reason, text=text)

    def cancel_all(self):
        for pending in list(self.s["pending"]):
            self.s["pending"].remove(pending)
            if pending["kind"] not in ("cancel", "close", "needPerson", "show"):
                self.do("cancel", action=pending["id"])

    def cancel_bound(self):
        """Cancel what acts on a version of the change that is no longer the current one."""
        for pending in list(self.s["pending"]):
            if pending["changeVersion"] is not None:
                self.s["pending"].remove(pending)
                self.do("cancel", action=pending["id"])

    def run(self, stage):
        self.s["rounds"]["started"] += stage in ("implement", "fix")
        self.do(
            "runAgent",
            stage=stage,
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
        else:
            self.move("ready")
            self.need(
                "roundsUsedUp", "The checks or the review still fail, and no fix round is left."
            )

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

    # Events. Each answers False when it changes nothing.

    def on_created(self, event):
        self.s["itemKind"] = event["itemKind"]
        self.s["actors"] = event["actors"]
        self.s["change"] = event["change"]
        if event["plan"] is not None:
            self.take_plan(event["plan"], {"name": "plan", "ref": "created"})
            self.move("awaitingApproval")
            self.need("approval", "The plan waits for its approval.")
        else:
            self.s["waits"].append({"id": "start", "for": "start", "until": None, "on": None})
        return True

    def on_requested(self, event):
        what, state = event["what"], self.state
        if what in ("plan", "planFromScratch", "start") and state in (
            "new",
            "awaitingApproval",
            "ready",
            "failed",
        ):
            if self.s["itemKind"] == "reviewOnly":
                return False
            self.start_plan()
        elif what == "go" and state in ("new", "awaitingApproval"):
            if self.s["itemKind"] == "reviewOnly":
                return False
            if self.s["plan"]:
                self.s["plan"]["approved"] = True
            self.start_change()
        elif what == "fix" and self.s["change"] and state in ("ready", "failed"):
            self.move("fixing")
            self.run("fix")
        elif what == "review" and self.s["change"] and state in ("new", "ready", "failed"):
            self.check()
        elif what == "retry" and state == "failed":
            if self.s["change"]:
                self.move("fixing")
                self.run("fix")
            else:
                self.start_plan()
        else:
            return False
        return True

    def on_planApproved(self, event):
        if self.state != "awaitingApproval" or self.s["plan"] is None:
            return False
        plan = self.s["plan"]
        if plan["questions"]:
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
        if self.state != "ready" or not self.s["change"]:
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
        if self.final():
            return False
        self.cancel_all()
        self.move("dropped")
        self.close("notPlanned")
        return True

    def on_stopped(self, event):
        if self.final() or self.state == "failed":
            return False
        self.cancel_all()
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
        if not self.final():
            return False
        self.move("new")
        self.need("reopened", "The work item was opened again.")
        return True

    def on_tick(self, event):
        waits = [wait for wait in self.s["waits"] if wait["id"] == event["wait"]]
        if not waits:
            return False
        self.s["waits"].remove(waits[0])
        return True

    def on_conflict(self, event):
        if self.final() or not self.s["change"] or event["changeVersion"] != self.version:
            return False
        self.cancel_bound()
        self.on_conflict_found()
        return True

    def on_changeAccepted(self, event):
        if self.final() or not self.s["change"]:
            return False
        self.cancel_all()
        self.move("accepted")
        self.close("completed")
        return True

    def on_changeEmptied(self, event):
        if self.final() or not self.s["change"]:
            return False
        self.cancel_all()
        self.move("noChange")
        self.close("completed")
        return True

    def on_changePushed(self, event):
        if self.final() or not self.s["change"] or event["changeVersion"] == self.version:
            return False
        self.s["change"] = {**self.s["change"], "version": event["changeVersion"]}
        self.cancel_all()
        self.check()
        return True

    def on_textChanged(self, event):
        return False

    def on_commented(self, event):
        return False

    def on_blockerClosed(self, event):
        return False

    def on_actionLost(self, event):
        pending = self.pending(event["action"])
        if pending is None:
            return False
        self.s["pending"].remove(pending)
        if pending["kind"] in ("runAgent", "runChecks", "publish", "accept", "createParts"):
            self.move("failed")
            self.need("outage", "An action the host started never reported.")
        return True

    def pending(self, number):
        return next((item for item in self.s["pending"] if item["id"] == number), None)

    def on_actionFinished(self, event):
        pending = self.pending(event["action"])
        if pending is None:
            return False
        if pending["changeVersion"] is not None and event["changeVersion"] != self.version:
            return False
        self.s["pending"].remove(pending)
        handler = getattr(self, f"finished_{pending['kind']}", None)
        if handler:
            handler(event["result"], event["action"])
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

    def finished_runAgent(self, result, number):
        stage = STAGE_OF.get(self.state)
        if stage is None:
            return
        tokens = result.get("tokens") or {}
        for key in ("input", "cached", "output"):
            self.s["used"]["tokens"][key] += tokens.get(key, 0)
        if result.get("timedOut") and stage in ("implement", "fix"):
            self.start_plan()
            return
        if result.get("error") or result.get("noResult") or result.get("output") is None:
            self.move("failed")
            self.need("failed", result.get("error") or "The agent gave no result that fits.")
            return
        getattr(self, f"finished_{stage}")(result["output"], number)

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
                self.need("approval", "The plan is too large and names no parts.")
        elif self.p["planApproval"] == "autoWhenClean":
            plan["approved"] = True
            self.start_change()
        else:
            self.move("awaitingApproval")
            self.need("approval", "The plan waits for its approval.")

    def finished_implement(self, output, number):
        self.finished_change(output, number)

    def finished_fix(self, output, number):
        self.finished_change(output, number)

    def finished_change(self, output, number):
        if output["status"] == "blocked":
            if output.get("questions"):
                self.move("awaitingApproval")
                self.need("question", "The round has questions for a person.")
            else:
                self.move("failed")
                self.need("blocked", filled(output.get("blocker")) or "The agent was blocked.")
        elif self.state == "working" and filled(output.get("no_change")) and not self.s["change"]:
            self.move("noChange")
            self.close("completed", {"name": "noChange", "ref": number})
        else:
            self.do("publish", stage=STAGE_OF[self.state])

    def finished_review(self, output, number):
        rounds = self.s["rounds"]
        if self.s["itemKind"] == "reviewOnly":
            # A person's own change is only checked and reviewed: it waits for its next request.
            self.move("ready")
            self.do("show", what="review", text={"name": "review", "ref": number})
            self.s["waits"].append({"id": "start", "for": "start", "until": None, "on": None})
            return
        rounds["criteria"] = [item["status"] for item in output.get("criteria", [])]
        rounds["checklist"] = [item["answer"] for item in output.get("checklist", [])]
        rounds["findings"] = [
            {"severity": item["severity"], "text": item["body"][:300]}
            for item in output.get("findings", [])
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

    def finished_publish(self, result, number):
        if result.get("change") is None:
            self.move("failed")
            self.need("failed", result.get("refused") or "The change could not be published.")
            return
        self.s["change"] = result["change"]
        self.check()

    def check(self):
        self.move("checking")
        self.do("runChecks", changeVersion=self.version)

    def finished_runChecks(self, result, number):
        failing = [
            check for check in result["checks"] if not check["passed"] and not check["skipped"]
        ]
        self.s["rounds"]["signatures"] = [check["signature"] or check["name"] for check in failing]
        if self.s["itemKind"] == "reviewOnly" or not failing:
            self.review()
        elif self.p["reviewAfterFailingChecks"] == "always":
            self.review()
        else:
            self.next_fix()

    def review(self):
        self.move("reviewing")
        self.run("review")

    def finished_accept(self, result, number):
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

    def finished_replaceChange(self, result, number):
        self.s["change"] = result["change"]
        if self.state == "working":
            self.run("implement")

    def finished_createParts(self, result, number):
        self.s["parts"] = [{"item": part["item"], "state": None} for part in result["parts"]]
        self.s["waits"].append({"id": "parts", "for": "parts", "until": None, "on": None})


def step(state, event, policies):
    """The new state and actions after `event`, and whether anything changed."""
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
    work = Step(state, policies)
    handler = getattr(work, f"on_{event['kind']}", None)
    if handler is None:
        raise EngineError(f"No rule for the event {event['kind']}.")
    if (
        before is not None
        and work.final()
        and event["kind"]
        not in (
            "reopened",
            "actionFinished",
            "actionLost",
        )
    ):
        changed = False
    else:
        changed = handler(event)
    if not changed:
        return before, [], False
    work.s["revision"] += 1
    return work.s, work.actions, True
