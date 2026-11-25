"""The states of a work item and what moves it between them, as docs/engine.md#states says.

`step` takes one state document, one event and the host's policies, and answers the new state
and the actions the host carries out. It is pure: it reads no network, no clock and no file.
The time is the event's, and the protocol's schemas come in as `check`, the function that tells
how a value breaks one of them.
"""

from __future__ import annotations

import copy

from ..protocol.size import (
    DIAGNOSES,
    LONGEST,
    MOST,
    STATE_LIMIT,
    clip,
    failure_key,
    fit,
    signature,
)
from ..schema import filled

FINAL = ("accepted", "noChange", "dropped")
# The state that runs each stage's agent.
RUNS = {"plan": "planning", "implement": "working", "fix": "fixing", "review": "reviewing"}
ACCESS = {"triage": "none", "plan": "read", "review": "read", "implement": "write", "fix": "write"}
# The texts each stage's prompt is written from (`prompt.write`), by the kinds of runAgent's
# `inputs`: the host fetches them for the `prompt` call.
INPUTS = {
    "triage": ["item", "discussion", "notes", "previousPlan"],
    "plan": [
        "item",
        "discussion",
        "notes",
        "previousPlan",
        "linked",
        "evidence",
        "parts",
        "workspace",
    ],
    "implement": ["item", "discussion", "notes", "plan", "linked", "evidence", "workspace"],
    "fix": [
        "item",
        "notes",
        "plan",
        "rounds",
        "checks",
        "findings",
        "linked",
        "evidence",
        "diff",
        "workspace",
    ],
    "review": [
        "item",
        "discussion",
        "plan",
        "rounds",
        "checks",
        "findings",
        "linked",
        "diff",
        "workspace",
    ],
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
# The states of a fix round: a count it took stands only once its review ends.
COUNTING = ("fixing", "checking", "reviewing")
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
    "answers": "never",
    "splitParts": 10,
    "splitDepth": 3,
}
# The states a conflict leads to under each conflict policy.
CONFLICTS = {"person": ("ready",), "resolve": ("fixing", "ready"), "redo": ("working", "ready")}
# The states where nothing runs that a conflict must wait for: it acts there at once. Checks of
# a change that conflicts can no longer lead to an accept, so they stop for it too, when a
# conflict round can start (`Step.at_once`).
CONFLICT_AT_ONCE = ("ready", "accepting")
# The fields of a changeObserved event that the state keeps as its `observed`.
OBSERVED = ("changeId", "version", "conflicts", "merged", "empty")
# What a title may end with that does not make it another: ASCII punctuation and whitespace,
# as `string.punctuation` and `string.whitespace` hold them (`autopilot.repeated_titles`).
ENDS = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ \t\n\r\x0b\x0c"
# Who may close the item by an answer: closing is a write, so a reader's answer that picks
# the option that only closes it plans it again (`autopilot._closing_comment`).
WRITERS = ("decides", "writer")
# What the engine's answers to a round's questions keep in `asked`: that it answered, and that
# its answers started a new plan, whose round they start too (`autopilot.asked_again`).
ANSWERED = ("answers", "replan")
# What the engine asks a plan once more for, when a plan that is too large is not split, and
# the attention after a second such plan (`autopilot._split`).
AGAIN_FOR = {
    "fewer": ("tooManyParts", "The plan has more parts than a split files, again."),
    "parts": ("noParts", "The plan is too large and names no parts, again."),
    "titles": ("partTitles", "Parts of the plan repeat the work item's title, again."),
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
        self.issued = 0
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

    def next_action(self):
        """The id of the next action: one a withdrawn action had is never given again."""
        self.issued += 1
        return self.number("a", self.issued)

    def move(self, name):
        """Go to `name`: stop the work the old state asked for, and end its waits and attention.
        A round's state keeps the policies the round started with."""
        issued = {action["id"] for action in self.actions}
        for action in list(self.s["pending"]):
            if action["kind"] in WORK and action["id"] in issued:
                self.withdraw(action)
            elif action["kind"] in WORK:
                self.cancel(action)
        self.retract_notices()
        if name not in ROUND:
            self.s["policies"] = {}
        elif self.state not in ROUND or not self.s["policies"]:
            self.s["policies"] = dict(self.given)
            self.p = self.given
        kept = () if name in FINAL else ("blocker",)
        self.s["waits"] = [wait for wait in self.s["waits"] if wait["for"] in kept]
        # A fix round that waited to be issued no longer waits once the item moves, and a
        # counted fix round ends once the item leaves its states.
        self.s["rounds"].pop("due", None)
        if name not in COUNTING:
            self.s["rounds"].pop("automatic", None)
        if self.state == "planning" and name != "planning":
            # The engine's answers count only for the plan they started: a plan that ends
            # without its result, such as one that fails, forgets them, so the next plan's
            # questions can be answered again. The plan's own result carries a re-plan's
            # answers on to the round that follows (`finished_plan`).
            self.forget_answers()
        self.s["state"] = name
        self.s["attention"] = None
        # A round's questions wait only while the item does, after the round that asked.
        self.s.pop("questions", None)

    def do(self, kind, **fields):
        action = {"id": self.next_action(), "kind": kind, "next": None}
        action.update(fields)
        self.actions.append(action)
        self.s["pending"].append(copy.deepcopy(action))
        return action

    def withdraw(self, action):
        """Take back an action asked for earlier in this step: the host never got it."""
        self.s["pending"].remove(action)
        self.actions = [item for item in self.actions if item["id"] != action["id"]]
        if action["kind"] == "runAgent" and action["stage"] in ("implement", "fix"):
            self.s["rounds"]["started"] -= 1
        self.give_back(action)

    def cancel(self, action):
        self.s["pending"].remove(action)
        self.do("cancel", action=action["id"])
        self.give_back(action)

    def give_back(self, action):
        """An automatic fix round that the engine stops before its review ends gives its count
        back: it can no longer lead to an accept, and its work was not judged
        (`report._stop_round`, `maintain._give_back`)."""
        rounds = self.s["rounds"]
        work = action["kind"] in ("runChecks", "publish") or (
            action["kind"] == "runAgent" and action["stage"] in ("fix", "review")
        )
        if work and rounds.pop("automatic", None):
            rounds["counted"] = max(rounds["counted"] - 1, 0)

    def wait(self, kind, on=None, until=None):
        self.waits += 1
        wait = {"id": self.number("w", self.waits), "for": kind, "until": until, "on": on}
        self.s["waits"].append(wait)
        return wait

    def waiting(self, kind):
        return any(wait["for"] == kind for wait in self.s["waits"])

    def retract_notices(self):
        """Attention changes: a notice stays pending only while attention says it, so that every
        pending action is one to carry out (docs/engine.md#the-state-document)."""
        issued = {action["id"] for action in self.actions}
        for action in list(self.s["pending"]):
            if action["kind"] == "needPerson" and action["id"] in issued:
                self.withdraw(action)
            elif action["kind"] == "needPerson":
                self.cancel(action)

    def need(self, reason, text, ref=None):
        """Ask a person, about `ref` when the decision names what it is on."""
        self.retract_notices()
        text = clip(text, "reason")
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

    def run(self, stage, again=False, diagnose=False):
        """Run `stage`'s agent. A fix round names the checks it looks into first: with
        `diagnose`, an automatic fix round, those that failed the same way before and after the
        fix round before it, as NexKit 6's `route` makes it a diagnosis round; a person's round,
        a conflict round and the round that the engine's answers run again name none."""
        if self.review_only and stage != "review":
            raise EngineError("A review-only item is only checked and reviewed.")
        self.s["rounds"]["started"] += stage in ("implement", "fix") and not again
        repeated = self.s["rounds"].get("repeated") or []
        fields = {"diagnose": repeated[:DIAGNOSES] if diagnose else []} if stage == "fix" else {}
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
            **fields,
        )

    def close(self, outcome, finding=None):
        if self.review_only:
            # A person's own pull request stays theirs, merged, emptied or dropped: the engine
            # never closes it (docs/engine.md#states).
            return
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
        """The checks or the review fail: an automatic fix round while one is left
        (`report._report`). A fix round that changed nothing is not run again, since another
        could only change nothing too. The round waits while the base branch has the same
        failure, for the item about it to close or the base to pass again, since a fix here
        would only fix that again; and while a hold is on, as nothing new starts by itself
        (`due`). It is counted when its run is issued (`issue_fix`)."""
        rounds = self.s["rounds"]
        # The round's checks or review judged it, so its count stands.
        rounds.pop("automatic", None)
        if rounds.get("recheck") == self.version:
            if self.conflicted():
                return
            self.move("failed" if self.p["whenRoundsRunOut"] == "failed" else "ready")
            text = "The fix round changed nothing, and the checks or the review still fail."
            self.need("roundsUsedUp", text)
            return
        if rounds["counted"] < self.p["fixRounds"]:
            base = rounds.get("baseFailure")
            self.move("fixing")
            rounds["due"] = True
            if base:
                # The failure is the base branch's, which its own item fixes: the round after
                # the wait looks into nothing that repeated (`report._report`, `ci_waiting`).
                rounds.pop("repeated", None)
                self.wait("baseFailure", base)
            elif not self.s["hold"]:
                self.issue_fix()
            return
        self.used_up()

    def issue_fix(self, fresh=False):
        """Issue the automatic fix round that is due, counted now, while `fixRounds` has one
        left; otherwise a person decides. `fresh`: the round was kept back, so it starts with
        the policies the host gives now, as NexKit 6 reads `fix.max_auto_rounds` again when
        it starts it (`autopilot._released`, `_ci_step`)."""
        rounds = self.s["rounds"]
        rounds.pop("due", None)
        if fresh:
            self.s["policies"] = dict(self.given)
            self.p = self.given
        if rounds["counted"] >= self.p["fixRounds"]:
            self.used_up()
            return
        rounds["counted"] += 1
        rounds["automatic"] = True
        self.run("fix", diagnose=True)

    def used_up(self):
        if self.conflicted():
            return
        self.move("failed" if self.p["whenRoundsRunOut"] == "failed" else "ready")
        self.need("roundsUsedUp", "The checks or the review still fail, and no fix round is left.")

    def ready(self):
        # The round's checks or review judged it, so its count stands.
        self.s["rounds"].pop("automatic", None)
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
        """Accept the change by the accept policy, or ask a person. A hold keeps the engine's
        accept back until it ends (`report._report`)."""
        if self.p["accept"] == "autoWhenPassed":
            self.move("accepting")
            if not self.s["hold"]:
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
        """The latest observation, always of the change the item has: `on_changeObserved` keeps
        only those, `took_change` refuses a result that observes another change, and a reopen
        that drops the change drops it too."""
        return self.s["observed"]

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
        # A failed item waits for a person whatever is observed, and so does one in `new`, but a
        # review-only item that waits to start: a newer version waits for the request that takes
        # it on. A split parent waits for its parts whatever is pushed to its change, as the
        # autopilot reads a parent's parts before its pull request (docs/engine.md#states).
        starting = self.review_only and self.waiting("start")
        whole = whole and self.state not in ("failed", "split")
        whole = whole and (self.state != "new" or starting)
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
            if self.state != "fixing":
                # The round published: its change stays under the push, and the push's checks
                # judge it, so its count stands, as NexKit 6 keeps it (`report._head_moved`,
                # `publish.BranchMovedError`). In `fixing`, `move` still cancels the agent and
                # `give_back` returns the count.
                self.s["rounds"].pop("automatic", None)
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
        the result, replaces the latest one; `step` reconciles with it after the handler."""
        observed = result["observed"]
        if observed and observed["changeId"] != result["change"]["id"]:
            raise EngineError(
                f"The result of a {result['change']['id']!r} change observes "
                f"{observed['changeId']!r}; the host reports the action lost instead."
            )
        self.s["change"] = result["change"]
        self.s["observed"] = {key: observed[key] for key in OBSERVED} if observed else None

    def at_once(self):
        """Whether a conflict acts now: where nothing runs that it must wait for, and in checks
        when a conflict round can start, since those checks can no longer lead to an accept;
        NexKit 6 lets them end otherwise."""
        if self.state in CONFLICT_AT_ONCE:
            return True
        return (
            self.state == "checking"
            and self.p["conflicts"] in ("resolve", "redo")
            and self.conflict_round_left()
        )

    def conflict_round_left(self):
        """Whether `conflictRounds` allows another round that makes the change again or resolves
        its conflict (docs/engine.md#states)."""
        return self.s["rounds"]["conflicts"] < self.p["conflictRounds"]

    def on_conflict_found(self):
        policy, rounds = self.p["conflicts"], self.s["rounds"]
        if self.s["hold"] and policy != "person":
            return self.conflict_held()
        left = self.conflict_round_left()
        if policy == "redo" and left:
            rounds["conflicts"] += 1
            self.move("working")
            self.do("replaceChange", reason="The change no longer applies to what it was made on.")
        elif policy == "resolve" and left:
            rounds["conflicts"] += 1
            self.move("fixing")
            # The round that resolves the conflict is no automatic fix round: the count of
            # one whose change conflicts stands, as it published (`report._skipped_conflicting`).
            rounds.pop("automatic", None)
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

    def conflict_held(self):
        """A hold keeps the round that the conflict policy would start back, whatever rounds
        are left, as NexKit 6's hold label does (`report._skipped_conflicting`): the item waits
        in `checking`, with nothing running, and the policy applies with its count once the
        hold ends (`resume`). In `ready` and `accepting`, it applies when the hold ends, as
        the reconcile after `released` finds the conflict. Whether it did anything."""
        if self.state in CONFLICT_AT_ONCE:
            return False
        running = any(a["kind"] in WORK for a in self.s["pending"])
        if not running:
            # Nothing of an automatic fix round stops: its count stands, as when a conflict
            # round takes over, while a stop of its checks gives it back (`move`).
            self.s["rounds"].pop("automatic", None)
        if self.state == "checking" and not running:
            return False
        self.move("checking")
        return True

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
        (docs/engine.md#states). It is the same round, so it counts nothing again. Checks and
        an accept follow what the latest observation of the change says first: `check` before
        its checks, and the reconcile after the step for an accept."""
        if self.held_back():
            return
        stage = next((stage for stage, name in RUNS.items() if name == self.state), None)
        if stage == "fix" and self.s["rounds"].get("due"):
            self.issue_fix(fresh=True)
        elif stage:
            automatic = stage == "fix" and bool(self.s["rounds"].get("automatic"))
            self.run(stage, again=True, diagnose=automatic)
        elif self.state == "checking":
            self.check()
        elif self.state == "accepting":
            # A conflict observed meanwhile acts at once in `accepting`: `step` reconciles
            # after this and takes the accept back.
            self.do("accept", changeVersion=self.version, by="engine")

    def timed_out(self):
        """An implement or fix agent ran out of time. With splits on, as autopilot does, the
        item asks once for a plan that splits it; otherwise, the second time, or for an item
        at the deepest level of parts, which is not split, a person decides
        (`autopilot._stopped`, `split_refused`)."""
        deepest = self.s.get("depth", 0) >= self.p["splitDepth"]
        if self.p["splits"] and "split" not in self.s["asked"] and not deepest:
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
        self.take_place(event)
        if self.review_only and not event["change"]:
            raise EngineError("A review-only item comes with its change.")
        if event["plan"] is not None and not self.review_only:
            self.take_plan(event["plan"], {"name": "plan", "ref": "created"})
            self.plan_waits()
        else:
            self.wait("start")
        return True

    def on_requested(self, event):
        observed = self.observed()
        if observed and observed["version"] != self.version:
            # Only a failed or reopened item keeps a version observed meanwhile, for the request
            # that takes it on: the work it asks for starts on that version
            # (docs/engine.md#states). A refused request changes nothing, as `step` keeps the
            # state it got unless reconciling acts, which a merge or an emptied change alone do.
            self.s["change"] = {**self.s["change"], "version": observed["version"]}
        self.take_place(event)
        if event["by"] is not None:
            # A person acted, so the engine may answer the next plan's questions again.
            self.forget_answers()
        if not self.requested(event):
            return False
        # A request starts work anew: what a fix round that changed nothing left is no longer
        # the last word on its version (`report._report` reads `recheck` of a fix round only),
        # and checks it asks for are not a fix round's, whatever ran last.
        self.s["rounds"].pop("recheck", None)
        self.s["rounds"].pop("fixed", None)
        return True

    def take_place(self, event):
        """The item's title and how deep a part it is, as `created` gives them and a request
        gives them again: the parts of a split may not repeat the title, and an item at the
        deepest level is not split."""
        if "title" in event:
            self.s["title"] = event["title"][: LONGEST["title"]]
        if "depth" in event:
            self.s["depth"] = event["depth"]

    def forget_answers(self):
        self.s["asked"] = [name for name in self.s["asked"] if name not in ANSWERED]

    def requested(self, event):
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
            if self.review_only or not change or not self.takes_request(("ready", "failed")):
                return False
            # A round of its own, with the policies given now. An automatic round before it
            # was judged already, or it is a round that waits, whose count stands too.
            self.s["rounds"].pop("automatic", None)
            self.s["policies"], self.p = {}, self.given
            self.move("fixing")
            self.run("fix")
        elif what == "review":
            if not change or not self.takes_request(("new", "ready", "failed")):
                return False
            self.s["rounds"].pop("automatic", None)
            # Its checks, and a conflict found before them, follow the policies given now. It
            # leaves `ready` and its attention even when a hold keeps the conflict policy back.
            self.s["policies"], self.p = {}, self.given
            self.move("checking")
            self.check()
        elif what == "retry" and state == "failed":
            if self.review_only and not change:
                return False
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

    def takes_request(self, states):
        """Whether a person's fix or review request is taken: in `states`, or in `fixing`
        while no run is pending there, as when a fix round waits for a hold or the base
        branch: NexKit 6 runs a person's command then."""
        if self.state in states:
            return True
        busy = ("runAgent", "publish", "replaceChange")
        return self.state == "fixing" and not any(a["kind"] in busy for a in self.s["pending"])

    def splits(self, plan):
        """Whether `plan` is split into its parts: only an item with no change is, since a
        split would leave the change with nothing to end it, and only as the split's rules
        allow (`unsplit`)."""
        return (
            plan["tooLarge"]
            and self.p["splits"]
            and not self.s["change"]
            and self.unsplit() is None
        )

    def unsplit(self):
        """Why the parts of the plan, which is too large, are not filed, as NexKit 6 decides
        it (`autopilot._split`), or None: `depth` for an item at the deepest level of parts,
        or what a plan would need for a split: `fewer` parts than the most a split files,
        `parts` at all, or parts that do not repeat the item's own `titles`."""
        plan = self.s["plan"]
        if self.s.get("depth", 0) >= self.p["splitDepth"]:
            return "depth"
        if plan["parts"] > self.p["splitParts"]:
            return "fewer"
        if not plan["split"]:
            return "parts"
        if self.repeated_titles():
            return "titles"
        return None

    def repeated_titles(self):
        """Whether a part is titled by the item's own title or starts with it, ignoring case,
        spaces and punctuation at the end: each part is titled by its own behaviour."""
        own = _bare(self.s.get("title", ""))
        return bool(own) and any(
            _bare(part["title"]).startswith(own) for part in self.s["plan"]["split"]
        )

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
        self.forget_answers()
        # A split that its rules refuse goes on as one change, as a person's `/nexkit go` on
        # such a plan does in NexKit 6.
        if self.splits(plan):
            self.start_split()
        else:
            self.start_change()
        return True

    def on_answered(self, event):
        attention = self.s["attention"] or {}
        asked = self.waiting_questions()
        if self.state != "awaitingApproval" or not asked or attention.get("ref") != event["asked"]:
            return False
        if (
            event["by"]["role"] in WRITERS
            and not self.s["change"]
            and self.picks_closing(event["answers"], asked)
        ):
            # Another plan could only stop at the same place, so no agent runs. A change that
            # exists stays with its item: its answer goes to the next plan.
            self.move("dropped")
            self.close("notPlanned")
            return True
        self.forget_answers()
        self.start_plan()
        return True

    def waiting_questions(self):
        """The questions that attention waits for answers to, whatever its reason: the plan's,
        or those of the round that asked, as attention names them by their ref; or none."""
        ref = (self.s["attention"] or {}).get("ref")
        plan, round_ = self.s["plan"], self.s.get("questions")
        if ref is None:
            return []
        if round_ and round_["ref"] == ref:
            return round_["items"]
        if plan and plan["text"]["ref"] == ref:
            return plan["questions"]
        return []

    def round_waits(self, *reasons):
        """Whether the questions of the round that asked wait, for one of `reasons`."""
        attention, round_ = self.s["attention"] or {}, self.s.get("questions")
        return (
            self.state == "awaitingApproval"
            and bool(round_)
            and attention.get("ref") == round_["ref"]
            and attention.get("reason") in reasons
        )

    def picks_closing(self, answers, asked):
        """Whether `answers` pick, for a question of those `asked` that attention waits for,
        the option that only closes the item (`state.closing_answer`)."""
        return any(
            0 < answer["question"] <= len(asked)
            and 0 < answer["option"] == asked[answer["question"] - 1].get("closes", 0)
            for answer in answers
        )

    def on_changesRequested(self, event):
        if self.state != "ready" or not self.s["change"] or self.review_only:
            return False
        self.s["rounds"].pop("recheck", None)
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
        # A person's own pull request stays theirs: `close` leaves a review-only item open.
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
        if self.state != "split" or (match and match[0]["state"] is not None):
            return False
        if not match:
            # A part may end before the result that names the parts arrives: keep its end.
            creating = [action for action in self.s["pending"] if action["kind"] == "createParts"]
            # No more ends than the parts being made, so the list stays within MOST.
            if not creating or len(parts) >= min(len(creating[-1]["parts"]), MOST["split"]):
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
        observed = self.observed() or {}
        gone = observed.get("merged") or observed.get("empty")
        if self.state in ("accepted", "noChange") or gone:
            # Its change went in or became empty, as its final state or the latest observation
            # says, even after a drop: the work starts again without it, and with its rounds
            # counted afresh.
            self.s["change"] = None
            self.s["observed"] = None
            self.s["rounds"] = blank(self.s["item"])["rounds"]
        # A close asked for before the reopen no longer stands.
        for action in list(self.s["pending"]):
            if action["kind"] == "close":
                self.cancel(action)
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
        """A person's comment: the engine may answer the next plan's questions again, and with
        automatic approval a plan that waits for a person on an item with no change is
        planned again, since the plan did not hold what the comment says
        (`autopilot._commented_after`)."""
        if event["by"]["role"] == "outside":
            return False
        answered = "answers" in self.s["asked"]
        self.forget_answers()
        if self.round_commented():
            return True
        plan = self.s["plan"]
        if (
            self.state != "awaitingApproval"
            or self.p["planApproval"] != "autoWhenClean"
            or self.s["change"]
            or plan is None
            or plan["approved"]
        ):
            return answered
        self.start_plan()
        return True

    def round_commented(self):
        """A person's comment on the questions of a round that wait, with automatic approval.
        On an item with no change, questions the engine could not answer are planned again,
        since the round stopped where the plan did not say what the comment says; questions
        it answered once in a row are answered again, since a person acted
        (`autopilot._stopped`, `_since_stop`). Whether it did either."""
        if self.p["planApproval"] != "autoWhenClean":
            return False
        if self.round_waits("question") and not self.s["change"]:
            self.start_plan()
            return True
        if self.round_waits("askedAgain"):
            self.answer_round(False)
            return True
        return False

    def on_blockerClosed(self, event):
        """An item that blocks this one ended, or the bug issue of a failure that the base
        branch has: the waits on it end. With automatic approval, the questions of a round on
        an item with no change, which the engine could not answer, are planned again: the
        round may have stopped for that work (`autopilot._since_stop`)."""
        ends = ("blocker", "baseFailure")
        waits = [w for w in self.s["waits"] if w["for"] in ends and w["on"] == event["blocker"]]
        if not waits and self.p["planApproval"] == "autoWhenClean" and not self.s["change"]:
            if self.round_waits("question"):
                self.start_plan()
                return True
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
        self.same_version(action, event)
        self.s["pending"].remove(action)
        self.spend(event)
        attention = self.s["attention"] or {}
        told = (attention.get("reason"), attention.get("text"), attention.get("ref"))
        # The engine's own states never reach this: `need` and `move` cancel or withdraw a
        # notice whenever attention changes. It guards a state that a host or an import wrote
        # (docs/engine.md#the-state-document), so that a stale notice is not sent again.
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
        if result["limit"]:
            self.limited(result["limit"])
            return
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

    def limited(self, limit):
        """The agent stopped at a usage limit: the item waits for its reset, and then the same
        stage runs again, counting nothing again (`resume`). A limit whose reset the host does
        not give, as when it does not resume such runs, waits for a person
        (`report.pause`)."""
        if limit["reset"] is None:
            self.move("failed")
            self.need("failed", limit["message"] or "The agent stopped at its usage limit.")
            return
        wait = self.wait("usageLimit", until=limit["reset"])
        self.do("wait", wait=wait["id"], until=limit["reset"])

    def finished_plan(self, output, number):
        """What follows a plan, as NexKit 6 decides it (`autopilot._planned`). With automatic
        approval: a plan that is too large is split, or planned once more for what a split
        needs; one that finds no change ends the item; one that needs paths only a person
        changes waits; questions are answered by the `answers` policy, at most once in a row,
        or wait; and a clean plan starts the change. Otherwise a person decides every plan,
        one that finds no change too, as NexKit 6 closes an item by itself only with
        autopilot."""
        # Whether the engine's answers started this plan, with no person acting since. Answers
        # to a round that only a new plan carries out start that plan's round too.
        again = "answers" in self.s["asked"]
        replan = "replan" in self.s["asked"]
        self.forget_answers()
        self.take_plan(output, {"name": "plan", "ref": number})
        plan = self.s["plan"]
        self.do("show", what="plan", text=plan["text"])
        if self.p["planApproval"] != "autoWhenClean":
            self.plan_waits()
        elif plan["tooLarge"] and self.p["splits"] and not self.s["change"]:
            self.split_or_ask()
        elif plan["tooLarge"]:
            self.ask("approval", "The plan is too large, and a person decides how to go on.")
        elif plan["noChange"]:
            self.move("noChange")
            self.close("completed", {"name": "noChange", "ref": number})
        elif plan["protectedPaths"]:
            self.ask("handMadeChange", "The change needs paths that only a person changes.")
        elif plan["questions"]:
            self.answer_or_ask(again)
        else:
            plan["approved"] = True
            self.start_change()
        if replan:
            # The answers of a re-plan reach the round that follows, past the move that
            # forgets the engine's answers.
            self.s["asked"] += list(ANSWERED)

    def ask(self, reason, text):
        """The plan waits for a person, for `reason`."""
        self.move("awaitingApproval")
        self.need(reason, text, self.s["plan"]["text"]["ref"])

    def answer_or_ask(self, again):
        """Answer the plan's questions with their recommended options and plan again, when the
        `answers` policy lets the engine and every question has one, at most once in a row:
        when the plan its answers started asks again and no person acted since, a person
        decides (`autopilot.asked_again`). A recommended option that only closes the item is
        a person's to pick."""
        asked = self.s["plan"]["questions"]
        if self.p["answers"] != "recommended" or not all(item["recommended"] for item in asked):
            self.ask("question", "The plan has questions for a person.")
        elif any(item.get("closes", 0) == item["recommended"] for item in asked):
            self.ask("closeRecommended", "A recommended answer only closes the work item.")
        elif again:
            self.ask("askedAgain", "The plan asks again after the engine's answers.")
        else:
            self.s["asked"].append("answers")
            text = {"name": "answers", "ref": self.s["plan"]["text"]["ref"]}
            self.do("show", what="answers", text=text)
            self.start_plan()

    def split_or_ask(self):
        """File the parts of a plan that is too large, all of them, or plan once more for what
        a split needs; after a second such plan, or at the deepest level of parts, a person
        decides (`autopilot._split`)."""
        plan = self.s["plan"]
        why = self.unsplit()
        if why is None:
            plan["approved"] = True
            self.start_split()
        elif why == "depth":
            self.ask("splitDepth", "The plan is too large, and the item is at the deepest level.")
        elif why in self.s["asked"]:
            self.ask(*AGAIN_FOR[why])
        else:
            self.s["asked"].append(why)
            self.start_plan()

    def plan_waits(self):
        """The plan waits for a person, for what NexKit 6 asks of them first
        (`report.needs_person`): its approval, of a plan that is too large or finds no change;
        a hand-made change; answers; or its approval. A person answers a plan's questions
        whatever attention says first."""
        plan = self.s["plan"]
        ref = plan["text"]["ref"]
        self.move("awaitingApproval")
        if plan["tooLarge"]:
            self.need("approval", "The plan is too large, and a person decides how to go on.", ref)
        elif plan["noChange"]:
            text = "The plan finds that no change is needed, and a person decides."
            self.need("approval", text, ref)
        elif plan["protectedPaths"]:
            self.need("handMadeChange", "The change needs paths that only a person changes.", ref)
        elif plan["questions"]:
            self.need("question", "The plan has questions for a person.", ref)
        else:
            self.need("approval", "The plan waits for its approval.", ref)

    def finished_implement(self, output, number):
        self.finished_change(output, number, "implement")

    def finished_fix(self, output, number):
        self.finished_change(output, number, "fix")

    def finished_change(self, output, number, stage):
        # Whether the engine's answers started this round, with no person acting since.
        again = "answers" in self.s["asked"]
        self.forget_answers()
        if output["status"] == "blocked":
            self.blocked(output, number, stage, again)
        elif stage == "implement" and filled(output.get("no_change")) and not self.s["change"]:
            self.move("noChange")
            self.close("completed", {"name": "noChange", "ref": number})
        else:
            self.do("publish", stage=stage)

    def blocked(self, output, number, stage, again):
        """A round that stopped as blocked: one whose change needs paths only a person changes
        waits for that person, whatever it asks; one that asks waits for answers, which the
        engine may give (`answer_round`); one that neither asks nor needs such paths fails
        (`autopilot._stopped`)."""
        conflicts = [
            {**item, "question": f"`{item['file']}`: {item['question']}"}
            for item in output.get("open_conflicts", [])
        ]
        named = [*output.get("questions", []), *conflicts]
        # A question with no text asks nothing (`state.questions`).
        asked = [_question(item) for item in named if str(item["question"]).strip()]
        asked = asked[: MOST["questions"]]
        if not asked and not output.get("protected_paths"):
            self.move("failed")
            self.need("blocked", filled(output.get("blocker")) or "The agent was blocked.")
            return
        self.move("awaitingApproval")
        # The round's finding that no change is needed counts only with an implement round's
        # recommendation to close the item (`report._autopilot_fields`).
        found = stage == "implement" and bool(filled(output.get("no_change")))
        self.s["questions"] = {"ref": number, "stage": stage, "items": asked, "noChange": found}
        if output.get("protected_paths"):
            self.need(
                "handMadeChange", "The change needs paths that only a person changes.", number
            )
        else:
            self.answer_round(again)

    def answer_round(self, again):
        """Answer a blocked round's questions with their recommended options, with automatic
        approval when the `answers` policy lets the engine and every question has one, at most
        once in a row, as `answer_or_ask` does for a plan (`autopilot._stopped`): an implement
        round that found no change is needed and recommends only closing the item ends it, unless
        a change exists, which a person then closes; a
        recommended option that only closes the item is a person's to pick; one that changes
        the item's scope plans it again, which on an item with a change a person decides, as
        the change was made for the scope it had; and the others start the round that asked
        again."""
        round_ = self.s["questions"]
        ref, asked = round_["ref"], round_["items"]
        answers = self.p["planApproval"] == "autoWhenClean" and self.p["answers"] == "recommended"
        if not answers or not all(item["recommended"] for item in asked):
            self.need("question", "The round has questions for a person.", ref)
        elif again:
            self.need("askedAgain", "The round asks again after the engine's answers.", ref)
        elif len(asked) == 1 and _closes(asked[0]) and round_["noChange"] and not self.s["change"]:
            self.move("noChange")
            self.close("completed", {"name": "noChange", "ref": ref})
        elif any(_closes(item) for item in asked):
            self.need("closeRecommended", "A recommended answer only closes the work item.", ref)
        elif any(item["recommended"] in item.get("replans", []) for item in asked):
            if self.s["change"]:
                text = "A recommended answer changes the scope of an item that has a change."
                self.need("replanRecommended", text, ref)
                return
            self.s["asked"] += list(ANSWERED)
            self.do("show", what="answers", text={"name": "answers", "ref": ref})
            self.start_plan()
        else:
            # The round that asked runs again, on the change it ran on.
            self.s["asked"].append("answers")
            self.do("show", what="answers", text={"name": "answers", "ref": ref})
            stage = round_["stage"]
            self.move(RUNS[stage])
            self.run(stage)

    def finished_review(self, output, number):
        rounds = self.s["rounds"]
        # The round's review ended, so its count stands.
        rounds.pop("automatic", None)
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
        # The verdict and the criteria decide, as in NexKit 6: a blocking finding of a review
        # that approves goes to the next fix round only if one runs (`report.apply_criteria`).
        if (
            output["verdict"] == "request_changes"
            or "unmet" in rounds["criteria"]
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
        before = self.version
        self.took_change(result)
        # A fix round that left the change as it was found the cause outside it: its checks
        # and review run again on the same version, and what still fails is a person's.
        rounds = self.s["rounds"]
        rounds.pop("recheck", None)
        if action["stage"] == "fix" and self.version == before:
            rounds["recheck"] = self.version
        # The checks of the version a fix round published compare with the checks before it.
        rounds.pop("fixed", None)
        if action["stage"] == "fix":
            rounds["fixed"] = self.version
        # The observation may name a later version: take it before the checks, so that a
        # conflict on it follows the policy once.
        if not self.reconcile():
            self.check()
        if not any(item["kind"] == "runChecks" for item in self.s["pending"]):
            # The change conflicts, so its checks do not run: the checks after the next round
            # have none of this round's to compare with (`report._skipped_conflicting`).
            for key in ("failing", "repeated", "fixed"):
                rounds.pop(key, None)

    def check(self):
        if self.conflicted():
            return
        self.move("checking")
        self.do("runChecks", changeVersion=self.version)

    def finished_runChecks(self, result, action):
        failing = [
            check for check in result["checks"] if not check["passed"] and not check["skipped"]
        ]
        self.s["rounds"]["signatures"] = [
            signature(check["signature"] or check["name"]) for check in failing
        ][: MOST["signatures"]]
        # The item about a failure the base branch has, which every failing check matches.
        self.s["rounds"]["baseFailure"] = (result.get("baseFailure") or None) if failing else None
        self.repeats(failing)
        if self.review_only or not failing:
            self.review()
        elif self.p["reviewAfterFailingChecks"] == "always":
            self.review()
        else:
            self.next_fix()

    def repeats(self, failing):
        """Keep the failing checks, each by the digest of its name and signature, and, for the
        checks of a fix round, which of them failed with the same signature before it: the
        next automatic fix round diagnoses those first (`report._report`'s
        `feedback.repeated`). A check that fails twice in one result counts by its last
        failing result."""
        rounds = self.s["rounds"]
        before = set(rounds.pop("failing", []))
        now = {check["name"]: check["signature"] or check["name"] for check in failing}
        now = {name: failure_key(name, text) for name, text in now.items()}
        after_fix = rounds.pop("fixed", None) == self.version
        repeated = [clip(name, "check") for name, key in now.items() if after_fix and key in before]
        rounds.pop("repeated", None)
        if now:
            rounds["failing"] = list(now.values())[: MOST["failing"]]
        if repeated:
            rounds["repeated"] = repeated[: MOST["repeated"]]

    def review(self):
        self.move("reviewing")
        self.run("review")

    def finished_accept(self, result, action):
        if result["accepted"]:
            self.move("accepted")
            self.close("completed")
        elif result["refusal"] == "conflict":
            self.on_conflict_found()
        elif result["refusal"] == "checkFailed":
            # A required check failed at the accept: a fix round, as after the round's own
            # checks, which waits while that failure is the base branch's
            # (`report._required_failed`). Not a recheck: those checks are not the round's.
            rounds = self.s["rounds"]
            rounds["baseFailure"] = result.get("baseFailure") or None
            rounds.pop("recheck", None)
            self.next_fix()
        elif result["refusal"] == "notAccepted":
            self.move("failed")
            self.need("failed", result["text"] or "The accept failed.")
        else:
            self.move("ready")
            self.need("acceptRefused", result["text"] or "The accept was refused.")

    def finished_replaceChange(self, result, action):
        self.took_change(result)
        self.run("implement")

    def finished_createParts(self, result, action):
        ended = {part["item"]: part["state"] for part in self.s["parts"]}
        self.s["parts"] = [
            {"item": part["item"], "state": ended.get(part["item"])}
            for part in result["parts"][: MOST["split"]]
        ]
        self.wait("parts")
        self.parts_ended()

    def finished_cancel(self, result, action):
        # The cancelled action's late result changes nothing, so its spending comes here.
        self.spend(result)


def _bare(title):
    return title.strip().rstrip(ENDS).casefold()


def _question(question):
    """A question as the state keeps it: its texts clipped, at most MOST["options"] options,
    and no number that names an option it left out. A round's question keeps the options that
    only a new plan carries out, but the one that only closes the item
    (`state.replanning_options`)."""
    options = [clip(option, "option") for option in question["options"][: MOST["options"]]]
    kept = {
        "question": clip(question["question"], "question"),
        "options": options,
        "recommended": _option(question["recommended"], options),
    }
    if "closes" in question:
        kept["closes"] = _option(question["closes"], options)
    if "replans" in question:
        closes = kept.get("closes", 0)
        kept["replans"] = sorted(
            {n for n in question["replans"] if 0 < n <= len(options) and n != closes}
        )
    return kept


def _option(number, options):
    """An option's number from 1 if the question keeps that option, else 0 for none."""
    return number if 0 < number <= len(options) else 0


def _closes(question):
    """Whether a question's recommended option only closes the item."""
    return 0 < question["recommended"] == question.get("closes", 0)


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
        # Even after an event that changed nothing else, so that a repeated observation
        # catches up (docs/engine.md#states).
        changed = work.reconcile() or changed
    if not changed:
        return before, [], False
    if work.review_only:
        _never_writes(work.actions)
    work.s["revision"] += 1
    work.s["recentEvents"] = [*work.s["recentEvents"], event["id"]][-RECENT:]
    if fit(work.s) is None:
        raise EngineError(f"The state document passes the limit of {STATE_LIMIT} bytes.")
    # The engine never answers a state its own schema refuses, such as more pending actions
    # than `maxItems` allows when a host never reports them: it refuses the event instead.
    found = check(work.s, "state.schema.json", "state")
    if found:
        raise EngineError(f"The answer would break the state's schema: {'; '.join(found[:3])}")
    return work.s, work.actions, True


def _never_writes(actions):
    """A review-only item is never planned, changed, fixed, accepted or closed
    (docs/engine.md#states)."""
    for action in actions:
        if action["kind"] in (*WRITES, "close") or action.get("stage", "review") != "review":
            raise EngineError(f"A review-only item never gets {action['kind']}.")
