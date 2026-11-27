"""#530: the base branch moved under an open NexKit pull request: a push to it (the `conflicts`
task), the hourly `schedule`, NexKit's own merge of another pull request (`merge_pull`), or the
report of an implement round that left a conflict round out (`_conflicts_after`) × `mergeable`
(false, true, unknown) × the latest round of the pull request (it runs only its checks, its
agent runs, or it ended) × whether the base branch's rules want pull requests up to date × whether
the head lacks its tip × conflict rounds used (none, `conflicts.max_rounds`) × updates used (none,
`conflicts.max_rounds`) × `conflicts.max_rounds` 1 or 3 × a hold label on the issue. Records the
command, GitHub after it, the preview, two sweeps and `nexkit status`. A scenario drops the
dimensions that its path and `mergeable` cannot reach."""

import itertools
import json
from unittest import mock

from common import (
    HEAD,
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

from nexkit import autopilot, maintain, progress
from nexkit import report as report_module
from nexkit.maintain import maintain as maintain_run
from nexkit.report import merge_pull, report
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config

BASE = "e" * 40
OTHER = "9" * 40
RUNS = "https://github.com/acme/app/actions/runs"
ROUND_RUN = 50
REVIEW = {
    "verdict": "approve",
    "summary": "S.",
    "findings": [],
    "criteria": [{"criterion": "x", "status": "met", "test": "t", "evidence": "e"}],
}


def scenarios():
    dims = dict(
        path=["push", "schedule", "merge", "implement"],
        mergeable=[False, True, None],
        latest=["checking", "agent", "ended"],
        strict=[False, True],
        behind=[False, True],
        used=["none", "max"],
        updates=["none", "max"],
        max_rounds=[1, 3],
        hold=[False, True],
    )
    seen = set()
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        # An implement round's report and a merge look at no round of the pull request's own.
        if sc["path"] in ("merge", "implement"):
            sc["latest"] = "ended"
        # Only a pull request that conflicts has conflict rounds to count, and only one that
        # does not conflict and is behind has updates.
        if sc["mergeable"] is True:
            sc["used"] = "none"
        if sc["mergeable"] is False or sc["path"] == "implement":
            sc["updates"] = "none"
            sc["strict"] = sc["behind"] = False
        if not sc["strict"]:
            sc["behind"] = False
        # Without a branch behind its base, the updates used are no matter.
        if not sc["behind"]:
            sc["updates"] = "none"
        # A hold label keeps the rounds back whatever else holds: the rounds used matter little.
        if (
            sc["hold"]
            and "max" in (sc["used"], sc["updates"])
            or sc["hold"]
            and sc["max_rounds"] != 3
        ):
            continue
        key = json.dumps(sc, sort_keys=True)
        if key not in seen:
            seen.add(key)
            yield sc


def in_job(gh, run_id, stage):
    """Run `run_id` is in `stage`: of two check jobs one ended and one runs, or the agent runs."""
    order = ["route", "agent", "publish", "shards", "verify", "combine", "review", "report"]
    jobs = [{"name": f"nexkit / {job}", "status": "completed"} for job in order]
    jobs = jobs[: order.index(stage)]
    if stage == "verify":
        jobs.append({"name": "nexkit / verify (unit)", "status": "completed"})
        jobs.append({"name": "nexkit / verify (e2e)", "status": "in_progress"})
    else:
        jobs.append({"name": f"nexkit / {stage}", "status": "in_progress"})
    gh.jobs[run_id] = jobs
    gh.runs[run_id] = {"status": "in_progress", "run_attempt": 1}


def run(sc):
    gh = FakeGitHub()
    labels = ["on-hold"] if sc["hold"] else ()
    planned_issue(gh, labels=labels)
    gh.branches["main"] = BASE
    gh.add_pull(6, 5, head_sha=HEAD)
    gh.pulls[6]["mergeable"] = sc["mergeable"]
    gh.strict = sc["strict"]
    if sc["behind"]:
        gh.compares[(BASE, HEAD)] = {"behind_by": 1}
    cfg = make_config(
        autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}},
        merge={"auto": False},
        conflicts={"auto_resolve": True, "max_rounds": sc["max_rounds"]},
        notify=["olivia"],
    )
    taken = sc["max_rounds"] if sc["used"] == "max" else 0
    number = 1
    for _ in range(taken):
        fields = {
            "conflicts": True,
            "base_sha": "d" * 40,
            "trigger": "fix (auto, conflicts)",
            "action": "fix",
            "kinds": ["auto", "conflicts"],
            "outcome": "needs_human",
        }
        gh.round_comment(6, number, "b" * 40, "failed", **fields)
        number += 1
    for _ in range(sc["max_rounds"] if sc["updates"] == "max" else 0):
        fields = {"updated_branch": True, "trigger": "review", "action": "review"}
        gh.round_comment(6, number, "b" * 40, "passed", **fields)
        number += 1
    res = {}
    art = Artifacts()
    with (
        mock.patch.object(report_module.time, "sleep", lambda s: None),
        mock.patch.object(maintain.time, "sleep", lambda s: None),
        quiet(),
    ):
        if sc["path"] == "implement":
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

            seed(gh, cfg)

            def first_round():
                progress.start(gh, d)
                return report(
                    gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
                )

            res["report"] = attempt(first_round)
        else:
            row = {
                "round": number,
                "trigger": "fix (auto)" if sc["latest"] != "ended" else "implement (autopilot)",
                "action": "fix" if sc["latest"] != "ended" else "implement",
                "head": HEAD,
                "url": f"{RUNS}/{ROUND_RUN}",
                "checks": "not run" if sc["latest"] != "ended" else "passed",
                "verdict": "not run" if sc["latest"] != "ended" else "approve",
            }
            if sc["latest"] == "ended":
                row.update(status="success", outcome="ready")
            else:
                row.update(status="running")
                in_job(gh, ROUND_RUN, "verify" if sc["latest"] == "checking" else "agent")
            gh.comment(6, render_run(row))
            # The base branch moved since the round published its head.
            gh.branches["main"] = BASE
            if sc["path"] == "push":
                seed(gh, cfg)
                decision = {"action": "maintain", "task": "conflicts", "base": "main"}
                res["push"] = attempt(
                    maintain_run, gh, decision, cfg, workflow="nexkit.yml", ref="main"
                )
            elif sc["path"] == "schedule":
                seed(gh, cfg)
                decision = {"action": "maintain", "task": "schedule"}
                res["schedule"] = attempt(
                    maintain_run, gh, decision, cfg, workflow="nexkit.yml", ref="main"
                )
            else:
                gh.add_issue(4, milestone=1)
                gh.add_pull(7, 4, head_sha=OTHER, branch="nexkit/issue-4")
                gh.comment(7, render_run({"round": 1, "trigger": "implement", "status": "running"}))
                target = {"pr": 7, "head": OTHER, "issue": 4, "base": "main"}
                seed(gh, cfg)
                res["merge"] = attempt(merge_pull, gh, cfg, target, {}, "", WORKFLOW, "main")
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
