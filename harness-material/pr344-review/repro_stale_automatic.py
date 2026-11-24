"""Finding: a stale `rounds.automatic` gives a judged round's count back (onlyAfterPassing)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eng import *
P = {"fixRounds": 2, "reviewAfterFailingChecks": "onlyAfterPassing"}
print("== A: two pushes during a hold wait")
play(P, START + [FAIL(), AG("fix"), CHANGE("v2"), {"kind": "held"}, FAIL(), PUSH("v3"), PUSH("v4")])
print("== B: a person's /nexkit fix during a hold wait, then a push during it")
play(P, START + [FAIL(), AG("fix"), CHANGE("v2"), {"kind": "held"}, FAIL(), {"kind": "requested", "what": "fix"}, PUSH("v3")])
print("== C: base-failure wait, a person's /nexkit review, then push during the checks")
play(P, START + [FAIL(), AG("fix"), CHANGE("v2"), FAIL("9"), {"kind": "requested", "what": "review"}, PUSH("v3")])
