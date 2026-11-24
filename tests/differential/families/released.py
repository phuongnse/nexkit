"""#347's review: a conflict round that a hold label kept back, released once the label is
gone (`autopilot._released`): `conflicts.auto_resolve` × `conflicts.max_rounds` 1-3 × the head
as the round left it, moved, or unknown × whether the pull request conflicts as the sweep
reads it, and as a second read finds it, each yes, no or unknown × 0-5 earlier conflict rounds
of three flavours (some that do not count, resumed, retried). Records the sweep's step.

When the two reads of `mergeable` differ, as while GitHub works the conflict out again,
NexKit 6 read it a second time and could start a conflict round that the first read and the
rules would not; the engine waits for a person with the first read's reason (`known.py`)."""

import itertools

from common import attempt, quiet

from nexkit import autopilot
from tests.support import FakeGitHub, make_config

H = "c" * 40
OUTCOMES = [None, "conflict_fix", "needs_merge", "already_on_base", "up_to_date"]


def scenarios():
    dims = dict(
        resolve=[True, False],
        most=[1, 2, 3],
        head=["same", "moved", "none"],
        mergeable=[True, False, None],
        again=[True, False, None],
        count=list(range(6)),
        flavour=[0, 1, 2],
    )
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))


def run(sc):
    cfg = make_config(
        conflicts={"auto_resolve": sc["resolve"], "max_rounds": sc["most"]},
        autopilot={"enabled": True},
    )
    head = {"same": H, "moved": "d" * 40, "none": None}[sc["head"]]
    gh = FakeGitHub()
    gh.add_issue(5)
    gh.add_pull(6, 5, head_sha=head or H)
    gh.pulls[6]["mergeable"] = sc["again"]
    if head is None:
        gh.pulls[6]["head"] = {
            "ref": "nexkit/issue-5",
            "sha": None,
            "repo": {"full_name": "acme/app"},
        }
    for k in range(sc["count"]):
        outcome = OUTCOMES[(k + sc["flavour"]) % len(OUTCOMES)] or "conflict_fix"
        fields = {"conflicts": True, "base_sha": "e" * 40, "outcome": outcome}
        if sc["flavour"] == 1 and k == 0:
            fields["resumed"] = True
        if sc["flavour"] == 2 and k == 1:
            fields["retry"] = 1
        gh.round_comment(6, k + 1, "b" * 40, "failed", **fields)
    count = sc["count"]
    run_ = {"round": count + 1, "head": H, "held_back": "conflicts", "outcome": "held"}
    run_["status"] = "success"
    gh.round_comment(6, count + 1, H, "not run", held_back="conflicts", outcome="held")
    pull_comments = gh.comments(6)
    reads = {"n": 0}
    real = gh.pull

    def pull(number):
        found = dict(real(number))
        found["mergeable"] = sc["mergeable"] if reads["n"] == 0 else sc["again"]
        reads["n"] += 1
        return found

    gh.pull = pull
    start = {"basis": None, "target": 5, "note": "", "remember": {}}
    with quiet():
        step = attempt(
            autopilot._released, gh, cfg, {"number": 5}, 6, {}, run_, pull_comments, start, None
        )
    return {"step": step}
