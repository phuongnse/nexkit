"""Show a run's progress where its command was given.

`route` calls `start` once a command is accepted; `report` calls `finish` at the end.
Nothing in between can write to GitHub, because the agent, verify and review jobs keep
read-only tokens.
"""

from __future__ import annotations

import sys

from .github import GitHubError, run_url
from .state import (
    FAILURE,
    MAX_RUNS,
    RUNNING,
    SUCCESS,
    empty_state,
    empty_status,
    find_run,
    read_state,
    read_status,
    render_state,
    render_status,
)

COMMANDS = {"plan": "plan", "implement": "go", "fix": "fix", "review": "review"}
STATUS_CONTEXTS = ("nexkit/checks", "nexkit/review")


def trigger(decision):
    return decision["action"] + (" (auto)" if decision.get("auto") else "")


def round_row(state, decision, url):
    """The pull request round for this run, added to the table when it is not there yet."""
    row = find_run(state["rounds"], url)
    if row is None:
        row = {"round": len(state["rounds"]) + 1, "url": url}
        state["rounds"].append(row)
    row["trigger"] = trigger(decision)
    return row


def save_state(gh, pr, comment_id, state):
    body = render_state(state)
    if comment_id:
        gh.update_comment(comment_id, body)
    else:
        gh.comment(pr, body)


def update_issue(gh, decision, status, text, url):
    """Set this run's line in the issue's status comment, creating the comment if needed."""
    issue = decision["issue"]
    comment_id, data = read_status(gh.comments(issue))
    data = data or empty_status()
    item = find_run(data["runs"], url)
    if item is None:
        item = {"url": url}
        data["runs"].append(item)
    item.update(command=COMMANDS[decision["action"]], status=status, text=text)
    data["runs"] = data["runs"][-MAX_RUNS:]
    if comment_id:
        gh.update_comment(comment_id, render_status(data))
    else:
        gh.comment(issue, render_status(data))


def start(gh, decision):
    """Acknowledge an accepted command and link the run. Never stops the run."""
    if decision.get("comment_id"):
        gh.react(decision["comment_id"], "eyes")
    url = run_url()
    try:
        if decision.get("pr"):
            description = f"NexKit {decision['action']} round in progress"
            for context in STATUS_CONTEXTS:
                gh.set_status(decision["head"], context, "pending", description, url)
            comment_id, state = read_state(gh.comments(decision["pr"]))
            state = state or empty_state(decision["issue"])
            row = round_row(state, decision, url)
            row.update(status=RUNNING, head=decision["head"], checks="-", verdict="-", cost=None)
            save_state(gh, decision["pr"], comment_id, state)
        else:
            update_issue(gh, decision, RUNNING, "", url)
    except GitHubError as exc:
        print(f"warning: could not show the run's progress: {exc}", file=sys.stderr)


def finish(gh, decision, ok, text):
    """Show the outcome of an issue command and react to the command comment."""
    if not decision.get("pr"):
        update_issue(gh, decision, SUCCESS if ok else FAILURE, text, run_url())
    if decision.get("comment_id"):
        gh.react(decision["comment_id"], "rocket" if ok else "confused")
