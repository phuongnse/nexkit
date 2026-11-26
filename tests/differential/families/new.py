"""#438: the first plan of a new issue × a hold label × a blocker (none, open, closed) × an open
issue of a lower wave in its milestone × the last run (none, a failed plan, a failed
`/nexkit go`, neither with a plan comment) × an author outside the repository × `max_parallel`
alone or with another issue busy × a sub-issue of a parent. Records the preview, one sweep with
GitHub after it, and `nexkit status`. The GitHub host never sends the engine a held or blocked
document, since NexKit 6's checks stop those issues first, so the engine's refusal of such a
start shows only to the walker."""

import itertools

from common import after, quiet

from nexkit.state import render_run
from tests.support import FakeGitHub, make_config

RUNS = "https://github.com/acme/app/actions/runs"
LAST = ["none", "plan_failed", "go_failed"]


def scenarios():
    dims = dict(
        hold=[False, True],
        blocker=["none", "open", "closed"],
        wave=[False, True],
        last=LAST,
        outsider=[False, True],
        parallel=[None, 1],
        busy=[False, True],
        parent=[False, True],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        if sc["busy"] and sc["parallel"] is None:
            continue
        yield sc


def build(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    labels = ["docs"] + (["on-hold"] if sc["hold"] else [])
    author = "eve" if sc["outsider"] else "alice"
    gh.add_issue(5, title="Cache", milestone=1, labels=labels, user=author)
    if sc["blocker"] != "none":
        closed = sc["blocker"] == "closed"
        state, reason = ("closed", "completed") if closed else ("open", None)
        gh.add_issue(7, title="Blocker", state=state, reason=reason)
        gh.blockers[5] = [7]
    if sc["wave"]:
        gh.add_issue(8, title="Journey", milestone=1, labels=["journey"])
    if sc["busy"]:
        gh.add_issue(9, title="Busy", milestone=1)
        running = {"url": f"{RUNS}/9", "command": "plan", "status": "running"}
        gh.comment(9, render_run(running, "Running."))
    if sc["parent"]:
        gh.add_issue(3, title="Parent", milestone=1)
        gh.parents[5] = 3
    if sc["last"] != "none":
        command = sc["last"].split("_")[0]
        failed = {"url": f"{RUNS}/1", "command": command, "status": "failure"}
        failed["outcome"] = "agent_error"
        gh.comment(5, render_run(failed, "Failed."))
    return gh


def run(sc):
    gh = build(sc)
    cfg = make_config(
        autopilot={
            "enabled": True,
            "max_parallel": sc["parallel"],
            "labels": {"on-hold": {"hold": True}, "journey": {"wave": 1}, "docs": {"wave": 2}},
        },
        notify=["olivia"],
    )
    res = {}
    with quiet():
        after(gh, cfg, res)
    return res
