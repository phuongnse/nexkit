"""Random walks of the engine against its invariants.

usage: python3 tests/differential/walk.py [--walks N] [--seed S] [--steps M] [--tree PATH]
                                          [--walk W] [--show]

Each walk starts an item with random policies, which change now and then, and sends up to
`--steps` events: every kind the conformance cases send, results of each action it has
pending (plans and rounds with questions, checks of one to four named checks failing with
one of two signatures, some skipped, with and without a failure the base branch has, reviews,
publishes that conflict or leave the version as it was, refused accepts, usage limits with and
without a reset), holds and their end, pushes, conflicts and lost actions. After every step it
checks the invariants below; `INVARIANTS` names them by the rule each guards. Walk W plays the
same events whatever else runs (`--walk W --show` prints its trail). It prints how often each
invariant broke, with an example, and exits 1 when one broke that `KNOWN` does not list.

A change that moves a decision into the engine adds invariants for its rules here.
"""

import argparse
import collections
import copy
import functools
import json
import multiprocessing
import os
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent

INVARIANTS = {
    "schema": "every answer fits the protocol's schema, and the engine never fails on one",
    "transition": "only the transitions that the states table and its rules name",
    "outbox": "each action answered is pending, whole, and leaves only with its result, its "
    "cancel or its loss; a close is pending only on a final item; a notice stands",
    "carried": "every open item has work, a wait, a hold or attention, so none is stuck",
    "counts": "a fix round is counted when its run is issued, one at a time, within `fixRounds`",
    "give-back": "only an automatic fix round stopped before its review ends gives its count "
    "back, and the count of one whose change conflicted stands, or that a person's push "
    "found published",
    "due": "a round that waits to be issued (`due`) is in `fixing` with no run, and waits",
    "automatic": "`automatic` marks only a counted fix round that its review has not judged",
    "diagnosis": "only an automatic fix round diagnoses, exactly the checks that failed the "
    "same way before and after the fix round before it",
    "usage-limit": "a usage limit waits for its reset, or for a person, counting nothing, and "
    "the same run starts again at its tick",
    "hold": "no conflict round starts and no change is made again while a hold is on",
    "answers": "the engine answers a round's questions at most once in a row, and only under "
    "`autoWhenClean` with `answers: recommended`",
    "questions": "a round's questions are kept only while they wait",
    "split": "an item with a change is never split",
    "required-checks": "a start of the wait for required checks lives only in `accepting`, on "
    "the change's version; a refusal for them that have not passed leaves one wait, a tick and "
    "the first start of that version; a tick more than six hours after it needs a person",
}

# Broken invariants that a known issue covers: (invariant, a text the problem starts with).
KNOWN = {
    ("transition", "('dropped', 'accepted')"): "#348",
    ("transition", "('dropped', 'noChange')"): "#348",
}

