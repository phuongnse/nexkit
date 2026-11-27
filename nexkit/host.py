"""The GitHub host's calls of the engine's `step` (docs/engine.md#getting-there).

Every decision that moves an item is the engine's. Most still keep no state document: for each
one, the host builds the item's document from NexKit 6's markers, as the import reads them
(`first_state`), sends the event that GitHub shows, and carries out the answer with NexKit 6's
code, in NexKit 6's words. The decisions after a plan, for a new issue's first plan, after a
writer's vouch, after a blocked or stopped round, at a usage limit or a transient error, when a
resume comes due and for a lost command read the item's stored state document too, and store
the engine's answer (`stored`, `decide`, `decide_all`, `resync`, below). After a plan, a stopped
round and a review, `report` also sends what the runner's `run.json` says the agent spent
(`spent`).

The engine decides what follows a plan: its approval by autopilot, the answers to its
questions, once in a row, a writer's answer that only closes the issue, a plan that finds no
change or needs a hand-made change, splits and their rules, a new plan after a collaborator's
comment, and what a plan waits for without autopilot. That includes the plan that autopilot asked
for after a round of the issue's pull request ran out of time (`autopilot._replanned`): the host
sends it with the pull request as the change and `supersede` asked, and when the engine splits the
issue and answers `withdrawChange`, the host closes the pull request as superseded.

It also decides what follows a `/nexkit go` that stopped as blocked with questions or with
protected paths its change needs (`autopilot.engine_round`): the answers to its questions, once
in a row, a recommended option that only closes the issue, with the round's finding that no
change is needed or without it, one that only a new plan carries out, a writer's answer that
only closes the issue, by a comment or a command, and a new plan after a collaborator's comment
or the close of an issue that blocks it. So does one that stopped as blocked with nothing to
answer, which waits for a person and, with autopilot, is planned again after a comment or the
close of an issue that blocks it (`afterFailure`). So does one that failed on an error, with no
pull request, and not on time: the host sends it as an agent error, and the engine plans it again
after a collaborator's comment or the same close.

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

It decides the wait for required checks that have not passed yet at a merge, and its end
(`report._refused`, `autopilot._merge_step`): `accept_answer` sends the merge's answer to
`step` as an `accept` result, so that the item stays in `accepting` with its first start kept,
and `checks_waited` sends the sweep's tick, which tries the merge again or, once the checks
have not passed for `machine.CHECKS_END`, leaves it to a person. A merge whose issue no
sweep looks at is the host's own refusal, since no tick would come for it.

It decides, in the sweep, whether a merge that GitHub refused is tried again: `refusal_cleared`
sends the head as an observation, `acceptable` when GitHub's `mergeable_state` says it would
accept it, and the host merges only when the engine asks for the accept.

It decides what follows the other refusals of a merge (`report._refused`, `report._head_moved`,
`report._guarded`): a head that moved, which the host sends as an observation of the new head
first, so that the engine checks it, or a person decides when none was read or it is the head
the round checked; the last refusal after the base branch kept moving through the host's quick
tries; the checks before the merge, an issue closed, edited after a vouch or unreadable; and
any other refusal GitHub gives that only a person clears.

It decides what follows a merge that GitHub refused for a conflict (`report._refused`):
`accept_answer` sends it as a `conflict` result with the conflict rounds used, and the engine
answers a conflict round while `conflicts.max_rounds` has one left, or a person. The host starts
the round with `start_conflict_round(left=...)`, which keeps NexKit 6's other reasons not to
start one. A pull request found `dirty` only at the end goes as a `conflict` without
`conflicts.auto_resolve` and as `needsPerson` with it.

It decides what follows a merge that GitHub refused because the branch is behind its base
(`report._update_branch`, `report._refused`): `accept_answer` sends it as a `behind` result with
the updates used, which `conflicts.max_rounds` limits apart from conflict rounds, and the engine
answers an update while one is left under `conflicts.auto_resolve`, and a person otherwise. The
host carries out the update with `maintain.update_branch(left=...)`, which starts the review that
carries the approval over, and sends an update that GitHub refused back (`update_failed`), which
the engine hands to a person. A sweep's merge that waits for required checks past six hours
sends the observation that its branch is behind (`checks_waited`), and the engine keeps trying
the merge while `conflicts.auto_resolve` is on.

It decides what follows a move of the base branch under an open NexKit pull request
(`base_moved`, called by `maintain.resolve_conflicts`, `maintain.stop_superseded` and
`report._conflicts_after`): the host sends the conflict, and that the branch is behind its base
when NexKit 6 would update it, with the conflict rounds and updates used, and starts the conflict
round, updates the branch, stops the round's checks as `superseded` or waits for a person as the
engine answers. A round that runs only its checks is in `checking`, any other pull request in
`ready`. A hold, NexKit 6's other reasons not to start a round and a conflict round or update
that fails to start after a stop (`stop_failed`) stay the host's.

It decides when a plan, implement or fix stage runs again after its agent stopped at a usage
limit or on a transient error (`stage_limited`, `stage_transient`, called by `report`), and when
a paused or waiting run starts at its time (`resumes`, called by `maintain.resume_one`), a hold
on the issue included. A transient stop of publish goes as a transient stop of the stage's agent.
A reset stays the host's to give.

It decides what follows a command that autopilot started and lost, whose run GitHub never
started, or that ended without a report, or that handed the work on to a run that never started
(`autopilot._again`, called by `_reportless` and `_handed_on`): `lost_again` sends `actionLost`
at the time of the last start, with autopilot's count of losses, and carries out the start
again, the wait or the person. What counts as lost, and the six-hour window of the count, stay
`autopilot`'s. A person's own lost command waits for that person.

It decides what the merge finds too (`report.merge_pull`, `autopilot._merge_step`):
`change_seen` sends a pull request that a person merged first, a change that its base branch
already holds, and a commit that a person pushed while the merge waited, as observations, and the
item ends or its new head is checked as the engine answers. An implement round that finds no
change is needed (`no_change_found`, called by `report._no_change`) and a fix round that finds its
change on the base branch (`on_base_found`, called by `report._already_on_base`) reach it too, and
close only when it ends the item as `noChange`; the `report` of the item's group stores the
answer of both in the item's state. Every parent's end goes through it: `part_ended` sends the end
of a part to its parent's document, at every level and on every close of the part (a merge, a
person's close, a change already on the base branch, no change needed, autopilot's sweep), and
the parent ends only when every part ended and one was completed, under `parentEnd`, whatever
the number of its sub-issues. The run of the part only previews that answer, and sends the end
to the parent's group as a `step` event with the note `part N` (`maintain.send_part_end`); the
run that takes it there stores the answer in the parent's state (`maintain.record_part_end`),
and so does the `step` run of a parent whose parts are all closed. Only the facts GitHub knows,
such as parts never filed as sub-issues, stay with `maintain.close_parent`.

Every round's checks and review reach it (`round_ended`): a review that paused at a usage
limit, a review that stopped on a transient error, a review that failed, and a person's own pull
request, which is only reviewed. It finds the checks
that failed the same way before and after a fix round, which the state comment keeps, and
names those that the fix round `route` starts diagnoses (`diagnosis`). A round whose commit
conflicts with its base branch starts the round that resolves it, within
`conflicts.max_rounds`, unless a label holds the issue, which keeps it back until the sweep
finds the label gone (`conflicting`, `conflicts_released`).

It decides the start of the first plan of a new issue too (`first_plan`, called by
`autopilot._first_plan`): the host builds the document that the import builds for an issue with
no plan, `new` and waiting to start, decides it on the stored document (`decide`), sends a
`requested` start with no person, and dispatches
`/nexkit plan` only when the engine answers with a plan run. NexKit 6's checks of a hold, trust,
blockers and waves come first, so the engine is never asked for a held or blocked issue.

It decides what follows a writer's vouch after an untrusted edit stopped a round or a merge
(`vouched`, called by `autopilot._vouched`): the host builds the document that the import
builds, `failed` with attention `untrusted` or, for a merge, `ready`, decides it on the stored
document (`decide`), sends the writer's newer
`/nexkit trust` as a `requested` `trust`, and starts a plan, an automatic fix round that counts,
or the checks and review, as the engine answers. A pull request with no automatic fix round left
waits for a person, where NexKit 6 started a counted round anyway
(`tests/differential/known.py`). Every start that autopilot makes follows an answer of the
engine (`tests/test_autopilot.py` and `tests/test_answers.py`).

It decides what a writer's approval of a NexKit pull request does too (`approved`, called by
`maintain.send_approval` and `maintain.merge_approved`): the host builds the document of the
latest round's outcome, `ready` with attention `acceptRefused`, `reopened`, `personChecklistItem`
or `accept`, sends the approval as a `changeApproved`, and merges, accepts the checklist items a
person decides or starts nothing as the engine answers. A hold, an implement round that still
runs, a pull request with no round and a round whose outcome takes no approval stay the host's.
An engine that refuses to answer leaves GitHub as it was.

It decides which of the issues that may start do, and in what order, too (`sweep`, called by
`autopilot.preview`): the start steps without a pull request go to the engine's `sweep` call, with
the issues in progress as `running`, each issue's labels, the index of its milestone, its wave and
whether pull requests wait for it as a bug issue about the default branch, and no account at a
usage limit. The host says each wait in NexKit 6's words, and an engine that refuses the sweep
leaves each issue that would start waiting for a person.

It decides whether a person's command starts its stage, too (`command`, called by
`route._decide`): a `/nexkit` comment, a *Request changes* review and each dispatch of a
command that NexKit sends. It carries out the answer that `take` stored for the run's own
command and does not send it to the engine again, which would take it for a duplicate, so each
command is stored as one revision. A command that an earlier run took (its event is in the
state's events seen) starts nothing and gets no reply, and one that no run took is decided on
the stored state, which a run of the item's group stores (`decide`, `resync`); another command
taken in this run that already started a stage refuses it. A command whose own run a newer run
replaced starts in the run that ran, when that run's own event starts nothing
(`route._taken_start`). The host builds the small document that NexKit 6's `route` read the
facts of, whether the issue or pull request is open, the latest plan, an open pull request for
`/nexkit go` and a base branch that already holds the change, where `take` sent nothing or the
engine is asked, and starts only the stage that the engine answers; a change that the engine
ends as `noChange` goes to the `on_base` task. A closed issue or pull request, which a state
comment outlives, and an open pull request for `/nexkit go` are the engine's to refuse on that
document, and the host says each in NexKit 6's words. An engine that refuses, or answers
another stage, starts nothing, and the person gets a reply. A fork, a person's pull request,
which takes no `/nexkit fix`, an unknown command and an implement round that still runs stay
host checks.

An item whose document or event the engine refuses waits for a person, and no other item is
held up by it.

Every run in an item's group first stores the item's first state when it has no state comment
(`store_import`): an open item's document imported from NexKit 6's markers, at revision 1. It then
takes the item's events (`take`, both called by `cli._take` from `nexkit route`): it answers every
comment, review and hold-label change of the item after the cursor in its state comment, oldest
first, so that a command whose run a newer one replaced is still answered, and a run whose writes
keep losing fails and leaves its event for the next run. A state comment that someone other than
the App changed stores and takes nothing: the item waits for a person.

The decisions after a plan (`after_plan`, `plan_carried_on`, `plan_waits_for`), for a new issue's
first plan (`first_plan`), after a writer's vouch (`vouched`), after a blocked or failed `/nexkit
go` (`after_go`, `round_carried_on`), after a stopped or timed-out round (`stopped`,
`stop_carried_on`), at a usage limit or a transient error (`stage_limited`, `stage_transient`), when
a resume comes due (`resumes`), for a lost command (`lost_again`), on a person's command
(`command`), on a closing command (`closing_command`, which only previews, since `take` stored its
event and the `maintain` job closes) and on an approval (`approved`: the `step` run of the item's
group stores it, and the approval's own run in `approved-N` previews) and at an item's end and a
parent's end (`no_change_found`, `on_base_found`, `part_ended`, as above) read the item's stored
state document (`stored`: the document in its state comment, or the import in memory when it has
none) and send their event through `decide`, or their events in a row through `decide_all`. While
other decisions still build their document from NexKit 6's markers, `resync` uses the stored
document only when it says what the markers' document says, apart from its revision, its events seen
and the ids of its outbox, and does not hold the event yet, or any event of a row. Otherwise the
decision is the markers' document at the stored revision, so that NexKit 6's answers stay exactly as
they are: a finished plan's import is `awaitingApproval`, which cannot take the plan's result, and a
second send of one event, such as the plan's result that `report` and the step run both send, would
be a duplicate. The last move to the stored document removes `resync`. A row of events, such as a
hold and then the stop, or a round's result and then a comment, is one write: the state after the
last event, at the revision read plus one, since `item_state.write` takes only the next revision,
and a tick of a resume names the wait of the document it is decided on (`wait_id`). A run in the
item's group stores the answer (`write`: the `report` after a stage, the `step` run, and the `step`
that starts a due resume, `maintain.resume_one`), only on top of an existing state comment, at the
next revision, with `item_state.apply`: a write that lost reads the state again, waits and decides
again, and when every try lost, the run warns and carries out the answer anyway, which the next
decision stores again. A run outside that group only previews: the sweep, `nexkit status` and
`plan_waits_for`, which asks what a plan waits for under no automatic approval, which is no answer
of the item's own, and the `resume` run, which only sends the `step`. With no configuration,
`resumes` and `lost_again` decide on the markers' document and read and store nothing. An item with
no state comment stores nothing here, since only `store_import` posts the first one, and one whose
state comment someone other than the App changed is refused (`autopilot.changed_comment`).
`pending_id` names the action of the outbox that a result answers.

No module outside `nexkit/engine/` writes on GitHub without an answer of the engine before it:
`tests/test_answers.py` reads their code and fails on a dispatch, a merge, a branch update, a close,
a cancel or a rerun that none comes before.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time

from . import autopilot, engine, first_state, item_state, protocol, usage
from . import config as configuration
from .checks import failing_signatures
from .engine.machine import RUNS
from .protocol.size import LONGEST, MOST
from .report import TOO_LARGE
from .schema import filled
from .state import (
    MAX_PARTS,
    asked_last,
    by_bot,
    closing_answer,
    closing_option,
    latest_plan,
    plan_data,
    plan_profile,
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
}
# What the runner's result says an agent spent, which the engine adds to the item's `used`.
SPENT = ("tokens", "seconds", "session")


def spent(ran):
    """`AGENT` with what the runner's result `ran` (`run.json`) spent, when it fits the protocol;
    otherwise `AGENT`. The rest of the result is sent as it was."""
    if not isinstance(ran, dict) or protocol.check(
        ran, "actions/runAgent.result.schema.json", "run.json"
    ):
        return dict(AGENT)
    return {**AGENT, **{key: ran[key] for key in SPENT}}


# What the engine asks a plan once more for, as autopilot's record keeps it.
REMEMBER = {"fewer": "fewer_asked", "parts": "parts_asked", "titles": "titles_asked"}
# The time of an event that GitHub gives no time for.
NO_TIME = "1970-01-01T00:00:00Z"
# What a person does about an issue whose next step the engine refused to decide.
REFUSED_DO = "read why below, then carry the issue on with `/nexkit plan` or `/nexkit go`"


class Refused(Exception):
    """The engine refused an item's document or event, or answered nothing that the host
    carries out."""


def item_profile(cfg, comments):
    """The profile that the item's runs use: the latest plan's, or `default_profile` when no plan
    names one, and None when there are no profiles or the name is no longer in them. It mirrors
    `route.with_profile`."""
    if not cfg["profiles"]:
        return None
    plan = latest_plan(comments)
    record = plan_profile(plan) if plan else None
    name = (record["profile"] if record else None) or cfg["default_profile"]
    return name if name in cfg["profiles"] else None


def policies(cfg, profile=None):
    """The engine's policies for the GitHub host's configuration
    (docs/engine-hosts.md#what-a-host-decides). `profile` is the profile that the item's runs
    use (`item_profile`): its stage settings give the budgets and agents of every stage but
    triage, which no profile changes."""
    taken = autopilot.takes(cfg)
    variant = configuration.with_profile(cfg, profile) if profile in cfg["profiles"] else cfg
    stages = [
        stage
        for stage in ("triage", "plan", "implement", "fix", "review")
        if stage == "fix" or stage in cfg["stages"]
    ]

    def agents(stage):
        if stage == "plan" and cfg["profiles"]:
            # Triage chooses the profile inside the plan job, so the runner chooses these.
            return {"agent": None, "model": None, "effort": None}
        settings = configuration.stage(cfg if stage == "triage" else variant, stage)
        return {key: settings[key] for key in ("agent", "model", "effort")}

    return {
        "planApproval": "autoWhenClean" if taken else "person",
        "reviewAfterFailingChecks": "always",
        "accept": "autoWhenPassed" if cfg["merge"]["auto"] else "person",
        "fixRounds": cfg["fix"]["max_auto_rounds"],
        "whenRoundsRunOut": "person",
        "conflicts": "resolve" if cfg["conflicts"]["auto_resolve"] else "person",
        "conflictRounds": cfg["conflicts"]["max_rounds"],
        "answers": cfg["autopilot"]["answer"],
        "afterFailure": "replan" if taken else "person",
        "splits": taken,
        "splitParts": MAX_PARTS,
        "splitDepth": autopilot.MAX_DEPTH,
        "retryWaits": list(cfg["retry"]["wait_minutes"]),
        "lostWaits": [int(wait.total_seconds() // 60) for wait in autopilot.AGAIN] if taken else [],
        # Triage has a time only when there are profiles to choose from.
        "budgets": {
            stage: {
                "seconds": configuration.stage_minutes(cfg if stage == "triage" else variant, stage)
                * 60
            }
            for stage in stages
        },
        "agents": {stage: agents(stage) for stage in stages},
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


# The kinds of source that name an event: a comment, a review, an event of the issue events API,
# a dispatch (by its run) and an agent's result (by its run).
SOURCES = item_state.SOURCES


def event_id(kind, source):
    """The id `<kind>-<id>` of an event that GitHub's object `source` (or its id) gives, so a
    source gives the same id each time and two sources never share one. Only GitHub's positive
    numeric id counts, never a text that a person wrote. Raises `ValueError`.
    See docs/engine.md#events-and-actions-arrive-more-than-once."""
    if kind not in SOURCES:
        raise ValueError(f"No event is named by a {kind!r}.")
    found = source.get("id") if isinstance(source, dict) else source
    if not isinstance(found, int) or isinstance(found, bool) or found <= 0:
        raise ValueError(f"A {kind} names an event by its positive number, not {found!r}.")
    return f"{kind}-{found}"


def _run_event_id(url=None):
    """The id of the agent's result of the workflow run that the link `url` of a run comment
    names, or of the run this process is in when there is no link. A process outside GitHub
    Actions, or a comment without a link, has no source and gives `run-local`."""
    from .route import RUN_ID

    match = RUN_ID.search(url or "")
    found = match.group(1) if match else None
    if found is None and not url:
        found = os.environ.get("GITHUB_RUN_ID", "")
    return event_id("run", int(found)) if found and found.isdigit() else "run-local"


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
        _run_event_id(run.get("url")),
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
        name = event_id("comment", comment)
        fields = {"by": _person(by, role), "answers": answers, "note": "", "asked": asked}
        return _event("answered", number, at, name, **fields)
    # A collaborator, as `autopilot._commented_after` reads them: whether they may write
    # matters only to an answer that closes.
    text = {"name": "comment", "ref": str(comment["id"])}
    by = _person(by, "reader")
    return _event("commented", number, at, event_id("comment", comment), by=by, text=text)


# -- an item's events from their sources ---------------------------------------------------

# What a writer's `/nexkit` comment can ask for on an item.
COMMANDS = ("plan", "go", "fix", "review", "trust")
# The form of a time that the cursor keeps.
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


class _Source:
    """A comment, a review or an issue event of GitHub, with the id of the event it gives
    (`event_id`) and the time it happened."""

    def __init__(self, kind, raw):
        self.kind, self.raw = kind, raw
        self.name = event_id(kind, raw)
        at = raw.get("submitted_at" if kind == "review" else "created_at") or ""
        self.at = at if TIME.fullmatch(at) else NO_TIME

    @property
    def cursor(self):
        return self.at, self.name

    @property
    def place(self):
        return item_state.order(self.cursor)


def _pull(doc):
    """The number of the change of the document `doc`, or None when it has none."""
    change = (doc.get("change") or {}).get("id") or ""
    return int(change) if change.isdigit() else None


def _sources(gh, number, pull, comments):
    """The `_Source`s of work item `number`, whose change is the pull request `pull`, oldest
    first, in the order the cursor keeps: its comments (`comments`) and those of its pull
    request, the reviews of the pull request and the item's issue events."""
    found = [("comment", raw) for raw in comments]
    if pull:
        found += [("comment", raw) for raw in gh.comments(pull)] if pull != number else []
        found += [("review", raw) for raw in gh.reviews(pull)]
    found += [("issue-event", raw) for raw in gh.issue_events(number)]
    sources = []
    for kind, raw in found:
        try:
            sources.append(_Source(kind, raw))
        except ValueError:
            # No number of GitHub's names it, so no event can.
            continue
    return sorted(sources, key=lambda one: one.place)


def _newer(one, cursor, stored):
    """Whether the source `one` is after the cursor, or, while there is none, after the state
    comment `stored`: the import that built the first document read what came before, and of
    the comment's own second only a comment with a higher id follows."""
    if cursor:
        return one.place > item_state.order(cursor)
    at = stored.get("created_at") or NO_TIME
    return one.at > at or (one.at == at and one.kind == "comment" and one.raw["id"] > stored["id"])


def _taken_event(gh, number, pull, comments, holds, one):
    """The event that the source `one` gives work item `number`, or None for a source that
    gives none: a comment of a bot, a review that is not a writer's approval or request for
    changes, any other event of an issue. `pull` is the item's change, `comments` the item's
    comments and `holds` the labels that hold. What each event does is the engine's to decide."""
    raw, name, at = one.raw, one.name, one.at
    user = raw.get("user") or {}
    login = user.get("login") or ""
    if one.kind == "issue-event":
        label = (raw.get("label") or {}).get("name")
        if raw.get("event") not in ("labeled", "unlabeled") or label not in holds:
            return None
        if raw["event"] == "labeled":
            return _event("held", number, at, name, label=label)
        return _event("released", number, at, name)
    if user.get("type") == "Bot" or by_bot(raw):
        return None
    if one.kind == "review":
        state = str(raw.get("state") or "").upper()
        if state not in ("APPROVED", "CHANGES_REQUESTED") or not login or not gh.can_write(login):
            return None
        by = _person(login, "writer")
        if state == "CHANGES_REQUESTED":
            note = {"name": "review", "ref": str(raw["id"])}
            return _event("changesRequested", number, at, name, by=by, note=note)
        version = raw.get("commit_id")
        if not pull or not isinstance(version, str) or not version:
            return None
        fields = {"by": by, "changeId": str(pull), "changeVersion": version}
        return _event("changeApproved", number, at, name, **fields)
    from .route import parse_command

    command, note = parse_command(raw.get("body"))
    if command in COMMANDS and login and gh.can_write(login):
        plan = None
        if command == "go":
            # The plan the person saw: the latest one posted before their command.
            before = [other for other in comments if other["id"] < raw["id"]]
            found = latest_plan(before)
            plan = str(found["id"]) if found else None
        fields = {"what": command, "by": _person(login, "writer"), "note": note, "plan": plan}
        return _event("requested", number, at, name, **fields)
    text = {"name": "comment", "ref": str(raw["id"])}
    return _event("commented", number, at, name, by=_person(login, "reader"), text=text)


def _edited(edited):
    """The `Refused` that stands for the state comment `edited` (an `item_state.Edited`)."""
    return Refused(autopilot.changed_comment([edited.comment]))


def store_import(gh, cfg, number, now=None):
    """Store the first state of the open work item `number` as the App's state comment at
    revision 1, imported from NexKit 6's markers (`first_state.import_item`), when it has no
    state comment, and answer whether it did. It keeps no cursor, so that `take` answers what
    came after the comment. A closed item stores nothing: it has nothing to carry on, and every
    closed issue starts a run in its group. A state comment that someone other than the App
    changed is not absent: it raises `Refused` and nothing is stored."""
    try:
        _, doc = item_state.read(gh, number)
    except item_state.Edited as edited:
        raise _edited(edited) from None
    issue = gh.issue(number)
    if doc is not None or issue.get("state") != "open":
        return False
    pull = None
    if "pull_request" not in issue:
        from .route import branch_for

        found = [
            one
            for one in gh.open_pulls(branch_for(number))
            if ((one.get("head") or {}).get("repo") or {}).get("full_name") == gh.repository
        ]
        pull = found[0]["number"] if found else None
    state = first_state.import_item(gh, cfg, number, pull, now=now)
    try:
        return item_state.write(gh, number, state, (None, None))
    except item_state.Edited as edited:
        raise _edited(edited) from None


def take(gh, cfg, number, tries=item_state.TRIES, sleep=time.sleep):
    """Answer every event of work item `number` that is still at its source after the state
    comment's cursor, oldest first, and give [(event, actions)] of those whose answer was
    stored. A run of an item's concurrency group calls it, so that a command whose run GitHub
    replaced with a newer one is answered by the run that ran, not lost
    (docs/engine-hosts.md#hosts). The sources are a writer's `/nexkit` comments, other people's
    comments, a writer's approving or *Request changes* review of the change, and the adding
    and removing of a label of `autopilot.labels` that holds the item. Each is answered by
    `item_state.apply`, which moves the cursor past it, even when the answer changes nothing,
    so that an old remark is never answered on a state that moved on. A work item with no
    state comment has nothing to take: `store_import` stores its first state before.

    Raises `item_state.Moved` when every try to store an answer lost to other writes: the run
    fails, and that event, and those after it, stay at their source for the next run. Raises
    `Refused` when the engine refuses an event or the state, or when someone other than the
    App changed the state comment: nothing is taken then."""
    try:
        return _take(gh, cfg, number, tries, sleep)
    except item_state.Edited as edited:
        raise _edited(edited) from None


def _take(gh, cfg, number, tries, sleep):
    comment, doc = item_state.read(gh, number)
    if doc is None:
        return []
    cursor = item_state.since(comment)
    comments = gh.comments(number)
    pull = _pull(doc)
    sources = [one for one in _sources(gh, number, pull, comments) if _newer(one, cursor, comment)]
    holds = {name for name, does in autopilot.autopilot_labels(cfg).items() if does.get("hold")}
    found_policies = policies(cfg, item_profile(cfg, comments))
    taken = []
    for one in sources:
        event = _taken_event(gh, number, pull, comments, holds, one)
        if event is not None and protocol.check(event, "event.schema.json", "event"):
            event = None
        if event is None:
            continue
        stored = []

        def answer(state, event=event, stored=stored):
            found = step(state, event, found_policies)
            stored[:] = [found["changed"]]
            return found["state"], found["actions"], found["changed"]

        _, actions = item_state.apply(
            gh, number, answer, tries=tries, sleep=sleep, since=one.cursor
        )
        if stored and stored[0]:
            taken.append((event, actions))
    return taken


# -- the stored document and the decisions that store their answer -------------------------


def pending_id(doc, kind, stage=None):
    """The id of the first action of `doc`'s outbox that is a `kind` (and runs `stage`, when
    given), which the result of that action names, or the id that NexKit 6's runs on their way
    stand for (`first_state.RUN_ID`) when `doc` has none."""
    for action in doc.get("pending") or []:
        if action["kind"] == kind and (stage is None or action.get("stage") == stage):
            return action["id"]
    return first_state.RUN_ID


def stored(gh, cfg, number, pull=None):
    """(comment, document) of work item `number`: its state comment and the document stored
    in it, or (None, document) with the document that the import builds in memory when it has
    none (`first_state.import_item`), and (None, None) when the import breaks the protocol or
    the comment holds a document that cannot be read, which holds nothing up and which nothing
    is stored over. Nothing is stored. Raises `Refused` when someone other than the App changed
    the comment."""
    try:
        comment, doc = item_state.read(gh, number)
    except item_state.Edited as edited:
        raise _edited(edited) from None
    except item_state.StateError:
        return None, None
    if doc is not None:
        return comment, doc
    try:
        return None, first_state.import_item(gh, cfg, number, pull)
    except (item_state.StateError, ValueError):
        return None, None


def _core(doc):
    """`doc` without what a decision does not read: the revision, the events seen, and the
    ids that each action of the outbox and each wait was given."""
    doc = copy.deepcopy(doc)
    doc.pop("revision", None)
    doc.pop("recentEvents", None)
    for key in ("pending", "waits"):
        for entry in doc.get(key) or []:
            entry.pop("id", None)
            entry.pop("next", None)
    return doc


def wait_id(doc, kind):
    """The id of the first wait of `doc` that is a `kind` (`usageLimit`, `time`, ...), which a
    tick names, or `w1.1`, the id of the first wait that the engine gives, when `doc` has none."""
    for wait in doc.get("waits") or []:
        if wait["for"] == kind:
            return wait["id"]
    return "w1.1"


def resync(state, built, event):
    """The document that `event` is decided on while decisions still build theirs from NexKit
    6's markers: the stored `state` when it says what `built` says, apart from its revision, its
    ids and its events seen, and does not hold `event` yet. Otherwise `built`, at the
    stored revision and with the events seen of `state`, but `event`, since the engine would
    take a second send of it for a duplicate. `event` may be a list of events that are sent in a
    row: `state` holds none of them, or `built` is the document. With no `state`, `built` itself.
    This is transitional: the last part of the move to the stored document removes it."""
    if state is None:
        return built
    events = event if isinstance(event, list) else [event]
    names = {one["id"] for one in events}
    seen = state["recentEvents"]
    if not names & set(seen) and _core(state) == _core(built):
        return state
    base = copy.deepcopy(built)
    base["revision"] = state["revision"]
    base["recentEvents"] = [name for name in seen if name not in names]
    return base


def decide_all(
    gh,
    cfg,
    number,
    events,
    built,
    *,
    pull=None,
    found_policies=None,
    action=None,
    wait=None,
    until=None,
    write=False,
    with_changed=False,
):
    """The engine's answers to `events` sent in a row on work item `number`, as (state,
    [(event sent, actions), ...]): decided on the stored document where it agrees with `built`
    (`resync`), with `action`, (kind, stage), as the id of the pending action that an
    `actionFinished` or `actionLost` event answers (`pending_id`), and with the id of the wait of
    kind `wait` in the document that a `tick` names (`wait_id`). The events after the first
    are sent on the state that the one before it gave, and the row stops after the event whose
    actions satisfy `until`, when given. `found_policies` default to the configuration's, and
    with no `cfg` the decision is read from `built` alone and nothing is read or stored.

    The state is the one after the last event, at the revision that was read plus one when
    anything changed, since `item_state.write` takes only the next revision: the row is stored
    as a single write. See
    `decide` for `write`. With `with_changed` the result is (state, answers, changed). Raises
    `Refused`."""
    found_policies = (policies(cfg) if cfg else {}) if found_policies is None else found_policies
    last = []

    def answer(state):
        base = resync(state, built, events)
        revision, state, answers, changed = base["revision"], base, [], False
        for event in events:
            sent = event
            if action is not None and event["kind"] in ("actionFinished", "actionLost"):
                sent = {**sent, "action": pending_id(state, *action)}
            if wait is not None and event["kind"] == "tick":
                sent = {**sent, "wait": wait_id(state, wait)}
            found = step(state, sent, found_policies)
            state, changed = found["state"], changed or found.get("changed", False)
            answers.append((sent, found["actions"]))
            if until is not None and until(found["actions"]):
                break
        if changed:
            state = {**state, "revision": revision + 1}
        last[:] = [(state, answers, changed)]
        return state, answers, changed

    def given():
        return last[0] if with_changed else last[0][:2]

    if cfg is None:
        answer(None)
        return given()
    comment, doc = stored(gh, cfg, number, pull)
    if not write or comment is None:
        answer(doc)
        return given()
    try:
        item_state.apply(
            gh, number, lambda state: answer(state), sleep=lambda wait: time.sleep(wait)
        )
    except item_state.Edited as edited:
        raise _edited(edited) from None
    except item_state.Moved:
        why = "other writes came first, so its answer is not stored"
        print(f"::warning::The state of #{number} moved: {why}.")
    return given()


def decide(
    gh,
    cfg,
    number,
    event,
    built,
    *,
    pull=None,
    found_policies=None,
    action=None,
    write=False,
    with_changed=False,
):
    """The engine's answer to `event` on work item `number`, as (state, actions): decided on
    the stored document where it agrees with `built`, the document that NexKit 6's markers
    leave for the decision (`resync`), and with `action`, (kind, stage), the id of the pending
    action that the event's result answers (`pending_id`). `found_policies` default to the
    configuration's.

    A run of the item's concurrency group passes `write`: when the item has a state comment,
    the answer is stored with `item_state.apply`, only on the revision that was read, and
    read and decided again after a wait when another write came first. When every try lost it
    warns and gives the answer anyway, which was not stored: the next decision stores its own,
    and failing the run would lose its report or its step. Without `write`, or with no state
    comment, which only `store_import` posts, the answer is a preview and nothing is stored.
    Raises `Refused` when the engine refuses or someone other than the App changed the
    state comment. With `with_changed` the answer is (state, actions, changed): whether the
    engine took the event as one that changes the state. `decide_all` sends several events in
    a row."""
    state, answers, changed = decide_all(
        gh,
        cfg,
        number,
        [event],
        built,
        pull=pull,
        found_policies=found_policies,
        action=action,
        write=write,
        with_changed=True,
    )
    if with_changed:
        return state, answers[0][1], changed
    return state, answers[0][1]


# -- what follows a plan -------------------------------------------------------------------


def after_plan(gh, cfg, issue, plan, run, record, start, comments, pull=None, write=False):
    """The step after the latest plan of `issue`, the comment `plan` that the run `run`
    posted, as the engine decides it: with `pull`, the plan autopilot asked for after a round
    of that pull request ran out of time. A collaborator's comment after an issue's plan comes
    first (`autopilot._assess`). `record` is autopilot's record and `start` what a command
    that the step starts begins from. With `write`, which only the run of the item's group
    passes, the engine's answer is stored (`decide`). Raises `Refused`."""
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
        if pull:
            # The plan that a round that ran out of time asked for: its split withdraws the change.
            doc["asked"].append("supersede")
        doc = _planning(doc, _depth(gh, issue, data), autopilot.asked_again(comments))
        event = _result(number, plan, data, run, change)
    action = None if said else ("runAgent", "plan")
    state, actions = decide(gh, cfg, number, event, doc, pull=pull, action=action, write=write)
    view = _PlanView(issue, plan, data, run, start, pull, picked, said)
    return view.carry(actions, doc["asked"], state["asked"])


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
            return self.split(kinds["createParts"], kinds.get("withdrawChange"))
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

    def split(self, create, withdraw):
        # The engine withdraws a change when it splits the item that has one, and only then.
        named = (withdraw or {}).get("changeId")
        if named != (str(self.pull) if self.pull else None):
            if not self.pull:
                raise Refused("The engine withdraws a change from an issue with no pull request.")
            why = f"The engine splits the issue without withdrawing pull request #{self.pull}."
            raise Refused(why)
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
            # The plan is too large, and the engine splits an item with a change only from the
            # plan that a round which ran out of time asked for.
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


def plan_carried_on(gh, cfg, issue, decision, output, ran=None, write=False):
    """Whether autopilot carries on after the plan `output` that a run of `decision` posted,
    as the engine decides what follows it, so that `report` mentions nobody. A plan that finds
    no change carries on whatever else it says. A recommended answer that only closes the
    issue needs a person, whom `report` mentions: the sweep finds that mention since the run
    (`autopilot.mentioned`), so it mentions nobody again. With `write`, which only `report`
    passes, the engine's answer is stored (`decide`). Raises `Refused`."""
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
    result = {**spent(ran), "output": plan_output({}, data, {})}
    event = _event(
        "actionFinished",
        number,
        None,
        _run_event_id(),
        action=first_state.RUN_ID,
        changeVersion=None,
        result=result,
    )
    _, actions = decide(gh, cfg, number, event, doc, action=("runAgent", "plan"), write=write)
    kinds = {action["kind"]: action for action in actions}
    if "runAgent" in kinds:
        return kinds["runAgent"]["stage"] == "plan"
    return "needPerson" not in kinds


def plan_waits_for(output, gh=None, cfg=None, number=None):
    """Why the plan `output` that a run posted waits for a person without automatic approval,
    as the engine's attention names it: `approval`, of a plan that is too large, finds no
    change or is clean, `handMadeChange` or `question`. A plan whose questions name no
    question still asks, as NexKit 6 reads it. With `gh`, `cfg` and the item's `number` it is
    decided on the item's stored document (`decide`), and it never stores: it asks what the
    plan waits for under no automatic approval, which is not the item's own answer. Raises
    `Refused`."""
    data = plan_record(output)
    found = plan_output({}, data, {})
    if output.get("questions") and not found["questions"]:
        found["questions"] = [{"question": "", "options": [], "recommended": 0, "closes": 0}]
    item = 0 if number is None else number
    doc = _planning(_document({"number": item}, {}), 0, False)
    result = {**AGENT, "output": found}
    event = _event("actionFinished", item, None, "plan", action=first_state.RUN_ID, **_NO_CHANGE)
    event = {**event, "result": result}
    if gh is None or cfg is None or number is None:
        answer = step(doc, event, {})["state"]
    else:
        action = ("runAgent", "plan")
        answer, _ = decide(gh, cfg, number, event, doc, found_policies={}, action=action)
    return (answer["attention"] or {}).get("reason")


_NO_CHANGE = {"changeVersion": None}


def new_document(issue, record):
    """The document of `issue` as the import builds it for an issue with no plan: `new`, waiting
    to start (`first_state.Item.place`)."""
    doc = _document(issue, record)
    doc["waits"] = [{"id": "w1.1", "for": "start", "until": None, "on": None}]
    return doc


def first_plan(gh, cfg, issue, record, start, write=False):
    """The step that starts the first plan of `issue`, which has none yet, as the engine decides
    it: autopilot's `requested` start of the plan, with no person behind it, on the new item's
    document. The plan runs only when the engine answers with a plan run; `start` is the step's
    fields. With `write`, the engine's answer is stored (`decide`). Raises `Refused`."""
    number = issue["number"]
    doc = new_document(issue, record)
    fields = {"what": "start", "by": None, "note": "", "plan": None, "title": doc["title"]}
    event = _event("requested", number, None, f"start-{number}", **fields)
    _, actions = decide(gh, cfg, number, event, doc, write=write)
    if any(a["kind"] == "runAgent" and a["stage"] == "plan" for a in actions):
        return autopilot._step(issue, "start", "no plan yet", command="plan", **start)
    raise Refused("The engine starts no plan for a new issue.")


VOUCHED = "a writer vouched for it again"


def vouched(gh, cfg, issue, comment, run, record, start, pull=None, write=False):
    """The step after a writer vouched for `issue` again once an untrusted edit stopped the
    round whose run comment `comment` holds the marker `run` (`autopilot._vouched_again`), as
    the engine decides it: the item is `failed` with attention `untrusted` for a round that
    stopped, or `ready` for a merge that did (`merge_untrusted`), with its pull request `pull`
    as the change, and the writer's newer vouch (`autopilot.vouch`) is a `requested` `trust`
    with role `writer`. A plan, an automatic fix round that counts, or the checks and review
    start only when the engine answers with them; a pull request with no automatic round left
    waits for a person. With `write`, the engine's answer is stored (`decide`). Raises
    `Refused`."""
    number = issue["number"]
    found = autopilot.vouch(gh, issue)
    if found is None or found["id"] <= comment["id"]:
        raise Refused("No writer vouched for the issue after the round that stopped.")
    said = str(run.get("attention") or "").strip()
    attention = {
        "reason": "untrusted",
        "text": first_state.clip(said, "reason") if said else first_state.SAYS["untrusted"],
        "ref": None,
    }
    if pull is None:
        doc = _plan_doc(issue, record, gh.comments(number))
        doc["state"] = "failed"
        if doc["plan"]:
            doc["plan"]["approved"] = True
    else:
        _, kept = autopilot.read_state(gh.comments(pull))
        merge = run.get("outcome") == autopilot.UNTRUSTED_MERGE
        auto_fixes = (kept or {}).get("auto_fixes", 0)
        name = "ready" if merge else "failed"
        doc = _pull_doc(None, number, pull, run.get("head"), name, auto_fixes)
    doc["attention"] = attention
    login = (found.get("user") or {}).get("login") or ""
    fields = {"what": "trust", "by": _person(login, "writer"), "note": "", "plan": None}
    at, name = found.get("created_at"), event_id("comment", found)
    event = _event("requested", number, at, name, **fields)
    _, actions = decide(gh, cfg, number, event, doc, pull=pull, write=write)
    kinds = {action["kind"]: action for action in actions}
    agent = kinds.get("runAgent")
    if agent and agent["stage"] == "plan" and pull is None:
        return autopilot._step(issue, "start", VOUCHED, command="plan", **start)
    if agent and agent["stage"] == "fix" and pull is not None:
        fields = {**start, "target": pull, "auto": "true", "command": "fix", "count": True}
        return autopilot._step(issue, "start", VOUCHED, **fields)
    if "runChecks" in kinds and pull is not None:
        fields = {**start, "target": pull, "auto": "true", "command": "review"}
        return autopilot._step(issue, "start", VOUCHED, **fields)
    if (kinds.get("needPerson") or {}).get("reason") == "roundsUsedUp" and pull is not None:
        do = f"comment `/nexkit fix` on pull request #{pull} for what still fails"
        why = f"{VOUCHED}, but it has no automatic fix rounds left"
        return autopilot._step(issue, "waits", autopilot.person(do, why))
    raise Refused("The engine starts nothing after a writer vouched for the item again.")


def sweep(cfg, where, starts, issues, running, awaited):
    """The start steps `starts` (no pull request, in `scope`'s order) that start, in the engine's
    order: its `sweep`, which orders them and keeps them within `autopilot.max_parallel`, with
    `running` issues in progress (`autopilot.preview`). The others become waits in NexKit 6's
    words. `issues` maps a number to its issue, and `awaited` a number to the pull requests that
    wait for it as a bug issue about the default branch. An engine that refuses the sweep leaves
    each issue that would start waiting for a person."""
    listed = [m["number"] for m in where.get("milestones") or []]
    items = []
    for one in starts:
        issue = issues[one["number"]]
        number = (issue.get("milestone") or {}).get("number")
        items.append(
            {
                "item": str(one["number"]),
                "labels": sorted(autopilot.labels_of(issue)),
                "milestone": listed.index(number) if number in listed else None,
                "wave": autopilot.wave(cfg, issue)[0],
                "awaited": bool(awaited.get(one["number"])),
                "account": None,
            }
        )
    request = {
        "protocol": 1,
        "call": "sweep",
        "policies": {
            **policies(cfg),
            "maxParallel": cfg["autopilot"]["max_parallel"],
            "priority": list(where["priority"]),
        },
        "running": running,
        "limited": [],
        "items": items,
    }
    found = engine.answer(json.loads(json.dumps(request)))
    if "error" in found:
        for one in starts:
            one.update(state="waits", reason=refused_reason(found["error"]), person=True)
        return []
    by_number = {str(one["number"]): one for one in starts}
    for wait in found["waits"]:
        reason = f"{wait['running']} issues are in progress (max_parallel)"
        by_number[wait["item"]].update(state="waits", reason=reason, person=False)
    started = []
    for start in found["starts"]:
        one = by_number[start["item"]]
        if start["beyondLimit"]:
            waiting = awaited[one["number"]]
            names = ", ".join(f"#{n}" for n in waiting)
            those = (
                f"pull requests {names} wait" if len(waiting) > 1 else f"pull request {names} waits"
            )
            one["reason"] += f"; {those} for it, so it starts beyond max_parallel"
        started.append(one)
    return started


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


def after_go(gh, cfg, issue, comment, run, record, start, comments, write=False):
    """The step after the `/nexkit go` of `issue` whose run comment `comment`, with the
    marker `run`, says it stopped as blocked, with questions or protected paths or with
    nothing to answer, or failed on an error and not on time (`autopilot.engine_round`,
    `autopilot._assess`), as the engine decides it: a writer's comment since that
    picks the option that only closes the issue closes it; otherwise the round's result,
    then a collaborator's comment since, or the close of an issue that blocked it after the
    round started. `record` is autopilot's record and `start` what
    a command that the step starts begins from. With `write`, which only the run of the item's
    group passes, the engine's answer is stored (`decide_all`). Raises `Refused`."""
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
        at, name = said[-1].get("created_at"), event_id("comment", said[-1])
        event = _event("answered", number, at, name, **fields)
        return view.carry(decide(gh, cfg, number, event, doc, write=write)[1])
    doc = _working(_plan_doc(issue, record, comments), autopilot.asked_again(comments))
    events = [_round_result(number, comment, run)]
    if said:
        # A collaborator, as `autopilot._commented_after` reads them.
        login = (said[-1].get("user") or {}).get("login") or ""
        text = {"name": "comment", "ref": str(said[-1]["id"])}
        at, name = said[-1].get("created_at"), event_id("comment", said[-1])
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
    _, answers = decide_all(
        gh,
        cfg,
        number,
        events,
        doc,
        action=("runAgent", "implement"),
        until=lambda acts: any(x["kind"] in ("runAgent", "close", "createParts") for x in acts),
        write=write,
    )
    actions = []
    for sent, found in answers:
        if found:
            actions = found
            view.by_event = sent["kind"]
    return view.carry(actions)


def _round_result(number, comment, run):
    """The end of the `/nexkit go` whose run comment `comment` holds the marker `run`: a
    blocked round's result, or for any other failed round an agent error, worded from NexKit's
    own marker and never from agent text."""
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
        action=first_state.RUN_ID,
        changeVersion=None,
        result=result,
    )


