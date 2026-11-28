"""#347: a round whose commit conflicts with the base branch's tip, as `publish` found, so
its checks and review did not run: an implement round, an automatic, a person's, a conflict or
a resumed conflict fix round × earlier conflict rounds 0-3 (one that does not count, or not) ×
the latest tried on this base commit, still running, or ended × `conflicts.max_rounds` 1-3 × a
hold label, none, or one NexKit cannot read × autopilot × `merge.auto`. For a held round, the
label taken off with the head moved or the pull request conflicting, clean or unknown, and
`auto_resolve` turned off or `max_rounds` lowered: two sweeps more. Records `report`, then the
preview, the sweeps, GitHub and `nexkit status` after each."""

import itertools
import json
from unittest import mock

from common import (
    FIX_OUTPUT,
    IMPLEMENT_OUTPUT,
    TOK,
    WORKFLOW,
    Artifacts,
    after,
    attempt,
    quiet,
    seed,
    snapshot,
)

from nexkit import autopilot, maintain, progress
from nexkit.github import GitHubError
from nexkit.report import report
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state
from tests.support import FakeGitHub, make_config

HEAD = "c" * 40
BASE = "e" * 40
KINDS = {
    "implement": ("implement", False, ()),
    "auto": ("fix", True, ("auto",)),
    "person": ("fix", False, ()),
    "conflicts": ("fix", True, ("auto", "conflicts")),
    "resumed": ("fix", True, ("auto", "conflicts", "resumed")),
}


def scenarios():
    dims = dict(
        kind=list(KINDS),
        earlier=[0, 1, 2, 3],
        uncounted=[False, True],
        last=["ended", "tried_here", "running"],
        max_rounds=[1, 2, 3],
        hold=["no", "label", "error"],
        ap=[True, False],
        merge=[False, True],
        after=["none", "same_conflicts", "same_clean", "same_unknown", "moved"],
        later=["same", "off", "lower"],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        if sc["hold"] != "label" and (sc["after"] != "none" or sc["later"] != "same"):
            continue
        if sc["uncounted"] and not sc["earlier"]:
            continue
        yield sc


def config(sc, later="same"):
    conflicts = {"auto_resolve": True, "max_rounds": sc["max_rounds"]}
    if later == "off":
        conflicts = {"auto_resolve": False}
    elif later == "lower":
        conflicts = {"auto_resolve": True, "max_rounds": 1}
    return make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        merge={"auto": sc["merge"]},
        conflicts=conflicts,
        notify=["olivia"],
    )


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1, labels=["on-hold"] if sc["hold"] == "label" else ())
    gh.branches["main"] = BASE
    runs = "https://github.com/acme/app/actions/runs"
    plan = {"command": "plan", "status": "success", "outcome": "planned"}
    gh.comment(5, render_run({"url": f"{runs}/1", **plan}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    go = {"command": "go", "status": "success", "outcome": "pr_opened"}
    gh.comment(5, render_run({"url": f"{runs}/2", **go}, "D."))
    gh.add_pull(6, 5, head_sha=HEAD if sc["kind"] == "implement" else "b" * 40)
    gh.pulls[6]["mergeable"] = False
    gh.comment(6, render_state({**empty_state(5), "auto_fixes": 1}))
    for n in range(sc["earlier"]):
        fields = {
            "conflicts": True,
            "base_sha": "d" * 40,
            "trigger": "fix (auto, conflicts)",
            "action": "fix",
            "kinds": ["auto", "conflicts"],
            "outcome": "needs_human",
        }
        if sc["uncounted"] and n == 0:
            fields["outcome"] = "up_to_date"
        gh.round_comment(6, n + 1, "b" * 40, "failed", **fields)
    if sc["kind"] != "implement":
        last = {
            "trigger": "fix (auto)",
            "action": "fix",
            "kinds": ["auto"],
            "outcome": "needs_human",
        }
        if sc["last"] == "tried_here":
            last["conflicts_started"] = BASE
        gh.round_comment(6, sc["earlier"] + 1, "b" * 40, "failed", **last)
    if sc["last"] == "running":
        row = {
            "round": sc["earlier"] + 2,
            "trigger": "review",
            "action": "review",
            "status": "running",
            "head": "b" * 40,
            "url": f"{runs}/99",
        }
        gh.comment(6, render_run(row, "R."))
    cfg = config(sc)
    action, auto, kinds = KINDS[sc["kind"]]
    implement = action == "implement"
    d = {
        "action": action,
        "issue": 5,
        "pr": None if implement else 6,
        "target": 5 if implement else 6,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": None if implement else "b" * 40,
        "note": "",
        "auto": auto,
        "run_id": 4242,
    }
    if "conflicts" in kinds:
        d["conflicts"] = True
        d["base_sha"] = "d" * 40
    if "resumed" in kinds:
        d["resumed"] = True
    art = Artifacts()
    fits = IMPLEMENT_OUTPUT if implement else FIX_OUTPUT
    art.agent({"status": "done", "tokens": TOK, "output": fits})
    pub = {"published": True, "pr": 6, "head": HEAD, "conflicts": ["a.py", "b.py"]}
    needs = {"publish": {"outputs": {"result": json.dumps(pub)}}}
    real_held = autopilot.held

    def held(gh_, cfg_, number):
        if sc["hold"] == "error" and autopilot.takes(cfg_):
            raise GitHubError(502, "Bad gateway")
        return real_held(gh_, cfg_, number)

    res = {}
    with quiet():
        try:
            progress.start(gh, d)
        except Exception as exc:  # noqa: BLE001
            res["start"] = f"EXC {type(exc).__name__}: {exc}"
        seed(gh, cfg)
    with (
        mock.patch.object(autopilot, "held", held),
        mock.patch.object(maintain.time, "sleep", lambda s: None),
        quiet(),
    ):
        res["report"] = attempt(
            report, gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
        )
        res["after_report"] = snapshot(gh)
        after(gh, cfg, res, "1")
        if sc["hold"] == "label":
            gh.issues[5]["labels"] = []
            if sc["after"] == "moved":
                gh.pulls[6]["head"]["sha"] = "9" * 40
            mergeable = {"same_clean": True, "same_unknown": None}.get(sc["after"], False)
            gh.pulls[6]["mergeable"] = mergeable
            cfg = config(sc, sc["later"])
            after(gh, cfg, res, "2")
            after(gh, cfg, res, "3")
    art.close()
    return res
