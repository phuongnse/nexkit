"""The GitHub host's calls of the engine's `step` (docs/engine.md#getting-there).

Decisions move into the engine a group at a time, and until the GitHub host runs only through
it, NexKit 6's code moves an item between the decisions that the engine holds. The host keeps no
state document for them: for each one, it builds the item's document from NexKit 6's markers, as
the import reads them (`first_state`), sends the event that GitHub shows, and carries out the
answer with NexKit 6's code, in NexKit 6's words.

So far the engine decides what follows a plan: its approval by autopilot, the answers to its
questions, once in a row, a writer's answer that only closes the issue, a plan that finds no
change or needs a hand-made change, splits and their rules, a new plan after a collaborator's
comment, and what a plan waits for without autopilot. One stays NexKit 6's
(`autopilot._replanned`): a plan that splits an issue whose pull request's round ran out of
time, since the engine never splits an item that has a change (docs/engine.md#states).

It also decides what follows a `/nexkit go` that stopped as blocked with questions or with
protected paths its change needs (`autopilot.engine_round`): the answers to its questions, once
in a row, a recommended option that only closes the issue, with the round's finding that no
change is needed or without it, one that only a new plan carries out, a writer's answer that
only closes the issue, by a comment or a command, and a new plan after a collaborator's comment
or the close of an issue that blocks it.

And it decides what follows a round's checks and review on a NexKit pull request (`report`):
the review's verdict and criteria, an automatic fix round within `fix.max_auto_rounds`, counted,
one that waits while the base branch has the same failure or while a hold is on, a fix round
that changed nothing, the rounds used up, and a change that passes, which a person confirms,
decides or merges, or which merges by `merge.auto` unless a hold keeps it back. A round whose
next step the engine refuses to decide waits for a person.

It also decides what follows a fix round that stopped as blocked with questions or protected
paths, as after a blocked `/nexkit go` but on the pull request, where an answer that changes
the issue's scope is a person's; and what follows an implement or fix round that ran out of
time: one plan that splits the issue, unless the issue asked before, a parent asked, or it is a
part at the deepest level.

It decides the end of a round's wait too, in the sweep: once the label that held back a merge
or an automatic fix round is gone, a head pushed meanwhile is reviewed, and the fix round runs,
counted only now, while `fix.max_auto_rounds` has one left; once the base branch's failure
that a fix round waited for ends, its bug issue closed or the base passing again, the same.
And it decides which rounds count: an automatic fix round that NexKit stopped gives its count
back (`report._stop_round`, `maintain._give_back`), and required checks that fail at the merge
start a counted fix round, wait for the base branch, or leave the rounds used up
(`report._required_failed`).

Every round's checks and review reach it (`round_ended`): a review that paused at a usage
limit, a review that failed, and a person's own pull request, which is only reviewed; a
review that runs again after a transient error is the host's own retry. It finds the checks
that failed the same way before and after a fix round, which the state comment keeps, and
names those that the fix round `route` starts diagnoses (`diagnosis`). A round whose commit
conflicts with its base branch starts the round that resolves it, within
`conflicts.max_rounds`, unless a label holds the issue, which keeps it back until the sweep
finds the label gone (`conflicting`, `conflicts_released`).

An item whose document or event the engine refuses waits for a person, and no other item is
held up by it.
"""

from __future__ import annotations

import json

from . import autopilot, engine, first_state
from .checks import failing_signatures
from .protocol.size import LONGEST, MOST, failure_key
from .report import TOO_LARGE
from .schema import filled
from .state import (
    MAX_PARTS,
    asked_last,
    closing_answer,
    closing_option,
    latest_plan,
    plan_data,
    plan_record,
    planned_parts,
    questions,
    recommended_option,
    replanning_options,
    run_comments,
)

# A runner's result around a plan: the plan's run ended in NexKit 6, which keeps only its record.
AGENT = {
    "tokens": None,
    "limit": None,
    "transient": False,
    "noResult": False,
    "error": None,
    "session": None,
    "changeVersion": None,
    "timedOut": False,
    "seconds": None,
    "usd": None,
}
# What the engine asks a plan once more for, as autopilot's record keeps it.
REMEMBER = {"fewer": "fewer_asked", "parts": "parts_asked", "titles": "titles_asked"}
# The time of an event that GitHub gives no time for.
NO_TIME = "1970-01-01T00:00:00Z"
# What a person does about an issue whose next step the engine refused to decide.
REFUSED_DO = "read why below, then carry the issue on with `/nexkit plan` or `/nexkit go`"


class Refused(Exception):
    """The engine refused an item's document or event, or answered nothing that the host
    carries out."""


def policies(cfg):
    """The engine's policies for the GitHub host's configuration
    (docs/engine.md#what-a-host-decides)."""
    taken = autopilot.takes(cfg)
    return {
        "planApproval": "autoWhenClean" if taken else "person",
        "reviewAfterFailingChecks": "always",
        "accept": "autoWhenPassed" if cfg["merge"]["auto"] else "person",
        "fixRounds": cfg["fix"]["max_auto_rounds"],
        "whenRoundsRunOut": "person",
        "conflicts": "resolve" if cfg["conflicts"]["auto_resolve"] else "person",
        "conflictRounds": cfg["conflicts"]["max_rounds"],
        "answers": cfg["autopilot"]["answer"],
        "splits": taken,
        "splitParts": MAX_PARTS,
        "splitDepth": autopilot.MAX_DEPTH,
    }


def step(state, event, found_policies):
    """The engine's answer to `event` on `state`, sent and read as JSON, as `nexkit-engine`
    would; `Refused` when the engine refuses it."""
    request = {
        "protocol": 1,
        "call": "step",
        "state": state,
        "event": event,
        "policies": found_policies,
    }
    found = engine.answer(json.loads(json.dumps(request)))
    if "error" in found:
        raise Refused(found["error"])
    return found


def refused_reason(error):
    """Why an issue waits whose next step the engine refused to decide."""
    return autopilot.person(*refused_parts(error))


def refused_parts(error):
    """(do, why) of `refused_reason`."""
    why = f"NexKit's engine refused to decide its next step: {autopilot.quoted(str(error), 300)}"
    return REFUSED_DO, why


# -- the document and the events -------------------------------------------------------


def plan_output(plan, data, run):
    """The plan's result, as the plan stage gives it, from the record `data` at the end of the
    plan comment `plan`, for what the engine decides from. `run` is the plan's run."""
    if data is None:
        # A plan from before NexKit kept its record: its run's attention says that it asked.
        asked = [{"question": "", "options": [], "recommended": 0, "closes": 0}]
        return _output(asked if run.get("attention") else [], False, [], [], "")
    asked = [
        {
            "question": item["question"],
            "options": item["options"],
            "recommended": _number(item["options"], item["recommended"]),
            "closes": _number(item["options"], closing_option(item)),
        }
        for item in questions(data.get("questions"))
    ]
    parts = [
        {
            "title": part.get("title") or "",
            "body": part.get("body") or "",
            # A plan from before parts named their dependencies: each needs the one before.
            "depends_on": part.get("depends_on", [number - 1] if number > 1 else []),
        }
        for number, part in enumerate(data.get("split") or [], 1)
    ]
    # The record keeps no part of a plan that lists more than a split files, only how many.
    missing = planned_parts(plan, data) - len(parts)
    parts += [{"title": "", "body": "", "depends_on": []} for _ in range(missing)]
    return _output(
        asked,
        bool(data.get("too_large")),
        parts,
        data.get("protected_paths") or [],
        data.get("no_change") or "",
    )