def _closed_since(gh, issue, comment):
    """{number: completed} of the issues that block `issue` and closed after the round of the
    run comment `comment` started."""
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
        elif reason in ("question", "blocked", "roundFailed"):
            text = self.run.get("attention") or autopilot.failed_reason("go")
        else:
            raise Refused(f"The engine waits for a person for {reason!r} after a stopped round.")
        return self.step("waits", text)


def _carries_on(actions):
    """Whether the engine's `actions` after a stopped round start a run or close the issue.
    Any wait for a person, the recommended answer that only closes the issue included, needs
    that person now, so `report` mentions them and the sweep does not (see
    `docs/engine.md#getting-there`)."""
    return any(action["kind"] in ("runAgent", "close") for action in actions)


def round_carried_on(gh, cfg, issue, decision, agent, ran=None, write=False):
    """Whether autopilot carries on after the `/nexkit go` that a run of `decision` ran and
    that stopped as blocked, with the agent's result `agent`, as the engine decides what
    follows it, so that `report` mentions nobody. A wait for a person, the closing answer
    included, makes `report` mention them. With `write`, which the `report` of the item's group
    passes, the answer is stored (`decide`). Raises `Refused`."""
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
        _run_event_id(),
        action=first_state.RUN_ID,
        changeVersion=None,
        result={**spent(ran), "output": round_output(run)},
    )
    _, actions = decide(
        gh,
        cfg,
        issue["number"],
        event,
        doc,
        action=("runAgent", "implement"),
        write=write,
    )
    return _carries_on(actions)


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


