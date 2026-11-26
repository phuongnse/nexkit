"""#423: a merge that GitHub refused for a conflict, or found conflicting at the end of the
refusal: the round's own merge after an implement round, a sweep over a merge that waits
(`merge_waiting`), or a writer's approval (`maintain.merge_approved`) × `conflicts.auto_resolve`
× earlier conflict rounds 0-3 × `conflicts.max_rounds` 1 or 3 × `mergeable` false or unknown ×
GitHub's `mergeable_state` (`dirty`, `blocked`) × the latest round (ended, tried on this base
commit, or a review that runs) × autopilot. Records the command, GitHub after it, the preview,
two sweeps and `nexkit status`."""

import itertools
import json
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
        mergeable=[False, None],
        state=["dirty", "blocked"],
        latest=["ended", "tried_here", "running"],
        ap=[True, False],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        # The round's own merge is the latest round itself.
        if sc["path"] == "first" and sc["latest"] == "running":
            continue
        yield sc


def run(sc):
    gh = FakeGitHub()
    planned_issue(gh)
    gh.branches["main"] = BASE
    gh.add_pull(6, 5, head_sha=HEAD)
    gh.pulls[6]["mergeable"] = sc["mergeable"]
    gh.pulls[6]["mergeable_state"] = sc["state"]
    gh.required = set()
    gh.merge_error = GitHubError(405, "Pull Request is not mergeable")
    cfg = make_config(
        autopilot={"enabled": sc["ap"]},
        merge={"auto": True},
        conflicts={"auto_resolve": sc["resolve"], "max_rounds": sc["max_rounds"]},
        notify=["olivia"],
    )
    for n in range(sc["earlier"]):
        fields = {
            "conflicts": True,
            "base_sha": "d" * 40,
            "trigger": "fix (auto, conflicts)",
            "action": "fix",
            "kinds": ["auto", "conflicts"],
            "outcome": "needs_human",
        }
        gh.round_comment(6, n + 1, "b" * 40, "failed", **fields)
    number = sc["earlier"] + 1
    tried = {"conflicts_started": BASE} if sc["latest"] == "tried_here" else {}
    res = {}
    art = Artifacts()
    with (
        mock.patch.object(report_module.time, "sleep", lambda s: None),
        mock.patch.object(maintain.time, "sleep", lambda s: None),
        quiet(),
    ):
        if sc["path"] == "first":
            gh.round_comment(
                6, number, "b" * 40, "failed", trigger="fix (auto)", outcome="needs_human", **tried
            )
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
                **tried,
            }
            gh.comment(6, render_run(row))
            if sc["latest"] == "running":
                running = {
                    "round": number + 1,
                    "trigger": "review",
                    "action": "review",
                    "status": "running",
                    "head": HEAD,
                    "url": f"{RUNS}/99",
                }
                gh.comment(6, render_run(running, "R."))
            if sc["path"] == "approved":
                decision = {"action": "maintain", "task": "approved", "pr": 6, "issue": 5}
                decision.update(actor="olivia", commit=HEAD)
                res["approved"] = attempt(
                    maintain_run, gh, decision, cfg, workflow="nexkit.yml", ref="main"
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
