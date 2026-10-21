"""Autopilot: work through the open issues in scope without commands.

A sweep looks at every open issue in scope and starts the one command that moves it on:
`/nexkit plan` for a ready issue, `/nexkit go` for a plan that needs nobody. Sweeps run in
the `maintain` job, one at a time in the `nexkit-autopilot` concurrency group, and start
commands with `workflow_dispatch`, like automatic rounds. They hold no agent credential and
never check out repository code. Events start them: the end of a run, an issue that opens,
closes or comes into scope, a closed milestone; the hourly schedule catches the rest.

Everything a sweep knows comes from GitHub: the issues in scope, their dependencies and
sub-issues, and NexKit's own markers. One record comment per issue says which command
autopilot started last, and for which state of the issue, so a command starts only once.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta

from .github import TRUSTED_ASSOCIATIONS, GitHubError
from .route import issue_from_branch, parse_command
from .state import (
    BOT_LOGIN,
    CONTINUED,
    PAUSED,
    RUNNING,
    SUCCESS,
    by_bot,
    latest_plan,
    run_comments,
)
from .usage import iso, parse_iso

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
# A command autopilot started that has not posted its run comment after this long did not
# run, for example because `route` refused it. Autopilot does not start it again.
LOST = timedelta(hours=6)
# Outcomes after which a run already started the next one by itself.
CHAINED = {"auto_fix", "conflict_fix"}


def takes(cfg):
    return bool(cfg.get("autopilot", {}).get("enabled"))


def request(gh, workflow, ref, closed=None):
    """Start a sweep. `closed` is the issue whose closing started it: when that empties
    the scope, the sweep says so on it."""
    gh.dispatch(workflow, ref, {**SWEEP, "note": str(closed or "")})


def request_safely(gh, cfg, workflow, ref, closed=None):
    """`request` for a run that must not fail because of it: errors become a line."""
    if not takes(cfg):
        return []
    try:
        request(gh, workflow, ref, closed)
    except GitHubError as exc:
        return [f"Could not start an autopilot sweep: {exc}"]
    return ["Started an autopilot sweep."]


def concerns(cfg, decision):
    """Whether an issue or milestone event can change what is in scope: a label event only
    for its label, a milestone event only for the milestone scope."""
    settings = cfg["autopilot"]
    event = decision.get("event")
    if event == "labeled":
        return settings["scope"] == "label" and decision.get("label") == settings["label"]
    if event in ("milestoned", "milestone_closed"):
        return settings["scope"] == "milestone"
    return True


def after_run(gh, cfg, decision, outcome, workflow, ref):
    """Start a sweep after a run, unless the run already started the next one itself or
    waits for a usage limit. A merge closed the issue, which may empty the scope."""
    if not takes(cfg) or outcome in CHAINED or outcome == "paused":
        return []
    closed = decision["issue"] if outcome == "merged" else None
    return request_safely(gh, cfg, workflow, ref, closed)


# -- scope -------------------------------------------------------------------------


def current_milestone(gh):
    """The earliest open milestone that has issues: by due date, then the oldest. Empty
    milestones are skipped; one whose issues are all closed stays current until a person
    closes it, so autopilot never moves on to the next milestone by itself."""
    found = [
        m for m in gh.milestones() if (m.get("open_issues") or 0) + (m.get("closed_issues") or 0)
    ]
    found.sort(key=lambda m: (m.get("due_on") is None, m.get("due_on") or "", m["number"]))
    return found[0] if found else None


def scope(gh, cfg):
    """The scope: its name for people, its milestone or label, and its open issues, oldest
    first. Without an open milestone, the milestone scope is empty."""
    settings = cfg["autopilot"]
    if settings["scope"] == "label":
        label = settings["label"]
        where = {"name": f"label `{label}`", "label": label, "milestone": None}
        items = gh.scoped_issues(label=label)
    else:
        milestone = current_milestone(gh)
        if not milestone:
            return {"name": "no open milestone", "label": None, "milestone": None, "issues": []}
        where = {"name": f"milestone {milestone['title']}", "label": None}
        where["milestone"] = milestone["number"]
        items = gh.scoped_issues(milestone=milestone["number"])
    issues = sorted((i for i in items if "pull_request" not in i), key=lambda i: i["number"])
    return {**where, "issues": issues}


def in_scope(where, issue):
    """Whether an issue, open or closed, belongs to the scope."""
    if where["label"] is not None:
        return where["label"] in {label.get("name") for label in issue.get("labels") or []}
    milestone = issue.get("milestone") or {}
    return where["milestone"] is not None and milestone.get("number") == where["milestone"]


def author(issue):
    return (issue.get("user") or {}).get("login") or ""


def trusted(gh, issue):
    """Autopilot skips a person's approval of the plan, so issue text drives an implement
    session. It takes only issues opened by someone with write access; only they and other
    writers can edit the issue."""
    login = author(issue)
    return bool(login) and login != BOT_LOGIN and gh.can_write(login)


def in_repository(gh, issue):
    """Whether an issue from the sub-issues API belongs to this repository: a parent can be
    in another repository of the same owner, with a number that means another issue here."""
    url = issue.get("repository_url")
    return not url or url.lower().endswith(f"/repos/{gh.repository}".lower())


def waits_for(gh, issue):
    """Why an issue is not ready yet, or None: an open issue that blocks it, or an open
    earlier sibling among its parent's sub-issues."""
    number = issue["number"]
    blockers = [b["number"] for b in gh.blocked_by(number) if b.get("state") == "open"]
    if blockers:
        return "waits for " + ", ".join(f"#{n}" for n in blockers)
    parent = gh.parent_issue(number)
    if parent and in_repository(gh, parent):
        for sibling in gh.sub_issues(parent["number"]):
            if sibling["number"] == number:
                break
            if sibling.get("state") == "open":
                return f"waits for #{sibling['number']}, an earlier part of #{parent['number']}"
    return None


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