def _stop_event(number, name, doc, output, timed_out, ran=None):
    version = (doc["change"] or {}).get("version")
    result = {**spent(ran), "timedOut": timed_out, "output": None if timed_out else output}
    return _event(
        "actionFinished",
        number,
        None,
        name,
        action=first_state.RUN_ID,
        changeVersion=version,
        result=result,
    )


def stopped(gh, cfg, issue, run, record, start, comments, pull=None, pull_comments=(), write=False):
    """The step after a round that stopped, whose marker is `run`, as the engine decides it:
    with `pull`, a fix round of that pull request that stopped as blocked with questions or
    protected paths (`autopilot.engine_round`); or a fix round or a `/nexkit go` that ran out
    of time, after which autopilot asks once for a split. `comments` are the issue's,
    `pull_comments` the pull request's, `record` autopilot's record and `start` what a
    command that the step starts begins from. With `write`, which only the run of the item's
    group passes, the engine's answer is stored (`decide`). Raises `Refused`."""
    number = issue["number"]
    timed_out = bool(run.get("timed_out"))
    place = _split_place(gh, issue, record) if timed_out else (0, False)
    again = False
    if not timed_out:
        again = autopilot.asked_again(pull_comments, comments)
    doc = _round_doc(issue, record, again, pull, run.get("head"), place)
    event = _stop_event(number, _run_event_id(run.get("url")), doc, fix_output(run), timed_out)
    _, actions = decide(
        gh,
        cfg,
        number,
        event,
        doc,
        pull=pull,
        action=("runAgent", "fix" if pull else "implement"),
        write=write,
    )
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


