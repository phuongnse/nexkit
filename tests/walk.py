"""Random walks of the engine against its invariants.

usage: python3 tests/walk.py [--walks N] [--seed S] [--steps M] [--tree PATH] [--walk W] [--show]
                             [--jobs J]

Each walk starts an item with random policies, which change now and then, and sends up to
`--steps` events: every kind the conformance cases send, results of each action it has
pending (plans and rounds with questions, checks of one to four named checks failing with
one of two signatures, some skipped, with and without a failure the base branch has, reviews,
publishes that conflict or leave the version as it was, refused accepts, usage limits with and
without a reset, transient stops), holds and their end, pushes, conflicts and lost actions.
After every step it checks the invariants below; `INVARIANTS` names them by the rule each
guards. Walk W plays the same events whatever else runs (`--walk W --show` prints its steps, and
those of the application's walk that follows it).
After `APPLICATION` of the walks, one more walk plays with an application's policies, as
docs/engine-hosts.md#what-a-host-decides gives them, and keeps them for the whole walk. Some
walks start from the example of an item that accepts, which a walk from a new item seldom
reaches. Each rule is checked both ways where it can be: a count neither passes its policy nor
fails to come back when its round stops. A random `sweep` request for each walk checks the
sweep's rules.
It prints how often each invariant broke, with an example, and exits 1 when one broke that
`KNOWN` does not list, or when the engine refuses more than `REFUSED` of the events, as walks it
refuses check nothing.

`tests/test_walk.py` plays a fixed sample of walks in the unit tests. This script plays longer
ones, and `tests/mutants.py` plays it against a broken engine. A change to an engine rule adds
invariants for its rules here, before NexKit 7.0 and after it.
"""

import argparse
import collections
import contextlib
import copy
import functools
import json
import multiprocessing
import os
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

INVARIANTS = {
    "schema": "every answer fits the protocol's schema, and the engine never fails on one",
    "transition": "only the transitions that the states table and its rules name",
    "outbox": "each action answered is pending, whole, and leaves only with its result, its "
    "cancel or its loss; a close is pending only on a final item; a notice stands",
    "carried": "every open item has work, a wait, a hold or attention, so none is stuck",
    "counts": "a fix round is counted when its run is issued, one at a time, within `fixRounds`",
    "budget": "each agent run's budget is its stage's, and an implement, fix or review run's is "
    "capped by what the item has left; none of those starts on a spent item budget, and an item "
    "fails for its budget only when it has none left",
    "give-back": "only an automatic fix round stopped before its review ends gives its count "
    "back, and the count of one whose change conflicted stands, or that a person's push "
    "found published",
    "due": "a round that waits to be issued (`due`) is in `fixing` with no run, and waits",
    "automatic": "`automatic` marks only a counted fix round that its review has not judged",
    "diagnosis": "only an automatic fix round diagnoses, exactly the checks that failed the "
    "same way before and after the fix round before it",
    "usage-limit": "a usage limit waits for its reset, or for a person, counting nothing, and "
    "the same run starts again at its tick, under a hold too",
    "retry": "a transient stop runs the same stage again after each wait of `retryWaits` in "
    "turn, counting nothing, and waits for a person after the last; the count of runs again "
    "starts afresh with a request and with a new plan, implement or fix run",
    "lost": "a lost agent run or checks that the engine started by itself start again after each "
    "wait of `lostWaits` in turn, counting nothing, once more only when the agent ran, and a "
    "person's work and what is past the last wait wait for a person; a hold keeps the start "
    "back; the count starts afresh when work finishes",
    "hold": "no conflict round starts and no change is made again while a hold is on",
    "answers": "the engine answers a round's questions at most once in a row, and only under "
    "`autoWhenClean` with `answers: recommended`",
    "questions": "a round's questions are kept only while they wait",
    "after-failure": "an item leaves `failed` for `planning` on a comment or a blocker's close "
    "only under `afterFailure: replan`, with no change and attention `blocked` or "
    "`roundFailed`, and a collaborator's comment on such an item always plans it again",
    "split": "an item with a change is split only with `withdrawChange` naming it, and only from "
    "a plan that a round which ran out of time asked for (`supersede`)",
    "parent-end": "a split ends as accepted only when every part ended, one was accepted and "
    "`parentEnd` is `whenPartAccepted`, and under `person` its parts' end leaves it to a person",
    "trust": "a `trust` changes the item only from a writer on an untrusted stop: with no change "
    "it plans, once; with a change it starts one automatic fix round, counted, or waits for the "
    "end of a hold, or for a person when no round is left; a stopped merge runs its checks",
    "start": "a `requested` start starts work only on an item that waits to start, with no hold "
    "and no blocker wait, and on a free `new` item it starts exactly one plan run",
    "accept-conflict": "a merge refused for a conflict starts a conflict round only under "
    "`resolve` with one left, in `fixing`, or makes the change again only under `redo` with one "
    "left; otherwise, and for a conflict found only at the end (`needsPerson`), it needs a person, "
    "in `ready` with `acceptRefused`",
    "sweep": "a sweep answers every item once, starts within `maxParallel` except an awaited item "
    "(always started, flagged beyond the limit), none on a limited account, in the order of "
    "priority, group, wave and place, and each wait counts those running before it",
    "required-checks": "a start of the wait for required checks lives only in `accepting`, on "
    "the change's version; a refusal for them that have not passed leaves one wait, a tick and "
    "the first start of that version; a tick more than six hours after it needs a person, "
    "unless the version is observed behind its base and the conflict policy is not `person`",
    "accept-behind": "a merge refused as behind its base asks for an update only when the "
    "conflict policy is not `person` and `conflictRounds` has an update left, which counts apart "
    "from conflict rounds when it succeeds; otherwise, and for an update that failed, it needs a "
    "person, in `ready` with `acceptRefused`",
    "behind-checks": "checks that run on a version observed behind its base, with no conflict and "
    "no hold, stop and the branch is updated when the conflict policy is not `person` and "
    "`conflictRounds` has an update left: one `updateChange`, the item staying in `checking`; "
    "otherwise a step in `checking` asks for no update",
    "accept-again": "an accept refused for a reason that waits for a person is asked for again "
    "only when the latest observation says the current version is acceptable, with no conflict, "
    "no hold and no review-only item, and the version was not refused while acceptable",
    "approvals": "a person's approval of the change accepts it only under `autoWhenPassed`: "
    "under `person`, `changeApproved` never issues an `accept` and changes nothing of a change "
    "that waits for its accept",
    "refusals": "a refused accept that no one retries, a moved head, a refusal after the base "
    "branch kept moving or one only a person clears, waits for a person in `ready`, and one "
    "that an untrusted edit stopped does so with `untrusted`; one that was not accepted fails "
    "the item",
}

# What each agent run spends in a walk: the item's own budget counts it.
BUDGET_TEXTS = (
    "The work item spent its token budget.",
    "The work item's time budget ran out.",
)
SPENT = {"input": 100, "cached": 0, "output": 50}
# Broken invariants that a known issue covers: (invariant, a text the problem starts with).
KNOWN = {
    ("transition", "('dropped', 'accepted')"): "#348",
    ("transition", "('dropped', 'noChange')"): "#348",
}

TITLE = "Cache the results"
# The share of walks that take an application's policies, fixed for the whole walk.
APPLICATION = 0.15
# The share of walks that start from the example of an item that accepts.
ACCEPTING = 0.2
# The share of those that an import left waiting to start, which no event of a walk makes.
HELD = 0.2
# The share that start from the example of a new item that waits to start, as the import builds
# it, and for a blocker too, which a walk from a created item reaches only by its own events.
NEW = 0.1
# The share that start from the example of a split whose parts end, which a walk from a new item
# reaches only through a plan that splits.
SPLIT = 0.1
# The share that start from the example of a failed or ready item that an untrusted edit
# stopped, which no event of a walk makes: only the host sees who edited the item.
VOUCH = 0.1
# The most events the engine may refuse, as a share of all: the walker sends some it may.
REFUSED = 0.1
ROLES = ["decides", "writer", "reader", "outside"]
# How long after the first refusal for required checks a walk's tick comes: around the six
# hours that end the wait.
TICKS = [0, 3600, 5 * 3600 + 3540, 6 * 3600, 6 * 3600 + 1, 7 * 3600]
SIX_HOURS = 6 * 3600
TIME = "%Y-%m-%dT%H:%M:%SZ"
CHECKS = ["unit", "lint", "e2e", "types"]


def _later(seconds):
    """The conformance runner's time for an event, `seconds` later."""
    start = datetime(2026, 11, 23, 15, 1, 11, tzinfo=UTC)
    return (start + timedelta(seconds=seconds)).strftime(TIME)


