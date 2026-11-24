"""Random walks of the engine with this review's own invariants.
python3 walk.py <repo> <walks> <seed> [maxsteps]"""
import copy, json, os, random, sys
REPO = sys.argv[1]; sys.path.insert(0, REPO); os.chdir(REPO)
from nexkit import engine, protocol
from tests.test_conformance import event_of, allowed, FINAL, WORK

WALKS, SEED = int(sys.argv[2]), int(sys.argv[3])
MAXSTEPS = int(sys.argv[4]) if len(sys.argv) > 4 else 40
CHECKS = ["unit", "lint", "e2e", "types"]
SIGS = ["sig-A", "sig-B"]
AGENT = {"tokens": None, "limit": None, "transient": False, "noResult": False, "error": None,
         "session": None, "changeVersion": None, "timedOut": False, "seconds": None, "usd": None}
FIX = json.load(open("tests/conformance/fixtures.json"))
problems = {}

def policies(rng):
    return {"fixRounds": rng.choice([0, 1, 2, 3]),
            "conflicts": rng.choice(["resolve", "resolve", "person", "redo"]),
            "conflictRounds": rng.choice([1, 1, 2, 3]),
            "accept": rng.choice(["person", "autoWhenPassed"]),
            "planApproval": rng.choice(["person", "autoWhenClean"]),
            "whenRoundsRunOut": rng.choice(["person", "failed"]),
            "reviewAfterFailingChecks": "always"}

def agent_result(rng, stage):
    r = rng.random()
    if r < 0.12:
        reset = rng.choice(["2026-11-25T04:40:00Z", None])
        return {**AGENT, "output": None, "limit": {"message": "limit", "reset": reset}}
    if r < 0.15:
        return {**AGENT, "output": None, "error": "boom"}
    if r < 0.17 and stage in ("implement", "fix"):
        return {**AGENT, "output": None, "timedOut": True}
    key = {"plan": ["plan", "plan", "planQuestions"], "implement": ["implement", "implement", "implementQuestions"],
           "fix": ["fix", "fix", "fix", "fixQuestions"], "review": ["review", "reviewChanges", "reviewChanges", "reviewApproveBlocking"]}[stage]
    return {**AGENT, "output": copy.deepcopy(FIX[rng.choice(key)])}

def checks_result(rng):
    n = rng.randint(1, 4)
    names = rng.sample(CHECKS, n)
    found = []
    for name in names:
        passed = rng.random() < 0.4
        skipped = (not passed) and rng.random() < 0.05
        found.append({"name": name, "passed": passed, "skipped": skipped,
                      "signature": None if passed else rng.choice(SIGS), "knownBaseFailure": False})
    base = "bug-9" if rng.random() < 0.12 and any(not c["passed"] for c in found) else None
    return {"checks": found, "baseFailure": base}

def gen_event(rng, st, versions):
    pending = st["pending"]
    work = [a for a in pending if a["kind"] in WORK and a["kind"] != "wait"]
    choices = []
    for a in work:
        choices += [("finish", a)] * 6
    for w in st["waits"]:
        if w["until"] is not None:
            choices += [("tick", w)] * 3
        if w["for"] == "baseFailure":
            choices += [("baseEnd", w)] * 3
    choices += [("push",), ("conflict",), ("hold",), ("release",), ("request",), ("policy",)]
    choices += [("drop",)] * (1 if rng.random() < 0.1 else 0) + [("lost", a) for a in work if rng.random() < 0.1]
    pick = rng.choice(choices)
    kind = pick[0]
    if kind == "finish":
        a = pick[1]
        if a["kind"] == "runAgent":
            res = agent_result(rng, a["stage"])
        elif a["kind"] == "runChecks":
            res = checks_result(rng)
        elif a["kind"] in ("publish", "replaceChange"):
            cur = (st["change"] or {}).get("version")
            if a["kind"] == "publish" and a.get("stage") == "fix" and cur and rng.random() < 0.15:
                ver = cur
            else:
                versions[0] += 1; ver = f"v{versions[0]}"
            cid = (st["change"] or {}).get("id") or "308"
            res = {"change": {"id": cid, "version": ver}, "refused": None,
                   "observed": {"changeId": cid, "version": ver, "conflicts": rng.random() < 0.2, "merged": False, "empty": False}}
        elif a["kind"] == "accept":
            ok = rng.random() < 0.6
            res = {"accepted": ok, "refusal": None if ok else rng.choice(["conflict", "checkFailed", "checkFailed", "headMoved", "notAccepted", "transient", "waitingForChecks"]), "text": "x"}
            if not ok and res["refusal"] == "checkFailed":
                res["baseFailure"] = "bug-9" if rng.random() < 0.3 else None
        else:
            res = {}
        return {"kind": "actionFinished", "action": a["id"], "result": res}
    if kind == "lost":
        return {"kind": "actionLost", "action": pick[1]["id"]}
    if kind == "tick":
        return {"kind": "tick", "wait": pick[1]["id"]}
    if kind == "baseEnd":
        return {"kind": "blockerClosed", "blocker": pick[1]["on"] or "bug-9", "completed": True}
    if kind == "push":
        if not st["change"]:
            return None
        versions[0] += 1
        return {"kind": "changeObserved", "version": f"v{versions[0]}", "conflicts": rng.random() < 0.2}
    if kind == "conflict":
        if not st["change"]:
            return None
        return {"kind": "changeObserved", "conflicts": rng.random() < 0.7}
    if kind == "hold":
        return {"kind": "held"}
    if kind == "release":
        return {"kind": "released"}
    if kind == "request":
        return {"kind": "requested", "what": rng.choice(["go", "fix", "review", "retry", "fix", "review"])}
    if kind == "policy":
        return "policy"
    if kind == "drop":
        return {"kind": "dropRequested"}
    return None

