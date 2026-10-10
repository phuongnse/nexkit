"""Work that follows from events other than commands. No agent runs here.

`route` turns those events into the `maintain` action, and the `maintain` job runs it. The
job holds write tokens for issues, but never checks out or executes repository code and
never receives the Claude credential.
"""

from __future__ import annotations

from .github import GitHubError
from .state import by_bot

# Each task runs only when its configuration key is on.
TASKS = {"parents": "close_parent_issues"}
UNDECIDED = "<!-- nexkit:parent-undecided -->"
HOW = {"completed": "completed", "not_planned": "not planned", "duplicate": "duplicate"}


def enabled(task, cfg):
    return bool(cfg.get(TASKS[task]))


def _closed_as(issue):
    return HOW.get(issue.get("state_reason") or "", "closed")


def close_parents(gh, number):
    """After issue `number` closed: close its parent when every sub-issue of the parent is
    closed and at least one was completed, then do the same for that parent's parent.

    A parent whose sub-issues were all closed as not planned stays open with a note for a
    person. Returns one line per parent that NexKit closed or left a note on."""
    lines, seen = [], {number}
    while True:
        parent = gh.parent_issue(number)
        if not parent or parent["number"] in seen or parent.get("state") != "open":
            return lines
        number = parent["number"]
        seen.add(number)
        subs = gh.sub_issues(number)
        if not subs or any(sub.get("state") == "open" for sub in subs):
            return lines
        listing = "\n".join(f"- #{sub['number']}: {_closed_as(sub)}" for sub in subs)
        if not any(sub.get("state_reason") == "completed" for sub in subs):
            latest = gh.comments(number)[-1:]
            if not (latest and by_bot(latest[0]) and UNDECIDED in latest[0]["body"]):
                gh.comment(
                    number,
                    f"{UNDECIDED}\nEvery sub-issue of this issue is closed, but none was "
                    f"completed:\n\n{listing}\n\nNexKit leaves this issue open. A person "
                    "should decide whether to close it.",
                )
            lines.append(f"Left #{number} open: none of its sub-issues was completed.")
            return lines
        gh.comment(
            number,
            f"NexKit closed this issue because all its sub-issues are closed:\n\n{listing}",
        )
        gh.close_issue(number, "completed")
        lines.append(f"Closed #{number} as completed: all its sub-issues are closed.")


def close_parents_safely(gh, number):
    """`close_parents` for a run that must not fail because of it: errors become a line."""
    try:
        return close_parents(gh, number)
    except GitHubError as exc:
        return [f"Could not check the parent issue of #{number}: {exc}"]


def maintain(gh, decision, cfg):
    """Run the decision's task. Returns lines for the job log."""
    task = decision["task"]
    if task == "parents":
        return close_parents_safely(gh, decision["issue"])
    raise ValueError(f"Unknown maintenance task: {task}")
