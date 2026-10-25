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
from .route import implementing, issue_from_branch
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
    # An issue opened, reopened or milestoned, or a milestone closed.
    "autopilot": ("autopilot.enabled",),
    # The run that looks at every issue in scope (nexkit/autopilot.py) and at every pull
    # request that conflicts with its base branch.
    "sweep": ("autopilot.enabled", "conflicts.auto_resolve"),
    # A run of another workflow finished, such as one of merge.after_workflows.
    "ci": ("autopilot.enabled",),
    # `route` refused a command that NexKit started by itself.
    "lost": ("autopilot.enabled",),
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
# How often, and how many times, a sweep after a run whose report did not finish looks
# whether that run ended, before it sweeps anyway.
END_POLL = 10
END_TRIES = 30


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


def wait_for_end(gh, run_id, tries=END_TRIES, delay=END_POLL):
    """Wait until the workflow run `run_id` ended, for at most `tries` looks. The run that
    asked for this sweep is still ending when it asks. Returns a line for the log."""
    for attempt in range(tries):
        try:
            if gh.workflow_run(run_id).get("status") == "completed":
                return f"Run {run_id} ended."
        except GitHubError as exc:
            return f"Could not read run {run_id}: {exc}"
        if attempt < tries - 1:
            time.sleep(delay)
    return f"Run {run_id} has not ended yet; a later sweep handles it."


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


def _queued(comment, run, now):
    """Whether the round `run` started the next one by itself, which has not posted its
    comment yet. One that did not run within `autopilot.LOST` holds nothing up."""
    if run.get("outcome") not in autopilot.CHAINED:
        return False
    at = parse_iso(comment.get("updated_at") or comment.get("created_at"))
    return at is None or now - at <= autopilot.LOST


def _tried(rounds):
    """(index into `rounds`, base commit) of the latest conflict round started: a conflict
    round itself, or the start that a round recorded as it ended (`conflicts_started`)."""
    found = None
    for index, (_, run) in enumerate(rounds):
        if run.get("conflicts"):
            found = (index, run.get("base_sha"))
        if run.get("conflicts_started"):
            found = (index, run["conflicts_started"])
    return found


def _rounds(gh, number):
    return [(c, run) for c, run in run_comments(gh.comments(number)) if "round" in run]


def start_conflict_round(
    gh, number, base, workflow, ref, max_rounds, row=None, now=None, settle=False
):
    """Start an automatic fix round that merges `base` into the pull request, unless one
    started since the base branch last moved and since the pull request's last round
    ended, its limit is used up, or a round is on its way: its latest round, or the
    implement round that opened it, is paused or still running, or started the next round
    by itself. `row` is the round that calls this while it reports: its comment still says
    running, but it is ending, and it records the start as it saves.

    A start is recorded on the pull request's latest round (`conflicts_started`), so a
    sweep that looks before the new round posts its comment does not start it twice.
    With `settle`, when autopilot does not, a latest round whose run ended without a
    report is shown as ended first, so it holds nothing up. Returns a line saying what
    happened, or "" when nothing was started for a known reason that needs no line."""
    now = now or datetime.now(UTC)
    own = (row or {}).get("url")
    rounds = _rounds(gh, number)
    if settle and rounds and autopilot.settle_ended(gh, number, *rounds[-1]):
        rounds = _rounds(gh, number)
    if own and not any(run.get("url") == own for _, run in rounds):
        # The implement round posts its round comment on the pull request as it reports.
        rounds.append(({}, row))
    tried = [run for _, run in rounds if run.get("conflicts")]
    sha = gh.branch_sha(base)
    last = _tried(rounds)
    if last and last == (len(rounds) - 1, sha):
        # Tried on this base commit, and no round ended since: a blocked round waits for a
        # person or the next move of the base branch.
        return ""
    comment, latest = rounds[-1] if rounds else ({}, {})
    ending = bool(own) and latest.get("url") == own
    if latest.get("status") in (PAUSED, RUNNING, CONTINUED) and not ending:
        # A running round merges the base branch itself when it conflicts, and a dispatch
        # now would replace a command waiting behind it. Its end starts the round.
        # A continued round has a run on its way.
        return ""
    if not ending and _queued(comment, latest, now):
        # The round it started next merges the base branch itself when it conflicts.
        return ""
    issue = issue_from_branch(gh.pull(number)["head"]["ref"])
    running = implementing(gh, issue) if issue else None
    if running and running.get("url") != own:
        # The implement round still checks, reviews and merges the pull request; a round
        # that pushed now would make its merge fail. Its report, or a later check, starts
        # this round with the branch as that round left it.
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
    if ending:
        row["conflicts_started"] = sha
    elif comment.get("id"):
        latest["conflicts_started"] = sha
        gh.update_comment(comment["id"], with_run(comment["body"], latest))
    return f"Started an automatic round on #{number} to merge `{base}`."


def resolve_conflicts(gh, base, workflow, ref, max_rounds, skip=(), delay=5, settle=False):
    """Start a conflict round for each open NexKit pull request into `base` that conflicts
    with it. With `settle`, a latest round whose run ended without a report holds nothing
    up (see `start_conflict_round`). Returns lines for a log or comment."""
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
                lines.append(
                    start_conflict_round(gh, number, base, workflow, ref, max_rounds, settle=settle)
                )
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
        # Even an event that changes nothing in scope, such as a milestoned issue with
        # `null`, starts a sweep: its run joins the group `nexkit-autopilot`, where it may
        # have replaced a sweep that waited.
        return autopilot.request_safely(gh, cfg, workflow, ref)
    if task == "sweep":
        lines = [wait_for_end(gh, decision["after"])] if decision.get("after") else []
        busy = set()
        if autopilot.takes(cfg):
            lines += autopilot.sweep(gh, cfg, workflow, ref, decision.get("closed"), busy)
        if cfg["conflicts"]["auto_resolve"]:
            # After the commands it started, which merge the base branch themselves.
            max_rounds = cfg["conflicts"]["max_rounds"]
            # Without autopilot, nothing else ends a round whose report did not finish.
            settle = not autopilot.takes(cfg)
            lines += resolve_conflicts(gh, ref, workflow, ref, max_rounds, skip=busy, settle=settle)
        return lines or ["Nothing to start."]
    if task == "ci":
        return autopilot.after_ci_run(gh, cfg, decision, workflow, ref)
    if task == "lost":
        # The sweep waits for this run to end, so it sees that the command never ran.
        return autopilot.request_safely(gh, cfg, workflow, ref, after=decision["after"])
    if task == "conflicts":
        return resolve_conflicts(
            gh,
            decision["base"],
            workflow,
            ref,
            cfg["conflicts"]["max_rounds"],
            settle=not autopilot.takes(cfg),
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
            settle = not autopilot.takes(cfg)
            max_rounds = cfg["conflicts"]["max_rounds"]
            lines += resolve_conflicts(gh, ref, workflow, ref, max_rounds, settle=settle)
        # Also catches events that started no workflow, such as issues NexKit closed.
        return lines + autopilot.request_safely(gh, cfg, workflow, ref)
    raise ValueError(f"Unknown maintenance task: {task}")
