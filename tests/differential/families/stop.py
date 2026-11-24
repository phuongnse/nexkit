"""#341: a fix round or a `/nexkit go` that stopped: blocked (12 question sets, a conflict it
could not resolve, protected paths), timed out, or another error × `autopilot.answer` ×
answers given before, with or without a comment since × autopilot's record that asked for a
split × the issue's place (top, a part three levels deep, a part whose parent asked, a part
one level deep) × automatic or a person's. Records `report`, GitHub after it, the preview, a
sweep and `nexkit status`."""

import copy
import itertools

from common import HEAD, WORKFLOW, Artifacts, after, attempt, quiet, snapshot

from nexkit import autopilot, progress
from nexkit.report import report
from nexkit.state import PLAN_MARKER, answers_marker, empty_state, render_run, render_state
from tests.support import BOT_LOGIN, FakeGitHub, make_config


def q(text, opts, rec, closes=0, replans=None):
    item = {"question": text, "options": opts, "recommended": rec}
    if closes:
        item["closes"] = closes
    if replans:
        item["replans"] = replans
    return item


Q_REC = q("Which port?", ["8080", "9090"], 1)
Q_NOREC = q("Which db?", ["pg", "my"], 0)
Q_CLOSE = q("Already fixed. Close?", ["Close it", "Keep it"], 1, closes=1)
Q_CLOSE_NOT = q("Close?", ["Close it", "Keep it"], 2, closes=1)
Q_REPLAN = q("Scope?", ["Narrow", "Wide", "Split it"], 3, replans=[3])
Q_REPLAN_NOT = q("Scope2?", ["Narrow", "Wide", "Split it"], 1, replans=[3])
Q_EMPTY = q("   ", ["a", "b"], 1)

QSETS = {
    "none": [],
    "rec": [Q_REC],
    "norec": [Q_NOREC],
    "close": [Q_CLOSE],
    "close_not": [Q_CLOSE_NOT],
    "replan": [Q_REPLAN],
    "replan_not": [Q_REPLAN_NOT],
    "close_replan": [Q_CLOSE, Q_REPLAN],
    "replan_close": [Q_REPLAN, Q_CLOSE],
    "rec_norec": [Q_REC, Q_NOREC],
    "rec_close": [Q_REC, Q_CLOSE],
    "empty": [Q_EMPTY],
    "conflict": "conflict",
}


def scenarios():
    dims = dict(
        place=["pull", "go"],
        kind=["blocked", "timed_out", "error"],
        qset=list(QSETS),
        protected=[False, True],
        answer=["recommended", "never"],
        answered=["no", "yes", "yes_comment"],
        record=[False, True],
        depth=["top", "deep3", "parent_asked", "deep1"],
        auto=[True, False],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        if sc["kind"] != "blocked" and (sc["qset"] != "none" or sc["protected"]):
            continue
        yield sc


def record_comment(gh, number, rec):
    gh.comment(number, "NexKit autopilot record\n\n" + autopilot.record_marker(rec))


def build(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    depth = sc["depth"]
    if depth == "top":
        gh.add_issue(5, title="Cache", milestone=1)
    else:
        top = 5 - {"deep3": 3, "parent_asked": 1, "deep1": 1}[depth]
        for n in range(top, 6):
            body = "Please add it." if n == top else f"<!-- nexkit:part {n - 1} -->\nPart."
            user = "alice" if n == top else BOT_LOGIN
            gh.add_issue(n, title=f"Issue {n}", body=body, user=user, milestone=1)
            if n > top:
                gh.parents[n] = n - 1
        if depth == "parent_asked":
            record_comment(gh, 4, {"split_asked": True, "command": "plan", "basis": [0, 0]})
    if sc["record"]:
        cmd = "fix" if sc["place"] == "pull" else "go"
        record_comment(gh, 5, {"split_asked": True, "command": cmd, "basis": [0, 0]})
    runs = itertools.count(1)

    def run_comment(number, **fields):
        url = f"https://github.com/acme/app/actions/runs/{next(runs)}"
        return gh.comment(number, render_run({"url": url, **fields}, "Done."))

    run_comment(5, command="plan", status="success", outcome="planned")
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    target = 5
    if sc["place"] == "pull":
        run_comment(5, command="go", status="success", outcome="pr_opened")
        gh.add_pull(6, 5, head_sha=HEAD)
        gh.comment(6, render_state({**empty_state(5), "auto_fixes": 1}))
        target = 6
        run_comment(
            6,
            round=1,
            trigger="implement",
            status="failure",
            outcome="auto_fix",
            head=HEAD,
            checks="failed",
            verdict="request_changes",
        )
    if sc["answered"] != "no":
        asked = [{**Q_REC, "recommended": "8080"}]
        if sc["place"] == "pull":
            run_comment(
                6,
                round=2,
                trigger="fix",
                status="failure",
                outcome="agent_blocked",
                head=HEAD,
                questions=asked,
            )
        else:
            run_comment(5, command="go", status="failure", outcome="agent_blocked", questions=asked)
        rec = {"answers": [{"question": "Which port?", "answer": "8080"}]}
        gh.comment(target, f"NexKit autopilot answers\n\n{answers_marker(rec)}")
        if sc["answered"] == "yes_comment":
            gh.human_comment(5, "I attached the files.")
    return gh, target


def agent_result(sc):
    if sc["kind"] == "timed_out":
        return {
            "status": "error",
            "error": "Claude did not finish within 30 minutes.",
            "timed_out": True,
        }
    if sc["kind"] == "error":
        return {"status": "error", "error": "Claude stopped (error_max_turns)."}
    found = {"status": "blocked"}
    qs = QSETS[sc["qset"]]
    if qs == "conflict":
        found["questions"] = []
        found["open_conflicts"] = [{"file": "a.py", "base_change": "x", "pr_change": "y", **Q_REC}]
    else:
        found["questions"] = qs
    if sc["protected"]:
        found["protected_paths"] = [".github/workflows/x.yml"]
    return {"status": "blocked", "error": "Stuck", "output": found}


def run(sc):
    gh, target = build(sc)
    cfg = make_config(autopilot={"enabled": True, "answer": sc["answer"]}, notify=["olivia"])
    pull = sc["place"] == "pull"
    d = {
        "action": "fix" if pull else "implement",
        "command": "fix" if pull else "go",
        "issue": 5,
        "pr": 6 if pull else None,
        "target": target,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": HEAD if pull else None,
        "note": "",
        "auto": sc["auto"],
        "run_id": 4242,
    }
    if sc["auto"]:
        d["autopilot"] = "true"
    art = Artifacts()
    art.agent(agent_result(sc))
    res = {}
    with quiet():
        try:
            progress.start(gh, d)
        except Exception as exc:  # noqa: BLE001
            res["start"] = f"EXC {type(exc).__name__}: {exc}"
        res["report"] = attempt(
            report, gh, d, cfg, {}, art.path, workflow_ref=WORKFLOW, default_branch="main"
        )
        res["after_report"] = snapshot(gh)
        # The sweep runs on a copy: `nexkit status` reads GitHub as the report left it.
        swept = copy.deepcopy(gh)
        after(swept, cfg, res, status=False)
        from nexkit.status import collect

        res["status"] = attempt(collect, gh, cfg, workers=1)
    art.close()
    return res
