exec(open("spec1.py").read().split("CASES = {")[0])

def checks_of(*found, base=None):
    """Checks that fail with the signatures given, by name: ("test", "FAIL A")."""
    r = {"checks": [{"name": name, "passed": sig is None, "skipped": False, "signature": sig,
                     "knownBaseFailure": False} for name, sig in found]}
    if base:
        r["baseFailure"] = base
    return ev("actionFinished", action="runChecks", result=r)

LIMIT = {"tokens": None, "limit": {"message": "The account's usage limit.", "reset": "2026-11-24T04:40:00Z"},
         "transient": False, "noResult": False, "error": None, "session": None, "changeVersion": None,
         "timedOut": False, "seconds": None, "usd": None, "output": None}
NO_RESET = {**LIMIT, "limit": {"message": "The account's usage limit.", "reset": None}}
CONFLICTING = {"change": {"id": "308", "version": "v2"}, "refused": None,
               "observed": {"changeId": "308", "version": "v2", "conflicts": True, "merged": False, "empty": False}}

CASES = {
 "an-automatic-fix-round-after-a-check-that-failed-the-same-way-before-and-after-a-fix-round-diagnoses-it": {
   "policies": {"fixRounds": 3},
   "steps": PRE + [checks_of(("test", "FAIL A"), ("lint", "FAIL B")),
     x(REVIEW, **{"rounds.counted": 1, "pending.-1.diagnose": []}),
     FIX, publish("v2"),
     x(checks_of(("test", "FAIL A"), ("lint", "FAIL C")), **{"rounds.repeated": ["test"]}),
     x(REVIEW, **{"rounds.counted": 2, "pending.-1.stage": "fix", "pending.-1.diagnose": ["test"]}),
     FIX, publish("v3"),
     x(checks_of(("test", "FAIL D"), ("lint", "FAIL C")), **{"rounds.repeated": ["lint"]}),
     x(REVIEW, **{"rounds.counted": 3, "pending.-1.diagnose": ["lint"]})]},
 "checks-that-fail-the-same-way-after-an-implement-round-or-a-review-request-are-not-diagnosed": {
   "policies": {"fixRounds": 1},
   "steps": PRE + [checks_of(("test", "FAIL A")),
     x(REVIEW, **{"rounds.counted": 1, "pending.-1.diagnose": []}),
     FIX, publish("v2"), checks_of(("test", "FAIL A")),
     x(REVIEW, **{"attention.reason": "roundsUsedUp", "rounds.repeated": ["test"]}),
     {**x(ev("requested", what="review"), **{"rounds.counted": 1}), "policies": {"fixRounds": 2}, "note": "A fix round is left now."},
     x(checks_of(("test", "FAIL A")), **{"rounds.failing": ["9e6d8cb93a58"]}),
     {**x(REVIEW, **{"rounds.counted": 2, "pending.-1.diagnose": []}), "note": "The checks it follows were a review's, so it diagnoses nothing."}]},
 "a-person-s-fix-round-and-a-conflict-round-diagnose-nothing": {
   "policies": {"fixRounds": 2, "conflicts": "resolve"},
   "state": {"example": "ready", "set": {"rounds": {"started": 2, "counted": 1, "conflicts": 0, "signatures": ["FAIL A"], "findings": [], "criteria": ["met"], "checklist": [], "repeated": ["test"], "failing": ["9e6d8cb93a58"]},
             "attention": {"reason": "roundsUsedUp", "text": "No round is left.", "ref": None}}},
   "steps": [
     x(ev("changeObserved", conflicts=True), **{"rounds.conflicts": 1, "rounds.counted": 1, "pending.-1.stage": "fix", "pending.-1.diagnose": []}),
     FIX, publish("v2"),
     {**x(checks_of(("test", "FAIL A")), **{"rounds.repeated": ["test"]}), "note": "A conflict round is a fix round: its checks compare with those before it."},
     x(REVIEW, **{"rounds.counted": 2, "pending.-1.diagnose": ["test"]}),
     FIX, publish("v3"), checks_of(("test", "FAIL A")),
     x(REVIEW, **{"attention.reason": "roundsUsedUp"}),
     x(ev("requested", what="fix"), **{"rounds.counted": 2, "pending.-1.stage": "fix", "pending.-1.diagnose": []})]},
 "a-wait-for-the-base-branch-s-failure-forgets-a-repeated-failure": {
   "policies": {"fixRounds": 3},
   "steps": PRE + [checks_of(("test", "FAIL A")),
     REVIEW, FIX, publish("v2"),
     x(checks_of(("test", "FAIL A"), base="9"), **{"rounds.baseFailure": "9"}),
     x(REVIEW, **{"rounds.due": True, "waits.0.for": "baseFailure"}),
     x(ev("blockerClosed", blocker="9", completed=True), **{"rounds.counted": 2, "pending.-1.stage": "fix", "pending.-1.diagnose": []})]},
 "a-hold-keeps-a-diagnosis-round-back-and-it-diagnoses-when-it-runs": {
   "policies": {"fixRounds": 3},
   "steps": PRE + [checks_of(("test", "FAIL A")),
     REVIEW, FIX, publish("v2"), ev("held"), checks_of(("test", "FAIL A")),
     x(REVIEW, **{"rounds.due": True, "rounds.counted": 1, "rounds.repeated": ["test"]}),
     x(ev("released"), **{"rounds.counted": 2, "pending.-1.diagnose": ["test"]})]},
 "an-automatic-fix-round-paused-at-a-usage-limit-runs-again-at-its-reset-counting-nothing-and-diagnoses-again": {
   "policies": {"fixRounds": 3},
   "steps": PRE + [checks_of(("test", "FAIL A")),
     REVIEW, FIX, publish("v2"), checks_of(("test", "FAIL A")),
     x(REVIEW, **{"rounds.counted": 2, "rounds.started": 3, "pending.-1.diagnose": ["test"]}),
     x(ev("actionFinished", action="runAgent", result=LIMIT), **{"waits.0.for": "usageLimit", "waits.0.until": "2026-11-24T04:40:00Z", "pending.-1.kind": "wait"}),
     x(ev("tick", wait="w11.1"), **{"rounds.counted": 2, "rounds.started": 3, "pending.-1.stage": "fix", "pending.-1.diagnose": ["test"]})]},
 "a-review-paused-at-a-usage-limit-waits-for-its-reset-and-runs-again": {
   "policies": {"fixRounds": 2},
   "steps": PRE + [checks(True),
     x(ev("actionFinished", action="runAgent", result=LIMIT), **{"waits.0.for": "usageLimit", "pending.-1.until": "2026-11-24T04:40:00Z"}),
     x(ev("tick", wait="w6.1"), **{"pending.-1.stage": "review", "rounds.started": 1}),
     ev("actionFinished", action="wait", result={}),
     x(REVIEW, **{"attention.reason": "accept"})]},
 "a-usage-limit-whose-reset-the-host-does-not-give-waits-for-a-person": {
   "policies": {},
   "steps": PRE + [checks(True),
     x(ev("actionFinished", action="runAgent", result=NO_RESET), **{"attention.reason": "failed", "waits": []})]},
 "a-hold-keeps-a-conflict-round-back-until-it-ends-whatever-rounds-are-left": {
   "policies": {"fixRounds": 2, "conflicts": "resolve", "conflictRounds": 1},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1}), FIX, ev("held"),
     x(ev("actionFinished", action="publish", result=CONFLICTING), **{"hold": True, "rounds.conflicts": 0, "pending": [], "rounds.counted": 1}),
     x(ev("released"), **{"rounds.conflicts": 1, "pending.-1.stage": "fix", "pending.-1.diagnose": [], "rounds.counted": 1}),
     FIX, ev("held"),
     x(ev("actionFinished", action="publish", result={**CONFLICTING, "change": {"id": "308", "version": "v3"}, "observed": {**CONFLICTING["observed"], "version": "v3"}}), **{"pending": [], "rounds.conflicts": 1}),
     x(ev("released"), **{"attention.reason": "acceptRefused", "rounds.conflicts": 1})]},
 "a-conflict-round-counts-no-fix-round-and-the-count-of-the-round-before-it-stands": {
   "policies": {"fixRounds": 2, "conflicts": "resolve"},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1, "rounds.automatic": True}),
     FIX,
     x(ev("actionFinished", action="publish", result=CONFLICTING), **{"rounds.counted": 1, "rounds.conflicts": 1, "pending.-1.stage": "fix"}),
     {**x(ev("changeObserved", version="v9"), **{"rounds.counted": 1}), "note": "A person's push stops the conflict round, which held no count to give back."},
     checks(False),
     x(REVIEW, **{"rounds.counted": 2})]},
 "a-fix-round-a-person-retries-after-it-failed-counts-nothing": {
   "policies": {"fixRounds": 1},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1}),
     x(ev("actionFinished", action="runAgent", result="@error"), **{"attention.reason": "failed", "rounds.counted": 1}),
     x(ev("requested", what="retry"), **{"rounds.counted": 1, "pending.-1.stage": "fix", "pending.-1.diagnose": []})]},
}