def _output(asked, too_large, split, protected, finding):
    return {
        "summary": "",
        "changes": [],
        "decisions": [],
        "risks": [],
        "acceptance_criteria": [],
        "implementation_notes": [],
        "revision": {"started_over": False, "changes": []},
        "questions": asked,
        "too_large": too_large,
        "split": split,
        "protected_paths": list(protected),
        "no_change": finding,
    }


def _number(options, text):
    return options.index(text) + 1 if text and text in options else 0


def _document(issue, record, plan=None, data=None, change=None):
    """The document of `issue` as NexKit 6's markers leave it for a decision after a plan:
    its title, what autopilot's `record` asked for, and the plan comment `plan` with its record
    `data`, if given, and its pull request's `change`."""
    doc = first_state.empty(str(issue["number"]))
    doc["title"] = str(issue.get("title") or "")[: LONGEST["title"]]
    doc["change"] = change
    doc["plan"] = first_state.plan_of(plan, data, False) if plan else None
    doc["asked"] = [name for key, name in first_state.ASKED.items() if record.get(key)]
    return doc


def _planning(doc, depth, again):
    """`doc` while its plan runs, which the engine waits for. `again` says that autopilot's
    answers started this plan and no person commented since (`autopilot.asked_again`)."""
    doc.update(state="planning", depth=depth)
    doc["pending"] = [first_state.agent_run("plan", (doc["change"] or {}).get("version"))]
    doc["asked"] += ["answers"] * again
    return doc


def _waiting(doc):
    """`doc` while its plan waits. NexKit 6 decides after a plan at the next sweep, so a
    comment that came before that sweep is taken on the plan as it waits."""
    plan = doc["plan"]
    reason = "question" if plan["questions"] else "approval"
    doc["state"] = "awaitingApproval"
    doc["attention"] = {
        "reason": reason,
        "text": first_state.SAYS[reason],
        "ref": plan["text"]["ref"],
    }
    return doc


def _event(kind, number, at, name, **fields):
    return {
        "protocol": 1,
        "kind": kind,
        "id": name,
        "item": str(number),
        "at": at or NO_TIME,
        **fields,
    }


def _result(number, plan, data, run, change=None):
    """The end of the plan's run, with the plan its comment `plan` holds."""
    version = (change or {}).get("version")
    return _event(
        "actionFinished",
        number,
        plan.get("created_at"),
        f"plan-{plan.get('id')}",
        action=first_state.RUN_ID,
        changeVersion=version,
        result={**AGENT, "changeVersion": version, "output": plan_output(plan, data, run)},
    )


def _person(login, role):
    return {"id": login or "someone", "role": role}


def _depth(gh, issue, data):
    """How deep a part `issue` is, read only for a plan that is too large, the one decision
    that reads it, as NexKit 6 reads it."""
    if not (data or {}).get("too_large"):
        return 0
    return autopilot.depth(issue, autopilot.lineage(gh, issue))


def _picked(text, data):
    """(question, option, text) of the option that only closes the issue that a person's
    `text` picks among the questions of the plan record or run marker `data`, or None
    (`state.closing_answer`)."""
    items = (data or {}).get("questions")
    option = closing_answer(text, items) if items else None
    if not option:
        return None
    for number, item in enumerate(questions(items), 1):
        if closing_option(item) == option:
            return number, item["options"].index(option) + 1, option
    return None


def _said(gh, number, comment, picked, doc):
    """The event of a collaborator's `comment` on the plan that waits in `doc`: an answer
    when it picks the option that only closes the issue, which `picked` names, otherwise a
    comment."""
    by = (comment.get("user") or {}).get("login") or ""
    at = comment.get("created_at")
    if picked:
        # Closing is a write: a collaborator who may only read changes the plan at most.
        role = "writer" if by and gh.can_write(by) else "reader"
        answers = [{"question": picked[0], "option": picked[1]}]
        asked = doc["attention"]["ref"]
        name = f"answer-{comment['id']}"
        fields = {"by": _person(by, role), "answers": answers, "note": "", "asked": asked}
        return _event("answered", number, at, name, **fields)
    # A collaborator, as `autopilot._commented_after` reads them: whether they may write
    # matters only to an answer that closes.
    text = {"name": "comment", "ref": str(comment["id"])}
    by = _person(by, "reader")
    return _event("commented", number, at, f"comment-{comment['id']}", by=by, text=text)


# -- what follows a plan -------------------------------------------------------------------


def after_plan(gh, cfg, issue, plan, run, record, start, comments, pull=None):
    """The step after the latest plan of `issue`, the comment `plan` that the run `run`
    posted, as the engine decides it: with `pull`, the plan autopilot asked for after a round
    of that pull request ran out of time. A collaborator's comment after an issue's plan comes
    first (`autopilot._assess`). `record` is autopilot's record and `start` what a command
    that the step starts begins from. Raises `Refused`."""
    number = issue["number"]
    data = plan_data(plan)
    said = [] if pull else autopilot._commented_after(comments, plan)
    picked = _picked(said[-1].get("body"), data) if said else None
    if said:
        doc = _waiting(_document(issue, record, plan, data))
        event = _said(gh, number, said[-1], picked, doc)
    else:
        change = None
        if pull:
            change = {"id": str(pull), "version": gh.pull(pull)["head"]["sha"]}
        doc = _document(issue, record, change=change)
        doc = _planning(doc, _depth(gh, issue, data), autopilot.asked_again(comments))
        event = _result(number, plan, data, run, change)
    found = step(doc, event, policies(cfg))
    view = _PlanView(issue, plan, data, run, start, pull, picked, said)
    return view.carry(found["actions"], doc["asked"], found["state"]["asked"])


