"""A person's review request on a conflicting change under a hold, from ready."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from eng import Item, chk, pub
for policy in ("resolve", "redo", "person"):
    print("==", policy)
    it = Item({"fixRounds": 2, "conflicts": policy, "conflictRounds": 2})
    it.send({"kind": "created"})
    it.send({"kind": "requested", "what": "go"})
    it.send({"kind": "actionFinished", "action": "runAgent", "result": "@agent:implement"})
    it.send({"kind": "actionFinished", "action": "publish", "result": pub("308", "v1")})
    it.send({"kind": "actionFinished", "action": "runChecks", "result": {"checks": [chk("test", True)]}})
    it.send({"kind": "actionFinished", "action": "runAgent", "result": "@agent:review"})  # ready (accept person)
    it.send({"kind": "held"})
    it.send({"kind": "changeObserved", "conflicts": True})
    it.send({"kind": "requested", "what": "review"})
    it.send({"kind": "released"})
    it.dump()
    print("attention", it.state["attention"], "pending", [a["kind"] for a in it.state["pending"]])
