"""Work that follows from events other than commands. No agent runs here.

`route` turns those events into the `maintain` action, and the `maintain` job runs it. The
job holds write tokens for issues, pull request comments and workflow dispatches, but never
checks out or executes repository code and never receives an agent credential.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import UTC, datetime, timedelta

from . import autopilot
from .autopilot import in_repository
from .config import setting
from .github import GitHubError
from .route import issue_from_branch
from .state import (
    CONTINUED,
    PAUSED,
    RUNNING,
    UNCOUNTED,
    by_bot,
    conflict_rounds_used,
    run_comments,
    with_run,
)
from .usage import PAUSE_WINDOW, iso, parse_iso

# Each task runs only when one of its configuration keys is on.
TASKS = {
    "parents": ("close_parent_issues", "autopilot.enabled"),
    "schedule": ("usage_limit.resume", "conflicts.auto_resolve", "autopilot.enabled"),
    "resume": ("usage_limit.resume",),
    "conflicts": ("conflicts.auto_resolve",),
    # An issue opened, reopened, labeled or milestoned, or a milestone closed.
    "autopilot": ("autopilot.enabled",),
    # The run that looks at every issue in scope (nexkit/autopilot.py).
    "sweep": ("autopilot.enabled",),
}
UNDECIDED = "<!-- nexkit:parent-undecided -->"
CLOSED = "<!-- nexkit:parent-closed -->"
HOW = {"completed": "completed", "not_planned": "not planned", "duplicate": "duplicate"}
# The `resume` dispatch: a run that waits for paused commands to come due and starts them.
# Its `number` input is not a number, so callers' concurrency groups give it a group of its
# own and it never holds up a command on an issue or pull request.
WAIT_INPUTS = {"command": "resume", "number": "resume", "note": "", "auto": "false"}
# How often a waiting run looks again for runs that paused while it waits.
POLL = timedelta(minutes=10)


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
        if not in_repository(gh, parent):
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


def close_parents_safely(gh, number):
    """`close_parents` for a run that must not fail because of it: errors become a line."""
    try:
        return close_parents(gh, number)
    except GitHubError as exc:
        return [f"Could not check the parent issue of #{number}: {exc}"]


def _number(comment):
    match = re.search(r"/issues/(\d+)$", comment.get("issue_url") or "")
    return int(match.group(1)) if match else None


def paused_runs(gh, now):
    """(comment, issue or pull request number, run, resume time) for each run paused at the
    usage limit that NexKit resumes by itself."""
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
        if at:
            yield comment, number, run, at


def resume_paused(gh, workflow, ref, now=None):
    """Start each command that paused at the usage limit and whose reset time has passed,
    once. Its run comment then shows it continued in a new run and holds no resume record.
    Returns (lines, the earliest resume time still to come or None)."""
    now = now or datetime.now(UTC)
    lines, upcoming = [], None
    for comment, number, run, at in paused_runs(gh, now):
        if at > now:
            upcoming = min(upcoming or at, at)
            continue
        resume = run["resume"]
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
        run["status"] = CONTINUED
        link = f"[a new run]({started['html_url']})" if started.get("html_url") else "a new run"
        target = "" if inputs["number"] == str(number) else f" on #{inputs['number']}"
        note = f"Resumed `/nexkit {inputs['command']}`{target} in {link} after the usage limit."
        gh.update_comment(comment["id"], with_run(comment["body"], run, note))
        lines.append(f"Resumed /nexkit {inputs['command']} on #{inputs['number']}.")
    return lines, upcoming


def start_waiting(gh, workflow, ref):
    """Dispatch the run that waits for paused commands to come due. A second one waits
    behind the first in its concurrency group and finds little or nothing left to do."""
    gh.dispatch(workflow, ref, WAIT_INPUTS)


def wait_and_resume(gh, workflow, ref, wait_minutes, now=datetime.now, sleep=time.sleep):
    """Resume paused commands as they come due, for up to `wait_minutes`. Looks again every
    `POLL`, so runs that pause meanwhile are resumed too. A resume due later is left to a
    later scheduled check, which starts another wait when it comes within reach."""
    deadline = now(UTC) + timedelta(minutes=wait_minutes)
    lines = []
    while True:
        done, upcoming = resume_paused(gh, workflow, ref, now(UTC))
        lines += done
        current = now(UTC)
        if upcoming is None or upcoming > deadline or current >= deadline:
            return lines
        sleep(max(0.0, min((upcoming - current).total_seconds(), POLL.total_seconds())))


def check_paused(gh, workflow, ref, wait_minutes, now=None):
    """The scheduled check. When a resume comes due within `wait_minutes`, it starts the run
    that waits for it, and every resume goes through that run, so two runs never start the
    same command. With no wait, it resumes what is due itself."""
    now = now or datetime.now(UTC)
    if not wait_minutes:
        return resume_paused(gh, workflow, ref, now)[0]
    reach = now + timedelta(minutes=wait_minutes)
    if not any(at <= reach for _, _, _, at in paused_runs(gh, now)):
        return []
    start_waiting(gh, workflow, ref)
    return ["Started a run that resumes paused commands as they come due."]


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


def start_conflict_round(gh, number, base, workflow, ref, max_rounds, own=None):
    """Start an automatic fix round that merges `base` into the pull request, unless one
    already tried this base commit, its limit is used up, or its latest round is paused or
    still running. `own` is the URL of the round that calls this while it reports: its
    comment still says running, but it is ending.
    Returns a line saying what happened, or "" when nothing was started for a known reason
    that needs no line."""
    rounds = [run for _, run in run_comments(gh.comments(number)) if "round" in run]
    tried = [run for run in rounds if run.get("conflicts")]
    sha = gh.branch_sha(base)
    if tried and tried[-1].get("base_sha") == sha:
        return ""
    latest = rounds[-1] if rounds else {}
    if latest.get("status") in (PAUSED, RUNNING, CONTINUED) and not (
        own and latest.get("url") == own
    ):
        # A running round merges the base branch itself when it conflicts, and a dispatch
        # now would replace a command waiting behind it. A later check starts the round.
        # A continued round has a run on its way.
        return ""
    if latest.get("conflicts") and latest.get("outcome") in UNCOUNTED:
        # The last round stopped because NexKit may not push the merge: a person merges
        # it, and their next round lets conflict rounds start again.
        return f"#{number} conflicts with `{base}`, which a person merges."
    if conflict_rounds_used(tried) >= max_rounds:
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
        lines = close_parents_safely(gh, decision["issue"]) if cfg["close_parent_issues"] else []
        return lines + autopilot.request_safely(gh, cfg, workflow, ref, decision["issue"])
    if task == "autopilot":
        if not autopilot.concerns(cfg, decision):
            return ["The event does not change autopilot's scope."]
        return autopilot.request_safely(gh, cfg, workflow, ref)
    if task == "sweep":
        return autopilot.sweep(gh, cfg, workflow, ref, decision.get("closed"))
    if task == "conflicts":
        return resolve_conflicts(
            gh, decision["base"], workflow, ref, cfg["conflicts"]["max_rounds"]
        )
    wait = cfg["usage_limit"]["wait_minutes"]
    if task == "resume":
        return wait_and_resume(gh, workflow, ref, wait)
    if task == "schedule":
        lines = []
        if cfg["usage_limit"]["resume"]:
            try:
                lines += check_paused(gh, workflow, ref, wait)
            except GitHubError as exc:
                print(f"warning: could not resume paused runs: {exc}", file=sys.stderr)
        if cfg["conflicts"]["auto_resolve"]:
            # Also catches base branch moves whose push started no run.
            lines += resolve_conflicts(gh, ref, workflow, ref, cfg["conflicts"]["max_rounds"])
        # Also catches events that started no workflow, such as issues NexKit closed.
        return lines + autopilot.request_safely(gh, cfg, workflow, ref)
    raise ValueError(f"Unknown maintenance task: {task}")
