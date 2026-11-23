"""The first state document of a work item that NexKit 6 left (docs/engine.md#versions).

NexKit 6 names no state: it reads where an item is from its comments, as `autopilot.assess`
does. The import reads the same markers, the open pull request, paused runs and waits, and
gives the state document the engine (`engine/machine.py`) would hold for the same item. It
reads only the markers of NexKit's App, through `state.py`, so a person's text sets none of
it, and it changes nothing on GitHub.
"""

from __future__ import annotations

import re

from . import autopilot, item_state, protocol
from .autopilot import CHAINED, CI_WAITING, HELD, PULL_CLOSED, UNTRUSTED_MERGE, UNTRUSTED_ROUND
from .github import TRUSTED_ASSOCIATIONS
from .protocol.size import MOST, fit
from .schema import filled
from .state import (
    CONTINUED,
    FAILURE,
    PAUSED,
    RUNNING,
    SUCCESS,
    answers_record,
    by_bot,
    conflict_rounds_used,
    depends_on,
    latest_plan,
    plan_data,
    planned_parts,
    questions,
    read_state,
    run_action,
    run_comments,
)

# The id of the action that a NexKit 6 run on its way stands for. The engine's own ids
# start at `a<revision>.1` with the revision after the import's, so none is the same.
RUN_ID = "a0.1"
# The state of the item while a command, or a round with that action, runs.
OF_COMMAND = {"plan": "planning", "go": "working", "fix": "fixing", "review": "checking"}
OF_ACTION = {"implement": "working", "fix": "fixing", "review": "checking"}
# What autopilot's record keeps that the state's `asked` keeps.
ASKED = {
    "split_asked": "split",
    "titles_asked": "titles",
    "parts_asked": "parts",
    "fewer_asked": "fewer",
}
# The most characters of a NexKit 6 reason that the state's attention keeps: the comments
# that NexKit 6 wrote keep it whole.
REASON = 500
# The plan comment's acceptance criteria, as `publish.render_plan` lists them.
CRITERIA = re.compile(r"^### Acceptance criteria\n\n((?:- \[ \] .*\n?)+)", re.M)
# Why a person is needed, as the engine says it for each reason.
SAYS = {
    "approval": "The plan waits for its approval.",
    "question": "The plan has questions for a person.",
    "handMadeChange": "The change needs paths that only a person changes.",
    "unverifiableCriterion": "A person confirms the criteria the review could not verify.",
    "personChecklistItem": "A person decides a checklist item.",
    "accept": "The change passed, and a person decides its accept.",
    "acceptRefused": "The accept was refused.",
    "roundsUsedUp": "The checks or the review still fail, and no fix round is left.",
    "splitIncomplete": "No part of the split was completed.",
    "blocked": "The agent was blocked.",
    "failed": "The last run failed.",
    "reopened": "The pull request was closed and opened again.",
}


def empty(item):
    """The state document of an item before anything happened to it."""
    return {
        "schemaVersion": 1,
        "revision": 1,
        "item": item,
        "itemKind": "change",
        "state": "new",
        "change": None,
        "attention": None,
        "hold": False,
        "waits": [],
        "pending": [],
        "actors": {"plan": "engine", "change": "engine", "review": "engine"},
        "plan": None,
        "rounds": {
            "started": 0,
            "counted": 0,
            "conflicts": 0,
            "signatures": [],
            "findings": [],
            "criteria": [],
            "checklist": [],
        },
        "used": {"tokens": {"input": 0, "cached": 0, "output": 0}, "since": None},
        "asked": [],
        "parts": [],
        "policies": {},
    }


def imported(issue, comments, pull=None, pull_comments=(), parts=(), blockers=(), held=False):
    """The first state document of `issue`, a GitHub issue, or a person's pull request for a
    review-only item, from its `comments`; its NexKit pull request `pull` and that pull
    request's `pull_comments`; its sub-issues `parts`; the open issues that block it,
    `blockers`; and whether a label holds it, `held`."""
    doc = empty(str(issue["number"]))
    doc["hold"] = bool(held)
    if "pull_request" in issue:
        pull, pull_comments = pull or issue, comments
        doc["itemKind"], doc["actors"]["change"] = "reviewOnly", "person"
    if pull:
        doc["change"] = {"id": str(pull["number"]), "version": pull["head"]["sha"]}
    item = Item(doc, issue, list(comments), pull, list(pull_comments))
    item.place(list(parts), list(blockers))
    if fit(doc) is None:
        raise item_state.StateError(f"The state of #{issue['number']} does not fit its limit.")
    found = protocol.check(doc, "state.schema.json", "state")
    if found:
        raise item_state.StateError("; ".join(found))
    return doc


