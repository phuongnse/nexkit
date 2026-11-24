"""#341: `report` after a round's checks and review. An implement, fix or review round,
automatic or a person's × checks passing, failing, two failing, mixed, nameless or not run ×
22 reviews (approve, request changes, findings, unmet and unverifiable criteria, checklist
items a person decides, older boolean criteria, a review that failed, is missing or paused) ×
a fix round that changed nothing × automatic fix rounds used (none recorded, 0-2) ×
`fix.max_auto_rounds` 0-2 × `merge.auto` × a hold label (or one NexKit cannot read) × the base
branch failing the same way (no, yes, an error reading it) × autopilot. Records `report`'s
result, GitHub after it and `nexkit status`."""

import itertools
import json
from unittest import mock

from common import (
    HEAD,
    TOK,
    WAIT,
    WORKFLOW,
    Artifacts,
    attempt,
    crit,
    done,
    quiet,
    review_output,
    snapshot,
)

from nexkit import autopilot
from nexkit.github import GitHubError
from nexkit.report import report
from nexkit.state import empty_state, render_state
from tests.support import FakeGitHub, make_config

BLOCK = {"severity": "blocking", "file": "a.py", "line": 3, "body": "Breaks x."}
SUGG = {"severity": "suggestion", "file": "a.py", "line": 3, "body": "Nicer y."}
PITEM = {"item": "Docs updated by owner", "answer": "not_met", "evidence": "none", "person": True}
NITEM = {"item": "Tests added", "answer": "not_met", "evidence": "missing"}
MITEM = {"item": "Tests added", "answer": "met", "evidence": "yes"}
out = review_output

REVIEWS = {
    "approve": done(out()),
    "rc": done(out("request_changes", findings=[BLOCK])),
    "rc_nofind": done(out("request_changes")),
    "approve_block": done(out(findings=[BLOCK])),
    "approve_sugg": done(out(findings=[SUGG])),
    "unmet": done(out(criteria=[crit("met", "a"), crit("unmet", "b")])),
    "unverif": done(out(criteria=[crit("met", "a"), crit("unverifiable", "b")])),
    "unmet_unverif": done(out(criteria=[crit("unmet", "a"), crit("unverifiable", "b")])),
    "pitem": done(out(checklist=[PITEM])),
    "pitem_unverif": done(out(criteria=[crit("unverifiable")], checklist=[PITEM])),
    "pitem_block": done(out(findings=[BLOCK], checklist=[PITEM])),
    "rc_pitem": done(out("request_changes", checklist=[PITEM])),
    "rc_pitem_block": done(out("request_changes", findings=[BLOCK], checklist=[PITEM])),
    "nitem": done(out(checklist=[NITEM])),
    "nitem_pitem": done(out(checklist=[NITEM, PITEM])),
    "mitem": done(out(checklist=[MITEM])),
    "bool_met": done(out(criteria=[{"criterion": "x", "met": True}])),
    "bool_unmet": done(out(criteria=[{"criterion": "x", "met": False}])),
    "no_criteria": done(out(criteria=[])),
    "failed": {"status": "error", "error": "Review crashed", "tokens": TOK},
    "missing": None,
    "paused": {
        "status": "paused",
        "error": "Claude hit the account's usage limit: limit",
        "limit": "limit · resets 4:40am (UTC)",
        "resume_at": "2099-10-11T04:40:00Z",
        "reset_known": True,
    },
}


def _chk(name, run, passed):
    return {
        "name": name,
        "run": run,
        "exit_code": 0 if passed else 1,
        "passed": passed,
        "output": "" if passed else "FAIL",
    }


CHECKS = {
    "pass": [_chk("test", "make test", True)],
    "fail": [_chk("test", "make test", False)],
    "two_fail": [_chk("test", "make test", False), _chk("lint", "make lint", False)],
    "mixed": [_chk("test", "make test", True), _chk("lint", "make lint", False)],
    "none": None,
    "noname": [_chk("", "make x", False)],
}


def scenarios():
    dims = dict(
        action=["implement", "fix_auto", "fix_person", "review_auto", "review_person"],
        checks=list(CHECKS),
        review=list(REVIEWS),
        recheck=[False, True],
        prior=[None, 0, 1, 2],
        rounds=[0, 1, 2],
        merge=[False, True],
        hold=["no", "label", "error"],
        base=["no", "yes", "error"],
        ap=[True, False],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        if sc["recheck"] and not sc["action"].startswith("fix"):
            continue
        yield sc


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1)
    if sc["hold"] == "label":
        gh.issues[5]["labels"] = [{"name": "on-hold"}]
    gh.add_pull(6, 5, head_sha=HEAD)
    if sc["prior"] is not None:
        gh.comment(6, render_state({**empty_state(5), "auto_fixes": sc["prior"]}))
    cfg = make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        merge={"auto": sc["merge"]},
        fix={"max_auto_rounds": sc["rounds"]},
        notify=["olivia"],
    )
    art = Artifacts()
    cause = {"output": {"cause": "Flaky env"}} if sc["recheck"] else {}
    art.agent({"status": "done", "tokens": TOK, **cause})
    art.checks(CHECKS[sc["checks"]])
    art.review(REVIEWS[sc["review"]])
    kind = sc["action"].split("_")[0]
    auto = sc["action"].endswith("auto")
    d = {
        "action": kind,
        "issue": 5,
        "pr": None if kind == "implement" else 6,
        "target": 5 if kind == "implement" else 6,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": None if kind == "implement" else HEAD,
        "note": "",
        "auto": auto,
    }
    if auto:
        d["autopilot"] = "true"
    needs = {}
    if kind != "review":
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

    real_held = autopilot.held

    def held(gh_, cfg_, number):
        if sc["hold"] == "error" and autopilot.takes(cfg_):
            raise GitHubError(502, "Bad gateway")
        return real_held(gh_, cfg_, number)

    res = {}
    with (
        mock.patch.object(autopilot, "ci_wait", ci_wait),
        mock.patch.object(autopilot, "held", held),
        quiet(),
    ):
        res["result"] = attempt(
            report, gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
        )
    with quiet():
        from nexkit.status import collect

        res["status"] = attempt(collect, gh, cfg, workers=1)
    res["gh"] = snapshot(gh)
    art.close()
    return res