class _PlanView:
    """What autopilot does for one answer of the engine after a plan, in NexKit 6's words."""

    def __init__(self, issue, plan, data, run, start, pull, picked, said):
        self.issue, self.plan, self.data, self.run = issue, plan, data or {}, run
        self.start, self.pull, self.picked, self.said = start, pull, picked, said

    def carry(self, actions, before, after):
        kinds = {action["kind"]: action for action in actions}
        if "close" in kinds:
            return self.closed(kinds["close"])
        if "createParts" in kinds:
            return self.split(kinds["createParts"])
        if "runAgent" in kinds:
            shown = any(a["kind"] == "show" and a["what"] == "answers" for a in actions)
            asked = [name for name in after if name not in before]
            return self.started(kinds["runAgent"], shown, asked)
        if "needPerson" in kinds:
            return self.waits(kinds["needPerson"]["reason"])
        raise Refused("The engine answered nothing to carry out.")

    def step(self, state, reason="", **fields):
        return autopilot._step(self.issue, state, reason, **fields)

    def closed(self, close):
        url = self.plan.get("html_url") or ""
        if close["outcome"] == "notPlanned":
            by = (self.said[-1].get("user") or {}).get("login") or ""
            reason = f"@{by} answered with the option that only closes the issue"
            return self.step("close", reason, answer=self.picked[2], by=by, asked=url)
        # Another round would find the same.
        reason = "its plan finds that no change is needed"
        return self.step("close", reason, finding=self.data["no_change"], plan_url=url)

    def split(self, create):
        # The parts the engine files, as the plan's record holds them, which needs no more
        # than the earlier parts that each part's `needs` names.
        recorded = self.data.get("split") or []
        parts = [recorded[part["key"] - 1] for part in create["parts"]]
        fields = {"parts": parts, "plan_url": self.plan.get("html_url") or "", "pull": self.pull}
        return self.step("split", "the plan splits the issue", **fields, **self.start)

    def started(self, run, shown, asked):
        start = self.start
        if run["stage"] == "fix":
            reason = "the plan after the round that ran out of time needs nobody"
            fields = {**start, "target": self.pull, "note": autopilot.REPLANNED_NOTE}
            return self.step("start", reason, command="fix", **fields)
        if run["stage"] == "implement":
            return self.step("start", "the plan needs nobody", command="go", **start)
        if shown:
            found = [
                {"question": item["question"], "answer": item["recommended"]}
                for item in questions(self.data.get("questions"))
            ]
            reason = "autopilot answers the plan's questions"
            return self.step("start", reason, command="plan", answers=found, **start)
        if not asked:
            reason = "collaborators commented after the plan"
            return self.step("start", reason, command="plan", **start)
        [name] = asked
        start = {**start, "note": self.note(name), "remember": {REMEMBER[name]: True}}
        return self.step("start", self.replan_reason(name), command="plan", **start)

    def parts(self):
        return planned_parts(self.plan, self.data)

    def note(self, name):
        if name == "fewer":
            return autopilot.FEWER_NOTE.format(parts=self.parts(), most=MAX_PARTS)
        if name == "parts":
            return autopilot.PARTS_NOTE
        repeated = autopilot.repeated_titles(self.issue, self.data.get("split") or [])
        titles = ", ".join(f'"{title}"' for title in repeated)
        return autopilot.TITLE_NOTE.format(titles=titles)

    def replan_reason(self, name):
        if name == "fewer":
            return (
                f"the plan has {self.parts()} parts and autopilot files at most {MAX_PARTS}, "
                "so autopilot plans again for fewer"
            )
        if name == "parts":
            return "the plan is too large and suggests no parts, so autopilot plans again"
        return "parts of the plan are titled like the issue, so autopilot plans again"

    def waits(self, reason):
        person, split_do = autopilot.person, autopilot.SPLIT_DO
        if reason == "question":
            text = self.run.get("attention") or autopilot.QUESTIONS_REASON
        elif reason == "askedAgain":
            text = autopilot.REPEATED
        elif reason == "closeRecommended":
            # The plan run's report names this reason and mentions `notify` with it, and an
            # older run that carried on records none.
            text = self.run.get("attention") or autopilot.CLOSE_UNFOUNDED
        elif reason == "handMadeChange":
            text = autopilot.protected_reason(self.data["protected_paths"])
        elif reason == "approval":
            # The plan is too large, and the engine splits no item with a change.
            text = TOO_LARGE
        elif reason == "splitDepth":
            # Its scope was set levels above, so a person splits it on, or implements it.
            text = person(autopilot.DEPTH_DO, autopilot.DEPTH_REASON)
        elif reason == "tooManyParts":
            parts, most = self.parts(), MAX_PARTS
            why = f"its plan has {parts} parts again, and autopilot files at most {most}"
            text = person(split_do, why)
        elif reason == "noParts":
            why = "its plan is too large for one session and suggests no parts again"
            text = person(split_do, why)
        elif reason == "partTitles":
            text = person(split_do, "the parts of its plan are titled like the issue again")
        else:
            raise Refused(f"The engine waits for a person for {reason!r} after a plan.")
        return self.step("waits", text)


def plan_carried_on(gh, cfg, issue, decision, output):
    """Whether autopilot carries on after the plan `output` that a run of `decision` posted,
    as the engine decides what follows it, so that `report` mentions nobody. A plan that finds
    no change carries on whatever else it says. A recommended answer that only closes the
    issue needs a person, whom `report` mentions: the sweep finds that mention since the run
    (`autopilot.mentioned`), so it mentions nobody again. Raises `Refused`."""
    if filled(output.get("no_change")):
        return True
    number = issue["number"]
    comments = gh.comments(number)
    pr = decision.get("pr")
    target = gh.comments(pr) if pr else comments
    again = autopilot.asked_again(target, comments if pr else ())
    data = plan_record(output)
    doc = _document(issue, autopilot.find_record(comments)[1])
    doc = _planning(doc, _depth(gh, issue, data), again)
    result = {**AGENT, "output": plan_output({}, data, {})}
    event = _event(
        "actionFinished",
        number,
        None,
        f"run-{decision.get('run_id') or number}",
        action=first_state.RUN_ID,
        changeVersion=None,
        result=result,
    )
    actions = step(doc, event, policies(cfg))["actions"]
    kinds = {action["kind"]: action for action in actions}
    if "runAgent" in kinds:
        return kinds["runAgent"]["stage"] == "plan"
    return "needPerson" not in kinds


def plan_waits_for(output):
    """Why the plan `output` that a run posted waits for a person without automatic approval,
    as the engine's attention names it: `approval`, of a plan that is too large, finds no
    change or is clean, `handMadeChange` or `question`. A plan whose questions name no
    question still asks, as NexKit 6 reads it. Raises `Refused`."""
    data = plan_record(output)
    found = plan_output({}, data, {})
    if output.get("questions") and not found["questions"]:
        found["questions"] = [{"question": "", "options": [], "recommended": 0, "closes": 0}]
    doc = _planning(_document({"number": 0}, {}), 0, False)
    result = {**AGENT, "output": found}
    event = _event("actionFinished", 0, None, "plan", action=first_state.RUN_ID, **_NO_CHANGE)
    answer = step(doc, {**event, "result": result}, {})
    return (answer["state"]["attention"] or {}).get("reason")


_NO_CHANGE = {"changeVersion": None}


# -- a blocked round ------------------------------------------------------------------


def asked_of(run):
    """The questions of the blocked round whose marker is `run`, as its agent named them:
    options by their numbers from 1, as the round's result gives them."""
    asked = []
    for item in questions(run.get("questions")):
        options = item["options"]
        replans = replanning_options(item)
        asked.append(
            {
                "question": item["question"],
                "options": options,
                "recommended": _number(options, recommended_option(item)),
                "closes": _number(options, closing_option(item)),
                "replans": [_number(options, option) for option in replans],
            }
        )
    return asked


def round_output(run):
    """The result of the implement round that stopped as blocked, whose marker is `run`, for
    what the engine decides from: its questions, the protected paths its change needs, and
    its finding that no change is needed."""
    return {
        "status": "blocked",
        "summary": "",
        "blocker": "",
        "questions": asked_of(run),
        "checks_run": [],
        "protected_paths": list(run.get("protected_paths") or []),
        "changes": [],
        "testing": "",
        "outside_plan": [],
        "reviewer_notes": [],
        "no_change": run.get("no_change") or "",
    }


def _working(doc, again):
    """`doc` while its `/nexkit go` runs, which the engine waits for. `again` says that
    autopilot's answers started this round and no person commented since
    (`autopilot.asked_again`)."""
    doc["state"] = "working"
    doc["pending"] = [first_state.agent_run("implement", None)]
    doc["asked"] += ["answers"] * again
    if doc["plan"]:
        doc["plan"]["approved"] = True
    return doc


def _asks(doc, comment, run):
    """`doc` while the questions of the blocked round whose run comment is `comment` wait:
    a comment that came before NexKit 6's next sweep is taken on them as they wait."""
    ref = str(comment["id"])
    items = [
        {**item, "replans": sorted(set(item["replans"]) - {0, item["closes"]})}
        for item in asked_of(run)
    ]
    found = bool(run.get("no_change"))
    doc["state"] = "awaitingApproval"
    doc["questions"] = {"ref": ref, "stage": "implement", "items": items, "noChange": found}
    doc["attention"] = {"reason": "question", "text": "The round has questions.", "ref": ref}
    if doc["plan"]:
        doc["plan"]["approved"] = True
    return doc


