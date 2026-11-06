"""Autopilot: work through the open issues in scope without commands.

A sweep looks at every open issue in scope and starts the one command that moves it on:
`/nexkit plan` for a ready issue, `/nexkit go` for a plan that needs nobody. Sweeps run in
the `maintain` job, one at a time in the `nexkit-autopilot` concurrency group, and start
commands with `workflow_dispatch`, like automatic rounds. They hold no agent credential and
never check out repository code. Events start them: the end of a run, an issue that opens,
closes or comes into scope, a closed milestone, a finished run on the default branch of a
workflow in `merge.after_workflows` or `autopilot.watch_workflows`; the hourly schedule
catches the rest.

Everything a sweep knows comes from GitHub: the issues in scope, their dependencies and
sub-issues, and NexKit's own markers. One record comment per issue says which command
autopilot started last, and for which state of the issue, so a command starts only once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import posixpath
import re
import string
from datetime import UTC, datetime, timedelta

from .checks import same_test, test_name
from .config import SEVERITIES, after_workflow_names, autopilot_labels, watched_workflows
from .github import TRUSTED_ASSOCIATIONS, GitHubError, alerts_off
from .progress import STATUS_CONTEXTS, save_state
from .redact import Redactor
from .route import AFTER_RUN, RUN_ID, TRUST, issue_from_branch, parse_command
from .schema import filled
from .state import (
    ACTIONS_BOT,
    BOT_LOGINS,
    CONTINUED,
    FAILURE,
    PAUSED,
    RUNNING,
    SUCCESS,
    answers_marker,
    answers_text,
    by_bot,
    ci_wait_reason,
    conflict_rounds_used,
    is_bot,
    latest_plan,
    plan_data,
    questions,
    read_state,
    round_questions,
    run_action,
    run_comments,
    run_kinds,
    with_run,
)
from .usage import PAUSE_WINDOW, iso, parse_iso

# The dispatch that starts a sweep. Its `number` input is not a number, so callers'
# concurrency groups give every sweep the group `nexkit-autopilot`: one runs at a time, and
# GitHub keeps only the latest waiting one, which sees everything the others would.
SWEEP = {"command": "autopilot", "number": "autopilot", "note": "", "auto": "false"}
# The `auto` input of the commands autopilot starts.
AUTO = "autopilot"
RECORD_PREFIX = "<!-- nexkit:autopilot "
RECORD_PATTERN = re.compile(r"<!-- nexkit:autopilot ([A-Za-z0-9+/=]+) -->\s*\Z")
DONE = "<!-- nexkit:autopilot-done -->"
# A run in these states carries on by itself, or a run is on its way.
ACTIVE = {RUNNING, PAUSED, CONTINUED}
# A run that continues a paused one and has not started after this long did not start, nor
# did the round that a round started next by itself. A command that autopilot started is
# lost sooner: once its own run ended, or after `autopilot.lost_after_minutes` when that run
# cannot be read.
LOST = timedelta(hours=6)
# What the comment of a run that ended without a report says once a sweep finds it.
REPORTLESS = "the run ended without a report"
# What the record keeps from one start to the next.
STICKY = ("split_asked", "titles_asked", "parts_asked")
# A sweep looks again after splitting an issue, at most this often.
PASSES = 3
# The note on the plan that autopilot asks for after an agent ran out of time.
SPLIT_NOTE = (
    "The last implement or fix round did not finish within its time limit. Split this issue: "
    "return `too_large` with parts that can each be done and tested in one session. Say in "
    "the summary why the round did not finish: too much work, or a failure whose cause it "
    "did not find. For too much work, put slow, mechanical work, such as test fixtures or "
    "settings in every environment, in its own part, apart from the change in behaviour. "
    "For a failure whose cause was not found, make the first part a diagnosis that finds the "
    "cause, with evidence, before any part changes code for it. Title each part by its own "
    "behaviour, never by this issue's title, and order the parts so that each leaves the "
    "default branch working."
)
# The note on the plan that autopilot asks for again when parts are titled like the issue.
TITLE_NOTE = (
    "NexKit autopilot did not create the parts of the last plan, because these titles repeat "
    "this issue's title: {titles}. Return the split again, with each part titled by its own "
    "behaviour."
)
# The note on the plan that autopilot asks for again when a plan is too large and suggests
# no parts.
PARTS_NOTE = (
    "NexKit autopilot could not split this issue, because the last plan says it is too large "
    "for one session but suggests no parts. Return `too_large` with parts that can each be "
    "done and tested in one session, each titled by its own behaviour, and order them so "
    "that each leaves the default branch working. If the work fits one session after all, "
    "return a plan that is not too large."
)
PART = re.compile(r"\A<!-- nexkit:part (\d+) -->")
# How deep parts go: the issue first split is at depth 0, its parts at 1, their parts at 2.
# An issue at this depth is not split again; a person decides.
MAX_DEPTH = 3
DEPTH_REASON = f"it is a part {MAX_DEPTH} levels deep, which autopilot splits no further"
# The most parent issues that a sweep walks up from an issue: GitHub's own nesting limit.
LINEAGE = 8
# A bug issue that autopilot opened because a workflow failed twice on the default branch,
# and the commit each of its reports is about.
# The workflow file and the run that opened an issue, at the start of its text, where
# nothing the workflow quotes can be.
WORKFLOW_ISSUE = re.compile(r"\A<!-- nexkit:workflow ([A-Za-z0-9_.-]+) (\d+) -->")
# What may start a run whose issue autopilot takes; a dispatch only by a writer or NexKit.
WORKFLOW_EVENTS = ("schedule", "push", "workflow_dispatch")
CI_BUG_PREFIX = "<!-- nexkit:ci "
CI_BUG = re.compile(r"\A<!-- nexkit:ci ([A-Za-z0-9_.-]+) -->")
CI_SHA_PREFIX = "<!-- nexkit:ci-sha "
CI_SHA = re.compile(r"<!-- nexkit:ci-sha ([0-9a-f]+) -->")
# What a red branch's report records of the latest failed attempt of its run: the run, and
# for each failed job the commands of its `run` steps that failed, from the job's log, and
# the tests its error annotations name (see `checks.test_name`). A pull request whose
# failing checks run only those commands, and fail only those tests, waits for the bug
# issue instead of a fix round. A report from before NexKit names no tests.
CI_FAILED_PREFIX = "<!-- nexkit:ci-failed "
CI_FAILED = re.compile(r"<!-- nexkit:ci-failed ([A-Za-z0-9+/=]+) -->")
# How a job's log starts a step, with the first line of the step's command, and how it ends
# a step whose command failed or ran out of time.
LOG_STEP = re.compile(r"##\[group\]Run (.*)")
LOG_FAILED = re.compile(
    r"##\[error\](?:Process completed with exit code [1-9]|The action .* has timed out)"
)
# An issue that autopilot opened for one failure in the runs of a workflow on the default
# branch that passed only when run again, or that failed and a newer run overtook. Its
# marker names the workflow and a key for the failure. One from NexKit 4.3.0 names only the
# workflow and holds every failure of its runs; it gets no new records.
CI_FLAKY_PREFIX = "<!-- nexkit:ci-flaky "
CI_FLAKY = re.compile(r"\A<!-- nexkit:ci-flaky ([A-Za-z0-9_.-]+)(?: ([0-9a-f]{16}))? -->")
# Error annotations of a failed job name its failures: test reporters title them with the
# test's name and point them at its file and line. The runner's own annotation, such as
# this one, points at the workflow and names nothing.
CI_GENERIC = re.compile(r"Process completed with exit code \d+")
# A run whose failures would open more issues than this records those failures on one issue
# for the run instead: so many at once have one cause, such as a broken runner.
CI_NEW_ISSUES = 5
# GitHub's longest issue title. A CI failure's title keeps its whole name and is cut, at a
# word boundary, only past this.
CI_TITLE = 256
# A test reporter's annotation title names a test by its path of describe blocks, such as
# "OrderHistory > pages through more than twenty orders"; the last segment is the test.
CI_TEST_SEGMENT = re.compile(r" (?:›|>) ")
# The most failures a red branch's title names one by one.
CI_TITLE_FAILURES = 3
# The run attempts that a bug or flaky-run report records, where it ends the text, so each
# run and attempt is recorded once.
CI_RUNS_PREFIX = "<!-- nexkit:ci-runs "
CI_RUNS = re.compile(r"<!-- nexkit:ci-runs ([0-9. ]+) -->\s*\Z")
# The runs of each workflow a sweep looks at: one page, newest first, of which the latest
# run, those created in the last day and those a bug issue reports. Sweeps run at least
# hourly, so the day covers missed sweeps; the records say what is new.
CI_RUNS_LIMIT = 100
CI_LOOK_BACK = timedelta(days=1)
# The most issues autopilot opened that a sweep reads at once: the open ones, and those
# updated within `CI_LOOK_BACK`.
ISSUES_LIMIT = 1000
# An issue that autopilot opened for a Dependabot advisory: at the start of its text, the
# advisory's id and the numbers of the alerts the issue covers.
DEPENDABOT_PREFIX = "<!-- nexkit:dependabot "
DEPENDABOT = re.compile(r"\A<!-- nexkit:dependabot (GHSA[a-z0-9-]+) ([0-9,]+) -->")
# A part of a split of such an issue: on the line after its part marker, so that it stays in
# scope without a milestone, as the issue does. No text of an agent comes before it.
DEPENDABOT_PART_PREFIX = "<!-- nexkit:dependabot-part "
DEPENDABOT_PART = re.compile(
    r"\A<!-- nexkit:part \d+ -->\n<!-- nexkit:dependabot-part (GHSA[a-z0-9-]+) -->"
)
GHSA_ID = re.compile(r"GHSA(?:-[a-z0-9]{4}){3}")
# The most issues for advisories that a sweep opens, so that the first sweep on a repository
# with old alerts stays short: the next sweeps open the rest.
DEPENDABOT_NEW_ISSUES = 5
# What marks the comment on a closed issue whose advisory still has open alerts, and how long
# after it closed that comment waits: an alert closes some time after the fix merges.
DEPENDABOT_STILL_OPEN = "<!-- nexkit:dependabot-open -->"
DEPENDABOT_SETTLE = timedelta(days=1)
# Why a sweep could not read alerts stays on the default branch's tip, where `nexkit status`
# and `nexkit doctor` read it: only the sweep's token can tell, and it holds nothing else.
DEPENDABOT_CONTEXT = "nexkit/dependabot"
NO_ALERTS_PERMISSION = "The App may not read alerts: grant it Dependabot alerts: read"
ALERTS_TURNED_OFF = "Dependabot alerts are turned off for this repository"
FAILED = {"failure", "timed_out"}
# Events whose runs test a pull request rather than the branch they name.
PULL_REQUEST_EVENTS = {"pull_request", "pull_request_target"}
# Outcomes after which a run already started the next one by itself.
# A round NexKit started, or a merge, stopped because someone without write access edited a
# vouched issue. A new vouch starts it again.
UNTRUSTED_ROUND = "agent_untrusted"
UNTRUSTED_MERGE = "merge_untrusted"
CHAINED = {"auto_fix", "conflict_fix", "retry", "branch_updated", "head_moved", "superseded"}
# The outcome of a round whose checks fail as the default branch does while a bug issue
# for that failure is open: no fix round starts until the default branch passes again.
CI_WAITING = "ci_waiting"
# The outcome of a round whose issue a label holds (see `held`): its `held_back` says what
# it did not start, "fix", "merge" or "conflicts", which a sweep starts once no label holds
# the issue.
HELD = "held"
# The `mergeable_state` of a pull request that GitHub would merge: a check that it does not
# require may fail (`unstable`), or hooks may run (`has_hooks`).
MERGEABLE_STATES = ("clean", "unstable", "has_hooks")


def takes(cfg):
    return bool(cfg.get("autopilot", {}).get("enabled"))


def sweeps(cfg):
    """Whether NexKit sweeps at all: for autopilot, or to start the conflict rounds of
    `conflicts.auto_resolve` (see `maintain.resolve_conflicts`)."""
    return takes(cfg) or bool(cfg.get("conflicts", {}).get("auto_resolve"))


def request(gh, workflow, ref, closed=None, after=None):
    """Start a sweep. `closed` is the issue whose closing started it, for the log; every
    sweep says when a milestone is done (see `done`). `after` is a workflow run that the
    sweep waits for to end first."""
    note = str(closed) if closed else AFTER_RUN.format(after) if after else ""
    gh.dispatch(workflow, ref, {**SWEEP, "note": note})


def request_safely(gh, cfg, workflow, ref, closed=None, after=None, gate=takes):
    """`request` for a run that must not fail because of it: errors become a line. Only
    when `gate(cfg)`: autopilot is on, or with `sweeps`, conflict rounds start by themselves
    too."""
    if not gate(cfg):
        return []
    try:
        request(gh, workflow, ref, closed, after)
    except GitHubError as exc:
        return [f"Could not start an autopilot sweep: {exc}"]
    return ["Started an autopilot sweep."]


def after_run(gh, cfg, decision, outcome, workflow, ref):
    """Start a sweep after a run, unless it waits for a usage limit, or it already started
    the next one itself and no finished run on the default branch `ref` needs a sweep. A
    merge closed the issue, which may empty the scope. With `conflicts.auto_resolve`, the
    sweep also starts the conflict rounds that were held back while a round ran."""
    if not sweeps(cfg) or outcome == "paused":
        return []
    if outcome in CHAINED and not (takes(cfg) and ci_due(gh, cfg, ref)):
        return []
    closed = decision["issue"] if outcome in ("merged", "no_change", "already_on_base") else None
    return request_safely(gh, cfg, workflow, ref, closed, gate=sweeps)


def after_unreported(gh, cfg, workflow, ref, run_id):
    """Start a sweep at the end of a run whose `report` did not finish, for example
    because its runner was lost or the run was cancelled: the sweep waits for the run to
    end, then handles its comment that still says running, as for any run that ended
    without a report, and starts the conflict round held back while it ran."""
    return request_safely(gh, cfg, workflow, ref, after=run_id, gate=sweeps)


def after_ci_run(gh, cfg, decision, workflow, ref):
    """Start a sweep after a run of another workflow finished, when it is a run of a
    workflow that sweeps watch (`watched_workflows`) on the default branch `ref` that a
    sweep acts on. A run whose event never came is noticed at the end of the next NexKit
    run (`after_run`)."""
    name = decision.get("workflow")
    if name not in watched_workflows(cfg):
        return [f"`{name}` is in neither merge.after_workflows nor autopilot.watch_workflows."]
    try:
        run = gh.workflow_run(int(decision["run"]))
        if run.get("head_branch") != ref or not on_default_branch(gh, run):
            return [f"The run of `{name}` did not test `{ref}`."]
        if not _acts_on(run, lambda: name in open_bugs(gh)):
            return [f"`{name}` passed on `{ref}`; a sweep has nothing to do for it."]
    except (GitHubError, KeyError, TypeError, ValueError) as exc:
        return [f"Could not read the run of `{name}`: {exc}"]
    return request_safely(gh, cfg, workflow, ref)


# -- scope -------------------------------------------------------------------------


def milestones(gh):
    """The open milestones in the order autopilot takes them: by due date, then the
    oldest."""
    found = list(gh.milestones())
    found.sort(key=lambda m: (m.get("due_on") is None, m.get("due_on") or "", m["number"]))
    return found


def current_milestone(gh):
    """The earliest open milestone that has issues, in the order of `milestones`. Empty
    milestones are skipped; one whose issues are all closed stays current until a person
    closes it, so autopilot never moves on to the next milestone by itself."""
    found = [
        m for m in milestones(gh) if (m.get("open_issues") or 0) + (m.get("closed_issues") or 0)
    ]
    return found[0] if found else None


def locate(gh, cfg):
    """The scope's name for people and its milestone. `every` is the `null` scope, which
    holds every open issue of this repository, with `milestones` for its order. Without an
    open milestone, the milestone scope holds nothing but the issues for advisories, which
    have no milestone: with `dependabot`, they are in scope anyway."""
    alerts = bool(cfg["autopilot"]["dependabot"])
    return {
        **_located(gh, cfg),
        "dependabot": alerts,
        "repository": gh.repository,
        "priority": list(cfg["autopilot"]["priority"]),
    }


def _located(gh, cfg):
    if cfg["autopilot"]["scope"] is None:
        return {
            "name": "every open issue",
            "every": True,
            "milestone": None,
            "milestones": milestones(gh),
        }
    milestone = current_milestone(gh)
    if not milestone:
        return {"name": "no open milestone", "every": False, "milestone": None}
    return {
        "name": f"milestone {milestone['title']}",
        "every": False,
        "milestone": milestone["number"],
        "milestones": [milestone],
    }


def scope(gh, cfg):
    """`locate` with the open issues in scope, in order: those with a label of
    `autopilot.priority` first, in its order, by the highest such label each has; then,
    with `null`, the issues of each open milestone in the order of `milestones`, then the
    others; each group oldest first. With the milestone scope, an issue with a label of
    `autopilot.priority` is in scope wherever it is."""
    where = locate(gh, cfg)
    if where["every"]:
        items = gh.scoped_issues()
    elif where["milestone"] is not None:
        items = gh.scoped_issues(milestone=where["milestone"])
    else:
        items = []
    issues = [i for i in items if "pull_request" not in i]
    if not where["every"]:
        known = {issue["number"] for issue in issues}
        # GitHub finds labels whatever their case; NexKit compares them exactly.
        extra = [
            item
            for label in where["priority"]
            for item in gh.labeled_issues(label)
            if _labeled(item, label)
        ]
        if where["dependabot"]:
            extra += [issue for issue in nexkit_issues(gh, "open") if advisory_work(issue)]
        for issue in extra:
            if "pull_request" not in issue and issue["number"] not in known:
                known.add(issue["number"])
                issues.append(issue)
    # An issue whose milestone is closed goes with those without one.
    order = {m["number"]: index for index, m in enumerate(where.get("milestones") or [])}
    issues.sort(
        key=lambda i: (
            priority_rank(where["priority"], i),
            order.get((i.get("milestone") or {}).get("number"), len(order)),
            i["number"],
        )
    )
    return {**where, "issues": issues}


# The labels of the issues NexKit opens: for a CI failure on the default branch, for a
# Dependabot advisory, and for a failure of flaky runs. `autopilot.priority` lists them by
# default, so these issues go first, in this order.
CI_LABEL, DEPENDABOT_LABEL, FLAKY_LABEL = "nexkit:ci", "nexkit:dependabot", "nexkit:flaky"
OWN_LABELS = (CI_LABEL, DEPENDABOT_LABEL, FLAKY_LABEL)


def own_label(issue):
    """The label of an issue that NexKit opened for a CI failure, an advisory or flaky
    runs, by its marker, or None. Its marker, not the label, makes NexKit trust it."""
    if ci_workflow(issue) is not None:
        return CI_LABEL
    if dependabot_advisory(issue) is not None:
        return DEPENDABOT_LABEL
    return FLAKY_LABEL if flaky_workflow(issue) is not None else None


def labels_of(issue):
    """The names of an issue's labels, with the label its marker gives it (`own_label`),
    which a sweep adds when the issue lacks it."""
    names = {item.get("name") for item in issue.get("labels") or [] if item.get("name")}
    own = own_label(issue)
    return names | {own} if own else names


def priority_rank(priority, issue):
    """Where an issue goes in `autopilot.priority`, `priority`: the place of its highest
    label there, or after every listed label."""
    names = labels_of(issue)
    return next((index for index, label in enumerate(priority) if label in names), len(priority))


def taken(gh, cfg, issue):
    """Whether autopilot works on an issue: in scope, from someone it trusts."""
    return in_scope(locate(gh, cfg), issue) and trusted(gh, issue, cfg)


def in_scope(where, issue):
    """Whether an issue, open or closed, belongs to the scope. With `null` every issue of
    this repository does; a blocker can be in another repository. The issues for advisories
    and their parts, and those with a label of `autopilot.priority`, are in every scope."""
    url = issue.get("repository_url")
    here = not url or url.lower().endswith(f"/repos/{where['repository']}".lower())
    if where["every"]:
        return here
    if where["dependabot"] and here and advisory_work(issue):
        return True
    if here and labels_of(issue) & set(where.get("priority") or ()):
        return True
    milestone = issue.get("milestone") or {}
    return where["milestone"] is not None and milestone.get("number") == where["milestone"]


def author(issue):
    return (issue.get("user") or {}).get("login") or ""


def trusted(gh, issue, cfg=None):
    return distrust(gh, issue, cfg) is None


def distrust(gh, issue, cfg=None, level=0):
    """Why autopilot leaves an issue alone, or None. It skips a person's approval of the
    plan, so issue text drives an implement session. It takes issues opened by someone with
    write access, since only they and other writers can edit them, the issues NexKit opened
    as its App, those a trusted workflow opened as the App or the Actions bot, and any other
    that a writer vouched for. `level` counts the parents that `_trusted_part` walked up."""
    problem = _source_problem(gh, issue, cfg, level)
    return problem and _vouch_problem(gh, issue, problem)


def vouch(gh, issue):
    """The latest `/nexkit trust` comment on an issue by someone who can write now, or None.
    The comment itself is the record: only its author and writers can change it, and no
    workflow can post it as a person, so what the Actions bot or text inside another
    comment says counts for nothing."""
    for comment in reversed(gh.comments(issue["number"])):
        user = comment.get("user") or {}
        login = user.get("login") or ""
        if not login or user.get("type") == "Bot" or by_bot(comment):
            continue
        if parse_command(comment.get("body")) == (TRUST, "") and gh.can_write(login):
            return comment
    return None


def _vouch_problem(gh, issue, problem):
    """`problem`, why autopilot leaves an issue alone, without a writer's vouch; with one,
    who changed the issue's title or text since who may not, or None. What the writer read
    is what they vouched for, so an earlier edit counts for nothing, and only writers and
    NexKit's App may change it after: a bot here is any workflow of the repository."""
    comment = vouch(gh, issue)
    return _edit_since(gh, issue, comment) if comment else problem