def stop_carried_on(gh, cfg, issue, decision, agent, ran=None, write=False):
    """Whether autopilot carries on after a fix round of `decision` that stopped as blocked,
    or an implement or fix round that ran out of time, with the agent's result `agent`, as
    the engine decides what follows it, so that `report` mentions nobody. A wait for a person,
    the closing answer included, makes `report` mention them. With `write`, which the `report`
    of the item's group passes, the answer is stored (`decide`). Raises `Refused`."""
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
    name = _run_event_id()
    event = _stop_event(issue["number"], name, doc, output, timed_out, ran)
    _, actions = decide(
        gh,
        cfg,
        issue["number"],
        event,
        doc,
        pull=pr,
        action=("runAgent", "fix" if pr else "implement"),
        write=write,
    )
    return _carries_on(actions)


# -- a person's command ----------------------------------------------------------------


def closing_command(gh, decision, comments, command, cfg=None):
    """A writer's `/nexkit plan` or `/nexkit go` on an issue whose questions NexKit asked
    last, those of its latest plan or of a `/nexkit go` that stopped as blocked
    (`state.asked_last`): when its note picks the option that only closes the issue and the
    engine drops the issue for it, the `maintain` job closes the issue as not planned, with
    no agent run. Otherwise the command goes to its round as it is. `command` is the comment
    of the command, which names the event. With `cfg` the engine answers on the item's stored
    state (`decide`), which a preview only reads: `take` already stored the command's event, and
    the `maintain` job carries out the close. Without it, the answer is on the document that
    NexKit 6's markers leave."""
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
    event = _event("answered", number, None, event_id("comment", command), by=by, **fields)
    try:
        if cfg is None:
            actions = step(doc, event, {})["actions"]
        else:
            _, actions = decide(
                gh,
                cfg,
                number,
                event,
                doc,
                pull=int(change["id"]) if change else None,
                found_policies={},
                write=False,
            )
    except Refused:
        return decision
    if not any(action["kind"] == "close" for action in actions):
        # A pull request that is open stays with its issue: a person closes both.
        return decision
    return {
        "action": "maintain",
        "task": "close_answer",
        "issue": number,
        "answer": picked[2],
        "asked": asked[0],
    }


