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
import hashlib
import json
import re
import string
from datetime import UTC, datetime, timedelta

from .github import TRUSTED_ASSOCIATIONS, GitHubError
from .route import issue_from_branch, parse_command
from .state import (
    BOT_LOGIN,
    CONTINUED,
    FAILURE,
    PAUSED,
    RUNNING,
    SUCCESS,
    answers_marker,
    by_bot,
    latest_plan,
    plan_data,
    questions,
    round_questions,
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
# What the record keeps from one start to the next.
STICKY = ("split_asked", "titles_asked")
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
PART = re.compile(r"\A<!-- nexkit:part (\d+) -->")
# How deep parts go: the issue first split is at depth 0, its parts at 1, their parts at 2.
# An issue at this depth is not split again; a person decides.
MAX_DEPTH = 3
DEPTH_REASON = f"a part {MAX_DEPTH} levels deep, which autopilot does not split; a person decides"
# The most parent issues that a sweep walks up from an issue: GitHub's own nesting limit.
LINEAGE = 8
# A bug issue that autopilot opened because a workflow failed twice on the default branch,
# and the commit each of its reports is about.
CI_BUG_PREFIX = "<!-- nexkit:ci "
CI_BUG = re.compile(r"\A<!-- nexkit:ci ([A-Za-z0-9_.-]+) -->")
CI_SHA_PREFIX = "<!-- nexkit:ci-sha "
CI_SHA = re.compile(r"<!-- nexkit:ci-sha ([0-9a-f]+) -->")
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
# The longest failure name in an issue title.
CI_TITLE_NAME = 60
# The run attempts that a bug or flaky-run report records, where it ends the text, so each
# run and attempt is recorded once.
CI_RUNS_PREFIX = "<!-- nexkit:ci-runs "
CI_RUNS = re.compile(r"<!-- nexkit:ci-runs ([0-9. ]+) -->\s*\Z")
# The runs of each workflow a sweep looks at: one page, newest first, of which the latest
# run, those created in the last day and those a bug issue reports. Sweeps run at least
# hourly, so the day covers missed sweeps; the records say what is new.
CI_RUNS_LIMIT = 100
CI_LOOK_BACK = timedelta(days=1)
FAILED = {"failure", "timed_out"}
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


def locate(gh, cfg):
    """The scope's name for people and its milestone or label. Without an open milestone,
    the milestone scope holds nothing."""
    settings = cfg["autopilot"]
    if settings["scope"] == "label":
        label = settings["label"]
        return {"name": f"label `{label}`", "label": label, "milestone": None}
    milestone = current_milestone(gh)
    if not milestone:
        return {"name": "no open milestone", "label": None, "milestone": None}
    return {
        "name": f"milestone {milestone['title']}",
        "label": None,
        "milestone": milestone["number"],
    }


def scope(gh, cfg):
    """`locate` with the open issues in scope, oldest first, after the issues for CI."""
    where = locate(gh, cfg)
    if where["label"] is not None:
        items = gh.scoped_issues(label=where["label"])
    elif where["milestone"] is not None:
        items = gh.scoped_issues(milestone=where["milestone"])
    else:
        items = []
    issues = [i for i in items if "pull_request" not in i]
    # A bug issue for a CI failure on the default branch goes before everything else, and
    # an issue for flaky runs right after.
    issues.sort(key=lambda i: (_rank(i), i["number"]))
    return {**where, "issues": issues}


def taken(gh, cfg, issue):
    """Whether autopilot works on an issue: in scope, from someone it trusts."""
    return in_scope(locate(gh, cfg), issue) and trusted(gh, issue)


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
    if login == BOT_LOGIN:
        return _rank(issue) < 2 or _trusted_part(gh, issue)
    return bool(login) and gh.can_write(login)


def _trusted_part(gh, issue, depth=0):
    """A part that autopilot created: it starts with the part marker, and the issue it
    names is its parent and is trusted itself."""
    match = PART.match(issue.get("body") or "")
    if not match or depth > 5:
        return False
    parent = gh.parent_issue(issue["number"])
    if not parent or parent["number"] != int(match.group(1)) or not in_repository(gh, parent):
        return False
    if author(parent) == BOT_LOGIN:
        return _trusted_part(gh, parent, depth + 1)
    return gh.can_write(author(parent))


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
        if not match or author(child) != BOT_LOGIN or int(match.group(1)) != parent["number"]:
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


def waits_for(gh, issue):
    """Why an issue is not ready yet, or None: open issues that block it on GitHub. The
    order of sub-issues does not count; a part that needs another says so with a *blocked
    by* relationship."""
    blockers = [b["number"] for b in gh.blocked_by(issue["number"]) if b.get("state") == "open"]
    return "waits for " + ", ".join(f"#{n}" for n in blockers) if blockers else None


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


def answers(cfg, items):
    """[{question, answer}] when `autopilot.answer` is "recommended" and every question
    has a recommended option, otherwise None."""
    if cfg["autopilot"]["answer"] != "recommended":
        return None
    found = questions(items)
    if not found or not all(item["recommended"] for item in found):
        return None
    return [{"question": item["question"], "answer": item["recommended"]} for item in found]


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
            return f"a part of a split ran out of time again, after #{number} asked for a split"
    return DEPTH_REASON if depth(issue, above) >= MAX_DEPTH else None


def _stopped(gh, cfg, issue, run, record, start, command, target):
    """The step after a `/nexkit go` or a round that stopped: answer a blocked round's
    questions, ask once for a split after the agent ran out of time, or wait."""
    found = answers(cfg, run.get("questions")) if run.get("outcome") == "agent_blocked" else None
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


def _split(gh, issue, plan, data, record, start, pull=None):
    """The step for an issue whose latest plan is too large and suggests parts, or None:
    split it; or, when parts are titled like the issue, ask once for the plan again; or
    wait for a person at `MAX_DEPTH` and after a second plan with such titles."""
    if not (data or {}).get("too_large") or not data.get("split"):
        return None
    if depth(issue, lineage(gh, issue)) >= MAX_DEPTH:
        return _step(issue, "waits", DEPTH_REASON)
    repeated = repeated_titles(issue, data["split"])
    if repeated and record.get("titles_asked"):
        return _step(issue, "waits", "the parts of its plan are titled like the issue again")
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


def _planned(gh, cfg, issue, plan, last, record, start):
    """The step after a plan that needs nobody's comment."""
    data = plan_data(plan)
    if data is None:  # a plan from before NexKit kept its record
        if last.get("attention"):
            return _step(issue, "waits", last["attention"])
        return _step(issue, "start", "the plan needs nobody", command="go", **start)
    if data.get("too_large"):
        return _split(gh, issue, plan, data, record, start) or _step(
            issue, "waits", last.get("attention") or "the plan is too large and suggests no split"
        )
    if data.get("questions"):
        found = answers(cfg, data["questions"])
        if not found:
            return _step(issue, "waits", last.get("attention") or "the plan has questions")
        reason = "autopilot answers the plan's questions"
        return _step(issue, "start", reason, command="plan", answers=found, **start)
    return _step(issue, "start", "the plan needs nobody", command="go", **start)


def assess(gh, cfg, issue, open_pulls, now=None):
    """What autopilot does for one open issue in scope:

    - `active`: a run or pull request carries it on, or a command autopilot started has
      not run yet. It counts toward `autopilot.max_parallel`.
    - `start`: start `command` on `target`, after posting `answers` when there are any.
    - `split`: create the `parts` of a plan that is too large as sub-issues.
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
    start = {"basis": current, "target": number, "note": "", "remember": {}}
    runs = [run for _, run in run_comments(comments) if "command" in run]
    last = runs[-1] if runs else None
    if last and last.get("status") in ACTIVE:
        return _step(issue, "active", f"`/nexkit {last['command']}` is {last['status']}")
    plan = latest_plan(comments)
    if pull:
        # The plan autopilot asked for after the pull request's agent ran out of time may
        # split the issue. A plan that is too large but that a person chose to implement
        # anyway does not.
        asked = record.get("split_asked") and record.get("command") == "plan"
        data = plan_data(plan) if asked and plan else None
        split = _split(gh, issue, plan, data, record, start, pull) if data else None
        rounds = [run for _, run in run_comments(pull_comments) if "round" in run]
        if not split and rounds and rounds[-1].get("status") == FAILURE:
            split = _stopped(gh, cfg, issue, rounds[-1], record, start, "fix", pull)
        if split:
            return {**split, "pull": pull}
        return _step(issue, "active", f"pull request #{pull} is open")
    blocked = waits_for(gh, issue)
    if blocked:
        return _step(issue, "waits", blocked)
    if last is None or plan is None:
        return _step(issue, "start", "no plan yet", command="plan", **start)
    if last["command"] == "go":
        if last.get("status") == SUCCESS:
            return _step(issue, "waits", "its pull request was closed; a person decides")
        return _stopped(gh, cfg, issue, last, record, start, "go", number) or _step(
            issue, "waits", last.get("attention") or "the last `/nexkit go` failed"
        )
    if last.get("status") != SUCCESS:
        return _step(issue, "waits", last.get("attention") or "the last `/nexkit plan` failed")
    if _after_plan(comments, plan):
        return _step(
            issue, "start", "collaborators commented after the plan", command="plan", **start
        )
    return _planned(gh, cfg, issue, plan, last, record, start)


def carries_on(gh, cfg, decision, outcome, agent):
    """Whether autopilot takes over after a run that would otherwise need a person, so
    `report` mentions nobody: it answers a plan's or a blocked round's questions, splits a
    plan that is too large, or asks for a split after the agent ran out of time."""
    if not takes(cfg):
        return False
    agent = agent or {}
    output = agent.get("output") or {}
    splits = outcome == "planned" and bool(output.get("too_large"))
    if splits:
        handled = bool(output.get("split"))
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
        if splits:
            # Autopilot splits the issue, or plans it again for its parts' titles.
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
    stay within `autopilot.max_parallel`, oldest issue first."""
    where = scope(gh, cfg)
    if not where["issues"]:
        return where, []
    pulls = nexkit_pulls(gh)
    steps = [assess(gh, cfg, issue, pulls) for issue in where["issues"]]
    limit = cfg["autopilot"]["max_parallel"]
    # An issue with an open pull request is in progress already.
    busy = sum(step["state"] == "active" or bool(step.get("pull")) for step in steps)
    for step in steps:
        if step["state"] != "start" or step.get("pull"):
            continue
        if limit is not None and busy >= limit:
            step.update(state="waits", reason=f"{busy} issues are in progress (max_parallel)")
            continue
        busy += 1
    return where, steps


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
    inputs = {"command": command, "number": str(target), "note": step["note"], "auto": AUTO}
    started = gh.dispatch(workflow, ref, inputs, run_details=True) or {}
    record = {"command": command, "target": target}
    if started.get("html_url"):
        record["url"] = started["html_url"]
    run = f" in [a new run]({record['url']})" if record.get("url") else ""
    on = "" if target == step["number"] else f" on #{target}"
    text = f"NexKit autopilot started `/nexkit {command}`{on}{run}: {step['reason']}."
    _save_record(gh, step, record, text, now)
    return f"Started /nexkit {command} on #{target}: {step['reason']}."


def split(gh, step, now=None):
    """Create the parts of a plan that is too large as sub-issues of the issue, in order,
    with its milestone and labels, and close its superseded pull request."""
    number = step["number"]
    parent = gh.issue(number)
    milestone = (parent.get("milestone") or {}).get("number")
    labels = [label["name"] for label in parent.get("labels") or [] if label.get("name")]
    plan = f" from [its plan]({step['plan_url']})" if step.get("plan_url") else ""
    created = []
    for index, part in enumerate(step["parts"], 1):
        body = (
            f"<!-- nexkit:part {number} -->\n{part['body'].strip()}\n\n"
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
        gh.comment(step["pull"], f"Superseded by {listing}, the parts of #{number}.")
        gh.close_pull(step["pull"])
    text = f"NexKit autopilot split this issue as its plan suggests: {listing}."
    parts = [issue["number"] for issue in created]
    _save_record(gh, step, {"command": "split", "target": number, "parts": parts}, text, now)
    return f"Split #{number} into {listing}."


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


# -- the default branch's CI ----------------------------------------------------------


def ci_workflow(issue):
    """The workflow a bug issue that autopilot opened for a CI failure is about, or None."""
    match = CI_BUG.match(issue.get("body") or "")
    return match.group(1) if match and author(issue) == BOT_LOGIN else None


def flaky_workflow(issue):
    """The workflow an issue that autopilot opened for flaky runs is about, or None."""
    match = CI_FLAKY.match(issue.get("body") or "")
    return match.group(1) if match and author(issue) == BOT_LOGIN else None


def flaky_failure(issue):
    """The key of the failure an issue that autopilot opened for flaky runs is about; None
    for one from NexKit 4.3.0, which holds every failure of its workflow, and for others."""
    match = CI_FLAKY.match(issue.get("body") or "")
    return match.group(2) if match and author(issue) == BOT_LOGIN else None


def _rank(issue):
    """0 for a bug issue for a CI failure, 1 for an issue for flaky runs, else 2."""
    if ci_workflow(issue) is not None:
        return 0
    return 1 if flaky_workflow(issue) is not None else 2


def _ci_records(gh, workflow):
    """What the issues that autopilot opened for `workflow`, open or closed, hold: the
    open bug issue, the open and the latest closed issue of each flaky failure, the commits
    bug issues report, the attempts recorded for each run, and for each run a bug issue
    reports, that issue's number."""
    found = {"bug": None, "open": {}, "closed": {}, "shas": set(), "seen": {}, "reporter": {}}
    # Newest first, so the first issue found for a failure is its latest.
    for issue in gh.issues_by(BOT_LOGIN):
        bug = ci_workflow(issue) == workflow
        if not bug and flaky_workflow(issue) != workflow:
            continue
        state = "open" if issue.get("state") == "open" else "closed"
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


def _failed_jobs(gh, run, attempt):
    lines = []
    for job in gh.attempt_jobs(run["id"], attempt):
        if job.get("conclusion") not in FAILED:
            continue
        steps = [s.get("name") for s in job.get("steps") or [] if s.get("conclusion") in FAILED]
        failed = f": {', '.join(f'`{name}`' for name in steps if name)}" if steps else ""
        link = f"[{job.get('name')}]({job['html_url']})" if job.get("html_url") else job.get("name")
        lines.append(f"- {link}{failed}")
    return lines or ["- The attempt failed before any job did; open it for the reason."]


def _failed_attempts(gh, run, attempts):
    """The attempts among `attempts` that failed. The latest attempt is the run itself."""
    latest = run.get("run_attempt") or 1
    failed = []
    for attempt in attempts:
        found = run if attempt == latest else gh.run_attempt(run["id"], attempt) or {}
        if found.get("conclusion") in FAILED:
            failed.append(attempt)
    return failed


def _failures(gh, run, failed):
    """Each failed attempt, linked, with its failed jobs and steps."""
    lines = []
    for attempt in failed:
        url = f"{run.get('html_url')}/attempts/{attempt}"
        lines += ["", f"Attempt [{attempt}]({url}):", *_failed_jobs(gh, run, attempt)]
    return lines


def _failure_text(gh, workflow, run, branch):
    sha = run.get("head_sha") or ""
    attempts = range(1, (run.get("run_attempt") or 1) + 1)
    return "\n".join(
        [
            f"[`{workflow}` failed]({run.get('html_url')}) on `{branch}` at {sha}, also when "
            "run again. Failed jobs and steps of each failed attempt:",
            *_failures(gh, run, _failed_attempts(gh, run, attempts)),
            "",
            f"{CI_SHA_PREFIX}{sha} -->",
            _runs_marker(run, attempts),
        ]
    )


def _plain(text, size=200):
    """`text` on one line, without backticks, at most `size` characters: for a code span."""
    text = " ".join(str(text or "").split()).replace("`", "'")
    return text if len(text) <= size else text[: size - 1] + "…"


def _shortened(name, size=CI_TITLE_NAME):
    """A long name's start and end, for an issue title."""
    half = (size - 1) // 2
    return name if len(name) <= size else f"{name[:half]}…{name[-half:]}"


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
    return {
        "key": _key("annotation", name),
        "title": f"`{_shortened(name)}` fails at times",
        "about": f"`{name}` failed",
        "line": f"- {link}: {names}" + (f": `{message}`" if message else ""),
    }


def _job_failure(workflow, job, link):
    """The failure of a failed job without an annotation that names one: its job and its
    first failed step."""
    name = _plain(job.get("name"))
    steps = [_plain(s.get("name")) for s in job.get("steps") or [] if s.get("conclusion") in FAILED]
    steps = [step for step in steps if step]
    if not steps:
        return {
            "key": _key("job", name),
            "title": f"Job `{_shortened(name)}` of `{workflow}` fails at times",
            "about": f"job `{name}` failed",
            "line": f"- {link}",
        }
    return {
        "key": _key("step", name, steps[0]),
        "title": (
            f"Step `{_shortened(steps[0])}` of job `{_shortened(name)}` in `{workflow}` "
            "fails at times"
        ),
        "about": f"step `{steps[0]}` of job `{name}` failed",
        "line": f"- {link}: {', '.join(f'`{step}`' for step in steps)}",
    }


def _attempt_failures(gh, workflow, run, attempt):
    """The failures of one failed attempt, in the order of its jobs."""
    found = []
    for job in gh.attempt_jobs(run["id"], attempt):
        if job.get("conclusion") not in FAILED:
            continue
        link = f"[{job.get('name')}]({job['html_url']})" if job.get("html_url") else job.get("name")
        notes = gh.check_run_annotations(job["id"]) if job.get("id") else []
        named = [failure for failure in (_annotated(link, note) for note in notes) if failure]
        found += named or [_job_failure(workflow, job, link)]
    return found or [
        {
            "key": _key("start"),
            "title": f"Starting `{workflow}` fails at times",
            "about": "the run failed before any job did",
            "line": "- The attempt failed before any job did; open it for the reason.",
        }
    ]


def _flaky_failures(gh, workflow, run, unseen):
    """The failures of the attempts among `unseen` that failed, each once, with its lines
    for each attempt it failed in; empty when none of them failed."""
    found = {}
    for attempt in _failed_attempts(gh, run, unseen):
        for item in _attempt_failures(gh, workflow, run, attempt):
            failure = found.setdefault(item["key"], {**item, "attempts": {}})
            lines = failure["attempts"].setdefault(attempt, [])
            if item["line"] not in lines:
                lines.append(item["line"])
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
        "title": f"Run {run['id']} of `{workflow}` failed in {count} ways at once",
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


def _record_flaky(gh, cfg, where, workflow, branch, known, failure, text):
    """Add a record to the open issue of its failure, or open one that links the latest
    closed one."""
    key, title = failure["key"], f"{failure['title']} on `{branch}`"
    issue = known["open"].get(key)
    if issue:
        gh.comment(issue["number"], text)
        return f"{title}: recorded on #{issue['number']}."
    people = "".join(f"@{login} " for login in cfg["notify"])
    closed = known["closed"].get(key)
    earlier = f" #{closed['number']}, now closed, was about the same failure." if closed else ""
    intro = (
        f"Runs of `{workflow}` on `{branch}` in which {failure['about']}: runs that passed "
        "only when run again, or that failed and a newer run overtook. Each record names the "
        "commit, the run, and where this failure showed in each failed attempt; later "
        f"records follow as comments.{earlier}"
    )
    body = f"{CI_FLAKY_PREFIX}{workflow} {key} -->\n{people}{intro}\n\n{text}"
    labels = ["bug", *([where["label"]] if where.get("label") else [])]
    issue = known["open"][key] = gh.create_issue(title, body, where.get("milestone"), labels)
    return f"{title}: opened #{issue['number']}."


def _record_flaky_run(gh, cfg, where, workflow, branch, known, run, unseen):
    """Record a run that passed only when run again, or that failed and a newer run
    overtook, on the issue of each failure in its attempts among `unseen`. Returns lines for
    the log, none when none of those attempts failed."""
    failures = _flaky_failures(gh, workflow, run, unseen)
    new = [failure for failure in failures if failure["key"] not in known["open"]]
    if len(new) > CI_NEW_ISSUES:
        failures = [f for f in failures if f["key"] in known["open"]]
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
        labels = ["bug", *([where["label"]] if where.get("label") else [])]
        title = f"`{workflow}` fails on `{branch}`"
        issue = known["bug"] = gh.create_issue(title, body, where.get("milestone"), labels)
        line = f"`{workflow}` fails on `{branch}`; opened #{issue['number']}."
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


def check_ci(gh, cfg, where, branch, now=None):
    """For each workflow of `merge.after_workflows`, its completed runs on the default
    branch that a sweep has not looked at. When the latest run fails, its failed jobs run
    once more, and a second failure opens a bug issue in scope, or comments on the one
    still open; a later attempt that passes says so there. A run that passed only when run
    again, or that failed and a newer run overtook, is recorded on an issue for each of its
    failures, as its failed jobs' error annotations name them. Markers in those issues
    record each run and attempt once. Returns lines for the job log."""
    since = (now or datetime.now(UTC)) - CI_LOOK_BACK
    lines = []
    for workflow in cfg["merge"]["after_workflows"]:
        try:
            runs = gh.workflow_runs(workflow, branch, CI_RUNS_LIMIT)
            # Only a run that failed, or that was run again, can lead to anything, so a
            # sweep after runs that passed at once reads no issue.
            candidates = [
                run
                for run in runs
                if run.get("status") == "completed"
                and (run.get("conclusion") in FAILED or (run.get("run_attempt") or 1) > 1)
            ]
            known = _ci_records(gh, workflow) if candidates else None
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


def sweep(gh, cfg, workflow, ref, closed=None):
    """Start what moves each issue in scope on. Returns lines for the job log. Parts that
    a split creates are in scope at once, so the sweep looks again after a split. A CI
    failure on the default branch comes first, so its bug issue is planned first."""
    lines = check_ci(gh, cfg, locate(gh, cfg), ref)
    for _ in range(PASSES):
        where, steps = preview(gh, cfg)
        splits = [step for step in steps if step["state"] == "split"]
        for step in steps:
            try:
                if step["state"] == "split":
                    lines.append(split(gh, step))
                elif step["state"] == "start":
                    lines.append(start(gh, step, workflow, ref))
            except GitHubError as exc:
                what = (
                    "split" if step["state"] == "split" else f"start /nexkit {step['command']} on"
                )
                lines.append(f"Could not {what} #{step['target']}: {exc}")
        if not splits:
            break
    if closed and not where["issues"]:
        lines += done(gh, cfg, where, closed)
    return lines or [f"Autopilot: nothing to start in {where['name']}."]
