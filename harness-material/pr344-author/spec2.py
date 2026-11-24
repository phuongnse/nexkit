exec(open("spec1.py").read().split("CASES = {")[0])
CASES = {
 "an-automatic-fix-round-that-the-host-stops-gives-its-count-back": {
   "policies": {"fixRounds": 2},
   "steps": PRE + [checks(False), x(REVIEW, **{"rounds.counted": 1}), FIX, publish("v2"), checks(False),
     x(REVIEW, **{"rounds.counted": 2, "rounds.automatic": True}),
     {**x(ev("stopped"), **{"rounds.counted": 1}), "note": "Such as a round whose agent the host may no longer run, which NexKit 6 gives back."},
     x(ev("requested", what="retry"), **{"rounds.counted": 1}),
     {**x(ev("stopped"), **{"rounds.counted": 1}), "note": "A person's round counts nothing, so it gives nothing back."}]},
}
