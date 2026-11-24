exec(open("spec1.py").read().split("CASES = {")[0])
CASES = {
 "a-person-s-review-request-during-a-hold-on-a-change-that-conflicts-follows-the-conflict-policy": {
   "policies": {"fixRounds": 1, "conflicts": "redo"},
   "steps": PRE + [ev("held"), checks(False),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     x(ev("changeObserved", version="v1", conflicts=True), **{"state": "fixing"}),
     {**x(ev("requested", what="review"), **{"state": "checking", "pending": [], "rounds.counted": 0}), "note": "The hold keeps the change made again back, as it keeps a conflict round back."},
     x(ev("released"), **{"state": "working", "pending.-1.kind": "replaceChange", "rounds.counted": 0})]},
 "a-person-s-review-request-during-a-base-failure-wait-on-a-change-that-conflicts-follows-the-conflict-policy": {
   "policies": {"fixRounds": 1, "conflicts": "redo"},
   "steps": PRE + [checks(False, "9"),
     x(REVIEW, **{"rounds.counted": 0, "rounds.due": True}),
     x(ev("changeObserved", version="v1", conflicts=True), **{"state": "fixing"}),
     x(ev("requested", what="review"), **{"state": "working", "pending.-1.kind": "replaceChange", "rounds.counted": 0})]},
}
