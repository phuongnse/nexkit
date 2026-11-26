"""#424: a merge that GitHub refused because the branch is behind its base: the round's own
merge after an implement round, a sweep over a merge that waits (`merge_waiting`), or a writer's
approval (`maintain.merge_approved`) × `conflicts.auto_resolve` × earlier updates of the branch
0-3 × `conflicts.max_rounds` 1 or 3 × an error of the update (none, of `update_branch`, of the
dispatch of the review) × required checks still pending or passed × the time a sweep's merge
waited already (none, 1 hour, 6 hours 1 minute) × autopilot. Records the command, GitHub after
it, the preview, two sweeps and `nexkit status`."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from unittest import mock

from common import HEAD, TOK, WORKFLOW, Artifacts, attempt, planned_issue, quiet, snapshot, sweep

from nexkit import autopilot, maintain, progress
from nexkit import report as report_module
from nexkit.github import GitHubError
from nexkit.maintain import maintain as maintain_run
from nexkit.report import report
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config

BASE = "e" * 40
RUNS = "https://github.com/acme/app/actions/runs"
WAITED = {"none": None, "1h": timedelta(hours=1), "6h01m": timedelta(hours=6, minutes=1)}
REVIEW = {
    "verdict": "approve",
    "summary": "S.",
    "findings": [],
    "criteria": [{"criterion": "x", "status": "met", "test": "t", "evidence": "e"}],
}


def scenarios():
    dims = dict(
        path=["first", "sweep", "approved"],
        resolve=[False, True],
        earlier=[0, 1, 2, 3],
        max_rounds=[1, 3],
        error=["none", "update", "dispatch"],
        checks=["pending", "passed"],
        waited=list(WAITED),
        ap=[True, False],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        # Only a sweep's merge has waited before.
        if sc["path"] != "sweep" and sc["waited"] != "none":
            continue
        yield sc


def run(sc):
    gh = FakeGitHub()
    planned_issue(gh)
    gh.branches["main"] = BASE
    gh.add_pull(6, 5, head_sha=HEAD)
    gh.pulls[6]["mergeable_state"] = "behind"
    gh.required = {"e2e"}
    if sc["checks"] == "pending":
        gh.merge_error = GitHubError(405, 'Required status check "e2e" is expected.')
    else:
        gh.commit_checks[HEAD] = [
            {"id": 77, "name": "e2e", "conclusion": "success", "html_url": "https://job/77"}
        ]
        gh.merge_error = GitHubError(405, "Head branch is out of date. Review and try again.")
    cfg = make_config(
        autopilot={"enabled": sc["ap"]},
        merge={"auto": True},
        conflicts={"auto_resolve": sc["resolve"], "max_rounds": sc["max_rounds"]},
        notify=["olivia"],
    )
    for n in range(sc["earlier"]):
        fields = {"updated_branch": True, "trigger": "review", "action": "review"}
        gh.round_comment(6, n + 1, "b" * 40, "passed", **fields)
    number = sc["earlier"] + 1
    res = {}
    art = Artifacts()
    broken = GitHubError(422, "Cannot update the branch")
    failing = mock.patch.object(gh, "update_branch", side_effect=broken)
    if sc["error"] == "update":
        failing.start()
    if sc["error"] == "dispatch":
        gh.dispatch_errors["nexkit.yml"] = GitHubError(500, "Dispatch failed")
    with (
        mock.patch.object(report_module.time, "sleep", lambda s: None),
        mock.patch.object(maintain.time, "sleep", lambda s: None),
        quiet(),
    ):
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
                "round": number,
                "trigger": "implement (autopilot)",
                "status": "success",
                "outcome": "merge_waiting" if sc["path"] == "sweep" else "merge_refused",
                "head": HEAD,
                "checks": "passed",
                "verdict": "approve",
                "url": f"{RUNS}/90",
            }
            ago = WAITED[sc["waited"]]
            if ago:
                row["waiting_since"] = autopilot.iso(datetime.now(UTC) - ago)
            gh.comment(6, render_run(row))
            if sc["path"] == "approved":
                decision = {"action": "maintain", "task": "approved", "pr": 6, "issue": 5}
                decision.update(actor="olivia", commit=HEAD)
                res["approved"] = attempt(
                    maintain_run, gh, decision, cfg, workflow="nexkit.yml", ref="main"
                )
        # An error belongs to the command's own try: the sweeps after it update as usual.
        if sc["error"] == "update":
            failing.stop()
        gh.dispatch_errors.clear()
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
