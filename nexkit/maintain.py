"""Work that follows from events other than commands, and the closing of a pull request
whose base branch already holds its change, instead of its command. No agent runs here.

`route` turns those events into the `maintain` action, and the `maintain` job runs it. The
job holds write tokens for issues, pull request comments and workflow dispatches, but never
checks out or executes repository code and never receives an agent credential.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
import zipfile
from datetime import UTC, datetime, timedelta

from . import autopilot, progress
from .autopilot import in_repository
from .config import setting
from .github import GitHubError
from .route import RUN_ID, implementing, issue_from_branch
from .state import (
    CONTINUED,
    FAILURE,
    MAX_PASSED,
    PAUSED,
    RUNNING,
    SUCCESS,
    by_bot,
    conflict_rounds_used,
    next_round,
    passed_checks,
    read_state,
    render_run,
    run_action,
    run_comments,
    run_kinds,
    with_run,
)
from .usage import PAUSE_WINDOW, iso, parse_iso

# Each task runs only when one of its configuration keys is on; one without keys always
# runs.
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
    # A run of another workflow finished, such as one of merge.after_workflows or
    # autopilot.watch_workflows.
    "ci": ("autopilot.enabled",),
    # `route` refused a command that NexKit started by itself.
    "lost": ("autopilot.enabled",),
    # A round on a pull request whose base branch already holds its change.
    "on_base": (),
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
# How many of a pull request's own commits, and of the files they changed, NexKit reads to
# find the commit of the base branch that brought the same change.
OWN_COMMITS = 10
OWN_FILES = 10
# The jobs of a round that a move of the base branch can leave without a purpose: its
# checks. A round in any other job goes on: its agent time, patch and review are worth more.
CHECK_JOBS = {"verify", "combine"}
# How long, and how often, an update of a branch waits for GitHub to move its head.
UPDATE_WAIT = 5
UPDATE_TRIES = 6
# The outcome of a round NexKit stopped in its checks because the base branch moved, and
# of one whose next round then did not start.
SUPERSEDED = "superseded"
STOP_FAILED = "stop_failed"
STOP_FAILED_REASON = (
    "NexKit stopped the round's checks after the base branch moved, but could not start the "
    "next round; comment `/nexkit fix` or `/nexkit review` on the pull request"
)
ON_BASE_FAILED = (
    "the base branch already holds this change, but NexKit could not close the pull "
    "request or its issue; close them yourself"
)


def enabled(task, cfg):
    return not TASKS[task] or any(setting(cfg, path) for path in TASKS[task])


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


def holding_commit(gh, pr, merge_base):
    """The commit of the base branch that brought the change of pull request `pr`, which
    changes nothing since it merged `merge_base`, or None when NexKit cannot tell one: the
    only commit, among those of the base branch merged into it since it started, that
    changed a file that its own commits changed."""
    try:
        own = [c for c in gh.pull_commits(pr) if len(c.get("parents") or []) == 1]
        if not own:
            return None
        start = own[0]["parents"][0]["sha"]
        merged = {c["sha"] for c in gh.compare(start, merge_base).get("commits") or []}
        files = []
        for commit in own[:OWN_COMMITS]:
            files += [f["filename"] for f in gh.commit(commit["sha"]).get("files") or []]
        found = set()
        for name in list(dict.fromkeys(files))[:OWN_FILES]:
            found |= {c["sha"] for c in gh.path_commits(merge_base, name)} & merged
    except (GitHubError, AttributeError, IndexError, KeyError, TypeError):
        return None
    return found.pop() if len(found) == 1 else None


def close_on_base(gh, cfg, target, commit=None):
    """Close a pull request whose base branch already holds its change without merging it,
    and its issue as completed, as a merge would. `target` names its `pr`, `issue`, `base`
    and `merge_base` (see `route.on_base`); `commit` is the commit that brought the change
    when the caller found it. Returns (outcome, text for the pull request's round, the
    commit that brought the change or None)."""
    pr, issue, base = target["pr"], target["issue"], target["base"]
    commit = commit or holding_commit(gh, pr, target["merge_base"])
    by = f", brought by {commit}" if commit else ""
    held = f"`{base}` already holds the change of this pull request{by}"
    try:
        gh.close_pull(pr)
    except GitHubError as exc:
        return "on_base_failed", f"{held}, but NexKit could not close it: {exc}", commit
    lines = [f"{held}, so it changes no file against `{base}`. NexKit closed it without merging."]
    try:
        if gh.issue(issue).get("state") == "open":
            closed = f"NexKit closed #{pr} without merging: `{base}` already holds its change"
            gh.comment(issue, f"{closed}{by}.")
            gh.close_issue(issue, "completed")
            lines.append(f"Closed #{issue} as completed.")
            if cfg["close_parent_issues"]:
                # Closing with the Actions token starts no workflow for `issues: closed`.
                lines += close_parents_safely(gh, issue)
    except GitHubError as exc:
        lines.append(f"Could not close #{issue}: {exc}. Close it yourself.")
        return "on_base_failed", "\n\n".join(lines), commit
    return "already_on_base", "\n\n".join(lines), commit


def on_base_round(gh, cfg, decision, workflow, ref):
    """Close a pull request whose base branch already holds its change, and its issue,
    instead of the round that `route` was asked to start (see `close_on_base`). Its latest
    round shows the outcome. Returns lines for the log."""
    pr = decision["pr"]
    outcome, text, commit = close_on_base(gh, cfg, decision)
    rounds = _rounds(gh, pr)
    if not rounds:
        gh.comment(pr, text)
    else:
        comment, run = rounds[-1]
        run.update(outcome=outcome, status=SUCCESS if outcome == "already_on_base" else FAILURE)
        run.pop("attention", None)
        if outcome != "already_on_base":
            run["attention"] = ON_BASE_FAILED
        if commit:
            run["on_base"] = commit
        gh.update_comment(comment["id"], with_run(comment["body"], run, text))
    lines = [f"#{pr}: {outcome}."]
    if outcome == "already_on_base":
        # Issues it blocked may start now, and the scope may be done.
        lines += autopilot.request_safely(gh, cfg, workflow, ref, decision["issue"])
    return lines


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
    if latest.get("conflicts") and latest.get("outcome") == "needs_merge":
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


def _updates_used(comments):
    return len([run for _, run in run_comments(comments) if run.get("updated_branch")])


def update_branch(gh, target, row, limit, workflow, ref, before_review=None):
    """Bring the branch of pull request `target["pr"]`, at `target["head"]`, up to date
    with its base branch on GitHub, and start `/nexkit review` on the new commit, which
    carries an earlier approval over when the pull request's own change is the same. At
    most `limit` updates per pull request (`conflicts.max_rounds`): `row`, the round that
    asks, records this one. `before_review` runs once the branch is updated. Returns None,
    or why nothing started."""
    pr, head, base = target["pr"], target["head"], target["base"]
    if _updates_used(gh.comments(pr)) >= limit:
        return f"Its {limit} automatic updates of the branch from `{base}` are used up."
    try:
        gh.update_branch(pr, head)
        if before_review:
            before_review()
        for _ in range(UPDATE_TRIES):
            if gh.pull(pr)["head"]["sha"] != head:
                break
            time.sleep(UPDATE_WAIT)
        # The review carries an earlier approval over when the change is the same.
        gh.dispatch(
            workflow,
            ref,
            {"command": "review", "number": str(pr), "note": "", "auto": "true,carry"},
        )
    except GitHubError as error:
        return f"NexKit could not update the branch: {error}"
    row["updated_branch"] = True
    return None


def _job(name):
    """`verify` for `nexkit / verify (unit)`: a job of the pipeline, as the workflow that
    calls it names it, without its check group."""
    return re.sub(r" \(.*\)$", "", (name or "").rsplit(" / ", 1)[-1]).strip()


def checking(gh, url):
    """The id of the workflow run at `url` while it runs only its checks (`CHECK_JOBS`),
    or None: in any other job, queued, ended, or unreadable."""
    match = RUN_ID.search(url or "")
    if not match:
        return None
    run_id = int(match.group(1))
    try:
        run = gh.workflow_run(run_id)
        if run.get("status") != "in_progress":
            return None
        jobs = gh.attempt_jobs(run_id, run.get("run_attempt") or 1)
    except GitHubError:
        return None
    active = {_job(job.get("name")) for job in jobs if job.get("status") != "completed"}
    return run_id if active and active <= CHECK_JOBS else None


def _running_round(gh, number, issue):
    """(the issue or pull request whose comment shows it, comment, run) for the round of
    pull request `number` that runs: its latest round, or the implement round that opened
    it. None without one."""
    rounds = _rounds(gh, number)
    if rounds:
        comment, run = rounds[-1]
        return (number, comment, run) if run.get("status") == RUNNING else None
    if issue is None:
        return None
    runs = [(c, run) for c, run in run_comments(gh.comments(issue)) if run.get("command") == "go"]
    if runs and runs[-1][1].get("status") == RUNNING:
        return issue, *runs[-1]
    return None


def _behind(gh, tip, head):
    """Whether `head` lacks the base branch's tip. False when GitHub cannot say."""
    try:
        return (gh.compare(tip, head).get("behind_by") or 0) > 0
    except (GitHubError, AttributeError):
        return False


