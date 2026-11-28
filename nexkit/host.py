"""The GitHub host's calls of the engine's `step` (docs/engine.md#getting-there).

Every decision that moves an item is the engine's. Most still keep no state document: for each
one, the host builds the item's document from NexKit 6's markers, as the import reads them
(`first_state`), sends the event that GitHub shows, and carries out the answer with NexKit 6's
code, in NexKit 6's words. The decisions after a plan, for a new issue's first plan, after a
writer's vouch, after a blocked or stopped round, at a usage limit or a transient error, when a
resume comes due, for a lost command and about a pull request's rounds read the item's stored
state document too, and store the engine's answer (`stored`, `decide`, `decide_all`, `resync`,
below), as does a person's command, NexKit's dispatch of one and a closing command. After a plan,
a stopped round and a review, `report` also sends what the runner's `run.json` says the agent
spent (`spent`).

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
after a collaborator's comment or the same close. These decisions (`after_go`, `round_carried_on`)
read the item's stored state alone. `report` sends the round's result on the stored `working`
state. The sweep and the `step` run send it only while the stored state still waits for the
implement run and does not hold the result's event yet, so no second result reaches the engine. A
collaborator's comment or a blocker's close that the stored state holds (its id is in the events
seen, because `take` stored it) is not sent again, and a writer's closing answer names the
question reference that the state holds. When nothing is left to send, the step carries out what
the stored state holds: a pending run, with autopilot's answers shown when the state says it
answered, a close, or the wait for a person with the attention's reason. An imported round that
autopilot's answers restart has no run pending, so autopilot's `requested` start, with no
person, starts it.

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
These decide on the stored state alone, and a hold that `take` released already is carried out
from the stored pending action, with no second release.
And it decides which rounds count: an automatic fix round that NexKit stopped gives its count
back (`report._stop_round`, `maintain._give_back`), and required checks that fail at the merge
start a counted fix round, wait for the base branch, or leave the rounds used up
(`report._required_failed`).

It decides the wait for required checks that have not passed yet at a merge, and its end
(`report._refused`, `autopilot._merge_step`): `accept_answer` sends the merge's answer to
`step` as the result of the `accept` that the item's stored state holds as pending, so that the
item stays in `accepting` with its first start (the stored `checksSince`) kept,
and `checks_waited` sends the sweep's tick, which tries the merge again or, once the checks
have not passed for `machine.CHECKS_END` since the stored `checksSince`, leaves it to a person.
A later sweep, after that hand-off, finds the item `ready` with `acceptRefused` and a tick that
changes nothing, which is `waits` again. A merge whose issue no sweep looks at is the host's own
refusal, since no tick would come for it.

It decides, in the sweep, whether a merge that GitHub refused is tried again: `refusal_cleared`
sends the head as an observation, `acceptable` when GitHub's `mergeable_state` says it would
accept it, and the host merges only when the engine asks for the accept. A head that GitHub
refused while it called the pull request `clean` is tried once: `report._refused` sends it as an
`acceptable` observation before the refusal (`acceptable_observed`), so that the engine keeps
`refusedAcceptable`, which `refusal_cleared` reads from the stored state.

It decides what follows the other refusals of a merge (`report._refused`, `report._head_moved`,
`report._guarded`): a head that moved, which the host sends as an observation of the new head
first, so that the engine checks it, or a person decides when none was read or it is the head
the round checked; the last refusal after the base branch kept moving through the host's quick
tries; the checks before the merge, an issue closed or unreadable; and any other refusal GitHub
gives that only a person clears. An issue edited after a vouch goes as an `untrusted` refusal,
which the engine answers with `ready` and attention `untrusted`, as the import does.

It decides what follows a merge that GitHub refused for a conflict (`report._refused`):
`accept_answer` sends it as a `conflict` result, and the engine answers a conflict round while
`conflicts.max_rounds` has one left, counted from the stored `rounds.conflicts`, or a person. The
host starts the round with `start_conflict_round(left=...)`, which keeps NexKit 6's other reasons
not to start one. A pull request found `dirty` only at the end goes as a `conflict` without
`conflicts.auto_resolve` and as `needsPerson` with it.

It decides what follows a merge that GitHub refused because the branch is behind its base
(`report._update_branch`, `report._refused`): `accept_answer` sends it as a `behind` result, and
the engine answers an update while one is left under `conflicts.auto_resolve`, and a person
otherwise. The updates used are the stored `rounds.updates`, which `conflicts.max_rounds` limits
apart from conflict rounds. The
host carries out the update with `maintain.update_branch(left=...)`, which starts the review that
carries the approval over, and sends an update that GitHub refused back (`update_failed`) as the
result of the `updateChange` that the stored state holds as pending, which the engine hands to a
person. The review that the update starts stores the update's result for the
pending update (`command`, `_branch_updated`), so that the update is counted and the checks of the
new head follow. A sweep's merge that waits for required checks past six hours
sends the observation that its branch is behind first, in one row with the tick
(`checks_waited`), since the stored observation may say otherwise, and the engine keeps trying
the merge while `conflicts.auto_resolve` is on.

It decides what follows a move of the base branch under an open NexKit pull request
(`base_moved`, called by `maintain.resolve_conflicts`, `maintain.stop_superseded` and
`report._conflicts_after`) on the item's stored state alone: the host sends the conflict, and that
the branch is behind its base when NexKit 6 would update it, and passes no count and no document
of the markers. Whether a round runs only its checks is the stored `runChecks` that the publish
job stores (`publish.store_round`), the conflict rounds and updates used and the automatic fix
rounds counted are the stored `rounds`. The engine starts the conflict round, updates the branch,
stops the round's checks as `superseded` or waits for a person. A hold, NexKit 6's other reasons
not to start a round and a conflict round or update that fails to start after a stop
(`stop_failed`) stay the host's. The push run, the schedule and another item's merge only preview
that answer, in groups of their own. The conflict round that they dispatch is counted by its own
run in the item's group (`command`): it sends the same observation with the answer stored, so that
the stored conflict rounds used are current before the round starts, and the round starts only when
the engine answers one. The review after the push run's update stores the observation that the
stopped head was behind and then the update's result, so that the stored updates used are current
too. When the push run stopped the checks of an automatic fix round that counts, the stored state
is still `checking` with the round's `rounds.automatic`, and the engine gives the round's count
back as it stores the answer. A conflict on a stored state that waits, such as `fixing` after a
round that ended waiting for CI, or `failed` after a failed review, starts no conflict round, where
NexKit 6 started one at once: a failed item waits for a person, and a due fix round merges the base
branch itself (`tests/differential/known.py`).

It decides when a plan, implement or fix stage runs again after its agent stopped at a usage
limit or on a transient error (`stage_limited`, `stage_transient`, called by `report`), and when
a paused or waiting run starts at its time (`resumes`, called by `maintain.resume_one`), a hold
on the issue included. All three decide on the stored state alone: the pending agent run that the
stop's result answers, the `again.transient` count that the runs again are counted from, and the
`usageLimit` or `time` wait that a resume's tick names. A hold goes first as `held` only when the
stored state is not held already, as the engine ignores a second one. A transient stop of publish
goes as a transient stop of the stage's agent. A reset stays the host's to give.

It decides what follows an untrusted edit that stopped the implement or fix round of a `/nexkit go`
or a fix command before its agent started (`stage_untrusted`, called by `report._untrusted_stop`):
the host sends the stop, which only it sees from who edited the issue, as an `untrusted` result of
the round's pending agent run, and the engine answers `failed` with attention `untrusted`, giving
an automatic fix round's count back. It decides on the stored state alone. A plan's untrusted stop
does not reach the engine, so the stored state still holds its plan run, which a writer's vouch
starts again (`vouched`).

It decides what follows a command that autopilot started and lost, whose run GitHub never
started, or that ended without a report, or that handed the work on to a run that never started
(`autopilot._again`, called by `_reportless` and `_handed_on`): `lost_again` sends `actionLost`
at the time of the last start, with autopilot's count of losses, and carries out the start
again, the wait or the person. What counts as lost, and the six-hour window of the count, stay
`autopilot`'s. A person's own lost command waits for that person.

The three results of a merge (`required_failed`, `accept_answer`, `update_failed`) decide on
the stored state alone and pass no document of the markers. A result names the pending action
that it answers by its id and its change version (`_pending_action`): the `accept`, or the
`updateChange` of a branch update that GitHub refused. NexKit 6's run id (`first_state.RUN_ID`)
stands in for none: a NexKit 6 merge on its way is imported with that id, so its action is found,
and when the stored state holds no such action the decision is refused, with the reason that the
item has no pending action to answer, nothing is stored, and the merge waits for a person. The
ids of the result events include the action's id (`accept-<pr>-<action id>`), so that the second
refusal of the same commit, answering the next `accept`, is not taken for a repeat of the first.
The fix rounds used, the conflict rounds, the updates and the start of the wait for required
checks are the stored ones, and NexKit 6's counts of them only word a round's comment.

It decides what the merge finds too (`report.merge_pull`, `autopilot._merge_step`):
`change_seen` sends a pull request that a person merged first, a change that its base branch
already holds, and a commit that a person pushed while the merge waited, as observations, and the
item ends or its new head is checked as the engine answers. `checks_waited`, `refusal_cleared`
and `change_seen` decide on the stored state alone, and the ids of their events carry the revision
that they were decided on, so that each sweep's event is new to the engine. A merge that goes
through is stored as the `accept` action's `accepted` result whether or not the issue has a parent
(`report._merge_stored`): when the engine refuses it, nothing is stored and the run only warns,
since the merge happened either way. Once GitHub shows an item closed, the `close` that its stored
outbox holds is stored as finished (`close_done`), by the run in the item's own group that sees
it: the merge's `report`, the `step` run of a parent that its last part closed
(`maintain.record_part_end`, the `parents` task) and the item's own `issues: closed` run, so that a
closed item keeps no `close` pending. A close that failed stays pending. An implement round that
finds no change is needed (`no_change_found`, called by `report._no_change`) and a fix round that
finds its change on the base branch (`on_base_found`, called by `report._already_on_base`) reach it
too, and close only when it ends the item as `noChange`; both decide on the stored state alone.
`no_change_found` sends the round's result, with the agent's finding, as the result of the implement
`runAgent` that the stored state has pending, and `on_base_found` sends the empty change on the
stored fix round. The `report` of the item's group stores the answer of both in the item's state;
with no configuration, or with no state to decide on, both raise `Refused`. Every parent's end
goes through it: `part_ended` sends the end of a part to its parent's stored state, at every level
and on every close of the part (a merge, a person's close, a change already on the base branch, no
change needed, autopilot's sweep), and the parent ends only when every part ended and one was
completed, under `parentEnd`. It builds no document from GitHub's sub-issues. Before the part's
own end, one row sends the end of every other sub-issue that GitHub shows closed and the stored
parts still hold as open, so that an end that never reached the parent's group still counts, and,
while the stored state waits for its parts to
be filed (`createParts`), the result that names the sub-issues autopilot filed. Two guards only
keep a parent open: a sub-issue that GitHub shows open and the stored parts do not list, such as
an eleventh or one added after the split, keeps it open, and a parent whose stored state is not
`split`, because it was stored before it had sub-issues, is refused, so that a person decides its
end (the merge's own `accepted` result is stored before, whatever its parent). The run of the part
only previews that answer, and sends the end
to the parent's group as a `step` event with the note `part N` (`maintain.send_part_end`); the
run that takes it there stores the row in the parent's state (`maintain.record_part_end`),
and so does the `step` run of a parent whose parts are all closed. Only the facts GitHub knows,
such as parts never filed as sub-issues, stay with `maintain.close_parent`.

Every round's checks and review reach it (`round_ended`): a review that paused at a usage
limit, a review that stopped on a transient error, a review that failed, and a person's own pull
request, which is only reviewed. It finds the checks
that failed the same way before and after a fix round, which the state comment keeps, and
names those that the fix round `route` starts diagnoses (`diagnosis`): an automatic round that
NexKit dispatches takes them from the `diagnose` list of its pending `runAgent` fix, which the
stored state holds and the dispatch carries out, with no event and no read. A round whose commit
conflicts with its base branch starts the round that resolves it, within
`conflicts.max_rounds`, unless a label holds the issue, which keeps it back until the sweep
finds the label gone (`conflicting`, `conflicts_released`). When an implement or fix round
ends, its publish job stores the agent's result and then the publish result in one write
right after it pushes the commit (`publish.store_round`, `round_published`), each only while the
stored state has that action pending, except the publish result of a commit that conflicts,
which `conflicting` sends. The stored state then waits for the round's checks on the published
head while they run, and a fix round's record of the head it changed and of changing nothing is
in it. The `report` finds the round stored and sends nothing again, or stores it when the
publish job could not, and the checks are decided on that state alone. A round that the import
already placed in its checks stores nothing new, and decides on the markers' document as
before. The import places a NexKit 6 round whose run is in its check jobs there too.

It decides the start of the first plan of a new issue too (`first_plan`, called by
`autopilot._first_plan`): it decides on the item's stored document (`decide`), or on the import in
memory when none is stored, and refuses with no configuration. It sends a `requested` start with
no person, named `start-<n>-<revision>` after the stored revision so that a later start is never
taken for a duplicate, and dispatches `/nexkit plan` only when the engine answers with a plan run.
When the stored state still holds the plan run, because its report never reached the engine as
after a plan that failed, the plan starts again, as NexKit 6 plans again, and the engine is not
asked. NexKit 6's checks of a hold, trust, blockers and waves come first, so the engine is never
asked for a held or blocked issue.

It decides what follows a writer's vouch after an untrusted edit stopped a round or a merge
(`vouched`, called by `autopilot._vouched`): it decides on the item's stored state alone, which
the import gives as `failed` with attention `untrusted` or, for a merge, `ready`, or on the import
in memory when none is stored, and refuses with no configuration. It sends the writer's newer
`/nexkit trust` as a `requested` `trust`, and starts a plan, an automatic fix round that counts,
or the checks and review, as the engine answers. When the item's own run already took the vouch
(`take` stored it), it carries out what the state holds instead of sending it again, and it
starts the plan run that a stored state still holds after a plan that an untrusted edit stopped,
as NexKit 6 plans again. A pull request with no automatic fix round left waits for a person,
where NexKit 6 started a counted round anyway (`tests/differential/known.py`). Every start that
autopilot makes follows an answer of the engine (`tests/test_autopilot.py` and
`tests/test_answers.py`).

It decides what a writer's approval of a NexKit pull request does too (`approved`, called by
`maintain.send_approval` and `maintain.merge_approved`): it sends the approval as a
`changeApproved` named `review-<id>` after its review, the name that `take` gives it, on the
item's stored document (`decide`), or on the import in memory when none is stored, and refuses
with no configuration. It merges, accepts the checklist items a person decides or starts nothing
as the engine answers. When the stored state already holds that name, because `take` stored the
review in the item's group, the stored answer is carried out and the engine is not asked again: a
pending `accept` merges (so does a hold on the accepting item, which `merge_approved` then says),
and attention `accept` accepts the items. A hold, an implement round that still runs, a pull
request with no round and a round whose outcome takes no approval stay the host's. An engine that
refuses to answer leaves GitHub as it was.

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
state's events seen) starts nothing and gets no reply; another command taken in this run that
already started a stage refuses it. A dispatch that NexKit sends (its `auto` input names a
kind) carries out the action that the stored state has pending for it (`_carried`: a `plan` or
`implement` or `fix` `runAgent`, or the `runChecks` of a review), which the engine already
decided and stored, and asks the engine nothing and stores nothing, so the same round dispatched
again is never taken for a duplicate. The action that the import gave to a NexKit 6 run on its
way (`first_state.RUN_ID`) is never carried out so. The first dispatch of a conflict round with
nothing pending asks the engine about the conflict instead (`base_moved` with `write`) and
stores the answer, one more conflict round used, before it starts the round, unless an earlier
round on the same base commit ended (`route._conflicts_tried`); a resumed or retried round was
counted when it started. The first dispatch of a review that follows a branch update (its `auto`
input names `carry`, and neither `resumed` nor `retry`) stores the update before it looks for a
pending action, and starts the checks only as the engine answers them (`_branch_updated`). Any
other command, a person's own dispatch and a dispatch with nothing pending too, is a new request,
named `dispatch-<run id>`, decided on the stored state, which a run of the item's group stores
(`decide`). An automatic fix round dispatched with nothing pending (its `auto` input names
`true`, and not `conflicts`, `retry` or `resumed`) says so in its request, and the engine counts it
and marks it automatic as it does its own due round; a person's dispatch stays a person's round.
A command whose own run a newer run replaced starts in the run that ran, when that run's own event
starts nothing (`route._taken_start`). No document built from NexKit 6's markers decides a
command: the stored state does, or the import in memory when nothing is stored. It
holds the facts that `route` once read: whether the issue or pull request is open, the latest
plan, an open pull request and the round that opened it, and the base branch that already holds
the change, which `command` sends as an observation. The host starts only the stage that the
engine answers; a change that the engine ends as `noChange` goes to the `on_base` task. An open
pull request for `/nexkit plan` or `/nexkit go` is the engine's to refuse, and the host says it in
NexKit 6's words ("Pull request #N is already open"); so is a command on an item whose state does
not take it, such as `/nexkit fix` on an item with no change, which `route` refuses with the
engine's reply. An engine that refuses, or answers another stage, starts nothing, and the
person gets a reply. A closed issue or pull request, which a state comment outlives, a fork, a
person's pull request, which takes no `/nexkit fix`, an unknown command and an implement round
that still runs for a person's command (`route._one_round`) stay host checks: the reply to a
command on a closed item is NexKit 6's ("The issue is closed", "The pull request is closed"),
given before the engine is asked, and `take` only moves its cursor past such a command or
*Request changes* review, so that no command changes a closed item's stored state. When the
configuration did not load, `route._decide` answers a command, a *Request changes* review or a
dispatch with the configuration error before the engine is asked, and `command` and
`closing_command` raise `Refused`, since the engine needs the policies of the configuration.

A writer's `/nexkit plan` or `/nexkit go` whose note picks the option that only closes the issue,
among the questions that the stored state waits for (a blocked round's, or the plan's, as the
state's attention names them), closes it as not planned with no agent run, but only the engine
decides that, and it closes only on an answer. So `take` stores such a command as an `answered`
event while the state has no change (`_command_answer`), as it does a plain comment that picks
that option (`_closing_answer`), and `route._decide` carries out the `close` that this stored
(`closing_command`, which hands the `maintain` job its `close_answer` task), before it asks the
engine about the command: a run whose own comment is not among the events taken, and one whose
event an earlier run took, only previews the answer on the stored state and stores nothing. A
closing command whose run a newer run replaced is carried out by the run that ran, as any command
is (`route._taken_start`). With an open pull request the engine closes nothing on an answer, and
the command goes to its round as it is.

An item whose document or event the engine refuses waits for a person, and no other item is
held up by it.

Every run in an item's group first stores the item's first state when it has no state comment
(`store_import`): an open item's document imported from NexKit 6's markers, at revision 1. It then
takes the item's events (`take`, both called by `cli._take` from `nexkit route`): it answers every
comment, review and hold-label change of the item after the cursor in its state comment, oldest
first (a hold label's adding only when autopilot takes the issue, as NexKit 6 reads it), so that
a command whose run a newer one replaced is still answered, and a run whose writes keep losing
fails and leaves its event for the next run. It passes over a command and a review of
a closed issue or pull request, which no stage follows. A state comment that someone other than
the App changed stores and takes nothing: the item waits for a person.

The decisions after a plan (`after_plan`, `plan_carried_on`, `plan_waits_for`), for a new issue's
first plan (`first_plan`), after a writer's vouch (`vouched`), at a merge (`required_failed`,
`accept_answer`, `checks_waited`, `refusal_cleared`, `update_failed`, `change_seen`), after a
blocked or failed `/nexkit go` (`after_go`, `round_carried_on`), after a stopped or timed-out round
(`stopped`, `stop_carried_on`), at a usage limit, a transient error or an untrusted edit
(`stage_limited`, `stage_transient`, `stage_untrusted`), when a resume comes due (`resumes`), for a
lost command (`lost_again`), on a person's command (`command`), on a closing command
(`closing_command`, which carries out the `close` that `take` stored, or previews when nothing was
taken, and the `maintain` job closes), on an approval (`approved`: the `step` run of the item's
group stores it, and the approval's own run in `approved-N` previews), at an item's end and a
parent's end (`no_change_found`, `on_base_found`, `part_ended`, as above) and about a pull request's
rounds read the item's stored state document (`stored`: the document in its state comment, or the
import in memory when it has none) and send their event through `decide`, or their events in a row
through `decide_all`. The ones about its rounds are a round's end (`after_round`, `round_ended`), a
conflict (`conflicting`, `conflicts_released`, `base_moved`), the end of a wait for a hold or the
base branch (`released`, `base_passes`), the checks that a fix round diagnoses (`diagnosis`) and
which rounds count (`stopped_counts`). The checks and the review of a round are two events that
GitHub shows apart, so `round_ended` stores two revisions, and so does a wait's end that has a head
to observe first. The automatic fix rounds and the conflict rounds used are the stored `rounds`
(`_stored_count`), and NexKit 6's count only when no state can be read, for example with no
configuration, except in the decisions that pass no document of the markers (below), which read the
stored `rounds` alone. A conflict round that the push run, the schedule, another item's merge or a
round's report starts is counted by the run it starts (`command`), so the stored count is current
when the next decision reads it.

While other decisions still build their document from NexKit 6's markers, `resync` uses the stored
document only when it says what the markers' document says, apart from its revision, its events
seen and the ids of its outbox, and does not hold the event yet, or any event of a row. Otherwise
the decision is the markers' document at the stored revision, with the stored `counted`,
`conflicts` and `updates` wherever the markers' are lower, so that a document made from
`first_state.empty` never gives a budget back, and so that NexKit 6's answers stay
exactly as they are: a finished plan's import is `awaitingApproval`, which cannot take the plan's
result, and a second send of one event, such as the plan's result that `report` and the step run
both send, would be a duplicate. A plan's result keeps the marks of the answers that the engine
gave to a round's questions (`_carry_replan`), which the markers cannot say, so that a round that
asks again after such a re-plan is not answered twice in a row. `after_go` and `round_carried_on`
no longer go through it: they pass no document of the markers. Nor do `no_change_found`,
`on_base_found`, `stage_limited`, `stage_transient`, `stage_untrusted` and `resumes`, which decide
on the stored state alone, and neither do `command`, `_on_base`, `closing_command` and
`base_moved`, which also takes every count from the stored `rounds`. So do
`checks_waited`, `refusal_cleared` and `change_seen`: the
sweep's looks at a merge that waits for required checks or that GitHub refused, and what the
merge finds. So do `required_failed`, `accept_answer` and `update_failed`. `first_plan` and
`approved` pass none either, and neither does `vouched`.
So do `released`, `base_passes` and `conflicts_released`: the end of a wait is the release of a
stored hold (`_release_row`), autopilot's start for an import made after the label was gone, or,
when `take` released the hold already, the stored pending action. `stopped_counts` too: whether a
stopped round gives its count back is the stored `rounds.automatic` and the pending fix run, never
a flag of its caller. The last move to the stored document removes `resync`.

A row of events, such as a hold and then the stop, or a round's result and then a comment, is one
write: the state after the last event, at the revision read plus one, since `item_state.write`
takes only the next revision, and a tick of a resume names the wait of the document it is decided
on (`wait_id`). A run in the item's group stores the answer (`write`: the `report` after a stage,
a round or at a merge, the `step` run, the approval's merge, and the `step` that starts a due
resume, `maintain.resume_one`), only on top of an existing state comment, at the next revision,
with `item_state.apply`: a write that lost reads the state again, waits and decides again, and when
every try lost, the run warns and carries out the answer anyway, which the next decision stores
again. A run outside that group only previews: the sweep, `nexkit status`, the push run, the
hourly schedule and another item's merge, which decide `base_moved` and `stopped_counts` in groups
of their own, `plan_waits_for`, which asks what a plan waits for under no automatic approval, and
`diagnosis` of a round whose action is not stored yet, which sends a tick that is no event of the
item's own, and the `resume` run, which only sends the `step`. None of them is an answer of the
item, so the conflict round that the push run, the schedule or a merge dispatches, the review
after a branch update and the give-back of a stopped automatic fix round are stored by the run that
they start in the item's group (`command`), so that the stored `counted`, `conflicts` and `updates`
are current after such a stop. With no configuration, `lost_again` decides on the markers'
document and reads and stores nothing, and `resumes` raises `Refused`. An item with no state
comment stores nothing here, since only `store_import` posts the first one, and one whose state
comment someone other than the App changed is refused (`autopilot.changed_comment`). `pending_id`
names the action of the outbox that a result answers. A merge that waits for required checks keeps
the time of its first wait in the stored document across the sweeps' previews. `accept_answer`
sends the events that the host read before the merge's result, such as a head that moved, and
then the result, as one row, stored as a single write: the result names the `accept` that was
pending before the row, which a head that moved cancels in the engine, and the engine then
ignores it.

No module outside `nexkit/engine/` writes on GitHub without an answer of the engine before it:
`tests/test_answers.py` reads their code and fails on a dispatch, a merge, a branch update, a close,
a cancel or a rerun that none comes before.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import time

from . import autopilot, engine, first_state, item_state, protocol, usage
from . import config as configuration
from .checks import failing_signatures
from .github import TRUSTED_ASSOCIATIONS, GitHubError
from .protocol.size import LONGEST, MOST
from .protocol.stages import RUNS
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
    (`event_id`), the time it happened and the number of the issue or pull request that it is
    on (`on`)."""

    def __init__(self, kind, raw, on):
        self.kind, self.raw, self.on = kind, raw, on
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
    found = [("comment", raw, number) for raw in comments]
    if pull:
        found += [("comment", raw, pull) for raw in gh.comments(pull)] if pull != number else []
        found += [("review", raw, pull) for raw in gh.reviews(pull)]
    found += [("issue-event", raw, number) for raw in gh.issue_events(number)]
    sources = []
    for kind, raw, on in found:
        try:
            sources.append(_Source(kind, raw, on))
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


