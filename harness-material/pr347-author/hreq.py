"""Differential harness: required checks that fail at the merge, at the round's first try or
while the merge waits for them (a sweep), with rounds left or used up, failing as the base
branch does or not; then the preview, a sweep and `nexkit status`.

usage: python3 hreq.py ROOT OUT [shard n]
"""
import contextlib, io, itertools, json, os, sys, tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
from common import main, snapshot, walk  # noqa: E402
from nexkit import autopilot, progress, report as report_module  # noqa: E402
from nexkit.github import GitHubError  # noqa: E402
from nexkit.report import report  # noqa: E402
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state  # noqa: E402
from tests.support import FakeGitHub, make_config  # noqa: E402

HEAD = "c" * 40
WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
TOK = {"input": 3, "cached": 1, "output": 2}
WAIT = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}
REVIEW = {"verdict": "approve", "summary": "S.", "findings": [],
          "criteria": [{"criterion": "x", "status": "met", "test": "t", "evidence": "e"}]}


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
    keys = list(dims)
    for values in itertools.product(*dims.values()):
        yield dict(zip(keys, values))


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
    if sc["fixes"] is not None:
        gh.comment(6, render_state({**empty_state(5), "auto_fixes": sc["fixes"]}))
    gh.required = {"e2e"}
    gh.commit_checks[HEAD] = [
        {"id": 77, "name": "e2e", "conclusion": "failure", "html_url": "https://job/77"}]
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

    def fake_ci_wait(gh_, cfg_, decision_, checks_, branch_):
        failing = [c for c in checks_ or [] if not c.get("passed")]
        if not failing or not autopilot.takes(cfg_):
            return None
        if sc["base"] == "error":
            raise GitHubError(502, "Bad gateway")
        return dict(WAIT) if sc["base"] == "yes" else None

    res = {"sc": sc}
    env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}
    tmp = tempfile.TemporaryDirectory()
    art = Path(tmp.name)
    with mock.patch.dict(os.environ, env), mock.patch.object(autopilot, "ci_wait", fake_ci_wait), \
            mock.patch.object(autopilot, "ci_wait_over", lambda g, w: None), \
            mock.patch.object(report_module.time, "sleep", lambda s: None), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if sc["path"] == "waiting":
            run_row = {"round": 1, "trigger": "implement (autopilot)", "status": "success",
                       "outcome": "merge_waiting", "head": HEAD,
                       "url": "https://github.com/acme/app/actions/runs/3",
                       "waiting_since": autopilot.iso(datetime.now(UTC))}
            gh.comment(6, render_run(run_row))
        else:
            kind = sc["path"].split("_")[1]
            auto = sc["path"].endswith("auto")
            for folder, data in (
                ("nexkit-agent", {"status": "done", "tokens": TOK}),
                ("nexkit-checks", [{"name": "test", "run": "make test", "exit_code": 0,
                                    "passed": True, "output": ""}]),
                ("nexkit-review", {"status": "done", "tokens": TOK, "output": REVIEW}),
            ):
                (art / folder).mkdir(exist_ok=True)
                name = "checks.json" if folder == "nexkit-checks" else "result.json"
                (art / folder / name).write_text(json.dumps(data))
            fix = kind == "fix"
            d = {"action": kind, "issue": 5, "pr": 6 if fix else None,
                 "target": 6 if fix else 5, "branch": "nexkit/issue-5", "base": "main",
                 "head": HEAD if fix else None, "note": "", "auto": auto, "run_id": 4242}
            if auto:
                d["autopilot"] = "true"
            pub = {"published": True, "pr": 6, "head": HEAD}
            needs = {"publish": {"outputs": {"result": json.dumps(pub)}}}
            try:
                progress.start(gh, d)
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