# The check jobs' artifacts of a round that NexKit stops, which hold what passed: their
# names, and the largest one read.
CHECK_PARTS = "nexkit-checks-"
PARTS_LIMIT = 20_000_000


def passed_before_stop(gh, run_id, head):
    """{check name: its command's fingerprint} for the checks of the run `run_id` that
    passed on `head`, the pull request's head, as the check jobs that finished recorded
    them in their `nexkit-checks-<group>` artifacts. Only with `parallel_checks` does a
    check job finish before the others; the one job without it records nothing until it
    ends. An artifact that checked another commit, or cannot be read, adds nothing."""
    try:
        artifacts = gh.run_artifacts(run_id)
    except GitHubError:
        return {}
    passed = {}
    for artifact in artifacts:
        if not str(artifact.get("name") or "").startswith(CHECK_PARTS):
            continue
        if artifact.get("expired") or (artifact.get("size_in_bytes") or 0) > PARTS_LIMIT:
            continue
        try:
            data = gh.artifact_zip(artifact["id"], PARTS_LIMIT)
        except (GitHubError, KeyError):
            continue
        results, checked = _check_part(data)
        if checked == head:
            passed.update(passed_checks(results))
    return dict(list(passed.items())[:MAX_PASSED])


def _check_part(data):
    """(results, commit checked) from a check job's artifact zip, or ([], None)."""
    if len(data) > PARTS_LIMIT:
        return [], None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {info.filename: info for info in archive.infolist()}
            wanted = [files.get("checks.json"), files.get("checked.json")]
            if not all(wanted) or any(info.file_size > 5_000_000 for info in wanted):
                return [], None
            results, checked = (json.loads(archive.read(info)) for info in wanted)
    except (zipfile.BadZipFile, ValueError, OSError):
        return [], None
    if not isinstance(results, list) or not isinstance(checked, dict):
        return [], None
    return results, checked.get("head")