def _plan_doc(issue, record, comments):
    plan = latest_plan(comments)
    return _document(issue, record, plan, plan_data(plan)) if plan else _document(issue, record)


def after_go(gh, cfg, issue, comment, run, record, start, comments):
    """The step after the `/nexkit go` of `issue` whose run comment `comment`, with the
    marker `run`, says it stopped as blocked with questions or protected paths
    (`autopilot.engine_round`), as the engine decides it: a writer's comment since that
    picks the option that only closes the issue closes it; otherwise the round's result,
    then a collaborator's comment since, or the close of an issue that blocked it after the
    round started (`autopilot._since_stop`). `record` is autopilot's record and `start` what
    a command that the step starts begins from. Raises `Refused`."""
    number = issue["number"]
    said = autopilot._commented_after(comments, comment)
    picked = _picked(said[-1].get("body"), run) if said else None
    by = ((said[-1].get("user") or {}).get("login") or "") if picked else ""
    view = _RoundView(issue, run, start, said, picked)
    if picked and by and gh.can_write(by):
        # Closing is a write: a collaborator who may only read comments on the questions.
        doc = _asks(_plan_doc(issue, record, comments), comment, run)
        answers = [{"question": picked[0], "option": picked[1]}]
        asked = doc["attention"]["ref"]
        fields = {"by": _person(by, "writer"), "answers": answers, "note": "", "asked": asked}
        at, name = said[-1].get("created_at"), f"answer-{said[-1]['id']}"
        event = _event("answered", number, at, name, **fields)
        return view.carry(step(doc, event, policies(cfg))["actions"])
    doc = _working(_plan_doc(issue, record, comments), autopilot.asked_again(comments))
    events = [_round_result(number, comment, run)]
    if said:
        # A collaborator, as `autopilot._commented_after` reads them.
        login = (said[-1].get("user") or {}).get("login") or ""
        text = {"name": "comment", "ref": str(said[-1]["id"])}
        at, name = said[-1].get("created_at"), f"comment-{said[-1]['id']}"
        events.append(_event("commented", number, at, name, by=_person(login, "reader"), text=text))
    else:
        closed = _closed_since(gh, issue, comment)
        view.closed = list(closed)
        events += [
            _event(
                "blockerClosed",
                number,
                None,
                f"blocker-{found}",
                blocker=str(found),
                completed=done,
            )
            for found, done in closed.items()
        ]
    actions = []
    for event in events:
        found = step(doc, event, policies(cfg))
        doc = found["state"]
        if found["actions"]:
            actions = found["actions"]
            view.by_event = event["kind"]
        if any(action["kind"] in ("runAgent", "close", "createParts") for action in actions):
            break
    return view.carry(actions)


def _round_result(number, comment, run):
    """The end of the `/nexkit go` whose run comment `comment` holds the marker `run`."""
    return _event(
        "actionFinished",
        number,
        comment.get("created_at"),
        f"go-{comment.get('id')}",
        action=first_state.RUN_ID,
        changeVersion=None,
        result={**AGENT, "output": round_output(run)},
    )


def _closed_since(gh, issue, comment):
    """{number: completed} of the issues that block `issue` and closed after the round of the
    run comment `comment` started, as `autopilot._since_stop` reads them."""
    started = autopilot.parse_iso(comment.get("created_at"))
    return {
        blocker["number"]: blocker.get("state_reason") in (None, "completed")
        for blocker in sorted(gh.blocked_by(issue["number"]), key=lambda found: found["number"])
        if blocker.get("state") == "closed" and autopilot._later(blocker.get("closed_at"), started)
    }


class _RoundView:
    """What autopilot does for one answer of the engine after a blocked `/nexkit go`, in
    NexKit 6's words."""

    def __init__(self, issue, run, start, said, picked):
        self.issue, self.run, self.start, self.said, self.picked = issue, run, start, said, picked
        self.closed, self.by_event = [], None

    def step(self, state, reason="", **fields):
        return autopilot._step(self.issue, state, reason, **fields)

    def carry(self, actions):
        kinds = {action["kind"]: action for action in actions}
        run = self.run
        if "close" in kinds and kinds["close"]["outcome"] == "notPlanned":
            by = (self.said[-1].get("user") or {}).get("login") or ""
            reason = f"@{by} answered with the option that only closes the issue"
            fields = {"answer": self.picked[2], "by": by, "asked": run.get("url") or ""}
            return self.step("close", reason, **fields)
        if "close" in kinds:
            # The recommended answer only closes the issue: another round would find the same.
            reason = "the last round found no change is needed and recommends closing the issue"
            return self.step("close", reason, finding=run["no_change"], run_url=run.get("url"))
        if "runAgent" in kinds:
            shown = any(a["kind"] == "show" and a["what"] == "answers" for a in actions)
            return self.started(kinds["runAgent"]["stage"], shown)
        if "needPerson" in kinds:
            return self.waits(kinds["needPerson"]["reason"])
        raise Refused("The engine answered nothing to carry out.")

    def started(self, stage, shown):
        start, number = self.start, self.issue["number"]
        found = [
            {"question": item["question"], "answer": item["recommended"]}
            for item in questions(self.run.get("questions"))
        ]
        if shown and stage == "plan":
            note = f"{autopilot.REPLAN_NOTE}\n\n{autopilot.answer_note(found)}"
            fields = {**start, "target": number, "note": note}
            reason = (
                "autopilot answers the blocked round with an option that a new plan carries out"
            )
            return self.step("start", reason, command="plan", answers=found, replan=True, **fields)
        if shown:
            fields = {**start, "target": number, "note": autopilot.answer_note(found)}
            reason = "autopilot answers the questions of the blocked round"
            return self.step("start", reason, command="go", answers=found, **fields)
        if stage != "plan":
            raise Refused(f"The engine starts {stage} after a blocked round with no answers.")
        if self.by_event == "commented":
            reason = "collaborators commented after the stopped `/nexkit go`"
        else:
            listed = ", ".join(f"#{n}" for n in self.closed)
            reason = f"{listed}, which blocked it, closed after the stopped `/nexkit go`"
        reason += ", so autopilot plans again"
        return self.step("start", reason, command="plan", **start)

    def waits(self, reason):
        if reason == "handMadeChange":
            text = autopilot.protected_reason(self.run["protected_paths"])
        elif reason == "askedAgain":
            text = autopilot.REPEATED
        elif reason == "closeRecommended":
            text = autopilot.CLOSE_UNFOUNDED
        elif reason == "question":
            text = self.run.get("attention") or autopilot.failed_reason("go")
        else:
            raise Refused(f"The engine waits for a person for {reason!r} after a blocked round.")
        return self.step("waits", text)


def _carries_on(actions):
    """Whether the engine's `actions` after a stopped round start a run or close the issue.
    Any wait for a person, the recommended answer that only closes the issue included, needs
    that person now, so `report` mentions them and the sweep does not. NexKit 6 said
    autopilot carries on there and left the mention to the sweep; this differs on purpose
    (#346)."""
    return any(action["kind"] in ("runAgent", "close") for action in actions)