def edited_after_vouch(gh, issue):
    """Who changed an issue after a writer vouched for it who may not, or None, also without
    a vouch. A sweep decides only when a command starts, and the author of a vouched issue
    can still edit it, so what acts on its text later asks again: a round started by NexKit
    just after it read the text (`context.gather`), and a merge."""
    comment = vouch(gh, issue)
    return _edit_since(gh, issue, comment) if comment else None


def _vouched_again(gh, issue, comment):
    """Whether a writer vouched for `issue` after the comment of the round that stopped."""
    latest = vouch(gh, issue)
    return latest is not None and latest["id"] > comment["id"]


def _edit_since(gh, issue, comment):
    """Who changed the issue since the vouch `comment` who may not, or None."""
    since = parse_iso(comment.get("created_at"))
    writer = comment["user"]["login"]
    for login, at in gh.issue_edits(issue["number"]):
        edited = parse_iso(at)
        if edited is not None and since is not None and edited < since:
            continue
        if is_bot(login) or (login and gh.can_write(login)):
            continue
        who = f"@{login}" if login else "someone GitHub cannot name"
        return f"{who} edited it without write access after @{writer} vouched for it"
    return None


def _source_problem(gh, issue, cfg, level):
    """Why who opened an issue does not make autopilot take it, or None."""
    login = author(issue)
    if is_bot(login) and (own_label(issue) or _trusted_part(gh, issue, cfg, level)):
        return None
    if is_bot(login) or login == ACTIONS_BOT:
        return workflow_problem(gh, issue, cfg)
    return None if login and gh.can_write(login) else _no_writer(login)


def _no_writer(login):
    return f"opened by @{login or 'an unknown user'}, who cannot write to the repository"


def workflow_problem(gh, issue, cfg):
    """Why an issue that a bot opened is not one that autopilot.trusted_workflows vouches
    for, or None. Its marker names the run that opened it; GitHub's record of that run
    proves the claim, and `github-actions[bot]` alone proves nothing, since any workflow of
    the repository, also on a pull request's branch, can write as it."""
    listed = cfg["autopilot"]["trusted_workflows"] if cfg else []
    if not listed:
        return _no_writer(author(issue))
    match = WORKFLOW_ISSUE.match(issue.get("body") or "")
    if not match:
        return f"opened by @{author(issue)}, without the marker of a trusted workflow"
    name, run_id = match.group(1), int(match.group(2))
    if name not in listed:
        return f"its marker names `{name}`, which autopilot.trusted_workflows does not list"
    try:
        run = gh.workflow_run(run_id)
    except GitHubError as exc:
        if exc.status != 404:
            raise
        return f"its marker names run {run_id}, which does not exist"
    return _run_problem(gh, cfg, issue, name, run) or _edit_problem(gh, issue)


def _run_problem(gh, cfg, issue, name, run):
    """Why the run `run` did not open this issue as a trusted workflow, or None."""
    number = run.get("id")
    found = posixpath.basename(str(run.get("path") or "").split("@")[0])
    if found != name:
        return f"its marker names `{name}`, but run {number} is a run of `{found}`"
    branch = gh.default_branch()
    if run.get("head_branch") != branch or not on_default_branch(gh, run):
        return f"run {number} of `{name}` did not run on `{branch}`"
    event = run.get("event")
    if event not in WORKFLOW_EVENTS:
        return f"run {number} of `{name}` was started by `{event}`"
    actor = (run.get("triggering_actor") or run.get("actor") or {}).get("login") or ""
    if event == "workflow_dispatch" and not _may_dispatch(gh, cfg, actor, name):
        return f"run {number} of `{name}` was started by @{actor or 'an unknown user'}"
    started, opened = parse_iso(run.get("created_at")), parse_iso(issue.get("created_at"))
    ended = parse_iso(run.get("updated_at")) if run.get("status") == "completed" else None
    if not started or not opened or opened < started or (ended and opened > ended):
        return f"run {number} of `{name}` was not running when this issue was opened"
    return None


def _may_dispatch(gh, cfg, actor, name):
    """Whether a dispatch by `actor` can start a run whose issue autopilot takes: a writer's,
    or NexKit's own of a workflow in `merge.after_workflows`."""
    if is_bot(actor):
        return name in after_workflow_names(cfg)
    return bool(actor) and gh.can_write(actor)


def _edit_problem(gh, issue):
    """Who changed the issue who may not, or None. A bot is the workflow that opened it, or
    another of the repository's workflows, which nothing tells apart."""
    for login in gh.issue_editors(issue["number"]):
        bot = is_bot(login) or login == ACTIONS_BOT
        if not bot and not (login and gh.can_write(login)):
            who = f"@{login}" if login else "someone GitHub cannot name"
            return f"{who} edited it without write access to the repository"
    return None


def _trusted_part(gh, issue, cfg=None, level=0):
    """A part that autopilot created: it starts with the part marker, and the issue it
    names is its parent, which autopilot takes itself. So a part inherits a writer's vouch
    for its parent, and loses it with its parent."""
    match = PART.match(issue.get("body") or "")
    if not match or level > 5:
        return False
    parent = gh.parent_issue(issue["number"])
    if not parent or parent["number"] != int(match.group(1)) or not in_repository(gh, parent):
        return False
    return distrust(gh, parent, cfg, level + 1) is None


def in_repository(gh, issue):
    """Whether an issue from the sub-issues API belongs to this repository: a parent can be
    in another repository of the same owner, with a number that means another issue here."""
    url = issue.get("repository_url")
    return not url or url.lower().endswith(f"/repos/{gh.repository}".lower())


def lineage(gh, issue):
    """The issues above an issue in this repository, nearest first: its parent, the
    parent's parent, and so on."""
    found, seen, number = [], {issue["number"]}, issue["number"]
    for _ in range(LINEAGE):
        parent = gh.parent_issue(number)
        if not parent or not in_repository(gh, parent) or parent["number"] in seen:
            break
        found.append(parent)
        seen.add(parent["number"])
        number = parent["number"]
    return found


def depth(issue, above):
    """How many levels of parts an issue is below the issue first split, given its
    `lineage`: 0 for an issue that is not a part that autopilot created."""
    level, child = 0, issue
    for parent in above:
        match = PART.match(child.get("body") or "")
        if not match or not is_bot(author(child)) or int(match.group(1)) != parent["number"]:
            break
        level, child = level + 1, parent
    return level


def _bare(title):
    return (title or "").strip().rstrip(string.punctuation + string.whitespace).casefold()


def repeated_titles(issue, parts):
    """The titles of parts that are the issue's own title or start with it, ignoring case,
    spaces and trailing punctuation. A part is titled by its own behaviour."""
    own = _bare(issue.get("title"))
    if not own:
        return []
    return [part.get("title") or "" for part in parts if _bare(part.get("title")).startswith(own)]


def waits_for(gh, issue, where=None):
    """(why an issue is not ready yet, whether a person must act), or None: open issues
    that block it on GitHub. The order of sub-issues does not count; a part that needs
    another says so with a *blocked by* relationship. Autopilot works on the blockers in
    its scope by itself; one outside it waits for a person."""
    # By number: GitHub's REST and GraphQL list them in different orders.
    blockers = sorted(
        (b for b in gh.blocked_by(issue["number"]) if b.get("state") == "open"),
        key=lambda b: b["number"],
    )
    if not blockers:
        return None
    reason = "waits for " + ", ".join(f"#{b['number']}" for b in blockers)
    outside = [b for b in blockers if where is not None and not in_scope(where, b)]
    if outside:
        listed = ", ".join(f"#{b['number']}" for b in outside)
        it, verb = ("it", "is") if len(outside) == 1 else ("them", "are")
        why = f"it {reason}, and {listed} {verb} outside autopilot's scope, where it does no work"
        do = f"finish {listed}, bring {it} into the scope, or remove the *blocked by* mark"
        return person(do, why), True
    return reason, False


def _labeled(issue, label):
    return any(item.get("name") == label for item in issue.get("labels") or [])


def wave(cfg, issue):
    """(the issue's wave, the label that sets it): the highest `wave` of its labels in
    `autopilot.labels`, or (0, None) when none sets one."""
    found = [
        (does["wave"], name)
        for name, does in autopilot_labels(cfg).items()
        if "wave" in does and _labeled(issue, name)
    ]
    return max(found, default=(0, None))


def hold_label(cfg, issue):
    """The label of `autopilot.labels` that holds the issue, or None."""
    return next(
        (
            name
            for name, does in autopilot_labels(cfg).items()
            if does.get("hold") and _labeled(issue, name)
        ),
        None,
    )


def held(gh, cfg, number):
    """The label that holds issue `number` back, when autopilot takes it, so that its
    sweeps start what was held back once the label is removed; otherwise None."""
    if not takes(cfg) or not any(d.get("hold") for d in autopilot_labels(cfg).values()):
        return None
    issue = gh.issue(number)
    label = hold_label(cfg, issue)
    return label if label and taken(gh, cfg, issue) else None


def held_reason(label):
    return f"on hold (`{label}`): autopilot starts nothing for it until the label is removed"


def _no_milestone(level, label):
    return person(
        f"give it a milestone, or take the label `{label}` off",
        f"it runs in wave {level} of its milestone (`{label}`), but has no milestone",
    )


def _ancestors(gh, number):
    """The issues above `number` in its sub-issues, nearest first. A part has the labels of
    the issue it came from, and that issue stays open until the part closes."""
    found = []
    parent = gh.parent_issue(number)
    while parent and parent["number"] not in found:
        found.append(parent["number"])
        parent = gh.parent_issue(parent["number"])
    return found


def _needing(gh, numbers):
    """The open issues that wait for `numbers` through *blocked by* marks, at any depth."""
    found, todo = set(), list(numbers)
    while todo:
        for issue in gh.blocking(todo.pop()):
            if issue.get("state") == "open" and issue["number"] not in found:
                found.add(issue["number"])
                todo.append(issue["number"])
    return found


