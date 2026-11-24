exec(open("spec312.py").read().split("\nCASES = {")[0])
CASES = {
 "a-round-whose-change-conflicts-at-its-publish-leaves-no-checks-for-the-next-round-to-compare-with": {
   "policies": {"fixRounds": 3, "conflicts": "resolve"},
   "steps": PRE + [checks_of(("test", "FAIL A")),
     x(REVIEW, **{"rounds.counted": 1, "rounds.failing": ["9e6d8cb93a58"]}),
     FIX,
     x(ev("actionFinished", action="publish", result=CONFLICTING), **{"rounds.conflicts": 1, "pending.-1.diagnose": []}),
     FIX, publish("v3"),
     {**x(checks_of(("test", "FAIL A")), **{"rounds.failing": ["9e6d8cb93a58"]}), "note": "The conflict round's checks have none before them to compare with, so nothing repeats."},
     x(REVIEW, **{"rounds.counted": 2, "pending.-1.diagnose": []})]},
 "a-review-request-on-the-version-a-fix-round-published-compares-nothing": {
   "policies": {"fixRounds": 3},
   "steps": PRE + [checks_of(("test", "FAIL A")), REVIEW, FIX, publish("v2"),
     {**x(ev("stopped"), **{"state": "failed"}), "note": "The fix round's checks never end."},
     x(ev("requested", what="review"), **{"state": "checking"}),
     checks_of(("test", "FAIL A")),
     {**x(REVIEW, **{"rounds.counted": 1, "pending.-1.diagnose": []}), "note": "The stop gave the automatic round's count back."}]},
 "a-person-s-review-request-follows-the-conflict-policy-given-now": {
   "policies": {"fixRounds": 1, "conflicts": "redo"},
   "steps": PRE + [checks_of(("test", "FAIL A"), base="9"),
     x(REVIEW, **{"rounds.due": True, "policies.conflicts": "redo"}),
     x(ev("changeObserved", version="v1", conflicts=True), **{"state": "fixing"}),
     {**x(ev("requested", what="review"), **{"state": "ready", "attention.reason": "acceptRefused"}), "policies": {"fixRounds": 1, "conflicts": "person"}, "note": "The round that waited started under redo; the request's round takes the person policy given now."}]},
 "a-hold-on-an-automatic-fix-round-whose-change-conflicts-keeps-its-count-when-pushes-follow": {
   "policies": {"fixRounds": 2, "conflicts": "resolve", "conflictRounds": 2},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1, "rounds.automatic": True}),
     ev("held"), FIX,
     x(ev("actionFinished", action="publish", result=CONFLICTING), **{"state": "checking", "pending": [], "rounds.counted": 1}),
     x(ev("changeObserved", version="v3"), **{"state": "checking", "rounds.counted": 1}),
     x(ev("changeObserved", version="v4"), **{"state": "checking", "rounds.counted": 1})]},
 "a-person-s-review-request-from-ready-during-a-hold-on-a-change-that-conflicts-waits-in-checking": {
   "policies": {"conflicts": "resolve"},
   "state": {"example": "ready", "set": {"hold": True, "attention": {"reason": "accept", "text": "The change passed, and a person decides its accept.", "ref": None},
             "observed": {"changeId": "308", "version": "5a54e2b", "conflicts": True, "merged": False, "empty": False}}},
   "steps": [
     x(ev("requested", what="review"), **{"state": "checking", "attention": None, "pending": []}),
     x(ev("released"), **{"rounds.conflicts": 1, "pending.-1.stage": "fix"})]},
}
