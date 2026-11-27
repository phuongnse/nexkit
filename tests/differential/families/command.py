"""#568: a person's command, a *Request changes* review and NexKit's own dispatch of a command
(an automatic fix round, a conflict round, a review) × the command (plan, go, fix, review, trust,
and one that does not exist) × the target: an open issue with a plan or none, a closed one, one
whose NexKit pull request is open or closed, a NexKit pull request that is open, closed, whose base
branch already holds its change, or whose implement round still runs, a person's pull request
that is open, closed or whose base holds its change, a fork's pull request, and an issue whose
last questions a note answers with the option that only closes it. Records the decision that
`route` makes and GitHub after it.

NexKit 6 decided every command in `route`; since #568 the engine answers whether it starts its
stage, and `route` says each refusal in NexKit 6's words. A fork, a person's pull request, an
unknown command and a running implement round stay `route`'s."""

import inspect

from common import attempt, quiet, seed, snapshot

from nexkit.state import PLAN_MARKER, plan_data_record, render_run
from tests.support import FakeGitHub, make_config

RUNS = "https://github.com/acme/app/actions/runs"
HEAD = "a" * 40
NOT_REPRODUCED = "Close the issue as not reproduced, with no code change"
ASKED = {
    "question": "The failure does not reproduce. How should this go on?",
    "options": ["Keep the issue open until someone attaches the logs", NOT_REPRODUCED],
    "recommended": 0,
    "closes": 2,
}
COMMANDS = ["plan", "go", "fix", "review", "trust", "nope"]
ISSUES = [
    "issue",
    "issue_plan",
    "issue_closed",
    "issue_pull",
    "issue_pull_unplanned",
    "issue_pull_closed",
    "closing",
]
PULLS = [
    "pull",
    "pull_closed",
    "pull_on_base",
    "pull_running",
    "person",
    "person_closed",
    "person_on_base",
    "fork",
]
# (the event that starts the command, its `auto` input when a dispatch)
SOURCES = ["comment", "dispatch", "auto", "conflicts"]
NUMBER = {"person": 10, "fork": 11}


def scenarios():
    for target in ISSUES + PULLS:
        for command in COMMANDS:
            for source in SOURCES:
                if source == "comment":
                    yield {"target": target, "command": command, "source": source}
                elif command != "trust":
                    yield {"target": target, "command": command, "source": source}
        if target in PULLS:
            yield {"target": target, "command": "fix", "source": "review"}


def number(target):
    if target in ISSUES:
        return 5
    return NUMBER.get(target.split("_")[0], 6)


def build(sc):
    gh = FakeGitHub()
    target = sc["target"]
    gh.add_issue(5, title="Cache", state="closed" if target == "issue_closed" else "open")
    planned = target in ("issue_plan", "issue_pull", "issue_pull_closed", "closing")
    if planned:
        plan = {"url": f"{RUNS}/1", "command": "plan", "status": "success", "outcome": "planned"}
        gh.comment(5, render_run(plan, "Planned."))
        record = plan_data_record({"questions": [ASKED]}) if target == "closing" else ""
        gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.\n\n{record}")
    if target.startswith(("issue_pull", "pull")):
        gh.add_pull(6, 5, head_sha=HEAD, state="closed" if target.endswith("closed") else "open")
    if target == "pull_on_base":
        gh.on_base.add(HEAD)
    if target == "pull_running":
        running = {"url": f"{RUNS}/9", "command": "go", "status": "running"}
        gh.comment(5, render_run(running, "Running."))
        gh.runs[9] = {"status": "in_progress"}
    if target.startswith("person"):
        state = "closed" if target.endswith("closed") else "open"
        gh.add_pull(10, 8, head_sha=HEAD, branch="upgrade/nexkit", state=state)
        if target == "person_on_base":
            gh.on_base.add(HEAD)
    if target == "fork":
        gh.add_pull(11, 5, head_sha=HEAD, repo="mallory/app")
    return gh


def decide(route, gh, sc):
    """The decision of `route` for the scenario's event, with the configuration where `route`
    takes one: NexKit 6's takes none."""
    n = number(sc["target"])
    on_pull = sc["target"] in PULLS
    note = NOT_REPRODUCED if sc["target"] == "closing" else ""
    extra = {"cfg": make_config()} if "cfg" in inspect.signature(route).parameters else {}
    if sc["source"] == "review":
        event = {
            "action": "submitted",
            "review": {
                "id": 31,
                "state": "changes_requested",
                "body": "Handle None",
                "user": {"login": "olivia"},
            },
            "pull_request": {"number": n},
        }
        return route(gh, "pull_request_review", event, **extra)
    if sc["source"] == "comment":
        issue = {"number": n, **({"pull_request": {}} if on_pull else {})}
        user = {"login": "alice", "type": "User"}
        body = f"/nexkit {sc['command']} {note}".strip()
        comment = {"id": 70, "body": body, "user": user}
        event = {"action": "created", "issue": issue, "comment": comment}
        return route(gh, "issue_comment", event, **extra)
    auto = {"dispatch": "false", "auto": "true", "conflicts": "conflicts"}[sc["source"]]
    inputs = {"command": sc["command"], "number": str(n), "note": note, "auto": auto}
    return route(gh, "workflow_dispatch", {"sender": {"login": "bot"}}, inputs, **extra)


def run(sc):
    from nexkit import route

    gh = build(sc)
    seed(gh, make_config())
    res = {}
    with quiet():
        res["decision"] = attempt(decide, route.route, gh, sc)
        # Reading an issue's comments makes an empty list of them in the fake: not GitHub's.
        for number in [n for n, found in gh.issue_comments.items() if not found]:
            del gh.issue_comments[number]
        res["gh"] = snapshot(gh)
    return res