def lower_waves(gh, cfg, issue):
    """(why an issue of a wave above 0 is not ready yet, whether a person must act), or
    None. It waits for every open issue of a lower wave in its milestone, read now, so an
    issue opened later and the parts of a split count without a mark; issues of its own
    wave run beside it. So that nothing waits in a circle, it does not wait for an issue
    above it, or for one that a person marked as needing it or an issue above it."""
    level, label = wave(cfg, issue)
    if not level:
        return None
    milestone = (issue.get("milestone") or {}).get("number")
    if milestone is None:
        return _no_milestone(level, label), True
    number = issue["number"]
    lower = [
        other
        for other in gh.scoped_issues(milestone=milestone)
        if "pull_request" not in other and other["number"] != number and wave(cfg, other)[0] < level
    ]
    if not lower:
        return None
    above = _ancestors(gh, number)
    # So that nothing waits in a circle, no issue waits for a lower wave that waits, through
    # *blocked by* marks, for it, an issue above it, or any issue of its wave or a higher
    # one in its milestone, which itself waits for that lower wave.
    level_up = [
        other["number"]
        for other in gh.scoped_issues(milestone=milestone)
        if "pull_request" not in other and wave(cfg, other)[0] >= level
    ]
    exempt = {*above, *_needing(gh, [number, *above, *level_up])}
    rest = [f"#{other['number']}" for other in lower if other["number"] not in exempt]
    if not rest:
        return None
    listed = ", ".join(rest)
    return f"it is in wave {level} (`{label}`), so it waits for {listed} of lower waves", False


def left_out(gh, cfg):
    """Steps for the open issues with a wave that have no milestone and that the milestone
    scope does not reach, so `nexkit status` can say why nothing happens."""
    if cfg["autopilot"]["scope"] is None:
        return []
    where, found = locate(gh, cfg), {}
    for name, does in autopilot_labels(cfg).items():
        if not does.get("wave"):
            continue
        for issue in gh.labeled_without_milestone(name):
            if not in_scope(where, issue):
                found.setdefault(issue["number"], issue)
    steps = []
    for issue in sorted(found.values(), key=lambda i: i["number"]):
        reason = _no_milestone(*wave(cfg, issue))
        steps.append(_step(issue, "waits", reason, person=False))
    return steps


# -- the record of what autopilot started -------------------------------------------


def find_record(comments):
    """(comment, record) for autopilot's record comment on an issue, or (None, {})."""
    for comment in reversed(comments):
        if by_bot(comment):
            match = RECORD_PATTERN.search(comment.get("body") or "")
            if match:
                return comment, json.loads(base64.b64decode(match.group(1)))
    return None, {}


def record_marker(record):
    data = base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode()
    return f"{RECORD_PREFIX}{data} -->"


def basis(issue_comments, pull_comments=()):
    """The state a command starts from: the latest NexKit run comment on the issue and on
    its pull request. A run that starts posts a new one, so a command that autopilot
    started is not started again until it ran."""
    latest = [
        max((c["id"] for c, _ in run_comments(found)), default=0)
        for found in (issue_comments, pull_comments)
    ]
    return latest


def _commented_after(comments, since):
    """Collaborators' comments after the comment `since`, such as the latest plan or the
    run comment of a round, other than commands: changes that the plan or the round did
    not hold."""
    at = next(i for i, c in enumerate(comments) if c["id"] == since["id"])
    return [
        c
        for c in comments[at + 1 :]
        if c.get("author_association") in TRUSTED_ASSOCIATIONS
        and not by_bot(c)
        and parse_command(c.get("body"))[0] is None
    ]


# -- one issue -------------------------------------------------------------------


def _step(issue, state, reason="", **fields):
    """One issue's step. A step that waits waits for a person, unless `person` is False:
    for another issue, or for a place within `max_parallel`."""
    if state == "waits":
        fields.setdefault("person", True)
    return {
        "number": issue["number"],
        "title": issue.get("title", ""),
        "state": state,
        "reason": reason,
        **fields,
    }


def _lost(gh, cfg, record, now):
    """Whether a command that autopilot started, which has posted no run comment since,
    never ran: its workflow run, which the dispatch named, ended, as when `route` refused
    it or a newer run in its concurrency group replaced it in the queue; or, when that run
    cannot be read, it started more than `autopilot.lost_after_minutes` ago. A run that is
    queued or running is on its way."""
    at = parse_iso(record.get("at"))
    if at is None:
        return False
    status = _run_status(gh, record.get("url"))
    if status == "completed":
        return True
    wait = timedelta(minutes=cfg["autopilot"]["lost_after_minutes"])
    return status is None and now - at > wait


def _run_status(gh, url):
    """The status of the workflow run at `url`, such as `queued` or `completed`, or None
    when there is no such run or it cannot be read."""
    match = RUN_ID.search(url or "")
    if not match:
        return None
    try:
        return gh.workflow_run(int(match.group(1))).get("status")
    except GitHubError:
        return None


def answers(cfg, items):
    """[{question, answer}] when `autopilot.answer` is "recommended" and every question
    has a recommended option, otherwise None."""
    if cfg["autopilot"]["answer"] != "recommended":
        return None
    found = questions(items)
    if not found or not all(item["recommended"] for item in found):
        return None
    return [{"question": item["question"], "answer": item["recommended"]} for item in found]


# The note on the fix round that follows a revised plan after a round ran out of time.
REPLANNED_NOTE = (
    "The last round did not finish within its time limit, and the plan was revised since. "
    "Follow the revised plan."
)
# Outcomes of runs that changed nothing about an issue: they only ran a command once more,
# found nothing to merge, or ended without a report.
UNCHANGED = {"retry", "lost", "up_to_date"}
# What every comment that mentions the people in `notify` says before its reason.
NEEDED = "a person is needed: "


def person(do, why):
    """Why a person is needed, as a mention and `nexkit status` say it: what to do, then
    why NexKit does not do it itself, each in plain words."""
    do, why = do.strip().rstrip("."), why.strip().rstrip(".")
    return f"{do}. {why[:1].upper()}{why[1:]}"


def quoted(text, size=150):
    """Text that an agent, GitHub or a person wrote, for a reason: in a code span on one
    line, where it can mention nobody and close no comment."""
    return "`" + _plain(text, size).replace("<", "‹").replace(">", "›") + "`"


# What ends the wait of an issue that autopilot leaves alone.
TRUST_DO = f"read the issue, and comment `/nexkit {TRUST}` on it if you trust it"
UNTRUSTED_REASON = person(
    TRUST_DO,
    "someone without write access edited it after a writer vouched for it, so NexKit does "
    "not act on its text",
)
REPEATED = person(
    "answer the questions in a comment, with what is new in them rather than the same option",
    "the round after autopilot's answers asked again, and nobody commented since",
)
# Why an issue waits after its pull request's round stopped without a report, or after the
# run it continues or the round it started next did not run.
LOST_DO = "read the workflow run in the Actions tab for why, then post the command once more"
SPLIT_DO = (
    "split the issue by hand into parts that each fit one session, with the `request` skill, "
    "or make it smaller and comment the change on it"
)
TIMED_OUT_DO = (
    "read the plan, the run log and the result files for why the round did not finish, then "
    "comment `/nexkit plan <the cause, with the evidence, and the smallest change that "
    "removes it>`"
)
QUESTIONS_REASON = person(
    "answer the plan's questions with `/nexkit plan <answers>`",
    "the plan asks for decisions that are the owner's to make",
)


def failed_reason(command):
    """Why an issue waits after its `/nexkit <command>` failed, when the run said no more."""
    return person(
        f"read the run's comment and log for why `/nexkit {command}` failed, then post it again",
        f"the last `/nexkit {command}` failed",
    )


def protected_reason(paths):
    """Why an issue waits when its change needs protected paths, which only a person
    changes."""
    # The paths come from an agent: quoted, so they mention nobody and fake no marker.
    listed = ", ".join(quoted(path) for path in paths[:5])
    more = f" and {len(paths) - 5} more" if len(paths) > 5 else ""
    return person(
        f"make the change in the protected paths ({listed}{more}) by hand, as the plan says",
        "only a person changes protected paths",
    )


def _by_person(comment):
    return comment.get("author_association") in TRUSTED_ASSOCIATIONS and not by_bot(comment)


def asked_again(target_comments, other_comments=()):
    """Whether the latest run on a target, the issue or the pull request a command ran on,
    is the first run after autopilot answered there, with no comment by a person since that
    answer on the issue or its pull request. Its questions then follow from autopilot's own
    answers: another answer would only change the wording of the note, so a person decides.
    """
    found = [(comment["id"], run) for comment, run in run_comments(target_comments)]
    if not found:
        return False
    latest = found[-1][0]
    answered = [
        c["id"] for c in target_comments if answers_text(c) is not None and c["id"] < latest
    ]
    between = [run for at, run in found if answered and answered[-1] < at < latest]
    if not answered or any(run.get("outcome") not in UNCHANGED for run in between):
        return False
    since = answered[-1]
    return not any(_by_person(c) and c["id"] > since for c in [*target_comments, *other_comments])


def closes(run):
    """Whether a blocked round found that no change is needed and recommends closing the
    issue with that finding. A finding alone is not enough: an agent may use `no_change` to
    say there is none, and recommend waiting or more work."""
    found = questions(run.get("questions"))
    return len(found) == 1 and found[0].get("closes", False) and bool(filled(run.get("no_change")))


def answer_note(found):
    lines = ["Answers for the owner, each the option NexKit recommended:"]
    for number, item in enumerate(found, 1):
        lines.append(f"{number}. {item['question']} Answer: {item['answer']}")
    return "\n".join(lines)


def split_refused(gh, issue):
    """Why autopilot does not ask for a split after an agent of this issue ran out of time,
    although the issue itself did not ask before, or None. A part inherits the time-outs of
    the issues above it: once one of them asked, a part that runs out of time again waits
    for a person, however deep it is. A part at `MAX_DEPTH` is not split at all."""
    above = lineage(gh, issue)
    for parent in above:
        if find_record(gh.comments(parent["number"]))[1].get("split_asked"):
            number = parent["number"]
            why = f"a part of a split ran out of time again, after #{number} asked for a split"
            return person(TIMED_OUT_DO, why)
    return person(TIMED_OUT_DO, DEPTH_REASON) if depth(issue, above) >= MAX_DEPTH else None


def _stopped(gh, cfg, issue, run, record, start, command, target, comments):
    """The step after a `/nexkit go` or a round that stopped: answer a blocked round's
    questions, ask once for a split after the agent ran out of time, or wait. `comments`
    holds the comments of the target, then those of the issue or its pull request."""
    found = answers(cfg, run.get("questions")) if run.get("outcome") == "agent_blocked" else None
    if run.get("outcome") == "agent_blocked" and run.get("protected_paths"):
        # Only a person changes protected paths, whatever the round recommends.
        return _step(issue, "waits", protected_reason(run["protected_paths"]))
    if found and asked_again(*comments):
        return _step(issue, "waits", REPEATED)
    if found and command == "go" and closes(run):
        # The recommended answer only closes the issue: another round would find the same.
        reason = "the last round found no change is needed and recommends closing the issue"
        return _step(issue, "close", reason, finding=run["no_change"], run_url=run.get("url"))
    if found:
        start = {**start, "target": target, "note": answer_note(found)}
        reason = "autopilot answers the questions of the blocked round"
        return _step(issue, "start", reason, command=command, answers=found, **start)
    if run.get("timed_out") and not record.get("split_asked"):
        refused = split_refused(gh, issue)
        if refused:
            return _step(issue, "waits", refused)
        start = {**start, "note": SPLIT_NOTE, "remember": {"split_asked": True}}
        reason = "the agent ran out of time, so autopilot asks for a split"
        return _step(issue, "start", reason, command="plan", **start)
    return None


def _later(text, since):
    """Whether the time `text` is known and after `since`."""
    at = parse_iso(text)
    return at is not None and since is not None and at > since


def _since_stop(gh, issue, comments, stop):
    """Why autopilot plans again after a `/nexkit go` that stopped, or None when nothing
    changed since that round started: `stop` is its run comment. Collaborators commented
    after it, other than with commands, or an issue that blocks it closed after it started,
    such as one a person marked as a blocker after the round asked to wait for it. A
    blocker that closed earlier was closed while the round ran, so it changes nothing."""
    if _commented_after(comments, stop):
        return "collaborators commented after the stopped `/nexkit go`, so autopilot plans again"
    started = parse_iso(stop.get("created_at"))
    closed = sorted(
        blocker["number"]
        for blocker in gh.blocked_by(issue["number"])
        if blocker.get("state") == "closed" and _later(blocker.get("closed_at"), started)
    )
    if closed:
        listed = ", ".join(f"#{n}" for n in closed)
        reason = f"{listed}, which blocked it, closed after the stopped `/nexkit go`"
        return f"{reason}, so autopilot plans again"
    return None


def _split(gh, issue, plan, data, record, start, pull=None):
    """The step for an issue whose latest plan is too large, or None: split it as the plan
    suggests; or, when it suggests no parts or parts titled like the issue, ask once for
    the plan again; or wait for a person at `MAX_DEPTH` and after a second such plan."""
    if not (data or {}).get("too_large"):
        return None
    if depth(issue, lineage(gh, issue)) >= MAX_DEPTH:
        do = "make the issue smaller and comment the change on it, or implement it by hand"
        return _step(issue, "waits", person(do, DEPTH_REASON))
    if not data.get("split") and record.get("parts_asked"):
        why = "its plan is too large for one session and suggests no parts again"
        return _step(issue, "waits", person(SPLIT_DO, why))
    if not data.get("split"):
        start = {**start, "note": PARTS_NOTE, "remember": {"parts_asked": True}}
        reason = "the plan is too large and suggests no parts, so autopilot plans again"
        return _step(issue, "start", reason, command="plan", **start)
    repeated = repeated_titles(issue, data["split"])
    if repeated and record.get("titles_asked"):
        why = "the parts of its plan are titled like the issue again"
        return _step(issue, "waits", person(SPLIT_DO, why))
    if repeated:
        titles = ", ".join(f'"{title}"' for title in repeated)
        start = {
            **start,
            "note": TITLE_NOTE.format(titles=titles),
            "remember": {"titles_asked": True},
        }
        reason = "parts of the plan are titled like the issue, so autopilot plans again"
        return _step(issue, "start", reason, command="plan", **start)
    fields = {"parts": data["split"], "plan_url": plan.get("html_url") or "", "pull": pull}
    return _step(issue, "split", "the plan splits the issue", **fields, **start)


def _planned(gh, cfg, issue, plan, last, record, start, comments):
    """The step after a plan that needs nobody's comment."""
    data = plan_data(plan)
    if data is None:  # a plan from before NexKit kept its record
        if last.get("attention"):
            return _step(issue, "waits", last["attention"])
        return _step(issue, "start", "the plan needs nobody", command="go", **start)
    if data.get("too_large"):
        return _split(gh, issue, plan, data, record, start)
    if filled(data.get("no_change")):
        # Another round would find the same. A plan posted before NexKit ignored a finding
        # of only quotes, such as `""`, may record one: such a plan goes on.
        reason = "its plan finds that no change is needed"
        fields = {"finding": data["no_change"], "plan_url": plan.get("html_url") or ""}
        return _step(issue, "close", reason, **fields)
    if data.get("protected_paths"):
        return _step(issue, "waits", protected_reason(data["protected_paths"]))
    if data.get("questions"):
        found = answers(cfg, data["questions"])
        if not found:
            return _step(issue, "waits", last.get("attention") or QUESTIONS_REASON)
        if asked_again(comments):
            return _step(issue, "waits", REPEATED)
        reason = "autopilot answers the plan's questions"
        return _step(issue, "start", reason, command="plan", answers=found, **start)
    return _step(issue, "start", "the plan needs nobody", command="go", **start)


def _meaningful(rounds):
    """(comment, round) for the latest round that says something about the pull request:
    a round that found nothing to merge does not."""
    for comment, run in reversed(rounds):
        if run.get("outcome") != "up_to_date":
            return comment, run
    return rounds[-1] if rounds else (None, None)