# What the engine's answer to a person's command names, by the stage it runs.
STARTS = {"plan": "plan", "implement": "implement", "fix": "fix"}


def starts(actions):
    """The stage that the engine's `actions` start, as `STARTS` names it (`review` for
    `runChecks`), or None when they start none."""
    for action in actions:
        if action["kind"] == "runChecks":
            return "review"
        if action["kind"] == "runAgent" and action["stage"] in STARTS:
            return STARTS[action["stage"]]
    return None


def command(
    cfg,
    item,
    what,
    *,
    gh=None,
    taken=None,
    by=None,
    note="",
    closed=False,
    change=None,
    plan=None,
    review_only=False,
    empty=False,
    review_id=None,
    source_id=None,
):
    """What the engine answers to a person's command `what` (`plan`, `go`, `fix` or `review`)
    on work item `item` (`route._decide`): `plan`, `implement`, `fix` or `review` for the stage
    it runs (`runChecks` is a `review`), `noChange` when the item ends because its base branch
    already holds the change, `refused` when it runs nothing, or `answered` when an earlier run
    already took the command, so that this one starts nothing and replies nothing. It raises
    `Refused` when the engine refuses the document or the event.

    With `gh` and `cfg`, the command is carried out from the item's stored state. When
    `take` answered it in this run (`taken`, [(event, actions)] as `take` gives them), the
    actions it stored are carried out and the engine is not asked again, which it would take for a
    duplicate. Otherwise the stored state says whether an earlier run took it (`answered`), and
    another command taken in this run that already started a stage refuses it. In any other case it
    is decided on the stored state (`decide`, which stores the answer in the item's group,
    `resync`). A closed item, with no `gh` or no `cfg`, is answered on the small document that
    NexKit 6's `route` read the facts of, which writes nothing.

    That document holds only the facts that NexKit 6's `route` reads, as `closing_command` and
    `_pull_doc` build theirs: `closed` is a closed issue or pull request (`dropped`); `change`,
    `{"id", "version"}` of the open pull request, makes the item `ready`, or `new` for a person's
    pull request (`review_only`); `plan` is the issue's latest plan comment, which makes the item
    wait for its approval. `empty` says that the change's base branch already holds it. `by` is
    the person, `note` their text, and `review_id` the id of a *Request changes* review, which is
    sent as `changesRequested` as `take` sends it. `source_id` is the id of the comment or review
    that gave the command, which names the event as `take` does."""
    doc = first_state.empty(str(item))
    if review_only:
        doc["itemKind"], doc["actors"]["change"] = "reviewOnly", "person"
    if closed:
        doc["state"] = "dropped"
    elif change:
        doc["state"] = "new" if review_only else "ready"
        doc["change"] = dict(change)
        doc["observed"] = {
            "changeId": change["id"],
            "version": change["version"],
            "conflicts": False,
            "merged": False,
            "empty": False,
        }
    elif plan:
        doc = _waiting(_document({"number": item}, {}, plan, plan_data(plan)))
    observed = None
    if empty and change and not closed:
        seen = {"version": change["version"], "merged": False, "empty": True}
        name = f"seen-{change['id']}-{change['version']}"
        fields = {"changeId": change["id"], "conflicts": False, **seen}
        observed = _event("changeObserved", item, None, name, **fields)
    kind = "review" if review_id is not None else "comment"
    sourced = True
    try:
        name = event_id(kind, review_id if review_id is not None else source_id)
    except ValueError:
        # NexKit's own dispatch has no source, and sends the same command again and again.
        name, sourced = f"command-{item}-{what}", False
    person = _person(by, "writer")
    if review_id is not None:
        found = {"by": person, "note": {"name": "review", "ref": str(review_id)}}
        request = _event("changesRequested", item, None, name, **found)
    else:
        found = {
            "what": what,
            "by": person,
            "note": note,
            "plan": str(plan["id"]) if plan and what == "go" else None,
        }
        request = _event("requested", item, None, name, **found)
    if gh is None or cfg is None or closed:
        events = [event for event in (observed, request) if event]
        after, actions = _answers(doc, events, policies(cfg) if cfg else {})
        if after["state"] == "noChange":
            return "noChange"
        return starts(actions) or "refused"
    pull = int(change["id"]) if change else None
    mine = [actions for event, actions in taken or [] if event["id"] == name]
    if mine:
        if observed and _on_base(gh, cfg, item, observed, doc, pull):
            return "noChange"
        return starts(mine[0]) or "refused"
    _, seen_doc = stored(gh, cfg, item, pull)
    if sourced and seen_doc is not None and name in seen_doc["recentEvents"]:
        return "answered"
    if any(
        event["kind"] in ("requested", "changesRequested") and starts(actions)
        for event, actions in taken or []
    ):
        return "refused"
    if observed and _on_base(gh, cfg, item, observed, doc, pull):
        return "noChange"
    _, actions = decide(gh, cfg, item, request, doc, pull=pull, write=True)
    return starts(actions) or "refused"