class Walker:
    """Plays walks of the engine that `sys.path` finds. Use it as a context: it reads each file
    that does not change during a walk once, and puts the reads back when the context ends."""

    def __init__(self):
        from nexkit import engine, protocol
        from nexkit.engine.machine import DEFAULTS, _later
        from nexkit.protocol.size import failure_key
        from nexkit.protocol.stages import RUNS
        from tests import test_conformance as tc

        self.engine, self.protocol, self.tc = engine, protocol, tc
        self.stack = contextlib.ExitStack()
        self.defaults, self.failure_key, self.later = dict(DEFAULTS), failure_key, _later
        self.runs = RUNS
        self.suite = tc.ConformanceTest("test_the_cases")
        self.shorts = collections.defaultdict(list)
        self.results = collections.defaultdict(list)
        for path in tc.cases():
            for step in json.loads(path.read_text())["steps"]:
                self.shorts[step["event"]["kind"]].append(step["event"])
        for event in self.shorts["actionFinished"]:
            self.results[event["action"]].append(event["result"])
        self.problems = collections.Counter()
        self.examples = {}
        self.known = collections.Counter()
        self.errors = collections.Counter()
        self.pairs = collections.Counter()
        self.events = 0
        self.applied = 0
        self.last_checks = None

    def __enter__(self):
        # The schemas the engine checks every state against stay the same objects.
        refs = functools.cache(self.protocol._refs)
        table = functools.cache(self.tc.table_transitions)
        self.stack.enter_context(mock.patch.object(self.protocol, "_refs", refs))
        self.stack.enter_context(mock.patch.object(self.tc, "table_transitions", table))
        return self

    def __exit__(self, *error):
        return self.stack.__exit__(*error)

    # -- what a walk sends ------------------------------------------------------------------

    def policies(self, rng):
        found = {}
        for name, values in {
            "planApproval": ["person", "autoWhenClean"],
            "answers": ["never", "recommended", "recommended"],
            "afterFailure": ["person", "replan"],
            "splits": [True, False],
            "splitParts": [1, 2, 10],
            "splitDepth": [1, 2, 3],
            "accept": ["person", "autoWhenPassed"],
            "conflicts": ["person", "resolve", "resolve", "redo"],
            "conflictRounds": [1, 1, 2, 3],
            "fixRounds": [0, 1, 2, 3],
            "whenRoundsRunOut": ["person", "failed"],
            "reviewAfterFailingChecks": ["always", "always", "onlyAfterPassing"],
            "retryWaits": [[], [0], [0, 5], [15, 30, 60]],
            "lostWaits": [[], [0], [0, 30], [0, 30, 60, 120]],
            "parentEnd": ["whenPartAccepted", "whenPartAccepted", "person"],
        }.items():
            if rng.random() < 0.85:
                found[name] = rng.choice(values)
        # Drawn from a generator of its own, which takes nothing from `rng`, so that the walks
        # that this policy joined play as they did without it.
        own = random.Random(hash(rng.getstate()))
        if own.random() < 0.85:
            found["budgets"] = own.choice(
                [
                    {},
                    {"implement": {"seconds": 2700}},
                    {"fix": {"tokens": 1000, "modelCalls": 5}, "review": {"seconds": 600}},
                    {"plan": {"seconds": None}, "implement": {"modelCalls": None, "tokens": 50000}},
                ]
            )
        if own.random() < 0.4:
            item = own.choice(
                [{"tokens": 300}, {"seconds": 1800}, {"tokens": 2000, "seconds": 7200}]
            )
            found["budgets"] = {**found.get("budgets", {}), "item": item}
        return found

    @staticmethod
    def application_policies(rng):
        """An application's policies, as docs/engine-hosts.md#what-a-host-decides gives them:
        a person approves a plan and a split, the engine reviews only after passing checks and
        accepts what passed, a conflict makes the change again, and each item has a budget."""
        return {
            "planApproval": "person",
            "answers": "never",
            "reviewAfterFailingChecks": "onlyAfterPassing",
            "accept": "autoWhenPassed",
            "fixRounds": 3,
            "whenRoundsRunOut": "person",
            "conflicts": "redo",
            "conflictRounds": rng.choice([1, 2, 3]),
            "splits": True,
            "splitParts": 10,
            "splitDepth": rng.choice([1, 2, 3]),
            "parentEnd": "whenPartAccepted",
            "afterFailure": "person",
            "retryWaits": rng.choice([[0], [15]]),
            "lostWaits": [],
            "budgets": {
                "item": {
                    "tokens": rng.choice([300, 2000, 50000]),
                    "seconds": rng.choice([1800, 7200]),
                },
                "fix": {"modelCalls": 5},
            },
        }

    def plan_output(self, rng):
        found = copy.deepcopy(self.tc.FIXTURES["plan"])
        questions = []
        for n in range(rng.choice([0, 0, 1, 2])):
            options = ["Keep it", "Drop it", "Close it"][: rng.choice([2, 3])]
            questions.append(
                {
                    "question": f"Q{n}?",
                    "options": options,
                    "recommended": rng.choice([0, 1, 2, len(options)]),
                    "closes": rng.choice([0, 0, len(options)]),
                }
            )
        found["questions"] = questions
        found["too_large"] = rng.random() < 0.4
        titles = [
            rng.choice([f"Part {i}", TITLE, TITLE + ": more", "Other"])
            for i in range(1, rng.choice([0, 2, 3, 11, 10]) + 1)
        ]
        found["split"] = [
            {"title": t, "body": "x", "depends_on": [i] if i else []} for i, t in enumerate(titles)
        ]
        found["no_change"] = rng.choice(["", "", "Done already."])
        found["protected"] = rng.choice([[], [], ["a"]])
        return found

    def round_output(self, rng, stage):
        questions = []
        for n in range(rng.choice([0, 1, 1, 2])):
            options = ["Keep it", "Split it", "Close it"][: rng.choice([2, 3])]
            question = {
                "question": rng.choice([f"Q{n}?", f"Q{n}?", "  "]),
                "options": options,
                "recommended": rng.choice([0, 1, 2, len(options), 7]),
                "replans": rng.choice([[], [2], [1, 2], [len(options)]]),
            }
            if stage == "implement":
                question["closes"] = rng.choice([0, 0, len(options)])
            questions.append(question)
        name = "implementQuestions" if stage == "implement" else "fixQuestions"
        found = copy.deepcopy(self.tc.FIXTURES[name])
        found["status"] = rng.choice(["blocked", "blocked", "done"])
        found["questions"] = questions
        found["protected"] = rng.choice([[], [], ["a.yml"]])
        if stage == "implement":
            found["no_change"] = rng.choice(["", "", "Done already."])
        elif rng.random() < 0.2:
            found["open_conflicts"] = [
                {
                    "location": "a.py",
                    "base_change": "x",
                    "this_change": "y",
                    "question": "Which?",
                    "options": ["base", "pr"],
                    "recommended": rng.choice([0, 1]),
                }
            ]
        return {**self.tc.AGENT, "output": found}

    def checks(self, rng):
        if self.last_checks and rng.random() < 0.4:
            # The same checks fail the same way again, as after a fix round that fixed nothing:
            # what a diagnosis round looks into.
            return copy.deepcopy(self.last_checks)
        found = []
        for name in rng.sample(CHECKS, rng.randint(1, 4)):
            passed = rng.random() < 0.4
            found.append(
                {
                    "name": name,
                    "passed": passed,
                    "skipped": not passed and rng.random() < 0.05,
                    "signature": None if passed else rng.choice(["sig-A", "sig-A", "sig-B", None]),
                    "knownBaseFailure": False,
                }
            )
        failing = any(not c["passed"] for c in found)
        base = rng.choice(["7", "8"]) if failing and rng.random() < 0.15 else None
        self.last_checks = {"checks": found, "baseFailure": None}
        return {"checks": found, "baseFailure": base}

    def finish(self, rng, state, action):
        tc = self.tc
        kind = action["kind"]
        if kind == "runAgent":
            stage = action["stage"]
            if rng.random() < 0.1:
                # A usage limit, with its reset or with none when the host does not resume.
                reset = rng.choice(["2026-11-25T04:40:00Z", None])
                return {**tc.AGENT, "output": None, "limit": {"message": "limit", "reset": reset}}
            if rng.random() < 0.05:
                return tc.result_of("@transient")
            if stage == "plan":
                if rng.random() < 0.1:
                    return tc.result_of("@error")
                return {**tc.AGENT, "output": self.plan_output(rng)}
            if stage in ("implement", "fix") and rng.random() < 0.5:
                return self.round_output(rng, stage)
            pool = [
                r
                for r in self.results["runAgent"]
                if isinstance(r, str) and r.startswith("@agent:" + stage)
            ]
            if stage != "review":
                pool += ["@timeout", "@error", "@untrusted"]
            return tc.result_of(rng.choice(pool or ["@error"]))
        if kind == "runChecks":
            if rng.random() < 0.6:
                return self.checks(rng)
        if kind == "accept" and rng.random() < 0.6:
            refusal = rng.choice(
                ["checkFailed", "checkFailed", "conflict", "behind", "notAccepted"]
                + ["transient", "headMoved", "needsPerson", "untrusted"] * 2
                + ["waitingForChecks"] * 8
            )
            found = {"accepted": False, "refusal": refusal, "text": "x"}
            if rng.random() < 0.5:
                found["baseFailure"] = rng.choice(["7", "8", None])
            return found
        if kind == "publish" and rng.random() < 0.4:
            cid = (state["change"] or {}).get("id") or "308"
            now = (state["change"] or {}).get("version")
            version = now if now and rng.random() < 0.3 else f"v{rng.randint(5, 99)}"
            seen = {"changeId": cid, "version": version, "merged": False, "empty": False}
            seen["conflicts"] = rng.random() < 0.3
            return {"change": {"id": cid, "version": version}, "refused": None, "observed": seen}
        if kind == "updateChange":
            cid = (state["change"] or {}).get("id") or "308"
            if rng.random() < 0.25:
                return {"change": None, "refused": rng.choice(["x", None]), "observed": None}
            version = f"v{rng.randint(5, 99)}"
            seen = {"changeId": cid, "version": version, "merged": False, "empty": False}
            seen["conflicts"] = rng.random() < 0.1
            if rng.random() < 0.2:
                seen["behind"] = True
            found = {"change": {"id": cid, "version": version}, "refused": None}
            return {**found, "observed": seen if rng.random() < 0.9 else None}
        pool = self.results.get(kind)
        if not pool:
            return None
        found = copy.deepcopy(rng.choice(pool))
        return tc.result_of(found) if isinstance(found, str) else found

    def tick(self, rng, state, wait):
        """A tick of `wait`; one of required checks comes at one of `TICKS` after their start."""
        found = {"kind": "tick", "wait": wait["id"]}
        since = (state.get("checksSince") or {}).get("at")
        if wait["for"] == "requiredChecks" and since:
            late = datetime.strptime(since, TIME).replace(tzinfo=UTC) + timedelta(
                seconds=rng.choice(TICKS)
            )
            found["at"] = late.strftime(TIME)
        return found

    def short(self, rng, state):
        tc = self.tc
        if state is None:
            event = {
                "kind": "created",
                "title": rng.choice([TITLE, "", "x" * 300]),
                "depth": rng.choice([0, 1, 2, 3, 5]),
            }
            if rng.random() < 0.2:
                event["plan"] = self.plan_output(rng)
            if rng.random() < 0.2:
                event["blockers"] = rng.sample(["7", "8"], rng.randint(1, 2))
            if rng.random() < 0.2:
                event["splitAsked"] = True
            return event
        kind = rng.choice(
            ["requested", "requested", "planApproved", "answered", "commented"]
            + ["actionFinished"] * 3
            + ["actionLost", "dropRequested", "changeObserved", "tick", "held", "released"]
            + ["reopened", "changesRequested", "changeApproved", "blockerClosed", "partEnded"]
            + ["stopped", "textChanged", "push", "conflict", "acceptable"]
        )
        busy = [a for a in state["pending"] if a["kind"] not in ("needPerson", "show", "close")]
        busy = [a for a in busy if a["kind"] != "cancel"]
        if busy and rng.random() < 0.45:
            kind = "actionFinished"
        if state["state"] in ("new", "awaitingApproval") and rng.random() < 0.3:
            kind = "requested"
        # Lost work again and again, and a hold set while its restart waits.
        again = state.get("again") or {}
        if again.get("lost") and rng.random() < 0.4:
            if any(w["until"] is not None for w in state["waits"]) and not state["hold"]:
                return {"kind": "held"}
            kind = "actionLost" if busy else kind

        if state["state"] == "fixing" and not state["hold"] and rng.random() < 0.15:
            kind = "held"
        # A change that waits for its accept gets a person's approval, now and then of another
        # version.
        if state["state"] == "ready" and (state["attention"] or {}).get("reason") in (
            "accept",
            "acceptRefused",
            "reopened",
        ):
            if rng.random() < 0.3:
                return {"kind": "changeApproved", **rng.choice([{}, {"changeVersion": "v0"}])}
        if state["rounds"].get("due") and rng.random() < 0.3:
            kind = rng.choice(["released", "blockerClosed", "requested", "push", "held"])
        open_parts = [part for part in state["parts"] if part["state"] is None]
        if state["state"] == "split" and open_parts and rng.random() < 0.4:
            end = rng.choice(["accepted", "accepted", "noChange", "dropped", "failed"])
            return {"kind": "partEnded", "part": rng.choice(open_parts)["item"], "state": end}
        checks = [wait for wait in state["waits"] if wait["for"] == "requiredChecks"]
        if checks and rng.random() < 0.6:
            if state["change"] and rng.random() < 0.25:
                # The merge waits on a branch that is behind its base, or no longer is.
                return {"kind": "changeObserved", "conflicts": False, "behind": rng.random() < 0.8}
            return self.tick(rng, state, checks[0])
        if state["waits"] and rng.random() < 0.25:
            wait = rng.choice(state["waits"])
            if wait["until"] is not None:
                return self.tick(rng, state, wait)
            if wait["for"] == "baseFailure":
                return {"kind": "blockerClosed", "blocker": wait["on"] or "7", "completed": True}
        if kind == "actionFinished":
            if not busy:
                kind = "requested"
            else:
                action = rng.choice(busy)
                result = self.finish(rng, state, action)
                if result is None:
                    return None
                if action["kind"] == "runAgent" and result.get("tokens") is None:
                    # Fixed, so that the walks play as they did without it.
                    result = {**result, "tokens": dict(SPENT)}
                found = {"kind": "actionFinished", "action": action["id"], "result": result}
                if action["kind"] == "accept":
                    # Refusals come at different times, so that the first one's is told apart.
                    late = rng.choice([0, 3600, 2 * 3600, 5 * 3600])
                    found["at"] = _later(late)
                return found
        if kind == "requested":
            whats = ["plan", "planFromScratch", "go", "fix", "review", "retry", "start", "trust"]
            if state["state"] in ("new", "awaitingApproval"):
                whats += ["go", "go", "start"]
            event = {"kind": "requested", "what": rng.choice(whats)}
            event["by"] = rng.choice([tc.PERSON, None])
            if event["what"] == "trust":
                event["by"] = rng.choice([{"id": "u", "role": role} for role in ROLES] + [None])
            if event["what"] == "fix" and event["by"] is None and rng.random() < 0.5:
                # NexKit's own automatic fix round, dispatched with nothing pending.
                event["automatic"] = True
            if rng.random() < 0.3:
                event["title"] = rng.choice([TITLE, "Other"])
            if rng.random() < 0.3:
                event["depth"] = rng.choice([0, 3])
            return event
        if kind == "answered":
            by = {"id": "u", "role": rng.choice(ROLES[:3])}
            answer = {"question": rng.choice([1, 2]), "option": rng.choice([1, 2, 3])}
            return {"kind": "answered", "by": by, "answers": [answer]}
        if kind == "blockerClosed":
            blocker = rng.choice(["7", "8"])
            return {"kind": "blockerClosed", "blocker": blocker, "completed": rng.random() < 0.5}
        if kind == "commented":
            return {"kind": "commented", "by": {"id": "u", "role": rng.choice(ROLES)}}
        if kind == "actionLost":
            if not state["pending"]:
                return None
            runs = [a for a in state["pending"] if a["kind"] in ("runAgent", "runChecks")]
            lost = rng.choice(runs if runs and rng.random() < 0.5 else state["pending"])
            event = {"kind": "actionLost", "action": lost["id"]}
            if rng.random() < 0.3:
                # The agent ran: it spent tokens.
                event["tokens"] = {"input": 10, "cached": 0, "output": 5}
            return event
        if kind == "tick":
            timed = [w for w in state["waits"] if w["until"] is not None]
            return self.tick(rng, state, rng.choice(timed)) if timed else None
        if kind == "push":
            if not state["change"]:
                return None
            version = f"v{rng.randint(100, 999)}"
            found = {"kind": "changeObserved", "version": version, "conflicts": rng.random() < 0.2}
            if rng.random() < 0.3:
                found["behind"] = True
            return found
        if kind == "acceptable":
            # The host's accept of the version would go through now, or no longer would.
            if not state["change"]:
                return None
            found = {"kind": "changeObserved", "conflicts": False}
            if rng.random() < 0.8:
                found["acceptable"] = True
            return found
        if kind == "conflict":
            if not state["change"]:
                return None
            found = {"kind": "changeObserved", "conflicts": rng.random() < 0.7}
            if rng.random() < 0.3:
                found["behind"] = rng.random() < 0.8
            return found
        pool = self.shorts.get(kind)
        return copy.deepcopy(rng.choice(pool)) if pool else None

    # -- the invariants ---------------------------------------------------------------------

    def retry_check(self, done, waits, again, after, state, new, event, actions, info):
        """A transient stop with the `again`-th wait of `waits` used: what follows it."""
        flag = self.flag
        old, rounds = state["rounds"], new["rounds"]
        if again >= len(waits):
            # An implement round on an item with no change fails for `roundFailed`.
            round_failed = state["state"] == "working" and not state["change"]
            reason = "roundFailed" if round_failed else "failed"
            if new["state"] != "failed" or (new["attention"] or {}).get("reason") != reason:
                flag("retry", "a transient stop after every wait is not for a person", info)
            return
        if new["state"] != state["state"]:
            flag("retry", "a transient stop moved the item", info)
        if after != again + 1:
            flag("retry", "a transient stop did not count one run again", info)
        if rounds["started"] != old["started"] or rounds["counted"] != old["counted"]:
            flag("retry", "a transient stop counted", info)
        if waits[again] == 0:
            runs = [a for a in actions if a["kind"] == "runAgent"]
            if not runs or runs[0]["stage"] != done["stage"]:
                flag("retry", "a wait of 0 does not run the stage again", info)
            elif runs[0].get("diagnose") != done.get("diagnose"):
                flag("retry", "a wait of 0 diagnoses otherwise", info)
            return
        until = self.later(event["at"], waits[again])
        if not any(w["for"] == "time" and w["until"] == until for w in new["waits"]):
            flag("retry", "a transient stop waits for no time or the wrong one", info)
        if not any(a["kind"] == "wait" for a in actions):
            flag("retry", "a transient stop asks for no tick", info)

    def lost_check(self, gone, state, new, event, actions, given, info):
        """An agent run or checks lost with `lostWaits` as the host gives them now: what
        follows it."""
        flag = self.flag
        waits = given["lostWaits"]
        before, after = state.get("again") or {}, new.get("again") or {}
        count, old, rounds = before.get("lost", 0), state["rounds"], new["rounds"]
        tokens = event["tokens"] or {}
        ran = gone["kind"] == "runAgent" and any(tokens.get(k, 0) > 0 for k in tokens)
        once = bool(before.get("once")) or ran
        runs_here = (
            self.runs.get(gone.get("stage")) == state["state"]
            if gone["kind"] == "runAgent"
            else state["state"] == "checking"
        )
        limit = min(1, len(waits)) if once else len(waits)
        if gone.get("by") == "person" or not runs_here or count >= limit:
            if new["state"] != "failed" or (new["attention"] or {}).get("reason") != "outage":
                why = "a person's" if gone.get("by") == "person" else "a past-the-last"
                flag("lost", f"{why} lost run does not wait for a person", info)
            return
        if (new["observed"] or {}).get("conflicts"):
            # The conflict policy acts on the checks that start again, as on any checks.
            return
        if self.stopped_for_budget(state, new):
            # A spent item budget stops the run that would start again (`budget` checks it).
            return
        if new["state"] != state["state"]:
            flag("lost", "a lost run that starts again moved the item", info)
        if after.get("lost") != count + 1:
            flag("lost", "a lost run did not count one start again", info)
        if ran and not after.get("once"):
            flag("lost", "a lost run whose agent ran did not start only once more", info)
        if rounds["started"] != old["started"] or rounds["counted"] != old["counted"]:
            flag("lost", "a lost run that starts again counted", info)
        starts = [a for a in actions if a["kind"] in ("runAgent", "runChecks")]
        if waits[count] == 0:
            free = not (state["hold"] or state["attention"])
            free = free and not [w for w in state["waits"] if w["for"] != "blocker"]
            if state["hold"] and starts:
                flag("lost", "a lost run starts again under a hold", info)
            elif free and (not starts or starts[0]["kind"] != gone["kind"]):
                flag("lost", "a wait of 0 does not start the work again", info)
            elif free and gone["kind"] == "runAgent" and starts[0]["stage"] != gone["stage"]:
                flag("lost", "a wait of 0 starts another stage", info)
            return
        until = self.later(event["at"], waits[count])
        if not any(w["for"] == "time" and w["until"] == until for w in new["waits"]):
            flag("lost", "a lost run waits for no time or the wrong one", info)
        if not any(a["kind"] == "wait" for a in actions):
            flag("lost", "a lost run asks for no tick", info)
        if starts:
            flag("lost", "a lost run starts again before its wait", info)

    @staticmethod
    def stopped_for_budget(state, new):
        """Whether this step failed the item for its own budget (the `budget` check asks that
        it was spent): the stop is that rule's, not the rule of the run that would have started."""
        text = (new["attention"] or {}).get("text")
        return text in BUDGET_TEXTS and new["attention"] != (state or {}).get("attention")

    @staticmethod
    def item_left(item, used, at):
        """What is left of an item's own budget as `(tokens, seconds)`, each None with no limit:
        tokens count input and output, and time counts from `used.since`."""
        tokens = seconds = None
        if item.get("tokens"):
            tokens = item["tokens"] - used["tokens"]["input"] - used["tokens"]["output"]
        if item.get("seconds") and used["since"]:
            began = datetime.fromisoformat(used["since"].replace("Z", "+00:00"))
            now = datetime.fromisoformat(at.replace("Z", "+00:00"))
            seconds = item["seconds"] - int((now - began).total_seconds())
        return tokens, seconds

    def flag(self, invariant, problem, info):
        for (name, start), issue in KNOWN.items():
            if name == invariant and problem.startswith(start):
                self.known[f"{invariant}: {problem} ({issue})"] += 1
                return
        key = f"{invariant}: {problem}"
        self.problems[key] += 1
        self.examples.setdefault(key, info)

    def check(self, state, event, short, pol, answer, info, memory):
        tc = self.tc
        flag = self.flag
        if "error" in answer:
            message = answer["error"]
            if "failed on this request" in message or "break the state" in message:
                flag("schema", "the engine failed: " + message[:100], info)
            self.errors[message[:70] + " " + event["kind"]] += 1
            return
        if self.protocol.check(answer, "calls/step.answer.schema.json"):
            flag("schema", "an answer breaks the schema", info)
        new = answer["state"]
        actions = answer["actions"]
        # A round keeps the policies it started with; a held or waiting round that starts now
        # takes those given now.
        eff = pol
        if state is not None and state["state"] in ("working", "fixing", "checking", "reviewing"):
            if state["policies"]:
                eff = dict(state["policies"])
                waited = event["kind"] in ("released", "blockerClosed", "tick", "requested")
                if state["rounds"].get("due") and waited:
                    eff = pol
        given = {**self.defaults, **pol}
        # Each run's budget is its stage's, as the host gives it now, with `modelCalls` only
        # when the stage sets it.
        # An implement, fix or review run's is capped by what the item has left, and none starts
        # once the item's own budget is spent (`used` is what the step left).
        left = self.item_left(given["budgets"].get("item") or {}, new["used"], event["at"])
        spent = left[0] is not None and left[0] <= 0 or left[1] is not None and left[1] <= 0
        for run in (a for a in actions if a["kind"] == "runAgent"):
            limits = given["budgets"].get(run["stage"]) or {}
            wanted = {"tokens": limits.get("tokens"), "seconds": limits.get("seconds")}
            if run["stage"] != "plan":
                for key, value in zip(("tokens", "seconds"), left, strict=True):
                    if value is not None and (wanted[key] is None or value < wanted[key]):
                        wanted[key] = value
                if spent:
                    flag("budget", f"a {run['stage']} run starts on a spent budget", info)
            if limits.get("modelCalls") is not None:
                wanted["modelCalls"] = limits["modelCalls"]
            if run["budget"] != wanted:
                flag("budget", f"a {run['stage']} run's budget is not its stage's", info)
        text = (new["attention"] or {}).get("text")
        by_budget = text in BUDGET_TEXTS and new["attention"] != (state or {}).get("attention")
        if by_budget and not spent:
            flag("budget", "an item fails for its budget while it has some left", info)
        # outbox
        try:
            memory["open"] = self.suite.check_outbox(memory["open"], event, answer, "walk")
        except AssertionError as error:
            flag("outbox", str(error).split(":")[-1].strip()[:80], info)
            memory["open"] = {a["id"]: a for a in new["pending"]}
        if new["state"] not in tc.FINAL and any(a["kind"] == "close" for a in new["pending"]):
            flag("outbox", "a close pending on an open item", info)
        attention = new["attention"] or {}
        for action in new["pending"]:
            if action["kind"] == "needPerson":
                told = (action["reason"], action["text"], action["ref"])
                if told != (attention.get("reason"), attention.get("text"), attention.get("ref")):
                    flag("outbox", "a notice no longer stands", info)
        # carried
        if new["state"] not in tc.FINAL:
            carried = [a for a in new["pending"] if a["kind"] in tc.WORK]
            if not (carried or new["waits"] or new["hold"] or new["attention"]):
                flag("carried", f"nothing carries the item on in {new['state']}", info)
        kept = new["hold"] or new["attention"]
        waits = {wait["for"] for wait in new["waits"]}
        if new["state"] == "fixing":
            running = any(
                a["kind"] in ("runAgent", "publish", "replaceChange") for a in new["pending"]
            )
            if not running and not (kept or waits & {"baseFailure", "start", "time", "usageLimit"}):
                flag("carried", "fixing with nothing to run", info)
        if new["state"] == "accepting":
            accepts = [a for a in new["pending"] if a["kind"] in ("accept", "updateChange")]
            if not (accepts or kept or new["waits"]):
                flag("carried", "accepting with no accept", info)
        # transition
        if state is not None and new["state"] != state["state"]:
            pair = (state["state"], new["state"])
            self.pairs[pair] += 1
            if pair not in tc.allowed(eff, bool(state["change"] or new["change"])):
                flag("transition", str(pair), info)
        # answers, questions, split
        if attention.get("reason") in ("question", "askedAgain", "closeRecommended"):
            if new["state"] == "awaitingApproval" and not (new["plan"] or new.get("questions")):
                flag("questions", "a question for a person with no plan or round questions", info)
        asked = new["asked"]
        if asked.count("answers") > 1 or asked.count("replan") > 1 or len(asked) > 16:
            flag("answers", "`asked` grows", info)
        if "replan" in asked and "answers" not in asked:
            flag("answers", "a replan without answers", info)
        questions = new.get("questions")
        if questions and (
            new["state"] != "awaitingApproval" or attention.get("ref") != questions["ref"]
        ):
            flag("questions", "round questions kept while they do not wait", info)
        if questions is not None and not questions["items"]:
            if attention.get("reason") != "handMadeChange":
                flag("questions", "round questions with no item", info)
        shown = [a for a in actions if a["kind"] == "show" and a["what"] == "answers"]
        finished = event["kind"] == "actionFinished"
        if shown and state is not None and "answers" in state["asked"] and finished:
            flag("answers", "answered twice in a row", info)
        if event["kind"] != "actionLost":  # a lost show is shown again, whatever the policies
            if shown and eff.get("planApproval") != "autoWhenClean":
                flag("answers", "answered without automatic approval", info)
            if shown and eff.get("answers") != "recommended":
                flag("answers", "answered without the answers policy", info)
        if attention.get("reason") == "replanRecommended" and not new["change"]:
            flag("questions", "replanRecommended without a change", info)
        if (
            new["state"] == "split"
            and state is not None
            and state["state"] != "split"
            and state["change"]
        ):
            withdrawn = any(
                a["kind"] == "withdrawChange" and a["changeId"] == state["change"]["id"]
                for a in actions
            )
            if not (withdrawn and "supersede" in state["asked"] and new["change"] is None):
                flag("split", "an item with a change was split without withdrawing it", info)
        # after-failure
        if (
            state is not None
            and state["state"] == "failed"
            and event["id"] not in (state["recentEvents"])
        ):
            replans = (
                given["afterFailure"] == "replan"
                and not state["change"]
                and (state["attention"] or {}).get("reason") in ("blocked", "roundFailed")
            )
            if event["kind"] in ("commented", "blockerClosed") and new["state"] == "planning":
                if not replans:
                    flag("after-failure", "planned again after a failure it should not", info)
            if event["kind"] == "commented" and replans and event["by"]["role"] != "outside":
                if new["state"] != "planning":
                    flag("after-failure", "a comment did not plan a failed item again", info)
        # parent-end
        if event["kind"] == "partEnded" and state is not None and state["state"] == "split":
            parts = new["parts"]
            making = any(a["kind"] == "createParts" for a in new["pending"])
            if answer["changed"] and parts and not making:
                over = all(part["state"] for part in parts)
                done = over and any(part["state"] == "accepted" for part in parts)
                wants = given["parentEnd"] == "whenPartAccepted"
                if new["state"] == "accepted" and not (done and wants):
                    flag("parent-end", "a split ended as accepted without the rule", info)
                if done and wants and new["state"] != "accepted":
                    flag("parent-end", "a split whose parts ended, one accepted, did not end", info)
                if done and not wants and attention.get("reason") != "splitIncomplete":
                    flag("parent-end", "a split left to a person says nothing", info)
        rounds = new["rounds"]
        # due, automatic
        if rounds.get("due"):
            if new["state"] != "fixing":
                flag("due", "due outside fixing", info)
            if any(a["kind"] in ("runAgent", "publish") for a in new["pending"]):
                flag("due", "due with a run pending", info)
            if not (kept or waits & {"baseFailure", "start", "time", "usageLimit"}):
                flag("due", "a due round that nothing ends", info)
        if rounds.get("automatic"):
            if new["state"] not in ("fixing", "checking", "reviewing"):
                flag("automatic", "automatic outside a round", info)
            if rounds["counted"] < 1:
                flag("automatic", "automatic with nothing counted", info)
        if rounds["counted"] < 0:
            flag("counts", "counted below zero", info)
        if state is None:
            return
        old = state["rounds"]
        fixes = [a for a in actions if a["kind"] == "runAgent" and a["stage"] == "fix"]
        # NexKit's own automatic request is a round that was dispatched already: it counts.
        dispatched = event["kind"] == "requested" and bool(event.get("automatic"))
        # counts
        most = {**self.defaults, **eff}["fixRounds"]
        if rounds["counted"] > max(most, old["counted"]):
            flag("counts", "counted past fixRounds", info)
        if rounds["counted"] > old["counted"]:
            if rounds["counted"] != old["counted"] + 1:
                flag("counts", "counted more than one at once", info)
            if not fixes:
                flag("counts", "counted without issuing a fix run", info)
            waited = event["kind"] in ("released", "blockerClosed", "tick", "requested")
            if old.get("due") and rounds["counted"] > given["fixRounds"] and waited:
                flag("counts", "a round that waited counted past the fixRounds given now", info)
            if not rounds.get("automatic") and new["state"] == "fixing":
                flag("automatic", "a counted round not marked automatic", info)
            vouched = event["kind"] == "requested" and event["what"] == "trust"
            # So is NexKit's own automatic fix round (`automatic`), which counts like a due one.
            vouched = vouched or bool(event.get("automatic"))
            if event["kind"] in ("requested", "changesRequested", "answered", "commented"):
                # A writer's vouch starts the engine's own round, which counts (`trust`).
                if not old.get("due") and not vouched:
                    flag("counts", "a person's round counted", info)
        if dispatched and fixes and rounds["conflicts"] == old["conflicts"]:
            # NexKit's own automatic fix round: counted and marked like a due one.
            if not rounds.get("automatic") or rounds["counted"] != old["counted"] + 1:
                flag("automatic", "an automatic request's fix round not counted and marked", info)
        if state["state"] == "fixing" and old.get("due") and event["kind"] == "requested":
            if short.get("what") in ("fix", "review") and answer["changed"] and not dispatched:
                if rounds["counted"] != old["counted"]:
                    flag("counts", "a person's request during a wait counted", info)
        if rounds["conflicts"] > old["conflicts"]:
            mine = {**self.defaults, **(state["policies"] or {})}["conflictRounds"]
            if rounds["conflicts"] > max({**self.defaults, **pol}["conflictRounds"], mine):
                flag("counts", "conflict rounds past conflictRounds", info)
        if rounds.get("updates", 0) > old.get("updates", 0):
            mine = {**self.defaults, **(state["policies"] or {})}["conflictRounds"]
            if rounds["updates"] > max({**self.defaults, **pol}["conflictRounds"], mine):
                flag("counts", "updates past conflictRounds", info)
        # hold
        if state["hold"] and new["hold"] and rounds["counted"] > old["counted"] and not dispatched:
            flag("hold", "an automatic fix round counted under a hold", info)
        # give-back
        # A person's push after the round published keeps its count: its change stays under the
        # push, and the push's checks judge it. While its agent runs, the push gives it back.
        pushed = (
            event["kind"] == "changeObserved"
            and event.get("version") != (state["change"] or {}).get("version")
            and not (event.get("merged") or event.get("empty"))
        )
        published = pushed and state["state"] in ("checking", "reviewing")
        if published and old.get("automatic") and rounds["counted"] < old["counted"]:
            flag("give-back", "a published round's count given back on a push", info)
        if (
            old.get("automatic")
            and not fixes
            and rounds["counted"] >= old["counted"]
            and not published
        ):
            pending = {a["id"]: a for a in state["pending"]}
            stopped = [pending.get(a["action"]) for a in actions if a["kind"] == "cancel"]
            work = [
                a
                for a in stopped
                if a
                and (
                    a["kind"] in ("runChecks", "publish")
                    or (a["kind"] == "runAgent" and a["stage"] in ("fix", "review"))
                )
            ]
            if work:
                flag("give-back", "an automatic round's work stopped and kept its count", info)
        if rounds["counted"] < old["counted"] and event["kind"] != "reopened":
            if not old.get("automatic") or rounds["counted"] != old["counted"] - 1:
                flag("give-back", "gave back without an automatic round", info)
        floor = memory.get("floor")
        if floor is not None and rounds["counted"] < floor and event["kind"] != "reopened":
            flag("give-back", "the count of a round whose change conflicted went down", info)
        if event["kind"] == "reopened":
            memory["floor"] = None
        # diagnosis
        for action in actions:
            if action["kind"] != "runAgent":
                continue
            if action["stage"] != "fix":
                if "diagnose" in action:
                    flag("diagnosis", f"a {action['stage']} run names diagnose", info)
                continue
            if "diagnose" not in action:
                flag("diagnosis", "a fix run without diagnose", info)
                continue
            if action["diagnose"]:
                if not rounds.get("automatic"):
                    flag("diagnosis", "a round that is not automatic diagnoses", info)
                if action["diagnose"] != (rounds.get("repeated") or [])[:10]:
                    flag("diagnosis", "diagnose is not the repeated failures", info)
                if rounds["conflicts"] > old["conflicts"]:
                    flag("diagnosis", "a conflict round diagnoses", info)
                if event["kind"] in ("requested", "changesRequested", "answered", "commented"):
                    if not event.get("automatic"):
                        flag("diagnosis", "a person's or an answered round diagnoses", info)
        if rounds["counted"] > old["counted"] and rounds.get("automatic"):
            for action in fixes:
                if action.get("diagnose") != (rounds.get("repeated") or [])[:10]:
                    flag("diagnosis", "an automatic fix round diagnoses other than repeated", info)
        done = None
        if finished and answer["changed"]:
            done = next((a for a in state["pending"] if a["id"] == event["action"]), None)
        # A run that stopped once the item's budget was spent fails the item for it.
        budget_stop = bool(by_budget and done and done["kind"] == "runAgent")
        if done and done["kind"] == "runChecks":
            failing = {}
            for found in event["result"]["checks"]:
                if not found["passed"] and not found["skipped"]:
                    failing[found["name"]] = found["signature"] or found["name"]
            before = set(old.get("failing", []))
            after_fix = old.get("fixed") == state["change"]["version"]
            want = [
                name
                for name, text in failing.items()
                if after_fix and self.failure_key(name, text) in before
            ]
            got = rounds.get("repeated") or []
            forgot = not got and "baseFailure" in waits
            if got != want and not forgot:
                flag(
                    "diagnosis", "repeated is not what failed the same way after a fix round", info
                )
            if not set(got) <= set(failing):
                flag("diagnosis", "repeated names a check that does not fail", info)
        # retry
        result = (event["result"] or {}) if finished else {}
        again = (state.get("again") or {}).get("transient", 0)
        after = (new.get("again") or {}).get("transient", 0)
        stopped = bool(done and done["kind"] == "runAgent" and result.get("transient"))
        stopped = stopped and not result.get("limit") and not budget_stop
        if stopped:
            wanted = {**self.defaults, **eff}["retryWaits"]
            self.retry_check(done, wanted, again, after, state, new, event, actions, info)
            if after and 0 < after <= len(wanted) and wanted[after - 1] > 0:
                memory["limited"] = (done["stage"], done.get("diagnose"), new["state"])
        if after > max(len({**self.defaults, **eff}["retryWaits"]), again):
            flag("retry", "the count of runs again passes the waits", info)
        if event["kind"] == "requested" and answer["changed"] and after and not stopped:
            flag("retry", "a request left the count of runs again", info)
        fresh = ("actionFinished", "planApproved", "answered", "changesRequested", "commented")
        if event["kind"] in fresh and not stopped and after:
            for action in actions:
                if action["kind"] == "runAgent" and action["stage"] != "review":
                    flag("retry", f"a new {action['stage']} run kept the count of runs again", info)
        # lost
        gone = None
        if event["kind"] == "actionLost" and answer["changed"]:
            gone = next((a for a in state["pending"] if a["id"] == event["action"]), None)
        if gone and gone["kind"] in ("runAgent", "runChecks"):
            self.lost_check(gone, state, new, event, actions, given, info)
        if done and done["kind"] in tc.WORK and done["kind"] != "wait":
            if {"lost", "once"} & set(new.get("again") or {}):
                flag("lost", "the count of lost work stands after work finished", info)
        counted = (new.get("again") or {}).get("lost", 0)
        if counted > max(len(given["lostWaits"]), (state.get("again") or {}).get("lost", 0)):
            flag("lost", "the count of starts again passes the waits", info)
        if event["kind"] in ("tick", "released") and answer["changed"]:
            if state["hold"] and new["hold"] and (state.get("again") or {}).get("lost"):
                if any(a["kind"] in ("runAgent", "runChecks") for a in actions):
                    flag("lost", "work starts under a hold while a lost start waits", info)
        if event["kind"] == "requested" and answer["changed"]:
            marked = [a for a in actions if a["kind"] in ("runAgent", "runChecks", "publish")]
            # A conflict round that the request leads to is the engine's own.
            own = rounds["conflicts"] > old["conflicts"]
            # So is the automatic fix round that a writer's vouch starts (`trust`).
            own = own or (event["what"] == "trust" and bool(rounds.get("automatic")))
            own = own or bool(event.get("automatic"))
            if not own and any(("by" in a) != (event["by"] is not None) for a in marked):
                flag("lost", "a request's work is marked other than by who asked", info)
        # usage-limit
        agent_run = done and done["kind"] == "runAgent"
        if agent_run and (event["result"] or {}).get("limit") and not budget_stop:
            reset = event["result"]["limit"]["reset"]
            if reset:
                if not any(w["for"] == "usageLimit" and w["until"] == reset for w in new["waits"]):
                    flag("usage-limit", "a usage limit with a reset waits for no reset", info)
                if not any(a["kind"] == "wait" for a in actions):
                    flag("usage-limit", "a usage limit asks for no tick", info)
                if new["state"] != state["state"]:
                    flag("usage-limit", "a usage limit moved the item", info)
                memory["limited"] = (done["stage"], done.get("diagnose"), new["state"])
            elif new["state"] != "failed":
                flag("usage-limit", "a usage limit with no reset is not for a person", info)
            if rounds["started"] != old["started"] or rounds["counted"] != old["counted"]:
                flag("usage-limit", "a usage limit counted", info)
        limited = memory.get("limited")
        if limited and event["kind"] == "tick" and answer["changed"]:
            memory.pop("limited")
            stage, diagnose, name = limited
            runs = [a for a in actions if a["kind"] == "runAgent"]
            if new["state"] == name and not new["attention"]:
                if not runs or runs[0]["stage"] != stage:
                    flag("usage-limit", "the tick after a usage limit runs nothing again", info)
                elif runs[0].get("diagnose") != diagnose:
                    flag("usage-limit", "the run after a usage limit diagnoses otherwise", info)
                if rounds["counted"] != old["counted"]:
                    flag("usage-limit", "the run after a usage limit counted", info)
        elif limited and answer["changed"] and new["state"] != limited[2]:
            memory.pop("limited")
        if event["kind"] == "tick" and answer["changed"] and rounds["started"] != old["started"]:
            # The reconcile after any event may start a conflict round.
            if not old.get("due") and rounds["conflicts"] == old["conflicts"]:
                flag("usage-limit", "a run again after a tick counted as started", info)
        self.required_checks(state, event, answer, done, given, flag, info)
        self.refusals(state, event, answer, done, flag, info)
        self.approvals(state, event, answer, given, flag, info)
        self.start(state, event, pol, answer, flag, info)
        self.trust(state, event, pol, answer, given, flag, info)
        self.accept_conflict(state, event, answer, done, given, flag, info)
        self.accept_behind(state, event, answer, done, given, flag, info)
        self.accept_again(state, event, answer, flag, info)
        self.behind_checks(state, event, answer, {**self.defaults, **eff}, flag, info)
        # automatic: a round that its checks or its review judged keeps its count, so only a
        # fix round issued in the same step is automatic after it.
        judged = done is not None and done["kind"] == "runChecks"
        judged = judged and not any(
            a["kind"] == "runAgent" and a["stage"] == "review" for a in actions
        )
        if done is not None and done["kind"] == "runAgent" and done["stage"] == "review":
            judged = (event["result"] or {}).get("output") is not None
        if judged and rounds.get("automatic") and not fixes:
            flag("automatic", "a judged round stays automatic", info)
        if rounds.get("due") and rounds.get("automatic"):
            flag("automatic", "a round that waits to be issued is automatic", info)
        # automatic, give-back: a fix round whose change conflicts at its publish
        if done and done["kind"] == "publish" and done.get("stage") == "fix":
            if not any(a["kind"] == "runChecks" for a in new["pending"]):
                if rounds.get("automatic"):
                    flag("automatic", "a round whose change conflicts stays automatic", info)
                if old.get("automatic"):
                    memory["floor"] = rounds["counted"]
        # hold
        if new["hold"] and rounds["conflicts"] > old["conflicts"]:
            flag("hold", "a conflict round started under a hold", info)
        if state["hold"] and new["hold"] and any(a["kind"] == "replaceChange" for a in actions):
            flag("hold", "a change made again under a hold", info)

    def reconciled(self, state, pol, like):
        """What the reconcile after any event answers `state` under `pol`: the answer to an
        event that asks nothing, a `textChanged`, with the id and time of the event `like`."""
        short = {"kind": "textChanged", "by": self.tc.PERSON}
        short["text"] = {"name": "body", "ref": "42"}
        event = self.tc.event_of(short, state, "r")
        event["id"], event["at"] = like["id"], like["at"]
        request = {"protocol": 1, "call": "step", "state": state, "event": event, "policies": pol}
        return self.engine.answer(json.loads(json.dumps(request)))

    def start(self, state, event, pol, answer, flag, info):
        """A `requested` start starts work only on an item that waits to start with no hold and
        no blocker wait, and plans a free `new` item once. A start that may not start may answer
        work only as the reconcile after any event does."""
        if state is None or event["kind"] != "requested" or event["what"] != "start":
            return
        new, actions = answer["state"], answer["actions"]
        waits = {wait["for"] for wait in state["waits"]}
        free = (
            state["itemKind"] == "change"
            and "start" in waits
            and "blocker" not in waits
            and not state["hold"]
        )
        started = [a for a in actions if a["kind"] in self.tc.WORK]
        if started and not free:
            # A conflict round that the request leads to is the reconcile's, as for `lost`: it
            # counts only if an event that asks nothing leads to the same work and state.
            same = False
            if new["rounds"]["conflicts"] > state["rounds"]["conflicts"]:
                other = self.reconciled(state, pol, event)
                if "state" in other:
                    work = lambda found: sorted(  # noqa: E731
                        (a["kind"], a.get("stage")) for a in found if a["kind"] in self.tc.WORK
                    )
                    # What the request sets by itself is not the reconcile's.
                    own = ("recentEvents", "title", "depth", "asked")
                    rest = lambda doc: {k: v for k, v in doc.items() if k not in own}  # noqa: E731
                    same = work(other["actions"]) == work(actions)
                    same = same and rest(other["state"]) == rest(new)
            if not same:
                flag("start", "a start works on an item that may not start", info)
        if free and state["state"] == "new":
            plans = [a for a in started if a["kind"] == "runAgent" and a["stage"] == "plan"]
            if len(plans) != 1 or new["state"] != "planning":
                flag("start", "a start of a free new item plans it other than once", info)

    def trust(self, state, event, pol, answer, given, flag, info):
        """A `trust` changes the item only from a writer on an untrusted stop (`failed` or
        `ready`): with no change it plans once; with a change it starts one automatic fix round,
        counted now, or waits for a hold to end, or goes to a person when `fixRounds` has none
        left; a stopped merge runs its checks. What the reconcile after any event does is not
        the request's."""
        if state is None or event["kind"] != "requested" or event["what"] != "trust":
            return
        if event["id"] in state["recentEvents"]:
            return
        new, actions = answer["state"], answer["actions"]
        by = event["by"]
        stopped = (state["attention"] or {}).get("reason") == "untrusted"
        writer = by is not None and by["role"] in ("decides", "writer")
        if not (stopped and writer and state["state"] in ("failed", "ready")):
            if answer["changed"]:
                other = self.reconciled(state, pol, event)
                if "state" not in other or other["state"]["state"] != new["state"]:
                    flag("trust", "a trust that is no writer's changed an untrusted stop", info)
            return
        seen = state["observed"]
        calm = not seen or not (seen["conflicts"] or seen["merged"] or seen["empty"])
        calm = calm and (not seen or seen["version"] == (state["change"] or {}).get("version"))
        if self.stopped_for_budget(state, new):
            # A spent item budget stops the round that the trust starts (`budget` checks it).
            return
        if not calm or not answer["changed"]:
            if calm:
                flag("trust", "a writer's trust on an untrusted stop changed nothing", info)
            return
        runs = [a for a in actions if a["kind"] == "runAgent"]
        old, rounds = state["rounds"], new["rounds"]
        if state["state"] == "ready":
            checks = [a for a in actions if a["kind"] == "runChecks"]
            if new["state"] != "checking" or len(checks) != 1:
                flag("trust", "a stopped merge does not run its checks again", info)
        elif not state["change"]:
            if new["state"] != "planning" or [a["stage"] for a in runs] != ["plan"]:
                flag("trust", "a writer's trust on an item with no change does not plan it", info)
        elif state["hold"]:
            if new["state"] != "fixing" or runs or not rounds.get("due"):
                flag("trust", "a vouched fix round under a hold does not wait for its end", info)
        elif rounds["counted"] >= given["fixRounds"] and old["counted"] >= given["fixRounds"]:
            reasons = [a["reason"] for a in actions if a["kind"] == "needPerson"]
            if runs or reasons != ["roundsUsedUp"] or rounds["counted"] != old["counted"]:
                flag("trust", "a vouched fix round starts with no automatic round left", info)
        else:
            fixes = [a for a in runs if a["stage"] == "fix"]
            if new["state"] != "fixing" or len(runs) != 1 or len(fixes) != 1:
                flag("trust", "a vouched fix round does not start one fix run", info)
            elif rounds["counted"] != old["counted"] + 1 or not rounds.get("automatic"):
                flag("trust", "a vouched fix round is not counted as automatic", info)

    def accept_conflict(self, state, event, answer, done, given, flag, info):
        """The invariants of a merge that was refused for a conflict."""
        if state is None or not answer["changed"] or not done or done["kind"] != "accept":
            return
        result = event.get("result") or {}
        kind = result.get("refusal")
        if result.get("accepted") or kind not in ("conflict", "needsPerson"):
            return
        if state["state"] != "accepting":
            return
        # What would move an item out of `accepting` by itself is not this rule's.
        seen = state["observed"]
        calm = bool(seen) and not (seen["conflicts"] or seen["merged"] or seen["empty"])
        calm = calm and seen["version"] == state["change"]["version"] and not state["hold"]
        if not calm or event.get("changeVersion") != state["change"]["version"]:
            return
        new = answer["state"]
        attention = new["attention"] or {}
        left = state["rounds"]["conflicts"] < given["conflictRounds"]
        policy = given["conflicts"] if kind == "conflict" else "person"
        want = {"resolve": "fixing", "redo": "working"}.get(policy) if left else None
        want = want or "ready"
        grew = new["rounds"]["conflicts"] - state["rounds"]["conflicts"]
        if new["state"] != want or grew != (want != "ready"):
            went = f"a conflict at the merge under {policy} went to {new['state']}"
            flag("accept-conflict", went, info)
        elif want == "ready" and attention.get("reason") != "acceptRefused":
            flag("accept-conflict", "a conflict at the merge left to a person says not why", info)

    def accept_behind(self, state, event, answer, done, given, flag, info):
        """The invariants of a merge that was refused as behind its base, and of the update
        that it asks for."""
        if state is None or not answer["changed"] or not done or state["state"] != "accepting":
            return
        new = answer["state"]
        attention = new["attention"] or {}
        result = event.get("result") or {}
        if done["kind"] == "updateChange":
            seen = state["observed"]
            if result["change"] is None:
                moved = bool(seen) and (
                    seen["version"] != state["change"]["version"]
                    or seen["conflicts"]
                    or seen["merged"]
                    or seen["empty"]
                )
                if moved:
                    # What the observation says meanwhile acts at once, whatever the update did.
                    return
                if new["state"] != "ready" or attention.get("reason") != "acceptRefused":
                    flag("accept-behind", "an update that failed does not need a person", info)
                elif new["rounds"].get("updates") != state["rounds"].get("updates"):
                    flag("accept-behind", "an update that failed counted", info)
                return
            grew = new["rounds"].get("updates", 0) - state["rounds"].get("updates", 0)
            # An update whose change conflicts follows the conflict policy, as any new version.
            conflicts = (result["observed"] or {}).get("conflicts")
            same = new["rounds"]["conflicts"] == state["rounds"]["conflicts"]
            if grew != 1 or not (same or conflicts):
                flag("accept-behind", "an update did not count apart from conflict rounds", info)
            return
        if done["kind"] != "accept" or result.get("accepted") or result.get("refusal") != "behind":
            return
        # What would move an item out of `accepting` by itself is not this rule's.
        seen = state["observed"]
        calm = bool(seen) and not (seen["conflicts"] or seen["merged"] or seen["empty"])
        calm = calm and seen["version"] == state["change"]["version"] and not state["hold"]
        if not calm or event.get("changeVersion") != state["change"]["version"]:
            return
        left = state["rounds"].get("updates", 0) < given["conflictRounds"]
        updates = given["conflicts"] != "person" and left
        asked = [a for a in answer["actions"] if a["kind"] == "updateChange"]
        if updates and (len(asked) != 1 or new["state"] != "accepting"):
            flag("accept-behind", "a branch behind with an update left is not updated", info)
        elif not updates and (asked or new["state"] != "ready"):
            flag("accept-behind", "a branch behind with no update to ask for is updated", info)
        elif not updates and attention.get("reason") != "acceptRefused":
            flag("accept-behind", "a branch behind left to a person says not why", info)
        elif asked and asked[0].get("changeVersion") != state["change"]["version"]:
            flag("accept-behind", "an update names another version than the change's", info)
        if new["rounds"]["conflicts"] != state["rounds"]["conflicts"]:
            flag("accept-behind", "a branch behind counted a conflict round", info)

    def accept_again(self, state, event, answer, flag, info):
        """The invariant of an accept refused for a reason that waits for a person: the engine
        asks for it again only when the observation that the step ends with says that the
        version is acceptable and not conflicting, nothing holds it, the item is not review-only
        and the version was not refused while it was acceptable (`refusedAcceptable`). A person's
        approval accepts as it says, and the `approvals` invariant judges it."""
        if state is None or state["state"] != "ready" or event["kind"] == "changeApproved":
            return
        if (state["attention"] or {}).get("reason") != "acceptRefused":
            return
        if not any(a["kind"] == "accept" for a in answer["actions"]):
            return
        new = answer["state"]
        seen = new["observed"] or {}
        version = (new["change"] or {}).get("version")
        if not seen.get("acceptable") or seen.get("conflicts") or seen.get("version") != version:
            flag("accept-again", "an accept is asked for again, but is not acceptable", info)
        elif new["hold"] or new["itemKind"] == "reviewOnly":
            flag("accept-again", "an accept is asked for again under a hold or a review", info)
        elif state.get("refusedAcceptable") == version:
            flag("accept-again", "an accept is asked for again for a version refused as such", info)

    def behind_checks(self, state, event, answer, eff, flag, info):
        """The invariants of checks that run on a version observed behind its base: they stop and
        the branch is updated when an update is left, and no step in `checking` asks for an
        update otherwise."""
        if state is None:
            return
        new = answer["state"]
        updates = [a for a in answer["actions"] if a["kind"] == "updateChange"]

        def left(of):
            room = (
                eff["conflicts"] != "person"
                and of["rounds"].get("updates", 0) < eff["conflictRounds"]
            )
            return room and not new["hold"] and of["itemKind"] != "reviewOnly"

        def live(of):
            seen = of["observed"]
            running = any(a["kind"] == "runChecks" for a in of["pending"])
            coming = any(
                a["kind"] in ("publish", "replaceChange", "updateChange") for a in of["pending"]
            )
            return bool(
                of["state"] == "checking"
                and seen
                and seen.get("behind")
                and of["change"]
                and seen["version"] == of["change"]["version"]
                and not (seen["conflicts"] or seen["merged"] or seen["empty"])
                and running
                and not coming
                and left(of)
            )

        if live(new):
            flag(
                "behind-checks", "checks run on a branch behind its base with an update left", info
            )
        if updates and state["state"] == "checking" and event["kind"] != "actionFinished":
            if len(updates) != 1 or not left(state) or new["state"] != "checking":
                flag("behind-checks", "a step in checks asked for an update it may not", info)
            elif any(a["kind"] == "runChecks" for a in state["pending"]) and not any(
                a["kind"] == "cancel" for a in answer["actions"]
            ):
                # Checks that were lost, and wait for a hold to end, have no run to cancel.
                flag("behind-checks", "an update in checks left their run going", info)

    def required_checks(self, state, event, answer, done, given, flag, info):
        """The invariants of the wait for required checks and its six hours."""
        new, actions = answer["state"], answer["actions"]
        since = new.get("checksSince")
        version = (new["change"] or {}).get("version")
        if since and (new["state"] != "accepting" or since["version"] != version):
            flag("required-checks", "a start outside accepting or of another version", info)
        if state is None or not answer["changed"]:
            return
        seen = state["observed"]
        # What would move an item out of `accepting` by itself is not this rule's.
        calm = bool(seen) and not (seen["conflicts"] or seen["merged"] or seen["empty"])
        calm = calm and seen["version"] == state["change"]["version"] and not state["hold"]
        result = event.get("result") or {}
        if done and done["kind"] == "accept" and result.get("refusal") == "waitingForChecks":
            if state["state"] != "accepting" or not calm:
                return
            held = [wait for wait in new["waits"] if wait["for"] == "requiredChecks"]
            first = state.get("checksSince") or {}
            kept = first.get("version") == state["change"]["version"]
            want = {"version": version, "at": first["at"] if kept else event["at"]}
            asked = [a for a in actions if a["kind"] == "wait"]
            if new["state"] != "accepting" or len(held) != 1 or len(asked) != 1:
                flag("required-checks", "a refusal for required checks leaves no single wait", info)
            elif asked[0]["wait"] != held[0]["id"] or asked[0]["until"] != held[0]["until"]:
                flag("required-checks", "the tick asked for is not the wait's", info)
            elif since != want:
                flag("required-checks", "the start is not the first refusal on this version", info)
            return
        ended = next((w for w in state["waits"] if w["id"] == event.get("wait")), None)
        if event["kind"] != "tick" or not ended or ended["for"] != "requiredChecks":
            return
        first = state.get("checksSince")
        if state["state"] != "accepting" or not first:
            return
        late = datetime.strptime(event["at"], TIME) - datetime.strptime(first["at"], TIME)
        waited = first["version"] == state["change"]["version"] and late.total_seconds() > SIX_HOURS
        attention = new["attention"] or {}
        # A branch behind its base keeps its merge, unless a person decides what is behind.
        behind = bool(seen and seen.get("behind") and seen["version"] == state["change"]["version"])
        if waited and behind and given["conflicts"] != "person":
            if "six hours" in attention.get("text", ""):
                flag("required-checks", "a branch behind its base ended with the six hours", info)
            if calm and not state["attention"] and new["state"] == "accepting":
                if not any(a["kind"] == "accept" for a in actions):
                    flag("required-checks", "a branch behind past six hours tries no accept", info)
        elif waited and (new["state"] != "ready" or attention.get("reason") != "acceptRefused"):
            flag("required-checks", "a tick six hours after the start needs no person", info)
        elif not waited and new["state"] == "ready" and "six hours" in attention.get("text", ""):
            flag("required-checks", "the six hours ended early", info)
        elif not waited and calm and not state["attention"] and len(state["waits"]) == 1:
            if not any(a["kind"] == "accept" for a in actions) and new["state"] == "accepting":
                flag("required-checks", "a tick within six hours tries no accept again", info)

    def approvals(self, state, event, answer, given, flag, info):
        """The invariant of an approval: `changeApproved` never issues an `accept` under
        `person`, and changes nothing of a change that waits for its accept."""
        if state is None or event["kind"] != "changeApproved" or given["accept"] != "person":
            return
        if any(action["kind"] == "accept" for action in answer["actions"]):
            flag("approvals", "an approval under `person` starts an accept", info)
        waits = (state["attention"] or {}).get("reason") in ("accept", "acceptRefused", "reopened")
        # A conflict, a merge or an emptied change that the host observed is the conflict
        # policy's to act on, whatever event comes.
        seen = state["observed"] or {}
        calm = not (seen.get("conflicts") or seen.get("merged") or seen.get("empty"))
        if waits and calm and state["state"] == "ready" and answer["changed"]:
            flag("approvals", "an approval under `person` changes a change that waits", info)

    def refusals(self, state, event, answer, done, flag, info):
        """The invariant of the refusals that wait for a person: with a calm observation, an
        accept refused as `transient`, `headMoved` or `needsPerson` leaves the item in `ready`
        with `acceptRefused`, and `notAccepted` in `failed`. An accept refused as `untrusted`
        leaves it in `ready` with `untrusted`, whatever the observation says."""
        if state is None or not answer["changed"] or state["state"] != "accepting":
            return
        if done is None or done["kind"] != "accept":
            return
        refusal = (event.get("result") or {}).get("refusal")
        if refusal == "untrusted":
            new = answer["state"]
            if new["state"] != "ready" or (new["attention"] or {}).get("reason") != "untrusted":
                flag("refusals", "an untrusted refusal does not wait for a person", info)
            return
        seen = state["observed"]
        calm = bool(seen) and not (seen["conflicts"] or seen["merged"] or seen["empty"])
        calm = calm and seen["version"] == state["change"]["version"] and not state["hold"]
        if not calm:
            return
        new = answer["state"]
        attention = new["attention"] or {}
        if refusal in ("transient", "headMoved", "needsPerson"):
            if new["state"] != "ready" or attention.get("reason") != "acceptRefused":
                flag("refusals", f"a {refusal} refusal does not wait for a person", info)
        elif refusal == "notAccepted" and new["state"] != "failed":
            flag("refusals", "an accept that was not accepted does not fail the item", info)

    # -- a walk ---------------------------------------------------------------------------------

    def sweep_check(self, w, seed):
        """Send the sweep call a random request, once for the walk, on a generator of its own so
        that the walk's events stay as they were, and check the answer against the sweep's
        rules."""
        rng = random.Random(f"sweep-{seed}-{w}")
        words = ["urgent", "later", "docs", "x"]
        items = [
            {
                "item": str(number),
                "tags": rng.sample(words, rng.randrange(3)),
                "group": rng.choice([None, 0, 1, 2]),
                "wave": rng.randrange(3),
                "awaited": rng.random() < 0.2,
                "account": rng.choice([None, None, "a", "b"]),
            }
            for number in range(1, rng.randrange(9) + 1)
        ]
        request = {
            "protocol": 1,
            "call": "sweep",
            "policies": {
                "maxParallel": rng.choice([None, 1, 2, 3, 4]),
                "priority": rng.sample(words, rng.randrange(4)),
            },
            "running": rng.randrange(5),
            "limited": rng.sample(["a", "b"], rng.randrange(3)),
            "items": items,
        }
        info = {"walk": w, "step": 0, "trail": [("sweep", len(items), "")], "event": request}
        answer = self.engine.answer(json.loads(json.dumps(request)))
        self.events += 1
        flag = functools.partial(self.flag, "sweep")
        found = self.protocol.check(answer, "calls/sweep.answer.schema.json", "answer")
        if found:
            flag(f"the answer does not fit its schema: {found[0]}", info)
            return
        by_item = {item["item"]: item for item in items}
        starts, waits = answer["starts"], answer["waits"]
        named = sorted(entry["item"] for entry in starts + waits)
        if named != sorted(by_item):
            flag("an item is not answered exactly once", info)
            return
        limit = request["policies"]["maxParallel"]
        running = request["running"]
        within = [entry for entry in starts if not entry["beyondLimit"]]
        if limit is not None and running + len(within) > limit and within:
            flag("starts that are not beyond the limit pass `maxParallel`", info)
        for entry in starts:
            item = by_item[entry["item"]]
            if entry["beyondLimit"] and not item["awaited"]:
                flag("an item that is not awaited starts beyond the limit", info)
            if item["account"] in request["limited"] and item["account"] is not None:
                flag("an item on a limited account starts", info)
        for entry in waits:
            item = by_item[entry["item"]]
            limited = item["account"] is not None and item["account"] in request["limited"]
            if entry["for"] == "place" and (item["awaited"] or limited):
                flag("an awaited item, or one on a limited account, waits for a place", info)
            if entry["for"] == "usageLimit" and not limited:
                flag("an item waits for a usage limit that is not its account's", info)
        priority = request["policies"]["priority"]
        position = {item["item"]: n for n, item in enumerate(items)}

        def key(name):
            item = by_item[name]
            tags = set(item["tags"])
            rank = next((n for n, tag in enumerate(priority) if tag in tags), len(priority))
            group = item["group"]
            wave = item["wave"] if group is not None else 0
            return (rank, group is None, group or 0, wave, position[name])

        everyone = sorted(by_item, key=key)
        order = [entry["item"] for entry in starts]
        if order != [name for name in everyone if name in set(order)]:
            flag("starts are not in the order of priority, group, wave and place", info)
        # Each wait counts the items running at its turn: those that ran and the starts before it.
        before = {}
        count = running
        for name in everyone:
            before[name] = count
            if name in set(order):
                count += 1
        for entry in waits:
            if entry["running"] != before[entry["item"]]:
                flag("a wait counts other items than those running before it", info)
        # An awaited item on an account that is not at a usage limit always starts.
        free = [
            name
            for name, item in by_item.items()
            if item["awaited"]
            and (item["account"] is None or item["account"] not in request["limited"])
        ]
        if any(name not in set(order) for name in free):
            flag("an awaited item on a free account does not start", info)

    def walk(self, w, seed, steps, show=False):
        """Walk `w`, and for `APPLICATION` of the walks one more with an application's policies.
        That one comes from generators of its own, so every walk plays as it did without it."""
        self.sweep_check(w, seed)
        self.play(w, random.Random(seed * 100003 + w), steps, show)
        own = random.Random(f"application-{seed}-{w}")
        if own.random() < APPLICATION:
            self.applied += 1
            self.play(w, own, steps, show, application=True)

    def play(self, w, rng, steps, show, application=False):
        pol = self.application_policies(rng) if application else self.policies(rng)
        state = None
        trail = []
        memory = {"open": {}, "floor": None}
        vouched = False
        if rng.random() < SPLIT:
            path = self.tc.EXAMPLES / "state" / "split.json"
            state = json.loads(path.read_text())
            memory["open"] = {action["id"]: action for action in state["pending"]}
        elif rng.random() < NEW:
            path = self.tc.EXAMPLES / "state" / "new.json"
            state = json.loads(path.read_text())
            state["waits"] = [{"id": "w1.1", "for": "start", "until": None, "on": None}]
            if rng.random() < 0.5:
                state["waits"].append({"id": "w1.2", "for": "blocker", "until": None, "on": "7"})
            state["hold"] = rng.random() < 0.3
            memory["open"] = {action["id"]: action for action in state["pending"]}
        elif rng.random() < VOUCH:
            # An untrusted edit stopped a round: a failed item with no change, or with one, or
            # a merge, in `ready`. The engine never marks one by itself.
            name = rng.choice(["failed", "failed", "ready"])
            state = json.loads((self.tc.EXAMPLES / "state" / f"{name}.json").read_text())
            if name == "failed" and rng.random() < 0.5:
                state["change"], state["observed"] = None, None
            text = "An edit by someone without write access stopped it."
            state["attention"] = {"reason": "untrusted", "text": text, "ref": None}
            state["hold"] = rng.random() < 0.15
            if state["change"] and rng.random() < 0.4:
                state["rounds"]["counted"] = rng.choice([1, 2, 3])
            memory["open"] = {action["id"]: action for action in state["pending"]}
            vouched = rng.random() < 0.8
        elif rng.random() < ACCEPTING:
            # Some walks start at a merge that waits for required checks, which a walk from a
            # new item seldom reaches: its accept, refusals and ticks come at once.
            path = self.tc.EXAMPLES / "state" / "accepting.json"
            state = json.loads(path.read_text())
            if rng.random() < HELD:
                # An import leaves a round that a hold or a blocker kept back waiting to
                # start, on a change that may conflict.
                state["waits"] = [{"id": "w1.1", "for": "start", "until": None, "on": None}]
                state["pending"] = []
                if rng.random() < 0.7:
                    blocker = {"id": "w1.2", "for": "blocker", "until": None, "on": "7"}
                    state["waits"].append(blocker)
                state["observed"] = {**state["observed"], "conflicts": rng.random() < 0.7}
            memory["open"] = {action["id"]: action for action in state["pending"]}
        self.last_checks = None
        for number in range(1, steps + 1):
            short = self.short(rng, state)
            if vouched and number == 1:
                # The first thing that happens to an item that an untrusted edit stopped is a
                # vouch, most of the time.
                role = rng.choice(["writer", "decides", "reader", "outside"])
                short = {"kind": "requested", "what": "trust", "by": {"id": "u", "role": role}}
            if short is None:
                continue
            try:
                event = self.tc.event_of(short, state, number)
            except Exception:  # noqa: BLE001
                continue
            if rng.random() < 0.05 and not application:
                pol = self.policies(rng)
            request = {"protocol": 1, "call": "step", "state": state, "event": event}
            request["policies"] = pol
            answer = self.engine.answer(json.loads(json.dumps(request)))
            self.events += 1
            where = (answer.get("state") or {}).get("state") or answer.get("error", "")[:80]
            trail.append((short["kind"], short.get("what") or short.get("action"), where))
            if show:
                print(json.dumps({"event": event, "policies": pol, "answer": answer}, default=str))
            info = {"walk": w, "step": number, "trail": trail[-8:], "event": event}
            self.check(state, event, short, pol, answer, info, memory)
            if "state" in answer:
                state = answer["state"]


