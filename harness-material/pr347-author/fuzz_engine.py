"""Random walks over the engine's step with the new plan rules, checking the conformance
suite's invariants after every call.

usage: python3 fuzz_engine.py ROOT WALKS SEED
"""

import collections
import copy
import json
import random
import sys

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
import os  # noqa: E402

os.chdir(ROOT)

from nexkit import engine, protocol  # noqa: E402
from tests import test_conformance as tc  # noqa: E402

WALKS, SEED = int(sys.argv[2]), int(sys.argv[3])
rng = random.Random(SEED)

# Every short event the cases send, by kind.
SHORTS = collections.defaultdict(list)
for path in tc.cases():
    case = json.loads(path.read_text())
    for step in case["steps"]:
        ev = step["event"]
        SHORTS[ev["kind"]].append(ev)
RESULTS = collections.defaultdict(list)
for ev in SHORTS["actionFinished"]:
    RESULTS[ev["action"] if not tc.ACTION_ID.match(ev["action"]) else "?"].append(ev["result"])

TITLE = "Cache the results"


def engine_defaults():
    from nexkit.engine.machine import DEFAULTS

    return dict(DEFAULTS)
BASE = copy.deepcopy(tc.FIXTURES["plan"])


def plan_output():
    out = copy.deepcopy(BASE)
    qs = []
    for n in range(rng.choice([0, 0, 1, 2])):
        opts = ["Keep it", "Drop it", "Close it"][: rng.choice([2, 3])]
        qs.append({"question": f"Q{n}?", "options": opts,
                   "recommended": rng.choice([0, 1, 2, len(opts)]), "closes": rng.choice([0, 0, len(opts)])})
    out["questions"] = qs
    out["too_large"] = rng.random() < 0.4
    k = rng.choice([0, 2, 3, 11, 10])
    titles = [rng.choice([f"Part {i}", TITLE, TITLE + ": more", "Other"]) for i in range(1, k + 1)]
    out["split"] = [{"title": t, "body": "x", "depends_on": [i] if i else []} for i, t in enumerate(titles)]
    out["no_change"] = rng.choice(["", "", "Done already."])
    out["protected_paths"] = rng.choice([[], [], ["a"]])
    return out


def round_output(stage):
    qs = []
    for n in range(rng.choice([0, 1, 1, 2])):
        opts = ["Keep it", "Split it", "Close it"][: rng.choice([2, 3])]
        q = {"question": rng.choice([f"Q{n}?", f"Q{n}?", "  "]), "options": opts,
             "recommended": rng.choice([0, 1, 2, len(opts), 7]), "replans": rng.choice([[], [2], [1, 2], [len(opts)]])}
        if stage == "implement":
            q["closes"] = rng.choice([0, 0, len(opts)])
        qs.append(q)
    base = copy.deepcopy(tc.FIXTURES["implementQuestions" if stage == "implement" else "fixQuestions"])
    base["status"] = rng.choice(["blocked", "blocked", "done"])
    base["questions"] = qs
    base["protected_paths"] = rng.choice([[], [], ["a.yml"]])
    if stage == "implement":
        base["no_change"] = rng.choice(["", "", "Done already."])
    elif rng.random() < 0.2:
        base["open_conflicts"] = [{"file": "a.py", "base_change": "x", "pr_change": "y", "question": "Which?",
                                   "options": ["base", "pr"], "recommended": rng.choice([0, 1])}]
    return {**tc.AGENT, "output": base}


def policies():
    p = {}
    for name, values in {
        "planApproval": ["person", "autoWhenClean"],
        "answers": ["never", "recommended", "recommended"],
        "splits": [True, False],
        "splitParts": [1, 2, 10],
        "splitDepth": [1, 2, 3],
        "accept": ["person", "autoWhenPassed"],
        "conflicts": ["person", "resolve", "redo"],
        "fixRounds": [0, 1, 2, 3],
        "whenRoundsRunOut": ["person", "failed"],
        "reviewAfterFailingChecks": ["always", "always", "onlyAfterPassing"],
        "conflictRounds": [1, 3],
    }.items():
        if rng.random() < 0.85:
            p[name] = rng.choice(values)
    return p


ROLES = ["decides", "writer", "reader", "outside"]


