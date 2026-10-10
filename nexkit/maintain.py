"""Work that follows from events other than commands. No agent runs here.

`route` turns those events into the `maintain` action, and the `maintain` job runs it. The
job holds write tokens for issues, but never checks out or executes repository code and
never receives the Claude credential.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import UTC, datetime, timedelta

from .config import setting
from .github import GitHubError
from .route import issue_from_branch
from .state import PAUSED, RUNNING, by_bot, run_comments, with_run
from .usage import iso, parse_iso

# Each task runs only when one of its configuration keys is on.
TASKS = {
    "parents": ("close_parent_issues",),
    "schedule": ("usage_limit.resume", "conflicts.auto_resolve"),
    "conflicts": ("conflicts.auto_resolve",),
}
UNDECIDED = "<!-- nexkit:parent-undecided -->"
CLOSED = "<!-- nexkit:parent-closed -->"
HOW = {"completed": "completed", "not_planned": "not planned", "duplicate": "duplicate"}
# Usage limits reset within a week; paused runs older than this are not resumed.
PAUSE_WINDOW = timedelta(days=8)


def enabled(task, cfg):
    return any(setting(cfg, path) for path in TASKS[task])


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
        if not _here(gh, parent):
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
        # Sub-issues closed together start one run each; only the first closes the parent.
        if gh.issue(number).get("state") != "open":
            return lines
        latest = gh.comments(number)[-1:]
        if not (latest and by_bot(latest[0]) and CLOSED in latest[0]["body"]):
            gh.comment(
                number,
                f"{CLOSED}\nNexKit closed this issue because all its sub-issues are "
                f"closed:\n\n{listing}",
            )
        gh.close_issue(number, "completed")
        lines.append(f"Closed #{number} as completed: all its sub-issues are closed.")


def _here(gh, issue):
    """Whether an issue from the sub-issues API belongs to this repository: a parent can be
    in another repository of the same owner, with a number that means another issue here."""
    url = issue.get("repository_url")
    return not url or url.lower().endswith(f"/repos/{gh.repository}".lower())


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


def conflict_note(base):
    return (
        f"Merge `{base}` into this branch and resolve the conflicts. Keep the intent of both "
        "sides; change nothing else."
    )


def is_conflicting(gh, number, tries=6, delay=5):
    """Whether GitHub says the pull request conflicts with its base branch. GitHub works
    this out in the background after the base branch moves; None when it has not yet."""
    for attempt in range(tries):
        mergeable = gh.pull(number).get("mergeable")
        if mergeable is not None:
            return mergeable is False
        if attempt < tries - 1:
            time.sleep(delay)
    return None


def start_conflict_round(gh, number, base, workflow, ref, max_rounds):
    """Start an automatic fix round that merges `base` into the pull request, unless one
    already tried this base commit, its limit is used up, or its latest round is paused.
    Returns a line saying what happened, or "" when nothing was started for a known reason
    that needs no line."""
    rounds = [run for _, run in run_comments(gh.comments(number)) if "round" in run]
    tried = [run for run in rounds if run.get("conflicts")]
    sha = gh.branch_sha(base)
    if tried and tried[-1].get("base_sha") == sha:
        return ""
    if rounds and rounds[-1].get("status") in (PAUSED, RUNNING):
        # A running round merges the base branch itself when it conflicts, and a dispatch
        # now would replace a command waiting behind it. A later check starts the round.
        return ""
    if len([run for run in tried if not run.get("resumed")]) >= max_rounds:
        return (
            f"#{number} conflicts with `{base}`, but its {max_rounds} automatic "
            "conflict rounds are used up."
        )
    inputs = {"command": "fix", "number": str(number), "note": conflict_note(base)}
    gh.dispatch(workflow, ref, {**inputs, "auto": "conflicts"})
    return f"Started an automatic round on #{number} to merge `{base}`."


def resolve_conflicts(gh, base, workflow, ref, max_rounds, skip=(), delay=5):
    """Start a conflict round for each open NexKit pull request into `base` that conflicts
    with it. Returns lines for a log or comment."""
    lines = []
    for pull in gh.pulls_into(base):
        number = pull["number"]
        nexkit = issue_from_branch(pull["head"]["ref"]) is not None
        if number in skip or not nexkit or pull["head"]["repo"]["full_name"] != gh.repository:
            continue
        try:
            conflicting = is_conflicting(gh, number, delay=delay)
            if conflicting is None:
                lines.append(f"GitHub has not worked out yet whether #{number} conflicts.")
            elif conflicting:
                lines.append(start_conflict_round(gh, number, base, workflow, ref, max_rounds))
        except GitHubError as exc:
            lines.append(f"Could not check #{number} for conflicts with `{base}`: {exc}")
    return [line for line in lines if line]


def maintain(gh, decision, cfg, *, workflow="", ref=""):
    """Run the decision's task. Returns lines for the job log."""
    task = decision["task"]
    if task == "parents":
        return close_parents_safely(gh, decision["issue"])
    if task == "conflicts":
        return resolve_conflicts(
            gh, decision["base"], workflow, ref, cfg["conflicts"]["max_rounds"]
        )
    if task == "schedule":
        lines = []
        if cfg["usage_limit"]["resume"]:
            try:
                lines += resume_paused(gh, workflow, ref)
            except GitHubError as exc:
                print(f"warning: could not resume paused runs: {exc}", file=sys.stderr)
        if cfg["conflicts"]["auto_resolve"]:
            # Also catches base branch moves whose push started no run.
            lines += resolve_conflicts(gh, ref, workflow, ref, cfg["conflicts"]["max_rounds"])
        return lines
    raise ValueError(f"Unknown maintenance task: {task}")
