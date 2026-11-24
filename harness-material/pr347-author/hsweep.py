"""Differential harness: sweeps over a pull request whose round waits, held back by a hold
label (its fix round, its merge or its conflict round) or by a failure the base branch has,
then what happens: the label goes or stays, the bug issue closes or the base passes again, a
person pushes, the pull request conflicts, the rounds used and `fix.max_auto_rounds` differ,
the issue closes; autopilot and `merge.auto` on and off. Two sweeps, the preview, the log,
everything on GitHub and `nexkit status`.

usage: python3 hsweep.py ROOT OUT [shard n]
"""
import contextlib, copy, io, itertools, os, sys
from unittest import mock

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
from common import main, snapshot, walk  # noqa: E402
from nexkit import autopilot  # noqa: E402
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state  # noqa: E402
from tests.support import FakeGitHub, make_config  # noqa: E402

HEAD, NEW = "c" * 40, "d" * 40
WAIT = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}
OVER = {"no": None, "closed": "#77 is closed", "passes": "`ci.yml` passes on `main` again",
        "unnamed": "the round that waits names no bug issue"}


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
    keys = list(dims)
    for values in itertools.product(*dims.values()):
        sc = dict(zip(keys, values))
        ci = sc["kind"].startswith("ci")
        if not ci and sc["over"] != "no":
            continue
        if sc["kind"] == "ci_nobug" and sc["over"] not in ("unnamed",):
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
    gh.comment(5, render_run({"url": next(url), "command": "plan", "status": "success",
                              "outcome": "planned"}, "Done."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    gh.comment(5, render_run({"url": next(url), "command": "go", "status": "success",
                              "outcome": "pr_opened"}, "Done."))
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
    gh.round_comment(6, 1, "b" * 40, "failed", trigger="implement", action="implement",
                     kinds=[], outcome="auto_fix", url=next(url))
    row = {"trigger": "fix (auto)", "action": "fix", "kinds": ["auto"], "url": next(url)}
    if ci:
        gh.round_comment(6, 2, HEAD, "failed", outcome="ci_waiting", ci_wait=wait,
                         status="success", **row)
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
    res = {"sc": sc}

    def over(gh_, wait):
        return OVER[sc["over"]]

    env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}
    with mock.patch.dict(os.environ, env), mock.patch.object(autopilot, "ci_wait_over", over), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            _, steps = autopilot.preview(gh, cfg)
            res["step"] = walk({s["number"]: s for s in steps}.get(5))
        except Exception as e:  # noqa: BLE001
            res["step"] = f"EXC {type(e).__name__}: {e}"
        for n in (1, 2):
            try:
                res[f"lines{n}"] = walk(autopilot.sweep(gh, cfg, "nexkit.yml", "main"))
            except Exception as e:  # noqa: BLE001
                res[f"lines{n}"] = f"EXC {type(e).__name__}: {e}"
            res[f"gh{n}"] = snapshot(gh)
        try:
            from nexkit.status import collect

            res["status"] = walk(collect(gh, cfg, workers=1))
        except Exception as e:  # noqa: BLE001
            res["status"] = f"EXC {type(e).__name__}: {e}"
    return res


if __name__ == "__main__":
    main(scenarios, run)
