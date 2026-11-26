"""#415: a `/nexkit go` that stopped as blocked with nothing to answer, with the run's attention
or without × autopilot on or off × `autopilot.answer` × autopilot's answers before it or not ×
what came after (nothing, a collaborator's comment, an outsider's, a command, a blocker closed
after the round, before it, or as not planned). Records the preview, one sweep with GitHub after
it, and `nexkit status`. With autopilot off no sweep runs (`maintain.maintain` starts one only
with `autopilot.takes`) and `nexkit status` does not read the preview, so only those two are
played."""

import itertools

from common import after, attempt, quiet, snapshot

from nexkit.state import PLAN_MARKER, answers_marker, render_run
from tests.support import FakeGitHub, make_config

SINCE = [
    "none",
    "collaborator",
    "outsider",
    "command",
    "blocker_after",
    "blocker_before",
    "blocker_not_planned",
]
STOPPED, AFTER, BEFORE = "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z", "2026-01-01T12:00:00Z"


def scenarios():
    dims = dict(
        attention=[True, False],
        auto=[True, False],
        answer=["recommended", "never"],
        answered=[False, True],
        since=SINCE,
    )
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))


def build(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, title="Cache", milestone=1)
    url = iter(f"https://github.com/acme/app/actions/runs/{n}" for n in range(1, 9))
    plan = {"command": "plan", "status": "success", "outcome": "planned"}
    gh.comment(5, render_run({"url": next(url), **plan}, "Done."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    if sc["answered"]:
        record = {"answers": [{"question": "Which port?", "answer": "8080"}]}
        gh.comment(5, f"NexKit autopilot answers\n\n{answers_marker(record)}")
    go = {"command": "go", "status": "failure", "outcome": "agent_blocked", "url": next(url)}
    if sc["attention"]:
        go["attention"] = "The agent stopped: it needs a secret it does not have"
    stop = gh.comment(5, render_run(go, "Stopped."))
    stop["created_at"] = STOPPED
    since = sc["since"]
    if since == "collaborator":
        gh.human_comment(5, "The secret is set now.")
    elif since == "outsider":
        gh.human_comment(5, "Try the other way.", login="eve", association="NONE")
    elif since == "command":
        gh.human_comment(5, "/nexkit plan")
    elif since.startswith("blocker"):
        gh.add_issue(7, title="Secret", state="closed", reason="completed")
        if since == "blocker_not_planned":
            gh.issues[7]["state_reason"] = "not_planned"
        gh.issues[7]["closed_at"] = BEFORE if since == "blocker_before" else AFTER
        gh.blockers[5] = [7]
    return gh


def run(sc):
    gh = build(sc)
    cfg = make_config(autopilot={"enabled": sc["auto"], "answer": sc["answer"]}, notify=["olivia"])
    res = {}
    with quiet():
        if sc["auto"]:
            after(gh, cfg, res)
        else:
            from nexkit.status import collect

            res["gh"] = snapshot(gh)
            res["status"] = attempt(collect, gh, cfg, workers=1)
    return res