def flag(name, walk, step, detail):
    problems.setdefault(name, []).append((walk, step, detail))

def walk(w):
    rng = random.Random(SEED * 100003 + w)
    pol = policies(rng)
    st = None
    versions = [0]
    n = 0
    shadow = {"before": {}, "fixed": None, "last_fix_diag": None, "auto_conflict": False}
    for short in ({"kind": "created"}, {"kind": "requested", "what": "go"}):
        n += 1
        ans = engine.answer({"protocol": 1, "call": "step", "state": st, "event": event_of(short, st, n), "policies": pol})
        if "state" not in ans:
            raise SystemExit(str(ans) + str(pol))
        st = ans["state"]
    for i in range(MAXSTEPS):
        if st["state"] in FINAL:
            break
        short = gen_event(rng, st, versions)
        if short is None:
            continue
        if short == "policy":
            pol = policies(rng); continue
        n += 1
        ev = event_of(short, st, n)
        req = {"protocol": 1, "call": "step", "state": st, "event": ev, "policies": pol}
        before_st = copy.deepcopy(st)
        ans = engine.answer(copy.deepcopy(req))
        if "error" in ans:
            # A refused event: only fine for events the walker may send wrongly.
            if ev["kind"] not in ("requested", "changeObserved", "released", "held", "tick", "blockerClosed", "actionLost"):
                flag("error", w, i, (ev["kind"], ans["error"][:120]))
            continue
        errs = protocol.check(ans, "calls/step.answer.schema.json")
        if errs:
            flag("schema", w, i, str(errs)[:200])
        new = ans["state"]; acts = ans["actions"]
        r0, r1 = before_st["rounds"], new["rounds"]
        # Transitions in the doc's table (with its rules for a change).
        if new["state"] != before_st["state"]:
            pair = (before_st["state"], new["state"])
            had = bool(before_st["change"] or new["change"])
            ok = allowed(pol, had) | allowed({**pol, **(before_st.get("policies") or {})}, had)
            if pair not in ok:
                flag("transition", w, i, (pair, ev["kind"]))
        # Carried on.
        if new["state"] not in FINAL:
            carried = [a for a in new["pending"] if a["kind"] in WORK] or new["waits"] or new["hold"] or new["attention"]
            if not carried:
                flag("stuck", w, i, (new["state"], ev["kind"]))
        # runAgent diagnose shape.
        for a in acts:
            if a["kind"] != "runAgent":
                continue
            if a["stage"] == "fix":
                if "diagnose" not in a:
                    flag("diagnose-missing", w, i, a["id"])
                    continue
                diag = a["diagnose"]
                if diag:
                    if not r1.get("automatic"):
                        flag("diagnose-not-automatic", w, i, (ev["kind"], diag))
                    if diag != (r1.get("repeated") or [])[:10]:
                        flag("diagnose-not-repeated", w, i, (diag, r1.get("repeated")))
                elif r1.get("automatic") and (r1.get("repeated") or []) and not (ev["kind"] == "actionFinished" and before_st["pending"] and False):
                    # An automatic round with repeated checks that diagnoses nothing: fine only for an
                    # answered round run again.
                    flag("automatic-no-diagnose", w, i, (ev["kind"], short.get("what"), r1.get("repeated"), new["state"]))
            elif "diagnose" in a:
                flag("diagnose-on-" + a["stage"], w, i, a["id"])
        # Hold: no conflict round or redo starts while held.
        if new["hold"] and ev["kind"] != "requested":
            if r1["conflicts"] > r0["conflicts"]:
                flag("conflict-round-under-hold", w, i, ev["kind"])
            if any(a["kind"] == "replaceChange" for a in acts):
                flag("redo-under-hold", w, i, ev["kind"])
        # repeated is a subset of failing.
        fnames = {x["check"] for x in r1.get("failing", [])}
        if not set(r1.get("repeated", [])) <= fnames and not (ev["kind"] in ("released","tick","blockerClosed")):
            pass  # repeated may outlive failing only through the import; checked below on checks
        # Shadow model of repeated, from NexKit 6's rule.
        finished = ev["kind"] == "actionFinished" and ev["action"] in {a["id"]: 1 for a in before_st["pending"]}
        pend0 = {a["id"]: a for a in before_st["pending"]}
        if finished and pend0[ev["action"]]["kind"] == "runChecks" and ans["changed"]:
            now = {}
            for c in ev["result"]["checks"]:
                if not c["passed"] and not c["skipped"]:
                    now[c["name"]] = c["signature"] or c["name"]
            after_fix = shadow["fixed"] == pend0[ev["action"]].get("changeVersion")
            exp = [k for k, v in now.items() if after_fix and shadow["before"].get(k) == v]
            got = r1.get("repeated", [])
            base_wait = any(x["for"] == "baseFailure" for x in new["waits"]) and not any(x["for"] == "baseFailure" for x in before_st["waits"])
            if base_wait:
                exp = []
            if got != exp:
                flag("repeated-mismatch", w, i, {"exp": exp, "got": got, "before": shadow["before"], "now": now, "after_fix": after_fix})
            shadow["before"] = now
            shadow["fixed"] = None
        if finished and pend0[ev["action"]]["kind"] == "publish" and ans["changed"]:
            if pend0[ev["action"]].get("stage") == "fix":
                shadow["fixed"] = new["change"]["version"] if new["change"] else None
            else:
                shadow["fixed"] = None
            if not any(a["kind"] == "runChecks" for a in new["pending"]):
                shadow["before"] = {}; shadow["fixed"] = None
            if ev["result"]["observed"] and ev["result"]["observed"]["conflicts"] and r0.get("automatic"):
                shadow["auto_conflict"] = True
                shadow["auto_conflict_at"] = (new["hold"], new["state"], pol.get("conflicts"), r1.get("automatic"))
        if ev["kind"] == "requested" and ans["changed"]:
            shadow["fixed"] = None
        # A counted automatic fix round whose change conflicted at its publish keeps its count.
        if r1["counted"] > r0["counted"]:
            shadow["auto_conflict"] = False
        if shadow["auto_conflict"] and r1["counted"] < r0["counted"]:
            flag("conflicted-round-gave-count-back", w, i, (ev["kind"], new["hold"], before_st["state"], new["state"], shadow.get("auto_conflict_at")))
        # Usage limit.
        if finished and pend0[ev["action"]]["kind"] == "runAgent" and ev["result"].get("limit") and ans["changed"]:
            lim = ev["result"]["limit"]
            if r1["counted"] != r0["counted"] or r1["started"] != r0["started"]:
                flag("limit-counted", w, i, None)
            if lim["reset"] is None:
                if new["state"] != "failed":
                    flag("limit-no-reset-not-failed", w, i, new["state"])
            else:
                ws = [x for x in new["waits"] if x["for"] == "usageLimit" and x["until"] == lim["reset"]]
                if not ws or new["state"] != before_st["state"]:
                    flag("limit-wait", w, i, (new["state"], new["waits"]))
                shadow["limited"] = (pend0[ev["action"]]["stage"], pend0[ev["action"]].get("diagnose"), new["state"])
        if ev["kind"] == "tick" and shadow.get("limited") and ans["changed"]:
            stage, diag, sname = shadow.pop("limited")
            runs = [a for a in acts if a["kind"] == "runAgent"]
            if new["state"] == sname and not new["hold"] and not new["attention"]:
                if not runs or runs[0]["stage"] != stage:
                    flag("limit-tick-no-rerun", w, i, (stage, [a["kind"] for a in acts], new["state"]))
                else:
                    if runs[0].get("diagnose") != diag:
                        flag("limit-tick-diagnose-changed", w, i, (diag, runs[0].get("diagnose")))
                    if r1["started"] != r0["started"] or r1["counted"] != r0["counted"]:
                        flag("limit-tick-counted", w, i, (r0["counted"], r1["counted"], r0["started"], r1["started"]))
        if ev["kind"] != "tick" and ans["changed"] and shadow.get("limited") and new["state"] != shadow["limited"][2]:
            shadow.pop("limited", None)
        st = new
    return n

total = 0
for w in range(WALKS):
    total += walk(w)
print(json.dumps({"walks": WALKS, "events": total, "problems": {k: len(v) for k, v in problems.items()},
                  "examples": {k: v[:3] for k, v in problems.items()}}, default=str, indent=1))
