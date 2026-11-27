"""The first state document of a work item that NexKit 6 left (docs/engine.md#versions).

NexKit 6 names no state: it reads where an item is from its comments, as `autopilot.assess`
does. The import reads the same markers, the open pull request, paused runs and waits, and
gives the state document the engine (`engine/machine.py`) would hold for the same item, from
which the engine's next event carries it on. It reads only the markers of NexKit's App, each
only where it ends the comment, so a person's or an agent's text sets none of it, and it
changes nothing on GitHub.

A `/nexkit go` that stopped as blocked is placed where the engine puts the round's result, under
the GitHub host's policies (`Item.after_go`).

Each place the import can put an item is one of KINDS, and a case in
`tests/conformance/imports/` imports each.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime

from . import autopilot, item_state, protocol
from .autopilot import CHAINED, CI_WAITING, HELD, PULL_CLOSED, UNTRUSTED_MERGE, UNTRUSTED_ROUND
from .checks import failing_signatures
from .engine.machine import ACCESS, INPUTS
from .engine.textutil import clip as clip_text
from .github import TRUSTED_ASSOCIATIONS
from .protocol.size import HISTORY, LONGEST, MOST, clip, failure_key, fit, signature
from .schema import filled
from .state import (
    COMMIT,
    FAILURE,
    PAUSED,
    SUCCESS,
    answers_record,
    by_bot,
    conflict_rounds_used,
    counted_fix,
    depends_on,
    latest_plan,
    plan_data,
    planned_parts,
    questions,
    read_state,
    run_action,
    run_comments,
)
from .usage import PAUSE_WINDOW, iso, parse_iso

# The most blockers an imported item waits for; the state keeps at most 64 waits.
BLOCKERS = 32
# The revision of an imported document, and the id of the action that a NexKit 6 run on its
# way stands for, as the engine numbers the actions of the answer that made revision 1. The
# GitHub host reports that run's end as this action's result, or its loss.
REVISION = 1
RUN_ID = "a1.1"
# The state of the item while a command, or a round with that action, runs.
OF_COMMAND = {"plan": "planning", "go": "working", "fix": "fixing", "review": "checking"}
OF_ACTION = {"implement": "working", "fix": "fixing", "review": "checking"}
# The states in which autopilot's answers still wait for the run they started.
ANSWERING = ("planning", "working", "fixing")
STAGES = {"planning": "plan", "working": "implement", "fixing": "fix", "reviewing": "review"}
# What autopilot's record keeps that the state's `asked` keeps.
ASKED = {
    "split_asked": "split",
    "titles_asked": "titles",
    "parts_asked": "parts",
    "fewer_asked": "fewer",
}
# The plan comment's acceptance criteria, as `publish.render_plan` lists them.
CRITERIA = re.compile(r"^### Acceptance criteria\n\n((?:- \[ \] .*\n?)+)", re.M)
# Why a person is needed, as the engine says it, for a reason NexKit 6's marker leaves out.
SAYS = {
    "approval": "The plan waits for its approval.",
    "question": "The plan has questions for a person.",
    "handMadeChange": "The change needs paths that only a person changes.",
    "unverifiableCriterion": "A person confirms the criteria the review could not verify.",
    "personChecklistItem": "A person decides a checklist item.",
    "accept": "The change passed, and a person decides its accept.",
    "acceptRefused": "The accept was refused.",
    "roundsUsedUp": "The checks or the review still fail, and no fix round is left.",
    "splitIncomplete": (
        "Every part of the split ended. A person closes the item, or opens parts for what is "
        "left: NexKit closes a parent only with `close_parent_issues` on and a part completed."
    ),
    "blocked": "The agent was blocked.",
    "failed": "The last run failed.",
    "roundFailed": "The last round failed on an error.",
    "untrusted": (
        "An edit by someone without write access stopped it, and a writer vouches for it again."
    ),
    "reopened": "The pull request was closed and opened again.",
    "outage": "A run ended without a report, and NexKit started it again as often as it does.",
    "paused": "The agent hit its usage limit, and NexKit does not resume this run.",
    "stale": "The run paused at the usage limit longer ago than NexKit resumes runs.",
    "overdue": "The run was to run again after a transient error, and no run started it.",
}
# Where the import puts an item, by what NexKit 6 shows of it.
KINDS = {
    "closed": "A closed issue: accepted, no change needed, or dropped.",
    "parts open": "A parent whose parts are not all closed waits for them.",
    "parts ended": "A parent whose parts ended, one completed, ends with `close_parent_issues`.",
    "parts ended for a person": "A parent whose parts ended waits for a person.",
    "review only": "A person's pull request with no round yet waits for a review request.",
    "reviewed": "A person's pull request that was reviewed waits for the next request.",
    "started": "A command autopilot started, or a run on its way, is the action waited for.",
    "paused": "A run paused at a usage limit waits for its reset.",
    "retrying": "A run that stopped on a transient error waits to run again.",
    "pause for a person": "A pause that NexKit 6 does not resume fails for a person.",
    "lost": "An automatic run that ended without a report waits to start again.",
    "lost for a person": "A lost run that NexKit 6 does not start again fails for a person.",
    "new": "An item with no plan waits to start.",
    "planned": "A plan waits for its approval, its answers or a hand-made change.",
    "no change": "A plan or a round found that no change is needed.",
    "go again": "A `/nexkit go` whose pull request was closed waits for a person to approve again.",
    "asks": (
        "A round that asks questions, or whose change needs paths only a person changes, waits "
        "for a person."
    ),
    "answered": (
        "A round or plan that the engine's answers to a blocked `/nexkit go` start waits for "
        "the sweep to start it."
    ),
    "failed": "A run or a round that failed waits for a person.",
    "no round yet": "A pull request whose first round has not reported is in its checks.",
    "next round": "A round that started the next one by itself: that one is on its way.",
    "held": "A round that a label held back waits for the hold to end.",
    "held, label gone": "A round that a label held back, whose label is gone, waits to start.",
    "required checks": "A merge waits for required checks.",
    "base failure": "A fix round waits until the base branch's failure ends.",
    "ready": "A change waits for a person: its accept, criteria or a checklist item.",
    "needs a person": "A round left the change for a person: rounds used up, a refused merge.",
}


def failing(recorded):
    """The engine's `rounds.failing` for checks as the state comment recorded them: each
    failing check by the digest of its name and signature (`checks.failing_signatures`)."""
    return [
        failure_key(name, found or name) for name, found in failing_signatures(recorded).items()
    ][: MOST["failing"]]


def empty(item):
    """The state document of an item before anything happened to it."""
    return {
        "schemaVersion": 1,
        "revision": REVISION,
        "item": item,
        "itemKind": "change",
        "state": "new",
        "change": None,
        "observed": None,
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
        "used": {
            "tokens": {"input": 0, "cached": 0, "output": 0},
            "seconds": 0,
            "since": None,
        },
        "asked": [],
        "parts": [],
        "policies": {},
        "recentEvents": [],
    }


def situation(
    issue,
    comments,
    cfg,
    now,
    pull=None,
    pull_comments=(),
    parts=(),
    blockers=(),
    held=False,
    depth=0,
    waves=(),
    taken=None,
):
    """(kind, document): where the import puts `issue`, a GitHub issue, or a person's pull
    request for a review-only item, and its first state document. It reads the item's
    `comments`; its NexKit pull request `pull` and that pull request's `pull_comments`; its
    sub-issues `parts`; the open issues that block it, `blockers`; whether a label holds it,
    `held`; how many levels of parts the issue is below the issue first split, `depth`, as
    `autopilot.depth` gives it; the numbers of the open issues of a lower wave that it waits for,
    `waves`; the configuration `cfg`; the time `now`, and whether autopilot takes the issue,
    `taken`, where None means `autopilot.takes(cfg)`."""
    doc = empty(str(issue["number"]))
    if issue.get("title") is not None:
        doc["title"] = str(issue["title"])[: LONGEST["title"]]
    if depth > 0:
        # Only a part has a depth, as `created` gives it: the engine splits no item at the
        # deepest level.
        doc["depth"] = depth
    doc["hold"] = bool(held)
    if "pull_request" in issue:
        pull, pull_comments = pull or issue, comments
        doc["itemKind"], doc["actors"]["change"] = "reviewOnly", "person"
    if pull:
        doc["change"] = {"id": str(pull["number"]), "version": pull["head"]["sha"]}
        doc["observed"] = observed(pull)
    taken = autopilot.takes(cfg) if taken is None else taken
    item = Item(doc, issue, list(comments), pull, list(pull_comments), cfg, now, taken)
    item.place(list(parts), list(blockers), list(waves))
    if doc["state"] in ANSWERING and "answers" not in doc["asked"] and item.answered():
        doc["asked"].append("answers")
    if fit(doc) is None:
        raise item_state.StateError(f"The state of #{issue['number']} does not fit its limit.")
    found = protocol.check(doc, "state.schema.json", "state")
    if found:
        raise item_state.StateError("; ".join(found))
    return item.kind, doc


def observed(pull):
    """The observation of a pull request, as the engine keeps the latest one: its head commit,
    whether it conflicts, was merged or changes no file. None while GitHub has not said whether
    it conflicts, or no count of its files was read: an observation holds only what is known
    (docs/engine-hosts.md#hosts)."""
    mergeable, files = pull.get("mergeable"), pull.get("changed_files")
    if not isinstance(mergeable, bool) or not isinstance(files, int) or isinstance(files, bool):
        return None
    return {
        "changeId": str(pull["number"]),
        "version": pull["head"]["sha"],
        "conflicts": not mergeable,
        "merged": bool(pull.get("merged")),
        "empty": files == 0,
    }


def imported(*args, **fields):
    """The first state document of an item, from what `situation` reads."""
    return situation(*args, **fields)[1]


class Item:
    """One item's NexKit 6 comments, read once, and the document that the import fills."""

    def __init__(self, doc, issue, comments, pull, pull_comments, cfg, now, taken):
        self.doc, self.issue, self.pull, self.cfg, self.now = doc, issue, pull, cfg, now
        self.comments, self.taken = comments, taken
        self.kind = None
        review_only = doc["itemKind"] == "reviewOnly"
        self.runs = [] if review_only else [r for r in run_comments(comments) if "command" in r[1]]
        self.rows = [(c, run) for c, run in run_comments(pull_comments) if "round" in run]
        self.plan = None if review_only else latest_plan(comments)
        self.data = plan_data(self.plan) if self.plan else None
        self.record = {} if review_only else autopilot.find_record(comments)[1]
        # NexKit 6's pull request state comment, whose marker counts only where it ends it.
        found = read_state(pull_comments)[1] if pull else None
        self.pr_state = found if isinstance(found, dict) else {}
        # A command that autopilot started and that has posted no run since (`assess`).
        current = autopilot.basis(comments, pull_comments)
        self.on_its_way = bool(self.record.get("command")) and self.record.get("basis") == current
        if self.plan:
            # Approved once a `/nexkit go` or a round came after it.
            after = [c for c, run in self.runs if run["command"] == "go"]
            after += [c for c, _ in self.rows]
            approved = any(c["id"] > self.plan["id"] for c in after)
            doc["plan"] = plan_of(self.plan, self.data, approved)
        doc["asked"] = [name for key, name in ASKED.items() if self.record.get(key)]
        if (
            pull
            and not review_only
            and self.record.get("split_asked")
            and self.record.get("command") == "plan"
            and self.runs
            and self.runs[-1][1]["command"] == "plan"
        ):
            # The plan that autopilot asked for after the pull request's round ran out of time
            # (`autopilot._assess`): its split withdraws the pull request.
            doc["asked"].append("supersede")
        self.all = comments if review_only else [*comments, *pull_comments]
        doc["used"]["tokens"] = _tokens([run for _, run in self.runs + self.rows])
        if pull and not review_only:
            self.take_rounds()

    def answered(self):
        """Whether autopilot's answers still wait for the run they started, as
        `autopilot.asked_again` reads it for the run on its way: no person commented on the
        issue or its pull request since, and every run since changed nothing about the item,
        or is still running."""
        answers = [c for c in self.all if answers_record(c) is not None]
        if not answers:
            return False
        last = max(answers, key=lambda c: c["id"])
        if any(_by_person(c) and c["id"] > last["id"] for c in self.all):
            return False
        replan = bool(answers_record(last).get("replan"))
        return all(
            run.get("status") in autopilot.ACTIVE or autopilot._unchanged(run, replan)
            for c, run in run_comments(self.all)
            if c["id"] > last["id"]
        )

    # Where the item is.

    def place(self, parts, blockers, waves=()):
        """Name the item's state, in the order `autopilot._assess` reads an item. It waits for the
        open `blockers` and for the issues of a lower wave in `waves`."""
        last_comment, last = self.runs[-1] if self.runs else (None, None)
        latest_comment, latest = _latest(self.rows)
        # A run of the issue that its pull request carries on is read there, unless it is
        # newer than the pull request's latest round, as a re-plan is.
        newer = latest_comment is None or (
            last_comment and last_comment["id"] > latest_comment["id"]
        )
        handed_on = bool(self.pull and last and (autopilot.in_pull_request(last) or not newer))
        if self.issue.get("state") == "closed":
            return self.final(last)
        if parts:
            return self.split(parts)
        if self.doc["itemKind"] == "reviewOnly":
            return self.review_only(latest_comment, latest)
        if not self.pull:
            # Every item without a pull request waits for the open issues that block it and for
            # those of a lower wave that hold it back, as one wait each: the first BLOCKERS of
            # them, so that the waits stay within the schema's `maxItems`.
            waited = [blocker["number"] for blocker in blockers] + list(waves)
            for number in list(dict.fromkeys(waited))[:BLOCKERS]:
                self.wait("blocker", on=str(number))
        if self.on_its_way:
            return self.running("started", OF_COMMAND.get(self.record["command"], "planning"))
        if last and not handed_on and last.get("status") in autopilot.ACTIVE:
            return self.active(last_comment, last, OF_COMMAND.get(last["command"], "planning"))
        if last and not handed_on and last.get("outcome") == "lost":
            return self.lost(last, last["command"], self.issue["number"])
        if self.pull and (handed_on or last is None or last["command"] != "plan"):
            return self.round(latest_comment, latest)
        if last is None or self.plan is None:
            self.wait("start")
            return self.be("new", "new")
        if last["command"] == "go":
            return self.after_go(last_comment, last)
        return self.after_plan(last)

    def final(self, last):
        """A closed issue: completed with its change, completed with a finding that no
        change is needed, or closed as not planned. An issue closed before GitHub kept a
        reason has none, and was completed."""
        no_change = filled((self.data or {}).get("no_change")) or (last or {}).get("outcome") in (
            "no_change",
            "already_on_base",
        )
        reason = self.issue.get("state_reason")
        if reason not in (None, "completed"):
            return self.be("closed", "dropped")
        return self.be("closed", "noChange" if no_change else "accepted")

    def split(self, parts):
        doc = self.doc
        doc["parts"] = [{"item": str(part["number"]), "state": _ended(part)} for part in parts]
        doc["parts"] = doc["parts"][: MOST["split"]]
        if doc["plan"]:
            doc["plan"]["approved"] = True
        if any(part["state"] is None for part in doc["parts"]):
            self.wait("parts")
            return self.be("parts open", "split")
        completed = any(part["state"] == "accepted" for part in doc["parts"])
        if completed and self.cfg["close_parent_issues"]:
            return self.be("parts ended", "accepted")
        return self.be("parts ended for a person", "split", "splitIncomplete")

    def review_only(self, comment, latest):
        if latest is None:
            self.wait("start")
            return self.be("review only", "new")
        if latest.get("status") in autopilot.ACTIVE:
            return self.active(comment, latest, "checking")
        self.wait("start")
        return self.be("reviewed", "ready")

    def after_go(self, comment, run):
        """Place an item whose latest run is a `/nexkit go` that did not end in a pull request.
        A round that stopped as blocked or failed on an error records its result (`seen`), as
        the import decided on it."""
        outcome = run.get("outcome")
        if run.get("status") == SUCCESS and outcome in ("no_change", "already_on_base"):
            return self.be("no change", "noChange")
        if run.get("status") == SUCCESS:
            # Its pull request was closed: a person asks for another, as after a plan.
            ref = (self.doc["plan"] or {}).get("text", {}).get("ref")
            return self.be("go again", "awaitingApproval", "approval", run, ref=ref)
        if outcome == "agent_blocked":
            return self.after_blocked(comment, run)
        if outcome == autopilot.UNTRUSTED_ROUND:
            # An untrusted edit's stop is `untrusted`: a comment does not plan it again, as in
            # NexKit 6, and a writer's vouch after it starts the next command.
            return self.be("failed", "failed", "untrusted", run)
        if run.get("status") == FAILURE and outcome != "issue_closed" and not run.get("timed_out"):
            # A round that failed on an error: NexKit 6 plans again after a comment.
            self.seen(_round_result(self.issue["number"], comment, run), run)
            return self.be("failed", "failed", "roundFailed", run)
        # A time-out and an issue that closed with its round are `failed`, so that a comment
        # does not plan them again under `afterFailure`: NexKit 6 waits for a person.
        return self.be("failed", "failed", "failed", run)

    def seen(self, event, run):
        """Record the round's result `event` as an event the state has seen, when the run's
        link names the workflow run it came from: the import decides on that result itself, so a
        later decision on this state never sends it again to a run the engine started after it.
        The id is the one that `report` and the sweep send (`host._run_event_id`)."""
        from .route import RUN_ID as NAMES

        if NAMES.search(run.get("url") or "") and event["id"] not in self.doc["recentEvents"]:
            self.doc["recentEvents"].append(event["id"])

    def after_blocked(self, comment, run):
        """A `/nexkit go` that stopped as blocked, in the state where the engine puts the
        round's result under the GitHub host's policies. Autopilot's answers apply only to an
        issue it takes. The questions and the attention name the run's comment, as a person's
        answer on GitHub does."""
        from . import host  # host imports this module

        doc = _working(copy.deepcopy(self.doc), autopilot.asked_again(self.comments))
        policies = host.policies(self.cfg)
        if not self.taken:
            policies["planApproval"] = "person"
        event = _round_result(self.issue["number"], comment, run)
        self.seen(event, run)
        try:
            found = host.step(doc, event, policies)["state"]
        except host.Refused:
            return self.be("failed", "failed", "failed", run)
        name = found["state"]
        if name == "awaitingApproval":
            ref = str(comment["id"])
            items = [
                {key: value for key, value in item.items() if key != "closes" or value}
                for item in found["questions"]["items"]
            ]
            self.doc["questions"] = {**found["questions"], "ref": ref, "items": items}
            attention = found["attention"]
            return self.be("asks", name, attention["reason"], run, text=attention["text"], ref=ref)
        if name == "failed":
            return self.be("failed", "failed", "blocked", run)
        if name == "noChange":
            self.doc["waits"] = []
            return self.be("no change", name)
        if name in ("planning", "working"):
            # The engine's answers start this stage again: the sweep carries that out.
            self.doc["asked"] = list(found["asked"])
            self.wait("start")
            return self.be("answered", name)
        raise item_state.StateError(f"The engine put a blocked `/nexkit go` in {name!r}.")

    def after_plan(self, run):
        plan = self.doc["plan"]
        if run.get("status") != SUCCESS or plan is None:
            return self.be("failed", "failed", "failed", run)
        if plan["noChange"]:
            return self.be("no change", "noChange")
        ref = plan["text"]["ref"]
        if plan["questions"]:
            return self.be("planned", "awaitingApproval", "question", run, ref=ref)
        if plan["protectedPaths"]:
            return self.be("planned", "awaitingApproval", "handMadeChange", run, ref=ref)
        return self.be("planned", "awaitingApproval", "approval", run, ref=ref)

    def round(self, comment, latest):
        """Place an item whose NexKit pull request is open by its latest round, as
        `autopilot._pull_step` reads it."""
        if latest is None:
            # The round that opened it has not reported yet.
            return self.running("no round yet", "checking")
        status, outcome = latest.get("status"), latest.get("outcome")
        name = OF_ACTION.get(run_action(latest), "checking")
        if status in autopilot.ACTIVE:
            return self.active(comment, latest, name, automatic=counted_fix(latest))
        if outcome in CHAINED:
            started = latest.get("outcome") == "auto_fix"
            return self.running("next round", _next_state(latest, name), automatic=started)
        if outcome == "lost":
            # `autopilot._reportless`: a fix round's command again, or a review for any other.
            command = "fix" if run_action(latest) == "fix" else "review"
            return self.lost(latest, command, self.pull["number"])
        # What follows was decided on the head the round checked: a head pushed since is in
        # the observation, which the engine checks before it goes on (`_released`,
        # `_merge_step`).
        self.on_checked_head(latest)
        if outcome == HELD:
            return self.held_back(latest)
        if outcome == "merge_waiting":
            # The merge is tried again at once, for six hours from the first try
            # (`autopilot._merge_step`); then a person finds out why the checks never ended.
            since = parse_iso(latest.get("waiting_since"))
            # A branch behind its base keeps being tried under `conflicts.auto_resolve`, however
            # long it waited, as the engine decides (`machine.behind_exempt`).
            behind = (self.pull or {}).get("mergeable_state") == "behind"
            keeps_trying = behind and self.cfg["conflicts"]["auto_resolve"]
            if since is not None and self.now - since > autopilot.LOST and not keeps_trying:
                text = "The required checks have not passed for six hours."
                return self.be("needs a person", "ready", "acceptRefused", None, text)
            if since is not None:
                # The six hours go on counting from the first refusal on this head.
                self.doc["checksSince"] = {
                    "version": self.doc["change"]["version"],
                    "at": iso(since),
                }
            self.wait("requiredChecks", until=iso(self.now))
            return self.be("required checks", "accepting")
        if outcome == CI_WAITING:
            # The fix round waits until the base branch's failure ends: its bug issue closes,
            # or the base passes again. NexKit 6 counts it when it starts it, and a person
            # decides when none is left then, as the engine does (`due`).
            wait = latest.get("ci_wait") or self.pr_state.get("ci_wait") or {}
            self.doc["rounds"]["due"] = True
            self.wait("baseFailure", on=str(wait.get("bug") or "none"))
            return self.be("base failure", "fixing")
        if outcome == "ready":
            return self.ready(latest)
        named = {
            "needs_human": "roundsUsedUp",
            "recheck_failed": "roundsUsedUp",
            "merge_refused": "acceptRefused",
            "conflicting": "acceptRefused",
            PULL_CLOSED: "reopened",
            UNTRUSTED_MERGE: "untrusted",
        }
        if outcome in named:
            if outcome == "merge_refused" and latest.get("refused_clean"):
                # GitHub called the head mergeable and still refused it: the engine does not
                # try that head again (`refusedAcceptable`).
                version = (self.doc["change"] or {}).get("version")
                if latest["refused_clean"] == version:
                    self.doc["refusedAcceptable"] = version
            return self.be("needs a person", "ready", named[outcome], latest)
        if outcome == "agent_blocked" and latest.get("questions"):
            stage = "implement" if latest.get("action") == "implement" else "fix"
            return self.asks(comment, latest, stage)
        if status == FAILURE:
            if outcome == UNTRUSTED_ROUND:
                return self.be("failed", "failed", "untrusted", latest)
            blocked = outcome == "agent_blocked"
            return self.be("failed", "failed", "blocked" if blocked else "failed", latest)
        return self.ready(latest)

    def asks(self, comment, run, stage):
        """A round of `stage` that stopped as blocked and asked, in the comment `comment` whose
        marker is `run`: its questions wait for answers, which name that comment."""
        ref = str(comment["id"])
        asked = [_question(item, True) for item in questions(run["questions"])]
        found = bool(filled(run.get("no_change")))
        asked = asked[: MOST["questions"]]
        self.doc["questions"] = {"ref": ref, "stage": stage, "items": asked, "noChange": found}
        return self.be("asks", "awaitingApproval", "question", run, ref=ref)

    def ready(self, row):
        rounds = self.doc["rounds"]
        if "unverifiable" in rounds["criteria"]:
            reason = "unverifiableCriterion"
        elif row.get("decide") or "not_met" in rounds["checklist"]:
            # NexKit 6 names the items a person decides; the review's own answers may not
            # have been kept, so the state says one is not met.
            reason = "personChecklistItem"
            if "not_met" not in rounds["checklist"]:
                rounds["checklist"] = [*rounds["checklist"][: MOST["checklist"] - 1], "not_met"]
        else:
            reason = "accept"
        return self.be("ready", "ready", reason, row)

    def held_back(self, row):
        """A round that a label held back from its merge, its fix round or its conflict
        round, as `autopilot._released` starts it: the item is in the state of that step,
        which starts once the hold ends, or, when the label is already gone, on the sweep's
        start. A conflict round waits in its checks, where the engine follows the conflict
        policy, with its count and limit, if the change still conflicts, and checks it if
        not. A fix round is due, and counted when it starts, as NexKit 6 counts it, or goes
        to a person when none is left then. A head pushed since the label is gone is checked
        first, as `/nexkit review`."""
        back = row.get("held_back")
        name = {"merge": "accepting", "conflicts": "checking"}.get(back, "fixing")
        if not self.doc["hold"] and self.head_moved():
            self.doc["change"]["version"] = self.pull["head"]["sha"]
            self.wait("start")
            return self.be("held, label gone", "checking")
        if name == "fixing":
            self.doc["rounds"]["due"] = True
        if self.doc["hold"]:
            return self.be("held", name)
        self.wait("start")
        return self.be("held, label gone", name)

    def on_checked_head(self, row):
        """Keep the head the round checked as the change's version, while the observation
        carries the head the pull request has now."""
        head = str(row.get("head") or "")
        if COMMIT.fullmatch(head):
            self.doc["change"]["version"] = head

    def head_moved(self):
        return self.pull["head"]["sha"] != self.doc["change"]["version"]

    # A run that is on its way, paused or lost.

    def active(self, comment, run, name, automatic=False):
        """A run that runs, continues in another, or paused, as `autopilot._stalled` reads it.
        `automatic` marks a run that runs as an automatic fix round. A run that runs again after
        a transient error keeps the count of its runs again."""
        if run.get("status") != PAUSED:
            self.keep_retries(run.get("retry"))
            return self.running("started", name, automatic)
        if run.get("verdict") == "paused":
            # A round paused at its review.
            name = "reviewing"
        resume = run.get("resume")
        if not isinstance(resume, dict):
            return self.be("pause for a person", "failed", "failed", run, SAYS["paused"])
        at = parse_iso(resume.get("at"))
        if resume.get("retry"):
            if at is None or self.now - at > autopilot.LOST:
                return self.be("pause for a person", "failed", "failed", run, SAYS["overdue"])
            # The command that runs again, such as `review` after a review's transient error.
            name = OF_COMMAND.get(resume.get("command"), name)
            self.wait("time", until=iso(at))
            self.keep_retries(resume["retry"])
            return self.be("retrying", name)
        paused_at = parse_iso(comment.get("created_at"))
        stale = paused_at is not None and self.now - paused_at > PAUSE_WINDOW
        if stale or at is None:
            return self.be("pause for a person", "failed", "failed", run, SAYS["stale"])
        if not self.cfg["usage_limit"]["resume"]:
            return self.be("pause for a person", "failed", "failed", run, SAYS["paused"])
        self.wait("usageLimit", until=iso(at))
        return self.be("paused", name)

    def keep_retries(self, count):
        """The engine counts the runs again after a transient error on from `count`, when it is
        a positive number, as NexKit 6 does."""
        if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
            self.doc["again"] = {"transient": min(count, 10)}

    def lost(self, run, command, target):
        """A run that ended without a report: one NexKit started by itself starts its
        `command` on `target` again, later each time, as `autopilot._reportless` and `_again`
        do; a person's, or one out of tries, waits for a person. Autopilot's record counts
        only when it is of the same command and target and started within six hours; one
        that says the agent ran (`once`) allows one more start."""
        automatic = bool(run.get("autopilot")) or bool(
            autopilot.run_kinds(run) & {"auto", "autopilot"}
        )
        record = self.record
        at = parse_iso(record.get("at"))
        same = (record.get("command"), str(record.get("target"))) == (command, str(target))
        record = record if same and at is not None and self.now - at <= autopilot.LOST else {}
        tries = record.get("tries")
        tries = tries if isinstance(tries, int) and not isinstance(tries, bool) else 0
        limit = 1 if record.get("once") else len(autopilot.AGAIN)
        if not automatic or tries >= limit:
            return self.be("lost for a person", "failed", "outage", run, SAYS["outage"])
        due = max((at or self.now) + autopilot.AGAIN[tries], self.now) if tries else self.now
        self.wait("time", until=iso(due))
        # The engine counts this loss as it does before it waits, so later restarts go on from it.
        self.doc["again"] = {"lost": min(tries + 1, 10)}
        if record.get("once"):
            self.doc["again"]["once"] = True
        return self.be("lost", OF_COMMAND.get(command, "planning"))

    # What the state holds beside its name.

    def be(self, kind, name, reason=None, run=None, text=None, ref=None):
        """Put the item in state `name`, as the import's `kind`, with attention for
        `reason`, said as NexKit 6's marker of `run` says it, or as `text`, about `ref`: the
        text that asked, which a person's decision names."""
        if kind not in KINDS:
            raise ValueError(f"The import has no kind {kind!r}.")
        self.kind = kind
        self.doc["state"] = name
        if reason:
            said = str((run or {}).get("attention") or "").strip()
            text = clip(said, "reason") if said else (text or SAYS[reason])
            self.doc["attention"] = {"reason": reason, "text": text, "ref": ref}

    def running(self, kind, name, automatic=False):
        """A command or round on its way: the action the engine waits for. An `automatic` fix
        round that holds a count is marked, so that a stop gives the count back, as NexKit 6
        does (`report._stop_round`)."""
        rounds = self.doc["rounds"]
        if automatic and name == "fixing" and rounds["counted"] >= 1:
            rounds["automatic"] = True
        version = (self.doc["change"] or {}).get("version")
        if name == "checking":
            action = {"id": RUN_ID, "next": None, "kind": "runChecks", "changeVersion": version}
        elif name == "accepting":
            action = {"id": RUN_ID, "next": None, "kind": "accept", "changeVersion": version}
            action["by"] = "engine"
        else:
            action = agent_run(STAGES[name], version)
        self.doc["pending"].append(action)
        return self.be(kind, name)

    def wait(self, kind, until=None, on=None):
        number = len(self.doc["waits"]) + 1
        wait = {"id": f"w{REVISION}.{number}", "for": kind, "until": until, "on": on}
        self.doc["waits"].append(wait)

    def take_rounds(self):
        """The rounds of the pull request as the engine counts them, and the last checks
        and review that NexKit 6's state comment recorded."""
        rounds, rows, record = self.doc["rounds"], [run for _, run in self.rows], self.pr_state
        started = len([row for row in rows if run_action(row) in ("implement", "fix")])
        # At least the round that opened the pull request, which an old marker may not name.
        rounds["started"] = max(started, 1)
        rounds["counted"] = _count(record.get("auto_fixes"))
        rounds["conflicts"] = conflict_rounds_used(rows)
        # The updates of the branch from its base, counted apart from conflict rounds.
        updates = len([row for row in rows if row.get("updated_branch")])
        if updates:
            rounds["updates"] = updates
        feedback = record.get("feedback") or {}
        feedback = feedback if isinstance(feedback, dict) else {}
        rounds["signatures"] = [
            signature(check.get("signature") or check.get("name"))
            for check in feedback.get("checks") or []
            if isinstance(check, dict) and not check.get("passed") and not check.get("skipped")
        ][: MOST["signatures"]]
        # The checks that failed the same way before and after the last fix round, which the
        # next automatic fix round diagnoses, and the failing checks that the checks after the
        # next fix round compare with. `fixed` is not imported: every imported fix round still
        # publishes, and the engine sets `fixed` then. An import kind that leaves a fix round's
        # checks pending would need `fixed` too. A conflicting round records no checks.
        failed = failing(feedback.get("checks"))
        if failed:
            rounds["failing"] = failed
        found = feedback.get("repeated")
        found = found if isinstance(found, list) else []
        repeated = [clip(name, "check") for name in found if isinstance(name, str)]
        if repeated:
            rounds["repeated"] = repeated[: MOST["repeated"]]
        review = feedback.get("review") or {}
        review = review if isinstance(review, dict) else {}
        rounds["criteria"] = [
            item["status"]
            for item in review.get("criteria") or []
            if isinstance(item, dict) and item.get("status") in ("met", "unmet", "unverifiable")
        ][: MOST["criteria"]]
        rounds["checklist"] = [
            item["answer"]
            for item in review.get("checklist") or []
            if isinstance(item, dict) and item.get("answer") in ("met", "not_met", "not_applicable")
        ][: MOST["checklist"]]
        findings = (record.get("last_review") or {}).get("findings") or []
        findings = [
            item
            for item in findings
            if isinstance(item, dict) and item.get("severity") in ("blocking", "suggestion")
        ]
        findings.sort(key=lambda item: item["severity"] != "blocking")
        rounds["findings"] = [
            {"severity": item["severity"], "text": clip(item.get("body") or "", "finding")}
            for item in findings[: MOST["findings"]]
        ]
        rounds.update(history(record))


def history(record):
    """The fix rounds and the last review of NexKit 6's state comment `record`, as the engine
    keeps them in `rounds`: {fixes, lastReview}, each only when the record holds one."""
    found = {}
    fixes = [fix for fix in map(_fix, record.get("fixes") or []) if fix]
    if fixes:
        found["fixes"] = fixes[-MOST["fixes"] :]
    last = _last_review(record.get("last_review"))
    if last:
        found["lastReview"] = last
    return found


def for_prompt(item, change, record, fix=None):
    """The state document that the prompt call reads the earlier rounds from, for the work
    item `item` whose change is `change`, {id, version} or None, on NexKit 6's state comment
    `record`. It holds the change and the history only. With `fix`, this run's fix round, in
    NexKit 6's form {auto, summary, note, cause}, the history ends with it as the newest
    record: the round that a review after it follows."""
    doc = empty(str(item))
    doc["change"] = change
    record = record if isinstance(record, dict) else {}
    if fix:
        rounds = [
            entry.get("round")
            for entry in [*(record.get("fixes") or []), record.get("last_review")]
            if isinstance(entry, dict)
        ]
        number = max([n for n in map(_number, rounds) if n is not None], default=0) + 1
        record = {**record, "fixes": [*(record.get("fixes") or []), {**fix, "round": number}]}
    doc["rounds"].update(history(record))
    return doc


def _number(value):
    """`value` when it is an integer that is not a boolean, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value, kind):
    """A text of NexKit 6's record as the engine keeps it of the history."""
    return clip_text(value if isinstance(value, str) else "", HISTORY[kind])


def _fix(item):
    """One fix round of NexKit 6's record as the engine keeps it (`rounds.fixes`), or None for
    one that names no round."""
    if not isinstance(item, dict) or _number(item.get("round")) is None:
        return None
    fix = {
        "round": item["round"],
        "auto": item.get("auto") is True,
        "diagnosis": item.get("diagnosis") is True,
        "note": _text(item.get("note"), "note"),
        "summary": _text(item.get("summary"), "summary"),
        "cause": _text(item.get("cause"), "cause"),
    }
    if isinstance(item.get("failing"), list):
        fix["failing"] = [
            {
                "name": clip(entry.get("name") or "", "check"),
                "signature": _text(entry.get("signature"), "signature"),
            }
            for entry in item["failing"]
            if isinstance(entry, dict)
        ][: MOST["fixFailing"]]
    if isinstance(item.get("conflicts"), list):
        fix["conflicts"] = [
            clip(name, "path") for name in item["conflicts"] if isinstance(name, str)
        ][: MOST["fixFailing"]]
    return fix


def _last_review(item):
    """The last review of NexKit 6's record as the engine keeps it (`rounds.lastReview`), or
    None for one that names no round, version or verdict the protocol knows."""
    if not isinstance(item, dict) or _number(item.get("round")) is None:
        return None
    head, verdict = item.get("head"), item.get("verdict")
    if not isinstance(head, str) or verdict not in ("approve", "request_changes"):
        return None
    found = [
        entry
        for entry in item.get("findings") or []
        if isinstance(entry, dict) and entry.get("severity") in ("blocking", "suggestion")
    ]
    review = {
        "round": item["round"],
        "version": head,
        "verdict": verdict,
        "findings": [
            {
                "severity": entry["severity"],
                "file": clip(
                    entry.get("file") if isinstance(entry.get("file"), str) else "", "path"
                ),
                "line": _number(entry.get("line")) or 0,
                "body": _text(entry.get("body"), "body"),
            }
            for entry in found[: MOST["findings"]]
        ],
    }
    left_out = _number(item.get("left_out"))
    if left_out and left_out > 0:
        review["leftOut"] = left_out
    return review


def _working(doc, again):
    """`doc` while its `/nexkit go` runs, which the engine waits for. `again` says that
    autopilot's answers started this round and no person commented since
    (`autopilot.asked_again`)."""
    doc["state"] = "working"
    doc["pending"] = [agent_run("implement", None)]
    doc["asked"] += ["answers"] * again
    if doc["plan"]:
        doc["plan"]["approved"] = True
    return doc


def _round_result(number, comment, run):
    """The end of the `/nexkit go` whose run comment `comment` holds the marker `run`: a
    blocked round's result, or for any other failed round an agent error, worded from NexKit's
    own marker and never from agent text."""
    from .host import AGENT, _event, _run_event_id, round_output  # host imports this module

    if run.get("outcome") == "agent_blocked":
        result = {**AGENT, "output": round_output(run)}
    else:
        text = str(run.get("attention") or "The round failed.")[: LONGEST["reason"]]
        result = {**AGENT, "error": text, "output": None}
    return _event(
        "actionFinished",
        number,
        comment.get("created_at"),
        _run_event_id(run.get("url")),
        action=RUN_ID,
        changeVersion=None,
        result=result,
    )


def agent_run(stage, version):
    """The `runAgent` action that a NexKit 6 run of `stage` on its way stands for, on the change
    version `version`, as the engine asks for it."""
    return {
        "id": RUN_ID,
        "next": None,
        "kind": "runAgent",
        "stage": stage,
        "changeVersion": version,
        "access": ACCESS[stage],
        "inputs": INPUTS[stage],
        "result": f"results/{stage}.schema.json",
        "agent": None,
        "model": None,
        "effort": None,
        "budget": {"tokens": None, "seconds": None},
        "session": None,
        "resume": False,
    }


def _latest(rows):
    """(comment, round) of the latest round that says something about the pull request: one
    that found nothing to merge does not (`autopilot._meaningful`)."""
    for comment, row in reversed(rows):
        if row.get("outcome") != "up_to_date":
            return comment, row
    return rows[-1] if rows else (None, None)


def _next_state(row, name):
    """The state of the run that a round started next: a fix round, its own command again,
    or a review of the head that a branch update or a moved head gave."""
    if row.get("outcome") in ("auto_fix", "conflict_fix"):
        return "fixing"
    command = (row.get("next") or {}).get("command")
    if command in OF_COMMAND and command != "plan":
        return OF_COMMAND[command]
    if row.get("outcome") in ("branch_updated", "head_moved", "superseded"):
        return "checking"
    return name


def _ended(part):
    if part.get("state") != "closed":
        return None
    return "dropped" if part.get("state_reason") not in (None, "completed") else "accepted"


def plan_of(comment, data, approved):
    """The plan as the engine keeps it, from the plan comment and its record, each list to
    MOST and each text clipped as the engine keeps them."""
    data = data or {}
    split = [part for part in data.get("split") or [] if isinstance(part, dict)]
    # The criteria the comment lists last: agent text before them cannot add to them.
    found = CRITERIA.findall(comment.get("body") or "")
    return {
        "text": {"name": "plan", "ref": str(comment["id"])},
        "questions": [_question(item) for item in questions(data.get("questions"))],
        "criteria": len(found[-1].splitlines()) if found else 0,
        "tooLarge": bool(data.get("too_large")),
        "parts": planned_parts(comment, data),
        "noChange": bool(filled(data.get("no_change"))),
        "protectedPaths": [
            clip(path, "path")
            for path in (data.get("protected_paths") or [])[: MOST["protectedPaths"]]
        ],
        "approved": approved,
        "split": [
            {
                "key": number,
                "title": clip(part.get("title") or "", "title"),
                "needs": depends_on(part, number),
            }
            for number, part in enumerate(split if len(split) <= MOST["split"] else [], 1)
        ],
    }


def _question(item, round_=False):
    """A question of a plan's or, with `round_`, a round's record, which names options by
    their texts, as the state keeps it, naming them by their numbers from 1."""
    options = [clip(option, "option") for option in item["options"][: MOST["options"]]]
    texts = item["options"][: MOST["options"]]

    def number(text):
        return texts.index(text) + 1 if text in texts else 0

    found = {"question": clip(item["question"], "question"), "options": options}
    found["recommended"] = number(item["recommended"])
    if item.get("closing"):
        found["closes"] = number(item["closing"])
    if round_:
        found["replans"] = sorted({number(text) for text in item.get("replanning") or []} - {0})
    return found


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


def import_item(gh, cfg, number, pull=None, now=None):
    """Read work item `number` on GitHub, with its NexKit pull request `pull` if one is open,
    and give its first state document."""
    now = now or datetime.now(UTC)
    issue = gh.issue(number)
    comments = gh.comments(number)
    if "pull_request" in issue:
        return imported(issue, comments, cfg, now, gh.pull(number))
    taken = autopilot.takes(cfg) and autopilot.taken(gh, cfg, issue)
    details = gh.pull(pull) if pull else None
    pull_comments = gh.comments(pull) if pull else []
    blockers = [found for found in gh.blocked_by(number) if found.get("state") == "open"]
    # An issue with no pull request waits for the lower waves of its milestone; `_assess`
    # reads waves only for one.
    waited = None if details else autopilot.waited_waves(gh, cfg, issue)
    return imported(
        issue,
        comments,
        cfg,
        now,
        details,
        pull_comments,
        gh.sub_issues(number),
        blockers,
        bool(autopilot.hold_label(cfg, issue)),
        autopilot.depth(issue, autopilot.lineage(gh, issue)),
        (waited[2] or []) if waited else [],
        taken,
    )