def _on_base(gh, cfg, item, observed, doc, pull):
    """Whether the engine ends the item as `noChange` on the observation `observed` that its
    base branch already holds the change (`command`)."""
    state, _ = decide(gh, cfg, item, observed, doc, pull=pull, write=True)
    return state["state"] == "noChange"


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
    ran=None,
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
        cfg,
        decision,
        pr,
        head,
        auto_fixes,
        checks,
        before,
        ("done", review),
        recheck,
        wait,
        label,
        ran,
    )
    if seen is not None:
        seen["repeated"] = repeated
    return end


# What `report` does after a round's review that is not a verdict on a NexKit pull request,
# by what the engine answers: a person's own pull request reviewed, a review paused at a
# usage limit or failed.
REVIEW_ENDS = ("person_reviewed", "paused", "review_failed")


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
    ran=None,
    now=None,
):
    """What follows the checks and the review of the round of `decision` on the pull request
    `pr`, which ended on `head`, as the engine decides it: (end, repeated). `review` is
    (`done`, the review NexKit acts on), (`paused`, the time the host runs it again after
    its usage limit resets, or None when it does not), (`failed`, why), or (`transient`, the
    error) for a review that stopped on a transient error at `now`, which the engine runs
    again, makes wait or leaves to a person as `stage_transient` does, counting from the
    decision's runs again. `before` are the checks of the round before, as the
    state comment recorded them (`checks.recorded`). The end is one of `ROUND_ENDS` after a
    verdict on a NexKit pull request, `stage_transient`'s answer after a transient stop,
    else one of `REVIEW_ENDS`; `repeated` names the checks
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
    failing = first_state.failing(before)
    if failing:
        doc["rounds"]["failing"] = failing
    if decision["action"] == "fix":
        # The checks of a fix round compare with the round's before it.
        doc["rounds"]["fixed"] = head
    run = {"id": first_state.RUN_ID, "next": None, "kind": "runChecks", "changeVersion": head}
    doc["pending"] = [run]
    kind, given = review
    retry = int(decision.get("retry") or 0)
    if kind == "transient" and retry > 0:
        # The runs again of the round's command, which a review handed on keeps counting.
        doc["again"] = {"transient": min(retry, MOST_AGAIN)}
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
    if kind == "done":
        result = {**spent(ran), "output": _review_output(given)}
    elif kind == "paused":
        message = "The review stopped at its usage limit."
        result = {
            **spent(ran),
            "output": None,
            "limit": {"message": message, "reset": _time(given)},
        }
    elif kind == "transient":
        result = {**spent(ran), "output": None, "transient": True, "error": str(given)}
    else:
        result = {**spent(ran), "output": None, "error": str(given or "The review failed.")}
    event = _event(
        "actionFinished",
        number,
        usage.iso(now) if kind == "transient" and now else None,
        f"round-review-{pr}-{head}",
        action=review_run[0]["id"],
        changeVersion=head,
        result=result,
    )
    answer = step(answer["state"], event, found)
    if kind == "transient":
        return _transient_answer(answer["state"], answer["actions"], "review"), _repeated(answer)
    if kind == "done" and not person:
        return _round_end(answer, recheck), _repeated(answer)
    return _review_end(answer, kind, person), _repeated(answer)


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


# -- a move of the base branch -----------------------------------------------------------


def base_moved(cfg, issue, pr, head, conflicts, behind, checking, used, updates):
    """What follows the move of the base branch under the NexKit pull request `pr` of `issue`,
    whose head is `head`, as the engine decides it (`maintain.resolve_conflicts`,
    `maintain.stop_superseded`, `report._conflicts_after`): `conflicts` says that the head
    conflicts with the base branch's tip, and `behind` that it lacks the tip while the base
    branch's rules want pull requests up to date, which only NexKit 6's reading of them says.
    `checking` says that a round runs only its checks, so the pull request is in `checking`
    with those checks pending; otherwise it is in `ready`. `used` is the conflict rounds used,
    and `updates` the updates of the branch used, which `conflicts.max_rounds` limits apart from
    each other. Returns `round`, the round that resolves the conflict; `update`, an update of
    the branch; or `waits`, when a person decides or the checks that run go on to their end.
    A hold is read before, as NexKit 6 does. Raises `Refused`."""
    doc = _pull_doc(cfg, issue, pr, head, "checking" if checking else "ready", 0)
    version = doc["change"]["version"]
    doc["rounds"]["conflicts"] = max(int(used or 0), 0)
    if int(updates or 0) > 0:
        doc["rounds"]["updates"] = int(updates)
    if checking:
        run = {"id": first_state.RUN_ID, "next": None, "kind": "runChecks"}
        doc["pending"] = [{**run, "changeVersion": version}]
    seen = {"changeId": str(pr), "version": version, "conflicts": bool(conflicts)}
    seen.update(merged=False, empty=False)
    if behind:
        seen["behind"] = True
    event = _event("changeObserved", issue, None, f"base-{pr}-{version}", **seen)
    answer = step(doc, event, policies(cfg))
    kinds = {action["kind"]: action for action in answer["actions"]}
    if (kinds.get("runAgent") or {}).get("stage") == "fix":
        return "round"
    if "updateChange" in kinds:
        return "update"
    if not kinds or "needPerson" in kinds:
        return "waits"
    raise Refused(f"The engine answers {sorted(kinds)} after a base branch moved.")


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
    For a held merge, `autopilot._released` uses the answer only to guard against a refusal,
    while `_merge_step` reads the head and merges. Raises `Refused`."""
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


def head_observed(issue, pr, head):
    """The event that tells the engine the NexKit pull request `pr` has the head `head` now,
    for `accept_answer`'s `before`."""
    fields = {"changeId": str(pr), "version": str(head), "conflicts": False}
    fields.update(merged=False, empty=False)
    return _event("changeObserved", issue, None, f"head-{pr}-{head}", **fields)


def accept_answer(
    cfg,
    issue,
    pr,
    head,
    auto_fixes,
    result,
    before=(),
    since=None,
    at=None,
    conflicts=0,
    updates=0,
):
    """What follows the answer of GitHub's merge of the NexKit pull request `pr`, as the engine
    decides it (`report._refused`): the accept's `result`, as `accepted` or with its refusal
    kind, sent after the events `before`, which tell the engine what the host read, such as a
    head that moved. `since`, when given, is when required checks first had not passed on
    `head`, as NexKit 6's `waiting_since` says, and `at` the time of the events. `conflicts`
    is the conflict rounds used, which `conflicts.max_rounds` limits, and `updates` the updates of
    the branch from its base, which it limits apart from them. Returns `(end, state)`, `end`
    being `accepted`, `waiting` while the required checks are awaited, `review` for the checks
    and review of a newer head that `before` observed, `fix` or `redo` for the round that
    follows, `update` for the update of a branch that is behind its base, `base_failure` while a
    failure the base branch has is awaited, or `person`, whose reason `state["attention"]` names.
    Raises `Refused`."""
    doc = _pull_doc(cfg, issue, pr, head, "accepting", auto_fixes)
    doc["rounds"]["conflicts"] = max(int(conflicts or 0), 0)
    if int(updates or 0) > 0:
        doc["rounds"]["updates"] = int(updates)
    version = doc["change"]["version"]
    action = {"id": first_state.RUN_ID, "next": None, "kind": "accept"}
    doc["pending"] = [{**action, "changeVersion": version, "by": "engine"}]
    if since:
        doc["checksSince"] = {"version": version, "at": str(since)}
    events = [{**event, "at": at or event["at"]} for event in before]
    fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
    events.append(_event("actionFinished", issue, at, f"accept-{pr}-{version}", **fields))
    state, actions = _answers(doc, events, policies(cfg))
    kinds = {action["kind"]: action for action in actions}
    waits = {wait["for"] for wait in state["waits"]}
    stage = (kinds.get("runAgent") or {}).get("stage")
    if state["state"] == "accepted":
        return "accepted", state
    if "updateChange" in kinds:
        return "update", state
    if state["state"] == "accepting" and "requiredChecks" in waits:
        return "waiting", state
    if "runChecks" in kinds:
        return "review", state
    if stage == "fix":
        return "fix", state
    if stage == "implement":
        return "redo", state
    if "baseFailure" in waits:
        return "base_failure", state
    if "needPerson" in kinds:
        return "person", state
    raise Refused(f"The engine answers {sorted(kinds)} after a merge, which the host cannot do.")


def approved(gh, cfg, issue, pr, run, commit, actor, write=False):
    """What a writer's approval of the NexKit pull request `pr` of `issue` does, as the engine
    decides it (`maintain.send_approval`, `maintain.merge_approved`): `run` is the marker of
    the pull request's latest round, which passed, and `commit` the commit that the approval
    names, or None. The document is the one the import builds for that round: `ready` with
    attention `acceptRefused` after a merge GitHub refused, `reopened` after a pull request
    reopened, `personChecklistItem` while items that a person decides are not met, and `accept`
    for a round that passed and left its accept to a person. Returns `merge`, when the engine
    accepts the change; `accept`, when it takes the approval as a person's decision of the
    checklist items and leaves the accept to them; or `none`, when it changes nothing, as for
    an approval of another version of a change that waits for its accept, or with `merge.auto`
    off. The engine answers on the item's stored state (`decide`). Only the `step` run in the
    item's group passes `write`, which stores the answer; the approval's own run in its group
    `approved-N` only previews. Raises `Refused`."""
    decide_items = bool(run.get("decide"))
    doc = _pull_doc(cfg, issue, pr, run.get("head"), "ready", 0)
    if decide_items:
        reason = "personChecklistItem"
        doc["rounds"]["checklist"] = ["not_met"]
    elif run.get("outcome") == "merge_refused":
        reason = "acceptRefused"
    elif run.get("outcome") == autopilot.PULL_CLOSED:
        reason = "reopened"
    else:
        reason = "accept"
    doc["attention"] = {"reason": reason, "text": first_state.SAYS[reason], "ref": None}
    version = str(commit or doc["change"]["version"])
    fields = {"by": _person(actor or "someone", "writer"), "changeId": str(pr)}
    fields["changeVersion"] = version
    event = _event("changeApproved", issue, None, f"approval-{pr}-{version}", **fields)
    state, actions, changed = decide(
        gh, cfg, issue, event, doc, pull=pr, write=write, with_changed=True
    )
    kinds = {action["kind"] for action in actions}
    if not changed:
        return "none"
    if "accept" in kinds:
        return "merge"
    if decide_items and (state["attention"] or {}).get("reason") == "accept":
        return "accept"
    raise Refused(f"The engine answers {sorted(kinds)} to an approval, which the host cannot do.")