def _taken_event(gh, number, pull, comments, holds, one, taken=lambda: True):
    """The event that the source `one` gives work item `number`, or None for a source that
    gives none: a comment of a bot, a review that is not a writer's approval or request for
    changes, any other event of an issue. `pull` is the item's change, `comments` the item's
    comments and `holds` the labels that hold. `taken` is a function that says whether autopilot
    takes the issue: adding a hold label counts only then, as NexKit 6 reads one only then
    (`autopilot.held`). Removing one always counts: the engine ignores `released` while the item is
    not held, and without it a hold stored while autopilot took the issue could never end. What
    each event does is the engine's to decide."""
    raw, name, at = one.raw, one.name, one.at
    user = raw.get("user") or {}
    login = user.get("login") or ""
    if one.kind == "issue-event":
        label = (raw.get("label") or {}).get("name")
        if raw.get("event") not in ("labeled", "unlabeled") or label not in holds:
            return None
        if raw["event"] == "labeled":
            if not taken():
                return None
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
    # The engine ignores what is not a collaborator's comment (`autopilot._commented_after`): an
    # author outside the repository's collaborators, and a command that is not taken as one.
    trusted = raw.get("author_association") in TRUSTED_ASSOCIATIONS and command is None
    by = _person(login, "reader" if trusted else "outside")
    return _event("commented", number, at, name, by=by, text=text)


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
    of a label of `autopilot.labels` that holds the item: the adding counts only when autopilot
    takes the issue, as NexKit 6 reads a hold label only then, and the removing always counts. A
    comment of someone outside the repository's collaborators, or text of a command that is not
    taken as one, is sent with the role `outside`, which the engine ignores, as autopilot ignores it
    (`autopilot._commented_after`). A writer's comment that picks the option that only closes the
    issue, on questions that the state holds, is sent as an `answered` event, and so is a writer's
    `/nexkit plan` or `/nexkit go` whose note picks that option while no change is open: the engine
    closes the issue only on an answer. Each is answered by
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
    closed = {}

    def is_closed(on):
        """Whether the issue or pull request `on` is closed, read once for this run."""
        if on not in closed:
            found = gh.pull(on) if on != number and on == pull else gh.issue(on)
            closed[on] = found.get("state") != "open"
        return closed[on]

    autopilot_takes = []

    def takes_it():
        """Whether autopilot takes the issue now, read once for this run, and only when a hold
        label was added."""
        if not autopilot_takes:
            autopilot_takes.append(
                autopilot.takes(cfg) and autopilot.taken(gh, cfg, gh.issue(number))
            )
        return autopilot_takes[0]

    taken = []
    for one in sources:
        event = _taken_event(gh, number, pull, comments, holds, one, takes_it)
        if event is not None and protocol.check(event, "event.schema.json", "event"):
            event = None
        if event is None:
            continue
        if event["kind"] in ("requested", "changesRequested") and is_closed(one.on):
            # A command on a closed issue or pull request is refused before the engine is asked
            # (`route._decide`), so it never reaches the stored state: `take` only moves the
            # cursor past it, since a stage that it started would be stored and never run.
            item_state.apply(
                gh,
                number,
                lambda state: (state, [], False),
                tries=tries,
                sleep=sleep,
                since=one.cursor,
            )
            continue
        stored = []

        def answer(state, event=event, stored=stored, one=one):
            event = (
                _closing_answer(gh, number, event, one, state)
                or _command_answer(state, event, number, one.at)
                or event
            )
            found = step(state, event, found_policies)
            stored[:] = [found["changed"], event]
            return found["state"], found["actions"], found["changed"]

        _, actions = item_state.apply(
            gh, number, answer, tries=tries, sleep=sleep, since=one.cursor
        )
        if stored and stored[0]:
            taken.append((stored[1], actions))
    return taken


