exec(open("spec1.py").read().split("CASES = {")[0])
CASES = {
 "a-held-fix-round-no-longer-waits-once-a-push-moves-the-item-to-its-checks": {
   "policies": {"fixRounds": 2},
   "steps": PRE + [ev("held"), checks(False),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     x(ev("changeObserved", version="v2"), **{"rounds.counted": 0, "pending.-1.kind": "runChecks"}),
     x(ev("released"), **{"rounds.counted": 0, "pending.-1.kind": "runChecks"})]},
}