def checks_waited(cfg, issue, pr, head, since, now, behind=False):
    """What follows a sweep's tick of the merge of the NexKit pull request `pr` that waits for
    required checks since `since` on `head`, which is None when no start was recorded, as the
    engine decides it (`autopilot._merge_step`): `merge`, to try it again, or `waits` for a
    person once the checks have not passed for more than six hours. A `behind` branch keeps
    being tried under `conflicts.auto_resolve`, as the engine decides. Raises `Refused`."""
    doc = _pull_doc(cfg, issue, pr, head, "accepting", 0)
    version = doc["change"]["version"]
    if behind:
        doc["observed"]["behind"] = True
    if since is not None:
        doc["checksSince"] = {"version": version, "at": usage.iso(since)}
    doc["waits"] = [{"id": "w1.1", "for": "requiredChecks", "until": usage.iso(now), "on": None}]
    event = _event("tick", issue, usage.iso(now), f"merge-{pr}-{version}", wait="w1.1")
    answer = step(doc, event, policies(cfg))
    kinds = {action["kind"]: action for action in answer["actions"]}
    if "accept" in kinds:
        return "merge"
    if (kinds.get("needPerson") or {}).get("reason") == "acceptRefused":
        return "waits"
    raise Refused(f"The engine answers {sorted(kinds)} after a merge's wait, which it cannot do.")


def refusal_cleared(cfg, issue, pr, run, acceptable):
    """What follows a sweep's look at the NexKit pull request `pr` whose merge GitHub refused,
    the marker `run` (`autopilot._refused_step`), as the engine decides it: `merge` when the
    engine accepts it again, once `acceptable` says GitHub would accept the head `run` names
    now (`mergeable_state` `clean`, `unstable` or `has_hooks`), or `waits` when it asks for
    nothing, such as when GitHub would not, or when that head was refused while it was
    acceptable (`refused_clean`), so that each head is tried once. The accept policy decides
    nothing here, as in NexKit 6. Raises `Refused`."""
    head = str(run["head"])
    doc = _pull_doc(cfg, issue, pr, head, "ready", 0)
    text = first_state.SAYS["acceptRefused"]
    doc["attention"] = {"reason": "acceptRefused", "text": text, "ref": None}
    if run.get("refused_clean") == run["head"]:
        doc["refusedAcceptable"] = doc["change"]["version"]
    fields = {"changeId": str(pr), "version": head, "conflicts": False}
    fields.update(merged=False, empty=False)
    if acceptable:
        fields["acceptable"] = True
    event = _event("changeObserved", issue, None, f"cleared-{pr}-{head}", **fields)
    answer = step(doc, event, policies(cfg))
    kinds = {action["kind"] for action in answer["actions"]}
    if "accept" in kinds:
        return "merge"
    if not kinds:
        return "waits"
    raise Refused(f"The engine answers {sorted(kinds)} after a refused merge, which it cannot do.")


def update_failed(cfg, issue, pr, head, auto_fixes, why, updates=0):
    """What follows an update of the NexKit pull request `pr`'s branch, which the engine asked
    for and GitHub refused, as it decides it (`report._update_branch`): `why` is GitHub's
    answer, `updates` the updates used before it. The engine hands the merge to a person: this
    returns `person`. Raises `Refused`."""
    doc = _pull_doc(cfg, issue, pr, head, "accepting", auto_fixes)
    if int(updates or 0) > 0:
        doc["rounds"]["updates"] = int(updates)
    version = doc["change"]["version"]
    action = {"id": first_state.RUN_ID, "next": None, "kind": "updateChange"}
    doc["pending"] = [{**action, "changeVersion": version}]
    result = {"change": None, "refused": str(why), "observed": None}
    fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
    event = _event("actionFinished", issue, None, f"update-{pr}-{version}", **fields)
    _, actions = _answers(doc, [event], policies(cfg))
    reasons = {action["kind"]: action for action in actions}
    if (reasons.get("needPerson") or {}).get("reason") == "acceptRefused":
        return "person"
    raise Refused(f"The engine answers {sorted(reasons)} after a failed update.")


def change_seen(cfg, issue, pr, head, auto_fixes, seen, waiting=False):
    """What follows what the host found about the NexKit pull request `pr` at its merge, as the
    engine decides it (`report.merge_pull`, `autopilot._merge_step`): `seen` is the observation
    without its change and its conflict, the pull request's `version`, and whether it was
    `merged` by a person or became `empty` because its base branch already holds the change.
    NexKit never tried the merge then, so the merge has no result to send. `waiting` says that
    the merge waits for required checks. Returns `accepted` or `noChange` when the item ended,
    and `review` when the engine checks the version that a person pushed. Raises `Refused`."""
    doc = _pull_doc(cfg, issue, pr, head, "accepting", auto_fixes)
    version = doc["change"]["version"]
    if waiting:
        doc["waits"] = [{"id": "w1.1", "for": "requiredChecks", "until": None, "on": None}]
    else:
        action = {"id": first_state.RUN_ID, "next": None, "kind": "accept"}
        doc["pending"] = [{**action, "changeVersion": version, "by": "engine"}]
    name = f"seen-{pr}-{seen['version']}"
    event = _event("changeObserved", issue, None, name, changeId=str(pr), conflicts=False, **seen)
    state, actions = _answers(doc, [event], policies(cfg))
    kinds = {action["kind"] for action in actions}
    if state["state"] in ("accepted", "noChange"):
        return state["state"]
    if "runChecks" in kinds:
        return "review"
    raise Refused(f"The engine answers {sorted(kinds)} to what the merge found.")


def no_change_found(gh, cfg, decision, finding, write=False):
    """`noChange` when the engine ends the item of `decision`, an implement round whose agent
    found that no change is needed, with `finding`, where the issue or its plan says to stop
    then (`report._no_change`): the host sends the round's result, which holds the finding, and
    closes the issue only as the engine ends the item. With `write`, which only the `report` of
    the item's group passes, the engine's answer is stored in the item's state (`decide`).
    Raises `Refused` for any other answer, and for an item that is not final."""
    number = decision["issue"]
    doc, _ = _stage_doc("implement", number, None, int(decision.get("retry") or 0))
    output = {**round_output({}), "status": "done", "no_change": finding}
    result = {**AGENT, "changeVersion": None, "output": output}
    fields = {"action": first_state.RUN_ID, "changeVersion": None, "result": result}
    event = _event("actionFinished", number, None, _run_event_id(), **fields)
    state, actions = decide(
        gh, cfg, number, event, doc, action=("runAgent", "implement"), write=write
    )
    if state["state"] == "noChange" and any(action["kind"] == "close" for action in actions):
        return "noChange"
    kinds = sorted({action["kind"] for action in actions})
    raise Refused(f"The engine answers {kinds} to an implement round that found no change.")


def on_base_found(gh, cfg, decision, write=False):
    """`noChange` when the engine ends the item of `decision`, a fix round that found, before
    its agent, that its base branch already holds the pull request's change
    (`report._already_on_base`): the host sends the observation that the change is empty, as
    the merge does (`change_seen`), but while the fix round's agent is pending, and closes the
    pull request and its issue only as the engine ends the item. With `write`, which only the
    `report` of the item's group passes, the engine's answer is stored in the item's state
    (`decide`). Raises `Refused` for any other answer."""
    number, pr, head = decision["issue"], decision["pr"], decision.get("head")
    change = {"id": pr, "version": head or pr}
    doc, version = _stage_doc("fix", number, change, int(decision.get("retry") or 0))
    doc["observed"] = {
        "changeId": str(pr),
        "version": version,
        "conflicts": False,
        "merged": False,
        "empty": False,
    }
    seen = {"conflicts": False, "version": version, "merged": False, "empty": True}
    name = f"onbase-{pr}-{version}"
    event = _event("changeObserved", number, None, name, changeId=str(pr), **seen)
    state, actions = decide(gh, cfg, number, event, doc, pull=pr, write=write)
    if state["state"] == "noChange" and any(action["kind"] == "close" for action in actions):
        return "noChange"
    kinds = sorted({action["kind"] for action in actions})
    raise Refused(f"The engine answers {kinds} to a change that its base branch already holds.")


def part_ended(gh, cfg, parent, subs, part, state, write=False):
    """Whether the parent issue `parent` ends once its sub-issue `part`, which closed as
    `state`, ended, as the engine decides it (`maintain.close_parent`, at every level and for
    every way a part closes): `close` when every one of the sub-issues `subs` ended and one
    was completed, under `parentEnd`, which GitHub gives as `person` without
    `close_parent_issues`; `person` when every part ended and a person decides; otherwise
    `stays`. Facts only GitHub knows, such as parts that were never filed, stay with
    `maintain.close_parent`. A parent with more sub-issues than a state holds sends a reduced
    list: the part, and one open, one completed and one dropped sub-issue, which keep the two
    facts the rule reads, that every part ended and that one was completed. With `write`,
    which only the `step` run of the parent's group passes (`maintain.record_part_end`), the
    engine's answer is stored in the parent's state, when it has a state comment (`decide`);
    the run of the part never does. Raises `Refused`."""
    ended = {str(sub["number"]): first_state._ended(sub) for sub in subs}
    # The part is one of them, though a list that GitHub has not caught up with may miss it.
    ended[str(part)] = None
    if len(ended) > MOST["split"]:
        ended = _reduced(ended, str(part))
    doc = first_state.empty(str(parent))
    doc["state"] = "split"
    doc["waits"] = [{"id": "w1.1", "for": "parts", "until": None, "on": None}]
    doc["parts"] = [{"item": item, "state": how} for item, how in ended.items()]
    end = "whenPartAccepted" if cfg["close_parent_issues"] else "person"
    found = {**policies(cfg), "parentEnd": end}
    event = _event("partEnded", parent, None, f"part-{parent}-{part}", part=str(part), state=state)
    _, actions = decide(gh, cfg, parent, event, doc, found_policies=found, write=write)
    kinds = {action["kind"] for action in actions}
    if "close" in kinds:
        return "close"
    return "person" if "needPerson" in kinds else "stays"


def _reduced(ended, part):
    """The parts `ended` (item to state) cut to `MOST["split"]`, with `part` first, then the
    first open one, the first completed and the first dropped, then the others in order. The
    engine's rule reads only whether every part ended and whether one was completed, and the
    list keeps both."""
    keep = [part]
    for wanted in (None, "accepted", "dropped"):
        keep += [item for item, how in ended.items() if how == wanted and item not in keep][:1]
    keep += [item for item in ended if item not in keep]
    return {item: ended[item] for item in keep[: MOST["split"]]}


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


# -- a stage that stops at a usage limit or on a transient error, and its resume ------------

# The stage that a resume record's command runs again.
RESUMED = {"plan": "plan", "go": "implement", "fix": "fix", "review": "review"}
# The most runs again the protocol counts (`again.transient` in state.schema.json).
MOST_AGAIN = 10


