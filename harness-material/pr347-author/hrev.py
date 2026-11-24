"""Differential harness: a round whose review failed, paused or is missing, then retried, on a
NexKit pull request (an implement round, an automatic or a person's fix round, a review
autopilot or a person started, a review run again after a transient error or resumed after
a usage limit) or on a person's own pull request; the checks pass, fail or did not run, after
checks that failed the same way or not; `retry.wait_minutes` lets it run again at once,
later, or not; `usage_limit.resume` on or off; autopilot and `merge.auto`. Then `route` of an
automatic fix round, the preview, a sweep, `nexkit status`, and the review that runs again
with its own report, then `route` once more.

usage: python3 hrev.py ROOT OUT [shard n]
"""
import contextlib, io, itertools, json, os, sys, tempfile
from pathlib import Path
from unittest import mock

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
from common import main, snapshot, walk  # noqa: E402
from nexkit import autopilot, progress  # noqa: E402
from nexkit.report import report  # noqa: E402
from nexkit.route import route  # noqa: E402
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state  # noqa: E402
from tests.support import FakeGitHub, make_config  # noqa: E402

WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
TOK = {"input": 3, "cached": 1, "output": 2}
HEAD = "c" * 40


def chk(name, passed, line="FAIL"):
    return {"name": name, "run": f"make {name}", "exit_code": 0 if passed else 1,
            "passed": passed, "output": "" if passed else f"ok\n{line}\n"}


CHECKS = {
    "pass": [chk("test", True)],
    "A": [chk("test", False, "FAIL A")],
    "AB": [chk("test", False, "FAIL A"), chk("lint", False, "FAIL B")],
    "none": None,
}
PREV = {
    "none": None,
    "A": [{"name": "test", "passed": False, "signature": "FAIL A"}],
    "AB": [{"name": "test", "passed": False, "signature": "FAIL A"},
           {"name": "lint", "passed": False, "signature": "FAIL B"}],
}


def crit(status):
    return {"criterion": "add works", "status": status, "test": "t", "evidence": "e"}