def stop_superseded(gh, pull, base, conflicting, strict, workflow, ref, max_rounds):
    """Stop the round of `pull` that runs only its checks when the base branch moved under
    it so that they cannot lead to a merge, and start the round that can, at once:

    - its head conflicts with `base` (`conflicting`): the conflict round;
    - the rules of `base` want pull requests up to date (`strict()`), and the head lacks
      its tip: an update of the branch and `/nexkit review`, which carries an approval
      over when the change is the same.

    The stopped round's outcome is `superseded`: it counts against neither
    `fix.max_auto_rounds` nor `conflicts.max_rounds`, and its run is cancelled with the
    jobs that would follow a cancellation, so no `report` or `finish` runs for it. The
    round it starts counts as usual; when none can start, nothing is stopped. Returns a
    line, or "" when the round goes on."""
    number, head = pull["number"], pull["head"]["sha"]
    found = _running_round(gh, number, issue_from_branch(pull["head"]["ref"]))
    run_id = found and checking(gh, found[2].get("url"))
    if not run_id:
        return ""
    where, comment, run = found
    tip = gh.branch_sha(base)
    if conflicting:
        why = f"`{head[:7]}` conflicts with it"
    elif strict() and _behind(gh, tip, head):
        why = f"the rules of `{base}` want pull requests up to date, and `{head[:7]}` lacks it"
    else:
        return ""
    comments = gh.comments(number)
    rounds = [r for _, r in run_comments(comments) if "round" in r]
    if (
        conflicting
        and conflict_rounds_used([r for r in rounds if r.get("conflicts")]) >= max_rounds
    ):
        return ""
    if not conflicting and _updates_used(comments) >= max_rounds:
        return ""
    # Before the stop, so only check jobs that finished count.
    passed = passed_before_stop(gh, run_id, head)
    try:
        gh.cancel_run(run_id)
    except GitHubError as exc:
        return f"Could not stop the checks of #{number}: {exc}"
    issue_run, checked = None, {head, run.get("head") or head}
    if where != number:
        # The implement round that opened the pull request: its round shows here too.
        issue_run = run
        run = {
            "round": next_round(comments),
            "url": run.get("url"),
            "trigger": "implement",
            "action": "implement",
            "kinds": [],
            # Its checks' setup, so the checks that passed count for the next rounds.
            **({"setup": run["setup"]} if run.get("setup") else {}),
        }
    run.update(head=head, checks="not run", verdict="not run", tokens=None)
    run.pop("attention", None)
    stopped = (
        f"NexKit stopped this round's checks: `{base}` moved to `{tip[:7]}`, and {why}, so "
        "they could not lead to a merge."
    )
    if passed:
        # The next rounds give each its commit in NEXKIT_CHECK_VERIFIED_SHA.
        run["passed"] = passed
        names = ", ".join(f"`{name}`" for name in passed)
        stopped += f" Before it stopped, {names} passed on `{head[:7]}`."
    else:
        run.pop("passed", None)
    if conflicting:
        started = start_conflict_round(gh, number, base, workflow, ref, max_rounds, row=run)
        failed = "" if started.startswith("Started") else started or "No round started."
    else:
        target = {"pr": number, "head": head, "base": base}
        failed = update_branch(gh, target, run, max_rounds, workflow, ref) or ""
        started = failed or (
            f"NexKit merged `{base}` into it and started `/nexkit review` on the new commit, "
            "which checks it and keeps an earlier approval when the pull request's own change "
            "is the same."
        )
    if failed:
        # Stopped, and nothing carries the pull request on: a person starts the next round.
        run.update(status=FAILURE, outcome=STOP_FAILED, attention=STOP_FAILED_REASON)
        started = failed
    else:
        run.update(status=SUCCESS, outcome=SUPERSEDED)
    _give_back(gh, number, comments, run)
    text = f"{stopped} {started}"
    for sha in checked:
        for context in progress.STATUS_CONTEXTS:
            gh.set_status(sha, context, "error", f"Stopped: {base} moved", run.get("url"))
    progress.save_round(gh, number, None if issue_run else comment["id"], run, text)
    if issue_run:
        issue_run.update({key: run[key] for key in ("status", "outcome")})
        if failed:
            issue_run["attention"] = STOP_FAILED_REASON
        gh.update_comment(comment["id"], render_run(issue_run, f"Pull request #{number}: {text}"))
    return f"#{number}: {text}"