def short_for(state):
    if state is None:
        ev = {"kind": "created", "title": rng.choice([TITLE, "", "x" * 300]), "depth": rng.choice([0, 1, 2, 3, 5])}
        if rng.random() < 0.2:
            ev["plan"] = plan_output()
        return ev
    kinds = ["requested", "requested", "planApproved", "answered", "commented", "actionFinished", "actionFinished",
             "actionFinished", "actionLost", "dropRequested", "changeObserved", "tick", "held", "released",
             "reopened", "changesRequested", "changeApproved", "blockerClosed", "partEnded", "stopped", "textChanged"]
    kind = rng.choice(kinds)
    busy = [a for a in state["pending"] if a["kind"] not in ("needPerson", "show", "close", "cancel")]
    if busy and rng.random() < 0.45:
        kind = "actionFinished"
    if state["state"] in ("new", "awaitingApproval") and rng.random() < 0.3:
        kind = "requested"
    if state["state"] == "fixing" and not state["hold"] and rng.random() < 0.15:
        kind = "held"
    if state["rounds"].get("due") and rng.random() < 0.3:
        kind = rng.choice(["released", "blockerClosed", "requested", "changeObserved", "held"])
    if kind == "actionFinished":
        pending = [a for a in state["pending"] if a["kind"] not in ("needPerson", "show", "close", "cancel")]
        if not pending:
            kind = "requested"
        else:
            action = rng.choice(pending)
            if action["kind"] == "runAgent" and action["stage"] == "plan":
                res = {**tc.AGENT, "output": plan_output()} if rng.random() < 0.9 else rng.choice(["@error"])
            elif action["kind"] == "runAgent":
                stage = action["stage"]
                pool = [r for r in RESULTS["runAgent"] if isinstance(r, str) and r.startswith("@agent:" + stage)] + ["@timeout", "@error"]
                if stage == "review":
                    pool = [r for r in RESULTS["runAgent"] if isinstance(r, str) and r.startswith("@agent:review")]
                res = rng.choice(pool)
                if stage in ("implement", "fix") and rng.random() < 0.5:
                    res = round_output(stage)
                if rng.random() < 0.12:
                    # A usage limit, with its reset or with none when the host does not resume.
                    res = {**tc.AGENT, "output": None, "limit": {"message": "limit",
                           "reset": rng.choice(["2026-11-24T04:40:00Z", None])}}
            else:
                pool = RESULTS.get(action["kind"]) or [None]
                res = copy.deepcopy(rng.choice(pool))
                if action["kind"] == "runChecks" and isinstance(res, dict) and rng.random() < 0.3:
                    res["baseFailure"] = rng.choice(["7", "8", None])
                if action["kind"] == "runChecks" and isinstance(res, dict) and rng.random() < 0.6:
                    res = copy.deepcopy(res)
                    res["checks"] = [{"name": name, "passed": rng.random() < 0.4, "skipped": rng.random() < 0.05,
                                      "signature": rng.choice(["s1", "s1", "s2", None]), "knownBaseFailure": False}
                                     for name in rng.sample(["t", "u", "v", "t"], rng.choice([1, 2, 3, 4]))]
                if action["kind"] == "accept" and rng.random() < 0.4:
                    res = {"accepted": False, "refusal": rng.choice(["checkFailed", "checkFailed", "conflict", "behind", "notAccepted"]),
                           "text": "x"}
                    if rng.random() < 0.5:
                        res["baseFailure"] = rng.choice(["7", "8", None])
                if action["kind"] == "publish" and isinstance(res, dict) and res.get("change") and rng.random() < 0.3:
                    v = (state["change"] or {}).get("version")
                    if v:
                        res["change"] = {**res["change"], "version": v}
                        if res.get("observed"):
                            res["observed"] = {**res["observed"], "version": v, "changeId": res["change"]["id"]}
                if action["kind"] == "publish" and rng.random() < 0.25:
                    cid = (state["change"] or {}).get("id") or "308"
                    v = rng.choice(["v7", "v8", "v9"])
                    res = {"change": {"id": cid, "version": v}, "refused": None,
                           "observed": {"changeId": cid, "version": v, "conflicts": True, "merged": False, "empty": False}}
                if res is None:
                    return None
            return {"kind": "actionFinished", "action": action["id"], "result": res}
    if kind == "requested":
        whats = ["plan", "planFromScratch", "go", "fix", "review", "retry", "start"]
        if state["state"] in ("new", "awaitingApproval"):
            whats += ["go", "go", "start"]
        ev = {"kind": "requested", "what": rng.choice(whats),
              "by": rng.choice([tc.PERSON, None])}
        if rng.random() < 0.3:
            ev["title"] = rng.choice([TITLE, "Other"])
        if rng.random() < 0.3:
            ev["depth"] = rng.choice([0, 3])
        return ev
    if kind == "answered":
        q = rng.choice([1, 2])
        return {"kind": "answered", "by": {"id": "u", "role": rng.choice(ROLES[:3])},
                "answers": [{"question": q, "option": rng.choice([1, 2, 3])}]}
    if kind == "blockerClosed":
        return {"kind": "blockerClosed", "blocker": rng.choice(["7", "8"]), "completed": rng.random() < 0.5}
    if kind == "commented":
        return {"kind": "commented", "by": {"id": "u", "role": rng.choice(ROLES)}}
    if kind == "actionLost":
        pending = [a for a in state["pending"]]
        if not pending:
            return None
        return {"kind": "actionLost", "action": rng.choice(pending)["id"]}
    pool = SHORTS.get(kind)
    return copy.deepcopy(rng.choice(pool)) if pool else None