def round_carried_on(gh, cfg, issue, decision, agent):
    """Whether autopilot carries on after the `/nexkit go` that a run of `decision` ran and
    that stopped as blocked, with the agent's result `agent`, as the engine decides what
    follows it, so that `report` mentions nobody. A wait for a person, the closing answer
    included, makes `report` mention them. Raises `Refused`."""
    from .report import _autopilot_fields

    comments = gh.comments(issue["number"])
    again = autopilot.asked_again(comments)
    found = _autopilot_fields(agent)
    run = {**found, "protected_paths": ((agent.get("output") or {}).get("protected_paths") or [])}
    doc = _working(_document(issue, autopilot.find_record(comments)[1]), again)
    event = _event(
        "actionFinished",
        issue["number"],
        None,
        f"run-{decision.get('run_id') or issue['number']}",
        action=first_state.RUN_ID,
        changeVersion=None,
        result={**AGENT, "output": round_output(run)},
    )
    return _carries_on(step(doc, event, policies(cfg))["actions"])


# -- a fix round that stopped, and a round that ran out of time --------------------------


def fix_output(run):
    """The result of the fix round that stopped as blocked, whose marker is `run`, for what the
    engine decides from: its questions, its conflicts among them, and the protected paths its
    change needs."""
    return {
        "status": "blocked",
        "summary": "",
        "blocker": "",
        "questions": asked_of(run),
        "checks_run": [],
        "protected_paths": list(run.get("protected_paths") or []),
        "cause": "",
        "cause_outside": False,
        "open_conflicts": [],
    }


def _split_place(gh, issue, record):
    """(depth, asked) of `issue` for a round that ran out of time: how deep a part it is, and
    whether an issue above it asked for a split, which a part inherits
    (`autopilot.split_refused`). Not read once the issue itself asked, as NexKit 6 reads it."""
    if record.get("split_asked"):
        return 0, False
    above = autopilot.lineage(gh, issue)
    asked = any(
        autopilot.find_record(gh.comments(parent["number"]))[1].get("split_asked")
        for parent in above
    )
    return autopilot.depth(issue, above), asked


def _round_doc(issue, record, again, pull=None, head=None, place=(0, False)):
    """`issue`'s document while its `/nexkit go`, or with `pull` a fix round of that pull
    request on `head`, runs, which the engine waits for. `again` says that autopilot's
    answers started it and no person commented since (`autopilot.asked_again`); `place` is
    what `_split_place` reads."""
    change = {"id": str(pull), "version": str(head or pull)} if pull else None
    doc = _document(issue, record, change=change)
    doc["state"] = "fixing" if pull else "working"
    stage = "fix" if pull else "implement"
    doc["pending"] = [first_state.agent_run(stage, (change or {}).get("version"))]
    doc["asked"] += ["answers"] * again
    doc["depth"] = place[0]
    if place[1] and "split" not in doc["asked"]:
        doc["asked"].append("split")
    return doc


def _stop_event(number, name, doc, output, timed_out):
    version = (doc["change"] or {}).get("version")
    result = {**AGENT, "timedOut": timed_out, "output": None if timed_out else output}
    return _event(
        "actionFinished",
        number,
        None,
        name,
        action=first_state.RUN_ID,
        changeVersion=version,
        result=result,
    )


def stopped(gh, cfg, issue, run, record, start, comments, pull=None, pull_comments=()):
    """The step after a round that stopped, whose marker is `run`, as the engine decides it:
    with `pull`, a fix round of that pull request that stopped as blocked with questions or
    protected paths (`autopilot.engine_round`); or a fix round or a `/nexkit go` that ran out
    of time, after which autopilot asks once for a split. `comments` are the issue's,
    `pull_comments` the pull request's, `record` autopilot's record and `start` what a
    command that the step starts begins from. Raises `Refused`."""
    number = issue["number"]
    timed_out = bool(run.get("timed_out"))
    place = _split_place(gh, issue, record) if timed_out else (0, False)
    again = False
    if not timed_out:
        again = autopilot.asked_again(pull_comments, comments)
    doc = _round_doc(issue, record, again, pull, run.get("head"), place)
    event = _stop_event(number, f"stop-{pull or number}", doc, fix_output(run), timed_out)
    actions = step(doc, event, policies(cfg))["actions"]
    kinds = {action["kind"]: action for action in actions}
    found = [
        {"question": item["question"], "answer": item["recommended"]}
        for item in questions(run.get("questions"))
    ]
    if timed_out and (kinds.get("runAgent") or {}).get("stage") == "plan":
        fields = {**start, "note": autopilot.SPLIT_NOTE, "remember": {"split_asked": True}}
        reason = "the agent ran out of time, so autopilot asks for a split"
        return autopilot._step(issue, "start", reason, command="plan", **fields)
    if not timed_out and (kinds.get("runAgent") or {}).get("stage") == "fix":
        fields = {**start, "target": pull, "note": autopilot.answer_note(found)}
        reason = "autopilot answers the questions of the blocked round"
        return autopilot._step(issue, "start", reason, command="fix", answers=found, **fields)
    reason = (kinds.get("needPerson") or {}).get("reason")
    if pull:
        failed = autopilot.person(
            f"read the last round's comment on #{pull} for why, then post its command again",
            f"the last round of #{pull} failed",
        )
    else:
        failed = autopilot.failed_reason("go")
    if timed_out and reason == "failed":
        refused = None if record.get("split_asked") else autopilot.split_refused(gh, issue)
        text = refused or run.get("attention") or failed
    elif not timed_out and reason in ROUND_WAITS:
        text = ROUND_WAITS[reason](run) or run.get("attention") or failed
    else:
        raise Refused(f"The engine answers {sorted(kinds)} after a stopped round.")
    return autopilot._step(issue, "waits", text)


# What a sweep says of a fix round's questions that wait for a person, by the engine's reason,
# in NexKit 6's words; "" for the round's own attention.
ROUND_WAITS = {
    "handMadeChange": lambda run: autopilot.protected_reason(run["protected_paths"]),
    "askedAgain": lambda run: autopilot.REPEATED,
    "closeRecommended": lambda run: autopilot.CLOSE_UNFOUNDED,
    "replanRecommended": lambda run: autopilot.REPLAN_IN_PULL,
    "question": lambda run: "",
    "blocked": lambda run: "",
}


def stop_carried_on(gh, cfg, issue, decision, agent):
    """Whether autopilot carries on after a fix round of `decision` that stopped as blocked,
    or an implement or fix round that ran out of time, with the agent's result `agent`, as
    the engine decides what follows it, so that `report` mentions nobody. A wait for a person,
    the closing answer included, makes `report` mention them. Raises `Refused`."""
    from .report import _autopilot_fields

    comments = gh.comments(issue["number"])
    record = autopilot.find_record(comments)[1]
    timed_out = bool(agent.get("timed_out"))
    pr = decision.get("pr") if decision["action"] == "fix" else None
    again = False
    if not timed_out:
        again = autopilot.asked_again(gh.comments(pr) if pr else comments, comments if pr else ())
    place = _split_place(gh, issue, record) if timed_out else (0, False)
    found = _autopilot_fields(agent)
    run = {**found, "protected_paths": ((agent.get("output") or {}).get("protected_paths") or [])}
    doc = _round_doc(issue, record, again, pr, decision.get("head"), place)
    output = fix_output(run) if pr else round_output(run)
    name = f"run-{decision.get('run_id') or issue['number']}"
    actions = step(doc, _stop_event(issue["number"], name, doc, output, timed_out), policies(cfg))
    return _carries_on(actions["actions"])


# -- a person's command ----------------------------------------------------------------


