"""The GitHub host's calls of the engine's `step` (docs/engine.md#getting-there).

Decisions move into the engine one at a time. Until all of them have, NexKit 6's code moves an
item between the steps that the engine decides, without the state document. So for each
decision the engine holds, the host builds the item's document from NexKit 6's markers, as the
first import does (`first_state`), at the point where that decision is taken and on the
revision stored on the item; sends the event that GitHub shows; and carries out what `step`
answers with NexKit 6's code. The sweep stores the answer on the item (`item_state`) once it
carried it out, and only when it changed what is stored.

The engine decides what follows a plan: its approval, the answers to its questions, once in a
row, a person's answer that only closes the issue, a plan that finds no change or needs a
hand-made change, splits and their limits, and a new plan after a collaborator's comment.
The words people read stay the host's: each reason and note is NexKit 6's own.
"""

from __future__ import annotations

import copy
import json

from . import autopilot, engine, first_state, item_state
from .github import GitHubError
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

# A runner's result around a stage's output: the plan's run already ended in NexKit 6.
AGENT = {
    "tokens": None,
    "limit": None,
    "transient": False,
    "noResult": False,
    "error": None,
    "session": None,
    "changeVersion": None,
    "timedOut": False,
}
# The engine asks for a plan once more for these, with NexKit 6's sticky flags and notes.
REMEMBER = {"fewer": "fewer_asked", "parts": "parts_asked", "titles": "titles_asked"}
# The time of an event that GitHub gives no time for.
NO_TIME = "1970-01-01T00:00:00Z"


def policies(cfg):
    """The engine's policies for what the GitHub host's configuration says
    (docs/engine.md#what-a-host-decides)."""
    return {
        "planApproval": "autoWhenClean" if autopilot.takes(cfg) else "person",
        "answers": "recommended" if cfg["autopilot"]["answer"] == "recommended" else "never",
        "splits": True,
        "splitParts": MAX_PARTS,
        "splitDepth": autopilot.MAX_DEPTH,
    }


def answer(request):
    """The engine's answer to `request`, sent and read as JSON, as `nexkit-engine` would."""
    found = engine.answer(json.loads(json.dumps(request)))
    if "error" in found:
        raise item_state.StateError(found["error"])
    return found


# -- what follows a plan -------------------------------------------------------------


