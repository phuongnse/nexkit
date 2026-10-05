"""Show each run in its own comment, posted where the run was started.

`route` calls `start` once a command is accepted, which posts the run comment; `report`
edits that comment with the outcome. Nothing in between can write to GitHub, because the
agent, verify and review jobs keep read-only tokens.
"""

from __future__ import annotations

import sys

from .github import GitHubError, run_url
from .state import (
    FAILURE,
    RUNNING,
    SUCCESS,
    find_run,
    next_round,
    render_run,
    render_state,
)

COMMANDS = {"plan": "plan", "implement": "go", "fix": "fix", "review": "review"}
STATUS_CONTEXTS = ("nexkit/checks", "nexkit/review")


def trigger(decision):
    return decision["action"] + (" (auto)" if decision.get("auto") else "")


def _save(gh, number, comment_id, body):
    if comment_id:
        gh.update_comment(comment_id, body)
    else:
        gh.comment(number, body)


def find_round(comments, decision):
    """(comment_id, round) for this run's round comment, or (None, a new round)."""
    url = run_url()
    comment_id, row = find_run(comments, url)
    row = row or {"round": next_round(comments), "url": url}
    row["trigger"] = trigger(decision)
    return comment_id, row


def save_round(gh, pr, comment_id, row, text=""):
    """Edit the round's comment, or post it when there is none (for example, deleted)."""
    _save(gh, pr, comment_id, render_run(row, text))


def save_state(gh, pr, comment_id, state):
    """Edit the state comment. A v1.1.0 state comment is left as it is; a new one replaces it."""
    if "rounds" in state:
        state = {key: value for key, value in state.items() if key != "rounds"}
        state["version"] = 2
        comment_id = None
    _save(gh, pr, comment_id, render_state(state))


def show_command(gh, decision, status, text=""):
    """Post or edit the comment for this run of an issue command."""
    issue = decision["issue"]
    url = run_url()
    comment_id, _ = find_run(gh.comments(issue), url)
    run = {"url": url, "command": COMMANDS[decision["action"]], "status": status}
    _save(gh, issue, comment_id, render_run(run, text))


def start(gh, decision):
    """Acknowledge an accepted command and link the run. Never stops the run."""
    if decision.get("comment_id"):
        gh.react(decision["comment_id"], "eyes")
    try:
        if decision.get("pr"):
            description = f"NexKit {decision['action']} round in progress"
            for context in STATUS_CONTEXTS:
                gh.set_status(decision["head"], context, "pending", description, run_url())
            comment_id, row = find_round(gh.comments(decision["pr"]), decision)
            row.update(status=RUNNING, head=decision["head"], checks="-", verdict="-", cost=None)
            save_round(gh, decision["pr"], comment_id, row)
        else:
            show_command(gh, decision, RUNNING)
    except GitHubError as exc:
        print(f"warning: could not show the run's progress: {exc}", file=sys.stderr)


def finish(gh, decision, ok, text):
    """Show the outcome of an issue command and react to the command comment."""
    if not decision.get("pr"):
        show_command(gh, decision, SUCCESS if ok else FAILURE, text)
    if decision.get("comment_id"):
        gh.react(decision["comment_id"], "rocket" if ok else "confused")
