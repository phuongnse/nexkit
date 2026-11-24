"""Differential of autopilot._conflicts_released (via _released) on one checkout.
python3 rel.py <repo>  -> one line per case"""
import itertools, json, os, sys, io, contextlib
REPO = sys.argv[1]; sys.path.insert(0, REPO); os.chdir(REPO)
from nexkit import autopilot
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config
H = "c" * 40
OUTCOMES = [None, "conflict_fix", "needs_merge", "already_on_base", "up_to_date"]
cases = itertools.product(
    [True, False],               # auto_resolve
    [1, 2, 3],                   # max_rounds
    [H, "d" * 40, None],         # head now
    [True, False, None],         # mergeable as the sweep reads it
    [True, False, None],         # mergeable as _conflict_wait reads it again
    range(0, 6),                 # earlier conflict rounds mask
    [0, 1, 2],                   # flavour of earlier rounds
)
for n, (resolve, most, head, mergeable, again, count, flavour) in enumerate(cases):
    cfg = make_config(conflicts={"auto_resolve": resolve, "max_rounds": most}, autopilot={"enabled": True})
    gh = FakeGitHub(); gh.add_issue(5); gh.add_pull(6, 5, head_sha=head or H)
    gh.pulls[6]["mergeable"] = again
    if head is None:
        gh.pulls[6]["head"] = {"ref": "nexkit/issue-5", "sha": None, "repo": {"full_name": "acme/app"}}
    rounds = []
    for k in range(count):
        fields = {"conflicts": True, "base_sha": "e" * 40, "outcome": OUTCOMES[(k + flavour) % len(OUTCOMES)] or "conflict_fix"}
        if flavour == 1 and k == 0: fields["resumed"] = True
        if flavour == 2 and k == 1: fields["retry"] = 1
        rounds.append(gh.round_comment(6, k + 1, "b" * 40, "failed", **fields))
    run = {"round": count + 1, "head": H, "held_back": "conflicts", "outcome": "held", "status": "success"}
    gh.round_comment(6, count + 1, H, "not run", held_back="conflicts", outcome="held")
    pull_comments = gh.comments(6)
    reads = {"n": 0}
    real = gh.pull
    def pull(number, _real=real, _reads=reads, _m=mergeable, _a=again):
        found = dict(_real(number))
        found["mergeable"] = _m if _reads["n"] == 0 else _a
        _reads["n"] += 1
        return found
    gh.pull = pull
    start = {"basis": None, "target": 5, "note": "", "remember": {}}
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            step = autopilot._released(gh, cfg, {"number": 5}, 6, {}, run, pull_comments, start, None)
        out = json.dumps(step, sort_keys=True, default=str)
    except Exception as exc:
        out = f"crash {type(exc).__name__}: {exc}"
    print(json.dumps({"n": n, "case": [resolve, most, head and head[:1], mergeable, again, count, flavour], "out": out}))