def closing_command(gh, decision, comments):
    """A writer's `/nexkit plan` or `/nexkit go` on an issue whose questions NexKit asked
    last, those of its latest plan or of a `/nexkit go` that stopped as blocked
    (`state.asked_last`): when its note picks the option that only closes the issue and the
    engine drops the issue for it, the `maintain` job closes the issue as not planned, with
    no agent run. Otherwise the command goes to its round as it is."""
    number = decision["issue"]
    asked = asked_last(comments)
    if asked is None:
        return decision
    runs = [(comment, run) for comment, run in run_comments(comments) if "command" in run]
    comment, run = runs[-1]
    plan = data = None
    if run.get("command") == "plan":
        plan = latest_plan(comments)
        data = plan_data(plan)
    picked = _picked(decision["note"], data or run)
    if not picked:
        return decision
    pulls = gh.open_pulls(decision["branch"])
    change = {"id": str(pulls[0]["number"]), "version": pulls[0]["head"]["sha"]} if pulls else None
    if plan:
        doc = _waiting(_document({"number": number}, {}, plan, data, change))
    else:
        doc = _asks(_document({"number": number}, {}, change=change), comment, run)
    answers = [{"question": picked[0], "option": picked[1]}]
    fields = {"answers": answers, "note": "", "asked": doc["attention"]["ref"]}
    by = _person(decision.get("actor"), "writer")
    event = _event("answered", number, None, f"command-{number}", by=by, **fields)
    try:
        found = step(doc, event, {})
    except Refused:
        return decision
    if not any(action["kind"] == "close" for action in found["actions"]):
        # A pull request that is open stays with its issue: a person closes both.
        return decision
    return {
        "action": "maintain",
        "task": "close_answer",
        "issue": number,
        "answer": picked[2],
        "asked": asked[0],
    }


# -- what follows a round's checks and review ------------------------------------------

# What `report` does after a round of a NexKit pull request, by what the engine answers: the
# round is ready for a person, or merges, or a hold keeps its merge back; an automatic fix
# round starts, waits for the base branch's failure to end, or a hold keeps it back; or a
# person decides what still fails.
ROUND_ENDS = (
    "ready",
    "merge",
    "held_merge",
    "auto_fix",
    "ci_waiting",
    "held_fix",
    "needs_human",
    "recheck_failed",
)


def after_round(
    cfg,
    decision,
    pr,
    head,
    auto_fixes,
    checks,
    review,
    recheck,
    wait,
    label,
    before=None,
    seen=None,
):
    """What follows a round of the NexKit pull request `pr` whose checks and review ended on
    `head`, as the engine decides it: one of `ROUND_ENDS`. `auto_fixes` is how many automatic
    fix rounds the pull request used, `checks` the checks' results (None: they did not run),
    `review` the review NexKit acts on (`report.apply_criteria`), `recheck` whether the round
    is a fix round that changed nothing and checked the same head again, `wait` what a fix
    round would wait for while the base branch has the same failure (`autopilot.ci_wait`),
    `label` the label that holds the issue, and `before` the checks of the round before, as
    the state comment recorded them. `seen`, a dict, gets the checks that failed the same
    way before and after a fix round as `repeated` (`round_ended`). Raises `Refused`."""
    end, repeated = round_ended(
        cfg, decision, pr, head, auto_fixes, checks, before, ("done", review), recheck, wait, label
    )
    if seen is not None:
        seen["repeated"] = repeated
    return end


# What `report` does after a round's review that is not a verdict on a NexKit pull request,
# by what the engine answers: a person's own pull request reviewed, a review paused at a
# usage limit or failed, or one that the host runs again after a transient error.
REVIEW_ENDS = ("person_reviewed", "paused", "review_failed", "again")


def round_ended(
    cfg,
    decision,
    pr,
    head,
    auto_fixes,
    checks,
    before,
    review,
    recheck=False,
    wait=None,
    label=None,
):
    """What follows the checks and the review of the round of `decision` on the pull request
    `pr`, which ended on `head`, as the engine decides it: (end, repeated). `review` is
    (`done`, the review NexKit acts on), (`paused`, the time the host runs it again after
    its usage limit resets, or None when it does not), (`failed`, why), or (`again`, None)
    for a review that the host runs again after a transient error, as its own retries do,
    which the engine does not see end. `before` are the checks of the round before, as the
    state comment recorded them (`checks.recorded`). The end is one of `ROUND_ENDS` after a
    verdict on a NexKit pull request, else one of `REVIEW_ENDS`; `repeated` names the checks
    that failed the same way before and after a fix round, for the next automatic fix round
    to diagnose. Raises `Refused`."""
    number = decision["issue"]
    found = policies(cfg)
    person = bool(decision.get("person"))
    doc = first_state.empty(str(number))
    if person:
        doc["itemKind"] = "reviewOnly"
    doc["state"] = "checking"
    doc["change"] = {"id": str(pr), "version": head}
    doc["hold"] = bool(label)
    doc["policies"] = found
    doc["rounds"]["counted"] = auto_fixes
    if recheck:
        doc["rounds"]["recheck"] = head
    failing = _failing(before)
    if failing:
        doc["rounds"]["failing"] = failing
    if decision["action"] == "fix":
        # The checks of a fix round compare with the round's before it.
        doc["rounds"]["fixed"] = head
    run = {"id": first_state.RUN_ID, "next": None, "kind": "runChecks", "changeVersion": head}
    doc["pending"] = [run]
    results = {"checks": _checks(checks), "baseFailure": str(wait["bug"]) if wait else None}
    event = _event(
        "actionFinished",
        number,
        None,
        f"checks-{pr}-{head}",
        action=run["id"],
        changeVersion=head,
        result=results,
    )
    answer = step(doc, event, found)
    review_run = [a for a in answer["actions"] if a["kind"] == "runAgent"]
    if not review_run or review_run[0]["stage"] != "review":
        raise Refused("The engine runs no review after a round's checks.")
    kind, given = review
    if kind == "again":
        return "again", _repeated(answer)
    if kind == "done":
        result = {**AGENT, "output": _review_output(given)}
    elif kind == "paused":
        message = "The review stopped at its usage limit."
        result = {**AGENT, "output": None, "limit": {"message": message, "reset": _time(given)}}
    else:
        result = {**AGENT, "output": None, "error": str(given or "The review failed.")}
    event = _event(
        "actionFinished",
        number,
        None,
        f"review-{pr}-{head}",
        action=review_run[0]["id"],
        changeVersion=head,
        result=result,
    )
    answer = step(answer["state"], event, found)
    if kind == "done" and not person:
        return _round_end(answer, recheck), _repeated(answer)
    return _review_end(answer, kind, person), _repeated(answer)


def _failing(recorded):
    """The engine's `rounds.failing` for checks as the state comment recorded them: each
    failing check by the digest of its name and signature (`checks.failing_signatures`)."""
    return [
        failure_key(name, found or name) for name, found in failing_signatures(recorded).items()
    ][: MOST["failing"]]


def _repeated(answer):
    return list(answer["state"]["rounds"].get("repeated") or [])


def _time(at):
    """`at` as the protocol writes a time, or None when it is none."""
    found = autopilot.parse_iso(at) if at else None
    return found.strftime("%Y-%m-%dT%H:%M:%SZ") if found else None


def _review_end(answer, kind, person):
    """`REVIEW_ENDS`' name for the engine's `answer` after a review that is not a verdict on a
    NexKit pull request."""
    state, actions = answer["state"], answer["actions"]
    kinds = {action["kind"]: action for action in actions}
    reason = (kinds.get("needPerson") or {}).get("reason")
    if kind == "done" and person and state["state"] == "ready" and "show" in kinds:
        return "person_reviewed"
    if kind == "paused" and (reason == "failed" or "wait" in kinds):
        return "paused"
    if kind == "failed" and reason == "failed":
        return "review_failed"
    raise Refused(f"The engine answers {sorted(kinds)} after a review, which the host cannot do.")