def _stage_doc(stage, item, change, retry):
    """(document, change version) of `item` while the agent of `stage` runs on `change` (a
    pull request's id and version, or None), with `retry` runs again after a transient error
    counted, as `first_state` imports a run that waits to run again."""
    doc = first_state.empty(str(item))
    doc["state"] = RUNS[stage]
    version = None
    if change:
        version = str(change["version"])
        doc["change"] = {"id": str(change["id"]), "version": version}
    doc["pending"] = [first_state.agent_run(stage, version)]
    if retry > 0:
        doc["again"] = {"transient": min(retry, MOST_AGAIN)}
    return doc, version


def _stopped_doc(decision):
    """(document, change version) of the plan, implement or fix stage of `decision` while its
    agent runs."""
    pr = decision.get("pr")
    change = {"id": pr, "version": decision.get("head") or pr} if pr else None
    retry = int(decision.get("retry") or 0)
    return _stage_doc(decision["action"], decision["issue"], change, retry)


def _hold_events(number, label):
    return [_event("held", number, None, f"held-{number}", label=str(label))] if label else []


def stage_limited(gh, cfg, decision, reset, label=None, write=False):
    """Until when the run of `decision`, a plan, implement or fix stage whose agent stopped at
    the usage limit, waits to run again as the engine decides it (`report.pause`), or None
    when it leaves the run to a person. `reset` is the time the host gives, or None when it
    does not resume the run, and `label` the label that holds the issue, if any. With `write`,
    which the `report` of the item's group passes, the answer is stored (`decide_all`).
    Raises `Refused`."""
    number = decision["issue"]
    doc, version = _stopped_doc(decision)
    message = "The agent stopped at its usage limit."
    limit = {"message": message, "reset": _time(reset)}
    result = {**AGENT, "changeVersion": version, "output": None, "limit": limit}
    fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
    events = _hold_events(number, label)
    events.append(_event("actionFinished", number, None, _run_event_id(), **fields))
    _, answers = decide_all(
        gh,
        cfg,
        number,
        events,
        doc,
        pull=decision.get("pr"),
        action=("runAgent", decision["action"]),
        write=write,
    )
    kinds = {action["kind"]: action for _, actions in answers for action in actions}
    if "wait" in kinds:
        return kinds["wait"]["until"]
    if (kinds.get("needPerson") or {}).get("reason") == "failed":
        return None
    raise Refused(f"The engine answers {sorted(kinds)} after a usage limit, which it cannot do.")


def stage_transient(gh, cfg, decision, now, error, label=None, write=False):
    """What follows the transient stop of the agent of `decision`, a plan, implement or fix
    stage, or of its publish, which goes as the same stop, at `now` (`report._retry`), as the
    engine decides it: `("now", count)` to run it
    again at once, `("later", until, count)` after the next wait of `retry.wait_minutes`, or
    `("failed",)` for a person after the last. `count` is the number of the run again. With
    `write`, which the `report` of the item's group passes, the answer is stored
    (`decide_all`). Raises `Refused`."""
    number = decision["issue"]
    doc, version = _stopped_doc(decision)
    result = {**AGENT, "changeVersion": version, "output": None, "transient": True}
    result["error"] = str(error)
    fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
    events = _hold_events(number, label)
    events.append(_event("actionFinished", number, usage.iso(now), _run_event_id(), **fields))
    state, answers = decide_all(
        gh,
        cfg,
        number,
        events,
        doc,
        pull=decision.get("pr"),
        action=("runAgent", decision["action"]),
        write=write,
    )
    actions = [action for _, found in answers for action in found]
    return _transient_answer(state, actions, decision["action"])


def _transient_answer(state, actions, stage):
    """What the engine answers after the transient stop of `stage`'s agent, from the `state`
    and `actions` it gave: `("now", count)`, `("later", until, count)` or `("failed",)` as
    `stage_transient` says. Raises `Refused`."""
    kinds = {action["kind"]: action for action in actions}
    count = (state.get("again") or {}).get("transient")
    if (kinds.get("runAgent") or {}).get("stage") == stage and count:
        return "now", count
    if "wait" in kinds and count:
        return "later", kinds["wait"]["until"], count
    # An implement round on an item with no change ends in `roundFailed`.
    if (kinds.get("needPerson") or {}).get("reason") in ("failed", "roundFailed"):
        return ("failed",)
    raise Refused(f"The engine answers {sorted(kinds)} after a transient stop, which it cannot do.")


def resumes(gh, cfg, run, number, now, label=None, write=False):
    """Whether the engine runs again the command of the resume record of `run`, which `number`
    paused at the usage limit or which waits to run again after a transient error, at `now`
    (`maintain.resume_one`): True to start it, which a hold on the issue, the `label`, does
    not stop. With `write`, which the `step` run of the item's group passes, the answer is
    stored and the wait is gone from the stored document (`decide_all`); the `resume` run
    only previews it. With no `cfg` it decides on the markers' document alone and reads and
    stores nothing. Raises `Refused`, for a command that NexKit never writes too."""
    resume = run["resume"]
    command = str(resume.get("command") or "")
    if command not in RESUMED:
        raise Refused(f"NexKit writes no resume of the command `{command}`.")
    stage = RESUMED[command]
    target = str(resume.get("number") or number)
    # A fix round and a review run on their pull request.
    change = {"id": target, "version": target} if stage in ("fix", "review") else None
    retry = resume.get("retry")
    retry = retry if isinstance(retry, int) and not isinstance(retry, bool) else 0
    doc, _ = _stage_doc(stage, number, change, retry)
    doc["pending"] = []
    until = _time(resume.get("at")) or NO_TIME
    kind = "time" if retry else "usageLimit"
    doc["waits"] = [{"id": "w1.1", "for": kind, "until": until, "on": None}]
    events = _hold_events(number, label)
    events.append(_event("tick", number, usage.iso(now), f"resume-{number}", wait="w1.1"))
    pull = int(target) if stage in ("fix", "review") and target.isdigit() else None
    pull = None if pull == number else pull
    _, answers = decide_all(gh, cfg, number, events, doc, pull=pull, wait=kind, write=write)
    if any(
        action["kind"] in ("runAgent", "runChecks") for _, actions in answers for action in actions
    ):
        return True
    raise Refused("The engine starts nothing at a resume, which the host cannot do.")


# -- a command that autopilot started again after it was lost -------------------------------


def lost_again(
    gh, cfg, issue, record, now, what, fields, extra=None, person=False, label=None, write=False
):
    """The step for a command whose run GitHub never started, or that ended without a report
    (`autopilot._again`, `_reportless`, `_handed_on`), as the engine decides it, or None for a
    person's command whose comment already shows its end. `what` says which loss it is, `fields`
    the command's start, `record` autopilot's record of its last start, or {} when none counts,
    and `extra` what the step carries besides. With `person`, the command is a person's, which
    waits for them. With `write`, which the `step` run of the item's group passes, the answer is
    stored (`decide_all`), the due tick of a wait that has already ended as a second answer.
    Raises `Refused`.

    The loss is sent at the time of the last start, as the engine measures a wait from the loss
    and NexKit 6 from that start, and the host keeps nothing that a later sweep could count
    from. The agent's tokens are not sent: the record's `once` says that the agent ran."""
    extra = extra or {}
    command = str(fields["command"])
    if command not in RESUMED:
        raise Refused(f"NexKit writes no start of the command `{command}`.")
    number, shown = issue["number"], f"`/nexkit {command}`"
    tries = autopilot._tries(record)
    doc, version = _lost_doc(RESUMED[command], number, fields["target"])
    if person:
        doc["pending"][0]["by"] = "person"
    if tries >= 1:
        doc["again"] = {"lost": min(tries, MOST_AGAIN)}
    if record.get("once"):
        doc.setdefault("again", {})["once"] = True
    found = {
        **(policies(cfg) if cfg else {}),
        "lostWaits": [int(wait.total_seconds() // 60) for wait in autopilot.AGAIN],
    }
    lost = _event(
        "actionLost",
        number,
        _time(record.get("at")) or usage.iso(now),
        f"lost-{number}-{tries}",
        action=first_state.RUN_ID,
        changeVersion=version,
        tokens=None,
        seconds=None,
    )
    # The pull request that the command runs on; a start that names the issue itself has none.
    target = int(fields["target"]) if str(fields["target"]).isdigit() else number
    pull = target if command in ("fix", "review") and target != number else None
    stage = RESUMED[command]
    action = ("runChecks", None) if stage == "review" else ("runAgent", stage)
    options = {"pull": pull, "found_policies": found, "write": write}
    state, answers = decide_all(
        gh, cfg, number, [*_hold_events(number, label), lost], doc, action=action, **options
    )
    actions = [item for _, found_actions in answers for item in found_actions]
    kinds = {action["kind"]: action for action in actions}
    if "wait" in kinds:
        due = autopilot.parse_iso(kinds["wait"]["until"])
        if due > now:
            reason = (
                f"{what}; autopilot starts {shown} again after {due:%H:%M} UTC, since GitHub "
                "may not start runs for a while"
            )
            return autopilot._step(issue, "active", reason, **extra)
        name, wait = f"lost-tick-{number}-{tries}", kinds["wait"]["wait"]
        tick = _event("tick", number, usage.iso(now), name, wait=wait)
        state, answers = decide_all(gh, cfg, number, [tick], state, **options)
        actions = [item for _, found_actions in answers for item in found_actions]
        kinds = {action["kind"]: action for action in actions}
    if "runAgent" in kinds or "runChecks" in kinds:
        again = state["again"]
        remember = {
            **fields.get("remember", {}),
            "tries": again["lost"],
            **({"once": True} if again.get("once") else {}),
        }
        reason = f"{what}, so autopilot starts {shown} {'again' if tries else 'once more'}"
        return autopilot._step(issue, "start", reason, **{**fields, "remember": remember}, **extra)
    if (kinds.get("needPerson") or {}).get("reason") == "outage":
        if person:
            if not extra:
                return None
            return autopilot._step(
                issue, "waits", autopilot.person(autopilot.LOST_DO, autopilot.REPORTLESS), **extra
            )
        if record.get("once"):
            why = f"{what}, after autopilot started it once more"
            return autopilot._step(
                issue, "waits", autopilot.person(autopilot.LOST_DO, why), **extra
            )
        why = (
            f"{what}, {tries + 1} times: GitHub created no job for it, or ended its run "
            "without a report, as it does while GitHub Actions has an outage"
        )
        do = autopilot.OUTAGE_DO.format(command=shown)
        return autopilot._step(issue, "waits", autopilot.person(do, why), **extra)
    if label:
        return autopilot._step(issue, "held", autopilot.held_reason(label), running=False)
    raise Refused(f"The engine answers {sorted(kinds)} after a lost run, which it cannot do.")


def _lost_doc(stage, number, target):
    """(document, change version) of `number` while the run of `stage` on `target`, the pull
    request of a fix round or a review, runs: the document that a lost run waits in."""
    if stage not in ("fix", "review"):
        return _stage_doc(stage, number, None, 0)
    change = {"id": target, "version": target}
    if stage == "fix":
        return _stage_doc(stage, number, change, 0)
    doc = first_state.empty(str(number))
    version = str(target)
    doc["state"] = "checking"
    doc["change"] = {"id": version, "version": version}
    doc["pending"] = [
        {"id": first_state.RUN_ID, "next": None, "kind": "runChecks", "changeVersion": version}
    ]
    return doc, version