def plan_output(plan, data, run):
    """The plan's result, as the plan stage gives it, from the record at the end of the plan
    comment `plan`, for what the engine decides from. `run` is the plan's run."""
    if data is None:
        # A plan from before NexKit kept its record: its run's attention says that it asked.
        asked = [{"question": "", "options": [], "recommended": 0, "closes": 0}]
        return _output(asked if run.get("attention") else [], False, [], [], "")
    asked = []
    for item in questions(data.get("questions")):
        options = item["options"]
        asked.append(
            {
                "question": item["question"],
                "options": options,
                "recommended": _number(options, item["recommended"]),
                "closes": _number(options, closing_option(item)),
            }
        )
    parts = [
        {
            "title": part.get("title") or "",
            "body": part.get("body") or "",
            # A plan from before parts named their dependencies: each needs the one before.
            "depends_on": part["depends_on"]
            if "depends_on" in part
            else [number - 1]
            if number > 1
            else [],
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
        "questions": asked,
        "too_large": too_large,
        "split": split,
        "protected_paths": list(protected),
        "no_change": finding,
        "acceptance_criteria": [],
    }


def _number(options, text):
    return options.index(text) + 1 if text and text in options else 0


def _document(issue, comments, stored, pull=None, pull_comments=()):
    """The item's document where NexKit 6 left it, on the revision stored on it."""
    doc = first_state.imported(issue, comments, pull, pull_comments)
    if stored is not None:
        doc["revision"] = stored["revision"]
    return doc


def _before_result(doc, title, depth, again):
    """`doc` as it stands while the plan's run is on its way: the result the engine waits
    for is the plan's. `again` says that autopilot's answers started this plan and no person
    commented since (`autopilot.asked_again`)."""
    doc.update(state="planning", attention=None, waits=[], title=title[:256], depth=depth)
    doc["pending"] = [{"id": first_state.RUN_ID, "kind": "runAgent", "changeVersion": None}]
    doc["asked"] = [name for name in doc["asked"] if name != "answers"] + ["answers"] * again
    return doc


def _waiting(doc):
    """`doc` as it stands while its plan waits: NexKit 6 decides after a plan at the next
    sweep, so a comment that came before it is answered on the plan as it waits."""
    asked = (doc["plan"] or {}).get("questions")
    reason = "question" if asked else "approval"
    doc.update(state="awaitingApproval", waits=[], pending=[])
    doc["attention"] = {"reason": reason, "text": first_state.SAYS[reason]}
    return doc


def _event(kind, number, at, **fields):
    return {"protocol": 1, "kind": kind, "item": str(number), "at": at or NO_TIME, **fields}


def _result(number, plan, data, run):
    result = {**AGENT, "output": plan_output(plan, data, run)}
    return _event(
        "actionFinished",
        number,
        plan.get("created_at"),
        action=first_state.RUN_ID,
        changeVersion=None,
        result=result,
    )


def _depth(gh, issue, data):
    """The item's depth among parts, read only for a plan that is too large, the only one
    whose decision reads it."""
    if not (data or {}).get("too_large"):
        return 0
    return autopilot.depth(issue, autopilot.lineage(gh, issue))


def _picked(comment, data):
    """(question, option, text) of the option that only closes the issue that a person's
    comment picks among the plan's questions, or None (`state.closing_answer`)."""
    items = (data or {}).get("questions")
    option = closing_answer(comment.get("body"), items) if items else None
    if not option:
        return None
    for number, item in enumerate(questions(items), 1):
        if closing_option(item) == option:
            return number, item["options"].index(option) + 1, option
    return None


def after_plan(gh, cfg, issue, plan, run, record, start, comments, pull=None, pull_comments=()):
    """The step after the latest plan of `issue`, whose run `run` posted it, as the engine
    decides it: with `pull`, the plan that autopilot asked for after a round of that pull
    request ran out of time. A collaborator's comment after the plan comes first."""
    number = issue["number"]
    data = plan_data(plan)
    _, stored = item_state.find(comments)
    said = [] if pull else autopilot._commented_after(comments, plan)
    picked = _picked(said[-1], data) if said else None
    if said:
        doc = _waiting(_document(issue, comments, stored))
        by = (said[-1].get("user") or {}).get("login") or ""
        at = said[-1].get("created_at")
        if picked:
            # Closing is a write: a collaborator who may only read changes the plan at most.
            role = "writer" if by and gh.can_write(by) else "reader"
            answers = [{"question": picked[0], "option": picked[1]}]
            event = _event("answered", number, at, by=_person(by, role), answers=answers, note="")
        else:
            text = {"name": "comment", "ref": str(said[-1]["id"])}
            event = _event("commented", number, at, by=_person(by, "reader"), text=text)
    else:
        details = gh.pull(pull) if pull else None
        doc = _document(issue, comments, stored, details, pull_comments)
        again = autopilot.asked_again(comments)
        doc = _before_result(doc, issue.get("title") or "", _depth(gh, issue, data), again)
        event = _result(number, plan, data, run)
    request = {"protocol": 1, "call": "step", "state": doc, "event": event}
    request["policies"] = policies(cfg)
    found = answer(request)
    plan_view = _PlanView(issue, plan, data, run, start, pull, picked, said)
    step = plan_view.carry(found["actions"], doc["asked"], found["state"]["asked"])
    step["engine"] = {"request": request, "for": step["state"]}
    return step


def _person(login, role):
    return {"id": login or "someone", "role": role}


class _PlanView:
    """What the sweep does for one answer of the engine after a plan, in NexKit 6's words."""

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
        raise item_state.StateError(f"The engine answered #{self.issue['number']} with nothing.")

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
        bodies = self.data.get("split") or []
        parts = [
            {
                "title": part["title"],
                "body": bodies[part["key"] - 1].get("body") or "",
                "depends_on": part["needs"],
            }
            for part in create["parts"]
        ]
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

    def repeated(self):
        return autopilot.repeated_titles(self.issue, self.data.get("split") or [])

    def note(self, name):
        if name == "fewer":
            return autopilot.FEWER_NOTE.format(parts=self.parts(), most=MAX_PARTS)
        if name == "parts":
            return autopilot.PARTS_NOTE
        titles = ", ".join(f'"{title}"' for title in self.repeated())
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
        if reason == "askedAgain":
            text = autopilot.REPEATED
        elif reason == "closeRecommended":
            text = autopilot.CLOSE_UNFOUNDED
        elif reason == "handMadeChange":
            text = autopilot.protected_reason(self.data["protected_paths"])
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
            text = self.run.get("attention") or autopilot.QUESTIONS_REASON
        return self.step("waits", text)


def plan_carried_on(gh, cfg, issue, decision, output):
    """Whether autopilot carries on after the plan `output` that a run of `decision` posted,
    as the engine decides it: whether its answer needs no person."""
    pr = decision.get("pr")
    comments = gh.comments(issue["number"])
    target = gh.comments(pr) if pr else comments
    again = autopilot.asked_again(target, comments if pr else ())
    _, stored = item_state.find(comments)
    depth = _depth(gh, issue, output)
    doc = _document(issue, comments, stored)
    doc = _before_result(doc, issue.get("title") or "", depth, again)
    # The plan as its comment's record keeps it, which the sweep reads after the run.
    result = {**AGENT, "output": plan_output({}, plan_record(output), {})}
    event = _event(
        "actionFinished",
        issue["number"],
        None,
        action=first_state.RUN_ID,
        changeVersion=None,
        result=result,
    )
    request = {"protocol": 1, "call": "step", "state": doc, "event": event}
    found = answer({**request, "policies": policies(cfg)})
    # As in NexKit 6, a run counts autopilot's answers as carrying on also when one only
    # closes the issue: the sweep then waits for a person and mentions them itself.
    return all(
        action["reason"] == "closeRecommended"
        for action in found["actions"]
        if action["kind"] == "needPerson"
    )


# -- a person's command ----------------------------------------------------------------


def asked_by_plan(comments):
    """Whether what NexKit asked last on an issue, if anything, is its latest plan's
    questions, rather than a blocked round's."""
    runs = [run for _, run in run_comments(comments) if "command" in run]
    return bool(runs) and runs[-1].get("command") == "plan" and asked_last(comments) is not None


def closing_command(gh, decision, comments):
    """A writer's `/nexkit plan` or `/nexkit go` whose note answers the latest plan's
    questions: `route` sends it to the engine as an answer when the note picks the option
    that only closes the issue, and the `maintain` job closes the issue when the engine
    drops it. Any other note goes to its round as it is."""
    number = decision["issue"]
    link, _ = asked_last(comments)
    data = plan_data(latest_plan(comments)) or {}
    picked = _picked({"body": decision["note"]}, data)
    if not picked:
        return decision
    _, stored = item_state.find(comments)
    # The import reads only the comments here; the issue is open, as a command needs.
    doc = _waiting(_document({"number": number}, comments, stored))
    pulls = gh.open_pulls(decision["branch"])
    if pulls:
        doc["change"] = {"id": str(pulls[0]["number"]), "version": pulls[0]["head"]["sha"]}
    answers = [{"question": picked[0], "option": picked[1]}]
    by = _person(decision.get("actor"), "writer")
    event = _event("answered", number, None, by=by, answers=answers, note="")
    found = answer({"protocol": 1, "call": "step", "state": doc, "event": event, "policies": {}})
    if not any(a["kind"] == "close" for a in found["actions"]):
        # A pull request that is open stays with its issue: a person closes both.
        return decision
    return {
        "action": "maintain",
        "task": "close_answer",
        "issue": number,
        "answer": picked[2],
        "asked": link,
    }


# -- storing ---------------------------------------------------------------------------


def _plain(doc):
    """A document without what every answer changes: its revision and its actions' ids."""
    pending = [{**item, "id": ""} for item in doc["pending"]]
    return {**doc, "revision": 0, "pending": pending}


def store(gh, step):
    """Store the engine's answer for `step` on its item, once the sweep carried it out as
    the engine decided, unless it changes nothing that is stored. The answer is given again
    on the stored revision, so a write that another came first to answers again. Returns
    lines for the log."""
    found = step.get("engine")
    if not found or found["for"] != step["state"]:
        return []
    request = found["request"]

    def again(stored):
        asked = copy.deepcopy(request)
        if stored is not None:
            asked["state"]["revision"] = stored["revision"]
        new = answer(asked)
        if not new["changed"] or (stored is not None and _plain(new["state"]) == _plain(stored)):
            return stored, [], False
        return new["state"], new["actions"], True

    number = step["number"]
    try:
        item_state.apply(gh, number, again)
    except (GitHubError, item_state.StateError, item_state.Moved) as exc:
        return [f"Could not store the state of #{number}: {exc}"]
    return []
