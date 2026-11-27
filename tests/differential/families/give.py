"""#344: rounds that stop, and which of them count. (a) `report` after a fix round of a pull
request that stopped: an agent NexKit may not run, a change already on the base branch
(closed or not), an agent error, a blocked agent, a branch already up to date, a publish that
failed × automatic or a person's × a conflict round × retried or resumed × rounds used. (b)
The base branch moves under a round's checks (`maintain.resolve_conflicts`): a conflict, an
up-to-date rule, an update that fails, for rounds of seven kinds × retry, resumed or conflict
flags × rounds used × `conflicts.max_rounds`. Each with the preview, a sweep and
`nexkit status`."""

import itertools
import json
from unittest import mock

from common import HEAD, TOK, WORKFLOW, Artifacts, after, attempt, quiet, seed, snapshot

from nexkit import maintain, progress
from nexkit.github import GitHubError
from nexkit.report import report
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state
from tests.support import FakeGitHub, make_config

AGENTS = {
    "untrusted": {"status": "untrusted", "error": "An untrusted person edited it."},
    "on_base": {"status": "already_on_base", "on_base": "e" * 40, "merge_base": "f" * 40},
    "on_base_failed": {"status": "already_on_base", "on_base": "e" * 40, "merge_base": "f" * 40},
    "error": {"status": "error", "error": "Claude stopped (error_max_turns)."},
    "blocked": {"status": "blocked", "error": "Stuck", "output": {"status": "blocked"}},
    "up_to_date": {"status": "up_to_date"},
    "publish_failed": {"status": "done", "tokens": TOK},
}


def scenarios():
    dims = dict(
        part=["report"],
        agent=list(AGENTS),
        auto=[True, False],
        conflicts=[False, True],
        again=["none", "retry", "resumed"],
        fixes=[None, 0, 1, 2],
        ap=[True, False],
    )
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))
    dims = dict(
        part=["superseded"],
        why=["conflicting", "behind", "behind_update_fails"],
        trigger=[
            "fix (auto)",
            "fix",
            "fix (auto, conflicts)",
            "implement",
            "review (auto)",
            "fix (auto, retry)",
            "fix (auto, resume)",
        ],
        flags=["none", "retry", "resumed", "conflicts"],
        fixes=[None, 0, 1, 2],
        max_rounds=[1, 3],
        ap=[True, False],
    )
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))


def issue_with_pull(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1)
    runs = "https://github.com/acme/app/actions/runs"
    plan = {"command": "plan", "status": "success", "outcome": "planned"}
    gh.comment(5, render_run({"url": f"{runs}/1", **plan}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    go = {"command": "go", "status": "success", "outcome": "pr_opened"}
    gh.comment(5, render_run({"url": f"{runs}/2", **go}, "D."))
    gh.add_pull(6, 5, head_sha=HEAD)
    if sc["fixes"] is not None:
        gh.comment(6, render_state({**empty_state(5), "auto_fixes": sc["fixes"]}))
    return gh


def run_report(sc, gh, cfg, res):
    art = Artifacts()
    if sc["agent"] == "on_base_failed":

        def refuse(number):
            raise GitHubError(502, "Bad gateway")

        gh.close_pull = refuse
    art.agent(AGENTS[sc["agent"]])
    d = {
        "action": "fix",
        "issue": 5,
        "pr": 6,
        "target": 6,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": HEAD,
        "note": "",
        "auto": sc["auto"],
        "run_id": 4242,
    }
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
        failed = {"published": False, "error": "push refused"}
        needs["publish"] = {"outputs": {"result": json.dumps(failed)}}

    def stopped():
        progress.start(gh, d)
        return report(gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main")

    seed(gh, cfg)
    res["report"] = attempt(stopped)
    art.close()


def run_superseded(sc, gh, cfg, res):
    checked = "d" * 40
    gh.pulls[6]["head"]["sha"] = checked
    first = {"round": 1, "trigger": "implement", "status": "success", "outcome": "auto_fix"}
    gh.comment(6, render_run({**first, "url": "https://x/runs/1", "head": "a" * 40}))
    row = {
        "round": 2,
        "trigger": sc["trigger"],
        "status": "running",
        "url": "https://github.com/acme/app/actions/runs/50",
        "head": "a" * 40,
    }
    row["action"] = sc["trigger"].split(" ")[0]
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

        def fail(number, sha):
            raise GitHubError(422, "Update refused")

        gh.update_branch = fail
    seed(gh, cfg)
    res["lines0"] = attempt(
        maintain.resolve_conflicts, gh, "main", "nexkit.yml", "main", sc["max_rounds"], delay=0
    )


def run(sc):
    gh = issue_with_pull(sc)
    cfg = make_config(
        autopilot={"enabled": sc["ap"]}, notify=["olivia"], conflicts={"auto_resolve": True}
    )
    res = {}
    with mock.patch.object(maintain.time, "sleep", lambda s: None), quiet():
        if sc["part"] == "report":
            run_report(sc, gh, cfg, res)
        else:
            run_superseded(sc, gh, cfg, res)
        res["after"] = snapshot(gh)
        after(gh, cfg, res)
    return res