problems = collections.Counter()
examples = {}
errors = collections.Counter()
pairs_seen = collections.Counter()


def flag(name, info):
    problems[name] += 1
    examples.setdefault(name, info)
    if os.environ.get("DUMP") and os.environ["DUMP"] in name and not os.path.exists(os.environ["DUMP_TO"]):
        json.dump({**info, "trail": None}, open(os.environ["DUMP_TO"], "w"), default=str)


for walk in range(WALKS):
    pol = policies()
    state = None
    trail = []
    walk_floor = None
    for number in range(1, 40):
        short = short_for(state)
        if short is None:
            continue
        try:
            event = tc.event_of(short, state, number)
        except Exception as e:  # noqa: BLE001
            continue
        if rng.random() < 0.05:
            pol = policies()
        request = {"protocol": 1, "call": "step", "state": state, "event": event, "policies": pol}
        answer = engine.answer(json.loads(json.dumps(request)))
        trail.append((short.get("kind"), short.get("what") or short.get("action"), (answer.get("state") or {}).get("state") if "state" in answer else answer.get("error", "")[:80]))
        info = {"policies": pol, "trail": trail[-8:], "event": event, "state": state}
        if "error" in answer:
            msg = answer["error"]
            if "failed on this request" in msg or "break the state" in msg or "passes the limit" in msg:
                flag("engine bug: " + msg[:120], info)
            errors[msg[:70] + " " + event["kind"]] += 1
            continue
        if protocol.check(answer, "calls/step.answer.schema.json"):
            flag("answer breaks schema", {**info, "why": protocol.check(answer, "calls/step.answer.schema.json")[:3]})
        new = answer["state"]
        # A round keeps the policies it started with: the checks read those the step used.
        eff = pol
        if state is not None and state["state"] in ("working", "fixing", "checking", "reviewing") and state["policies"]:
            eff = {**state["policies"]}
            # A fix round that waited starts with the policies given now.
            if state["rounds"].get("due") and event["kind"] in ("released", "blockerClosed", "tick", "requested"):
                eff = pol
        if new["state"] not in tc.FINAL:
            if [a for a in new["pending"] if a["kind"] == "close"]:
                flag("close pending on open item", info)
            carried = [a for a in new["pending"] if a["kind"] in tc.WORK] or new["waits"] or new["hold"] or new["attention"]
            if not carried:
                flag(f"nothing carries the item on in {new['state']}", info)
        att = new["attention"] or {}
        for a in new["pending"]:
            if a["kind"] == "needPerson" and (a["reason"], a["text"], a["ref"]) != (att.get("reason"), att.get("text"), att.get("ref")):
                flag("notice no longer stands", info)
        if state is not None and new["state"] != state["state"]:
            pair = (state["state"], new["state"])
            pairs_seen[pair] += 1
            if pair not in tc.allowed(eff, bool(state["change"] or new["change"])):
                flag(f"transition not on the page {pair}", info)
        # New rules: the engine answers at most once in a row
        if new["state"] == "awaitingApproval" and att.get("reason") in ("question", "askedAgain", "closeRecommended") and not (new["plan"] or new.get("questions")):
            flag("question attention without plan", info)
        if new["asked"].count("answers") > 1 or new["asked"].count("replan") > 1 or len(new["asked"]) > 16:
            flag("asked grows", info)
        if "replan" in new["asked"] and "answers" not in new["asked"]:
            flag("replan without answers", info)
        rq = new.get("questions")
        if rq and (new["state"] != "awaitingApproval" or att.get("ref") != rq["ref"]):
            flag("round questions kept while they do not wait", info)
        if rq is not None and not rq["items"] and att.get("reason") != "handMadeChange":
            flag("round questions empty", info)
        shown = [a for a in answer["actions"] if a["kind"] == "show" and a["what"] == "answers"]
        if shown and state is not None and "answers" in state["asked"] and event["kind"] == "actionFinished":
            flag("answered twice in a row", info)
        # A lost show is shown again under a new id, whatever the policies are now.
        shown = shown if event["kind"] != "actionLost" else []
        if shown and eff.get("planApproval") != "autoWhenClean":
            flag("answered without automatic approval", info)
        if shown and eff.get("answers") != "recommended":
            flag("answered without the answers policy", info)
        if att.get("reason") == "replanRecommended" and not new["change"]:
            flag("replanRecommended without a change", info)
        if new["state"] == "split" and state is not None and state["change"]:
            flag("an item with a change was split", info)
        fr = {**tc._defaults(), **eff}.get("fixRounds", 2) if hasattr(tc, "_defaults") else eff.get("fixRounds", 2)
        if state is not None and new["rounds"]["counted"] > max(fr, state["rounds"]["counted"]):
            flag("counted past fixRounds", info)
        if new["state"] == "fixing" and not any(a["kind"] in ("runAgent", "publish", "replaceChange") for a in new["pending"]):
            if not (new["hold"] or new["attention"] or any(w["for"] in ("baseFailure", "start", "time", "usageLimit") for w in new["waits"])):
                flag("fixing with nothing to run", info)
        if new["state"] == "accepting" and not any(a["kind"] == "accept" for a in new["pending"]):
            if not (new["hold"] or new["attention"] or new["waits"]):
                flag("accepting with no accept", info)
        # #342: rounds are counted when their run is issued, and stopped ones give it back.
        given = {**engine_defaults(), **pol}
        if state is not None:
            old_r, new_r = state["rounds"], new["rounds"]
            fixes = [a for a in answer["actions"] if a["kind"] == "runAgent" and a["stage"] == "fix"]
            cancels = [a for a in answer["actions"] if a["kind"] == "cancel"]
            if new_r["counted"] > old_r["counted"]:
                if new_r["counted"] != old_r["counted"] + 1:
                    flag("counted more than one at once", info)
                if not fixes and event["kind"] not in ("actionLost",):
                    flag("counted without issuing a fix run", info)
                if old_r.get("due") and new_r["counted"] > given["fixRounds"] and event["kind"] in ("released", "blockerClosed", "tick", "requested"):
                    flag("a due round counted past the fixRounds given now", info)
                if not new_r.get("automatic") and new["state"] in ("fixing",):
                    flag("counted round not marked automatic", info)
            if new_r["counted"] < old_r["counted"] and event["kind"] != "reopened":
                if not old_r.get("automatic") or new_r["counted"] != old_r["counted"] - 1:
                    flag("gave back without an automatic round", info)
                if not cancels and event["kind"] != "reopened":
                    # A withdraw within the step leaves no cancel: only when this step issued it.
                    if not old_r.get("automatic"):
                        flag("gave back with nothing stopped", info)
            if event["kind"] == "requested" and short.get("what") == "review" and new["state"] == "checking" and answer["changed"] and new_r.get("recheck") is not None and new_r.get("recheck") == old_r.get("recheck") and old_r.get("recheck"):
                if not any(a["kind"] == "publish" for a in answer["actions"]):
                    flag("recheck kept after a taken request", info)
            if (state["state"] == "fixing" and old_r.get("due") and event["kind"] == "requested"
                    and short.get("what") in ("fix", "review") and answer["changed"]):
                if new_r["counted"] != old_r["counted"]:
                    flag("a person's request during a wait counted", info)
        # #312: diagnosis rounds, usage limits, conflict rounds kept back by a hold.
        if state is not None:
            old_r, new_r = state["rounds"], new["rounds"]
            for a in answer["actions"]:
                if a["kind"] != "runAgent":
                    continue
                if a["stage"] != "fix":
                    if "diagnose" in a:
                        flag("diagnose on a run that is not a fix round", info)
                    continue
                if "diagnose" not in a:
                    flag("a fix run without diagnose", info)
                    continue
                if a["diagnose"]:
                    if not new_r.get("automatic"):
                        flag("a diagnosis round that is not automatic", info)
                    if a["diagnose"] != (new_r.get("repeated") or [])[:10]:
                        flag("diagnose is not the repeated failures", info)
                    if new_r["conflicts"] > old_r["conflicts"]:
                        flag("a conflict round diagnoses", info)
                    if event["kind"] in ("requested", "changesRequested", "answered", "commented"):
                        flag("a person's or an answered round diagnoses", info)
            if event["kind"] == "actionFinished" and answer["changed"]:
                act = next((a for a in state["pending"] if a["id"] == event["action"]), None)
                if act and act["kind"] == "runChecks":
                    failing = {}
                    for c in event["result"]["checks"]:
                        if not c["passed"] and not c["skipped"]:
                            failing[c["name"]] = c["signature"] or c["name"]
                    from nexkit.protocol.size import failure_key
                    before = set(old_r.get("failing", []))
                    want = [n for n, s in failing.items() if old_r.get("fixed") == state["change"]["version"] and failure_key(n, s) in before]
                    got = new_r.get("repeated") or []
                    # A base-failure wait that the same step decides forgets them.
                    if got != want and not (not got and any(w["for"] == "baseFailure" for w in new["waits"])):
                        flag("repeated is not what failed the same way after a fix round", {**info, "got": got, "want": want})
                elif act and act["kind"] == "runAgent" and (event["result"] or {}).get("limit"):
                    reset = event["result"]["limit"]["reset"]
                    if reset and not any(w["for"] == "usageLimit" and w["until"] == reset for w in new["waits"]):
                        flag("a usage limit with a reset waits for no reset", info)
                    if reset and not any(a["kind"] == "wait" for a in answer["actions"]):
                        flag("a usage limit asks for no tick", info)
                    if not reset and new["state"] != "failed":
                        flag("a usage limit with no reset is not for a person", info)
                    if new_r["started"] != old_r["started"] or new_r["counted"] != old_r["counted"]:
                        flag("a usage limit counted", info)
            if event["kind"] == "tick" and answer["changed"] and new_r["started"] != old_r["started"]:
                # The reconcile after any event may start a conflict round.
                if not old_r.get("due") and new_r["conflicts"] == old_r["conflicts"]:
                    flag("a run again after a tick counted as started", info)
            if event["kind"] == "actionFinished" and answer["changed"]:
                act = next((a for a in state["pending"] if a["id"] == event["action"]), None)
                if act and act["kind"] == "publish" and act["stage"] == "fix" and not any(a["kind"] == "runChecks" for a in new["pending"]):
                    if new_r.get("automatic"):
                        flag("an automatic round whose change conflicts keeps automatic", info)
            if walk_floor is not None and new_r["counted"] < walk_floor and event["kind"] != "reopened":
                flag("the count of an automatic round whose change conflicted went down", info)
            if new["hold"] and new_r["conflicts"] > old_r["conflicts"]:
                flag("a conflict round started under a hold", info)
            if state["hold"] and new["hold"] and any(a["kind"] == "replaceChange" for a in answer["actions"]):
                flag("a change made again under a hold", info)
        if state is not None and event["kind"] == "actionFinished" and answer["changed"]:
            act = next((a for a in state["pending"] if a["id"] == event["action"]), None)
            if act and act["kind"] == "publish" and act["stage"] == "fix" and state["rounds"].get("automatic") \
                    and not any(a["kind"] == "runChecks" for a in new["pending"]):
                walk_floor = new["rounds"]["counted"]
        if event["kind"] == "reopened":
            walk_floor = None
        nr = new["rounds"]
        if nr.get("due"):
            if new["state"] != "fixing":
                flag("due outside fixing", info)
            if any(a["kind"] in ("runAgent", "publish") for a in new["pending"]):
                flag("due with a run pending", info)
            if not (new["hold"] or new["attention"] or any(w["for"] in ("baseFailure", "start", "time", "usageLimit") for w in new["waits"])):
                flag("due round stuck", info)
        if nr.get("automatic"):
            if new["state"] not in ("fixing", "checking", "reviewing"):
                flag("automatic outside a round", info)
            if nr["counted"] < 1:
                flag("automatic with nothing counted", info)
        if nr["counted"] < 0:
            flag("counted below zero", info)
        state = new

print("walks", WALKS, "seed", SEED)
print("problems", dict(problems))
for name, info in examples.items():
    print("----", name)
    print(json.dumps({k: info[k] for k in ("policies", "trail")}, default=str)[:1500])
    print(json.dumps(info["event"], default=str)[:800])
print("top errors", errors.most_common(15))
print("pairs", sorted(pairs_seen.items()))