def _ended(gh, run):
    """Whether the workflow run of a run comment that still says running has ended, so its
    report never wrote the outcome."""
    match = RUN_ID.search(run.get("url") or "")
    if not match:
        return False
    try:
        return gh.workflow_run(int(match.group(1))).get("status") == "completed"
    except GitHubError:
        return False


def _automatic(run):
    """Whether NexKit started a run by itself: a command autopilot started, or an automatic
    round. A person's command may carry a note that a new start would not have."""
    return bool(run.get("autopilot")) or bool(run_kinds(run) & {"auto", "autopilot"})


def _auto_input(run):
    """The `auto` input that starts a run's kind of automatic round again, from its
    trigger, or "" for a command that autopilot started."""
    kinds = run_kinds(run)
    if "conflicts" in kinds:
        found = ["conflicts"]
    elif "auto" in kinds:
        found = ["true"]
    else:
        return ""
    return ",".join(found + ([AUTO] if AUTO in kinds else []))


def _not_resumed(comment, run, now):
    """Why a run paused at the usage limit waits for a person, or None while NexKit
    resumes it by itself."""
    do = "post the command again once the usage limit has reset"
    if not isinstance(run.get("resume"), dict):
        return run.get("attention") or person(
            do, "the agent hit its usage limit, and NexKit does not resume this run"
        )
    at = parse_iso(comment.get("created_at"))
    if at and now - at > PAUSE_WINDOW:
        return person(do, "it paused at the usage limit longer ago than NexKit resumes runs")
    return None


def _stalled(gh, issue, comment, run, record, start, target, now):
    """The step for a run whose comment says it is running, paused or continued, when that
    no longer carries the issue on, or None. A run that ended without a report starts once
    more when NexKit started it by itself; anything else waits for a person."""
    status = run.get("status")
    if status == PAUSED:
        reason = _not_resumed(comment, run, now)
        return _step(issue, "waits", reason) if reason else None
    if status == CONTINUED:
        at = parse_iso(comment.get("updated_at"))
        lost = at is not None and now - at > LOST
        why = "the run that continues it did not start within six hours"
        return _step(issue, "waits", person(LOST_DO, why)) if lost else None
    if status != RUNNING or not _ended(gh, run):
        return None
    command = run.get("command") or ("fix" if run_action(run) == "fix" else "review")
    stale = {"number": target, "comment": comment["id"]}
    again = record.get("retried") and (record.get("command"), record.get("target")) == (
        command,
        target,
    )
    if not _automatic(run) or again:
        why = REPORTLESS + (", after autopilot started it once more" if again else "")
        return _step(issue, "waits", person(LOST_DO, why), stale=stale)
    fields = {**start, "target": target, "remember": {"retried": True}}
    auto = _auto_input(run)
    if auto:
        # The same kind of round: one after failing checks merges the base branch first.
        fields["auto"] = auto
    reason = f"{REPORTLESS}, so autopilot starts `/nexkit {command}` once more"
    return _step(issue, "start", reason, command=command, stale=stale, **fields)


def settle_ended(gh, number, comment, run):
    """Show a run comment on `number` that still says running as ended without a report,
    when its workflow run has ended. Returns whether it did. Sweeps do this for the issues
    autopilot takes (see `_stalled`); conflict rounds without autopilot do it here, so a
    round that ended without a report does not hold them back."""
    if run.get("status") != RUNNING or not _ended(gh, run):
        return False
    settle(gh, {"stale": {"number": number, "comment": comment["id"]}})
    return True


def settle(gh, step):
    """Show the run of a stale step as ended without a report, with its pending statuses
    as errors, so it no longer shows as running."""
    stale = step.get("stale")
    if not stale:
        return
    for comment, run in run_comments(gh.comments(stale["number"])):
        if comment["id"] != stale["comment"] or run.get("status") != RUNNING:
            continue
        run.update(status=FAILURE, outcome="lost", attention=REPORTLESS)
        line = "The run ended without a report. Its job log has what happened."
        gh.update_comment(comment["id"], with_run(comment["body"], run, line))
        if run.get("head"):
            for context in STATUS_CONTEXTS:
                gh.set_status(
                    run["head"], context, "error", "The NexKit round did not finish", run.get("url")
                )


def _conflict_wait(gh, cfg, pull, rounds):
    """Why a pull request that conflicts with its base branch waits for a person, or None:
    without `conflicts.auto_resolve`, or when its automatic conflict rounds are used up."""
    details = gh.pull(pull)
    if details.get("mergeable") is not False:
        return None
    base = (details.get("base") or {}).get("ref") or "its base branch"
    do = f"comment `/nexkit fix` on pull request #{pull}, with any rule the conflict needs"
    if not cfg["conflicts"]["auto_resolve"]:
        why = "with `conflicts.auto_resolve` off, NexKit starts no round to resolve conflicts"
        return person(do, f"it conflicts with `{base}`, and {why}")
    if conflict_rounds_used(rounds) >= cfg["conflicts"]["max_rounds"]:
        why = f"it conflicts with `{base}`, and its automatic conflict rounds are used up"
        return person(do, why)
    return None


def _pull_step(gh, cfg, issue, pull, rounds, record, start, comments, pull_comments, now):
    """The step for an issue whose NexKit pull request is open, from its latest round: a
    round that runs, or started the next one, carries it on; one that stopped is answered
    or split as after `/nexkit go`; anything that needs a person waits for them."""
    comment, latest = _meaningful(rounds)
    if latest is None:
        return _step(issue, "active", f"pull request #{pull} is open")
    if latest.get("status") in ACTIVE:
        stalled = _stalled(gh, issue, comment, latest, record, start, pull, now)
        return stalled or _step(issue, "active", f"pull request #{pull} has a round on its way")
    if latest.get("outcome") in CHAINED:
        at = parse_iso(comment.get("updated_at") or comment.get("created_at"))
        if at is not None and now - at > LOST:
            why = f"the round that pull request #{pull} started next did not run within six hours"
            return _step(issue, "waits", person(LOST_DO, why))
        return _step(issue, "active", f"pull request #{pull} has a round on its way")
    if latest.get("outcome") in (UNTRUSTED_ROUND, UNTRUSTED_MERGE):
        if not _vouched_again(gh, issue, comment):
            return _step(issue, "waits", latest.get("attention") or UNTRUSTED_REASON)
        # A round that stopped gave its automatic round back; a merge needs a new review.
        again = {"command": "fix", "count": True}
        if latest["outcome"] == UNTRUSTED_MERGE:
            again = {"command": "review"}
        fields = {**start, "target": pull, "auto": "true", **again}
        return _step(issue, "start", "a writer vouched for it again", **fields)
    if latest.get("outcome") == HELD:
        return _released(gh, cfg, issue, pull, comment, latest, pull_comments, start, now)
    if latest.get("outcome") == "merge_waiting":
        return _merge_step(gh, cfg, issue, pull, comment, latest, start, now)
    if latest.get("outcome") == "merge_refused":
        step = _refused_step(gh, issue, pull, comment, latest)
        if step:
            return step
    if latest.get("outcome") == CI_WAITING:
        return _ci_step(gh, cfg, issue, pull, latest, pull_comments, start)
    if latest.get("status") == FAILURE:
        found = (pull_comments, comments)
        step = _stopped(gh, cfg, issue, latest, record, start, "fix", pull, found)
        failed = person(
            f"read the last round's comment on #{pull} for why, then post its command again",
            f"the last round of #{pull} failed",
        )
        return step or _step(issue, "waits", latest.get("attention") or failed)
    if latest.get("attention"):
        return _step(issue, "waits", latest["attention"])
    conflict = _conflict_wait(gh, cfg, pull, [run for _, run in rounds])
    if conflict:
        return _step(issue, "waits", conflict)
    return _step(issue, "active", f"pull request #{pull} is open")


def _released(gh, cfg, issue, pull, comment, run, pull_comments, start, now):
    """The step once no label holds an issue any more whose pull request's round held
    back its merge, its automatic fix round or its conflict round (`HELD`): that, now, for
    what is still so. A head that moved meanwhile, such as by a person's own commit, gets
    `/nexkit review` instead, as does a conflict that is gone."""
    back = run.get("held_back")
    gone = "the label that held it is gone"
    if back == "merge":
        step = _merge_step(gh, cfg, issue, pull, comment, run, start, now)
        if step["state"] == "merge":
            step["reason"] = f"{gone}, so autopilot merges pull request #{pull}"
        return step
    details = gh.pull(pull)
    head = (details.get("head") or {}).get("sha")
    review = {**start, "target": pull, "auto": "true"}
    if head != run.get("head"):
        reason = f"{gone}, and pull request #{pull} got the new commit `{str(head)[:7]}`"
        return _step(issue, "start", reason, command="review", **review)
    rounds = [r for _, r in run_comments(pull_comments) if "round" in r]
    if back == "conflicts":
        # `maintain` imports this module.
        from .maintain import conflict_note

        if details.get("mergeable") is not False:
            reason = f"{gone}, and pull request #{pull} no longer conflicts, so it is checked"
            return _step(issue, "start", reason, command="review", **review)
        waits = _conflict_wait(gh, cfg, pull, rounds)
        if waits:
            return _step(issue, "waits", waits)
        base = (details.get("base") or {}).get("ref") or ""
        fields = {**start, "target": pull, "auto": "conflicts", "note": conflict_note(base)}
        reason = f"{gone}, so pull request #{pull} gets the round that resolves its conflicts"
        return _step(issue, "start", reason, command="fix", **fields)
    _, state = read_state(pull_comments)
    if (state or {}).get("auto_fixes", 0) >= cfg["fix"]["max_auto_rounds"]:
        do = f"comment `/nexkit fix` on pull request #{pull} for what still fails"
        return _step(issue, "waits", person(do, f"{gone}, but it has no automatic fix rounds left"))
    fields = {**start, "target": pull, "auto": "true", "count": True}
    reason = f"{gone}, so pull request #{pull} gets the automatic fix round it held back"
    return _step(issue, "start", reason, command="fix", **fields)


def _refused_step(gh, issue, pull, comment, run):
    """The merge of a pull request whose merge GitHub refused, once GitHub reports it as
    mergeable (`clean`) on the head that its checks and review passed, such as after an
    owner approved it while no run could, or a rule changed; or None. A head that GitHub
    called mergeable and still refused is not tried again."""
    details = gh.pull(pull)
    head = (details.get("head") or {}).get("sha")
    if details.get("mergeable_state") not in MERGEABLE_STATES or head != run.get("head"):
        return None
    if run.get("refused_clean") == head:
        return None
    base = (details.get("base") or {}).get("ref") or ""
    reason = f"GitHub now accepts the merge of pull request #{pull}, which it refused"
    return _step(issue, "merge", reason, round_comment=comment["id"], base=base, clean=True)


def _merge_step(gh, cfg, issue, pull, comment, run, start, now):
    """The step for a pull request whose merge waits for required checks: try the merge
    again while its head is the one approved, for at most `LOST`. A new commit on its
    branch gets `/nexkit review`, as a merge refused because the branch moved does. A
    required check that failed gets an automatic fix round when the sweep tries the merge
    (see `report.merge_waiting`), and a branch behind its base, however long it waited,
    an update from it when `conflicts.auto_resolve` is on."""
    details = gh.pull(pull)
    head = details["head"]["sha"]
    if head != run.get("head"):
        reason = (
            f"pull request #{pull} got the new commit `{head[:7]}` while its merge waited for "
            "required checks"
        )
        fields = {**start, "target": pull, "auto": "true"}
        return _step(issue, "start", reason, command="review", **fields)
    since = parse_iso(run.get("waiting_since"))
    behind = cfg["conflicts"]["auto_resolve"] and details.get("mergeable_state") == "behind"
    if since and now - since > LOST and not behind:
        do = (
            "find out from their runs why the checks neither passed nor failed, start them "
            f"again, and comment `/nexkit review` on #{pull} once they can pass"
        )
        why = f"the required checks of pull request #{pull} have not passed for six hours"
        return _step(issue, "waits", person(do, why))
    base = (details.get("base") or {}).get("ref") or ""
    reason = f"pull request #{pull} waits for its required checks"
    return _step(issue, "merge", reason, round_comment=comment["id"], base=base)


def _replanned(gh, cfg, issue, plan, last, record, start, comments, pull):
    """The step after the plan autopilot asked for when a round of the pull request ran out
    of time: split the issue as the plan suggests, answer its questions, or, when it needs
    nobody, a fix round on the pull request that follows the revised plan."""
    if last.get("status") != SUCCESS:
        return _step(issue, "waits", last.get("attention") or failed_reason("plan"))
    data = plan_data(plan) if plan else None
    split = _split(gh, issue, plan, data, record, start, pull) if data else None
    if split:
        return split
    step = _planned(gh, cfg, issue, plan, last, record, start, comments)
    if step["state"] != "start" or step["command"] != "go":
        return step
    reason = "the plan after the round that ran out of time needs nobody"
    fields = {**start, "target": pull, "note": REPLANNED_NOTE}
    return _step(issue, "start", reason, command="fix", **fields)


def assess(gh, cfg, issue, open_pulls, now=None, where=None):
    """What autopilot does for one open issue in scope:

    - `active`: a run or pull request carries it on, or a command autopilot started has
      not run yet. It counts toward `autopilot.max_parallel`.
    - `start`: start `command` on `target`, after posting `answers` when there are any.
    - `split`: create the `parts` of a plan that is too large as sub-issues.
    - `waits`: nothing to start, for `reason`. With `person`, a person decides, and
      `mentioned` says whether `notify` was mentioned since the latest run; otherwise it
      waits for another issue or for a place within `max_parallel`.
    - `held`: a label of `autopilot.labels` with `hold` holds it: nothing starts, its pull
      request gets no automatic round or merge, and it takes no place unless a run on it
      is still `running`.

    `where` is the scope, from `locate`.
    """
    step = _assess(gh, cfg, issue, open_pulls, now, where)
    if step.get("person"):
        pull = step.get("pull")
        found = [gh.comments(issue["number"]), gh.comments(pull) if pull else []]
        step["mentioned"] = mentioned(*found)
    return step


def mentioned(*groups):
    """Whether NexKit mentioned a person in these comments since the latest run comment
    in any of them."""
    latest = max((c["id"] for found in groups for c, _ in run_comments(found)), default=0)
    return any(
        by_bot(c) and NEEDED in (c.get("body") or "") and c["id"] > latest
        for found in groups
        for c in found
    )


def mention_text(cfg, reason):
    """The comment that mentions the people in `notify`, or None without any: what to do,
    why NexKit does not, and where to look (see `person`)."""
    if not cfg["notify"]:
        return None
    people = " ".join(f"@{login}" for login in cfg["notify"])
    return f"{people} {NEEDED}{_unmentioned(reason).rstrip('.')}."


MENTION = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9._-]+|\[bot\])?")


def _unmentioned(reason):
    """`reason` as it may stand in a comment that mentions `notify`: outside its code
    spans, a login such as the author of an issue goes in a code span of its own, so that
    nobody outside `notify` is mentioned, and nothing opens or closes an HTML comment."""
    # NexKit's markers are HTML comments: none may start or end here, in a span either.
    parts = reason.replace("<!--", "‹!--").replace("-->", "--›").split("`")
    if len(parts) % 2 == 0:
        # An unpaired backtick: no part counts as a code span.
        parts = ["'".join(parts)]
    for index in range(0, len(parts), 2):
        text = parts[index].replace("<", "‹").replace(">", "›")
        parts[index] = MENTION.sub(lambda m: f"`{m.group(0)}`", text)
    return "`".join(parts)


