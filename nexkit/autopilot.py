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
import posixpath
import re
import string
from datetime import UTC, datetime, timedelta

from .github import TRUSTED_ASSOCIATIONS, GitHubError
from .progress import STATUS_CONTEXTS, save_state
from .redact import Redactor
from .route import RUN_ID, issue_from_branch, parse_command
from .state import (
    BOT_LOGIN,
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
    latest_plan,
    plan_data,
    questions,
    read_state,
    round_questions,
    run_comments,
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
# A command autopilot started that has not posted its run comment after this long did not
# run, for example because `route` refused it, or GitHub dropped it from the queue.
# Autopilot starts it once more, then waits for a person. A run that continues a paused one
# and has not started after this long did not start either.
LOST = timedelta(hours=6)
# What the comment of a run that ended without a report says once a sweep finds it.
REPORTLESS = "the run ended without a report"
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
# What a red branch's report records of the latest failed attempt of its run: the run, and
# for each failed job the commands of its `run` steps that failed, from the job's log. A
# pull request whose failing checks run only those commands waits for the bug issue
# instead of a fix round.
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
# "RecordHistory > pages through more than twenty entries"; the last segment is the test.
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
FAILED = {"failure", "timed_out"}
# Events whose runs test a pull request rather than the branch they name.
PULL_REQUEST_EVENTS = {"pull_request", "pull_request_target"}
# Outcomes after which a run already started the next one by itself.
CHAINED = {"auto_fix", "conflict_fix", "retry", "branch_updated", "head_moved"}
# The outcome of a round whose checks fail as the default branch does while a bug issue
# for that failure is open: no fix round starts until the default branch passes again.
CI_WAITING = "ci_waiting"


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
    """Whether an issue or milestone event can change what is in scope: a milestone event
    only for the milestone scope. With `null` every open issue is in scope already."""
    if decision.get("event") in ("milestoned", "milestone_closed"):
        return cfg["autopilot"]["scope"] == "milestone"
    return True


def after_run(gh, cfg, decision, outcome, workflow, ref):
    """Start a sweep after a run, unless the run already started the next one itself or
    waits for a usage limit. A merge closed the issue, which may empty the scope."""
    if not takes(cfg) or outcome in CHAINED or outcome == "paused":
        return []
    closed = decision["issue"] if outcome in ("merged", "no_change") else None
    return request_safely(gh, cfg, workflow, ref, closed)


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
    open milestone, the milestone scope holds nothing."""
    if cfg["autopilot"]["scope"] is None:
        return {
            "name": "every open issue",
            "every": True,
            "milestone": None,
            "milestones": milestones(gh),
            "repository": gh.repository,
        }
    milestone = current_milestone(gh)
    if not milestone:
        return {"name": "no open milestone", "every": False, "milestone": None}
    return {
        "name": f"milestone {milestone['title']}",
        "every": False,
        "milestone": milestone["number"],
    }


def scope(gh, cfg):
    """`locate` with the open issues in scope. The issues for CI go first; with `null`,
    then the issues of each open milestone in the order of `milestones`, then the others;
    each group oldest first."""
    where = locate(gh, cfg)
    if where["every"]:
        items = gh.scoped_issues()
    elif where["milestone"] is not None:
        items = gh.scoped_issues(milestone=where["milestone"])
    else:
        items = []
    issues = [i for i in items if "pull_request" not in i]
    # A bug issue for a CI failure on the default branch goes before everything else, and
    # an issue for flaky runs right after. An issue whose milestone is closed goes with
    # those without one.
    order = {m["number"]: index for index, m in enumerate(where.get("milestones") or [])}
    issues.sort(
        key=lambda i: (
            _rank(i),
            order.get((i.get("milestone") or {}).get("number"), len(order)),
            i["number"],
        )
    )
    return {**where, "issues": issues}


def taken(gh, cfg, issue):
    """Whether autopilot works on an issue: in scope, from someone it trusts."""
    return in_scope(locate(gh, cfg), issue) and trusted(gh, issue)


def in_scope(where, issue):
    """Whether an issue, open or closed, belongs to the scope. With `null` every issue of
    this repository does; a blocker can be in another repository."""
    if where["every"]:
        url = issue.get("repository_url")
        return not url or url.lower().endswith(f"/repos/{where['repository']}".lower())
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


def waits_for(gh, issue, where=None):
    """(why an issue is not ready yet, whether a person must act), or None: open issues
    that block it on GitHub. The order of sub-issues does not count; a part that needs
    another says so with a *blocked by* relationship. Autopilot works on the blockers in
    its scope by itself; one outside it waits for a person."""
    blockers = [b for b in gh.blocked_by(issue["number"]) if b.get("state") == "open"]
    if not blockers:
        return None
    reason = "waits for " + ", ".join(f"#{b['number']}" for b in blockers)
    outside = [b for b in blockers if where is not None and not in_scope(where, b)]
    if outside:
        listed = ", ".join(f"#{b['number']}" for b in outside)
        return f"{reason}; {listed} {'is' if len(outside) == 1 else 'are'} not in scope", True
    return reason, False


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
REPEATED = (
    "the round after autopilot's answers asked again, and nobody commented since; a person decides"
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


def _stopped(gh, cfg, issue, run, record, start, command, target, comments):
    """The step after a `/nexkit go` or a round that stopped: answer a blocked round's
    questions, ask once for a split after the agent ran out of time, or wait. `comments`
    holds the comments of the target, then those of the issue or its pull request."""
    found = answers(cfg, run.get("questions")) if run.get("outcome") == "agent_blocked" else None
    if found and asked_again(*comments):
        return _step(issue, "waits", REPEATED)
    if found and len(found) == 1 and command == "go" and run.get("no_change"):
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


def _planned(gh, cfg, issue, plan, last, record, start, comments):
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
    labels = re.search(r"\((.*)\)", str(run.get("trigger") or ""))
    kinds = {kind.strip() for kind in labels.group(1).split(",")} if labels else set()
    return bool(run.get("autopilot")) or bool(kinds & {"auto", "autopilot"})


def _not_resumed(comment, run, now):
    """Why a run paused at the usage limit waits for a person, or None while NexKit
    resumes it by itself."""
    if not isinstance(run.get("resume"), dict):
        return run.get("attention") or "paused at the usage limit, and NexKit does not resume it"
    at = parse_iso(comment.get("created_at"))
    if at and now - at > PAUSE_WINDOW:
        return "paused at the usage limit longer than NexKit resumes runs"
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
        return _step(issue, "waits", "the run that continues it did not start") if lost else None
    if status != RUNNING or not _ended(gh, run):
        return None
    trigger = str(run.get("trigger") or "")
    command = run.get("command") or ("fix" if trigger.startswith("fix") else "review")
    stale = {"number": target, "comment": comment["id"]}
    again = record.get("retried") and (record.get("command"), record.get("target")) == (
        command,
        target,
    )
    if not _automatic(run) or again:
        return _step(issue, "waits", REPORTLESS, stale=stale)
    fields = {**start, "target": target, "remember": {"retried": True}}
    reason = f"{REPORTLESS}, so autopilot starts `/nexkit {command}` once more"
    return _step(issue, "start", reason, command=command, stale=stale, **fields)


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
    if not cfg["conflicts"]["auto_resolve"]:
        return f"pull request #{pull} conflicts with `{base}`; comment `/nexkit fix` on it"
    if conflict_rounds_used(rounds) >= cfg["conflicts"]["max_rounds"]:
        return f"pull request #{pull} conflicts with `{base}`, and its conflict rounds are used up"
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
            reason = f"the round that pull request #{pull} started next did not run"
            return _step(issue, "waits", reason)
        return _step(issue, "active", f"pull request #{pull} has a round on its way")
    if latest.get("outcome") == "merge_waiting":
        return _merge_step(gh, issue, pull, comment, latest, now)
    if latest.get("outcome") == CI_WAITING:
        return _ci_step(gh, cfg, issue, pull, latest, pull_comments, start)
    if latest.get("status") == FAILURE:
        found = (pull_comments, comments)
        step = _stopped(gh, cfg, issue, latest, record, start, "fix", pull, found)
        return step or _step(
            issue, "waits", latest.get("attention") or f"the last round of #{pull} failed"
        )
    if latest.get("attention"):
        return _step(issue, "waits", latest["attention"])
    conflict = _conflict_wait(gh, cfg, pull, [run for _, run in rounds])
    if conflict:
        return _step(issue, "waits", conflict)
    return _step(issue, "active", f"pull request #{pull} is open")


def _merge_step(gh, issue, pull, comment, run, now):
    """The step for a pull request whose merge waits for required checks: try the merge
    again while its head is the one approved, for at most `LOST`."""
    details = gh.pull(pull)
    if details["head"]["sha"] != run.get("head"):
        return _step(issue, "waits", f"pull request #{pull} changed after its approval")
    since = parse_iso(run.get("waiting_since"))
    if since and now - since > LOST:
        reason = f"the required checks of pull request #{pull} have not passed for hours"
        return _step(issue, "waits", reason)
    base = (details.get("base") or {}).get("ref") or ""
    reason = f"pull request #{pull} waits for its required checks"
    return _step(issue, "merge", reason, round_comment=comment["id"], base=base)


def _replanned(gh, cfg, issue, plan, last, record, start, comments, pull):
    """The step after the plan autopilot asked for when a round of the pull request ran out
    of time: split the issue as the plan suggests, answer its questions, or, when it needs
    nobody, a fix round on the pull request that follows the revised plan."""
    if last.get("status") != SUCCESS:
        return _step(issue, "waits", last.get("attention") or "the last `/nexkit plan` failed")
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
    """The comment that mentions the people in `notify`, or None without any."""
    if not cfg["notify"]:
        return None
    people = " ".join(f"@{login}" for login in cfg["notify"])
    return f"{people} {NEEDED}{reason}."


def _assess(gh, cfg, issue, open_pulls, now=None, where=None):
    number = issue["number"]
    if not trusted(gh, issue):
        login = author(issue) or "an unknown user"
        return _step(issue, "waits", f"opened by @{login}, who cannot write to the repository")
    parts = gh.sub_issues(number)
    if parts and all(part.get("state") == "closed" for part in parts):
        # `close_parent_issues` closes it by itself; without it, a person does.
        reason = "every sub-issue of this parent issue is closed; a person closes it"
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
        if not _lost(record, now):
            return _step(issue, "active", f"waits for the {command} that autopilot started")
        reason = f"the {command} that autopilot started did not run"
        if record.get("retried"):
            return _step(issue, "waits", f"{reason}, again")
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
    blocked = waits_for(gh, issue, where)
    if blocked:
        return _step(issue, "waits", blocked[0], person=blocked[1])
    if last is None or plan is None:
        return _step(issue, "start", "no plan yet", command="plan", **start)
    if last["command"] == "go":
        if last.get("status") == SUCCESS:
            return _step(issue, "waits", "its pull request was closed; a person decides")
        return _stopped(gh, cfg, issue, last, record, start, "go", number, (comments,)) or _step(
            issue, "waits", last.get("attention") or "the last `/nexkit go` failed"
        )
    if last.get("status") != SUCCESS:
        return _step(issue, "waits", last.get("attention") or "the last `/nexkit plan` failed")
    if _after_plan(comments, plan):
        return _step(
            issue, "start", "collaborators commented after the plan", command="plan", **start
        )
    return _planned(gh, cfg, issue, plan, last, record, start, comments)


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
        if not splits:
            # Questions again right after autopilot's answers: a person decides.
            pr = decision.get("pr")
            target = gh.comments(pr or issue["number"])
            return not asked_again(target, gh.comments(issue["number"]) if pr else ())
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
        or (bool(step.get("pull")) and (step["state"] != "waits" or not step["person"]))
        for step in steps
    )
    for step in steps:
        if step["state"] != "start" or step.get("pull"):
            continue
        if limit is not None and busy >= limit:
            reason = f"{busy} issues are in progress (max_parallel)"
            step.update(state="waits", reason=reason, person=False)
            continue
        busy += 1
    return where, steps


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
    started = gh.dispatch(workflow, ref, inputs, run_details=True) or {}
    if step.get("count"):
        _count_round(gh, target)
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


def close(gh, cfg, step, workflow, ref):
    """Close an issue as completed with the finding of its last round, which found no
    change is needed and recommended closing it. Returns lines for the log."""
    number = step["number"]
    url = step.get("run_url")
    round_ = f"[the last round]({url})" if url else "the last round"
    gh.comment(
        number,
        f"NexKit autopilot closes this issue with the answer {round_} recommended: no change "
        f"is needed.\n\n{step['finding']}",
    )
    gh.close_issue(number, "completed")
    lines = [f"Closed #{number}: {step['reason']}."]
    if cfg["close_parent_issues"]:
        # `maintain` imports this module. Closing with the Actions token starts no workflow.
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
    """Say once, on the issue whose closing emptied the scope, that the scope is done. With
    `null`, the scope never empties for good, so this says it of the issue's milestone
    instead, while the other milestones go on."""
    if not where["every"] and where["issues"]:
        return []
    try:
        issue = gh.issue(closed)
    except GitHubError:
        return []
    if issue.get("state") != "closed" or not in_scope(where, issue):
        return []
    if where["every"]:
        milestone = issue.get("milestone") or {}
        number = milestone.get("number")
        if number is None or any(
            (i.get("milestone") or {}).get("number") == number for i in where["issues"]
        ):
            return []
        titles = {m["number"]: m.get("title") for m in where["milestones"]}
        name = f"milestone {milestone.get('title') or titles.get(number) or number}"
    else:
        name = where["name"]
    if any(by_bot(c) and DONE in (c.get("body") or "") for c in gh.comments(closed)):
        return []
    people = "".join(f"@{login} " for login in cfg["notify"])
    text = f"{DONE}\n{people}NexKit autopilot finished {name}: every issue in it is closed."
    if where["milestone"] is not None:
        text += " Close the milestone to let autopilot start on the next one."
    gh.comment(closed, text)
    return [f"Autopilot finished {name}."]


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


def _failed_jobs(gh, run, attempt, failed_jobs=None):
    """Each failed job of an attempt, linked, with its failed steps, the failures its error
    annotations name and the end of its log, so the issue holds what an agent needs. With
    `failed_jobs`, each failed job's name and failed commands are added to it."""
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
            failed_jobs.append({"job": job.get("name"), "commands": failed_commands(log)})
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


def _job_failure(workflow, job, link):
    """The failure of a failed job without an annotation that names one: its job and its
    first failed step."""
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
    return {
        "key": _key("step", name, steps[0]),
        **subject,
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
            "kind": "start",
            "name": workflow,
            "subject": workflow,
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


def _record_flaky(gh, cfg, where, workflow, branch, known, failure, text):
    """Add a record to the open issue of its failure, or open one that links the latest
    closed one."""
    key, title = failure["key"], ci_title(failure, branch)
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
    issue = known["open"][key] = gh.create_issue(title, body, where.get("milestone"), ["bug"])
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
        failures = _attempt_failures(gh, workflow, run, run.get("run_attempt") or 1)
        title = red_title(workflow, failures, branch)
        issue = known["bug"] = gh.create_issue(title, body, where.get("milestone"), ["bug"])
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


def _on_default_branch(gh, run):
    """Whether a run tested this repository's default branch. GitHub matches the branch by
    the name of a run's head branch alone, which a pull request from a fork can share, and
    such a run's job and step names come from the fork."""
    repo = (run.get("head_repository") or {}).get("full_name")
    return repo == gh.repository and run.get("event") not in PULL_REQUEST_EVENTS


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
            runs = [
                run
                for run in gh.workflow_runs(workflow, branch, CI_RUNS_LIMIT)
                if _on_default_branch(gh, run)
            ]
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


# -- pull requests that wait for the default branch -----------------------------------

# The runs of a workflow on the default branch that show whether it passes again.
CI_WAIT_RUNS = 20


def _recorded_failure(gh, issue):
    """What the latest report on a red branch's bug issue records of its failed attempt:
    the run and the failed commands of its failed jobs, or None when no report does."""
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
        commands = {c for job in jobs for c in job.get("commands") or [] if isinstance(c, str)}
        return {"run": run, "commands": commands - {""}}
    return None


def passes_again(gh, workflow, branch, since):
    """Whether `workflow` passes on the default branch `branch` again: its newest run
    there that passed or failed passed, and is run `since` or a newer one. A run that is
    still running does not count, so neither does an older run while `since` runs again."""
    for run in gh.workflow_runs(workflow, branch, CI_WAIT_RUNS):
        if run.get("status") != "completed" or not _on_default_branch(gh, run):
            continue
        if run.get("conclusion") not in (*FAILED, "success"):
            continue
        return run["id"] >= since and run.get("conclusion") == "success"
    return False


def ci_wait(gh, cfg, decision, checks, branch):
    """What a pull request's round waits for instead of an automatic fix round, or None:
    an open bug issue for a workflow of `merge.after_workflows` that still fails on the
    default branch `branch`, when every failing check runs a command that failed in the
    latest failed attempt the issue records. A pull request's checks are NexKit's own,
    not jobs and steps of that workflow, so a check and a step are the same when they run
    the same command, by its first line. Only for a pull request into the default branch
    whose issue autopilot takes, since sweeps end the wait, and never for the bug issue
    or its parts."""
    failing = {
        command_line(check.get("run"))
        for check in checks or []
        if isinstance(check, dict) and not check.get("passed")
    }
    workflows = cfg["merge"]["after_workflows"]
    if not failing or "" in failing or not workflows or not takes(cfg):
        return None
    if decision.get("base") != branch:
        return None
    bugs = {}
    # Newest first, so the first open one found for a workflow is the one reports go to.
    for found in gh.issues_by(BOT_LOGIN):
        if found.get("state") == "open" and ci_workflow(found) in workflows:
            bugs.setdefault(ci_workflow(found), found)
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
        if not recorded or not failing <= recorded["commands"]:
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
        f"Every failing check runs a command that fails in `{workflow}` on `{branch}`, which "
        f"#{bug} is about, so NexKit starts no fix round for it here. Once `{workflow}` "
        f"passes on `{branch}` again, or #{bug} is closed, NexKit autopilot starts the "
        f"automatic fix round, which merges `{branch}` first. Waiting uses no automatic fix "
        "round."
    )


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
        return _step(issue, "waits", f"pull request #{pull} {ci_wait_reason(wait)}", person=False)
    _, state = read_state(pull_comments)
    if (state or {}).get("auto_fixes", 0) >= cfg["fix"]["max_auto_rounds"]:
        reason = f"{over}, but pull request #{pull} has no automatic fix rounds left"
        return _step(issue, "waits", reason)
    reason = f"{over}, so pull request #{pull} gets the automatic fix round it waited for"
    fields = {**start, "target": pull, "auto": "true", "count": True}
    return _step(issue, "start", reason, command="fix", **fields)


def _count_round(gh, pull):
    """Count the automatic fix round a pull request waited for, as `report` counts one it
    starts, and end the wait in its state comment."""
    comment_id, state = read_state(gh.comments(pull))
    if not state:
        return
    state["auto_fixes"] = (state.get("auto_fixes") or 0) + 1
    state.pop("ci_wait", None)
    save_state(gh, pull, comment_id, state)


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
    if closed:
        lines += done(gh, cfg, where, closed)
    return lines or [f"Autopilot: nothing to start in {where['name']}."]
