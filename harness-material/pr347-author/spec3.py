exec(open("spec1.py").read().split("CASES = {")[0])
CASES = {
 "failing-checks-with-no-fix-round-left-wait-for-a-person-when-a-review-follows-only-passing-checks": {
   "policies": {"fixRounds": 0, "reviewAfterFailingChecks": "onlyAfterPassing"},
   "steps": PRE + [x(checks(False), **{"attention.reason": "roundsUsedUp"})]},
}