def _assess(gh, cfg, issue, open_pulls, now=None, where=None):
    number = issue["number"]
    label = hold_label(cfg, issue)
    if label:
        pull = open_pulls.get(number)
        found = [gh.comments(number), gh.comments(pull) if pull else []]
        # A run that is under way finishes, and keeps its place within max_parallel.
        running = any(
            run.get("status") in ACTIVE
            for comments in found
            for run in [r for _, r in run_comments(comments)][-1:]
        )
        fields = {"pull": pull} if pull else {}
        return _step(issue, "held", held_reason(label), running=running, **fields)
    left_alone = distrust(gh, issue, cfg)
    if left_alone:
        return _step(issue, "waits", person(TRUST_DO, f"autopilot leaves it alone: {left_alone}"))
    parts = gh.sub_issues(number)
    if parts and all(part.get("state") == "closed" for part in parts):
        # `close_parent_issues` closes it by itself; without it, a person does.
        reason = person(
            "close it, or open new parts for what is left",
            "every sub-issue of this parent issue is closed, and NexKit closes a parent only "
            "with `close_parent_issues` on and a part completed",
        )
        return _step(issue, "waits", reason)
    if parts:
        reason = "a parent issue: autopilot works on its sub-issues"
        return _step(issue, "waits", reason, person=False)
    comments = gh.comments(number)
    pull = open_pulls.get(number)
    pull_comments = gh.comments(pull) if pull else []
    current = basis(comments, pull_comments)
    _, record = find_record(comments)
    now = now or datetime.now(UTC)
    if record.get("basis") == current:
        command = f"`/nexkit {record['command']}`"
        if not _lost(gh, cfg, record, now):
            return _step(issue, "active", f"waits for the {command} that autopilot started")
        reason = f"the {command} that autopilot started did not run"
        if record.get("retried"):
            do = f"read the dispatched run for why {command} did not run, then post it yourself"
            return _step(issue, "waits", person(do, f"{reason}, twice"))
        return _step(
            issue,
            "start",
            f"{reason}, so autopilot starts it once more",
            command=record["command"],
            target=record.get("target") or number,
            note=record.get("note") or "",
            basis=current,
            remember={"retried": True},
            **({"auto": record["auto"]} if record.get("auto") else {}),
        )
    start = {"basis": current, "target": number, "note": "", "remember": {}}
    found = [(c, run) for c, run in run_comments(comments) if "command" in run]
    last = found[-1][1] if found else None
    handed_on = pull and last and last.get("status") == CONTINUED
    if last and last.get("status") in ACTIVE and not handed_on:
        stalled = _stalled(gh, issue, *found[-1], record, start, number, now)
        return stalled or _step(issue, "active", f"`/nexkit {last['command']}` is {last['status']}")
    plan = latest_plan(comments)
    if pull:
        # The plan autopilot asked for after the pull request's agent ran out of time. A
        # plan that is too large but that a person chose to implement anyway is not one.
        asked = record.get("split_asked") and record.get("command") == "plan"
        if asked and last and last.get("command") == "plan":
            step = _replanned(gh, cfg, issue, plan, last, record, start, comments, pull)
        else:
            rounds = [(c, run) for c, run in run_comments(pull_comments) if "round" in run]
            step = _pull_step(
                gh, cfg, issue, pull, rounds, record, start, comments, pull_comments, now
            )
        return {**step, "pull": pull}
    blocked = waits_for(gh, issue, where) or lower_waves(gh, cfg, issue)
    if blocked:
        return _step(issue, "waits", blocked[0], person=blocked[1])
    if last and last.get("outcome") == UNTRUSTED_ROUND:
        if _vouched_again(gh, issue, found[-1][0]):
            return _step(issue, "start", "a writer vouched for it again", command="plan", **start)
        return _step(issue, "waits", last.get("attention") or UNTRUSTED_REASON)
    if last is None or plan is None:
        return _step(issue, "start", "no plan yet", command="plan", **start)
    if last["command"] == "go":
        if last.get("outcome") == "issue_closed":
            # Reopened after it closed while the round ran: nothing changed for the plan.
            reason = person(
                "comment `/nexkit go` to implement the plan again, or `/nexkit plan` to plan anew",
                "it closed while `/nexkit go` ran and was reopened, which changes nothing for "
                "its plan",
            )
            return _step(issue, "waits", reason)
        if last.get("status") == SUCCESS:
            reason = person(
                "comment `/nexkit go` for a new pull request, or close the issue",
                "its pull request was closed, and autopilot opens no other by itself",
            )
            return _step(issue, "waits", reason)
        step = _stopped(gh, cfg, issue, last, record, start, "go", number, (comments,))
        if step:
            return step
        changed = None if last.get("timed_out") else _since_stop(gh, issue, comments, found[-1][0])
        if changed:
            return _step(issue, "start", changed, command="plan", **start)
        return _step(issue, "waits", last.get("attention") or failed_reason("go"))
    if last.get("status") != SUCCESS:
        return _step(issue, "waits", last.get("attention") or failed_reason("plan"))
    if _commented_after(comments, plan):
        return _step(
            issue, "start", "collaborators commented after the plan", command="plan", **start
        )
    return _planned(gh, cfg, issue, plan, last, record, start, comments)


def carries_on(gh, cfg, decision, outcome, agent):
    """Whether autopilot takes over after a run that would otherwise need a person, so
    `report` mentions nobody: it answers a plan's or a blocked round's questions, splits a
    plan that is too large or plans it again for its parts, or asks for a split after the
    agent ran out of time."""
    if not takes(cfg):
        return False
    agent = agent or {}
    output = agent.get("output") or {}
    splits = outcome == "planned" and bool(output.get("too_large"))
    closes = outcome == "planned" and bool(filled(output.get("no_change")))
    if splits or closes:
        # Autopilot splits the issue, or closes it with the plan's finding.
        handled = True
    elif output.get("protected_paths"):
        # Only a person changes protected paths.
        handled = False
    elif outcome == "planned":
        handled = answers(cfg, output.get("questions")) is not None
    elif outcome == "agent_blocked":
        handled = answers(cfg, round_questions(agent)) is not None
    elif outcome == "agent_error" and agent.get("timed_out"):
        handled = decision["action"] in ("implement", "fix")
    else:
        return False
    try:
        if not handled:
            return False
        issue = gh.issue(decision["issue"])
        if not taken(gh, cfg, issue):
            return False
        if outcome == "agent_error":
            record = find_record(gh.comments(issue["number"]))[1]
            return not record.get("split_asked") and not split_refused(gh, issue)
        if closes:
            return True
        if not splits:
            # Questions again right after autopilot's answers: a person decides.
            pr = decision.get("pr")
            target = gh.comments(pr or issue["number"])
            return not asked_again(target, gh.comments(issue["number"]) if pr else ())
        if splits:
            # Autopilot splits the issue, or plans it again for parts that it lacks or
            # that are titled like the issue.
            record = find_record(gh.comments(issue["number"]))[1]
            start = {"basis": None, "target": issue["number"], "note": "", "remember": {}}
            return _split(gh, issue, {}, output, record, start)["state"] != "waits"
    except GitHubError:
        return False
    return True


# -- a sweep -----------------------------------------------------------------------


def nexkit_pulls(gh):
    """{issue number: pull request number} for NexKit's open pull requests."""
    found = {}
    for pull in gh.list_pulls("open"):
        issue = issue_from_branch(pull["head"]["ref"])
        if issue is not None and pull["head"]["repo"]["full_name"] == gh.repository:
            found[issue] = pull["number"]
    return found


def preview(gh, cfg):
    """(scope, one step per open issue in scope) for what a sweep would do now. Starts
    stay within `autopilot.max_parallel`, in the order of `scope`."""
    where = scope(gh, cfg)
    if not where["issues"]:
        return where, []
    pulls = nexkit_pulls(gh)
    steps = [assess(gh, cfg, issue, pulls, where=where) for issue in where["issues"]]
    limit = cfg["autopilot"]["max_parallel"]
    # An issue whose pull request autopilot carries on is in progress already, also while
    # the pull request waits for another issue. One that waits for a person leaves its
    # place to the others.
    busy = sum(
        step["state"] == "active"
        or bool(step.get("running"))
        or (
            bool(step.get("pull"))
            and step["state"] != "held"
            and (step["state"] != "waits" or not step["person"])
        )
        for step in steps
    )
    # A bug issue about the default branch whose failure pull requests wait for
    # (`ci_waiting`) ends their wait, so neither it nor its parts, once it is split, wait
    # for the places they hold. Each counts once it started, so nothing else starts
    # beyond the limit.
    awaited = {}
    for step in steps:
        if step.get("ci_bug"):
            awaited.setdefault(step["ci_bug"], []).append(step["pull"])
    issues = {issue["number"]: issue for issue in where["issues"]}
    for step in steps:
        if step["state"] != "start" or step.get("pull"):
            continue
        if limit is not None and busy >= limit:
            waiting = _awaited_by(gh, issues.get(step["number"]), awaited)
            if not waiting:
                reason = f"{busy} issues are in progress (max_parallel)"
                step.update(state="waits", reason=reason, person=False)
                continue
            names = ", ".join(f"#{n}" for n in waiting)
            many = len(waiting) > 1
            those = f"pull requests {names} wait" if many else f"pull request {names} waits"
            step["reason"] += f"; {those} for it, so it starts beyond max_parallel"
        busy += 1
    return where, steps


def _awaited_by(gh, issue, awaited):
    """The pull requests that wait for `issue`, or for an issue above it, as a bug issue
    about the default branch: [] when none do. `awaited` maps each such bug issue to the
    pull requests that wait for it."""
    if not issue or not awaited:
        return []
    for found in (issue, *lineage(gh, issue)):
        if found["number"] in awaited:
            return awaited[found["number"]]
    return []


def mention(gh, cfg, step):
    """Mention the people in `notify` on an issue that started to wait for a person, once
    for its latest run, whether or not that run's `report` did. Returns a line or None."""
    text = mention_text(cfg, step["reason"])
    if not text or not step.get("person") or step.get("mentioned"):
        return None
    gh.comment(step["number"], text)
    step["mentioned"] = True
    return f"#{step['number']} waits for a person: {step['reason']}."


def _save_record(gh, step, record, text, now=None):
    """Edit the issue's record comment, or post it. Flags such as `split_asked` stay."""
    comment, previous = find_record(gh.comments(step["number"]))
    record = {
        **{key: previous[key] for key in STICKY if key in previous},
        **step.get("remember", {}),
        **record,
        "basis": step["basis"],
        "at": iso(now or datetime.now(UTC)),
    }
    body = f"{text}\n\n{record_marker(record)}"
    if comment:
        gh.update_comment(comment["id"], body)
    else:
        gh.comment(step["number"], body)


def post_answers(gh, number, found):
    """Answer for the owner in a comment that the agents read as the owner's discussion."""
    lines = ["NexKit autopilot answers for the owner, with the option NexKit recommended:", ""]
    for index, item in enumerate(found, 1):
        lines += [f"{index}. {item['question']}", f"   **{item['answer']}**"]
    gh.comment(number, "\n".join(lines) + "\n\n" + answers_marker({"answers": found}))


def start(gh, step, workflow, ref, now=None):
    """Start a step's command, after posting its answers, and record it on the issue.
    Returns a line for the log."""
    command, target = step["command"], step["target"]
    if step.get("answers"):
        post_answers(gh, target, step["answers"])
    settle(gh, step)
    auto = step.get("auto") or AUTO
    inputs = {"command": command, "number": str(target), "note": step["note"], "auto": auto}
    # Counted before the dispatch, so a failure between the two never starts a round that
    # the next sweep starts and counts again; a dispatch that fails takes the count back.
    undo = _count_round(gh, target) if step.get("count") else None
    try:
        started = gh.dispatch(workflow, ref, inputs, run_details=True) or {}
    except GitHubError:
        if undo:
            undo()
        raise
    record = {"command": command, "target": target}
    if step.get("auto"):
        # A command that never ran starts once more as the same kind of round.
        record["auto"] = step["auto"]
    if step["note"]:
        # A command that never ran starts once more with the same note.
        record["note"] = step["note"]
    if started.get("html_url"):
        record["url"] = started["html_url"]
    run = f" in [a new run]({record['url']})" if record.get("url") else ""
    on = "" if target == step["number"] else f" on #{target}"
    text = f"NexKit autopilot started `/nexkit {command}`{on}{run}: {step['reason']}."
    _save_record(gh, step, record, text, now)
    return f"Started /nexkit {command} on #{target}: {step['reason']}."


def close_pull(gh, pull, text):
    """Close NexKit's pull request `pull` without merging it, delete its branch when the
    repository deletes head branches on merge, and say so in a comment."""
    # `maintain` imports this module.
    from .maintain import delete_head_branch

    gh.close_pull(pull)
    gh.comment(pull, "\n\n".join([text, *delete_head_branch(gh, pull)]))


def close(gh, cfg, step, workflow, ref):
    """Close an issue as completed with the finding of its last round, which found no
    change is needed and recommended closing it. Returns lines for the log."""
    number = step["number"]
    if "plan_url" in step:
        url = step["plan_url"]
        plan = f"[its plan]({url})" if url else "its plan"
        intro = f"NexKit autopilot closes this issue with the finding of {plan}"
    else:
        url = step.get("run_url")
        round_ = f"[the last round]({url})" if url else "the last round"
        intro = f"NexKit autopilot closes this issue with the answer {round_} recommended"
    gh.comment(number, f"{intro}: no change is needed.\n\n{step['finding']}")
    gh.close_issue(number, "completed")
    lines = [f"Closed #{number}: {step['reason']}."]
    if step.get("pull"):
        # The plan after a round that ran out of time found no change is needed.
        try:
            close_pull(gh, step["pull"], f"#{number} closed: its plan found no change is needed.")
            lines.append(f"Closed #{step['pull']} with it.")
        except GitHubError as exc:
            lines.append(f"Could not close #{step['pull']}: {exc}")
    if cfg["close_parent_issues"]:
        # `maintain` imports this module. Here it runs at once; the `issues: closed` run
        # that follows finds nothing left.
        from .maintain import close_parents_safely

        lines += close_parents_safely(gh, number)
    # Issues it blocked may start now, and the scope may be done.
    return lines + request_safely(gh, cfg, workflow, ref, number)


def split(gh, step, now=None):
    """Create the parts of a plan that is too large as sub-issues of the issue, in order,
    with its milestone and labels, and close its superseded pull request."""
    number = step["number"]
    parent = gh.issue(number)
    milestone = (parent.get("milestone") or {}).get("number")
    labels = [label["name"] for label in parent.get("labels") or [] if label.get("name")]
    plan = f" from [its plan]({step['plan_url']})" if step.get("plan_url") else ""
    ghsa = advisory_work(parent)
    mark = f"{DEPENDABOT_PART_PREFIX}{ghsa} -->\n" if ghsa else ""
    created = []
    for index, part in enumerate(step["parts"], 1):
        body = (
            f"<!-- nexkit:part {number} -->\n{mark}{part['body'].strip()}\n\n"
            f"Part {index} of #{number}{plan}, which was too large for one session."
        )
        issue = gh.create_issue(part["title"].strip(), body, milestone, labels)
        gh.add_sub_issue(number, issue["id"])
        created.append(issue)
        # A plan from before parts named their dependencies: each part needs the one before.
        needs = part["depends_on"] if "depends_on" in part else [index - 1] if index > 1 else []
        for earlier in needs:
            if 0 < earlier < index:
                gh.add_blocked_by(issue["number"], created[earlier - 1]["id"])
    listing = ", ".join(f"#{issue['number']}" for issue in created)
    if step.get("pull"):
        close_pull(gh, step["pull"], f"Superseded by {listing}, the parts of #{number}.")
    text = f"NexKit autopilot split this issue as its plan suggests: {listing}."
    parts = [issue["number"] for issue in created]
    _save_record(gh, step, {"command": "split", "target": number, "parts": parts}, text, now)
    return f"Split #{number} into {listing}."


