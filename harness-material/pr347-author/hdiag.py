"""Differential harness: chains of 2-4 rounds on one pull request, each started through
`route` as NexKit would dispatch it (an automatic fix round, a person's, a conflict round, a
resumed or retried automatic round, a review), then its checks and review and `report`. The
checks fail with the same or another signature, or pass; the review requests changes,
approves, fails or pauses; a fix round may change nothing. Rounds used and
`fix.max_auto_rounds`, a hold label, a failure the base branch has, autopilot and
`merge.auto` vary. Records each `route` decision (with `diagnose`), each `report`, then a
last automatic fix round's `route`, the preview, a sweep, GitHub and `nexkit status`.

usage: python3 hdiag.py ROOT OUT [shard n]
"""
import contextlib, io, json, os, random, sys, tempfile
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
from nexkit.route import route  # noqa: E402
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state  # noqa: E402
from tests.support import FakeGitHub, make_config  # noqa: E402

WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
TOK = {"input": 3, "cached": 1, "output": 2}
WAIT = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}


def chk(name, passed, line="FAIL"):
    return {"name": name, "run": f"make {name}", "exit_code": 0 if passed else 1,
            "passed": passed, "output": "" if passed else f"ok\n{line}\n"}


CHECKS = {
    "pass": [chk("test", True), chk("lint", True)],
    "A": [chk("test", False, "FAIL A"), chk("lint", True)],
    "B": [chk("test", False, "FAIL B"), chk("lint", True)],
    "AB": [chk("test", False, "FAIL A"), chk("lint", False, "FAIL B")],
    "LA": [chk("test", True), chk("lint", False, "FAIL A")],
    "AA": [chk("test", False, "FAIL A"), chk("lint", False, "FAIL A")],
    "none": None,
    # Many checks: more than 30 results, and more than ten that repeat.
    "M_all": [chk(f"c{n:02}", False, f"FAIL {n}x") for n in range(40)],
    "M_some": [chk(f"c{n:02}", n not in (31, 32, 33, 34, 35), f"FAIL {n}x") for n in range(40)],
    "M_many": [chk(f"c{n:02}", not 5 <= n < 22, f"FAIL {n}x") for n in range(40)],
    "M_other": [chk(f"c{n:02}", n < 20, f"FAIL {n}y") for n in range(40)],
}
PREV = {
    "none": None,
    "pass": [{"name": "test", "passed": True}],
    "A": [{"name": "test", "passed": False, "signature": "FAIL A"}],
    "AB": [{"name": "test", "passed": False, "signature": "FAIL A"},
           {"name": "lint", "passed": False, "signature": "FAIL B"}],
    "LA": [{"name": "lint", "passed": False, "signature": "FAIL A"}],
    "M_all": "M_all",
}


def crit(status):
    return {"criterion": "add works", "status": status, "test": "t", "evidence": "e"}


REVIEWS = {
    "approve": {"status": "done", "tokens": TOK, "output": {
        "verdict": "approve", "summary": "S.", "criteria": [crit("met")], "findings": []}},
    "rc": {"status": "done", "tokens": TOK, "output": {
        "verdict": "request_changes", "summary": "S.", "criteria": [crit("unmet")],
        "findings": [{"severity": "blocking", "file": "a.py", "line": 1, "body": "Breaks."}]}},
    "failed": {"status": "error", "error": "Review crashed", "tokens": TOK},
    "paused": {"status": "paused", "error": "limit", "limit": "limit · resets 4:40am (UTC)",
               "resume_at": "2099-10-11T04:40:00Z", "reset_known": True},
}
KINDS = {
    "auto": ("fix", "true"),
    "person": ("fix", "false"),
    "conflicts": ("fix", "conflicts"),
    "resumed": ("fix", "true,resume"),
    "retry": ("fix", "true,retry"),
    "review": ("review", "true"),
    "review_person": ("review", "false"),
}


