"""Random walks of the engine at NEXKIT_TREE with the reviewer's own invariants.

Usage: NEXKIT_TREE=... python3 walk.py SEED WALKS [STEPS]
Prints one JSON line per violation kind with a count and the shortest example.
"""

import copy
import json
import os
import random
import sys

TREE = os.environ.get("NEXKIT_TREE", os.path.expanduser("~/repos/nexkit-r344"))
sys.path.insert(0, TREE)
os.chdir(TREE)

from nexkit import engine, protocol  # noqa: E402
from tests.test_conformance import FINAL, WORK, allowed, event_of  # noqa: E402

COUNTING = ("fixing", "checking", "reviewing")
FIXT = {
    "plan": ["plan", "plan", "planQuestions", "planRecommended", "planNoChange", "planProtected"],
    "implement": ["implement", "implement", "implement", "implementQuestions", "implementBlocked", "implementRecommended", "@timeout", "@error"],
    "fix": ["fix", "fix", "fix", "fix", "fixQuestions", "fixRecommended", "@timeout", "@error"],
    "review": ["review", "review", "reviewChanges", "reviewChanges", "reviewChanges", "reviewUnverifiable", "reviewApproveBlocking", "@error"],
}


def pol(rng):
    return {
        "fixRounds": rng.choice([0, 1, 2, 3]),
        "reviewAfterFailingChecks": rng.choice(["always", "onlyAfterPassing"]),
        "whenRoundsRunOut": rng.choice(["person", "failed"]),
        "accept": rng.choice(["person", "autoWhenPassed"]),
        "conflicts": rng.choice(["person", "resolve", "redo"]),
        "conflictRounds": rng.choice([1, 2]),
        "planApproval": rng.choice(["person", "autoWhenClean"]),
        "answers": rng.choice(["recommended", "person"]) if False else "recommended",
    }


def checks(rng):
    if rng.random() < 0.35:
        return {"checks": [{"name": "test", "passed": True, "skipped": False, "signature": None, "knownBaseFailure": False}]}
    base = rng.random() < 0.5
    result = {"checks": [{"name": "test", "passed": False, "skipped": False, "signature": rng.choice(["fail a", "fail b"]), "knownBaseFailure": base}]}
    if base:
        result["baseFailure"] = rng.choice(["9", "9", "11"])
    return result


def accept_result(rng):
    kind = rng.choice(["ok", "checkFailed", "checkFailed", "checkFailedBase", "conflict", "transient", "notAccepted", "behind"])
    if kind == "ok":
        return {"accepted": True, "refusal": None, "text": None}
    if kind == "checkFailedBase":
        return {"accepted": False, "refusal": "checkFailed", "text": "x", "baseFailure": "9"}
    return {"accepted": False, "refusal": kind, "text": "x"}


def finish(rng, state, action, versions):
    kind = action["kind"]
    if kind == "runAgent":
        return {"kind": "actionFinished", "action": action["id"], "result": "@agent:" + rng.choice(FIXT[action["stage"]]) if True else None}
    if kind == "publish":
        cur = (state["change"] or {}).get("version")
        if cur and rng.random() < 0.3:
            v = cur
        else:
            versions[0] += 1
            v = f"v{versions[0]}"
        return {"kind": "actionFinished", "action": action["id"], "result": {"change": {"id": "308", "version": v}, "refused": None}}
    if kind == "runChecks":
        return {"kind": "actionFinished", "action": action["id"], "result": checks(rng)}
    if kind == "accept":
        return {"kind": "actionFinished", "action": action["id"], "result": accept_result(rng)}
    if kind == "replaceChange":
        versions[0] += 1
        return {"kind": "actionFinished", "action": action["id"], "result": {"change": {"id": "309", "version": f"v{versions[0]}"}, "refused": None}}
    if kind == "createParts":
        return {"kind": "actionFinished", "action": action["id"], "result": {"parts": [{"key": 1, "item": "43"}]}}
    return {"kind": "actionFinished", "action": action["id"], "result": {}}


