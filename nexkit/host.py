"""The GitHub host's calls of the engine's `step` (docs/engine.md#getting-there).

Decisions move into the engine a group at a time, and until the GitHub host runs only through
it, NexKit 6's code moves an item between the decisions that the engine holds. The host keeps no
state document for them: for each one, it builds the item's document from NexKit 6's markers, as
the import reads them (`first_state`), sends the event that GitHub shows, and carries out the
answer with NexKit 6's code, in NexKit 6's words.

So far the engine decides what follows a plan: its approval by autopilot, the answers to its
questions, once in a row, a writer's answer that only closes the issue, a plan that finds no
change or needs a hand-made change, splits and their rules, and a new plan after a
collaborator's comment. One stays NexKit 6's (`autopilot._replanned`): a plan that splits an
issue whose pull request's round ran out of time, since the engine never splits an item that has
a change (docs/engine.md#states).

An item whose document or event the engine refuses waits for a person, and no other item is
held up by it.
"""

from __future__ import annotations

import json

from . import autopilot, engine, first_state
from .protocol.size import LONGEST
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
    why = f"NexKit's engine refused to decide its next step: {autopilot.quoted(str(error), 300)}"
    return autopilot.person(REFUSED_DO, why)


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
    `text` picks among the questions of the plan record `data`, or None
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
            text = autopilot.CLOSE_UNFOUNDED
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
    as the engine decides what follows it, so that `report` mentions nobody. As in NexKit 6,
    a plan that finds no change carries on whatever else it says, and so do autopilot's
    answers whose recommended option only closes the issue, unless they were asked again:
    the sweep then waits for a person and mentions them itself. A plan that needs nobody is
    no run that would need a person. Raises `Refused`."""
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
    if "needPerson" in kinds:
        return kinds["needPerson"]["reason"] == "closeRecommended" and not again
    return True


# -- a person's command ----------------------------------------------------------------


def asked_by_plan(comments):
    """Whether what NexKit asked last on an issue, if anything, is its latest plan's
    questions, rather than a blocked round's (`state.asked_last`)."""
    runs = [run for _, run in run_comments(comments) if "command" in run]
    return bool(runs) and runs[-1].get("command") == "plan" and asked_last(comments) is not None


def closing_command(gh, decision, comments):
    """A writer's `/nexkit plan` or `/nexkit go` on an issue whose latest plan's questions
    NexKit asked last: when its note picks the option that only closes the issue and the
    engine drops the issue for it, the `maintain` job closes the issue as not planned, with
    no agent run. Otherwise the command goes to its round as it is."""
    number = decision["issue"]
    plan = latest_plan(comments)
    data = plan_data(plan)
    picked = _picked(decision["note"], data)
    if not picked:
        return decision
    pulls = gh.open_pulls(decision["branch"])
    change = {"id": str(pulls[0]["number"]), "version": pulls[0]["head"]["sha"]} if pulls else None
    doc = _waiting(_document({"number": number}, {}, plan, data, change))
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
        "asked": plan.get("html_url") or "",
    }