def _closing_answer(gh, number, event, one, state):
    """The `answered` event that the collaborator's comment `event`, taken from the source
    `one`, is on the questions that `state` holds, when it picks the option that only closes the
    issue and its author may write, which closing is; otherwise None, and the comment stays a
    comment. Without it, the questions that wait would be answered by a new plan, and the
    writer's answer lost (`after_go`)."""
    asked = state.get("questions")
    if event["kind"] != "commented" or event["by"]["role"] != "reader" or not asked:
        return None
    picked = _picked(one.raw.get("body"), {"questions": asked["items"]})
    by = (one.raw.get("user") or {}).get("login") or ""
    if not picked or not by or not gh.can_write(by):
        return None
    answers = [{"question": picked[0], "option": picked[1]}]
    fields = {"by": _person(by, "writer"), "answers": answers, "note": "", "asked": asked["ref"]}
    return _event("answered", number, one.at, one.name, **fields)


def _waiting_asked(state):
    """(ref, items) of the questions that `state` waits for answers to, as the engine reads them
    (`machine.waiting_questions`): those of the round or the plan that attention names, while the
    item awaits an answer with no change open, which is where an answer closes it. None when it
    waits for none."""
    if state["state"] != "awaitingApproval" or state.get("change"):
        return None
    ref = (state["attention"] or {}).get("ref")
    asked, plan = state.get("questions"), state["plan"]
    if ref is None:
        return None
    if asked and asked["ref"] == ref:
        return ref, asked["items"]
    if plan and plan["text"]["ref"] == ref:
        return ref, plan["questions"]
    return None


def _closing_pick(state, note, by, number, at, name):
    """The `answered` event `name` of the writer `by` (a `_person`) whose command note `note`
    picks, among the questions that `state` waits for (`_waiting_asked`), the option that only
    closes the issue; otherwise None. Only the pick is read here: whether the issue closes is the
    engine's (`on_answered`)."""
    waiting = _waiting_asked(state) if note else None
    picked = _picked(note, {"questions": waiting[1]}) if waiting else None
    if not picked:
        return None
    answers = [{"question": picked[0], "option": picked[1]}]
    fields = {"by": by, "answers": answers, "note": "", "asked": waiting[0]}
    return _event("answered", number, at, name, **fields)


def _command_answer(state, event, number, at):
    """The `answered` event that a writer's `/nexkit plan` or `/nexkit go`, the `requested` event
    `event`, is on the questions that `state` waits for, when its note picks the option that only
    closes the issue; otherwise None, and the command stays a request. A `requested` go on those
    questions would start the change, and the engine closes only on an answer."""
    if event["kind"] != "requested" or event["what"] not in ("plan", "go"):
        return None
    return _closing_pick(state, event["note"], event["by"], number, at, event["id"])


# -- the stored document and the decisions that store their answer -------------------------


def pending_id(doc, kind, stage=None):
    """The id of the first action of `doc`'s outbox that is a `kind` (and runs `stage`, when
    given), which the result of that action names, or the id that NexKit 6's runs on their way
    stand for (`first_state.RUN_ID`) when `doc` has none."""
    for action in doc.get("pending") or []:
        if action["kind"] == kind and (stage is None or action.get("stage") == stage):
            return action["id"]
    return first_state.RUN_ID


def _pending_action(state, kind):
    """The first action of `state`'s outbox that is a `kind`, which the result of a merge
    answers, or `Refused` when the stored state holds none: the result never names an action
    that is not pending, and NexKit 6's run id (`first_state.RUN_ID`) stands in for none."""
    for action in (state or {}).get("pending") or []:
        if action["kind"] == kind:
            return action
    raise Refused(f"The item has no pending {kind} to answer.")


def stored(gh, cfg, number, pull=None):
    """(comment, document) of work item `number`: its state comment and the document stored
    in it, or (None, document) with the document that the import builds in memory when it has
    none (`first_state.import_item`), and (None, None) when the import breaks the protocol, the
    comment holds a document that cannot be read, or GitHub cannot be read, which holds nothing
    up (NexKit 6's decisions read nothing here) and which nothing is stored over. Nothing is
    stored. Raises `Refused` when someone other than the App changed the comment."""
    try:
        comment, doc = item_state.read(gh, number)
    except item_state.Edited as edited:
        raise _edited(edited) from None
    except (item_state.StateError, GitHubError):
        return None, None
    if doc is not None:
        return comment, doc
    try:
        return None, first_state.import_item(gh, cfg, number, pull)
    except (item_state.StateError, ValueError, GitHubError):
        return None, None


def _stored_count(state, kept, key):
    """The stored `rounds[key]` of `state`, or NexKit 6's count `kept` when no state can be
    read (no configuration, or a read that failed), which is the only count left then."""
    if state is not None:
        return int((state.get("rounds") or {}).get(key) or 0)
    return max(int(kept or 0), 0)


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


# Transitional, with `resync`: when set, `decide_all` calls `on_fallback(origin, why)` once for
# each decision that `resync` took from the markers' document, with `origin` the host function
# that asked, as `module.qualified name`, and `why` `no state` (nothing is stored), `differs`
# (the stored state says something else than `built`) or `seen` (the stored state holds the event
# already). Only a test or the differential harness sets it. Part 9 of #649 removes it with
# `resync`. It is read as a global at call time.
on_fallback = None


def _why(state, base, event):
    """Why `resync` took `base` from the markers instead of the stored `state`, or None when it
    took `state` (`base is state`)."""
    if base is state:
        return None
    if state is None:
        return "no state"
    events = event if isinstance(event, list) else [event]
    if {one["id"] for one in events} & set(state["recentEvents"]):
        return "seen"
    return "differs"