TITLE = "Cache the results"
# The share of walks that start from the example of an item that accepts.
ACCEPTING = 0.2
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
    def __init__(self, tree):
        sys.path.insert(0, str(tree))
        os.chdir(tree)
        from nexkit import engine, protocol
        from nexkit.engine.machine import DEFAULTS
        from nexkit.protocol.size import failure_key
        from tests import test_conformance as tc

        # Both read files that do not change during a walk: read each once. The schemas the
        # engine checks every state against stay the same objects.
        protocol._refs = functools.cache(protocol._refs)
        tc.table_transitions = functools.cache(tc.table_transitions)
        self.engine, self.protocol, self.tc = engine, protocol, tc
        self.defaults, self.failure_key = dict(DEFAULTS), failure_key
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
        self.last_checks = None

    # -- what a walk sends ------------------------------------------------------------------

    def policies(self, rng):
        found = {}
        for name, values in {
            "planApproval": ["person", "autoWhenClean"],
            "answers": ["never", "recommended", "recommended"],
            "splits": [True, False],
            "splitParts": [1, 2, 10],
            "splitDepth": [1, 2, 3],
            "accept": ["person", "autoWhenPassed"],
            "conflicts": ["person", "resolve", "resolve", "redo"],
            "conflictRounds": [1, 1, 2, 3],
            "fixRounds": [0, 1, 2, 3],
            "whenRoundsRunOut": ["person", "failed"],
            "reviewAfterFailingChecks": ["always", "always", "onlyAfterPassing"],
        }.items():
            if rng.random() < 0.85:
                found[name] = rng.choice(values)
        return found

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
        found["protected_paths"] = rng.choice([[], [], ["a"]])
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
        found["protected_paths"] = rng.choice([[], [], ["a.yml"]])
        if stage == "implement":
            found["no_change"] = rng.choice(["", "", "Done already."])
        elif rng.random() < 0.2:
            found["open_conflicts"] = [
                {
                    "file": "a.py",
                    "base_change": "x",
                    "pr_change": "y",
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
                pool += ["@timeout", "@error"]
            return tc.result_of(rng.choice(pool or ["@error"]))
        if kind == "runChecks":
            if rng.random() < 0.6:
                return self.checks(rng)
        if kind == "accept" and rng.random() < 0.6:
            refusal = rng.choice(
                ["checkFailed", "checkFailed", "conflict", "behind", "notAccepted"]
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
            return event
        kind = rng.choice(
            ["requested", "requested", "planApproved", "answered", "commented"]
            + ["actionFinished"] * 3
            + ["actionLost", "dropRequested", "changeObserved", "tick", "held", "released"]
            + ["reopened", "changesRequested", "changeApproved", "blockerClosed", "partEnded"]
            + ["stopped", "textChanged", "push", "conflict"]
        )
        busy = [a for a in state["pending"] if a["kind"] not in ("needPerson", "show", "close")]
        busy = [a for a in busy if a["kind"] != "cancel"]
        if busy and rng.random() < 0.45:
            kind = "actionFinished"
        if state["state"] in ("new", "awaitingApproval") and rng.random() < 0.3:
            kind = "requested"
        if state["state"] == "fixing" and not state["hold"] and rng.random() < 0.15:
            kind = "held"
        if state["rounds"].get("due") and rng.random() < 0.3:
            kind = rng.choice(["released", "blockerClosed", "requested", "push", "held"])
        checks = [wait for wait in state["waits"] if wait["for"] == "requiredChecks"]
        if checks and rng.random() < 0.6:
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
                found = {"kind": "actionFinished", "action": action["id"], "result": result}
                if action["kind"] == "accept":
                    # Refusals come at different times, so that the first one's is told apart.
                    late = rng.choice([0, 3600, 2 * 3600, 5 * 3600])
                    found["at"] = _later(late)
                return found
        if kind == "requested":
            whats = ["plan", "planFromScratch", "go", "fix", "review", "retry", "start"]
            if state["state"] in ("new", "awaitingApproval"):
                whats += ["go", "go", "start"]
            event = {"kind": "requested", "what": rng.choice(whats)}
            event["by"] = rng.choice([tc.PERSON, None])
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
            return {"kind": "actionLost", "action": rng.choice(state["pending"])["id"]}
        if kind == "tick":
            timed = [w for w in state["waits"] if w["until"] is not None]
            return self.tick(rng, state, rng.choice(timed)) if timed else None
        if kind == "push":
            if not state["change"]:
                return None
            version = f"v{rng.randint(100, 999)}"
            return {"kind": "changeObserved", "version": version, "conflicts": rng.random() < 0.2}
        if kind == "conflict":
            if not state["change"]:
                return None
            return {"kind": "changeObserved", "conflicts": rng.random() < 0.7}
        pool = self.shorts.get(kind)
        return copy.deepcopy(rng.choice(pool)) if pool else None

    # -- the invariants ---------------------------------------------------------------------

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
        if new["state"] == "accepting" and not any(a["kind"] == "accept" for a in new["pending"]):
            if not (kept or new["waits"]):
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
        if new["state"] == "split" and state is not None and state["change"]:
            flag("split", "an item with a change was split", info)
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
            if event["kind"] in ("requested", "changesRequested", "answered", "commented"):
                if not old.get("due"):
                    flag("counts", "a person's round counted", info)
        if state["state"] == "fixing" and old.get("due") and event["kind"] == "requested":
            if short.get("what") in ("fix", "review") and answer["changed"]:
                if rounds["counted"] != old["counted"]:
                    flag("counts", "a person's request during a wait counted", info)
        if rounds["conflicts"] > old["conflicts"]:
            mine = {**self.defaults, **(state["policies"] or {})}["conflictRounds"]
            if rounds["conflicts"] > max({**self.defaults, **pol}["conflictRounds"], mine):
                flag("counts", "conflict rounds past conflictRounds", info)
        # hold
        if state["hold"] and new["hold"] and rounds["counted"] > old["counted"]:
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
                    flag("diagnosis", "a person's or an answered round diagnoses", info)
        if rounds["counted"] > old["counted"] and rounds.get("automatic"):
            for action in fixes:
                if action.get("diagnose") != (rounds.get("repeated") or [])[:10]:
                    flag("diagnosis", "an automatic fix round diagnoses other than repeated", info)
        done = None
        if finished and answer["changed"]:
            done = next((a for a in state["pending"] if a["id"] == event["action"]), None)
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
        # usage-limit
        if done and done["kind"] == "runAgent" and (event["result"] or {}).get("limit"):
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
            if new["state"] == name and not new["hold"] and not new["attention"]:
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
        self.required_checks(state, event, answer, done, flag, info)
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

    def required_checks(self, state, event, answer, done, flag, info):
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
        if waited and (new["state"] != "ready" or attention.get("reason") != "acceptRefused"):
            flag("required-checks", "a tick six hours after the start needs no person", info)
        elif not waited and new["state"] == "ready" and "six hours" in attention.get("text", ""):
            flag("required-checks", "the six hours ended early", info)
        elif not waited and calm and not state["attention"] and len(state["waits"]) == 1:
            if not any(a["kind"] == "accept" for a in actions) and new["state"] == "accepting":
                flag("required-checks", "a tick within six hours tries no accept again", info)

    # -- a walk ---------------------------------------------------------------------------------

    def walk(self, w, seed, steps, show=False):
        rng = random.Random(seed * 100003 + w)
        pol = self.policies(rng)
        state = None
        trail = []
        memory = {"open": {}, "floor": None}
        if rng.random() < ACCEPTING:
            # Some walks start at a merge that waits for required checks, which a walk from a
            # new item seldom reaches: its accept, refusals and ticks come at once.
            path = self.tc.EXAMPLES / "state" / "accepting.json"
            state = json.loads(path.read_text())
            memory["open"] = {action["id"]: action for action in state["pending"]}
        self.last_checks = None
        for number in range(1, steps + 1):
            short = self.short(rng, state)
            if short is None:
                continue
            try:
                event = self.tc.event_of(short, state, number)
            except Exception:  # noqa: BLE001
                continue
            if rng.random() < 0.05:
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


def _walks(job):
    tree, walks, seed, steps, show = job
    walker = Walker(tree)
    for w in walks:
        walker.walk(w, seed, steps, show=show)
    table = walker.tc.table_transitions()
    refused = sum(walker.errors.values())
    found = (walker.problems, walker.examples, walker.known, walker.events, walker.pairs, table)
    return (*found, refused)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--walks", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=349)
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--tree", default=str(HERE.parent.parent), help="the tree to walk")
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
    pairs, table, refused = collections.Counter(), set(), 0
    for (
        their_problems,
        their_examples,
        their_known,
        their_events,
        their_pairs,
        their_table,
        their_refused,
    ) in found:
        table |= their_table
        refused += their_refused
        problems.update(their_problems)
        pairs.update(their_pairs)
        known.update(their_known)
        events += their_events
        for name, info in their_examples.items():
            if name not in examples or info["walk"] < examples[name]["walk"]:
                examples[name] = info
    print(f"{len(walks)} walks, seed {args.seed}, {events} events")
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
