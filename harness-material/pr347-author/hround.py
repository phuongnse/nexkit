"""Differential harness: a round of a pull request whose last round waited (for a failure the
base branch has, or held back by a hold label) or did not: a person's `/nexkit fix` or
`/nexkit review`, the review autopilot starts after a person's push, or the automatic fix
round a wait's end started; its checks and review, then `report`, the preview, a sweep and
`nexkit status`.

usage: python3 hround.py ROOT OUT [shard n]
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
from nexkit.github import GitHubError  # noqa: E402
from nexkit.report import report  # noqa: E402
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state  # noqa: E402
from tests.support import FakeGitHub, make_config  # noqa: E402

HEAD = "c" * 40
WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
TOK = {"input": 3, "cached": 1, "output": 2}
WAIT = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}


def crit(status):
    return {"criterion": "add works", "status": status, "test": "t", "evidence": "e"}


REVIEWS = {
    "approve": {"verdict": "approve", "summary": "S.", "criteria": [crit("met")], "findings": []},
    "rc": {"verdict": "request_changes", "summary": "S.", "criteria": [crit("unmet")],
           "findings": [{"severity": "blocking", "file": "a.py", "line": 1, "body": "Breaks."}]},
}


def chk(passed):
    return [{"name": "test", "run": "make test", "exit_code": 0 if passed else 1,
             "passed": passed, "output": "" if passed else "FAIL"}]


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
    keys = list(dims)
    for values in itertools.product(*dims.values()):
        sc = dict(zip(keys, values))
        if sc["recheck"] and not sc["action"].startswith("fix"):
            continue
        if sc["base"] != "no" and sc["checks"]:
            continue
        yield sc


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1, labels=["on-hold"] if sc["hold"] else ())
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/1",
                              "command": "plan", "status": "success", "outcome": "planned"}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/2",
                              "command": "go", "status": "success", "outcome": "pr_opened"}, "D."))
    gh.add_pull(6, 5, head_sha=HEAD)
    state = {**empty_state(5), "auto_fixes": sc["fixes"]}
    if sc["prior"] == "ci":
        state["ci_wait"] = dict(WAIT)
    gh.comment(6, render_state(state))
    row = {"trigger": "fix (auto)", "action": "fix", "kinds": ["auto"],
           "url": "https://github.com/acme/app/actions/runs/3"}
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
    tmp = tempfile.TemporaryDirectory()
    art = Path(tmp.name)

    def write(folder, name, data):
        (art / folder).mkdir(exist_ok=True)
        (art / folder / name).write_text(json.dumps(data))

    kind, who = sc["action"].split("_")
    auto = who == "auto"
    out = {"cause": "Flaky env"} if sc["recheck"] else {}
    write("nexkit-agent", "result.json", {"status": "done", "tokens": TOK, "output": out})
    write("nexkit-checks", "checks.json", chk(sc["checks"]))
    write("nexkit-review", "result.json",
          {"status": "done", "tokens": TOK, "output": REVIEWS[sc["review"]]})
    d = {"action": kind, "issue": 5, "pr": 6, "target": 6, "branch": "nexkit/issue-5",
         "base": "main", "head": HEAD, "note": "", "auto": auto, "run_id": 4242}
    if auto:
        d["autopilot"] = "true"
    needs = {}
    if kind == "fix":
        pub = {"published": True, "pr": 6, "head": HEAD}
        if sc["recheck"]:
            pub["recheck"] = True
        needs["publish"] = {"outputs": {"result": json.dumps(pub)}}

    def fake_ci_wait(gh_, cfg_, decision_, checks_, branch_):
        failing = [c for c in checks_ or [] if not c.get("passed")]
        if not failing or not autopilot.takes(cfg_):
            return None
        if sc["base"] == "error":
            raise GitHubError(502, "Bad gateway")
        return dict(WAIT) if sc["base"] == "yes" else None

    res = {"sc": sc}
    env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}
    with mock.patch.dict(os.environ, env), mock.patch.object(autopilot, "ci_wait", fake_ci_wait), \
            mock.patch.object(autopilot, "ci_wait_over", lambda g, w: None), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            progress.start(gh, d)
        except Exception as e:  # noqa: BLE001
            res["start"] = f"EXC {type(e).__name__}: {e}"
        try:
            res["report"] = walk(report(gh, d, cfg, needs, art, workflow_ref=WORKFLOW,
                                        default_branch="main"))
        except Exception as e:  # noqa: BLE001
            res["report"] = f"EXC {type(e).__name__}: {e}"
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
    tmp.cleanup()
    return res


if __name__ == "__main__":
    main(scenarios, run)