def resync(state, built, event):
    """The document that `event` is decided on while decisions still build theirs from NexKit
    6's markers: the stored `state` when it says what `built` says, apart from its revision, its
    ids and its events seen, and does not hold `event` yet. Otherwise `built`, at the
    stored revision and with the events seen of `state`, but `event`, since the engine would
    take a second send of it for a duplicate. `event` may be a list of events that are sent in a
    row: `state` holds none of them, or `built` is the document. The fallback keeps the stored
    counts of automatic fix rounds (`counted`), conflict rounds (`conflicts`) and branch updates
    (`updates`) wherever `built`'s are lower, so that a document made from `first_state.empty`
    never gives a budget back: only the engine's answer lowers a count. With no `state`, `built`
    itself. With no `built`, the stored `state` alone. `decide_all` reports each such fallback to
    `on_fallback`, when it is set. This is transitional: the last part of the move to the stored
    document removes it."""
    if state is None or built is None:
        return built if state is None else state
    events = event if isinstance(event, list) else [event]
    names = {one["id"] for one in events}
    seen = state["recentEvents"]
    if not names & set(seen) and _core(state) == _core(built):
        return state
    base = copy.deepcopy(built)
    base["revision"] = state["revision"]
    base["recentEvents"] = [name for name in seen if name not in names]
    for key in ("counted", "conflicts", "updates"):
        most = max(
            int((state.get("rounds") or {}).get(key) or 0),
            int((base.get("rounds") or {}).get(key) or 0),
        )
        # `updates` is optional: a zero is not written where the document has none.
        if key != "updates" or most > 0:
            base["rounds"][key] = most
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
    `actionFinished` or `actionLost` event answers (`pending_id`), or a list of them, one for
    each event of the row (None for one that answers no action), each read on the state that the
    event before it gave, and with the id of the wait of
    kind `wait` in the document that a `tick` names (`wait_id`). `built` may be a function of
    the stored document, which it reads on each try, so that a count comes from the state that
    is decided on (`_stored_count`), or None, which decides on the stored document alone: the caller
    makes sure a state is stored, since with none `built` has nothing to fall back on and the
    call raises `Refused`. `events` may be a function of the stored document too, which is read
    on each try, so that a hold is sent only while the state decided on is not held (`_hold_row`).
    The events after the first
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
    # The function that asked, which a later part converts: `decide` and `_decided` ask for their
    # own caller.
    frame = sys._getframe(1)
    while frame.f_code in (decide.__code__, _decided.__code__):
        frame = frame.f_back
    origin = f"{frame.f_globals.get('__name__')}.{frame.f_code.co_qualname}"
    fell = []

    def answer(state):
        rows = events(state) if callable(events) else events
        base = resync(state, built(state) if callable(built) else built, rows)
        if base is None:
            raise Refused("The item has no state to decide on.")
        fell[:] = [_why(state, base, rows)]
        revision, state, answers, changed = base["revision"], base, [], False
        for index, event in enumerate(rows):
            sent = event
            named = action[index] if isinstance(action, list) else action
            if named is not None and event["kind"] in ("actionFinished", "actionLost"):
                sent = {**sent, "action": pending_id(state, *named)}
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
        # Once per call, whatever the tries that `item_state.apply` made.
        if on_fallback is not None and fell and fell[0] is not None:
            on_fallback(origin, fell[0])
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
    wait=None,
    write=False,
    with_changed=False,
):
    """The engine's answer to `event` on work item `number`, as (state, actions): decided on
    the stored document where it agrees with `built`, the document that NexKit 6's markers
    leave for the decision (`resync`), or on the stored document alone when `built` is None,
    which needs a state comment to be stored, and with `action`, (kind, stage), the id of the
    pending action that the event's result answers (`pending_id`), or with `wait`, the kind of a
    wait, the id of the wait of that kind that the tick answers (`wait_id`). `built` may be a
    function of the stored document, which it reads on each try, so that a count comes from the
    state that is decided on (`_stored_count`). `found_policies` default to the configuration's.

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
        wait=wait,
        write=write,
        with_changed=True,
    )
    if with_changed:
        return state, answers[0][1], changed
    return state, answers[0][1]


def close_done(gh, cfg, number):
    """Store the result of the `close` that work item `number`'s stored outbox holds, once
    GitHub shows the item closed, and answer whether the engine took it. A close that the engine
    asked for (a merge's, a parent's after its last part, an end with no change) is carried out
    by other code of NexKit, or by a person, and nothing else reports it, so without this the
    stored `close` stays pending after the item is closed. It reports what GitHub shows: an item
    that is still open, a close that failed, and an item that holds no `close` change nothing.
    Only a run in the item's group calls it, since it stores the item's state: the merge's
    `report`, the `step` run of a parent (`maintain.record_part_end`, the `parents` task) and the
    item's own `issues: closed` run. It decides on the stored state alone (`decide`), so an item
    with no state comment stores nothing. Raises `Refused` and `GitHubError`."""
    comment, doc = stored(gh, cfg, number)
    if comment is None or not any(action["kind"] == "close" for action in doc["pending"]):
        return False
    if gh.issue(number).get("state") == "open":
        return False
    action = pending_id(doc, "close")
    # A `close` has no change version, which is the one a result of it echoes.
    fields = {"action": action, "changeVersion": None, "result": {}}
    event = _event("actionFinished", number, None, f"closed-{number}-{action}", **fields)
    _, _, changed = decide(
        gh, cfg, number, event, None, action=("close",), write=True, with_changed=True
    )
    return changed


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
        doc = _carry_replan(gh, cfg, number, doc)
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


# The marks of `asked` for the answers that the engine gave (`machine.ANSWERED`).
ANSWERED_MARKS = ("answers", "replan")


def _carry_replan(gh, cfg, number, doc):
    """`doc`, the document that NexKit 6's markers leave for the result of a plan, with the marks
    of the answers that the engine gave to a round's questions when the stored state holds a
    re-plan: the markers cannot say that such a plan carries them on to the round after it, so
    that a round that asks again is not answered a second time in a row."""
    held = stored(gh, cfg, number)[1] if cfg is not None else None
    if held is not None and "replan" in held["asked"]:
        doc["asked"] = [*doc["asked"], *(m for m in ANSWERED_MARKS if m not in doc["asked"])]
    return doc


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
    doc = _carry_replan(gh, cfg, number, doc)
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


def first_plan(gh, cfg, issue, record, start, write=False):
    """The step that starts the first plan of `issue`, which has none yet, as the engine decides
    it on the item's stored state alone (`stored`, `decide`): autopilot's `requested` start of
    the plan, with no person behind it, named `start-<n>-<revision>` after the stored revision,
    so that a later start is never taken for a duplicate. When the stored state still holds the
    plan run (its report never reached the engine, as after a plan that failed), the plan starts
    again, as NexKit 6 plans again, and the engine is not asked. Otherwise the plan runs only
    when the engine answers with a plan run; `start` is the step's fields. `record` is
    autopilot's record, which this decision no longer reads. With `write`, the engine's answer
    is stored (`decide`). Raises `Refused`, with no configuration or no state to decide on."""
    number = issue["number"]
    doc = stored(gh, cfg, number)[1] if cfg is not None else None
    if doc is None:
        raise Refused("The item has no state to decide on.")
    view = _PlanView(issue, {}, {}, {}, start, None, None, [])
    if any(a["kind"] == "runAgent" and a.get("stage") == "plan" for a in doc["pending"]):
        # The plan run is the engine's own answer, which the stored state still holds.
        return view.step("start", "no plan yet", command="plan", **start)
    name = f"start-{number}-{doc['revision']}"
    fields = {"what": "start", "by": None, "note": "", "plan": None, "title": doc["title"]}
    event = _event("requested", number, None, name, **fields)
    _, actions = decide(gh, cfg, number, event, None, write=write)
    if any(a["kind"] == "runAgent" and a["stage"] == "plan" for a in actions):
        return view.step("start", "no plan yet", command="plan", **start)
    raise Refused("The engine starts no plan for a new issue.")


VOUCHED = "a writer vouched for it again"


def vouched(gh, cfg, issue, comment, run, record, start, pull=None, write=False):
    """The step after a writer vouched for `issue` again once an untrusted edit stopped the
    round whose run comment `comment` holds the marker `run` (`autopilot._vouched_again`), as
    the engine decides it on the item's stored state alone (`stored`, `decide`): the state is
    `failed` with attention `untrusted` for a round that stopped, or `ready` for a merge that
    did, as the import gives it, and the writer's newer vouch (`autopilot.vouch`) is a
    `requested` `trust` with role `writer`, named `comment-<id>` as `take` names it. When the
    item's own run already took that vouch (its name is among the stored recent events), nothing
    is sent: the step carries out what the state holds (`_vouch_held`), a plan, an automatic fix
    round that counts, the checks and review, or the wait for a person. So does an issue whose
    stored state still holds its plan run, since an untrusted edit's stop of a plan never
    reaches the engine and the engine ignores a vouch there: the plan starts again, as NexKit 6
    plans again. A plan, a fix round or the checks and review start only when the state or the
    engine's answer holds them; a pull request with no automatic round left waits for a person.
    `record` is autopilot's record, which this decision no longer reads. With `write`, the
    engine's answer is stored (`decide`). Raises `Refused`, with no configuration or no state to
    decide on."""
    number = issue["number"]
    found = autopilot.vouch(gh, issue)
    if found is None or found["id"] <= comment["id"]:
        raise Refused("No writer vouched for the issue after the round that stopped.")
    doc = stored(gh, cfg, number, pull)[1] if cfg is not None else None
    if doc is None:
        raise Refused("The item has no state to decide on.")
    actions = _vouch_actions(gh, cfg, number, found, doc, pull, write)
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


def _vouch_actions(gh, cfg, number, found, doc, pull, write):
    """The engine's actions for the writer's vouch `found` on the item's stored state `doc`: what
    `_vouch_held` says when the vouch is taken already or the state holds the issue's plan run,
    otherwise the answer to the vouch as a `requested` `trust` (`decide`)."""
    name = event_id("comment", found)
    pending = any(a["kind"] == "runAgent" and a.get("stage") == "plan" for a in doc["pending"])
    if name in doc["recentEvents"] or (pull is None and pending):
        return _vouch_held(doc)
    login = (found.get("user") or {}).get("login") or ""
    fields = {"what": "trust", "by": _person(login, "writer"), "note": "", "plan": None}
    event = _event("requested", number, found.get("created_at"), name, **fields)
    return decide(gh, cfg, number, event, None, pull=pull, write=write)[1]


def _vouch_held(doc):
    """What the stored state `doc` holds for a writer's vouch to carry out, as the engine's
    actions: its first pending run (a plan or a fix) or checks, otherwise the wait for a person
    that its attention names, otherwise nothing."""
    for action in doc["pending"]:
        if action["kind"] in ("runAgent", "runChecks"):
            return [action]
    if doc["attention"]:
        return [{"kind": "needPerson", "reason": doc["attention"]["reason"]}]
    return []


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


def after_go(gh, cfg, issue, comment, run, record, start, comments, write=False):
    """The step after the `/nexkit go` of `issue` whose run comment `comment`, with the
    marker `run`, says it stopped as blocked, with questions or protected paths or with
    nothing to answer, or failed on an error and not on time (`autopilot.engine_round`,
    `autopilot._assess`), as the engine decides it on the item's stored state alone. It sends
    only what that state does not hold yet: the round's result, while the state still waits for
    the implement run and does not have the result's event among its recent events; then a
    writer's answer that picks the option that only closes the issue, which names the question
    reference the state holds, or a collaborator's comment since, or the close of an issue that
    blocked it after the round started, each unless its id is in the recent events, because
    `take` or `report` stored it. When nothing is left to send, the step carries out what the
    state holds (`_held`): a pending run, a close, or the wait for a person. `record` is
    autopilot's record, which this decision no longer reads, and `start` what a command that
    the step starts begins from. With `write`, which only the run of the item's group passes,
    the engine's answer is stored (`decide_all`). Raises `Refused`, with no configuration or no
    state to decide on."""
    number = issue["number"]
    said = autopilot._commented_after(comments, comment)
    picked = _picked(said[-1].get("body"), run) if said else None
    by = ((said[-1].get("user") or {}).get("login") or "") if picked else ""
    view = _RoundView(issue, run, start, said, picked)
    doc = stored(gh, cfg, number)[1] if cfg is not None else None
    if doc is None:
        raise Refused("The item has no state to decide on.")
    seen = set(doc["recentEvents"])
    result = first_state._round_result(number, comment, run)
    waiting = any(a["kind"] == "runAgent" and a.get("stage") == "implement" for a in doc["pending"])
    send_result = result["id"] not in seen and waiting
    events = [result] if send_result else []
    heard, restart = None, False
    if said:
        at, name = said[-1].get("created_at"), event_id("comment", said[-1])
        login = (said[-1].get("user") or {}).get("login") or ""
        if name in seen:
            # `take` stored it: a writer's pick on the questions as an answer (`_closing_answer`),
            # any other comment as a comment.
            heard = "commented"
        elif picked and by and gh.can_write(by):
            # Closing is a write: a collaborator who may only read comments on the questions.
            asked = pending_id(doc, "runAgent", "implement") if send_result else None
            asked = asked or (doc.get("questions") or {}).get("ref")
            if asked is None:
                raise Refused("The round has no questions that the answer names.")
            answers = [{"question": picked[0], "option": picked[1]}]
            fields = {"by": _person(by, "writer"), "answers": answers, "note": "", "asked": asked}
            events.append(_event("answered", number, at, name, **fields))
        else:
            # A collaborator, as `autopilot._commented_after` reads them.
            text = {"name": "comment", "ref": str(said[-1]["id"])}
            by_reader = _person(login, "reader")
            events.append(_event("commented", number, at, name, by=by_reader, text=text))
    else:
        closed = _closed_since(gh, issue, comment)
        view.closed = list(closed)
        for found, done in closed.items():
            name = f"blocker-{found}"
            if name in seen:
                heard = "blockerClosed"
                continue
            events.append(
                _event("blockerClosed", number, None, name, blocker=str(found), completed=done)
            )
        if not send_result and doc["state"] in ("working", "planning"):
            # Autopilot's answers start the round again: the import leaves no run pending for it.
            if any(wait["for"] == "start" for wait in doc["waits"]):
                name = f"start-{number}-{doc['revision']}"
                fields = {"what": "start", "by": None, "note": "", "plan": None}
                events.insert(0, _event("requested", number, None, name, **fields))
                restart = True
    if not events:
        view.by_event = heard
        return view.carry(_held(doc))
    state, answers = decide_all(
        gh,
        cfg,
        number,
        events,
        None,
        action=("runAgent", "implement"),
        until=lambda acts: any(x["kind"] in ("runAgent", "close", "createParts") for x in acts),
        write=write,
    )
    actions = []
    for sent, found in answers:
        if found:
            actions = found
            view.by_event = sent["kind"]
    if restart:
        # A request shows no answers, though the state says that autopilot's answers started it.
        actions = _with_answers(actions, state)
    return view.carry(actions or _held(state))


def _with_answers(actions, doc):
    """`actions` and, when they run an agent that autopilot's answers started, which the state
    `doc` says in its `asked`, the `show` of those answers, if they do not hold it."""
    shown = any(a["kind"] == "show" and a["what"] == "answers" for a in actions)
    runs = any(a["kind"] == "runAgent" for a in actions)
    if runs and not shown and "answers" in doc["asked"]:
        return [*actions, {"kind": "show", "what": "answers"}]
    return actions


def _held(doc):
    """What the stored state `doc` holds for autopilot to carry out, as the engine's actions:
    its pending run, with autopilot's answers shown when it answered, or its pending close; a
    change that ends as `noChange` closes as completed, which the import keeps no close for;
    otherwise the wait for a person that its attention names. None when it holds nothing."""
    for action in doc["pending"]:
        if action["kind"] == "runAgent":
            return _with_answers([action], doc)
    for action in doc["pending"]:
        if action["kind"] == "close":
            return [action]
    if doc["state"] == "noChange":
        return [{"kind": "close", "outcome": "completed"}]
    if doc["attention"]:
        return [{"kind": "needPerson", "reason": doc["attention"]["reason"]}]
    return []


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
    follows it on the item's stored state, which waits for the implement run, so that `report`
    mentions nobody. A wait for a person, the closing answer included, makes `report` mention
    them. With `write`, which the `report` of the item's group passes, the answer is stored
    (`decide`), and the sweep's `after_go` then does not send the result again. Raises
    `Refused`."""
    from .report import _autopilot_fields

    found = _autopilot_fields(agent)
    run = {**found, "protected_paths": ((agent.get("output") or {}).get("protected_paths") or [])}
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
        None,
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

# What a path that needs the engine raises when the configuration did not load: `route._decide`
# answers with the configuration error before it gets here, so no decision is made without the
# policies of the configuration.
NO_CONFIG = "NexKit's configuration did not load, so the engine is not asked."


def closing_command(gh, cfg, number, note, comments, source_id, *, branch, by=None, taken=None):
    """What the `maintain` job needs to close issue `number` as not planned, with no agent run, as
    {"answer": the option that was picked, "asked": the link of the questions}, for a writer's
    `/nexkit plan` or `/nexkit go` whose note picks the option that only closes the issue, among
    the questions that its stored state waits for, when the engine drops the issue for that
    answer (`route._decide` builds the `close_answer` task from it); otherwise None, and the
    command goes to its round as it is. `source_id` is the id
    of the command's comment, which names the event as `take` does, `by` the writer, `branch` the
    issue's NexKit branch, and `comments` the issue's comments, which hold the questions' link.

    The engine's answer is stored by `take`, which sends the command as an `answered` event
    (`_command_answer`): when it is among `taken`, [(event, actions)] as `take` gives them, the
    `close` it stored is carried out, and the engine is not asked again, which it would take for a
    duplicate. Otherwise the engine is asked on the item's stored state, or on the import in memory
    when none is stored, and nothing is stored: a command that an earlier run took is not
    answered here, and an open pull request keeps the item for a person, since the engine closes
    only an item with no change. With no `cfg` it raises `Refused` and asks the engine nothing. It
    only sees an open issue: `route._decide` refuses a command on a closed one first."""
    if cfg is None:
        raise Refused(NO_CONFIG)
    name = event_id("comment", source_id)
    mine = [actions for event, actions in taken or [] if event["id"] == name]
    if mine:
        actions = mine[0]
    else:
        pulls = gh.open_pulls(branch)
        pull = pulls[0]["number"] if pulls else None
        _, doc = stored(gh, cfg, number, pull)
        if doc is None or name in doc["recentEvents"]:
            return None
        event = _closing_pick(doc, note, _person(by, "writer"), number, None, name)
        if event is None:
            return None
        try:
            _, actions = decide(gh, cfg, number, event, None, pull=pull, write=False)
        except Refused:
            return None
    if not any(action["kind"] == "close" for action in actions):
        return None
    asked = asked_last(comments)
    picked = _picked(note, {"questions": asked[1]}) if asked else None
    return {"answer": picked[2] if picked else note, "asked": asked[0] if asked else ""}


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


# The stage of the pending action that NexKit's own dispatch of a command carries out.
DISPATCHED = {"plan": "plan", "go": "implement", "fix": "fix", "review": "review"}


def _carried(doc, what):
    """The first action of `doc`'s outbox that NexKit's own dispatch of the command `what`
    carries out, or None: one that `starts` as the stage the command runs. Never the action
    that the import gave to a NexKit 6 run already on its way (`first_state.RUN_ID`), which a
    dispatch decides as a new request, as NexKit 6 did."""
    for action in (doc or {}).get("pending") or []:
        if action["id"] != first_state.RUN_ID and starts([action]) == DISPATCHED[what]:
            return action
    return None


def _dispatch_id(doc):
    """The id of a dispatch that no comment or review gave: `dispatch-<run id>`, so that two
    runs never send the same request. Outside GitHub Actions there is no run id, and the id
    is `dispatch-local-<revision + 1>` of the stored `doc`."""
    run = os.environ.get("GITHUB_RUN_ID", "")
    if run.isdigit() and int(run) > 0:
        return event_id("dispatch", int(run))
    return f"dispatch-local-{int((doc or {}).get('revision', 0)) + 1}"