def done(gh, cfg, where):
    """Say once that a milestone is done: each open milestone in scope that has issues, all
    of them closed, gets a done notice on its most recently closed issue, mentioning
    `notify`, unless that issue has one from NexKit already. For the milestone scope
    that is the current milestone, which stays current until a person closes it; with
    `null`, any open milestone, while the others go on. It comes from what GitHub shows,
    not from the note of the sweep that a closed issue started, so a sweep that replaced
    that one in the queue says it as well."""
    lines = []
    for milestone in where.get("milestones") or []:
        number = milestone["number"]
        if not milestone.get("closed_issues") or any(
            (issue.get("milestone") or {}).get("number") == number for issue in where["issues"]
        ):
            continue
        try:
            issue = gh.latest_closed_issue(number)
            if not issue or any(
                by_bot(c) and DONE in (c.get("body") or "") for c in gh.comments(issue["number"])
            ):
                continue
            name = f"milestone {milestone.get('title') or number}"
            people = "".join(f"@{login} " for login in cfg["notify"])
            text = f"{DONE}\n{people}NexKit autopilot finished {name}: every issue in it is closed."
            if not where["every"]:
                text += " Close the milestone to let autopilot start on the next one."
            gh.comment(issue["number"], text)
        except GitHubError as exc:
            lines.append(f"Could not say whether milestone {number} is done: {exc}")
            continue
        lines.append(f"Autopilot finished {name}.")
    return lines


# -- the default branch's CI ----------------------------------------------------------


def ci_workflow(issue):
    """The workflow a bug issue that autopilot opened for a CI failure is about, or None."""
    match = CI_BUG.match(issue.get("body") or "")
    return match.group(1) if match and is_bot(author(issue)) else None


def flaky_workflow(issue):
    """The workflow an issue that autopilot opened for flaky runs is about, or None."""
    match = CI_FLAKY.match(issue.get("body") or "")
    return match.group(1) if match and is_bot(author(issue)) else None


def flaky_failure(issue):
    """The key of the failure an issue that autopilot opened for flaky runs is about; None
    for one from NexKit 4.3.0, which holds every failure of its workflow, and for others."""
    match = CI_FLAKY.match(issue.get("body") or "")
    return match.group(2) if match and is_bot(author(issue)) else None


def dependabot_advisory(issue):
    """The id of the Dependabot advisory an issue that autopilot opened is about, or None."""
    match = DEPENDABOT.match(issue.get("body") or "")
    return match.group(1) if match and is_bot(author(issue)) else None


def advisory_work(issue):
    """The id of the advisory that an issue autopilot opened, or a part of it, is about, or
    None."""
    match = DEPENDABOT_PART.match(issue.get("body") or "")
    part = match.group(1) if match and is_bot(author(issue)) else None
    return dependabot_advisory(issue) or part


def label_own(gh):
    """Add its label (`own_label`) to each open issue that NexKit opened for a CI failure,
    an advisory or flaky runs and that lacks it, such as one from before NexKit labelled
    them. Returns lines for the log."""
    lines = []
    try:
        for issue in nexkit_issues(gh, "open"):
            label = own_label(issue)
            if label and not _labeled(issue, label):
                gh.add_labels(issue["number"], [label])
                lines.append(f"Labelled #{issue['number']} `{label}`.")
    except GitHubError as exc:
        lines.append(f"Could not label NexKit's issues: {exc}")
    return lines


def nexkit_issues(gh, state="all", since=None):
    """The issues NexKit opened as its App, newest first."""
    found = {
        issue["number"]: issue
        for login in sorted(BOT_LOGINS)
        for issue in gh.issues_by(login, ISSUES_LIMIT, state, since)
    }
    return sorted(found.values(), key=lambda issue: -issue["number"])


def ci_issues(gh, since):
    """The issues autopilot opened that can hold a record a sweep needs, newest first:
    every open one, and every one updated since `since`. A record of a run created since
    then was written after the run started, so its issue was updated since then too,
    however many issues and pull requests NexKit opened after it."""
    found = {i["number"]: i for i in nexkit_issues(gh, "open")}
    for issue in nexkit_issues(gh, since=iso(since)):
        found.setdefault(issue["number"], issue)
    return sorted(found.values(), key=lambda issue: -issue["number"])


def _ci_records(gh, workflow, issues):
    """What the issues that autopilot opened for `workflow`, open or closed, hold: the
    open bug issue, the open and the latest closed issue of each flaky failure, the commits
    bug issues report, the attempts recorded for each run, and for each run a bug issue
    reports, that issue's number. `issues` comes from `ci_issues`."""
    found = {"bug": None, "open": {}, "closed": {}, "shas": set(), "seen": {}, "reporter": {}}
    # A failed step's issue, whichever workflow opened it (see `_issue_of`).
    found.update(any_open={}, any_closed={})
    # Newest first, so the first issue found for a failure is its latest.
    for issue in issues:
        state = "open" if issue.get("state") == "open" else "closed"
        if flaky_failure(issue):
            found["any_" + state].setdefault(flaky_failure(issue), issue)
        bug = ci_workflow(issue) == workflow
        if not bug and flaky_workflow(issue) != workflow:
            continue
        if bug and state == "open" and found["bug"] is None:
            found["bug"] = issue
        elif not bug and flaky_failure(issue):
            found[state].setdefault(flaky_failure(issue), issue)
        texts = [issue.get("body") or ""]
        texts += [c.get("body") or "" for c in gh.comments(issue["number"]) if by_bot(c)]
        for text in texts:
            if bug:
                found["shas"].update(CI_SHA.findall(text))
            match = CI_RUNS.search(text)
            for item in match.group(1).split() if match else ():
                run_id, _, attempt = item.partition(".")
                if not (run_id.isdigit() and attempt.isdigit()):
                    continue
                found["seen"].setdefault(int(run_id), set()).add(int(attempt))
                if bug:
                    found["reporter"].setdefault(int(run_id), issue["number"])
    return found


def _runs_marker(run, attempts):
    items = " ".join(f"{run['id']}.{attempt}" for attempt in sorted(attempts))
    return f"{CI_RUNS_PREFIX}{items} -->"


def _failed_jobs(gh, run, attempt, failed_jobs=None):
    """Each failed job of an attempt, linked, with its failed steps, the failures its error
    annotations name and the end of its log, so the issue holds what an agent needs. With
    `failed_jobs`, each failed job's name, failed commands and the tests its error
    annotations name are added to it."""
    lines = []
    for job in gh.attempt_jobs(run["id"], attempt):
        if job.get("conclusion") not in FAILED:
            continue
        steps = [s.get("name") for s in job.get("steps") or [] if s.get("conclusion") in FAILED]
        failed = f": {', '.join(f'`{name}`' for name in steps if name)}" if steps else ""
        link = f"[{job.get('name')}]({job['html_url']})" if job.get("html_url") else job.get("name")
        lines.append(f"- {link}{failed}")
        notes = gh.check_run_annotations(job["id"]) if job.get("id") else []
        for found in (_annotated(link, note) for note in notes):
            if found:
                lines.append("  " + found["line"].replace(f"- {link}: ", "- ", 1))
        log = _job_log(gh, job)
        if failed_jobs is not None:
            tests = [name for name in map(_annotated_test, notes) if name]
            failed_jobs.append(
                {
                    "job": job.get("name"),
                    "commands": failed_commands(log),
                    "tests": list(dict.fromkeys(tests)),
                }
            )
        tail = _log_tail(log)
        if tail:
            lines += ["", "  The end of its log:", "", "  ```text", *tail, "  ```", ""]
    return lines or ["- The attempt failed before any job did; open it for the reason."]


# The lines of a failed job's log that a CI bug issue shows, at most this many characters.
LOG_LINES = 30
LOG_CHARS = 4000
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")


def _job_log(gh, job):
    """A failed job's log as text, or "" when GitHub has none."""
    if not job.get("id"):
        return ""
    try:
        raw = gh.job_log(job["id"])
    except GitHubError:
        return ""
    return raw.decode("utf-8", errors="replace")


def command_line(command):
    """The first line of a command, on one line: what a job's log names a step by, and
    what a check is compared by."""
    lines = [line for line in str(command or "").splitlines() if line.strip()]
    return " ".join(lines[0].split()) if lines else ""


def failed_commands(log):
    """The command of each `run` step whose process failed or ran out of time, from a
    job's log, by the first line of the command, as the log starts the step with it."""
    found, current = [], ""
    for raw in log.lstrip("\ufeff").splitlines():
        line = TIMESTAMP.sub("", raw).strip()
        step = LOG_STEP.match(line)
        if step:
            current = command_line(step.group(1))
        elif current and LOG_FAILED.match(line) and current not in found:
            found.append(current)
    return found


def _log_tail(log):
    """The last lines of a failed job's log, redacted and indented for the issue, or []."""
    text = log[-LOG_CHARS * 4 :]
    lines = [TIMESTAMP.sub("", line).rstrip() for line in text.splitlines()]
    lines = [line[:300].replace("```", "'''") for line in lines if line.strip()][-LOG_LINES:]
    shown = Redactor()("\n".join(lines))[-LOG_CHARS:]
    return [f"  {line}" for line in shown.splitlines()]


def _failed_attempts(gh, run, attempts):
    """The attempts among `attempts` that failed. The latest attempt is the run itself."""
    latest = run.get("run_attempt") or 1
    failed = []
    for attempt in attempts:
        found = run if attempt == latest else gh.run_attempt(run["id"], attempt) or {}
        if found.get("conclusion") in FAILED:
            failed.append(attempt)
    return failed


def _failures(gh, run, failed, failed_jobs=None):
    """Each failed attempt, linked, with its failed jobs and steps. With `failed_jobs`, the
    failed jobs of the latest failed attempt and their failed commands are added to it."""
    lines = []
    for attempt in failed:
        url = f"{run.get('html_url')}/attempts/{attempt}"
        found = failed_jobs if attempt == failed[-1] else None
        lines += ["", f"Attempt [{attempt}]({url}):", *_failed_jobs(gh, run, attempt, found)]
    return lines


def _failed_marker(run, failed_jobs):
    record = {"run": run["id"], "jobs": failed_jobs}
    data = base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode()
    return f"{CI_FAILED_PREFIX}{data} -->"


def _failure_text(gh, workflow, run, branch):
    sha = run.get("head_sha") or ""
    attempts = range(1, (run.get("run_attempt") or 1) + 1)
    failed_jobs = []
    lines = _failures(gh, run, _failed_attempts(gh, run, attempts), failed_jobs)
    return "\n".join(
        [
            f"[`{workflow}` failed]({run.get('html_url')}) on `{branch}` at {sha}, also when "
            "run again. Failed jobs and steps of each failed attempt:",
            *lines,
            "",
            f"{CI_SHA_PREFIX}{sha} -->",
            _failed_marker(run, failed_jobs),
            _runs_marker(run, attempts),
        ]
    )


def _plain(text, size=200):
    """`text` on one line, without backticks, at most `size` characters: for a code span."""
    text = " ".join(str(text or "").split()).replace("`", "'")
    return text if len(text) <= size else text[: size - 1] + "…"


def _quoted(text):
    """A whole name in double quotes, on one line, for an issue title."""
    text = " ".join(str(text or "").split()).replace("`", "'").replace('"', "'")
    return f'"{text}"'


def _test_subject(title, path, place):
    """What an annotation's title or place names in an issue title: the test, by the last
    segment of its title and its file name, else the file and line."""
    if title:
        name = CI_TEST_SEGMENT.split(" ".join(title.split()))[-1]
        where = f" in {posixpath.basename(path)}" if path else ""
        return {"kind": "test", "name": name, "subject": f"Test {_quoted(name)}{where}"}
    line = place.removeprefix(path).removeprefix(":")
    subject = f"Line {line} of {path}" if line else path
    return {"kind": "place", "name": place, "subject": subject}


def ci_title(failure, branch):
    """The title of a flaky failure's issue: what fails, where, and how often."""
    if failure["kind"] == "start":
        return _fit(f"{failure['subject']} does not start at times on {branch}")
    if failure["kind"] == "run":
        return _fit(f"{failure['subject']} on {branch}")
    return _fit(f"{failure['subject']} fails at times on {branch}")


def red_title(workflow, failures, branch):
    """The title of a red branch's bug issue, from the failures of its latest attempt."""
    failures = list({failure["key"]: failure for failure in failures}.values())
    if len(failures) == 1:
        [failure] = failures
        verb = "does not start" if failure["kind"] == "start" else "fails"
        return _fit(f"{failure['subject']} {verb} on {branch}")
    kinds = {failure["kind"] for failure in failures}
    if len(failures) > CI_TITLE_FAILURES or len(kinds) > 1 or kinds & {"place", "start"}:
        return _fit(f"{workflow} fails in {len(failures)} ways on {branch}")
    names = [_quoted(failure["name"]) for failure in failures]
    listed = f"{', '.join(names[:-1])} and {names[-1]}"
    return _fit(f"{kinds.pop().capitalize()}s {listed} in {workflow} fail on {branch}")


def _fit(title):
    """`title` as it is, or cut at the last word boundary within GitHub's limit."""
    if len(title) <= CI_TITLE:
        return title
    cut = title[: CI_TITLE + 1].rsplit(" ", 1)[0]
    return cut if cut else title[:CI_TITLE]


def _key(*parts):
    """The key of a failure in its issue's marker."""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _annotated(link, note):
    """The failure an error annotation of a failed job names: its title, such as a test's
    name, else its file and line; None for an annotation of the workflow itself."""
    message = _plain(note.get("message"))
    if note.get("annotation_level") != "failure" or CI_GENERIC.match(message):
        return None
    path = _plain(note.get("path"))
    if path == ".github" or path.startswith(".github/workflows/"):
        path = ""
    place = f"{path}:{note['start_line']}" if path and note.get("start_line") else path
    name = _plain(note.get("title")) or place
    if not name:
        return None
    names = ", ".join(f"`{text}`" for text in dict.fromkeys((name, place)) if text)
    title = " ".join(str(note.get("title") or "").split()).replace("`", "'")
    return {
        "key": _key("annotation", name),
        **_test_subject(title, path, place),
        "about": f"`{name}` failed",
        "line": f"- {link}: {names}" + (f": `{message}`" if message else ""),
    }


def _annotated_test(note):
    """The test an error annotation of a failed job names, as `checks.test_name` names it
    for comparing with a pull request's JUnit results, or "": its title, with its file."""
    message = " ".join(str(note.get("message") or "").split())
    if note.get("annotation_level") != "failure" or CI_GENERIC.match(message):
        return ""
    path = str(note.get("path") or "")
    if path == ".github" or path.startswith(".github/workflows/"):
        path = ""
    return test_name(note.get("title"), path)


def _job_failure(workflow, job, link, error=""):
    """The failure of a failed job without an annotation that names one: its job and its
    first failed step. A failed step is keyed by the step and `error`, the message of the
    job's first error annotation, not by the job: the same step failing the same way in
    several jobs or workflows, such as a download that a host refused, is one failure."""
    name = _plain(job.get("name"))
    failed = [s.get("name") for s in job.get("steps") or [] if s.get("conclusion") in FAILED]
    failed = [step for step in failed if _plain(step)]
    steps = [_plain(step) for step in failed]
    as_job = {
        "kind": "job",
        "name": job.get("name"),
        "subject": f"Job {_quoted(job.get('name'))} in {workflow}",
    }
    if not steps:
        return {
            "key": _key("job", name),
            **as_job,
            "about": f"job `{name}` failed",
            "line": f"- {link}",
        }
    # A step named like its job, such as the only step of a job, is named as the job.
    if steps[0].casefold() == name.casefold():
        subject = as_job
    else:
        what = f"Step {_quoted(failed[0])} of job {_quoted(job.get('name'))} in {workflow}"
        subject = {"kind": "step", "name": failed[0], "subject": what}
    about = f"step `{steps[0]}` failed" + (f" with `{error}`" if error else "")
    return {
        "key": _key("step", steps[0], error),
        # The key of NexKit 5.0 and earlier, by job and step, which older issues hold.
        "legacy": _key("step", name, steps[0]),
        "shared": True,
        "step": failed[0],
        "jobs": [job.get("name")],
        **subject,
        "about": about,
        "line": f"- {link}: {', '.join(f'`{step}`' for step in steps)}",
    }


