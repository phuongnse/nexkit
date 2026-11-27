"""#538: a writer's vouch after an untrusted edit stopped a round or a merge, on an issue whose
author cannot write: where it stopped (the issue's `/nexkit go`, a round of its pull request,
or the merge of its pull request) × the vouch (none at all, one before the stop only, a
reader's after it, a writer's after it). A pull request's round varies the automatic fix rounds
left or used up in its state comment, and whether the stopped round was automatic or a person's.
Records the preview, one sweep with GitHub after it, and `nexkit status`.

Where no automatic fix round is left, NexKit 6 started a counted round past
`fix.max_auto_rounds` after a writer's vouch, and the engine hands it to a person (`known.py`)."""

import itertools

from common import after, quiet, seed

from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state
from tests.support import FakeGitHub, make_config

EARLIER, EDIT, LATER = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"
HEAD = "c" * 40
MOST = 2


def scenarios():
    dims = dict(
        where=["issue", "pull_round", "pull_merge"], vouch=["none", "before", "reader", "writer"]
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        if sc["where"] != "pull_round":
            yield {**sc, "rounds": "left", "by": "auto"}
            continue
        yield {**sc, "rounds": "left", "by": "auto"}
        yield {**sc, "rounds": "left", "by": "person"}
        yield {**sc, "rounds": "used_up", "by": "person"}


def build(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, title="Cache", user="bob", milestone=1)
    if sc["vouch"] != "none":
        gh.human_comment(5, "/nexkit trust", "olivia", "OWNER")["created_at"] = EARLIER
    url = iter(f"https://github.com/acme/app/actions/runs/{n}" for n in range(1, 9))
    gh.comment(5, render_run({"url": next(url), "command": "plan", "status": "success"}, "Done."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    gh.edits[5] = [("bob", EDIT)]
    attention = "Read the issue, and comment `/nexkit trust` on it if you trust it"
    if sc["where"] == "issue":
        go = {"command": "go", "status": "failure", "outcome": "agent_untrusted", "url": next(url)}
        gh.comment(5, render_run({**go, "attention": attention}, "Stopped."))
    else:
        done = {"command": "go", "status": "success", "outcome": "pr_opened", "url": next(url)}
        gh.comment(5, render_run(done, "Done."))
        gh.add_pull(9, 5, head_sha=HEAD)
        fixes = MOST if sc["rounds"] == "used_up" else 1
        gh.comment(9, render_state({**empty_state(5), "auto_fixes": fixes}))
        merge = sc["where"] == "pull_merge"
        trigger = "implement" if merge else "fix (auto)" if sc["by"] == "auto" else "fix"
        run = {
            "round": 2,
            "trigger": trigger,
            "status": "success" if merge else "failure",
            "url": next(url),
            "head": HEAD,
            "outcome": "merge_untrusted" if merge else "agent_untrusted",
            "attention": attention,
        }
        if merge:
            run.update(checks="passed", verdict="approve")
        gh.comment(9, render_run(run, "Stopped."))
    if sc["vouch"] == "reader":
        gh.human_comment(5, "/nexkit trust", "rita", "NONE")["created_at"] = LATER
    elif sc["vouch"] == "writer":
        gh.human_comment(5, "/nexkit trust", "olivia", "OWNER")["created_at"] = LATER
    return gh


def run(sc):
    gh = build(sc)
    cfg = make_config(autopilot={"enabled": True}, fix={"max_auto_rounds": MOST}, notify=["olivia"])
    seed(gh, cfg)
    res = {}
    with quiet():
        after(gh, cfg, res)
    return res