def command(
    cfg,
    item,
    what,
    *,
    gh=None,
    taken=None,
    by=None,
    note="",
    change=None,
    plan=None,
    empty=False,
    review_id=None,
    source_id=None,
    dispatched=False,
    carried=None,
    observe_conflict=False,
    updated=False,
    automatic=False,
):
    """What the engine answers to a person's command `what` (`plan`, `go`, `fix` or `review`)
    on work item `item` (`route._decide`): `plan`, `implement`, `fix` or `review` for the stage
    it runs (`runChecks` is a `review`), `noChange` when the item ends because its base branch
    already holds the change, `refused` when it runs nothing, or `answered` when an earlier run
    already took the command, so that this one starts nothing and replies nothing. It raises
    `Refused` when the engine refuses the document or the event, and when there is no `gh` or no
    `cfg`, since the engine needs the configuration's policies and `route._decide` gives the
    configuration error before it asks.

    The command is carried out from the item's stored state. When `take` answered it in this
    run (`taken`, [(event, actions)] as `take` gives them), the actions it stored are carried out
    and the engine is not asked again, which it would take for a duplicate. Otherwise the stored
    state says whether an earlier run took it (`answered`), and another command taken in this
    run that already started a stage refuses it.

    After those, NexKit's own `dispatched` command (its `auto` input names a kind) carries out
    the action that the stored state already has pending for it (`_carried`): the engine is not
    asked again and nothing is stored. The action is added to `carried`, a list the caller
    passes, so that `diagnosis` reads its checks. With nothing pending, a conflict round
    (`observe_conflict`, which `route` sets for the first dispatch of one that no earlier round
    on that base commit stops) is counted here, by its own run in the item's group: the run sends
    the observation that the head conflicts through `base_moved` with `write`, named
    `dispatch-<run id>`, with the configuration's policies and `conflicts.auto_resolve` on, as
    the push run previewed it, and stores the answer, one more conflict round used and the fix
    round pending. The round starts only when the engine answers one (`fix`), and `refused`
    otherwise. The review that follows a branch update (`updated`, which `route` sets for a
    `review` dispatch whose `auto` input names `carry` and neither `resumed` nor `retry`) stores
    that update first (`_branch_updated`), before it looks for a pending action, and starts when
    the engine answers its checks. With nothing pending, an automatic fix round (`automatic`,
    which `route` sets when the `auto` input names `true` and none of `conflicts`, `retry` or
    `resumed`) is a new request named `dispatch-<run id>` (`_dispatch_id`) that says it is
    automatic, with `by` null: the engine counts it, marks it automatic and diagnoses the
    repeated checks, as it does its own due round. A person's own dispatch, and any other
    command with nothing pending, is a new request of that name from the person, decided on the
    stored state (`decide`, which stores the answer in the item's group). No
    closed issue or pull request reaches it: `route._decide` refuses a command on one before, in
    NexKit 6's words, so that no command changes the stored state of a closed item.

    The item's state decides all of it: nothing is built from NexKit 6's markers. `change`,
    `{"id", "version"}` of the open pull request, names it where the item has no state comment yet
    (the import in memory); `plan` is the issue's latest plan comment, which only names the plan
    that a `/nexkit go` was sent for. `empty` says that the change's base branch already holds it.
    `by` is the person, `note` their text, and `review_id` the id of a *Request changes* review,
    which is sent as `changesRequested` as `take` sends it. `source_id` is the id of the comment or
    review that gave the command, which names the event as `take` does."""
    if gh is None or cfg is None:
        raise Refused(NO_CONFIG)
    observed = None
    if empty and change:
        seen = {"version": change["version"], "merged": False, "empty": True}
        name = f"seen-{change['id']}-{change['version']}"
        fields = {"changeId": change["id"], "conflicts": False, **seen}
        observed = _event("changeObserved", item, None, name, **fields)
    kind = "review" if review_id is not None else "comment"
    sourced = True
    try:
        name = event_id(kind, review_id if review_id is not None else source_id)
    except ValueError:
        # NexKit's own dispatch, or a person's from the Actions tab, has no source: each run
        # is a request of its own, named by the run.
        name, sourced = _dispatch_id(None), False
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
    pull = int(change["id"]) if change else None
    mine = [actions for event, actions in taken or [] if event["id"] == name]
    if mine:
        if observed and _on_base(gh, cfg, item, observed, pull):
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
    if not sourced and updated and what == "review" and change:
        comment, found = stored(gh, cfg, item, pull)
        if comment is not None:
            # Before the base branch is asked and before `_carried`: the engine holds what it
            # observes of the head while the update is pending, and the checks of the round that
            # the update stopped may still be pending, so carrying them out would leave the update
            # uncounted. A change that the update left empty ends the item there.
            answer = _branch_updated(gh, cfg, item, change, found, empty=bool(observed))
            if answer is not None:
                return answer
    if observed and _on_base(gh, cfg, item, observed, pull):
        return "noChange"
    if not sourced:
        comment, found = stored(gh, cfg, item, pull)
        action = _carried(found, what) if dispatched else None
        if action:
            if carried is not None:
                carried.append(action)
            return starts([action])
        if observe_conflict and what == "fix" and change:
            return _conflict_round(gh, cfg, item, change, found)
        request = {**request, "id": _dispatch_id(found)}
        if automatic and what == "fix":
            # NexKit's own automatic fix round: the engine counts and marks it as its own.
            request = {**request, "by": None, "automatic": True}
    _, actions = decide(gh, cfg, item, request, None, pull=pull, write=True)
    return starts(actions) or "refused"


def _latest_round(gh, pull):
    """The record of the latest round comment of the pull request `pull`, or `{}`."""
    rounds = [run for _, run in run_comments(gh.comments(pull)) if "round" in run]
    return rounds[-1] if rounds else {}


def _asked(cfg):
    """The configuration as the push run asked the engine about a base move: a conflict round
    or an update of the branch is automatic, whatever the configuration says."""
    return {**cfg, "conflicts": {**cfg["conflicts"], "auto_resolve": True}}


def _conflict_round(gh, cfg, item, change, found):
    """`fix` when the engine answers a conflict round on the observation that `change`, the
    pull request's `{"id", "version"}`, conflicts with its base branch, which it stores as one
    more conflict round used, else `refused` (`command`). `found` is the stored state. When the
    push run stopped the pull request's checks (`maintain.stop_superseded`), the stored state is
    still `checking`, and the engine cancels the checks and gives back the count of an automatic
    fix round that counted (`rounds.automatic`)."""
    pull = int(change["id"])
    answer = base_moved(
        gh,
        cfg,
        item,
        pull,
        change["version"],
        True,
        False,
        write=True,
        name=_dispatch_id(found),
        found_policies=policies(_asked(cfg)),
    )
    return "fix" if answer == "round" else "refused"


def _branch_updated(gh, cfg, item, change, found, empty=False):
    """`review` when the engine answers the checks of the new head `change` (`{"id", "version"}`)
    after a branch update, which it stores as one more update used (`command`), `noChange` when
    the head, which `empty` says holds no change against the base branch, ends the item, `refused`
    when it answers none, and None when this run is not the one to store it. `found` is the stored
    state.

    A merge's update (`report._update_branch`) is stored as a pending `updateChange` already
    (`accept_answer`): only its result is sent. After the push run's update
    (`maintain.stop_superseded`) nothing is pending, so the run first tells the engine that the
    head the stopped round checked (the latest round comment's, which records the update) is behind
    its base, as the push run previewed it, and the engine answers the update. The stopped
    round's count is given back with that observation (`base_moved`). Where the engine answers no
    update, the new head is sent as an observation and the checks run only as the engine asks for
    them. None, so that the dispatch carries out what is pending, for a run that sent these events
    already, and when the latest round does not record an update from another head."""
    name = _dispatch_id(found)
    if {name, f"{name}-updated"} & set(found["recentEvents"]):
        return None
    pull, version = int(change["id"]), change["version"]
    pending = next((a for a in found["pending"] if a["kind"] == "updateChange"), None)
    if pending:
        old = pending["changeVersion"]
    else:
        row = _latest_round(gh, pull)
        if not row.get("updated_branch") or not row.get("head") or row["head"] == version:
            return None
        old = row["head"]
        answer = base_moved(
            gh,
            cfg,
            item,
            pull,
            old,
            False,
            True,
            write=True,
            name=name,
            found_policies=policies(_asked(cfg)),
        )
        if answer != "update":
            seen = {**head_observed(item, pull, version), "id": f"{name}-head", "empty": empty}
            state, actions = decide(gh, cfg, item, seen, None, pull=pull, write=True)
            return _updated_end(state, actions)
    observed = {
        "changeId": change["id"],
        "version": version,
        "conflicts": False,
        "merged": False,
        "empty": empty,
    }
    result = {"change": {"id": change["id"], "version": version}, "refused": None}
    result["observed"] = observed
    # `action` names the pending update in place of `first_state.RUN_ID`.
    event = _event(
        "actionFinished",
        item,
        None,
        f"{name}-updated",
        action=first_state.RUN_ID,
        changeVersion=old,
        result=result,
    )
    options = {"pull": pull, "action": ("updateChange",), "write": True}
    state, actions = decide(gh, cfg, item, event, None, **options)
    return _updated_end(state, actions)


def _updated_end(state, actions):
    """What `command` does after the engine's answer to a branch update: `noChange` when it ends
    the item, otherwise the stage that it starts, or `refused`."""
    return "noChange" if state["state"] == "noChange" else starts(actions) or "refused"


def _on_base(gh, cfg, item, observed, pull):
    """Whether the engine ends the item as `noChange` on the observation `observed` that its
    base branch already holds the change (`command`)."""
    state, _ = decide(gh, cfg, item, observed, None, pull=pull, write=True)
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


def round_published(gh, cfg, decision, pr, head, agent, ran, conflicts):
    """Store the end of the implement or fix round of `decision`, whose commit `head` is on the
    pull request `pr`, in the item's state, so that the round's checks are decided on it: the
    agent's result (`agent`, the stage's checked result; `ran`, what its session spent), and
    when the commit does not `conflict` with its base branch, the publish result, in one write.
    A conflicting commit gets only the agent's result, since `conflicting` sends its publish
    result. The publish job calls it first, right after it pushes the commit. The report's call
    then finds the round stored and sends nothing: the stored state waits for the round's
    checks on `head`, with one `runChecks` that the engine gave an id. Each result is sent only
    while the stored state has that action pending, so that a round the import already placed
    in its checks sends nothing, and the report decides it on the markers' document. True when
    the stored state then has `runChecks` pending on `head`: the checks and the review decide on
    it alone (`round_ended`). False when nothing was sent, or the engine did not answer so,
    which warns and lets the round go on as NexKit 6 decides it. Raises `Refused`."""
    if cfg is None or decision.get("action") not in ("implement", "fix"):
        return False
    if not isinstance(agent, dict) or not isinstance(agent.get("output"), dict):
        return False
    number = decision["issue"]
    stage = "fix" if decision["action"] == "fix" else "implement"
    comment, doc = stored(gh, cfg, number, pull=pr)
    if comment is None:
        return False
    running = [
        action
        for action in doc.get("pending") or []
        if action["kind"] == "runAgent" and action.get("stage") == stage
    ]
    if not running:
        # The publish job stored the round already: one `runChecks` on `head` that the engine
        # gave an id, not the import's.
        checks = [a for a in doc.get("pending") or [] if a["kind"] == "runChecks"]
        return (
            not conflicts
            and len(checks) == 1
            and checks[0].get("changeVersion") == head
            and checks[0].get("id") != first_state.RUN_ID
        )
    run = _run_event_id()
    result = {**spent(ran), "changeVersion": head, "output": agent["output"]}
    events = [
        _event(
            "actionFinished",
            number,
            None,
            run,
            changeVersion=running[0].get("changeVersion"),
            result=result,
        )
    ]
    if not conflicts:
        seen = {"changeId": str(pr), "version": head, "conflicts": False}
        seen.update(merged=False, empty=False)
        published = {"change": {"id": str(pr), "version": head}, "refused": None, "observed": seen}
        # The id names the round's run too: a fix round that changes nothing publishes the head
        # that an earlier round did, and the engine takes a repeated id for a duplicate.
        name = f"publish-{pr}-{head}-{run}"
        events.append(
            _event("actionFinished", number, None, name, changeVersion=None, result=published)
        )
    names = [("runAgent", stage), ("publish", stage)][: len(events)]
    state, _ = decide_all(gh, cfg, number, events, None, pull=pr, action=names, write=True)
    if conflicts:
        return False
    checks = [a for a in state["pending"] if a["kind"] == "runChecks"]
    if len(checks) == 1 and checks[0].get("changeVersion") == head:
        return True
    print(f"::warning::The state of #{number} does not wait for the checks of {head[:7]}.")
    return False


def after_round(
    gh,
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
    write=False,
    published=False,
):
    """What follows a round of the NexKit pull request `pr` whose checks and review ended on
    `head`, as the engine decides it: one of `ROUND_ENDS`. `auto_fixes` is how many automatic
    fix rounds the pull request used, as NexKit 6's state comment counts them, `checks` the
    checks' results (None: they did not run), `review` the review NexKit acts on
    (`report.apply_criteria`), `recheck` whether the round is a fix round that changed nothing
    and checked the same head again, `wait` what a fix round would wait for while the base
    branch has the same failure (`autopilot.ci_wait`), `label` the label that holds the issue,
    and `before` the checks of the round before, as the state comment recorded them. `seen`, a
    dict, gets the checks that failed the same way before and after a fix round as `repeated`
    (`round_ended`). With `write`, which only `report` passes, the answers are stored
    (`round_ended`). Raises `Refused`."""
    end, repeated = round_ended(
        gh,
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
        write=write,
        published=published,
    )
    if seen is not None:
        seen["repeated"] = repeated
    return end


# What `report` does after a round's review that is not a verdict on a NexKit pull request,
# by what the engine answers: a person's own pull request reviewed, a review paused at a
# usage limit or failed.
REVIEW_ENDS = ("person_reviewed", "paused", "review_failed")