DONE = {
    "approve": {"verdict": "approve", "summary": "S.", "criteria": [crit("met")], "findings": []},
    "rc": {"verdict": "request_changes", "summary": "S.", "criteria": [crit("unmet")],
           "findings": [{"severity": "blocking", "file": "a.py", "line": 1, "body": "Breaks."}]},
    "pitem": {"verdict": "approve", "summary": "S.", "criteria": [crit("unverifiable")],
              "findings": [], "checklist": [{"item": "Docs", "answer": "not_met",
                                             "evidence": "no", "person": True}]},
}
REVIEWS = {
    "approve": {"status": "done", "tokens": TOK, "output": DONE["approve"]},
    "rc": {"status": "done", "tokens": TOK, "output": DONE["rc"]},
    "pitem": {"status": "done", "tokens": TOK, "output": DONE["pitem"]},
    "failed": {"status": "error", "error": "Review crashed", "tokens": TOK},
    "transient": {"status": "error", "error": "API overloaded", "transient": True,
                  "tokens": TOK},
    "missing": None,
    "paused": {"status": "paused", "error": "limit", "limit": "limit · resets 4:40am (UTC)",
               "resume_at": "2099-10-11T04:40:00Z", "reset_known": True},
    "paused_unknown": {"status": "paused", "error": "limit", "limit": "limit",
                       "resume_at": "2099-10-11T04:40:00Z", "reset_known": False},
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
        yield dict(zip(dims, values))


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1)
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/1",
                              "command": "plan", "status": "success", "outcome": "planned"}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/2",
                              "command": "go", "status": "success", "outcome": "pr_opened"}, "D."))
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
    gh.round_comment(pr, 1, "b" * 40, "failed", outcome="auto_fix", trigger="fix (auto)",
                     action="fix", kinds=["auto"])
    waits = {"now": [0], "later": [15], "none": []}[sc["retry"]]
    cfg = make_config(
        autopilot={"enabled": sc["ap"]},
        merge={"auto": sc["merge"]},
        retry={"wait_minutes": waits},
        usage_limit={"resume": sc["resume"]},
        notify=["olivia"],
    )
    action, extra = KINDS[sc["kind"]]
    d = {"action": action, "issue": pr if own else 5, "pr": None if action == "implement" else pr,
         "target": 5 if action == "implement" else pr,
         "branch": "feature/x" if own else "nexkit/issue-5", "base": "main",
         "head": None if action == "implement" else HEAD, "note": "", "auto": False,
         "run_id": 4242, **extra}
    if own:
        d["person"] = True
    res = {"sc": sc}
    env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}
    with mock.patch.dict(os.environ, env), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        res["report"] = one_round(gh, cfg, d, sc["checks"], REVIEWS[sc["review"]], pr)
        res["route"] = walk(dispatch(gh, pr, "fix", "true"))
        res["after_report"] = snapshot(gh)
        try:
            _, steps = autopilot.preview(gh, cfg)
            res["step"] = walk({s["number"]: s for s in steps}.get(5))
        except Exception as e:  # noqa: BLE001
            res["step"] = f"EXC {type(e).__name__}: {e}"
        try:
            res["lines"] = walk(autopilot.sweep(gh, cfg, "nexkit.yml", "main"))
        except Exception as e:  # noqa: BLE001
            res["lines"] = f"EXC {type(e).__name__}: {e}"
        res["after_sweep"] = snapshot(gh)
        try:
            from nexkit.status import collect

            res["status"] = walk(collect(gh, cfg, workers=1))
        except Exception as e:  # noqa: BLE001
            res["status"] = f"EXC {type(e).__name__}: {e}"
        # The review that runs again, as the dispatch or the resume would start it.
        again = [x for x in gh.dispatches if x["inputs"].get("command") == "review"]
        if again or sc["review"].startswith("paused"):
            inputs = again[-1]["inputs"] if again else {
                "command": "review", "number": str(pr), "note": "", "auto": "true,resume"}
            d2 = dispatch(gh, pr, inputs["command"], inputs.get("auto"))
            res["route2"] = walk(d2)
            if isinstance(d2, dict) and d2.get("action") == "review":
                d2 = dict(d2, run_id=4343)
                res["report2"] = one_round(gh, cfg, d2, sc["checks"], REVIEWS["rc"], pr)
                res["route3"] = walk(dispatch(gh, pr, "fix", "true"))
        res["end"] = snapshot(gh)
    return res


def dispatch(gh, pr, command, auto):
    event = {"inputs": {"command": command, "number": str(pr), "note": "", "auto": auto or ""},
             "sender": {"login": "nexkit[bot]"}}
    try:
        return route(gh, "workflow_dispatch", event, event["inputs"])
    except Exception as e:  # noqa: BLE001
        return f"EXC {type(e).__name__}: {e}"


def one_round(gh, cfg, d, checks, review, pr):
    tmp = tempfile.TemporaryDirectory()
    art = Path(tmp.name)

    def write(folder, name, data):
        (art / folder).mkdir(exist_ok=True)
        (art / folder / name).write_text(json.dumps(data))

    needs = {}
    if d["action"] != "review":
        write("nexkit-agent", "result.json", {"status": "done", "tokens": TOK, "output": {}})
        pub = {"published": True, "pr": pr, "head": HEAD}
        needs["publish"] = {"outputs": {"result": json.dumps(pub)}}
    if CHECKS[checks] is not None:
        write("nexkit-checks", "checks.json", CHECKS[checks])
    if review is not None:
        write("nexkit-review", "result.json", review)
    else:
        needs["review"] = {"result": "failure"}
    try:
        progress.start(gh, d)
    except Exception as e:  # noqa: BLE001
        return f"EXC start {type(e).__name__}: {e}"
    try:
        found = walk(report(gh, d, cfg, needs, art, workflow_ref=WORKFLOW,
                            default_branch="main"))
    except Exception as e:  # noqa: BLE001
        found = f"EXC {type(e).__name__}: {e}"
    tmp.cleanup()
    return found


if __name__ == "__main__":
    main(scenarios, run)
