"""Show each run in its own comment, posted where the run was started.

`route` calls `start` once a command is accepted, which posts the run comment; `report`
edits that comment with the outcome. Nothing in between can write to GitHub, because the
agent, verify and review jobs keep read-only tokens.
"""

from __future__ import annotations

import sys

from .github import GitHubError, run_url
from .state import (
    PAUSED,
    RUNNING,
    SUCCESS,
    find_run,
    next_round,
    render_run,
    render_state,
    run_comments,
    with_run,
)

COMMANDS = {"plan": "plan", "implement": "go", "fix": "fix", "review": "review"}
STATUS_CONTEXTS = ("nexkit/checks", "nexkit/review")


def trigger(decision):
    """`fix`, `fix (auto)`, `fix (auto, conflicts)`, `review (resumed)`, ..."""
    labels = [
        label
        for label, on in (
            ("auto", decision.get("auto")),
            ("conflicts", decision.get("conflicts")),
            ("resumed", decision.get("resumed")),
        )
        if on
    ]
    return decision["action"] + (f" ({', '.join(labels)})" if labels else "")


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
    for key in ("conflicts", "resumed", "base_sha"):
        if decision.get(key):
            row[key] = decision[key]
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


def show_command(gh, decision, status, text="", comments=None, **fields):
    """Post or edit the comment for this run of an issue command. `fields` go into its
    marker, such as the outcome."""
    issue = decision["issue"]
    url = run_url()
    comments = gh.comments(issue) if comments is None else comments
    comment_id, _ = find_run(comments, url)
    run = {"url": url, "command": COMMANDS[decision["action"]], "status": status, **fields}
    _save(gh, issue, comment_id, render_run(run, text))


def replace_paused(gh, comments, why="Replaced by a later command."):
    """Stop paused runs in `comments` from resuming: a new command replaces them."""
    for comment, run in run_comments(comments):
        if run.get("status") == PAUSED and run.pop("resume", None):
            gh.update_comment(comment["id"], with_run(comment["body"], run, why))


def start(gh, decision):
    """Acknowledge an accepted command and link the run. Never stops the run."""
    if decision.get("comment_id"):
        gh.react(decision["comment_id"], "eyes")
    try:
        number = decision.get("pr") or decision["issue"]
        comments = gh.comments(number)
        replace_paused(gh, comments)
        if decision.get("pr"):
            description = f"NexKit {decision['action']} round in progress"
            for context in STATUS_CONTEXTS:
                gh.set_status(decision["head"], context, "pending", description, run_url())
            comment_id, row = find_round(comments, decision)
            row.update(status=RUNNING, head=decision["head"], checks="-", verdict="-", cost=None)
            save_round(gh, decision["pr"], comment_id, row)
        else:
            show_command(gh, decision, RUNNING, comments=comments)
    except GitHubError as exc:
        print(f"warning: could not show the run's progress: {exc}", file=sys.stderr)


def finish(gh, decision, status, text, **fields):
    """Show the outcome of an issue command and react to the command comment. A paused
    run keeps 👀: it has not finished."""
    if not decision.get("pr"):
        show_command(gh, decision, status, text, **fields)
    if decision.get("comment_id") and status != PAUSED:
        gh.react(decision["comment_id"], "rocket" if status == SUCCESS else "confused")