def scenarios():
    rng = random.Random(312)
    for index in range(int(os.environ.get("COUNT", "40000"))):
        many = os.environ.get("MANY") == "1"
        steps = []
        for _ in range(rng.choice([2, 2, 3, 3, 4])):
            kind = rng.choice(list(KINDS) + ["auto", "auto", "auto"])
            steps.append({
                "kind": kind,
                "checks": rng.choice(["pass", "A", "B", "AB", "LA", "AA", "A", "AB", "none"]
                                     + (["M_all", "M_some", "M_many", "M_other"] * 3 if many else [])),
                "review": rng.choice(["rc", "rc", "approve", "failed", "paused"]),
                "recheck": KINDS[kind][0] == "fix" and rng.random() < 0.15,
            })
        yield {
            "prev": rng.choice(list(PREV) + (["M_all"] * 3 if many else [])) if many else rng.choice([k for k in PREV if k != "M_all"]),
            "prev_repeated": rng.choice([None, ["test"], ["lint", "test"]]),
            "fixes": rng.choice([0, 1, 2]),
            "rounds": rng.choice([0, 1, 2, 3]),
            "hold": rng.choice([False, False, True]),
            "base": rng.choice(["no", "no", "yes", "error"]),
            "ap": rng.choice([True, True, False]),
            "merge": rng.choice([False, True]),
            "steps": steps,
        }


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1, labels=["on-hold"] if sc["hold"] else ())
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/1",
                              "command": "plan", "status": "success", "outcome": "planned"}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/2",
                              "command": "go", "status": "success", "outcome": "pr_opened"}, "D."))
    head = "a" * 40
    gh.add_pull(6, 5, head_sha=head)
    state = {**empty_state(5), "auto_fixes": sc["fixes"]}
    if PREV[sc["prev"]] is not None or sc["prev_repeated"]:
        prev = PREV[sc["prev"]]
        if prev == "M_all":
            from nexkit.checks import recorded
            prev = recorded(CHECKS["M_all"])
        state["feedback"] = {"head": head, "checks": prev, "review": None}
        if sc["prev_repeated"]:
            state["feedback"]["repeated"] = sc["prev_repeated"]
    gh.comment(6, render_state(state))
    gh.round_comment(6, 1, head, "failed", outcome="auto_fix", trigger="fix (auto)",
                     action="fix", kinds=["auto"])
    cfg = make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        merge={"auto": sc["merge"]},
        fix={"max_auto_rounds": sc["rounds"]},
        conflicts={"auto_resolve": True},
        notify=["olivia"],
    )

    def fake_ci_wait(gh_, cfg_, decision_, checks_, branch_):
        failing = [c for c in checks_ or [] if not c.get("passed")]
        if not failing or not autopilot.takes(cfg_):
            return None
        if sc["base"] == "error":
            raise GitHubError(502, "Bad gateway")
        return dict(WAIT) if sc["base"] == "yes" else None

    def dispatch_route(kind):
        command, auto = KINDS[kind]
        event = {"inputs": {"command": command, "number": "6", "note": "", "auto": auto},
                 "sender": {"login": "nexkit[bot]"}}
        try:
            return route(gh, "workflow_dispatch", event, event["inputs"])
        except Exception as e:  # noqa: BLE001
            return f"EXC {type(e).__name__}: {e}"

    res = {"sc": sc, "rounds": []}
    env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}
    with mock.patch.dict(os.environ, env), mock.patch.object(autopilot, "ci_wait", fake_ci_wait), \
            mock.patch.object(autopilot, "ci_wait_over", lambda g, w: None), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        for number, step in enumerate(sc["steps"], 2):
            out = {}
            d = dispatch_route(step["kind"])
            out["route"] = walk(d)
            if not isinstance(d, dict) or d.get("action") not in ("fix", "review"):
                res["rounds"].append(out)
                continue
            d = dict(d, run_id=4000 + number)
            tmp = tempfile.TemporaryDirectory()
            art = Path(tmp.name)

            def write(folder, name, data):
                (art / folder).mkdir(exist_ok=True)
                (art / folder / name).write_text(json.dumps(data))

            needs = {}
            if d["action"] == "fix":
                agent = {"status": "done", "tokens": TOK, "output": {"cause": "Env"}}
                if step["kind"] == "conflicts":
                    agent["start_base"] = "e" * 40
                write("nexkit-agent", "result.json", agent)
                if not step["recheck"]:
                    head = f"{number:x}" * 40
                    gh.pulls[6]["head"]["sha"] = head
                pub = {"published": True, "pr": 6, "head": head}
                if step["recheck"]:
                    pub["recheck"] = True
                needs["publish"] = {"outputs": {"result": json.dumps(pub)}}
            if CHECKS[step["checks"]] is not None:
                write("nexkit-checks", "checks.json", CHECKS[step["checks"]])
            write("nexkit-review", "result.json", REVIEWS[step["review"]])
            try:
                progress.start(gh, d)
            except Exception as e:  # noqa: BLE001
                out["start"] = f"EXC {type(e).__name__}: {e}"
            try:
                out["report"] = walk(report(gh, d, cfg, needs, art, workflow_ref=WORKFLOW,
                                            default_branch="main"))
            except Exception as e:  # noqa: BLE001
                out["report"] = f"EXC {type(e).__name__}: {e}"
            tmp.cleanup()
            res["rounds"].append(out)
        res["last_auto"] = walk(dispatch_route("auto"))
        res["last_person"] = walk(dispatch_route("person"))
        res["after_rounds"] = snapshot(gh)
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
    return res


if __name__ == "__main__":
    main(scenarios, run)
