"""Drive the engine at a tree (argv or NEXKIT_TREE) with short events, as the conformance runner does."""
import json, os, sys
TREE = os.environ.get("NEXKIT_TREE", os.path.expanduser("~/repos/nexkit-r344"))
sys.path.insert(0, TREE)
os.chdir(TREE)
from nexkit import engine
from tests.test_conformance import event_of

def play(policies, shorts, state=None, show=True, number=0):
    out = []
    for short in shorts:
        if "policies" in short:
            policies = short.pop("policies")
        number += 1
        event = event_of(short, state, number)
        ans = engine.answer({"protocol": 1, "call": "step", "state": state, "event": event, "policies": policies})
        if "error" in ans:
            out.append(("ERROR", short["kind"], ans["error"]))
            if show: print("ERROR", short, ans["error"])
            continue
        state = ans["state"]
        r = state["rounds"]
        line = (short["kind"] + ("/" + short.get("what", "") if short["kind"] == "requested" else ""),
                state["state"], [a["kind"] + ("/" + a.get("stage", "") if a["kind"] == "runAgent" else "") for a in ans["actions"]],
                {k: r.get(k) for k in ("counted", "due", "automatic", "recheck", "started")},
                [w["for"] for w in state["waits"]], (state["attention"] or {}).get("reason"), state["hold"])
        out.append(line)
        if show: print(*line)
    return state, out

CHANGE = lambda v: {"kind": "actionFinished", "action": "publish", "result": {"change": {"id": "308", "version": v}, "refused": None}}
FAIL = lambda base=None: {"kind": "actionFinished", "action": "runChecks", "result": {"checks": [{"name": "test", "passed": False, "skipped": False, "signature": "fail test_x", "knownBaseFailure": False}], **({"baseFailure": base} if base else {})}}
PASS = {"kind": "actionFinished", "action": "runChecks", "result": {"checks": [{"name": "test", "passed": True, "skipped": False, "signature": None, "knownBaseFailure": False}]}}
AG = lambda name: {"kind": "actionFinished", "action": "runAgent", "result": "@agent:" + name}
PUSH = lambda v: {"kind": "changeObserved", "version": v}
START = [{"kind": "created"}, {"kind": "requested", "what": "go"}, AG("implement"), CHANGE("v1")]