def pick(rng, state, versions):
    pending = state["pending"] if state else []
    menu = []
    work = [a for a in pending if a["kind"] in ("runAgent", "publish", "runChecks", "accept", "replaceChange", "createParts")]
    other = [a for a in pending if a not in work]
    for a in work:
        menu += [lambda a=a: finish(rng, state, a, versions)] * 6
    for a in other:
        menu += [lambda a=a: finish(rng, state, a, versions)]
    for a in work:
        menu += [lambda a=a: {"kind": "actionLost", "action": a["id"]}] * 0  # losses are rare
    if pending and rng.random() < 0.03:
        a = rng.choice(pending)
        menu += [lambda a=a: {"kind": "actionLost", "action": a["id"]}]
    menu += [lambda: {"kind": "held"}] * 2 + [lambda: {"kind": "released"}] * 3
    menu += [lambda: {"kind": "blockerClosed", "blocker": rng.choice(["9", "9", "11"]), "completed": True}] * 3
    for what in ("fix", "review", "fix", "review", "go", "retry", "start", "plan"):
        menu += [lambda what=what: {"kind": "requested", "what": what}]
    if state and state["change"]:
        def push():
            versions[0] += 1
            return {"kind": "changeObserved", "version": f"v{versions[0]}"}
        menu += [push] * 2
        menu += [lambda: {"kind": "changeObserved", "conflicts": True, "version": state["change"]["version"]}]
        menu += [lambda: {"kind": "changesRequested"}, lambda: {"kind": "changeApproved"}]
        if rng.random() < 0.05:
            menu += [lambda: {"kind": "changeObserved", "merged": True, "version": state["change"]["version"]}]
    menu += [lambda: {"kind": "planApproved"}, lambda: {"kind": "answered"}, lambda: {"kind": "commented"}]
    menu += [lambda: {"kind": "stopped", "outcome": "other", "text": "Stopped by the host."}]
    if rng.random() < 0.05:
        menu += [lambda: {"kind": "dropRequested"}]
    if rng.random() < 0.05:
        menu += [lambda: {"kind": "reopened"}]
    return rng.choice(menu)()


