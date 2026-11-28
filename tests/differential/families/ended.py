"""#393: what follows a merge's observations and a parent's end: the round's own merge after an
implement round, a sweep over a merge that waits (`merge_waiting`), or a writer's approval
(`maintain.merge_approved`) × the pull request at the merge (open, merged by a person, closed
unmerged) × whether its base branch already holds the change × a head pushed while the merge
waited (the sweep only) × the parent of the issue (none, or a sibling part that is open,
completed or not planned) × `close_parent_issues`. Records the command, GitHub after it, the
preview, two sweeps and `nexkit status`."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from unittest import mock

from common import (
    HEAD,
    IMPLEMENT_OUTPUT,
    TOK,
    WORKFLOW,
    Artifacts,
    attempt,
    planned_issue,
    quiet,
    seed,
    snapshot,
    sweep,
)

from nexkit import autopilot, progress
from nexkit import report as report_module
from nexkit.maintain import maintain
from nexkit.report import report
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config

NEW = "d" * 40
REVIEW = {
    "verdict": "approve",
    "summary": "S.",
    "findings": [],
    "criteria": [{"criterion": "x", "status": "met", "test": "t", "evidence": "e"}],
}


def scenarios():
    dims = dict(
        path=["first", "sweep", "approved"],
        pull=["open", "merged", "closed"],
        base=[False, True],
        moved=[False, True],
        parent=["none", "open", "completed", "not_planned"],
        close=[False, True],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        merging = sc["pull"] == "open"
        # A pull request that ended is read before its base branch and its head, and no parent
        # follows a merge that did not happen; a head pushed meanwhile is the sweep's.
        if not merging and (sc["base"] or sc["moved"] or sc["parent"] != "none"):
            continue
        if sc["moved"] and (sc["path"] != "sweep" or sc["parent"] != "none"):
            continue
        # The setting reads only where a parent exists.
        if sc["parent"] == "none" and sc["close"]:
            continue
        yield sc


def run(sc):
    gh = FakeGitHub()
    planned_issue(gh)
    gh.add_pull(6, 5, head_sha=NEW if sc["moved"] else HEAD)
    gh.pulls[6]["mergeable_state"] = "clean"
    if sc["base"]:
        gh.on_base.add(HEAD)

    def ended():
        if sc["pull"] == "merged":
            # GitHub closed the issue through the pull request's `Closes` line.
            gh.issues[5].update(state="closed", state_reason="completed")
            gh.pulls[6].update(state="closed", merged=True, merged_by={"login": "olivia"})
        elif sc["pull"] == "closed":
            gh.pulls[6].update(state="closed", merged=False)

    # A writer's approval finds the item's state stored while the pull request was open, as the
    # item's group stored it before a person merged or closed the pull request.
    if sc["path"] != "approved":
        ended()
    if sc["parent"] != "none":
        gh.add_issue(1, title="Epic")
        gh.parents[5] = 1
        gh.add_issue(2, state="closed" if sc["parent"] != "open" else "open")
        if sc["parent"] != "open":
            gh.issues[2]["state_reason"] = sc["parent"]
        gh.parents[2] = 1
    cfg = make_config(
        autopilot={"enabled": True},
        merge={"auto": True},
        close_parent_issues=sc["close"],
        notify=["olivia"],
    )
    res = {}
    art = Artifacts()
    with mock.patch.object(report_module.time, "sleep", lambda s: None), quiet():
        if sc["path"] == "first":
            art.agent({"status": "done", "tokens": TOK, "output": IMPLEMENT_OUTPUT})
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
                seed(gh, cfg)
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
            if sc["path"] == "sweep":
                row["waiting_since"] = autopilot.iso(datetime.now(UTC) - timedelta(hours=1))
            gh.comment(6, render_run(row))
            seed(gh, cfg)
            if sc["path"] == "approved":
                ended()
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
