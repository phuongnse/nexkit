"""#344: required checks that fail at the merge, at the round's own merge after an implement
round, an automatic or a person's fix round, or while the merge waits for them (a sweep) ×
rounds used (no state, 0-3) × `fix.max_auto_rounds` 0-2 × the base failing the same way (no,
yes, an error) × autopilot × a hold label × `conflicts.auto_resolve` × the refusal naming the
check or not. Records `report`, GitHub after it, the preview, a sweep and `nexkit status`."""

import itertools
import json
from datetime import UTC, datetime
from unittest import mock

from common import HEAD, TOK, WAIT, WORKFLOW, Artifacts, after, attempt, quiet, seed, snapshot

from nexkit import autopilot, progress
from nexkit import report as report_module
from nexkit.github import GitHubError
from nexkit.report import report
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state
from tests.support import FakeGitHub, make_config

REVIEW = {
    "verdict": "approve",
    "summary": "S.",
    "findings": [],
    "criteria": [{"criterion": "x", "status": "met", "test": "t", "evidence": "e"}],
}


def scenarios():
    dims = dict(
        path=["first_implement", "first_fix_auto", "first_fix_person", "waiting"],
        fixes=[None, 0, 1, 2, 3],
        rounds=[0, 1, 2],
        base=["no", "yes", "error"],
        ap=[True, False],
        hold=[False, True],
        resolve=[False, True],
        named=[True, False],
    )
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))


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
    if sc["fixes"] is not None:
        gh.comment(6, render_state({**empty_state(5), "auto_fixes": sc["fixes"]}))
    gh.required = {"e2e"}
    failed = {"id": 77, "name": "e2e", "conclusion": "failure", "html_url": "https://job/77"}
    gh.commit_checks[HEAD] = [failed]
    gh.logs[77] = b"##[group]Run make e2e\n##[error]Process completed with exit code 1.\n"
    text = 'Required status check "e2e" is failing.' if sc["named"] else "Merge blocked."
    gh.merge_error = GitHubError(405, text)
    cfg = make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        merge={"auto": True},
        fix={"max_auto_rounds": sc["rounds"]},
        conflicts={"auto_resolve": sc["resolve"]},
        notify=["olivia"],
    )

    def ci_wait(gh_, cfg_, decision_, checks_, branch_):
        failing = [c for c in checks_ or [] if not c.get("passed")]
        if not failing or not autopilot.takes(cfg_):
            return None
        if sc["base"] == "error":
            raise GitHubError(502, "Bad gateway")
        return dict(WAIT) if sc["base"] == "yes" else None

    res = {}
    art = Artifacts()
    with (
        mock.patch.object(autopilot, "ci_wait", ci_wait),
        mock.patch.object(autopilot, "ci_wait_over", lambda g, w: None),
        mock.patch.object(report_module.time, "sleep", lambda s: None),
        quiet(),
    ):
        if sc["path"] == "waiting":
            row = {
                "round": 1,
                "trigger": "implement (autopilot)",
                "status": "success",
                "outcome": "merge_waiting",
                "head": HEAD,
                "url": f"{runs}/3",
                "waiting_since": autopilot.iso(datetime.now(UTC)),
            }
            gh.comment(6, render_run(row))
            seed(gh, cfg)
        else:
            kind = sc["path"].split("_")[1]
            auto = sc["path"].endswith("auto")
            art.agent({"status": "done", "tokens": TOK})
            art.checks(
                [{"name": "test", "run": "make test", "exit_code": 0, "passed": True, "output": ""}]
            )
            art.review({"status": "done", "tokens": TOK, "output": REVIEW})
            fix = kind == "fix"
            d = {
                "action": kind,
                "issue": 5,
                "pr": 6 if fix else None,
                "target": 6 if fix else 5,
                "branch": "nexkit/issue-5",
                "base": "main",
                "head": HEAD if fix else None,
                "note": "",
                "auto": auto,
                "run_id": 4242,
            }
            if auto:
                d["autopilot"] = "true"
            pub = {"published": True, "pr": 6, "head": HEAD}
            needs = {"publish": {"outputs": {"result": json.dumps(pub)}}}

            seed(gh, cfg)

            def first_round():
                progress.start(gh, d)
                return report(
                    gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
                )

            res["report"] = attempt(first_round)
            res["after_report"] = snapshot(gh)
        after(gh, cfg, res)
    art.close()
    return res