def _checks(checks):
    """The checks' results as `runChecks` gives them, each failing one with its signature
    (`checks.failing_signatures`); checks that did not run fail as one."""
    if checks is None:
        return [_check("checks", False)]
    found = []
    for check in checks:
        # Named as `checks.failing_signatures` names it, which the checks before compare by.
        name, passed = str(check.get("name")), bool(check.get("passed"))
        sign = None if passed else failing_signatures([check]).get(name)
        found.append(_check(name, passed, sign or None))
    return found


def _check(name, passed, sign=None):
    return {
        "name": name,
        "passed": passed,
        "skipped": False,
        "signature": sign,
        "knownBaseFailure": False,
    }


def _review_output(review):
    """The review NexKit acts on as the review stage gives it, for what the engine decides
    from: its verdict, each criterion's status, its findings and the checklist items not met
    that a person decides (`report.to_decide`)."""
    from .report import criterion_status, to_decide

    criteria = [
        {
            "criterion": str(item.get("criterion") or ""),
            "status": criterion_status(item),
            "test": "",
            "evidence": "",
        }
        for item in review.get("criteria") or []
    ]
    findings = [
        {
            "severity": "blocking" if item.get("severity") == "blocking" else "suggestion",
            "file": "",
            "line": 0,
            "body": str(item.get("body") or ""),
        }
        for item in review.get("findings") or []
    ]
    checklist = [
        {"number": number, "answer": "not_met", "evidence": ""}
        for number, _ in enumerate(to_decide(review), 1)
    ]
    return {
        "verdict": review["verdict"],
        "summary": "",
        "criteria": criteria,
        "findings": findings,
        "previous_findings": [],
        "checklist": checklist,
    }


def _round_end(answer, recheck):
    """`ROUND_ENDS`' name for the engine's `answer` after a round's review."""
    state, actions = answer["state"], answer["actions"]
    kinds = {action["kind"]: action for action in actions}
    if "accept" in kinds:
        return "merge"
    if state["state"] == "accepting" and state["hold"]:
        return "held_merge"
    if "runAgent" in kinds and kinds["runAgent"]["stage"] == "fix":
        return "auto_fix"
    if state["state"] == "fixing" and any(w["for"] == "baseFailure" for w in state["waits"]):
        return "ci_waiting"
    if state["state"] == "fixing" and state["hold"]:
        return "held_fix"
    reason = (kinds.get("needPerson") or {}).get("reason")
    if reason == "roundsUsedUp":
        return "recheck_failed" if recheck else "needs_human"
    if reason in ("accept", "unverifiableCriterion", "personChecklistItem"):
        return "ready"
    raise Refused(
        f"The engine answers {sorted(kinds)} after a round, which the host does not carry out."
    )


# -- a round whose change conflicts, and a hold on its conflict round ----------------------


def conflicting(cfg, decision, pr, head, held, used):
    """What follows a round of `decision` whose commit `head` on the pull request `pr`
    conflicts with its base branch's tip, as `publish` found, so that its checks and review
    did not run, as the engine decides it (`report._skipped_conflicting`): `round`, the
    round that resolves the conflict; `held` while a label `held` the issue, which keeps it
    back; or `used_up` when its `used` conflict rounds leave none, or when
    `conflicts.auto_resolve` is off. `publish` finds a conflict only with it on, so that
    answer comes only when the configuration changed between the two jobs, where NexKit 6
    started the round all the same. Raises `Refused`."""
    number = decision["issue"]
    stage = "fix" if decision["action"] == "fix" else "implement"
    before = {"id": str(pr), "version": str(decision.get("head"))} if stage == "fix" else None
    doc = _pull_doc(cfg, number, pr, None, RUNS_OF[stage], 0)
    doc.update(change=before, observed=None, hold=bool(held))
    doc["rounds"]["conflicts"] = max(int(used or 0), 0)
    doc["pending"] = [{"id": first_state.RUN_ID, "next": None, "kind": "publish", "stage": stage}]
    seen = {"changeId": str(pr), "version": head, "conflicts": True}
    seen.update(merged=False, empty=False)
    result = {"change": {"id": str(pr), "version": head}, "refused": None, "observed": seen}
    fields = {"action": first_state.RUN_ID, "changeVersion": None, "result": result}
    event = _event("actionFinished", number, None, f"publish-{pr}-{head}", **fields)
    answer = step(doc, event, policies(cfg))
    state, kinds = answer["state"], {action["kind"]: action for action in answer["actions"]}
    if (kinds.get("runAgent") or {}).get("stage") == "fix":
        return "round"
    if state["state"] == "checking" and state["hold"] and not state["pending"]:
        return "held"
    if (kinds.get("needPerson") or {}).get("reason") == "acceptRefused":
        return "used_up"
    raise Refused(f"The engine answers {sorted(kinds)} after a conflict, which the host cannot do.")


# The state of the item while a round's agent runs, by its stage.
RUNS_OF = {"implement": "working", "fix": "fixing"}


def conflicts_released(cfg, issue, pull, run, head, conflicts, used):
    """What follows once no label holds `issue` whose pull request's round, the marker `run`,
    held back the round that resolves its conflict, as the engine decides it
    (`autopilot._conflicts_released`): `review` of a head that moved, which the next round
    checks for a conflict, or of one that no longer `conflicts`; `round`, while its `used`
    conflict rounds leave one; or `waits` for a person, with `conflicts.auto_resolve` off or
    no conflict round left. Raises `Refused`."""
    version = str(run.get("head") or pull)
    doc = _pull_doc(None, issue, pull, version, "checking", 0)
    doc["hold"] = True
    doc["observed"]["conflicts"] = True
    doc["rounds"]["conflicts"] = max(int(used or 0), 0)
    moved = head != run.get("head")
    seen = {"changeId": str(pull), "version": str(head or "none") if moved else version}
    seen.update(conflicts=bool(conflicts) and not moved, merged=False, empty=False)
    events = [
        _event("changeObserved", issue, None, f"head-{pull}-{seen['version']}", **seen),
        _event("released", issue, None, f"released-{pull}-{version}"),
    ]
    _, actions = _answers(doc, events, policies(cfg))
    kinds = {action["kind"]: action for action in actions}
    if "runChecks" in kinds:
        return "review"
    if (kinds.get("runAgent") or {}).get("stage") == "fix":
        return "round"
    if (kinds.get("needPerson") or {}).get("reason") == "acceptRefused":
        return "waits"
    raise Refused(f"The engine answers {sorted(kinds)} after a held conflict, which it cannot do.")


# -- the checks a fix round diagnoses ------------------------------------------------------


def diagnosis(issue, pr, head, automatic, repeated):
    """The checks that the fix round `route` starts on the pull request `pr` looks into first,
    as the engine runs that round: the `repeated` ones that failed the same way before and
    after the fix round before it, which the state comment keeps, for an `automatic` round;
    none for a person's round or a conflict round, which NexKit 6's markers say it is.
    Raises `Refused`."""
    if not repeated:
        return []
    doc = _pull_doc(None, issue, pr, head, "fixing", 1)
    doc["rounds"]["repeated"] = list(repeated)[: MOST["repeated"]]
    if automatic:
        doc["rounds"]["automatic"] = True
    # The round runs as the engine starts a round it issued again, as after a wait.
    doc["waits"] = [{"id": "w1.1", "for": "time", "until": NO_TIME, "on": None}]
    event = _event("tick", issue, None, f"route-{pr}-{head}", wait="w1.1")
    _, actions = _answers(doc, [event], {})
    runs = [action for action in actions if action["kind"] == "runAgent"]
    if not runs or runs[0]["stage"] != "fix":
        raise Refused("The engine runs no fix round for the command.")
    return list(runs[0].get("diagnose") or [])