class Item:
    """One item's NexKit 6 comments, read once, and the document that the import fills."""

    def __init__(self, doc, issue, comments, pull, pull_comments):
        self.doc, self.issue, self.pull = doc, issue, pull
        review_only = doc["itemKind"] == "reviewOnly"
        self.runs = [] if review_only else [r for r in run_comments(comments) if "command" in r[1]]
        self.rows = [run for _, run in run_comments(pull_comments) if "round" in run]
        self.plan = None if review_only else latest_plan(comments)
        self.data = plan_data(self.plan) if self.plan else None
        _, self.pr_state = read_state(pull_comments) if pull else (None, None)
        self.pr_state = self.pr_state or {}
        _, self.record = autopilot.find_record(comments)
        # A command that autopilot started and that has posted no run since (`assess`).
        current = autopilot.basis(comments, pull_comments)
        self.on_its_way = bool(self.record.get("command")) and self.record.get("basis") == current
        if self.plan:
            go = [c for c, run in self.runs if run["command"] == "go"]
            approved = bool(pull) or any(c["id"] > self.plan["id"] for c in go)
            doc["plan"] = _plan(self.plan, self.data, approved)
        doc["asked"] = _asked(self.record, [*comments, *pull_comments])
        doc["used"]["tokens"] = _tokens([run for _, run in self.runs] + self.rows)
        if pull and not review_only:
            self.take_rounds()

    # Where the item is.

    def place(self, parts, blockers):
        """Name the item's state, in the order `autopilot._assess` reads an item."""
        last = self.runs[-1][1] if self.runs else None
        # A run of the issue that its pull request carries on is read there.
        handed_on = bool(self.pull and last and autopilot.in_pull_request(last))
        active = bool(last) and last.get("status") in autopilot.ACTIVE and not handed_on
        if self.issue.get("state") == "closed":
            self.final(last)
        elif parts:
            self.split(parts)
        elif self.doc["itemKind"] == "reviewOnly":
            self.review_only()
        elif self.on_its_way:
            self.running(OF_COMMAND.get(self.record["command"], "planning"))
        elif active and last["status"] == PAUSED:
            self.paused(OF_COMMAND.get(last["command"], "planning"), last)
        elif active:
            self.running(OF_COMMAND.get(last["command"], "planning"))
        elif self.pull:
            self.round()
        elif last is None or self.plan is None:
            self.new(blockers)
        elif last["command"] == "go":
            self.after_go(last)
        else:
            self.after_plan(last)

    def final(self, last):
        """A closed issue: completed with its change, completed with a finding that no
        change is needed, or closed as not planned."""
        no_change = filled((self.data or {}).get("no_change")) or (last or {}).get("outcome") in (
            "no_change",
            "already_on_base",
        )
        if self.issue.get("state_reason") != "completed":
            self.doc["state"] = "dropped"
        else:
            self.doc["state"] = "noChange" if no_change else "accepted"

    def split(self, parts):
        doc = self.doc
        doc["parts"] = [{"item": str(part["number"]), "state": _ended(part)} for part in parts]
        if doc["plan"]:
            doc["plan"]["approved"] = True
        if any(part["state"] is None for part in doc["parts"]):
            doc["state"] = "split"
            doc["waits"].append(_wait("parts"))
        elif any(part["state"] == "accepted" for part in doc["parts"]):
            doc["state"] = "accepted"
        else:
            self.need("split", "splitIncomplete")

    def review_only(self):
        latest = _latest(self.rows)
        if latest is None:
            self.doc["waits"].append(_wait("start"))
        elif latest.get("status") == RUNNING:
            self.running("checking")
        elif latest.get("status") == PAUSED:
            self.paused("reviewing", latest)
        else:
            self.doc["state"] = "ready"
            self.doc["waits"].append(_wait("start"))

    def new(self, blockers):
        self.doc["waits"].append(_wait("start"))
        for blocker in blockers:
            number = str(blocker["number"])
            self.doc["waits"].append({**_wait("blocker"), "id": f"blocker-{number}", "on": number})

    def after_go(self, run):
        outcome = run.get("outcome")
        if run.get("status") == SUCCESS and outcome in ("no_change", "already_on_base"):
            self.doc["state"] = "noChange"
        elif run.get("status") == SUCCESS:
            # Its pull request was closed: a person asks for another, as after a plan.
            self.need("awaitingApproval", "approval", run)
        elif outcome == "agent_blocked" and run.get("questions"):
            self.need("awaitingApproval", "question", run)
        elif outcome in ("agent_blocked", UNTRUSTED_ROUND):
            self.need("failed", "blocked", run)
        else:
            self.need("failed", "failed", run)

    def after_plan(self, run):
        plan = self.doc["plan"]
        if run.get("status") != SUCCESS:
            self.need("failed", "failed", run)
        elif plan["noChange"]:
            self.doc["state"] = "noChange"
        elif plan["questions"]:
            self.need("awaitingApproval", "question", run)
        elif plan["protectedPaths"]:
            self.need("awaitingApproval", "handMadeChange", run)
        else:
            self.need("awaitingApproval", "approval", run)

    def round(self):
        """Place an item whose NexKit pull request is open by its latest round, as
        `autopilot._pull_step` reads it."""
        latest = _latest(self.rows)
        if latest is None:
            # The round that opened it has not reported yet.
            return self.running("checking")
        status, outcome = latest.get("status"), latest.get("outcome")
        name = OF_ACTION.get(run_action(latest), "checking")
        if status == RUNNING:
            return self.running(name)
        if status == PAUSED:
            return self.paused("reviewing" if latest.get("verdict") == "paused" else name, latest)
        if status == CONTINUED or outcome in CHAINED:
            return self.running(_next_state(latest, name))
        if outcome == HELD:
            return self.held_back(latest)
        if outcome == "merge_waiting":
            self.doc["state"] = "accepting"
            self.doc["waits"].append(_wait("requiredChecks"))
            return None
        if outcome == CI_WAITING:
            # The fix round waits until the base branch passes again, or its bug closes.
            wait = latest.get("ci_wait") or self.pr_state.get("ci_wait") or {}
            self.doc["state"] = "fixing"
            on = str(wait["bug"]) if wait.get("bug") else None
            self.doc["waits"].append({**_wait("baseFailure"), "on": on})
            return None
        if outcome == "ready":
            return self.ready(latest)
        named = {
            "needs_human": "roundsUsedUp",
            "recheck_failed": "roundsUsedUp",
            "merge_refused": "acceptRefused",
            "conflicting": "acceptRefused",
            PULL_CLOSED: "reopened",
            UNTRUSTED_MERGE: "blocked",
        }
        if outcome in named:
            return self.need("ready", named[outcome], latest)
        if outcome == "agent_blocked" and latest.get("questions"):
            return self.need("awaitingApproval", "question", latest)
        if status == FAILURE:
            blocked = outcome in ("agent_blocked", UNTRUSTED_ROUND)
            return self.need("failed", "blocked" if blocked else "failed", latest)
        return self.ready(latest)

    def ready(self, row):
        rounds = self.doc["rounds"]
        if "unverifiable" in rounds["criteria"]:
            reason = "unverifiableCriterion"
        elif row.get("decide"):
            reason = "personChecklistItem"
            if "not_met" not in rounds["checklist"]:
                rounds["checklist"].append("not_met")
        else:
            reason = "accept"
        self.need("ready", reason, row)

    def held_back(self, row):
        """A round that a label held back from its merge, its fix round or its conflict
        round: the item is where that step starts, and starts it once the label is gone."""
        name = "accepting" if row.get("held_back") == "merge" else "fixing"
        if self.doc["hold"]:
            self.doc["state"] = name
        else:
            self.running(name)

    # What the state holds beside its name.

    def running(self, name):
        """A command or round on its way: the action the engine waits for."""
        version = (self.doc["change"] or {}).get("version")
        kind = {"checking": "runChecks", "accepting": "accept"}.get(name, "runAgent")
        bound = kind != "runAgent" or name in ("fixing", "reviewing")
        self.doc["state"] = name
        pending = {"id": RUN_ID, "kind": kind, "changeVersion": version if bound else None}
        self.doc["pending"].append(pending)

    def paused(self, name, run):
        """A run paused at a usage limit, or to run again after a transient stop: a wait with
        its time, or, when NexKit 6 does not resume it, a failure for a person."""
        resume = run.get("resume")
        if not isinstance(resume, dict):
            return self.need("failed", "failed", run)
        retry, at = bool(resume.get("retry")), resume.get("at")
        wait = _wait("time" if retry else "usageLimit")
        wait.update(id="retry" if retry else wait["id"], until=at if isinstance(at, str) else None)
        self.doc["state"] = name
        self.doc["waits"].append(wait)
        return None

    def need(self, name, reason, run=None):
        self.doc["state"] = name
        said = str((run or {}).get("attention") or "").strip()
        text = said[:REASON] + ("..." if len(said) > REASON else "") if said else SAYS[reason]
        self.doc["attention"] = {"reason": reason, "text": text}

    def take_rounds(self):
        """The rounds of the pull request as the engine counts them, and the last checks
        and review that NexKit 6's state comment recorded."""
        rounds, rows, record = self.doc["rounds"], self.rows, self.pr_state
        started = len([row for row in rows if run_action(row) in ("implement", "fix")])
        # At least the round that opened the pull request, which an old marker may not name.
        rounds["started"] = max(started, 1)
        rounds["counted"] = _count(record.get("auto_fixes"))
        rounds["conflicts"] = conflict_rounds_used(rows)
        feedback = record.get("feedback") or {}
        rounds["signatures"] = [
            str(check.get("signature") or check.get("name"))
            for check in feedback.get("checks") or []
            if isinstance(check, dict) and not check.get("passed") and not check.get("skipped")
        ][: MOST["signatures"]]
        review = feedback.get("review") or {}
        rounds["criteria"] = [
            item["status"]
            for item in review.get("criteria") or []
            if item.get("status") in ("met", "unmet", "unverifiable")
        ]
        rounds["checklist"] = [
            item["answer"]
            for item in review.get("checklist") or []
            if item.get("answer") in ("met", "not_met", "not_applicable")
        ]
        findings = (record.get("last_review") or {}).get("findings") or []
        findings = [item for item in findings if item.get("severity") in ("blocking", "suggestion")]
        findings.sort(key=lambda item: item["severity"] != "blocking")
        rounds["findings"] = [
            {"severity": item["severity"], "text": str(item.get("body") or "")[:300]}
            for item in findings[: MOST["findings"]]
        ]


