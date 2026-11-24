"""Differential harness: rounds that stop and which of them count. (a) `report` after a fix
round of a pull request that stopped: an agent NexKit may not run, a change already on the
base branch (closed or not), an agent error, a blocked agent, a branch already up to date, a
publish that failed; automatic or a person's, a conflict round, retried, resumed. (b) the
base branch moves under a round's checks (`maintain.resolve_conflicts`, `stop_superseded`):
a conflict, or rules that want it up to date, an update that fails; for rounds of each kind.
Each with the rounds used before, then the preview, a sweep and `nexkit status`.

usage: python3 hgive.py ROOT OUT [shard n]
"""
import contextlib, io, itertools, json, os, sys, tempfile
from pathlib import Path
from unittest import mock

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
from common import main, snapshot, walk  # noqa: E402
from nexkit import autopilot, maintain, progress  # noqa: E402
from nexkit.github import GitHubError  # noqa: E402
from nexkit.report import report  # noqa: E402
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state  # noqa: E402
from tests.support import FakeGitHub, make_config  # noqa: E402

HEAD = "c" * 40
WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
TOK = {"input": 3, "cached": 1, "output": 2}


def scenarios():
    dims = dict(
        part=["report"],
        agent=["untrusted", "on_base", "on_base_failed", "error", "blocked", "up_to_date",
               "publish_failed"],
        auto=[True, False],
        conflicts=[False, True],
        again=["none", "retry", "resumed"],
        fixes=[None, 0, 1, 2],
        ap=[True, False],
    )
    keys = list(dims)
    for values in itertools.product(*dims.values()):
        yield dict(zip(keys, values))
    dims = dict(
        part=["superseded"],
        why=["conflicting", "behind", "behind_update_fails"],
        trigger=["fix (auto)", "fix", "fix (auto, conflicts)", "implement", "review (auto)",
                 "fix (auto, retry)", "fix (auto, resume)"],
        flags=["none", "retry", "resumed", "conflicts"],
        fixes=[None, 0, 1, 2],
        max_rounds=[1, 3],
        ap=[True, False],
    )
    keys = list(dims)
    for values in itertools.product(*dims.values()):
        yield dict(zip(keys, values))


def issue_with_pull(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1)
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/1",
                              "command": "plan", "status": "success", "outcome": "planned"}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/2",
                              "command": "go", "status": "success", "outcome": "pr_opened"}, "D."))
    gh.add_pull(6, 5, head_sha=HEAD)
    if sc["fixes"] is not None:
        gh.comment(6, render_state({**empty_state(5), "auto_fixes": sc["fixes"]}))
    return gh


def run_report(sc, gh, cfg, res):
    tmp = tempfile.TemporaryDirectory()
    art = Path(tmp.name)
    (art / "nexkit-agent").mkdir()
    agent = {
        "untrusted": {"status": "untrusted", "error": "An untrusted person edited it."},
        "on_base": {"status": "already_on_base", "on_base": "e" * 40, "merge_base": "f" * 40},
        "on_base_failed": {"status": "already_on_base", "on_base": "e" * 40,
                           "merge_base": "f" * 40},
        "error": {"status": "error", "error": "Claude stopped (error_max_turns)."},
        "blocked": {"status": "blocked", "error": "Stuck", "output": {"status": "blocked"}},
        "up_to_date": {"status": "up_to_date"},
        "publish_failed": {"status": "done", "tokens": TOK},
    }[sc["agent"]]
    if sc["agent"] == "on_base_failed":
        def refuse(number):
            raise GitHubError(502, "Bad gateway")

        gh.close_pull = refuse
    (art / "nexkit-agent" / "result.json").write_text(json.dumps(agent))
    d = {"action": "fix", "issue": 5, "pr": 6, "target": 6, "branch": "nexkit/issue-5",
         "base": "main", "head": HEAD, "note": "", "auto": sc["auto"], "run_id": 4242}
    if sc["auto"]:
        d["autopilot"] = "true"
    if sc["conflicts"]:
        d["conflicts"] = True
    if sc["again"] == "retry":
        d["retry"] = 1
    if sc["again"] == "resumed":
        d["resumed"] = True
    needs = {}
    if sc["agent"] == "publish_failed":
        needs["publish"] = {"outputs": {"result": json.dumps({"published": False,
                                                              "error": "push refused"})}}
    try:
        progress.start(gh, d)
        res["report"] = walk(report(gh, d, cfg, needs, art, workflow_ref=WORKFLOW,
                                    default_branch="main"))
    except Exception as e:  # noqa: BLE001
        res["report"] = f"EXC {type(e).__name__}: {e}"
    tmp.cleanup()


def run_superseded(sc, gh, res):
    checked = "d" * 40
    gh.pulls[6]["head"]["sha"] = checked
    first = {"round": 1, "trigger": "implement", "status": "success", "outcome": "auto_fix"}
    gh.comment(6, render_run({**first, "url": "https://x/runs/1", "head": "a" * 40}))
    row = {"round": 2, "trigger": sc["trigger"], "status": "running",
           "url": "https://github.com/acme/app/actions/runs/50", "head": "a" * 40}
    kind = sc["trigger"].split(" ")[0]
    row["action"] = kind
    if sc["flags"] != "none":
        row[sc["flags"]] = True if sc["flags"] != "retry" else 1
    gh.comment(6, render_run(row))
    order = ["route", "agent", "publish", "shards", "verify", "combine", "review", "report"]
    jobs = [{"name": f"nexkit / {job}", "status": "completed"} for job in order[:4]]
    jobs.append({"name": "nexkit / verify (e2e)", "status": "in_progress"})
    gh.jobs[50] = jobs
    gh.runs[50] = {"status": "in_progress", "run_attempt": 1}
    gh.branches["main"] = "e" * 40
    if sc["why"] == "conflicting":
        gh.pulls[6]["mergeable"] = False
    else:
        gh.strict = True
        gh.compares[("e" * 40, checked)] = {"behind_by": 1}
    if sc["why"] == "behind_update_fails":
        gh.update_errors = {}

        def fail(number, sha):
            raise GitHubError(422, "Update refused")

        gh.update_branch = fail
    try:
        res["lines0"] = walk(maintain.resolve_conflicts(
            gh, "main", "nexkit.yml", "main", sc["max_rounds"], delay=0))
    except Exception as e:  # noqa: BLE001
        res["lines0"] = f"EXC {type(e).__name__}: {e}"


def run(sc):
    gh = issue_with_pull(sc)
    cfg = make_config(autopilot={"enabled": sc["ap"]}, notify=["olivia"],
                      conflicts={"auto_resolve": True})
    res = {"sc": sc}
    env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}
    with mock.patch.dict(os.environ, env), mock.patch.object(maintain.time, "sleep", lambda s: None), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if sc["part"] == "report":
            run_report(sc, gh, cfg, res)
        else:
            run_superseded(sc, gh, res)
        res["after"] = snapshot({**vars(gh), "update_branch": None} and gh)
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
