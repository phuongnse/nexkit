"""#347: a round whose review failed, paused or is missing, then the review that runs again,
on a NexKit pull request (an implement round, an automatic or a person's fix round, a review
autopilot or a person started, a review run again after a transient error or resumed after a
usage limit) or on a person's own pull request × checks passing, failing, two failing or not
run × earlier checks failing the same way or not × approve, request changes, a checklist item
for a person, an error, a transient error, no result, a pause with a known or unknown reset ×
`retry.wait_minutes` at once, later or none × `usage_limit.resume` × autopilot ×
`merge.auto`. Records `report`, `route` of an automatic fix round, the preview, a sweep,
`nexkit status`, then the review that runs again with its own `report`, and `route` once
more."""

import itertools
import json

from common import (
    APPROVE,
    FAILED,
    PAUSED,
    PAUSED_UNKNOWN,
    REQUEST_CHANGES,
    TOK,
    TRANSIENT,
    WORKFLOW,
    Artifacts,
    after,
    attempt,
    chk,
    crit,
    done,
    quiet,
    review_output,
    snapshot,
    walk,
)

from nexkit import progress
from nexkit.report import report
from nexkit.route import route
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state
from tests.support import FakeGitHub, make_config

HEAD = "c" * 40
CHECKS = {
    "pass": [chk("test", True)],
    "A": [chk("test", False, "FAIL A")],
    "AB": [chk("test", False, "FAIL A"), chk("lint", False, "FAIL B")],
    "none": None,
}
PREV = {
    "none": None,
    "A": [{"name": "test", "passed": False, "signature": "FAIL A"}],
    "AB": [
        {"name": "test", "passed": False, "signature": "FAIL A"},
        {"name": "lint", "passed": False, "signature": "FAIL B"},
    ],
}
PITEM = {"item": "Docs", "answer": "not_met", "evidence": "no", "person": True}
REVIEWS = {
    "approve": APPROVE,
    "rc": REQUEST_CHANGES,
    "pitem": done(review_output(criteria=[crit("unverifiable")], checklist=[PITEM])),
    "failed": FAILED,
    "transient": TRANSIENT,
    "missing": None,
    "paused": PAUSED,
    "paused_unknown": PAUSED_UNKNOWN,
}
KINDS = {
    "implement": ("implement", {}),
    "fix_auto": ("fix", {"auto": True, "autopilot": "true"}),
    "fix_person": ("fix", {}),
    "review_auto": ("review", {"auto": True, "autopilot": "true"}),
    "review_person": ("review", {}),
    "review_retry": ("review", {"auto": True, "autopilot": "true", "retry": 1}),
    "review_resumed": ("review", {"auto": True, "autopilot": "true", "resumed": True}),
    "own": ("review", {}),
}


def scenarios():
    dims = dict(
        kind=list(KINDS),
        checks=list(CHECKS),
        prev=list(PREV),
        review=list(REVIEWS),
        retry=["now", "later", "none"],
        resume=[True, False],
        ap=[True, False],
        merge=[False, True],
    )
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1)
    runs = "https://github.com/acme/app/actions/runs"
    plan = {"command": "plan", "status": "success", "outcome": "planned"}
    gh.comment(5, render_run({"url": f"{runs}/1", **plan}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    go = {"command": "go", "status": "success", "outcome": "pr_opened"}
    gh.comment(5, render_run({"url": f"{runs}/2", **go}, "D."))
    own = sc["kind"] == "own"
    pr = 7 if own else 6
    if own:
        gh.add_pull(7, 0, head_sha=HEAD, branch="feature/x")
    else:
        gh.add_pull(6, 5, head_sha=HEAD)
    state = {**empty_state(pr if own else 5), "auto_fixes": 1}
    if own:
        state["person"] = True
    if PREV[sc["prev"]] is not None:
        state["feedback"] = {"head": "b" * 40, "checks": PREV[sc["prev"]], "review": None}
    gh.comment(pr, render_state(state))
    gh.round_comment(
        pr,
        1,
        "b" * 40,
        "failed",
        outcome="auto_fix",
        trigger="fix (auto)",
        action="fix",
        kinds=["auto"],
    )
    waits = {"now": [0], "later": [15], "none": []}[sc["retry"]]
    cfg = make_config(
        autopilot={"enabled": sc["ap"]},
        merge={"auto": sc["merge"]},
        retry={"wait_minutes": waits},
        usage_limit={"resume": sc["resume"]},
        notify=["olivia"],
    )
    action, extra = KINDS[sc["kind"]]
    implement = action == "implement"
    d = {
        "action": action,
        "issue": pr if own else 5,
        "pr": None if implement else pr,
        "target": 5 if implement else pr,
        "branch": "feature/x" if own else "nexkit/issue-5",
        "base": "main",
        "head": None if implement else HEAD,
        "note": "",
        "auto": False,
        "run_id": 4242,
        **extra,
    }
    if own:
        d["person"] = True
    res = {}
    with quiet():
        res["report"] = one_round(gh, cfg, d, sc["checks"], REVIEWS[sc["review"]], pr)
        res["route"] = walk(dispatch(gh, pr, "fix", "true"))
        res["after_report"] = snapshot(gh)
        after(gh, cfg, res)
        # The review that runs again, as the dispatch or the resume would start it.
        again = [x for x in gh.dispatches if x["inputs"].get("command") == "review"]
        if again or sc["review"].startswith("paused"):
            inputs = (
                again[-1]["inputs"]
                if again
                else {"command": "review", "number": str(pr), "note": "", "auto": "true,resume"}
            )
            d2 = dispatch(gh, pr, inputs["command"], inputs.get("auto"))
            res["route2"] = walk(d2)
            if isinstance(d2, dict) and d2.get("action") == "review":
                d2 = dict(d2, run_id=4343)
                res["report2"] = one_round(gh, cfg, d2, sc["checks"], REQUEST_CHANGES, pr)
                res["route3"] = walk(dispatch(gh, pr, "fix", "true"))
        res["end"] = snapshot(gh)
    return res


def dispatch(gh, pr, command, auto):
    inputs = {"command": command, "number": str(pr), "note": "", "auto": auto or ""}
    event = {"inputs": inputs, "sender": {"login": "nexkit[bot]"}}
    try:
        return route(gh, "workflow_dispatch", event, inputs)
    except Exception as exc:  # noqa: BLE001
        return f"EXC {type(exc).__name__}: {exc}"


def one_round(gh, cfg, d, checks, review, pr):
    art = Artifacts()
    needs = {}
    if d["action"] != "review":
        art.agent({"status": "done", "tokens": TOK, "output": {}})
        pub = {"published": True, "pr": pr, "head": HEAD}
        needs["publish"] = {"outputs": {"result": json.dumps(pub)}}
    art.checks(CHECKS[checks])
    if review is not None:
        art.review(review)
    else:
        needs["review"] = {"result": "failure"}
    try:
        progress.start(gh, d)
    except Exception as exc:  # noqa: BLE001
        art.close()
        return f"EXC start {type(exc).__name__}: {exc}"
    found = attempt(
        report, gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
    )
    art.close()
    return found
