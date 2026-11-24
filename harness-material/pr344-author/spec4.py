exec(open("spec1.py").read().split("CASES = {")[0])
CASES = {
 "a-fix-round-its-failing-checks-judged-keeps-its-count-when-pushes-come-during-the-next-wait": {
   "policies": {"fixRounds": 2, "reviewAfterFailingChecks": "onlyAfterPassing"},
   "steps": PRE + [x(checks(False), **{"rounds.counted": 1, "pending.-1.stage": "fix"}), FIX, publish("v2"),
     ev("held"),
     x(checks(False), **{"rounds.counted": 1, "rounds.due": True}),
     x(ev("changeObserved", version="v3"), **{"rounds.counted": 1}),
     x(ev("changeObserved", version="v4"), **{"rounds.counted": 1})]},
 "a-fix-round-its-review-judged-keeps-its-count-when-a-person-asks-for-a-fix-during-the-next-wait": {
   "policies": {"fixRounds": 2},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1}), FIX, publish("v2"),
     ev("held"), checks(False),
     x(REVIEW, **{"rounds.counted": 1, "rounds.due": True}),
     x(ev("requested", what="fix"), **{"rounds.counted": 1, "pending.-1.stage": "fix"}),
     x(ev("changeObserved", version="v3"), **{"rounds.counted": 1})]},
 "a-person-s-fix-request-while-an-automatic-fix-round-runs-changes-nothing": {
   "policies": {"fixRounds": 2},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1, "pending.-1.stage": "fix"}),
     x(ev("requested", what="fix"), **{"rounds.counted": 1, "pending.-1.stage": "fix"})]},
 "a-required-check-that-fails-at-the-accept-after-a-fix-round-that-changed-nothing-starts-the-next-fix-round": {
   "policies": {"fixRounds": 2, "accept": "autoWhenPassed"},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1}), FIX,
     x(publish("v1"), **{"rounds.recheck": "v1"}), checks(True),
     x(REVIEW, **{"pending.-1.kind": "accept"}),
     x(accept(), **{"rounds.counted": 2, "pending.-1.stage": "fix"})]},
}
