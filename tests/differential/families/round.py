"""#344: a round of a pull request whose last round waited (for a failure the base branch
has, or held back by a hold label) or did not: a person's `/nexkit fix` or `/nexkit review`,
the review autopilot starts after a person's push, or the automatic fix round a wait's end
started × checks passing or failing × approve or request changes × a fix round that changed
nothing × the base failing the same way (no, yes, an error) × a hold label × rounds used 0-2 ×
`fix.max_auto_rounds` 0-2 × `merge.auto` × autopilot. Records `report`, GitHub after it, the
preview, a sweep and `nexkit status`."""

import itertools
import json
from unittest import mock

from common import (
    APPROVE,
    HEAD,
    REQUEST_CHANGES,
    TOK,
    WAIT,
    WORKFLOW,
    Artifacts,
    after,
    attempt,
    quiet,
    seed,
    snapshot,
)

from nexkit import autopilot, progress
from nexkit.github import GitHubError
from nexkit.report import report
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state
from tests.support import FakeGitHub, make_config

REVIEWS = {"approve": APPROVE, "rc": REQUEST_CHANGES}


def _checks(passed):
    return [
        {
            "name": "test",
            "run": "make test",
            "exit_code": 0 if passed else 1,
            "passed": passed,
            "output": "" if passed else "FAIL",
        }
    ]


def scenarios():
    dims = dict(
        prior=["ci", "held", "none"],
        action=["fix_person", "review_person", "review_auto", "fix_auto"],
        checks=[True, False],
        review=list(REVIEWS),
        recheck=[False, True],
        base=["no", "yes", "error"],
        hold=[False, True],
        fixes=[0, 1, 2],
        rounds=[0, 1, 2],
        merge=[False, True],
        ap=[True, False],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        if sc["recheck"] and not sc["action"].startswith("fix"):
            continue
        if sc["base"] != "no" and sc["checks"]:
            continue
        yield sc


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1, labels=["on-hold"] if sc["hold"] else ())
    runs = "https://github.com/acme/app/actions/runs"
    plan = {"command": "plan", "status": "success", "outcome": "planned"}
    gh.comment(5, render_run({"url": f"{runs}/1", **plan}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    go = {"command": "go", "status": "success", "outcome": "pr_opened"}
    gh.comment(5, render_run({"url": f"{runs}/2", **go}, "D."))
    gh.add_pull(6, 5, head_sha=HEAD)
    state = {**empty_state(5), "auto_fixes": sc["fixes"]}
    if sc["prior"] == "ci":
        state["ci_wait"] = dict(WAIT)
    gh.comment(6, render_state(state))
    row = {"trigger": "fix (auto)", "action": "fix", "kinds": ["auto"], "url": f"{runs}/3"}
    if sc["prior"] == "ci":
        gh.round_comment(6, 1, "b" * 40, "failed", outcome="ci_waiting", ci_wait=dict(WAIT), **row)
    elif sc["prior"] == "held":
        gh.round_comment(6, 1, "b" * 40, "failed", outcome="held", held_back="fix", **row)
    else:
        gh.round_comment(6, 1, "b" * 40, "failed", outcome="auto_fix", **row)
    cfg = make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        merge={"auto": sc["merge"]},
        fix={"max_auto_rounds": sc["rounds"]},
        notify=["olivia"],
    )
    kind, who = sc["action"].split("_")
    auto = who == "auto"
    art = Artifacts()
    cause = {"cause": "Flaky env"} if sc["recheck"] else {}
    art.agent({"status": "done", "tokens": TOK, "output": cause})
    art.checks(_checks(sc["checks"]))
    art.review(REVIEWS[sc["review"]])
    d = {
        "action": kind,
        "issue": 5,
        "pr": 6,
        "target": 6,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": HEAD,
        "note": "",
        "auto": auto,
        "run_id": 4242,
    }
    if auto:
        d["autopilot"] = "true"
    needs = {}
    if kind == "fix":
        pub = {"published": True, "pr": 6, "head": HEAD}
        if sc["recheck"]:
            pub["recheck"] = True
        needs["publish"] = {"outputs": {"result": json.dumps(pub)}}

    def ci_wait(gh_, cfg_, decision_, checks_, branch_):
        failing = [c for c in checks_ or [] if not c.get("passed")]
        if not failing or not autopilot.takes(cfg_):
            return None
        if sc["base"] == "error":
            raise GitHubError(502, "Bad gateway")
        return dict(WAIT) if sc["base"] == "yes" else None

    seed(gh, cfg)
    res = {}
    with (
        mock.patch.object(autopilot, "ci_wait", ci_wait),
        mock.patch.object(autopilot, "ci_wait_over", lambda g, w: None),
        quiet(),
    ):
        try:
            progress.start(gh, d)
        except Exception as exc:  # noqa: BLE001
            res["start"] = f"EXC {type(exc).__name__}: {exc}"
        res["report"] = attempt(
            report, gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
        )
        res["after_report"] = snapshot(gh)
        after(gh, cfg, res)
    art.close()
    return res