def _first_error(notes):
    """The message of a failed job's first error annotation, on one line, or "". The
    runner's own "Process completed with exit code" says nothing about the failure."""
    for note in notes:
        message = _plain(note.get("message"))
        if note.get("annotation_level") == "failure" and not CI_GENERIC.match(message):
            return message
    return ""


def _merged(found, item, workflow):
    """Add `item` to `found`, {key: failure}: a failed step already there from another job
    gets the job's lines, and names the step and how many jobs it failed in."""
    failure = found.get(item["key"])
    if failure is None:
        found[item["key"]] = {**item, "jobs": list(item.get("jobs") or [])}
        return found[item["key"]]
    for job in item.get("jobs") or []:
        if job not in failure["jobs"]:
            failure["jobs"].append(job)
    if failure.get("shared") and len(failure["jobs"]) > 1:
        count = len(failure["jobs"])
        what = f"Step {_quoted(failure['step'])} in {count} jobs of {workflow}"
        failure.update(kind="step", name=failure["step"], subject=what)
    return failure


def _attempt_failures(gh, workflow, run, attempt):
    """The failures of one failed attempt, in the order of its jobs. A step that failed the
    same way in several jobs is one failure, with a line for each job."""
    found = {}
    for job in gh.attempt_jobs(run["id"], attempt):
        if job.get("conclusion") not in FAILED:
            continue
        link = f"[{job.get('name')}]({job['html_url']})" if job.get("html_url") else job.get("name")
        notes = gh.check_run_annotations(job["id"]) if job.get("id") else []
        named = [failure for failure in (_annotated(link, note) for note in notes) if failure]
        for item in named or [_job_failure(workflow, job, link, _first_error(notes))]:
            failure = _merged(found, {**item, "lines": []}, workflow)
            if item["line"] not in failure["lines"]:
                failure["lines"].append(item["line"])
    return list(found.values()) or [
        {
            "key": _key("start"),
            "kind": "start",
            "name": workflow,
            "subject": workflow,
            "about": "the run failed before any job did",
            "line": "- The attempt failed before any job did; open it for the reason.",
            "lines": ["- The attempt failed before any job did; open it for the reason."],
        }
    ]


def _flaky_failures(gh, workflow, run, unseen):
    """The failures of the attempts among `unseen` that failed, each once, with its lines
    for each attempt it failed in; empty when none of them failed."""
    found = {}
    for attempt in _failed_attempts(gh, run, unseen):
        for item in _attempt_failures(gh, workflow, run, attempt):
            failure = _merged(found, {**item, "attempts": {}}, workflow)
            lines = failure["attempts"].setdefault(attempt, [])
            lines += [line for line in item["lines"] if line not in lines]
    return list(found.values())


def _run_failure(workflow, run, failures):
    """The failures of one run that would open too many issues, as one failure of the run."""
    attempts = {}
    for failure in failures:
        for attempt, lines in failure["attempts"].items():
            attempts.setdefault(attempt, []).extend(lines)
    count = len(failures)
    return {
        "key": _key("run", str(run["id"])),
        "kind": "run",
        "name": str(run["id"]),
        "subject": f"Run {run['id']} of {workflow} failed in {count} ways at once",
        "about": (
            f"{count} failures without an issue showed at once, more than NexKit opens issues "
            "for, so they likely have one cause, such as the runner or a service"
        ),
        "attempts": dict(sorted(attempts.items())),
    }


def _flaky_text(workflow, run, branch, failure, unseen):
    """The record of a failure in a run that passed only when run again, or that failed and
    a newer run overtook, for the run's attempts in `unseen`."""
    link = f"[A run of `{workflow}`]({run.get('html_url')}) on `{branch}` at {run.get('head_sha')}"
    if run.get("conclusion") in FAILED:
        what = f"{link} failed, and a newer run overtook it."
    else:
        what = f"{link} passed in attempt {run.get('run_attempt') or 1} after failing."
    lines = [f"{what} Where this failure showed in each failed attempt:"]
    for attempt, found in failure["attempts"].items():
        url = f"{run.get('html_url')}/attempts/{attempt}"
        lines += ["", f"Attempt [{attempt}]({url}):", *found]
    return "\n".join([*lines, "", _runs_marker(run, unseen)])


def _issue_of(known, failure, state):
    """The `state` ("open" or "closed") issue of a failure: by its key among the issues of
    its workflow; for a failed step, also among those of every other workflow, and by the
    key that NexKit 5.0 and earlier gave it, by job and step."""
    key = failure["key"]
    issue = known[state].get(key)
    if issue is None and failure.get("shared"):
        issue = known["any_" + state].get(key) or known[state].get(failure.get("legacy"))
    return issue


def _record_flaky(gh, cfg, where, workflow, branch, known, failure, text):
    """Add a record to the open issue of its failure, or open one that links the latest
    closed one."""
    key, title = failure["key"], ci_title(failure, branch)
    issue = _issue_of(known, failure, "open")
    if issue:
        known["open"][key] = issue
        gh.comment(issue["number"], text)
        return f"{title}: recorded on #{issue['number']}."
    people = "".join(f"@{login} " for login in cfg["notify"])
    closed = _issue_of(known, failure, "closed")
    earlier = f" #{closed['number']}, now closed, was about the same failure." if closed else ""
    where_ = f"on `{branch}`" if failure.get("shared") else f"of `{workflow}` on `{branch}`"
    anywhere = ", in any job and workflow" if failure.get("shared") else ""
    intro = (
        f"Runs {where_} in which {failure['about']}{anywhere}: runs that passed only when "
        "run again, or that failed and a newer run overtook. Each record names the "
        "commit, the run, and where this failure showed in each failed attempt; later "
        f"records follow as comments.{earlier}"
    )
    body = f"{CI_FLAKY_PREFIX}{workflow} {key} -->\n{people}{intro}\n\n{text}"
    labels = ["bug", FLAKY_LABEL]
    issue = known["open"][key] = gh.create_issue(title, body, where.get("milestone"), labels)
    return f"{title}: opened #{issue['number']}."


def _record_flaky_run(gh, cfg, where, workflow, branch, known, run, unseen):
    """Record a run that passed only when run again, or that failed and a newer run
    overtook, on the issue of each failure in its attempts among `unseen`. Returns lines for
    the log, none when none of those attempts failed."""
    failures = _flaky_failures(gh, workflow, run, unseen)
    new = [failure for failure in failures if not _issue_of(known, failure, "open")]
    if len(new) > CI_NEW_ISSUES:
        failures = [f for f in failures if _issue_of(known, f, "open")]
        failures.append(_run_failure(workflow, run, new))
    lines = []
    for failure in failures:
        text = _flaky_text(workflow, run, branch, failure, unseen)
        lines.append(_record_flaky(gh, cfg, where, workflow, branch, known, failure, text))
    return lines


def _report_failure(gh, cfg, where, workflow, branch, known, run):
    text = _failure_text(gh, workflow, run, branch)
    issue = known["bug"]
    if issue:
        gh.comment(issue["number"], f"It fails again on a newer commit.\n\n{text}")
        line = f"`{workflow}` fails again; commented on #{issue['number']}."
    else:
        people = "".join(f"@{login} " for login in cfg["notify"])
        body = f"{CI_BUG_PREFIX}{workflow} -->\n{people}{text}"
        failures = _attempt_failures(gh, workflow, run, run.get("run_attempt") or 1)
        title = red_title(workflow, failures, branch)
        labels = ["bug", CI_LABEL]
        issue = known["bug"] = gh.create_issue(title, body, where.get("milestone"), labels)
        line = f"{title}; opened #{issue['number']}."
    known["shas"].add(run.get("head_sha") or "")
    known["reporter"][run["id"]] = issue["number"]
    return line


def _passed_later(gh, workflow, run, number, unseen):
    """Say on the bug issue that reported a run that a later attempt of it passed."""
    attempt = run.get("run_attempt") or 1
    text = (
        f"Attempt {attempt} of [the run of `{workflow}`]({run.get('html_url')}) at "
        f"{run.get('head_sha')} passed: the failure did not repeat when run again."
    )
    gh.comment(number, f"{text}\n\n{_runs_marker(run, unseen)}")
    return f"`{workflow}` passed in attempt {attempt}; commented on #{number}."


def _check_run(gh, cfg, where, workflow, branch, known, run, latest):
    """What one completed run on the default branch leads to: lines for the log."""
    attempt = run.get("run_attempt") or 1
    seen = known["seen"].get(run["id"], set())
    unseen = [n for n in range(1, attempt + 1) if n not in seen]
    lines = []
    if run.get("conclusion") in FAILED:
        if latest and attempt < 2:
            gh.rerun_failed_jobs(run["id"])
            return [f"Running the failed jobs of `{workflow}` on `{branch}` again."]
        if not unseen or (run.get("head_sha") or "") in known["shas"]:
            return []
        if latest:
            lines = [_report_failure(gh, cfg, where, workflow, branch, known, run)]
        else:
            lines = _record_flaky_run(gh, cfg, where, workflow, branch, known, run, unseen)
    elif run.get("conclusion") == "success" and attempt > 1 and unseen:
        reporter = known["reporter"].get(run["id"])
        if reporter:
            lines = [_passed_later(gh, workflow, run, reporter, unseen)]
        else:
            lines = _record_flaky_run(gh, cfg, where, workflow, branch, known, run, unseen)
    if lines:
        known["seen"].setdefault(run["id"], set()).update(unseen)
    return lines


def open_bugs(gh):
    """{workflow: the open bug issue about it} for the bug issues that autopilot opened for
    a CI failure on the default branch: the newest open one of each, which reports go to."""
    bugs = {}
    for found in nexkit_issues(gh, "open"):
        if found.get("state") == "open" and ci_workflow(found):
            bugs.setdefault(ci_workflow(found), found)
    return bugs


def _acts_on(run, bug_open):
    """Whether a sweep acts on a finished run on the default branch: one that failed or
    was run again, or one that passed while `bug_open()` says a bug issue about its
    workflow is open, since pull requests may wait for that workflow to pass again."""
    if run.get("conclusion") in FAILED or (run.get("run_attempt") or 1) > 1:
        return True
    return run.get("conclusion") == "success" and bug_open()


# The runs of each workflow that `ci_due` looks at.
CI_DUE_RUNS = 5


def ci_due(gh, cfg, branch):
    """Whether the newest finished run, on the default branch `branch`, of a workflow that
    sweeps watch is one a sweep acts on. A run that cannot be read is left to a sweep."""
    bugs = None
    for workflow in watched_workflows(cfg):
        try:
            runs = [
                run
                for run in gh.workflow_runs(workflow, branch, CI_DUE_RUNS)
                if run.get("status") == "completed" and on_default_branch(gh, run)
            ]
            if not runs:
                continue

            def bug_open(workflow=workflow):
                nonlocal bugs
                bugs = open_bugs(gh) if bugs is None else bugs
                return workflow in bugs

            if _acts_on(runs[0], bug_open):
                return True
        except GitHubError:
            return True
    return False


def on_default_branch(gh, run):
    """Whether a run tested this repository's default branch. GitHub matches the branch by
    the name of a run's head branch alone, which a pull request from a fork can share, and
    such a run's job and step names come from the fork."""
    repo = (run.get("head_repository") or {}).get("full_name")
    return repo == gh.repository and run.get("event") not in PULL_REQUEST_EVENTS


def check_ci(gh, cfg, where, branch, now=None):
    """For each workflow that sweeps watch (`watched_workflows`), whatever started its
    runs, its completed runs on the default branch that a sweep has not looked at. When
    the latest run fails, its failed jobs run once more, and a second failure opens a bug
    issue in scope, or comments on the one still open; a later attempt that passes says
    so there. A run that passed only when run again, or that failed and a newer run
    overtook, is recorded on an issue for each of its failures, as its failed jobs' error
    annotations name them. Markers in those issues record each run and attempt once.
    Returns lines for the job log."""
    since = (now or datetime.now(UTC)) - CI_LOOK_BACK
    lines = []
    for workflow in watched_workflows(cfg):
        try:
            runs = [
                run
                for run in gh.workflow_runs(workflow, branch, CI_RUNS_LIMIT)
                if on_default_branch(gh, run)
            ]
            # Only a run that failed, or that was run again, can lead to anything, so a
            # sweep after runs that passed at once reads no issue.
            candidates = [
                run
                for run in runs
                if run.get("status") == "completed"
                and (run.get("conclusion") in FAILED or (run.get("run_attempt") or 1) > 1)
            ]
            known = _ci_records(gh, workflow, ci_issues(gh, since)) if candidates else None
            # Oldest first, so records follow the order of the runs.
            for run in reversed(candidates):
                latest = run is runs[0]
                created = parse_iso(run.get("created_at"))
                recent = latest or created is None or created >= since
                if not recent and run["id"] not in known["reporter"]:
                    continue
                lines += _check_run(gh, cfg, where, workflow, branch, known, run, latest)
        except GitHubError as exc:
            lines.append(f"Could not check `{workflow}` on `{branch}`: {exc}")
    return lines


# -- Dependabot alerts -----------------------------------------------------------------


def _read_alerts(alerts, wanted):
    """(the open alerts of the severities `wanted`, why they cannot be read), through
    `alerts`, the client that holds the token for them, or None when the App gave none.
    (None, None) when GitHub failed for a reason that passes."""
    if alerts is None:
        return None, NO_ALERTS_PERMISSION
    try:
        return alerts.dependabot_alerts(wanted), None
    except GitHubError as exc:
        if alerts_off(exc):
            return None, ALERTS_TURNED_OFF
        limited = "rate limit" in str(exc).lower()
        return None, NO_ALERTS_PERMISSION if exc.status in (401, 403, 404) and not limited else None


def _show_problem(gh, branch, problem):
    """Keep why a sweep cannot read alerts on the tip of `branch`, as a status that says what
    is missing, and take it back once it can. Only a change writes anything."""
    tip = gh.branch_sha(branch)
    found = next((s for s in gh.commit_statuses(tip) if s.get("context") == DEPENDABOT_CONTEXT), {})
    if problem and (found.get("state") != "error" or found.get("description") != problem):
        gh.set_status(tip, DEPENDABOT_CONTEXT, "error", problem)
    elif not problem and found.get("state") == "error":
        gh.set_status(tip, DEPENDABOT_CONTEXT, "success", "Autopilot reads Dependabot alerts")


def alerts_problem(gh, branch):
    """What the last sweep on the tip of `branch` said was missing, or None."""
    statuses = gh.commit_statuses(gh.branch_sha(branch))
    found = next((s for s in statuses if s.get("context") == DEPENDABOT_CONTEXT), {})
    return found.get("description") if found.get("state") == "error" else None


def _advisories(alerts):
    """{advisory id: {"advisory", "alerts"}} for open alerts, in their order. An alert
    without a well-formed id cannot be marked, so it gets no issue."""
    found = {}
    for alert in alerts:
        advisory = alert.get("security_advisory") or {}
        ghsa = str(advisory.get("ghsa_id") or "")
        if GHSA_ID.fullmatch(ghsa) and isinstance(alert.get("number"), int):
            found.setdefault(ghsa, {"advisory": advisory, "alerts": []})["alerts"].append(alert)
    return found


def _advisory_issues(gh):
    """{advisory id: its issues that autopilot opened, newest first}."""
    found = {}
    for issue in nexkit_issues(gh):
        ghsa = dependabot_advisory(issue)
        if ghsa:
            found.setdefault(ghsa, []).append(issue)
    return found


def _covered(issue):
    """The numbers of the alerts that an advisory's issue covers."""
    match = DEPENDABOT.match(issue.get("body") or "")
    return {int(number) for number in match.group(2).split(",") if number} if match else set()


def _inert(text):
    """`text` from an advisory or a manifest, for an issue that drives an agent without a
    person's approval: on one line, and with nothing that closes a tag or a comment."""
    return _plain(text).replace("<", "‹").replace(">", "›")


def _package(alert):
    return ((alert.get("dependency") or {}).get("package") or {}).get("name") or "a package"