def _after_plan(comments, plan):
    """Collaborators' comments after the latest plan, other than commands: changes that
    the plan does not hold yet."""
    at = next(i for i, c in enumerate(comments) if c["id"] == plan["id"])
    return [
        c
        for c in comments[at + 1 :]
        if c.get("author_association") in TRUSTED_ASSOCIATIONS
        and not by_bot(c)
        and parse_command(c.get("body"))[0] is None
    ]


# -- one issue -------------------------------------------------------------------


def _step(issue, state, reason="", **fields):
    return {
        "number": issue["number"],
        "title": issue.get("title", ""),
        "state": state,
        "reason": reason,
        **fields,
    }


def _lost(record, now):
    at = parse_iso(record.get("at"))
    return at is not None and now - at > LOST


def assess(gh, cfg, issue, open_pulls, now=None):
    """What autopilot does for one open issue in scope:

    - `active`: a run or pull request carries it on, or a command autopilot started has
      not run yet. It counts toward `autopilot.max_parallel`.
    - `start`: start `command` on `target`.
    - `waits`: nothing to start, for `reason`: not ready yet, or a person decides.
    """
    number = issue["number"]
    if not trusted(gh, issue):
        login = author(issue) or "an unknown user"
        return _step(issue, "waits", f"opened by @{login}, who cannot write to the repository")
    if gh.sub_issues(number):
        return _step(issue, "waits", "a parent issue: autopilot works on its sub-issues")
    comments = gh.comments(number)
    pull = open_pulls.get(number)
    pull_comments = gh.comments(pull) if pull else []
    current = basis(comments, pull_comments)
    _, record = find_record(comments)
    if record.get("basis") == current:
        command = f"`/nexkit {record['command']}`"
        if _lost(record, now or datetime.now(UTC)):
            return _step(issue, "waits", f"the {command} that autopilot started did not run")
        return _step(issue, "active", f"waits for the {command} that autopilot started")
    if pull:
        return _step(issue, "active", f"pull request #{pull} is open")
    runs = [run for _, run in run_comments(comments) if "command" in run]
    last = runs[-1] if runs else None
    if last and last.get("status") in ACTIVE:
        return _step(issue, "active", f"`/nexkit {last['command']}` is {last['status']}")
    blocked = waits_for(gh, issue)
    if blocked:
        return _step(issue, "waits", blocked)
    start = {"basis": current, "target": number, "note": ""}
    plan = latest_plan(comments)
    if last is None or plan is None:
        return _step(issue, "start", "no plan yet", command="plan", **start)
    if last["command"] == "go":
        if last.get("status") == SUCCESS:
            return _step(issue, "waits", "its pull request was closed; a person decides")
        return _step(issue, "waits", last.get("attention") or "the last `/nexkit go` failed")
    if last.get("status") != SUCCESS:
        return _step(issue, "waits", last.get("attention") or "the last `/nexkit plan` failed")
    if _after_plan(comments, plan):
        return _step(
            issue, "start", "collaborators commented after the plan", command="plan", **start
        )
    if last.get("attention"):
        return _step(issue, "waits", last["attention"])
    return _step(issue, "start", "the plan needs nobody", command="go", **start)


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
    stay within `autopilot.max_parallel`, oldest issue first."""
    where = scope(gh, cfg)
    if not where["issues"]:
        return where, []
    pulls = nexkit_pulls(gh)
    steps = [assess(gh, cfg, issue, pulls) for issue in where["issues"]]
    limit = cfg["autopilot"]["max_parallel"]
    busy = sum(step["state"] == "active" for step in steps)
    for step in steps:
        if step["state"] != "start":
            continue
        if limit is not None and busy >= limit:
            step.update(state="waits", reason=f"{busy} issues are in progress (max_parallel)")
            continue
        busy += 1
    return where, steps


def start(gh, step, workflow, ref, now=None):
    """Start a step's command and record it on the issue. Returns a line for the log."""
    command, target = step["command"], step["target"]
    inputs = {"command": command, "number": str(target), "note": step["note"], "auto": AUTO}
    started = gh.dispatch(workflow, ref, inputs, run_details=True) or {}
    record = {
        "command": command,
        "target": target,
        "basis": step["basis"],
        "at": iso(now or datetime.now(UTC)),
    }
    if started.get("html_url"):
        record["url"] = started["html_url"]
    run = f" in [a new run]({record['url']})" if record.get("url") else ""
    on = "" if target == step["number"] else f" on #{target}"
    text = f"NexKit autopilot started `/nexkit {command}`{on}{run}: {step['reason']}."
    body = f"{text}\n\n{record_marker(record)}"
    comment, _ = find_record(gh.comments(step["number"]))
    if comment:
        gh.update_comment(comment["id"], body)
    else:
        gh.comment(step["number"], body)
    return f"Started /nexkit {command} on #{target}: {step['reason']}."


