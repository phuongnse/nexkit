"""#390: the wait for required checks at a merge and its six hours: the round's own merge after
an implement round, a sweep over a merge that waits (`merge_waiting`), or a writer's approval
(`maintain.merge_approved`) × when the checks first had not passed (none, 1 hour, 5 hours 59
minutes, 6 hours 1 minute ago, or at a time in 2099, which the comparison keeps) × the head
moved × GitHub's `mergeable_state` (`blocked`, `behind`) × `conflicts.auto_resolve` × autopilot
× the checks still pending or passed. Records the command, GitHub after it, the preview, two
sweeps and `nexkit status`."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from unittest import mock

from common import HEAD, TOK, WORKFLOW, Artifacts, attempt, planned_issue, quiet, snapshot, sweep

from nexkit import autopilot, progress
from nexkit import report as report_module
from nexkit.github import GitHubError
from nexkit.maintain import maintain
from nexkit.report import report
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config

NEW = "d" * 40
AGO = {
    "none": None,
    "1h": timedelta(hours=1),
    "5h59m": timedelta(hours=5, minutes=59),
    "6h01m": timedelta(hours=6, minutes=1),
    # A time the clock does not give, so that the comparison sees whether it is kept.
    "2099": "2099-01-01T00:00:00Z",
}
REVIEW = {
    "verdict": "approve",
    "summary": "S.",
    "findings": [],
    "criteria": [{"criterion": "x", "status": "met", "test": "t", "evidence": "e"}],
}


def scenarios():
    dims = dict(
        path=["first", "sweep", "approved"],
        since=list(AGO),
        moved=[False, True],
        state=["blocked", "behind"],
        resolve=[False, True],
        ap=[True, False],
        checks=["pending", "passed"],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        # The round's own merge has no earlier refusal to count from.
        if sc["path"] == "first" and sc["since"] != "none":
            continue
        yield sc


def run(sc):
    gh = FakeGitHub()
    planned_issue(gh)
    gh.add_pull(6, 5, head_sha=NEW if sc["moved"] else HEAD)
    gh.pulls[6]["mergeable_state"] = sc["state"]
    gh.required = {"e2e"}
    if sc["checks"] == "pending":
        gh.merge_error = GitHubError(405, 'Required status check "e2e" is expected.')
    else:
        gh.commit_checks[HEAD] = [
            {"id": 77, "name": "e2e", "conclusion": "success", "html_url": "https://job/77"}
        ]
    cfg = make_config(
        autopilot={"enabled": sc["ap"]},
        merge={"auto": True},
        conflicts={"auto_resolve": sc["resolve"]},
        notify=["olivia"],
    )
    res = {}
    art = Artifacts()
    with mock.patch.object(report_module.time, "sleep", lambda s: None), quiet():
        if sc["path"] == "first":
            art.agent({"status": "done", "tokens": TOK})
            art.checks(
                [{"name": "test", "run": "make test", "exit_code": 0, "passed": True, "output": ""}]
            )
            art.review({"status": "done", "tokens": TOK, "output": REVIEW})
            d = {
                "action": "implement",
                "issue": 5,
                "pr": None,
                "target": 5,
                "branch": "nexkit/issue-5",
                "base": "main",
                "head": None,
                "note": "",
                "auto": True,
                "autopilot": "true",
                "run_id": 4242,
            }
            pub = {"published": True, "pr": 6, "head": HEAD}
            needs = {"publish": {"outputs": {"result": json.dumps(pub)}}}

            def first_round():
                progress.start(gh, d)
                return report(
                    gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
                )

            res["report"] = attempt(first_round)
        else:
            row = {
                "round": 1,
                "trigger": "implement (autopilot)",
                "status": "success",
                "outcome": "merge_waiting" if sc["path"] == "sweep" else "merge_refused",
                "head": HEAD,
                "checks": "passed",
                "verdict": "approve",
                "url": "https://github.com/acme/app/actions/runs/3",
            }
            ago = AGO[sc["since"]]
            if isinstance(ago, timedelta):
                row["waiting_since"] = autopilot.iso(datetime.now(UTC) - ago)
            elif ago:
                row["waiting_since"] = ago
            gh.comment(6, render_run(row))
            if sc["path"] == "approved":
                decision = {"action": "maintain", "task": "approved", "pr": 6, "issue": 5}
                decision.update(actor="olivia", commit=HEAD)
                res["approved"] = attempt(
                    maintain, gh, decision, cfg, workflow="nexkit.yml", ref="main"
                )
        res["gh"] = snapshot(gh)
        _, steps = autopilot.preview(gh, cfg)
        res["step"] = attempt(lambda: {s["number"]: s for s in steps}.get(5))
        for n in (1, 2):
            res[f"lines{n}"] = attempt(sweep, gh, cfg)
            res[f"gh{n}"] = snapshot(gh)
        from nexkit.status import collect

        res["status"] = attempt(collect, gh, cfg, workers=1)
    art.close()
    return res