def _give_back(gh, number, comments, run):
    """An automatic fix round that NexKit stopped gives its `fix.max_auto_rounds` back."""
    automatic = run_action(run) == "fix" and "auto" in run_kinds(run)
    if not automatic or run.get("conflicts") or run.get("retry") or run.get("resumed"):
        return
    state_id, state = read_state(comments)
    if state and state.get("auto_fixes"):
        state["auto_fixes"] -= 1
        progress.save_state(gh, number, state_id, state)


def resolve_conflicts(gh, base, workflow, ref, max_rounds, skip=(), delay=5, settle=False):
    """Start a conflict round for each open NexKit pull request into `base` that conflicts
    with it. A round that runs only its checks on a head that can no longer merge stops
    first (see `stop_superseded`). With `settle`, a latest round whose run ended without a
    report holds nothing up (see `start_conflict_round`). Returns lines for a log or
    comment."""
    lines, rules = [], []

    def strict():
        # The rule of `base`, read once, and only for a round in its checks.
        if not rules:
            try:
                rules.append(gh.wants_up_to_date(base))
            except GitHubError as exc:
                print(f"warning: could not read the rules of `{base}`: {exc}", file=sys.stderr)
                rules.append(False)
        return rules[0]

    for pull in gh.pulls_into(base):
        number = pull["number"]
        nexkit = issue_from_branch(pull["head"]["ref"]) is not None
        if number in skip or not nexkit or pull["head"]["repo"]["full_name"] != gh.repository:
            continue
        try:
            conflicting = is_conflicting(gh, number, delay=delay)
            stopped = stop_superseded(
                gh, pull, base, conflicting, strict, workflow, ref, max_rounds
            )
            if stopped:
                lines.append(stopped)
            elif conflicting is None:
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
    if task == "on_base":
        return on_base_round(gh, cfg, decision, workflow, ref)
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
