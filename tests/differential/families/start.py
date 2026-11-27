"""#437: which waiting issues start and in what order: issues with and without priority labels
× milestones (two, of either due date, or none) × wave labels × the scope (a milestone or
every issue) × `max_parallel` × issues that are busy × a bug issue about the default branch,
or a part of one, that pull requests wait for. A few hundred scenarios, drawn from a fixed
seed, so that both trees play the same ones. Records every step of the preview in order, a
sweep and GitHub after it, and `nexkit status`."""

import random
from unittest import mock

from common import HEAD, WAIT, attempt, quiet, seed, snapshot, sweep

from nexkit import autopilot
from nexkit.state import PLAN_MARKER, render_run
from tests.support import BOT_LOGIN, FakeGitHub, make_config

RUNS = "https://github.com/acme/app/actions/runs"
COUNT = 400
# A free issue's milestone, priority label and wave label.
MILESTONES = [1, 1, 2, None]
LABELS = [None, None, "urgent", "later"]
WAVES = [None, None, "journey", "docs"]
BUG = WAIT["bug"]


def scenarios():
    rng = random.Random(437)
    for _ in range(COUNT):
        bug = rng.choice(["none", "bug", "part"])
        yield {
            "scope": rng.choice(["milestone", "every"]),
            "due": rng.choice(["first", "second", "none"]),
            "priority": rng.choice(["default", "urgent"]),
            "parallel": rng.choice([None, 1, 2, 3]),
            "busy": rng.randrange(3),
            "bug": bug,
            "bug_milestone": rng.choice([1, 2, None]),
            "waiting": 0 if bug == "none" else rng.randrange(3),
            "free": [
                [rng.choice(MILESTONES), rng.choice(LABELS), rng.choice(WAVES)]
                for _ in range(rng.randrange(1, 6))
            ],
        }


def labels(*names):
    return [name for name in names if name]


def build(sc):
    gh = FakeGitHub()
    due = {"first": ("2099-02-01", "2099-01-01"), "second": ("2099-01-01", "2099-02-01")}
    one, two = due.get(sc["due"], (None, None))
    gh.add_milestone(1, due_on=one and one + "T00:00:00Z")
    gh.add_milestone(2, due_on=two and two + "T00:00:00Z")
    for number, (milestone, label, wave) in enumerate(sc["free"], 11):
        gh.add_issue(number, milestone=milestone, labels=labels(label, wave))
    for number in range(41, 41 + sc["busy"]):
        gh.add_issue(number, milestone=1)
        running = {"url": f"{RUNS}/{number}", "command": "plan", "status": "running"}
        gh.comment(number, render_run(running, "Running."))
    if sc["bug"] != "none":
        body = f"<!-- nexkit:ci {WAIT['workflow']} -->\nThe workflow fails."
        gh.add_issue(BUG, title="CI", body=body, user=BOT_LOGIN, milestone=sc["bug_milestone"])
    if sc["bug"] == "part":
        gh.add_issue(78, title="Part", milestone=sc["bug_milestone"])
        gh.parents[78] = BUG
    for index in range(sc["waiting"]):
        issue, pull = 21 + index, 31 + index
        gh.add_issue(issue, milestone=1)
        plan = {"command": "plan", "status": "success", "outcome": "planned"}
        gh.comment(issue, render_run({"url": f"{RUNS}/{issue}1", **plan}, "Done."))
        gh.comment(issue, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
        go = {"command": "go", "status": "success", "outcome": "pr_opened"}
        gh.comment(issue, render_run({"url": f"{RUNS}/{issue}2", **go}, "Done."))
        gh.add_pull(pull, issue, head_sha=HEAD)
        row = {"trigger": "fix (auto)", "action": "fix", "kinds": ["auto"], "url": f"{RUNS}/{pull}"}
        gh.round_comment(
            pull, 1, HEAD, "failed", outcome="ci_waiting", ci_wait=WAIT, status="success", **row
        )
    return gh


def run(sc):
    gh = build(sc)
    priority = ["nexkit:ci", "nexkit:dependabot", "nexkit:flaky"]
    cfg = make_config(
        autopilot={
            "enabled": True,
            "scope": None if sc["scope"] == "every" else "milestone",
            "max_parallel": sc["parallel"],
            "priority": ["urgent", "later", *priority] if sc["priority"] == "urgent" else priority,
            "labels": {"journey": {"wave": 1}, "docs": {"wave": 2}},
        },
        notify=["olivia"],
    )
    seed(gh, cfg)
    res = {}
    over = mock.patch.object(autopilot, "ci_wait_over", lambda gh_, wait: None)

    def preview():
        _, steps = autopilot.preview(gh, cfg)
        keys = ("number", "state", "command", "reason", "person")
        return [[step.get(key) for key in keys] for step in steps]

    with over, quiet():
        res["steps"] = attempt(preview)
        res["lines"] = attempt(sweep, gh, cfg)
        res["gh"] = snapshot(gh)
        from nexkit.status import collect

        res["status"] = attempt(collect, gh, cfg, workers=1)
    return res