def done(gh, cfg, where, closed):
    """Say once, on the issue whose closing emptied the scope, that the scope is done."""
    try:
        issue = gh.issue(closed)
    except GitHubError:
        return []
    if issue.get("state") != "closed" or not in_scope(where, issue):
        return []
    if any(by_bot(c) and DONE in (c.get("body") or "") for c in gh.comments(closed)):
        return []
    people = "".join(f"@{login} " for login in cfg["notify"])
    text = (
        f"{DONE}\n{people}NexKit autopilot finished {where['name']}: every issue in it is closed."
    )
    if where["milestone"] is not None:
        text += " Close the milestone to let autopilot start on the next one."
    gh.comment(closed, text)
    return [f"Autopilot finished {where['name']}."]


def sweep(gh, cfg, workflow, ref, closed=None):
    """Start what moves each issue in scope on. Returns lines for the job log."""
    where, steps = preview(gh, cfg)
    lines = []
    for step in steps:
        if step["state"] != "start":
            continue
        try:
            lines.append(start(gh, step, workflow, ref))
        except GitHubError as exc:
            lines.append(f"Could not start /nexkit {step['command']} on #{step['target']}: {exc}")
    if closed and not where["issues"]:
        lines += done(gh, cfg, where, closed)
    return lines or [f"Autopilot: nothing to start in {where['name']}."]