# -- the end of a round's wait, and which rounds count ----------------------------------


def _pull_doc(cfg, issue, pr, head, state, auto_fixes):
    """The document of `issue` whose NexKit pull request `pr` is in `state` on `head`, with
    `auto_fixes` automatic fix rounds counted, as the state comment of `pr` keeps them."""
    doc = first_state.empty(str(issue))
    version = str(head or pr)
    doc["state"] = state
    doc["change"] = {"id": str(pr), "version": version}
    doc["observed"] = {
        "changeId": str(pr),
        "version": version,
        "conflicts": False,
        "merged": False,
        "empty": False,
    }
    doc["rounds"]["counted"] = max(int(auto_fixes or 0), 0)
    doc["rounds"]["started"] = 1
    if cfg and state in ("fixing", "checking", "reviewing"):
        doc["policies"] = policies(cfg)
    return doc


def _answers(doc, events, found_policies):
    """The engine's actions for `events` sent in order from `doc`, and the state after them."""
    actions = []
    for event in events:
        found = step(doc, event, found_policies)
        doc, actions = found["state"], [*actions, *found["actions"]]
    return doc, actions


def _wait_end(doc, actions):
    """What follows the end of a round's wait, by the engine's answer: `review` of a head
    pushed meanwhile, `merge`, the automatic `fix` round, counted, or `used_up` when it has
    no round left."""
    kinds = {action["kind"]: action for action in actions}
    if "runChecks" in kinds:
        return "review"
    if "accept" in kinds:
        return "merge"
    if (kinds.get("runAgent") or {}).get("stage") == "fix":
        return "fix"
    if (kinds.get("needPerson") or {}).get("reason") == "roundsUsedUp":
        return "used_up"
    raise Refused(f"The engine answers {sorted(kinds)} after a wait, which the host cannot do.")


def released(cfg, issue, pull, run, head, auto_fixes):
    """What follows once no label holds `issue` whose pull request's round, the marker `run`,
    held back its merge or its automatic fix round, as the engine decides it
    (`autopilot._released`): one of `_wait_end`'s. `head` is the pull request's head now. A
    fix round held back is counted only now, while `fix.max_auto_rounds` has one left as it
    reads now. `_released` reads only the head, so a conflict is the next round's to find.
    Raises `Refused`."""
    back = "accepting" if run.get("held_back") == "merge" else "fixing"
    doc = _pull_doc(cfg, issue, pull, run.get("head"), back, auto_fixes)
    doc["hold"] = True
    if back == "fixing":
        doc["rounds"]["due"] = True
    version = doc["change"]["version"]
    events = []
    if head != run.get("head"):
        fields = {"changeId": str(pull), "version": str(head or "none")}
        fields.update(conflicts=False, merged=False, empty=False)
        events.append(_event("changeObserved", issue, None, f"head-{pull}-{head}", **fields))
    events.append(_event("released", issue, None, f"released-{pull}-{version}"))
    return _wait_end(*_answers(doc, events, policies(cfg)))


def base_passes(cfg, issue, pull, run, auto_fixes):
    """What follows the end of the wait of `issue`'s pull request `pull`, whose round, the
    marker `run`, waits while the base branch has the same failure (`ci_waiting`), once its
    bug issue closed or the base passes again, which the host sends as the end of that bug
    issue (`autopilot._ci_step`): `fix`, counted now, or `used_up`. Raises `Refused`."""
    wait = run.get("ci_wait") or {}
    bug = str(wait.get("bug") or "none")
    doc = _pull_doc(cfg, issue, pull, run.get("head"), "fixing", auto_fixes)
    doc["rounds"]["due"] = True
    doc["waits"] = [{"id": "w1.1", "for": "baseFailure", "until": None, "on": bug}]
    event = _event("blockerClosed", issue, None, f"base-{pull}-{bug}", blocker=bug, completed=True)
    return _wait_end(*_answers(doc, [event], policies(cfg)))


def required_failed(cfg, issue, pr, head, auto_fixes, wait):
    """What follows required checks that failed on `head` of the NexKit pull request `pr` at
    its merge, as the engine decides it (`report._required_failed`): `auto_fix`, counted,
    `ci_waiting` while they fail as the base branch does, which `wait` names
    (`autopilot.ci_wait`), or `needs_human` when no automatic fix round is left. NexKit 6
    tries a merge only when no label holds the issue. Raises `Refused`."""
    doc = _pull_doc(cfg, issue, pr, head, "accepting", auto_fixes)
    version = doc["change"]["version"]
    action = {"id": first_state.RUN_ID, "next": None, "kind": "accept"}
    doc["pending"] = [{**action, "changeVersion": version, "by": "engine"}]
    result = {
        "accepted": False,
        "refusal": "checkFailed",
        "text": "A required check failed.",
        "baseFailure": str(wait["bug"]) if wait else None,
    }
    fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
    event = _event("actionFinished", issue, None, f"required-{pr}-{version}", **fields)
    answer = step(doc, event, policies(cfg))
    state, kinds = answer["state"], {action["kind"]: action for action in answer["actions"]}
    if (kinds.get("runAgent") or {}).get("stage") == "fix":
        return "auto_fix"
    if any(wait["for"] == "baseFailure" for wait in state["waits"]):
        return "ci_waiting"
    if (kinds.get("needPerson") or {}).get("reason") == "roundsUsedUp":
        return "needs_human"
    raise Refused(f"The engine answers {sorted(kinds)} after a required check failed.")


def stopped_counts(issue, pr, head, auto_fixes, automatic, by_host, checking=False):
    """How many automatic fix rounds of the NexKit pull request `pr` count after its round on
    `head` stopped, as the engine decides it: an automatic one (`automatic`) that the host
    stopped before its review ended (`by_host`), such as for an agent it may not run, a change
    already on the base branch, or checks that the base branch's move stopped (`checking`),
    gives its count back; a round whose agent stopped by itself keeps it
    (`report._stop_round`, `maintain._give_back`). The host's own stops, and what follows
    them, stay NexKit 6's, so the host reports one as `stopped`; no policy decides a stop.
    Raises `Refused`."""
    counted = max(int(auto_fixes or 0), 0)
    doc = _pull_doc(None, issue, pr, head, "checking" if checking else "fixing", counted)
    version = doc["change"]["version"]
    if checking:
        run = {"id": first_state.RUN_ID, "next": None, "kind": "runChecks"}
        doc["pending"] = [{**run, "changeVersion": version}]
    else:
        doc["pending"] = [first_state.agent_run("fix", version)]
    if automatic and counted:
        doc["rounds"]["automatic"] = True
    if by_host:
        text = "The host stopped the round before its review."
        event = _event("stopped", issue, None, f"stop-{pr}-{version}", outcome="other", text=text)
    else:
        result = {**AGENT, "changeVersion": version, "output": None, "error": "The round stopped."}
        fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
        event = _event("actionFinished", issue, None, f"stop-{pr}-{version}", **fields)
    return step(doc, event, {})["state"]["rounds"]["counted"]