def _found(walker):
    refused = sum(walker.errors.values())
    found = (walker.problems, walker.examples, walker.known, walker.events, walker.pairs)
    return (*found, walker.tc.table_transitions(), refused, walker.applied)


def _walks(job):
    tree, walks, seed, steps, show = job
    # The tree to walk comes first, and the walk reads no file by a relative path.
    sys.path.insert(0, str(tree))
    os.chdir(tree)
    with Walker() as walker:
        for w in walks:
            walker.walk(w, seed, steps, show=show)
        return _found(walker)


def sample(walks, seed=349, steps=40):
    """`walks` walks of the engine that `sys.path` finds, in this process, with no change to the
    working folder or `sys.path`: what a test plays. Returns the problems with an example of each,
    the known ones, and the counts of events, refused events and walks with an application's
    policies."""
    with Walker() as walker:
        for w in range(walks):
            walker.walk(w, seed, steps)
    return {
        "problems": walker.problems,
        "examples": walker.examples,
        "known": walker.known,
        "events": walker.events,
        "refused": sum(walker.errors.values()),
        "applied": walker.applied,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--walks", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=349)
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--tree", default=str(ROOT), help="the tree to walk")
    parser.add_argument("--walk", type=int, help="play only this walk")
    parser.add_argument("--show", action="store_true", help="print each step of the walk")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    tree = Path(args.tree).resolve()
    walks = [args.walk] if args.walk is not None else list(range(args.walks))
    jobs = max(min(args.jobs, len(walks)), 1)
    chunks = [(tree, walks[n::jobs], args.seed, args.steps, args.show) for n in range(jobs)]
    if jobs == 1:
        found = [_walks(chunks[0])]
    else:
        with multiprocessing.Pool(jobs) as pool:
            found = pool.map(_walks, chunks)
    problems, examples, known, events = collections.Counter(), {}, collections.Counter(), 0
    pairs, table, refused, applied = collections.Counter(), set(), 0, 0
    for (
        their_problems,
        their_examples,
        their_known,
        their_events,
        their_pairs,
        their_table,
        their_refused,
        their_applied,
    ) in found:
        table |= their_table
        refused += their_refused
        applied += their_applied
        problems.update(their_problems)
        pairs.update(their_pairs)
        known.update(their_known)
        events += their_events
        for name, info in their_examples.items():
            if name not in examples or info["walk"] < examples[name]["walk"]:
                examples[name] = info
    print(f"{len(walks)} walks, seed {args.seed}, {events} events")
    print(f"  {applied} walks took an application's policies")
    played = len(table & set(pairs))
    print(f"  {played} of the {len(table)} transitions the states table names played")
    # A walk whose events the engine mostly refuses checks nothing: that must stay rare.
    share = refused / max(events, 1)
    print(f"  {refused} events refused ({share:.1%}); more than {REFUSED:.0%} fails the walks")
    for name, n in sorted(known.items()):
        print(f"  listed: {n} × {name}")
    for name, n in problems.most_common():
        info = examples[name]
        print(f"  {n} × {name}")
        print(f"    e.g. walk {info['walk']} step {info['step']}: {info['trail']}")
    print(f"{sum(problems.values())} broken invariants not listed")
    return 1 if problems or share > REFUSED else 0


if __name__ == "__main__":
    sys.exit(main())
