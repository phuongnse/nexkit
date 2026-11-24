"""Small engine driver on top of the repo's conformance helpers. Run with cwd = repo root."""
import json, sys, os
sys.path.insert(0, os.getcwd())
from tests.test_conformance import event_of, run_program

def chk(name, passed=False, sig="sig-A"):
    return {"name": name, "passed": passed, "skipped": False, "signature": None if passed else sig, "knownBaseFailure": False}

def pub(cid, ver, conflicts=False):
    return {"change": {"id": cid, "version": ver}, "refused": None,
            "observed": {"changeId": cid, "version": ver, "conflicts": conflicts, "merged": False, "empty": False}}

class Item:
    def __init__(self, policies):
        self.policies = policies; self.state = None; self.n = 0; self.log = []
    def send(self, short, policies=None):
        self.n += 1
        if policies is not None: self.policies = policies
        req = {"protocol": 1, "call": "step", "state": self.state,
               "event": event_of(short, self.state, self.n), "policies": self.policies}
        ans = json.loads(run_program(req))
        if "error" in ans:
            self.log.append((short["kind"], "ERROR", ans["error"]))
            return ans
        self.state = ans["state"]
        self.log.append((short["kind"], self.state["state"], [a["kind"] + (":" + a.get("stage","")) if a["kind"]=="runAgent" else a["kind"] for a in ans["actions"]], dict(self.state["rounds"])))
        return ans
    def dump(self):
        for row in self.log: print(row)
