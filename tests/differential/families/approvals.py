"""#569: a writer's approval of a NexKit pull request (`maintain.merge_approved`, the `approved`
task, which the approval's `step` runs in the issue's queue) × the latest round's outcome (a merge
GitHub refused, a ready round, a ready round with a checklist item a person decides, a pull
request reopened after its round found it closed, and one that needs a person) × `merge.auto` ×
the approved commit (the head, none, another) × a hold label on the issue × an implement round
that still runs × a head that moved since the round. Records the command, GitHub after it, the
preview, a sweep and `nexkit status`. NexKit 6 has no run of the approval that only sends the
`step`, so only the merge is played on both trees."""

import itertools
from unittest import mock

from common import HEAD, after, attempt, planned_issue, quiet, seed

from nexkit import report as report_module
from nexkit.maintain import maintain
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config

NEW = "d" * 40
OTHER = "f" * 40
RUNNING = "https://github.com/acme/app/actions/runs/50"
OUTCOMES = {
    "merge_refused": {"outcome": "merge_refused"},
    "ready": {"outcome": "ready"},
    "decided": {"outcome": "ready", "decide": ["Rule 1"]},
    "pull_closed": {"outcome": "pull_closed"},
    "needs_human": {"outcome": "needs_human"},
}


def scenarios():
    dims = dict(
        outcome=list(OUTCOMES),
        auto=[True, False],
        commit=["head", "none", "other"],
        hold=[False, True],
        implementing=[False, True],
        moved=[False, True],
    )
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))


def run(sc):
    gh = FakeGitHub()
    planned_issue(gh, labels=["on-hold"] if sc["hold"] else ())
    gh.add_pull(6, 5, head_sha=NEW if sc["moved"] else HEAD)
    gh.pulls[6]["mergeable_state"] = "blocked"
    gh.required = set()
    if sc["implementing"]:
        go = {"url": RUNNING, "command": "go", "status": "running"}
        gh.comment(5, render_run(go, "Running."))
        gh.runs[50] = {"status": "in_progress", "run_attempt": 1}
    cfg = make_config(
        autopilot={"enabled": True, "labels": {"on-hold": {"hold": True}}},
        merge={"auto": sc["auto"]},
        notify=["olivia"],
        review={"checklist": [{"text": "Rule 1", "person": True}]},
    )
    row = {
        "round": 1,
        "trigger": "implement (autopilot)",
        "status": "failure" if sc["outcome"] == "needs_human" else "success",
        "head": HEAD,
        "checks": "passed",
        "verdict": "approve",
        "url": "https://github.com/acme/app/actions/runs/3",
        **OUTCOMES[sc["outcome"]],
    }
    gh.comment(6, render_run(row, "Done."))
    seed(gh, cfg)
    decision = {"action": "maintain", "task": "approved", "pr": 6, "issue": 5, "actor": "olivia"}
    decision["commit"] = {"head": HEAD, "none": None, "other": OTHER}[sc["commit"]]
    res = {}
    with quiet(), mock.patch.object(report_module.time, "sleep", lambda s: None):
        res["approved"] = attempt(maintain, gh, decision, cfg, workflow="nexkit.yml", ref="main")
        after(gh, cfg, res)
    return res
