def ev(kind, **f):
    return {"event": {"kind": kind, **f}}

def checks(passed, base=None):
    r = {"checks": [{"name": "test", "passed": passed, "skipped": False,
                     "signature": None if passed else "fail test_x", "knownBaseFailure": False}]}
    if base:
        r["baseFailure"] = base
    return ev("actionFinished", action="runChecks", result=r)

def publish(v):
    return ev("actionFinished", action="publish", result={"change": {"id": "308", "version": v}, "refused": None})

def x(step, **expect):
    return {**step, "expect": expect}

PRE = [ev("created"), ev("requested", what="go"),
       ev("actionFinished", action="runAgent", result="@agent:implement"), publish("v1")]
REVIEW = ev("actionFinished", action="runAgent", result="@agent:review")
FIX = ev("actionFinished", action="runAgent", result="@agent:fix")

def accept(base=None):
    r = {"accepted": False, "refusal": "checkFailed", "text": "A required check failed."}
    if base:
        r["baseFailure"] = base
    return ev("actionFinished", action="accept", result=r)

CASES = {
 "a-person-s-push-during-a-hold-is-checked-and-the-fix-round-after-it-counts-when-it-runs": {
   "policies": {"fixRounds": 1},
   "steps": PRE + [ev("held"), checks(False),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True, "hold": True}),
     x(ev("changeObserved", version="v2"), **{"rounds.counted": 0, "change.version": "v2", "hold": True}),
     checks(False),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     x(ev("released"), **{"rounds.counted": 1, "pending.-1.stage": "fix"})]},
 "a-person-s-push-during-a-base-failure-wait-is-checked": {
   "policies": {"fixRounds": 1},
   "steps": PRE + [checks(False, "9"),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     x(ev("changeObserved", version="v2"), **{"rounds.counted": 0, "waits": []}),
     checks(False),
     x(REVIEW, **{"rounds.counted": 1, "pending.-1.stage": "fix"})]},
 "a-person-s-fix-request-during-a-base-failure-wait-runs-and-counts-nothing": {
   "policies": {"fixRounds": 1},
   "steps": PRE + [checks(False, "9"),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     x(ev("requested", what="fix"), **{"rounds.counted": 0, "waits": [], "pending.-1.stage": "fix"}),
     FIX, publish("v2"), checks(False, "9"),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     x(ev("blockerClosed", blocker="9", completed=True), **{"rounds.counted": 1, "pending.-1.stage": "fix"})]},
 "a-person-s-review-request-during-a-hold-checks-the-change": {
   "policies": {"fixRounds": 1},
   "steps": PRE + [ev("held"), checks(False),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     x(ev("requested", what="review"), **{"rounds.counted": 0, "hold": True}),
     checks(True),
     x(REVIEW, **{"attention.reason": "accept", "rounds.counted": 0})]},
 "a-lower-fix-round-limit-set-during-a-hold-leaves-the-held-round-to-a-person": {
   "policies": {"fixRounds": 2},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1}), FIX, publish("v2"),
     ev("held"), checks(False),
     x(REVIEW, **{"rounds.counted": 1, "rounds.due": True}),
     {**x(ev("released"), **{"rounds.counted": 1, "attention.reason": "roundsUsedUp"}), "policies": {"fixRounds": 1}}]},
 "a-person-s-review-of-a-version-a-fix-round-left-as-it-was-leads-to-the-next-fix-round": {
   "policies": {"fixRounds": 2},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1}), FIX,
     x(publish("v1"), **{"rounds.recheck": "v1"}), checks(False),
     x(REVIEW, **{"attention.reason": "roundsUsedUp", "rounds.counted": 1}),
     ev("requested", what="review"), checks(False),
     x(REVIEW, **{"rounds.counted": 2, "pending.-1.stage": "fix"})]},
 "the-base-failure-wait-ends-when-the-base-passes-again-and-an-end-sent-before-the-wait-changes-nothing": {
   "policies": {"fixRounds": 1},
   "steps": PRE + [
     {**ev("blockerClosed", blocker="9", completed=True), "note": "The base branch passes again before the round's checks end: the host sends the end of the failure's item, which no wait names yet."},
     checks(False, "9"),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     {**x(ev("blockerClosed", blocker="9", completed=True), **{"rounds.counted": 1, "waits": []}), "note": "The host's next sweep finds the base passing again, or the item closed, while the wait stands, and sends it again as a new event."}]},
 "an-automatic-fix-round-that-the-host-stops-gives-its-count-back": {
   "policies": {"fixRounds": 2},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1, "rounds.automatic": True}),
     x(ev("stopped"), **{"rounds.counted": 0}),
     x(ev("requested", what="retry"), **{"rounds.counted": 0}),
     x(ev("stopped"), **{"rounds.counted": 0})]},
 "an-automatic-fix-round-whose-checks-a-conflict-stops-gives-its-count-back": {
   "policies": {"fixRounds": 2, "conflicts": "resolve"},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1}), FIX, publish("v2"),
     x(ev("changeObserved", version="v2", conflicts=True), **{"rounds.counted": 0, "rounds.conflicts": 1})]},
 "an-automatic-fix-round-whose-review-ends-keeps-its-count": {
   "policies": {"fixRounds": 2},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1}), FIX, publish("v2"), checks(True),
     x(REVIEW, **{"rounds.counted": 1, "attention.reason": "accept"}),
     x(ev("dropRequested"), **{"rounds.counted": 1})]},
 "a-required-check-that-fails-at-the-accept-starts-a-counted-fix-round": {
   "policies": {"fixRounds": 2, "accept": "autoWhenPassed"},
   "steps": PRE + [checks(True), REVIEW,
     x(accept(), **{"rounds.counted": 1, "pending.-1.stage": "fix"})]},
 "a-required-check-that-fails-as-the-base-does-at-the-accept-waits-for-the-base": {
   "policies": {"fixRounds": 2, "accept": "autoWhenPassed"},
   "steps": PRE + [checks(True), REVIEW,
     x(accept("9"), **{"rounds.counted": 0, "rounds.due": True, "waits.0.on": "9"}),
     x(ev("blockerClosed", blocker="9", completed=False), **{"rounds.counted": 1})]},
 "a-required-check-that-fails-at-the-accept-with-no-fix-round-left-waits-for-a-person": {
   "policies": {"fixRounds": 0, "accept": "autoWhenPassed"},
   "steps": PRE + [checks(True), REVIEW,
     x(accept(), **{"attention.reason": "roundsUsedUp"})]},
}
