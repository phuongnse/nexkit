"""Work that follows from events other than commands. No agent runs here.

`route` turns those events into the `maintain` action, and the `maintain` job runs it. The
job holds write tokens for issues, but never checks out or executes repository code and
never receives the Claude credential.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta

from .github import GitHubError
from .state import PAUSED, by_bot, run_comments, with_run
from .usage import iso, parse_iso

# Each task runs only when one of its configuration keys is on.
TASKS = {
    "parents": ("close_parent_issues",),
    "schedule": ("resume_after_usage_limit",),
}
UNDECIDED = "<!-- nexkit:parent-undecided -->"
HOW = {"completed": "completed", "not_planned": "not planned", "duplicate": "duplicate"}
# Usage limits reset within a week; paused runs older than this are not resumed.
PAUSE_WINDOW = timedelta(days=8)


def enabled(task, cfg):
    return any(cfg.get(key) for key in TASKS[task])


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


def _number(comment):
    match = re.search(r"/issues/(\d+)$", comment.get("issue_url") or "")
    return int(match.group(1)) if match else None


def resume_paused(gh, workflow, ref, now=None):
    """Start each command that paused at the usage limit and whose reset time has passed,
    once. Its run comment then says it was resumed and holds no resume record."""
    now = now or datetime.now(UTC)
    lines = []
    for comment in gh.recent_comments(iso(now - PAUSE_WINDOW)):
        number = _number(comment)
        found = next(run_comments([comment]), None)
        if not number or not found:
            continue
        run = found[1]
        resume = run.get("resume")
        if run.get("status") != PAUSED or not isinstance(resume, dict):
            continue
        at = parse_iso(resume.get("at"))
        if not at or at > now:
            continue
        inputs = {
            "command": str(resume.get("command") or ""),
            "number": str(resume.get("number") or number),
            "note": str(resume.get("note") or ""),
            "auto": str(resume.get("auto") or "resume"),
        }
        try:
            started = gh.dispatch(workflow, ref, inputs, run_details=True) or {}
        except GitHubError as exc:
            # The record stays, so the next scheduled run tries again.
            lines.append(f"Could not resume /nexkit {inputs['command']} on #{number}: {exc}")
            continue
        del run["resume"]
        link = f"[a new run]({started['html_url']})" if started.get("html_url") else "a new run"
        target = "" if inputs["number"] == str(number) else f" on #{inputs['number']}"
        note = f"Resumed `/nexkit {inputs['command']}`{target} in {link} after the usage limit."
        gh.update_comment(comment["id"], with_run(comment["body"], run, note))
        lines.append(f"Resumed /nexkit {inputs['command']} on #{inputs['number']}.")
    return lines


def maintain(gh, decision, cfg, *, workflow="", ref=""):
    """Run the decision's task. Returns lines for the job log."""
    task = decision["task"]
    if task == "parents":
        return close_parents_safely(gh, decision["issue"])
    if task == "schedule":
        lines = []
        if cfg["resume_after_usage_limit"]:
            try:
                lines += resume_paused(gh, workflow, ref)
            except GitHubError as exc:
                print(f"warning: could not resume paused runs: {exc}", file=sys.stderr)
        return lines
    raise ValueError(f"Unknown maintenance task: {task}")