def _alert_line(alert):
    """One alert of an advisory's issue, in words that come from GitHub's own fields."""
    dependency = alert.get("dependency") or {}
    package = dependency.get("package") or {}
    vulnerability = alert.get("security_vulnerability") or {}
    number = alert["number"]
    link = f"[#{number}]({alert['html_url']})" if alert.get("html_url") else f"#{number}"
    line = f"- {link}: `{_inert(package.get('name'))}` ({_inert(package.get('ecosystem'))})"
    line += f" in `{_inert(dependency.get('manifest_path'))}`"
    versions = _plain(vulnerability.get("vulnerable_version_range"))
    patched = _plain((vulnerability.get("first_patched_version") or {}).get("identifier"))
    if versions:
        line += f", affected: `{versions}`"
    return line + (f", patched in `{patched}`" if patched else ", no patched version yet")


def _advisory_title(ghsa, found):
    """The title: prompts hold it outside their untrusted text, so it holds no words of the
    advisory, only what names the packages and the advisory."""
    names = list(dict.fromkeys(_inert(_package(alert)) for alert in found["alerts"]))
    packages = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return _fit(f"Dependabot: {packages} ({ghsa}, {_inert(found['advisory'].get('severity'))})")


def _open_advisory(gh, cfg, ghsa, found, earlier):
    """Open the issue of an advisory. Its text holds only what GitHub's advisory database
    and the repository's manifests say, each on one line, in a code span where it can
    mention nobody, since it drives an agent."""
    advisory, alerts = found["advisory"], found["alerts"]
    numbers = ",".join(str(alert["number"]) for alert in alerts)
    people = "".join(f"@{login} " for login in cfg["notify"])
    cve = _inert(advisory.get("cve_id"))
    named = f"[{ghsa}](https://github.com/advisories/{ghsa})" + (f", {cve}" if cve else "")
    closed = f"\n\n#{earlier['number']}, now closed, was about it too." if earlier else ""
    body = (
        f"{DEPENDABOT_PREFIX}{ghsa} {numbers} -->\n{people}Dependabot reports {named} "
        f"({_inert(advisory.get('severity'))}): `{_inert(advisory.get('summary'))}`\n\n"
        + "\n".join(_alert_line(alert) for alert in alerts)
        + "\n\nUpdate each package to a patched version. For a package without one, ask a "
        f"person how to go on.{closed}"
    )
    return gh.create_issue(_advisory_title(ghsa, found), body, None, ["bug", DEPENDABOT_LABEL])


def _still_open(gh, cfg, ghsa, issue, now):
    """Say once on the closed `issue` of an advisory that its alerts stayed open, a while
    after it closed, so a person decides. Returns lines for the log."""
    closed = parse_iso(issue.get("closed_at"))
    if closed is None or now - closed < DEPENDABOT_SETTLE:
        return []
    number = issue["number"]
    if any(
        by_bot(c) and DEPENDABOT_STILL_OPEN in (c.get("body") or "") for c in gh.comments(number)
    ):
        return []
    people = "".join(f"@{login} " for login in cfg["notify"])
    gh.comment(
        number,
        f"{DEPENDABOT_STILL_OPEN}\n{people}Dependabot still reports {ghsa} after this issue "
        "closed, and NexKit opens no second issue for the same alerts. Reopen this one, or "
        "dismiss the alerts.",
    )
    return [f"{ghsa} is still open after #{number} closed; said so there."]


def check_dependabot(gh, alerts, cfg, branch, now=None):
    """With `autopilot.dependabot`, open one issue for each advisory of an open alert at or
    above that severity, in no milestone, with a marker of the advisory. An open issue with
    that marker is enough. So that a fix an alert outlives never repeats, an advisory whose
    closed issue covers its alerts gets no second issue, only a word on that one; an alert
    no issue covered does. `alerts` is the client that holds the token for reading alerts,
    or None. Without it, or with alerts turned off, it opens nothing and leaves what is
    missing on the tip of `branch`. Returns lines for the job log."""
    level = cfg["autopilot"]["dependabot"]
    if not level:
        return []
    now = now or datetime.now(UTC)
    found, problem = _read_alerts(alerts, list(SEVERITIES[SEVERITIES.index(level) :]))
    lines = []
    try:
        if found is not None or problem:
            _show_problem(gh, branch, problem)
        if found is None:
            return [problem or "Could not read Dependabot alerts; the next sweep tries again."]
        issues = _advisory_issues(gh) if found else {}
        opened = 0
        for ghsa, advisory in _advisories(found).items():
            known = issues.get(ghsa, [])
            if any(issue.get("state") == "open" for issue in known):
                continue
            covered = set().union(*(_covered(issue) for issue in known))
            if known and {alert["number"] for alert in advisory["alerts"]} <= covered:
                lines += _still_open(gh, cfg, ghsa, known[0], now)
                continue
            if opened < DEPENDABOT_NEW_ISSUES:
                issue = _open_advisory(gh, cfg, ghsa, advisory, known[0] if known else None)
                opened += 1
                lines.append(f"{ghsa}: opened #{issue['number']}.")
    except GitHubError as exc:
        lines.append(f"Could not go through Dependabot alerts: {exc}")
    return lines


# -- pull requests that wait for the default branch -----------------------------------

# The runs of a workflow on the default branch that show whether it passes again.
CI_WAIT_RUNS = 20


def _recorded_failure(gh, issue):
    """What the latest report on a red branch's bug issue records of its failed attempt:
    the run, the failed commands of its failed jobs, and for each command the tests that
    failed in the jobs that ran it, or None when no report does. A job without recorded
    tests, as in reports from before NexKit recorded them, adds none."""
    texts = [issue.get("body") or ""]
    texts += [c.get("body") or "" for c in gh.comments(issue["number"]) if by_bot(c)]
    for text in reversed(texts):
        match = CI_FAILED.search(text)
        if not match:
            continue
        try:
            record = json.loads(base64.b64decode(match.group(1)))
        except ValueError:
            return None
        run = record.get("run") if isinstance(record, dict) else None
        if not isinstance(run, int) or isinstance(run, bool):
            return None
        jobs = [job for job in record.get("jobs") or [] if isinstance(job, dict)]
        commands, tests = set(), {}
        for job in jobs:
            names = {t for t in _strings(job.get("tests")) if t}
            for command in _strings(job.get("commands")):
                if command:
                    commands.add(command)
                    tests.setdefault(command, set()).update(names)
        return {"run": run, "commands": commands, "tests": tests}
    return None


def _strings(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def same_failure(check, recorded):
    """Whether a pull request's failing check fails as the default branch's latest failed
    attempt in `recorded` does: it runs a command that failed there, by its first line,
    and the failing tests match. When both sides name failing tests (its JUnit
    `failed_tests`, the failed jobs' error annotations), every test failing in the check
    failed there too. When only one side names tests, the failures differ. When neither
    does, as for a build or lint step, the command decides."""
    command = command_line(check.get("run"))
    if not command or command not in recorded["commands"]:
        return False
    ours = {name for name in _strings(check.get("failed_tests")) if name}
    theirs = recorded["tests"].get(command) or set()
    if not ours and not theirs:
        return True
    if not ours or not theirs:
        return False
    return all(any(same_test(name, other) for other in theirs) for name in ours)


def passes_again(gh, workflow, branch, since):
    """Whether `workflow` passes on the default branch `branch` again: its newest run
    there that passed or failed passed, and is run `since` or a newer one. A run that is
    still running does not count, so neither does an older run while `since` runs again."""
    for run in gh.workflow_runs(workflow, branch, CI_WAIT_RUNS):
        if run.get("status") != "completed" or not on_default_branch(gh, run):
            continue
        if run.get("conclusion") not in (*FAILED, "success"):
            continue
        return run["id"] >= since and run.get("conclusion") == "success"
    return False


def ci_wait(gh, cfg, decision, checks, branch):
    """What a pull request's round waits for instead of an automatic fix round, or None:
    an open bug issue for a workflow that sweeps watch, which still fails on the default
    branch `branch`, when every failing check fails as the latest failed attempt the issue
    records does (see `same_failure`). A pull request's checks are NexKit's own,
    not jobs and steps of that workflow, so a check and a step are the same when they run
    the same command, by its first line, and fail the same tests. Only for a pull request
    into the default branch whose issue autopilot takes, since sweeps end the wait, and
    never for the bug issue or its parts."""
    failing = [
        check for check in checks or [] if isinstance(check, dict) and not check.get("passed")
    ]
    workflows = watched_workflows(cfg)
    if not failing or not workflows or not takes(cfg):
        return None
    if decision.get("base") != branch:
        return None
    bugs = {name: bug for name, bug in open_bugs(gh).items() if name in workflows}
    if not bugs:
        return None
    issue = gh.issue(decision["issue"])
    if not taken(gh, cfg, issue):
        return None
    own = {issue["number"], *(parent["number"] for parent in lineage(gh, issue))}
    for workflow in workflows:
        bug = bugs.get(workflow)
        if not bug or bug["number"] in own:
            continue
        recorded = _recorded_failure(gh, bug)
        if not recorded or not all(same_failure(check, recorded) for check in failing):
            continue
        if passes_again(gh, workflow, branch, recorded["run"]):
            continue
        return {
            "bug": bug["number"],
            "workflow": workflow,
            "branch": branch,
            "run": recorded["run"],
        }
    return None


def ci_wait_message(wait):
    """What the round that waits says."""
    bug, workflow, branch = wait["bug"], wait["workflow"], wait["branch"]
    return (
        f"Every failing check fails as `{workflow}` does on `{branch}`, which #{bug} is "
        "about: it runs a command that failed there and fails only tests that failed there, "
        "or neither side names a failing test. So NexKit starts no fix round for it here. "
        "Once "
        f"`{workflow}` "
        f"passes on `{branch}` again, or #{bug} is closed, NexKit autopilot starts the "
        f"automatic fix round, which merges `{branch}` first. Waiting uses no automatic fix "
        "round."
    )


def failed_checks(gh, head, names):
    """The required checks that failed on the commit `head` while its merge waited for
    them: [{name, url, commands, tests}]. `names` are the checks GitHub's refusal names;
    without them, every check run of the commit that failed. The check run of a GitHub
    Actions job is that job, so its log and error annotations give the commands of its
    `run` steps that failed and the tests the annotations name, as for a red default
    branch (see `_failed_jobs`). A check that is no check run, such as a commit status,
    has neither, nor has one that cannot be read."""
    try:
        runs = [run for run in gh.check_runs(head) if run.get("conclusion") in FAILED]
    except GitHubError:
        runs = []
    wanted = list(dict.fromkeys(names)) or list(
        dict.fromkeys(run["name"] for run in runs if run.get("name"))
    )
    found = []
    for name in wanted:
        item = {"name": name, "url": "", "commands": [], "tests": []}
        run = next((run for run in runs if run.get("name") == name), None)
        if run and run.get("id"):
            item["url"] = run.get("html_url") or ""
            item["commands"] = failed_commands(_job_log(gh, run))
            try:
                notes = gh.check_run_annotations(run["id"])
            except GitHubError:
                notes = []
            item["tests"] = list(dict.fromkeys(filter(None, map(_annotated_test, notes))))
        found.append(item)
    return found


def as_checks(failed):
    """The required checks of `failed_checks` as the checks `ci_wait` compares: one for
    each failed command, and one without a command, which fails as nothing on the default
    branch does, for a check whose commands are not known."""
    return [
        {"name": item["name"], "passed": False, "run": command, "failed_tests": item["tests"]}
        for item in failed
        for command in item["commands"] or [""]
    ]


def ci_wait_over(gh, wait):
    """Why a pull request's wait for a bug issue is over, or None while it lasts: the
    issue is closed, or its workflow passes on the default branch again."""
    bug, workflow, branch = wait.get("bug"), wait.get("workflow"), wait.get("branch")
    if not (isinstance(bug, int) and workflow and branch):
        return "the round that waits names no bug issue"
    if gh.issue(bug).get("state") != "open":
        return f"#{bug} is closed"
    if passes_again(gh, workflow, branch, wait.get("run") or 0):
        return f"`{workflow}` passes on `{branch}` again"
    return None


def _ci_step(gh, cfg, issue, pull, run, pull_comments, start):
    """The step for a pull request whose round waits for a bug issue about the default
    branch: it waits while the wait lasts, then gets the automatic fix round it waited
    for, which merges the base branch and counts toward `fix.max_auto_rounds`."""
    wait = run.get("ci_wait") or {}
    over = ci_wait_over(gh, wait)
    if over is None:
        reason = f"pull request #{pull} {ci_wait_reason(wait)}"
        return _step(issue, "waits", reason, person=False, ci_bug=wait["bug"])
    _, state = read_state(pull_comments)
    if (state or {}).get("auto_fixes", 0) >= cfg["fix"]["max_auto_rounds"]:
        do = f"comment `/nexkit fix` on pull request #{pull} for its failing checks"
        reason = person(do, f"{over}, but it has no automatic fix rounds left")
        return _step(issue, "waits", reason)
    reason = f"{over}, so pull request #{pull} gets the automatic fix round it waited for"
    fields = {**start, "target": pull, "auto": "true", "count": True}
    return _step(issue, "start", reason, command="fix", **fields)


def _count_round(gh, pull):
    """Count the automatic fix round a pull request waited for, as `report` counts one it
    starts, and end the wait in its state comment. Returns what takes that back, or None
    when there is no state comment."""
    comment_id, state = read_state(gh.comments(pull))
    if not state:
        return None
    before = json.loads(json.dumps(state))
    state["auto_fixes"] = (state.get("auto_fixes") or 0) + 1
    state.pop("ci_wait", None)
    save_state(gh, pull, comment_id, state)
    return lambda: save_state(gh, pull, comment_id, before)


def sweep(gh, cfg, workflow, ref, closed=None, busy=None, alerts=None):
    """Start what moves each issue in scope on. Returns lines for the job log. Parts that
    a split creates are in scope at once, so the sweep looks again after a split. A CI
    failure on the default branch comes first, so its bug issue is planned first, and then
    each Dependabot advisory; `alerts` is the client that may read them (see
    `check_dependabot`). Adds each pull request it started a command on, or merged, to
    `busy`. `closed`, the issue whose closing started the sweep, changes nothing: `done`
    reads what is done from GitHub, so a sweep that replaced this one loses nothing."""
    busy = set() if busy is None else busy
    lines = check_ci(gh, cfg, locate(gh, cfg), ref)
    lines += check_dependabot(gh, alerts, cfg, ref)
    lines += label_own(gh)
    for _ in range(PASSES):
        where, steps = preview(gh, cfg)
        splits = [step for step in steps if step["state"] == "split"]
        for step in steps:
            if step["state"] in ("start", "merge", "close"):
                busy.update(n for n in (step.get("target"), step.get("pull")) if n)
            try:
                if step["state"] == "split":
                    lines.append(split(gh, step))
                elif step["state"] == "close":
                    lines += close(gh, cfg, step, workflow, ref)
                elif step["state"] == "start":
                    lines.append(start(gh, step, workflow, ref))
                elif step["state"] == "merge":
                    # `report` holds what a merge leads to; it imports this module.
                    from .report import merge_waiting

                    lines.append(merge_waiting(gh, cfg, step, workflow, ref))
            except GitHubError as exc:
                what = {
                    "split": "split",
                    "merge": "merge the pull request of",
                    "close": "close",
                }.get(step["state"], f"start /nexkit {step.get('command')} on")
                target = step.get("target") or step.get("pull") or step["number"]
                lines.append(f"Could not {what} #{target}: {exc}")
        if not splits:
            break
    for step in steps:
        if step["state"] != "waits":
            continue
        try:
            settle(gh, step)
            line = mention(gh, cfg, step)
        except GitHubError as exc:
            line = f"Could not mention anyone on #{step['number']}: {exc}"
        lines += [line] if line else []
    # A sweep that a closed issue started names it in its note, but what it says comes
    # from what GitHub shows, so a sweep that replaced it in the queue says it too.
    lines += done(gh, cfg, where)
    return lines or [f"Autopilot: nothing to start in {where['name']}."]