def walk(seed, steps):
    rng = random.Random(seed)
    policies = pol(rng)
    state = None
    versions = [0]
    events = [{"kind": "created"}, {"kind": "requested", "what": "go"}]
    found = []
    # The reviewer's tracking of counted rounds.
    judged = False  # the latest counted round's checks/review ended
    person_since = False  # a person's round was taken since the latest count
    gave_back_since = False
    trail = []
    for n in range(steps):
        if n < len(events):
            short = events[n]
        else:
            if rng.random() < 0.06:
                policies = pol(rng)
            short = pick(rng, state, versions)
        try:
            event = event_of(copy.deepcopy(short), state, n + 1)
        except Exception:
            continue
        before = state
        answer = engine.answer({"protocol": 1, "call": "step", "state": state, "event": event, "policies": policies})
        trail.append({"event": short, "policies": policies})
        if "error" in answer:
            if "failed on this request" in answer["error"] or "schema" in answer["error"]:
                found.append(("engine error", answer["error"][:200]))
                return found, trail
            continue
        state = answer["state"]
        if not answer["changed"]:
            continue
        acts = answer["actions"]
        r0 = (before or {"rounds": {"counted": 0}})["rounds"]
        r1 = state["rounds"]
        fix_issued = any(a["kind"] == "runAgent" and a["stage"] == "fix" for a in acts)
        name = state["state"]
        # Schemas.
        for a in acts:
            if protocol.check(a, "action.schema.json"):
                found.append(("action schema", a["kind"]))
        # Count rises only by one, with a fix run issued, within fixRounds in force.
        rose = r1["counted"] > r0["counted"]
        if rose:
            stored = {**{"fixRounds": 2}, **policies, **state["policies"]}
            if r1["counted"] != r0["counted"] + 1:
                found.append(("count rose by more than one", None))
            if not fix_issued:
                found.append(("count rose without a fix run", short["kind"]))
            if r1["counted"] > stored["fixRounds"]:
                found.append(("count past fixRounds", (r1["counted"], stored["fixRounds"])))
            if not r1.get("automatic"):
                found.append(("count rose without automatic", None))
            judged, person_since, gave_back_since = False, False, False
        elif r1["counted"] < r0["counted"] and not (before and before["state"] in ("accepted", "noChange") and short["kind"] == "reopened"):
            if r1["counted"] != r0["counted"] - 1:
                found.append(("count fell by more than one", None))
            if not r0.get("automatic"):
                found.append(("count fell for a round with no count", None))
            if gave_back_since:
                found.append(("gave back twice", None))
            if judged:
                found.append(("gave back a judged round", (before["state"], short["kind"], policies["reviewAfterFailingChecks"])))
            if person_since:
                found.append(("gave back after a person's round", (before["state"], short["kind"], policies["reviewAfterFailingChecks"])))
            gave_back_since = True
        # A round is judged once its review ends, or its failing checks under onlyAfterPassing
        # lead on without a review.
        if short["kind"] == "actionFinished":
            act = next((a for a in (before or {}).get("pending", []) if a["id"] == event["action"]), None)
            if act and act["kind"] == "runAgent" and act["stage"] == "review" and not rose:
                judged = True
            if act and act["kind"] == "runChecks" and name != "reviewing" and not rose:
                judged = True
        if short["kind"] in ("requested", "changesRequested") and not fix_issued or (short["kind"] == "requested" and short.get("what") in ("fix", "review", "retry")):
            if r1["counted"] == r0["counted"]:
                person_since = True
        # due and automatic.
        if r1.get("due"):
            if name != "fixing":
                found.append(("due outside fixing", name))
            if any(a["kind"] in ("runAgent", "publish") for a in state["pending"]):
                found.append(("due with a run pending", name))
            if not (state["hold"] or state["waits"] or state["attention"]):
                found.append(("due with nothing to wait for", [w["for"] for w in state["waits"]]))
        if r1.get("automatic"):
            if name not in COUNTING:
                found.append(("automatic outside a round", name))
            if r1["counted"] < 1:
                found.append(("automatic without a count", None))
        # Nothing stuck.
        if name not in FINAL:
            carried = [a for a in state["pending"] if a["kind"] in WORK] or state["waits"] or state["hold"] or state["attention"]
            if not carried:
                found.append(("stuck", (name, short["kind"])))
            if name in ("fixing", "accepting") and not ([a for a in state["pending"] if a["kind"] in WORK] or state["waits"] or state["hold"] or state["attention"]):
                found.append(("stuck in " + name, short["kind"]))
        # Transitions.
        if before is not None and before["state"] != name:
            pair = (before["state"], name)
            kept = before["policies"] if before["state"] in ("working", "fixing", "checking", "reviewing") else {}
            if pair not in allowed({**policies, **kept}, bool(before["change"] or state["change"])) and pair not in allowed(policies, bool(before["change"] or state["change"])):
                found.append(("transition not in the table", pair))
        if found:
            return found, trail
    return found, trail


def main():
    seed, walks = int(sys.argv[1]), int(sys.argv[2])
    steps = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    kinds = {}
    for w in range(walks):
        found, trail = walk(seed * 1_000_003 + w, steps)
        for kind, detail in found:
            key = kind
            have = kinds.get(key)
            if have is None or len(trail) < len(have["trail"]):
                kinds[key] = {"count": (have or {"count": 0})["count"], "detail": detail, "trail": trail, "seed": seed * 1_000_003 + w}
            kinds[key]["count"] += 1
    for key, value in kinds.items():
        print(json.dumps({"kind": key, **value}, default=str))
    print(json.dumps({"walks": walks, "seed": seed}))


if __name__ == "__main__":
    main()