def round_ended(
    gh,
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
    write=False,
    published=False,
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
    to diagnose.

    The checks' result and the review's are two events: each is decided on the stored
    document and, with `write`, which only `report` passes, stored as its own revision
    (`decide`). The automatic fix rounds used are the stored `rounds.counted`, or `auto_fixes`
    when no state can be read (`_stored_count`). `published`, which only `report` passes, says
    that it stored the round's agent and publish results first (`round_published`): the checks
    are then decided on the stored state alone, with no document built from the markers, and
    `label`, the label that holds the pull request, goes first as a `held` event while the
    stored state is not held.
    Raises `Refused`."""
    number = decision["issue"]
    found = policies(cfg)
    person = bool(decision.get("person"))
    kind, given = review

    def built(state):
        doc = first_state.empty(str(number))
        if person:
            doc["itemKind"] = "reviewOnly"
        doc["state"] = "checking"
        doc["change"] = {"id": str(pr), "version": head}
        doc["hold"] = bool(label)
        doc["policies"] = found
        doc["rounds"]["counted"] = _stored_count(state, auto_fixes, "counted")
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
        retry = int(decision.get("retry") or 0)
        if kind == "transient" and retry > 0:
            # The runs again of the round's command, which a review handed on keeps counting.
            doc["again"] = {"transient": min(retry, MOST_AGAIN)}
        return doc

    results = {"checks": _checks(checks), "baseFailure": str(wait["bug"]) if wait else None}
    name, review_name = f"checks-{pr}-{head}", f"round-review-{pr}-{head}"
    # Decided on the stored state alone, which the round's agent and publish results reached
    # (`round_published`), except a review that stopped on a transient error after runs again,
    # whose count the round's command still carries until the stored state does.
    alone = published and not (kind == "transient" and int(decision.get("retry") or 0) > 0)
    if published:
        # A fix round that changed nothing checks the head that an earlier round checked, and
        # the engine takes a repeated id for a duplicate, which no markers' document hides.
        name, review_name = f"{name}-{_run_event_id()}", f"{review_name}-{_run_event_id()}"
    event = _event(
        "actionFinished",
        number,
        None,
        name,
        action=first_state.RUN_ID,
        changeVersion=head,
        result=results,
    )
    options = {"pull": pr, "found_policies": found, "write": write}
    if alone:
        # A label added while the agent worked, or the hold that `report` assumes when it cannot
        # read the labels, is not stored yet: `held` goes first, only while the state is not held.
        state, answers = decide_all(
            gh,
            cfg,
            number,
            _hold_row(number, label, event),
            None,
            action=("runChecks", None),
            **options,
        )
        actions = answers[-1][1]
    else:
        state, actions = decide(
            gh, cfg, number, event, built, action=("runChecks", None), **options
        )
    review_run = [a for a in actions if a["kind"] == "runAgent"]
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
        review_name,
        action=review_run[0]["id"],
        changeVersion=head,
        result=result,
    )
    action = ("runAgent", "review")
    state, actions = decide(gh, cfg, number, event, state, action=action, **options)
    answer = {"state": state, "actions": actions}
    if kind == "transient":
        return _transient_answer(state, actions, "review"), _repeated(answer)
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


def conflicting(gh, cfg, decision, pr, head, held, used, write=False):
    """What follows a round of `decision` whose commit `head` on the pull request `pr`
    conflicts with its base branch's tip, as `publish` found, so that its checks and review
    did not run, as the engine decides it (`report._skipped_conflicting`): `round`, the
    round that resolves the conflict; `held` while a label `held` the issue, which keeps it
    back; or `used_up` when its `used` conflict rounds leave none, or when
    `conflicts.auto_resolve` is off. `publish` finds a conflict only with it on, so that
    answer comes only when the configuration changed between the two jobs, where NexKit 6
    started the round all the same. The conflict rounds used are the stored
    `rounds.conflicts`, or `used` when no state can be read (`_stored_count`), and none while a
    label `held` the issue, as NexKit 6 reads them, so that a hold never turns into `used_up`. With
    `write`, which only `report` passes, the answer is stored (`decide`). Raises
    `Refused`."""
    number = decision["issue"]
    stage = "fix" if decision["action"] == "fix" else "implement"
    before = {"id": str(pr), "version": str(decision.get("head"))} if stage == "fix" else None

    def built(state):
        doc = _pull_doc(cfg, number, pr, None, RUNS_OF[stage], 0)
        doc.update(change=before, observed=None, hold=bool(held))
        doc["rounds"]["counted"] = _stored_count(state, 0, "counted")
        doc["rounds"]["conflicts"] = 0 if held else _stored_count(state, used, "conflicts")
        doc["pending"] = [
            {"id": first_state.RUN_ID, "next": None, "kind": "publish", "stage": stage}
        ]
        return doc

    seen = {"changeId": str(pr), "version": head, "conflicts": True}
    seen.update(merged=False, empty=False)
    result = {"change": {"id": str(pr), "version": head}, "refused": None, "observed": seen}
    fields = {"action": first_state.RUN_ID, "changeVersion": None, "result": result}
    event = _event("actionFinished", number, None, f"publish-{pr}-{head}", **fields)
    state, actions = decide(
        gh, cfg, number, event, built, pull=pr, action=("publish", stage), write=write
    )
    kinds = {action["kind"]: action for action in actions}
    if (kinds.get("runAgent") or {}).get("stage") == "fix":
        return "round"
    if state["state"] == "checking" and state["hold"] and not state["pending"]:
        return "held"
    if (kinds.get("needPerson") or {}).get("reason") == "acceptRefused":
        return "used_up"
    raise Refused(f"The engine answers {sorted(kinds)} after a conflict, which the host cannot do.")


# The state of the item while a round's agent runs, by its stage.
RUNS_OF = {"implement": "working", "fix": "fixing"}


def conflicts_released(gh, cfg, issue, pull, run, head, conflicts, write=False):
    """What follows once no label holds `issue` whose pull request's round, the marker `run`,
    held back the round that resolves its conflict, as the engine decides it
    (`autopilot._conflicts_released`): `review` of a head that moved, which the next round
    checks for a conflict, or of one that no longer `conflicts`; `round`, while the conflict
    rounds used, the stored `rounds.conflicts`, leave one; or `waits` for a person, with
    `conflicts.auto_resolve` off or no conflict round left. It decides on the stored state
    alone: the head's observation and the release (`_release_row`) are two events, each decided
    on the stored document and, with `write`, which only the item's own `step` run passes,
    stored as its own revision. When `take` stored the label's removal already, no release is
    sent: the action that the stored state has pending is carried out. Raises `Refused`."""
    version = str(run.get("head") or pull)
    moved = head != run.get("head")
    seen = {"changeId": str(pull), "version": str(head or "none") if moved else version}
    seen.update(conflicts=bool(conflicts) and not moved, merged=False, empty=False)
    events = [
        _event("changeObserved", issue, None, f"head-{pull}-{seen['version']}", **seen),
        _release_row(issue, pull, version),
    ]
    state, actions = _decided(gh, cfg, issue, pull, events, write)
    kinds = {action["kind"]: action for action in actions or state["pending"]}
    if "runChecks" in kinds:
        return "review"
    if (kinds.get("runAgent") or {}).get("stage") == "fix":
        return "round"
    if (kinds.get("needPerson") or {}).get("reason") == "acceptRefused":
        return "waits"
    raise Refused(f"The engine answers {sorted(kinds)} after a held conflict, which it cannot do.")


def _release_row(issue, pull, version):
    """The event that ends a hold on `issue`'s pull request `pull`, whose round ran on `version`,
    as a function of the stored document (`decide_all`), since what the stored state holds says
    which it is: `released` while it holds the item, autopilot's start `requested` when an
    import made after the label was gone left only the wait for it (`first_state.Item.held_back`),
    and none when `take` stored the removal already, so that the pending action is carried out and
    the engine takes no second release."""

    def row(state):
        if state is None:
            return []
        if state["hold"]:
            return [_event("released", issue, None, f"released-{pull}-{version}")]
        if any(wait["for"] == "start" for wait in state["waits"]):
            fields = {"what": "start", "by": None, "note": "", "plan": None}
            name = f"start-{issue}-{state['revision']}"
            return [_event("requested", issue, None, name, **fields)]
        return []

    return row


def _decided(gh, cfg, issue, pull, events, write, found_policies=None):
    """(state, actions) of the engine's answers to `events` sent in order on work item `issue`,
    each on the stored document: an event, or a function of the stored document that gives a list
    of events (`decide_all`). With `write` and a state comment, each element is decided and
    stored as its own revision; otherwise they are sent in a row on one read, so that the next
    sees what the one before it gave. The actions of every answer, in order; none when the
    stored state needs none."""
    options = {"pull": pull, "found_policies": found_policies}

    def row(state, elements=events):
        found = []
        for one in elements:
            found += one(state) if callable(one) else [one]
        return found

    comment = stored(gh, cfg, issue, pull)[0] if write and len(events) > 1 else None
    if comment is None:
        state, answers = decide_all(gh, cfg, issue, row, None, write=write, **options)
        return state, [action for _, found in answers for action in found]
    actions = []
    for one in events:
        state, answers = decide_all(
            gh, cfg, issue, lambda state, one=one: row(state, [one]), None, write=True, **options
        )
        actions += [action for _, found in answers for action in found]
    return state, actions


# -- a move of the base branch -----------------------------------------------------------


def base_moved(
    gh,
    cfg,
    issue,
    pr,
    head,
    conflicts,
    behind,
    write=False,
    name=None,
    found_policies=None,
):
    """What follows the move of the base branch under the NexKit pull request `pr` of `issue`,
    whose head is `head`, as the engine decides it from the item's stored state alone
    (`maintain.resolve_conflicts`, `maintain.stop_superseded`, `report._conflicts_after`):
    `conflicts` says that the head conflicts with the base branch's tip, and `behind` that it
    lacks the tip while the base branch's rules want pull requests up to date, which only
    NexKit 6's reading of them says. Whether a round runs only its checks is the stored
    `runChecks` that the publish job stores (`publish.store_round`), and the conflict rounds
    used, the updates of the branch used and the automatic fix rounds counted are the stored
    `rounds`, which `conflicts.max_rounds` limits apart from each other. Returns `round`, the
    round that resolves the conflict; `update`, an update of the branch; or `waits`, when a
    person decides or the checks that run go on to their end. A hold is read before, as NexKit
    6 does. The sweep, the push run and a merge's run each decide in a group of their own, so
    they never pass `write`: it would store a state that only the item's group may write. The
    run of the conflict round or of the review after a branch update that they start, in the
    item's group, passes `write` (`command`), with `name`, the event's id, which is
    `base-<pr>-<head>` by default, and `found_policies`, which default to the configuration's.
    When the checks of an automatic fix round that counts are stopped, the engine's cancel of
    the pending checks gives the count back when the stored `rounds.automatic` says so
    (`machine.give_back`), as `stopped_counts` decides it for the push run's preview. Raises
    `Refused`."""
    version = str(head or pr)
    seen = {"changeId": str(pr), "version": version, "conflicts": bool(conflicts)}
    seen.update(merged=False, empty=False)
    if behind:
        seen["behind"] = True
    event = _event("changeObserved", issue, None, name or f"base-{pr}-{version}", **seen)
    options = {"pull": pr, "found_policies": found_policies, "write": write}
    _, actions = decide(gh, cfg, issue, event, None, **options)
    kinds = {action["kind"]: action for action in actions}
    if (kinds.get("runAgent") or {}).get("stage") == "fix":
        return "round"
    if "updateChange" in kinds:
        return "update"
    if not kinds or "needPerson" in kinds:
        return "waits"
    raise Refused(f"The engine answers {sorted(kinds)} after a base branch moved.")


# -- the checks a fix round diagnoses ------------------------------------------------------


def diagnosis(gh, cfg, issue, pr, head, automatic, repeated, pending=None):
    """The checks that the fix round `route` starts on the pull request `pr` looks into first,
    as the engine runs that round: for an `automatic` round, the `diagnose` list of its
    `pending` action, the `runAgent` fix that the stored state holds and that the dispatch
    carries out (`command`), with no read and no event. A person's round and a conflict round
    diagnose nothing, as NexKit 6's markers say they are.

    Without a `pending` action, which only a round that does not store its action yet has (the
    sweep's, a vouch's and the merge's required checks'), or with one that a person's command
    stored (`by`) and that no round ran, such as a conflict round that `route` refused, the
    `repeated` checks that failed the same way before and after the fix round before it, which
    the state comment keeps, are the ones to diagnose. With `gh` and `cfg` the engine's answer to
    a tick on a built document is decided on the item's stored document (`decide`), and it never
    stores: the tick is no event of the item's own, only a way to learn what the engine would
    diagnose. Without a configuration it is decided on the markers' document alone. This path
    is transitional: it goes when those rounds store their action. Raises `Refused`."""
    if not automatic:
        return []
    if (
        pending is not None
        and pending["kind"] == "runAgent"
        and pending.get("stage") == "fix"
        and pending.get("by") != "person"
    ):
        return list(pending.get("diagnose") or [])
    if not repeated:
        return []
    doc = _pull_doc(None, issue, pr, head, "fixing", 1)
    doc["rounds"]["repeated"] = list(repeated)[: MOST["repeated"]]
    doc["rounds"]["automatic"] = True
    # The round runs as the engine starts a round it issued again, as after a wait.
    doc["waits"] = [{"id": "w1.1", "for": "time", "until": NO_TIME, "on": None}]
    event = _event("tick", issue, None, f"route-{pr}-{head}", wait="w1.1")
    if gh is None or cfg is None:
        _, actions = _answers(doc, [event], {})
    else:
        _, actions = decide(gh, cfg, issue, event, doc, pull=pr, found_policies={}, write=False)
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


def released(gh, cfg, issue, pull, run, head, write=False):
    """What follows once no label holds `issue` whose pull request's round, the marker `run`,
    held back its merge or its automatic fix round, as the engine decides it
    (`autopilot._released`): one of `_wait_end`'s. `head` is the pull request's head now. A
    fix round held back is counted only now, while `fix.max_auto_rounds` has one left as it
    reads now: the rounds used are the stored `rounds.counted`. `_released` reads only the head,
    so a conflict is the next round's to find. For a held merge, `autopilot._released` uses the
    answer only to guard against a refusal, while `_merge_step` reads the head and merges. It
    decides on the stored state alone. A head pushed meanwhile and the release
    (`_release_row`) are two events, each decided on the stored document and, with `write`, which
    only the item's own `step` run passes, stored as its own revision (`decide_all`). When `take`
    stored the label's removal already, no release is sent and nothing is stored: the action that
    the stored state has pending is carried out. Raises `Refused`."""
    version = str(run.get("head") or pull)
    events = []
    if head != run.get("head"):
        fields = {"changeId": str(pull), "version": str(head or "none")}
        fields.update(conflicts=False, merged=False, empty=False)
        events.append(_event("changeObserved", issue, None, f"head-{pull}-{head}", **fields))
    events.append(_release_row(issue, pull, version))
    state, actions = _decided(gh, cfg, issue, pull, events, write)
    return _wait_end(state, actions or state["pending"])


def base_passes(gh, cfg, issue, pull, run, write=False):
    """What follows the end of the wait of `issue`'s pull request `pull`, whose round, the
    marker `run`, waits while the base branch has the same failure (`ci_waiting`), once its
    bug issue closed or the base passes again, which the host sends as the end of that bug
    issue (`autopilot._ci_step`): `fix`, counted now, or `used_up`. It decides on the stored
    state alone, with the rounds used from the stored `rounds.counted`. With `write`, which only
    the item's own `step` run passes, the answer is stored (`decide`). When the end of the wait
    is stored already, the action that the stored state has pending is carried out. Raises
    `Refused`."""
    bug = str((run.get("ci_wait") or {}).get("bug") or "none")
    event = _event("blockerClosed", issue, None, f"base-{pull}-{bug}", blocker=bug, completed=True)
    state, actions = decide(gh, cfg, issue, event, None, pull=pull, write=write)
    return _wait_end(state, actions or state["pending"])


def required_failed(gh, cfg, issue, pr, wait, write=False):
    """What follows required checks that failed at the merge of the NexKit pull request `pr`, as
    the engine decides it (`report._required_failed`): `auto_fix`, counted, `ci_waiting` while
    they fail as the base branch does, which `wait` names (`autopilot.ci_wait`), or `needs_human`
    when no automatic fix round is left. The result answers the `accept` that the item's stored
    state holds as pending, with its id and change version, and the automatic fix rounds used are
    the stored ones. NexKit 6 tries a merge only when no label holds the issue. `write` stores the
    answer (`decide_all`). Raises `Refused`, also when the stored state holds no pending
    `accept`, and then stores nothing."""

    def row(state):
        pending = _pending_action(state, "accept")
        result = {
            "accepted": False,
            "refusal": "checkFailed",
            "text": "A required check failed.",
            "baseFailure": str(wait["bug"]) if wait else None,
        }
        fields = {
            "action": pending["id"],
            "changeVersion": pending.get("changeVersion"),
            "result": result,
        }
        return [_event("actionFinished", issue, None, f"required-{pr}-{pending['id']}", **fields)]

    state, answers = decide_all(gh, cfg, issue, row, None, pull=pr, write=write)
    kinds = {action["kind"]: action for _, actions in answers for action in actions}
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


def acceptable_observed(issue, pr, head, at):
    """The event that tells the engine GitHub would accept the head `head` of the NexKit pull
    request `pr` now, for `accept_answer`'s `before`: sent before a refusal of that head, so that
    the engine keeps `refusedAcceptable` and does not try the head again (`report._refused`)."""
    fields = {"changeId": str(pr), "version": str(head), "conflicts": False}
    fields.update(merged=False, empty=False, acceptable=True)
    return _event("changeObserved", issue, at, f"acceptable-{pr}-{head}-{at}", **fields)


def accept_answer(gh, cfg, issue, pr, result, before=(), at=None, write=False):
    """What follows the answer of GitHub's merge of the NexKit pull request `pr`, as the engine
    decides it (`report._refused`): the accept's `result`, as `accepted` or with its refusal
    kind, sent after the events `before`, which tell the engine what the host read, such as a
    head that moved, and `at` the time of the events. It answers the `accept` that the item's
    stored state holds as pending, with its id and change version, as read before the events
    `before`: a head that moved cancels that `accept` in the engine, which ignores the result
    then. The conflict rounds and updates used, which `conflicts.max_rounds` limits apart from
    each other, and the start of the wait for required checks (`checksSince`) are the stored
    ones. Returns `(end, state)`, `end` being `accepted`, `waiting` while the required checks
    are awaited, `review` for the checks and review of a newer head that `before` observed, `fix`
    or `redo` for the round that follows, `update` for the update of a branch that is behind its
    base, `base_failure` while a failure the base branch has is awaited, or `person`, whose reason
    `state["attention"]` names. The events `before` and the result are one row, stored as a
    single write with `write` (`decide_all`). Raises `Refused`, also when the stored state holds
    no pending `accept`, and then stores nothing."""

    def row(state):
        pending = _pending_action(state, "accept")
        fields = {
            "action": pending["id"],
            "changeVersion": pending.get("changeVersion"),
            "result": result,
        }
        finished = _event("actionFinished", issue, at, f"accept-{pr}-{pending['id']}", **fields)
        return [*({**event, "at": at or event["at"]} for event in before), finished]

    state, answers = decide_all(gh, cfg, issue, row, None, pull=pr, write=write)
    actions = [action for _, found in answers for action in found]
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


def approved(gh, cfg, issue, pr, run, commit, actor, write=False, review=None):
    """What a writer's approval of the NexKit pull request `pr` of `issue` does, as the engine
    decides it (`maintain.send_approval`, `maintain.merge_approved`): `run` is the marker of
    the pull request's latest round, which passed, `commit` the commit that the approval names,
    or None, and `review` the id of the approving review. The approval is a `changeApproved`
    named `review-<id>` after its review, which is the name `take` gives it (`event_id`), or
    `approval-<pr>-<version>` with no review id. Returns `merge`, when the engine accepts the
    change; `accept`, when it takes the approval as a person's decision of the checklist items
    (`run` has `decide`) and leaves the accept to them; or `none`, when it changes nothing, as
    for an approval of another version of a change that waits for its accept, or with
    `merge.auto` off. A held item that takes the approval gives `merge` too, with no accept, so
    that `maintain.merge_approved` says the hold. The engine answers on the item's stored state
    alone (`stored`, `decide`).
    When the stored state already holds the approval's event, because `take` stored it in the
    item's group, the stored answer is carried out and the engine is not asked again: a pending
    `accept` merges, and attention `accept` accepts the items a person decides. Only the `step`
    run in the item's group passes `write`, which stores the answer; the approval's own run in
    its group `approved-N` only previews. Raises `Refused`, with no configuration or no state
    to decide on."""
    decide_items = bool(run.get("decide"))
    doc = stored(gh, cfg, issue, pr)[1] if cfg is not None else None
    if doc is None:
        raise Refused("The item has no state to decide on.")
    version = str(commit or doc["change"]["version"])
    name = event_id("review", review) if review is not None else f"approval-{pr}-{version}"
    if name in doc["recentEvents"]:
        held = doc["hold"] and doc["state"] == "accepting"
        if held or any(action["kind"] == "accept" for action in doc["pending"]):
            return "merge"
        if decide_items and (doc["attention"] or {}).get("reason") == "accept":
            return "accept"
        return "none"
    fields = {"by": _person(actor or "someone", "writer"), "changeId": str(pr)}
    fields["changeVersion"] = version
    event = _event("changeApproved", issue, None, name, **fields)
    state, actions, changed = decide(
        gh, cfg, issue, event, None, pull=pr, write=write, with_changed=True
    )
    kinds = {action["kind"] for action in actions}
    if not changed:
        return "none"
    if "accept" in kinds or (not kinds and state["hold"] and state["state"] == "accepting"):
        # A hold keeps the engine's accept back, and the host says so (`maintain.merge_approved`).
        return "merge"
    if decide_items and (state["attention"] or {}).get("reason") == "accept":
        return "accept"
    raise Refused(f"The engine answers {sorted(kinds)} to an approval, which the host cannot do.")


def _revision(state):
    """The stored revision of `state`, which a sweep's events name in their ids: it changes on
    every stored write, so that each sweep's event is new to the engine and a repeated id is
    never mistaken for a duplicate. 0 when there is no state."""
    return int((state or {}).get("revision") or 0)


def _answered(answers):
    """The kinds of the actions of all `answers` (`decide_all`'s rows), each with its action."""
    return {action["kind"]: action for _, actions in answers for action in actions}


def checks_waited(gh, cfg, issue, pr, head, now, behind=False, write=False):
    """What follows a sweep's tick of the merge of the NexKit pull request `pr` that waits for
    required checks on `head`, as the engine decides it (`autopilot._merge_step`): `merge`, to
    try it again, or `waits` for a person once the checks have not passed for more than six
    hours. The start of the wait is the stored `checksSince`. `behind` says that GitHub calls the
    branch behind its base: it is sent first, as an observation in one row with the tick, since
    the stored observation may say otherwise, and a `behind` branch keeps being tried under
    `conflicts.auto_resolve`, as the engine decides. After the six hours the engine left the merge
    to a person (`ready` with `acceptRefused`) and a later tick changes nothing, which is
    `waits` too. A merge that a label held back has no wait for required checks: it is `merge`,
    since its accept is the one that the release gives (`released`). The ids of the events carry
    the stored revision (`_revision`). It decides on the stored state alone. `write` stores the
    answer (`decide_all`). Raises `Refused`."""
    head, at = str(head), usage.iso(now)

    def events(state):
        row, rev = [], _revision(state)
        if behind:
            fields = {"changeId": str(pr), "version": head, "conflicts": False}
            fields.update(merged=False, empty=False, behind=True)
            row.append(_event("changeObserved", issue, at, f"behind-{pr}-{head}-r{rev}", **fields))
        row.append(_event("tick", issue, at, f"merge-{pr}-{head}-r{rev}", wait="w1.1"))
        return row

    state, answers = decide_all(
        gh, cfg, issue, events, None, pull=pr, wait="requiredChecks", write=write
    )
    kinds = _answered(answers)
    if "accept" in kinds:
        return "merge"
    if (kinds.get("needPerson") or {}).get("reason") == "acceptRefused":
        return "waits"
    left = (state["attention"] or {}).get("reason") == "acceptRefused"
    if not kinds and state["state"] == "ready" and left:
        return "waits"
    waiting = any(wait["for"] == "requiredChecks" for wait in state["waits"])
    if not kinds and state["state"] == "accepting" and not waiting:
        # A merge that a label held back waits for no required checks: its accept is the one
        # that the release gives (`released`), pending in the stored state or still to be
        # stored, and the host tries the merge (`autopilot._released`).
        return "merge"
    raise Refused(f"The engine answers {sorted(kinds)} after a merge's wait, which it cannot do.")


def refusal_cleared(gh, cfg, issue, pr, head, acceptable, write=False):
    """What follows a sweep's look at the NexKit pull request `pr` whose merge GitHub refused, on
    its head `head` (`autopilot._refused_step`), as the engine decides it: `merge` when the
    engine accepts it again, once `acceptable` says GitHub would accept `head` now
    (`mergeable_state` `clean`, `unstable` or `has_hooks`), or `waits` when it asks for nothing,
    such as when GitHub would not, or when that head was refused while it was acceptable, so that
    each head is tried once: the stored `refusedAcceptable` says so, which `report._refused` sends
    as an `acceptable` observation before a refusal of a `clean` pull request. The accept policy
    decides nothing here, as in NexKit 6. The id of the event carries the stored revision
    (`_revision`). It decides on the stored state alone. `write` stores the answer
    (`decide_all`). Raises `Refused`."""
    head = str(head)

    def events(state):
        fields = {"changeId": str(pr), "version": head, "conflicts": False}
        fields.update(merged=False, empty=False)
        if acceptable:
            fields["acceptable"] = True
        name = f"cleared-{pr}-{head}-r{_revision(state)}"
        return [_event("changeObserved", issue, None, name, **fields)]

    _, answers = decide_all(gh, cfg, issue, events, None, pull=pr, write=write)
    kinds = set(_answered(answers))
    if "accept" in kinds:
        return "merge"
    if not kinds:
        return "waits"
    raise Refused(f"The engine answers {sorted(kinds)} after a refused merge, which it cannot do.")


def update_failed(gh, cfg, issue, pr, why, write=False):
    """What follows an update of the NexKit pull request `pr`'s branch, which the engine asked
    for and GitHub refused, as it decides it (`report._update_branch`): `why` is GitHub's
    answer. The result answers the `updateChange` that the item's stored state holds as
    pending, with its id and change version. The engine hands the merge to a person: this
    returns `person`. `write` stores the answer (`decide_all`). Raises `Refused`, also when the
    stored state holds no pending `updateChange`, and then stores nothing."""

    def row(state):
        pending = _pending_action(state, "updateChange")
        result = {"change": None, "refused": str(why), "observed": None}
        fields = {
            "action": pending["id"],
            "changeVersion": pending.get("changeVersion"),
            "result": result,
        }
        return [_event("actionFinished", issue, None, f"update-{pr}-{pending['id']}", **fields)]

    _, answers = decide_all(gh, cfg, issue, row, None, pull=pr, write=write)
    reasons = {action["kind"]: action for _, actions in answers for action in actions}
    if (reasons.get("needPerson") or {}).get("reason") == "acceptRefused":
        return "person"
    raise Refused(f"The engine answers {sorted(reasons)} after a failed update.")


def change_seen(gh, cfg, issue, pr, seen, write=False):
    """What follows what the host found about the NexKit pull request `pr` at its merge, as the
    engine decides it (`report.merge_pull`, `autopilot._merge_step`): `seen` is the observation
    without its change and its conflict, the pull request's `version`, and whether it was
    `merged` by a person or became `empty` because its base branch already holds the change.
    NexKit never tried the merge then, so the merge has no result to send. The stored state
    holds whether the merge waits for required checks or has its accept pending. Returns
    `accepted` or `noChange` when the item ended, and `review` when the engine checks the
    version that a person pushed, also when the stored state checks that version already and
    waits for its checks to start or has them pending (a held merge whose pull request got a
    commit), as NexKit 6 starts the review. The id of the event carries the stored revision
    (`_revision`). It decides on the stored state alone. `write` stores the answer
    (`decide_all`). Raises `Refused`."""

    def events(state):
        name = f"seen-{pr}-{seen['version']}-r{_revision(state)}"
        return [
            _event("changeObserved", issue, None, name, changeId=str(pr), conflicts=False, **seen)
        ]

    state, answers = decide_all(gh, cfg, issue, events, None, pull=pr, write=write)
    kinds = set(_answered(answers))
    if state["state"] in ("accepted", "noChange"):
        return state["state"]
    if "runChecks" in kinds:
        return "review"
    same = state["change"]["version"] == seen["version"]
    starting = any(wait["for"] == "start" for wait in state["waits"]) or any(
        action["kind"] == "runChecks" and action.get("changeVersion") == seen["version"]
        for action in state["pending"]
    )
    if not kinds and state["state"] == "checking" and same and starting:
        # The stored state checks that version already and waits for its checks to start, or has
        # them pending since the release of the hold stored the start, as for a held merge whose
        # pull request got a commit (`first_state.Item.held_back`): the host starts them, as
        # NexKit 6 does.
        return "review"
    raise Refused(f"The engine answers {sorted(kinds)} to what the merge found.")


def no_change_found(gh, cfg, decision, finding, write=False):
    """`noChange` when the engine ends the item of `decision`, an implement round whose agent
    found that no change is needed, with `finding`, where the issue or its plan says to stop
    then (`report._no_change`): the host sends the round's result, which holds the finding, as
    the result of the implement `runAgent` that the item's stored state has pending, and closes
    the issue only as the engine ends the item. With `write`, which only the `report` of the
    item's group passes, the engine's answer is stored in the item's state (`decide`). Raises
    `Refused` for any other answer, for an item with no state to decide on, and with no
    configuration, before anything is read or written."""
    number = decision["issue"]
    output = {**round_output({}), "status": "done", "no_change": finding}
    result = {**AGENT, "changeVersion": None, "output": output}
    fields = {"action": first_state.RUN_ID, "changeVersion": None, "result": result}
    event = _event("actionFinished", number, None, _run_event_id(), **fields)
    state, actions = decide(
        gh, cfg, number, event, None, action=("runAgent", "implement"), write=write
    )
    if state["state"] == "noChange" and any(action["kind"] == "close" for action in actions):
        return "noChange"
    kinds = sorted({action["kind"] for action in actions})
    raise Refused(f"The engine answers {kinds} to an implement round that found no change.")


def on_base_found(gh, cfg, decision, write=False):
    """`noChange` when the engine ends the item of `decision`, a fix round that found, before
    its agent, that its base branch already holds the pull request's change
    (`report._already_on_base`): the host sends the observation that the change is empty, as
    the merge does (`change_seen`), on the item's stored fix round, and closes the pull request
    and its issue only as the engine ends the item. With `write`, which only the `report` of
    the item's group passes, the engine's answer is stored in the item's state (`decide`).
    Raises `Refused` for any other answer, for an item with no state to decide on, and with no
    configuration, before anything is read or written."""
    number, pr = decision["issue"], decision["pr"]
    version = _stage_version(decision)
    seen = {"conflicts": False, "version": version, "merged": False, "empty": True}
    name = f"onbase-{pr}-{version}"
    event = _event("changeObserved", number, None, name, changeId=str(pr), **seen)
    state, actions = decide(gh, cfg, number, event, None, pull=pr, write=write)
    if state["state"] == "noChange" and any(action["kind"] == "close" for action in actions):
        return "noChange"
    kinds = sorted({action["kind"] for action in actions})
    raise Refused(f"The engine answers {kinds} to a change that its base branch already holds.")


def part_ended(gh, cfg, parent, subs, part, state, write=False):
    """Whether the parent issue `parent` ends once its sub-issue `part`, which closed as
    `state`, ended, as the engine decides it on the parent's stored state alone
    (`maintain.close_parent`, at every level and for every way a part closes): `close` when
    every part ended and one was completed, under `parentEnd`, which GitHub gives as `person`
    without `close_parent_issues`; `person` when every part ended and a person decides;
    otherwise `stays`. The answer comes from the state after the row as well as from its
    actions, so that a parent whose end was already stored answers the end of a part that closed
    at the same time with `close` too, whether its `close` is pending or done.

    The row that the engine gets holds what GitHub shows and the stored state lacks. Before the
    end of `part`, it holds the end of every other sub-issue that GitHub shows closed and the
    stored `parts` still hold as open, so that the end of an earlier part that never reached the
    parent's group still counts. When the stored state still waits for its parts to be filed
    (`createParts`, which autopilot carries out and reports nowhere else), the row starts with
    that action's result: the sub-issues that autopilot filed for this parent
    (`autopilot._part_of`), by number, matched to the action's part keys in order.

    The host guards two facts that no event carries, only in the direction of keeping the parent
    open (D10). A sub-issue other than `part` that GitHub shows open and the stored `parts` do
    not list, such as an eleventh one or one added after the split, answers `stays` before
    anything is sent or stored. A parent whose stored state is not `split`, such as one stored
    before it had sub-issues, raises `Refused`: a person decides its end. A completed sub-issue
    beyond the ten that a state holds is not sent either, so the engine may leave the parent to
    a person where NexKit 6 closed it (`tests/differential/known.py`). A parent that the engine
    already accepted answers `close` with nothing sent.

    With `write`, which only the `step` run of the parent's group passes
    (`maintain.record_part_end`), the row is stored in the parent's state, when it has a state
    comment (`decide_all`); the run of the part never does. Raises `Refused`."""
    _, doc = stored(gh, cfg, parent)
    if doc is None:
        raise Refused("The parent has no state to decide on.")
    listed = {held["item"] for held in doc["parts"]}
    unlisted = [
        sub
        for sub in subs
        if sub.get("state") == "open"
        and sub["number"] != int(part)
        and str(sub["number"]) not in listed
    ]
    if unlisted:
        return "stays"
    if doc["state"] == "accepted":
        return "close"
    if doc["state"] != "split":
        raise Refused("The parent's stored state holds no split, so a person decides its end.")
    own = str(part)

    def row(stored_state):
        events = []
        ended = {held["item"]: held["state"] for held in stored_state["parts"]}
        filing = [a for a in stored_state.get("pending") or [] if a["kind"] == "createParts"]
        if filing:
            keys = [made["key"] for made in filing[0]["parts"]]
            filed = sorted(sub["number"] for sub in subs if autopilot._part_of(sub, parent))
            pairs = zip(keys, filed, strict=False)
            named = [{"key": key, "item": str(item)} for key, item in pairs]
            result = {"parts": named}
            fields = {"action": filing[0]["id"], "changeVersion": None, "result": result}
            events.append(_event("actionFinished", parent, None, f"parts-{parent}", **fields))
            ended = {made["item"]: ended.get(made["item"]) for made in named[: MOST["split"]]}
        for sub in subs:
            number, how = str(sub["number"]), first_state._ended(sub)
            if number != own and how and number in ended and ended[number] is None:
                name = f"part-{parent}-{number}"
                events.append(_event("partEnded", parent, None, name, part=number, state=how))
        name = f"part-{parent}-{part}"
        events.append(_event("partEnded", parent, None, name, part=own, state=state))
        return events

    end = "whenPartAccepted" if cfg["close_parent_issues"] else "person"
    found = {**policies(cfg), "parentEnd": end}
    after, answers = decide_all(gh, cfg, parent, row, None, found_policies=found, write=write)
    kinds = {action["kind"] for _, actions in answers for action in actions}
    if "close" in kinds or after["state"] == "accepted":
        return "close"
    reason = (after.get("attention") or {}).get("reason")
    parts = after["parts"]
    waits = after["state"] == "split" and reason == "splitIncomplete"
    if "needPerson" in kinds or (waits and all(held["state"] for held in parts)):
        return "person"
    return "stays"


def stopped_counts(gh, cfg, issue, pr, head, by_host, write=False):
    """How many automatic fix rounds of the NexKit pull request `pr` count after its round on
    `head` stopped, as the engine decides it from the stored state alone: a round that the host
    stopped before its review ended (`by_host`), such as for an agent it may not run, a change
    already on the base branch, or checks that the base branch's move stopped, gives its count
    back when the stored `rounds.automatic` marks it and the stored state still holds its pending
    fix run or checks (`machine.give_back`); a round whose agent stopped by itself keeps it
    (`report._stop_round`, `maintain._give_back`). The count is the stored `rounds.counted`.
    The host's own stops, and what follows them, stay NexKit 6's, so the host reports one as
    `stopped`; no policy decides a stop. With `write`, which only a run of the item's group
    passes, the answer is stored (`decide`); `maintain._give_back` runs in the push run's or the
    sweep's group and never does: it only previews, and the run that the push run starts in the
    item's group stores the give-back (`command`, `base_moved`). Raises `Refused`."""
    version = str(head or pr)
    action = None
    if by_host:
        text = "The host stopped the round before its review."
        event = _event("stopped", issue, None, f"stop-{pr}-{version}", outcome="other", text=text)
    else:
        result = {**AGENT, "changeVersion": version, "output": None, "error": "The round stopped."}
        fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
        event = _event("actionFinished", issue, None, f"stop-{pr}-{version}", **fields)
        action = ("runAgent", "fix")
    state, _ = decide(
        gh, cfg, issue, event, None, pull=pr, found_policies={}, action=action, write=write
    )
    return state["rounds"]["counted"]


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


def _stage_version(decision):
    """The version of the change that the stage of `decision` runs on: a fix round's pull
    request head, or None for a plan and an implement round, which have no change yet."""
    pr = decision.get("pr")
    return str(decision.get("head") or pr) if pr else None


def _hold_events(number, label):
    """The `held` event of a label that holds the item, if any. Its id holds the run, because
    the engine drops an event whose id is among its last twenty: a hold of a later round, after
    the label went and came again, is a new event and not a repeat of the earlier one."""
    if not label:
        return []
    return [_event("held", number, None, f"held-{number}-{_run_event_id()}", label=str(label))]


def _hold_row(number, label, *rest):
    """The events of a stop or a tick, as a function of the stored document: `held` first only
    while the state decided on is not held already, since the engine ignores a second hold."""

    def row(state):
        held = [] if (state or {}).get("hold") else _hold_events(number, label)
        return [*held, *rest]

    return row


def stage_limited(gh, cfg, decision, reset, label=None, write=False):
    """Until when the run of `decision`, a plan, implement or fix stage whose agent stopped at
    the usage limit, waits to run again as the engine decides it (`report.pause`), or None
    when it leaves the run to a person. `reset` is the time the host gives, or None when it
    does not resume the run, and `label` the label that holds the issue, if any. It decides on
    the stored state alone, on its pending agent run, and sends `held` first only when the
    state is not held. With `write`, which the `report` of the item's group passes, the answer
    is stored (`decide_all`). Raises `Refused`."""
    number = decision["issue"]
    version = _stage_version(decision)
    message = "The agent stopped at its usage limit."
    limit = {"message": message, "reset": _time(reset)}
    result = {**AGENT, "changeVersion": version, "output": None, "limit": limit}
    fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
    finished = _event("actionFinished", number, None, _run_event_id(), **fields)
    _, answers = decide_all(
        gh,
        cfg,
        number,
        _hold_row(number, label, finished),
        None,
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
    `("failed",)` for a person after the last. `count` is the number of the run again, counted
    from the stored `again.transient`. It decides on the stored state alone, on its pending
    agent run, and sends `held` first only when the state is not held. With `write`, which the
    `report` of the item's group passes, the answer is stored (`decide_all`). Raises
    `Refused`."""
    number = decision["issue"]
    version = _stage_version(decision)
    result = {**AGENT, "changeVersion": version, "output": None, "transient": True}
    result["error"] = str(error)
    fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
    finished = _event("actionFinished", number, usage.iso(now), _run_event_id(), **fields)
    state, answers = decide_all(
        gh,
        cfg,
        number,
        _hold_row(number, label, finished),
        None,
        pull=decision.get("pr"),
        action=("runAgent", decision["action"]),
        write=write,
    )
    actions = [action for _, found in answers for action in found]
    return _transient_answer(state, actions, decision["action"])


def stage_untrusted(gh, cfg, decision, text, write=False):
    """The state after an untrusted edit stopped the implement or fix round of `decision`
    before its agent started (`report._untrusted_stop`), as the engine decides it: `failed`,
    with attention `untrusted` and `text` as its words, and an automatic fix round's count
    given back. The host sends the stop as an `untrusted` result of the round's pending agent
    run, since only it sees who edited the issue. It decides on the stored state alone. With
    `write`, which the `report` of the item's group passes, the answer is stored
    (`decide`). Raises `Refused` for any other answer, for an item with no state to decide on,
    and with no configuration, before anything is read or written."""
    number = decision["issue"]
    version = _stage_version(decision)
    result = {**AGENT, "changeVersion": version, "output": None, "untrusted": True}
    result["error"] = str(text or "") or None
    fields = {"action": first_state.RUN_ID, "changeVersion": version, "result": result}
    event = _event("actionFinished", number, None, _run_event_id(), **fields)
    state, _ = decide(
        gh,
        cfg,
        number,
        event,
        None,
        pull=decision.get("pr"),
        action=("runAgent", decision["action"]),
        write=write,
    )
    if state["state"] != "failed" or (state["attention"] or {}).get("reason") != "untrusted":
        raise Refused(f"The engine answers {state['state']} to an untrusted stop of a round.")
    return state


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


def _pull_item(gh, number):
    """(item, pull) of a run that paused on `number`, which is a pull request when its fix round
    or review ran on it: the issue of a NexKit pull request of this repository, which holds
    the state, and the pull request, or `number` itself and None for an issue and for any other
    pull request, as `route.work_item` keys the queue. Raises `Refused` when GitHub cannot be
    read."""
    from .route import issue_from_branch

    try:
        if "pull_request" not in gh.issue(number):
            return number, None
        head = gh.pull(number).get("head") or {}
    except GitHubError as error:
        raise Refused(f"GitHub could not be read for #{number}: {error}") from None
    issue = issue_from_branch(head.get("ref"))
    if issue is not None and (head.get("repo") or {}).get("full_name") == gh.repository:
        return issue, number
    return number, None


def resumes(gh, cfg, run, number, now, label=None, write=False):
    """Whether the engine runs again the command of the resume record of `run`, which `number`
    paused at the usage limit or which waits to run again after a transient error, at `now`
    (`maintain.resume_one`): True to start it, which a hold on the issue, the `label`, does
    not stop. With `write`, which the `step` run of the item's group passes, the answer is
    stored and the wait is gone from the stored document (`decide_all`); the `resume` run
    only previews it. It decides on the stored state alone: the tick names its `usageLimit` or
    `time` wait, and `held` goes first only when the state is not held. A run paused on a
    NexKit pull request of this repository (`number` is the pull request) is decided on the state of
    its issue (`_pull_item`). Raises `Refused` with no `cfg`, before it reads anything, and for
    a command that NexKit never writes."""
    if cfg is None:
        raise Refused("The configuration did not load, so there is no state to resume on.")
    resume = run["resume"]
    command = str(resume.get("command") or "")
    if command not in RESUMED:
        raise Refused(f"NexKit writes no resume of the command `{command}`.")
    stage = RESUMED[command]
    target = str(resume.get("number") or number)
    retry = resume.get("retry")
    retry = retry if isinstance(retry, int) and not isinstance(retry, bool) else 0
    kind = "time" if retry else "usageLimit"
    # A fix round and a review run on their pull request.
    pull = int(target) if stage in ("fix", "review") and target.isdigit() else None
    item = number
    if pull is not None and pull == number:
        item, pull = _pull_item(gh, number)
    # The id names this wait by its resume time: the engine ignores an id that it still holds
    # among its last events, and one issue resumes more than once.
    tick = _event("tick", item, usage.iso(now), f"resume-{number}-{resume.get('at')}", wait="w1.1")
    _, answers = decide_all(
        gh, cfg, item, _hold_row(item, label, tick), None, pull=pull, wait=kind, write=write
    )
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
