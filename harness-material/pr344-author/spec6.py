exec(open("spec1.py").read().split("CASES = {")[0])
CASES = {
 "a-person-s-review-request-during-a-hold-on-a-change-that-conflicts-follows-the-conflict-policy": {
   "policies": {"fixRounds": 1, "conflicts": "redo"},
   "steps": PRE + [ev("held"), checks(False),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     x(ev("changeObserved", version="v1", conflicts=True), **{"state": "fixing"}),
     x(ev("requested", what="review"), **{"state": "working", "pending.-1.kind": "replaceChange", "rounds.counted": 0})]},
}
