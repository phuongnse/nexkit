"""#344: sweeps over a pull request whose round waits, held back by a hold label (its fix
round, its merge, its conflict round, or a held round of an old marker) or by a failure the
base branch has (naming its bug issue or not), then what happens: the label goes or stays,
the bug issue closes or the base passes again, a person pushes, the pull request conflicts,
the rounds used and `fix.max_auto_rounds` differ, the issue closes; autopilot, `merge.auto`
and `conflicts.auto_resolve`. Records the preview, two sweeps with GitHub after each, and
`nexkit status`."""

import itertools
from unittest import mock

from common import after, attempt, quiet, snapshot

from nexkit import autopilot
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state
from tests.support import FakeGitHub, make_config

HEAD, NEW = "c" * 40, "d" * 40
WAIT = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}
OVER = {
    "no": None,
    "closed": "#77 is closed",
    "passes": "`ci.yml` passes on `main` again",
    "unnamed": "the round that waits names no bug issue",
}


def scenarios():
    dims = dict(
        kind=["held_fix", "held_merge", "held_conflicts", "held_none", "ci", "ci_nobug"],
        label=[False, True],
        over=list(OVER),
        pushed=[False, True],
        conflicts=[False, True],
        fixes=[None, 0, 1, 2, 3],
        rounds=[0, 1, 2],
        ap=[True, False],
        merge=[False, True],
        resolve=[False, True],
        closed=[False, True],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        ci = sc["kind"].startswith("ci")
        if not ci and sc["over"] != "no":
            continue
        if sc["kind"] == "ci_nobug" and sc["over"] != "unnamed":
            continue
        if sc["kind"] == "ci" and sc["over"] == "unnamed":
            continue
        if sc["kind"] != "held_merge" and sc["merge"]:
            continue
        if sc["kind"] != "held_conflicts" and sc["resolve"]:
            continue
        yield sc


def build(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1, labels=["on-hold"] if sc["label"] else ())
    url = iter(f"https://github.com/acme/app/actions/runs/{n}" for n in range(1, 99))
    plan = {"command": "plan", "status": "success", "outcome": "planned"}
    gh.comment(5, render_run({"url": next(url), **plan}, "Done."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    go = {"command": "go", "status": "success", "outcome": "pr_opened"}
    gh.comment(5, render_run({"url": next(url), **go}, "Done."))
    gh.add_pull(6, 5, head_sha=NEW if sc["pushed"] else HEAD)
    gh.pulls[6]["mergeable"] = not sc["conflicts"]
    if sc["conflicts"]:
        gh.pulls[6]["mergeable_state"] = "dirty"
    ci = sc["kind"].startswith("ci")
    wait = dict(WAIT) if sc["kind"] == "ci" else {"workflow": "ci.yml", "branch": "main"}
    if sc["fixes"] is not None:
        state = {**empty_state(5), "auto_fixes": sc["fixes"]}
        if ci:
            state["ci_wait"] = wait
        gh.comment(6, render_state(state))
    first = {"trigger": "implement", "action": "implement", "kinds": [], "outcome": "auto_fix"}
    gh.round_comment(6, 1, "b" * 40, "failed", url=next(url), **first)
    row = {"trigger": "fix (auto)", "action": "fix", "kinds": ["auto"], "url": next(url)}
    if ci:
        gh.round_comment(
            6, 2, HEAD, "failed", outcome="ci_waiting", ci_wait=wait, status="success", **row
        )
    else:
        back = sc["kind"].removeprefix("held_")
        fields = {} if back == "none" else {"held_back": back}
        checks = "not run" if back == "conflicts" else ("passed" if back == "merge" else "failed")
        gh.round_comment(6, 2, HEAD, checks, outcome="held", status="success", **fields, **row)
    if sc["closed"]:
        gh.issues[5]["state"] = "closed"
        gh.issues[5]["state_reason"] = "completed"
    return gh


def run(sc):
    gh = build(sc)
    cfg = make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        merge={"auto": sc["merge"]},
        fix={"max_auto_rounds": sc["rounds"]},
        conflicts={"auto_resolve": sc["resolve"]},
        notify=["olivia"],
    )
    res = {}
    over = mock.patch.object(autopilot, "ci_wait_over", lambda gh_, wait: OVER[sc["over"]])
    with over, quiet():
        after(gh, cfg, res, sweep=False, status=False)
        for n in (1, 2):
            res[f"lines{n}"] = attempt(autopilot.sweep, gh, cfg, "nexkit.yml", "main")
            res[f"gh{n}"] = snapshot(gh)
        from nexkit.status import collect

        res["status"] = attempt(collect, gh, cfg, workers=1)
    return res
