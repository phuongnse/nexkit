"""#570: a merge that GitHub refused, which the sweep tries again once GitHub reports the pull
request as mergeable: the round's marker (`merge_refused`) × GitHub's `mergeable_state`
(`clean`, `unstable`, `has_hooks`, `blocked`, `behind`, `dirty`) × the head moved or not × a
head that GitHub called mergeable and still refused (`refused_clean`) or not × `merge.auto` on or
off × the retried merge going through or refused again with a 405. Records GitHub after it, the
preview, two sweeps and `nexkit status`."""

import contextlib
import itertools
from unittest import mock

from common import HEAD, attempt, planned_issue, quiet, seed, snapshot, sweep

from nexkit import autopilot
from nexkit import report as report_module
from nexkit.github import GitHubError
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config

NEW = "d" * 40


def scenarios():
    dims = dict(
        state=["clean", "unstable", "has_hooks", "blocked", "behind", "dirty"],
        moved=[False, True],
        clean=[False, True],
        auto=[True, False],
        merge=["through", "405"],
    )
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))


def run(sc):
    gh = FakeGitHub()
    planned_issue(gh)
    gh.add_pull(6, 5, head_sha=NEW if sc["moved"] else HEAD)
    gh.pulls[6]["mergeable_state"] = sc["state"]
    gh.required = set()
    if sc["merge"] == "405":
        gh.merge_error = GitHubError(405, "Merge refused.")
    cfg = make_config(
        autopilot={"enabled": True},
        merge={"auto": sc["auto"]},
        conflicts={"auto_resolve": True},
        notify=["olivia"],
    )
    row = {
        "round": 1,
        "trigger": "implement (autopilot)",
        "status": "failure",
        "outcome": "merge_refused",
        "head": HEAD,
        "checks": "passed",
        "verdict": "approve",
        "url": "https://github.com/acme/app/actions/runs/3",
    }
    if sc["clean"]:
        row["refused_clean"] = HEAD
    gh.comment(6, render_run(row))
    seed(gh, cfg)
    res = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(report_module.time, "sleep", lambda s: None))
        stack.enter_context(quiet())
        res["gh"] = snapshot(gh)
        _, steps = autopilot.preview(gh, cfg)
        res["step"] = attempt(lambda: {s["number"]: s for s in steps}.get(5))
        for n in (1, 2):
            res[f"lines{n}"] = attempt(sweep, gh, cfg)
            res[f"gh{n}"] = snapshot(gh)
        from nexkit.status import collect

        res["status"] = attempt(collect, gh, cfg, workers=1)
    return res