def _wait(kind):
    return {"id": kind, "for": kind, "until": None, "on": None}


def _latest(rows):
    """The latest round that says something about the pull request: one that found nothing
    to merge does not (`autopilot._meaningful`)."""
    for row in reversed(rows):
        if row.get("outcome") != "up_to_date":
            return row
    return rows[-1] if rows else None


def _next_state(row, name):
    """The state of the run that a round started next: a fix round, its own command again,
    or a review of the head that a branch update or a moved head gave."""
    if row.get("outcome") in ("auto_fix", "conflict_fix"):
        return "fixing"
    command = (row.get("next") or {}).get("command")
    if command in OF_COMMAND:
        return OF_COMMAND[command]
    if row.get("outcome") in ("branch_updated", "head_moved", "superseded"):
        return "checking"
    return name


def _ended(part):
    if part.get("state") != "closed":
        return None
    return "accepted" if part.get("state_reason") == "completed" else "dropped"


def _plan(comment, data, approved):
    """The plan as the engine keeps it, from the plan comment and its record."""
    data = data or {}
    split = data.get("split") or []
    criteria = CRITERIA.search(comment.get("body") or "")
    return {
        "text": {"name": "plan", "ref": str(comment["id"])},
        "questions": [_question(item) for item in questions(data.get("questions"))],
        "criteria": len(criteria.group(1).splitlines()) if criteria else 0,
        "tooLarge": bool(data.get("too_large")),
        "parts": planned_parts(comment, data),
        "noChange": bool(filled(data.get("no_change"))),
        "protectedPaths": [str(path) for path in data.get("protected_paths") or []][
            : MOST["protectedPaths"]
        ],
        "approved": approved,
        "split": [
            {
                "key": number,
                "title": str(part.get("title") or ""),
                "needs": depends_on(part, number),
            }
            for number, part in enumerate(split, 1)
            if isinstance(part, dict)
        ],
    }


def _question(item):
    """A question of a plan's record, which names options by their texts, as the state
    keeps it, naming them by their numbers from 1."""
    options = item["options"][: MOST["options"]]

    def number(text):
        return options.index(text) + 1 if text in options else 0

    found = {"question": item["question"], "options": options}
    found["recommended"] = number(item["recommended"])
    if item.get("closing"):
        found["closes"] = number(item["closing"])
    return found


def _asked(record, comments):
    """What autopilot asked for that the engine must not ask for again: its record's split
    and parts requests, and answers it gave with no comment by a person since, which it
    gives once in a row (`autopilot.asked_again`)."""
    asked = [name for key, name in ASKED.items() if record.get(key)]
    answered = [c for c in comments if answers_record(c) is not None]
    if answered and not any(_by_person(c) and c["id"] > answered[-1]["id"] for c in comments):
        asked.append("answers")
    return asked


def _by_person(comment):
    return comment.get("author_association") in TRUSTED_ASSOCIATIONS and not by_bot(comment)


def _tokens(runs):
    total = {"input": 0, "cached": 0, "output": 0}
    for run in runs:
        tokens = run.get("tokens")
        if isinstance(tokens, dict):
            for key in total:
                total[key] += _count(tokens.get(key))
    return total


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def import_item(gh, cfg, number, pull=None):
    """Read work item `number` on GitHub, with its NexKit pull request `pull` if one is open,
    and give its first state document."""
    issue = gh.issue(number)
    comments = gh.comments(number)
    if "pull_request" in issue:
        return imported(issue, comments, gh.pull(number), held=False)
    details = gh.pull(pull) if pull else None
    pull_comments = gh.comments(pull) if pull else []
    blockers = [found for found in gh.blocked_by(number) if found.get("state") == "open"]
    return imported(
        issue,
        comments,
        details,
        pull_comments,
        gh.sub_issues(number),
        blockers,
        bool(autopilot.hold_label(cfg, issue)),
    )
