# States, events and actions

_Part of NexKit 7's engine, which is planned as [the engine](engine.md) says under its title, with what is built so far._

This page holds the states a work item moves through in the engine, what sits beside them, and the
events a host sends and the actions the engine asks for. [The engine](engine.md) holds the calls,
the state document, the sweep and the protocol's versions, and [runners, policies and
hosts](engine-hosts.md) holds what a host brings.


## States

A work item is always in one state. NexKit 6 has no field for it and reads it from its comments;
the engine makes it explicit. Beside the state, the item has a **change** or none, so that a
re-plan, a question or a failure keeps the pull request or draft that exists.

| State | What happens | Next |
| --- | --- | --- |
| `new` | The item waits to be started | `planning`, `working`, `checking`, and `fixing` for an item reopened with its change |
| `planning` | An agent writes a plan | `awaitingApproval`, `working` or `fixing`, `split`, `noChange`, `failed` |
| `awaitingApproval` | The plan, or a split, waits for its approval or for answers, or a round's questions wait for answers | `planning`, `working` or `fixing`, `split`, `noChange`, `dropped` |
| `working` | An agent makes the change | `checking`, `awaitingApproval`, `planning`, `noChange`, `failed` |
| `checking` | The host's checks run on the change | `reviewing`, `fixing`, `ready`, `failed` |
| `reviewing` | A second agent session, with no memory of the first, reviews the change | `ready`, `accepting`, `fixing`, `failed` |
| `fixing` | An agent works on the failures and findings, in the same change, or its round waits to start | `checking`, `awaitingApproval`, `planning`, `working`, `ready`, `failed` |
| `ready` | The change passed, or a person decides what is left | `accepting`, `fixing`, `checking` |
| `accepting` | The host accepts the change | `accepted`, `ready`, `checking`, `fixing`, `working`, `failed` |
| `accepted` | The change is in | `new`, when a person reopens it |
| `split` | The work is carried by its parts | `accepted`, `dropped` |
| `noChange` | Nothing needs to change, and the item says why | `new`, when a person reopens it |
| `dropped` | A person ended the work | `new`, when a person reopens it |
| `failed` | A step failed in a way that waiting does not help, or a token or time budget ran out. The change is kept, and the item waits for a person whatever is observed of it, but a merge or an emptied change | `planning` when a person asks, when a writer vouches after an untrusted stop, or when the failure policy plans an item with no change again; `fixing` and `checking` only when a person asks, or a writer vouches after an untrusted stop; `ready` when a writer vouches and no automatic fix round is left; `dropped` |

- **Approval leads to the change that exists.** An approved plan goes to `working` when the item
  has no change, and to `fixing` when it has one, such as after a re-plan or a question in a fix
  round. An item that has a change is split only from the plan that a round which ran out of
  time asked for, and that split withdraws the change, so nothing is left with no end. Once a
  person approves the split plan of an item with a change, it goes on as one change. A split goes
  on as one change too when its rules refuse it, as below. A plan that asks questions waits for
  answers, even one an item was made with.
- **What follows a plan.** Under `person`, every plan waits for a person, a plan that finds no
  change too, for what NexKit 6 asks of them first: the approval of a plan that is too large or
  finds no change, a hand-made change, answers, or the approval of a clean plan. A person may
  answer a plan's questions whatever its attention names first, under `autoWhenClean` too. Under
  `autoWhenClean`, as NexKit 6's autopilot decides:
  - A plan that is too large is split into its parts, all of them, when splits are on and the
    item has no change, or has one and the plan is the one that a round which ran out of time
    asked for (`supersede`). A split of an item with a change withdraws the change. A split is
    refused for an item at `splitDepth` levels of parts, which waits for a person, and for a plan
    with more parts than `splitParts`, with no parts, or with a part titled by the item's own
    title or starting with it, ignoring case, spaces and punctuation at its end. For each of these three the item is planned once more, and a second such plan
    waits for a person.
    A split ends as `accepted` once every part ended and one was accepted, under `parentEnd`
    `whenPartAccepted`; under `person`, or when no part was accepted, a person decides
    (`splitIncomplete`).
  - A plan that is too large and is not split, because splits are off or the item has a change
    and the plan is not that one, waits for a person (`approval`). Otherwise, a plan that finds no change ends the item in
    `noChange`, one that needs paths only a person changes waits for that person, and a clean
    plan is approved and starts the change.
  - Questions wait for a person unless `answers` is `recommended` and every question has a
    recommended option. Then the engine answers with them and plans again, once in a row: when
    the plan its answers started asks again and no person acted since, a person decides. A
    recommended option that only closes the item is always a person's to pick. The engine's
    answers count only for the plan they started: a plan that ends without a result, such as one
    that fails, forgets them, and the next plan's questions are answered again.
  - A person's comment on a plan that waits, on an item with no change, plans it again, since the
    plan did not hold what the comment says. A writer's answer that picks the option that only
    closes the item drops it, with no agent run, since another plan could only stop at the same
    place; on an item with a change, the answer goes to the next plan.
- **A round that stops as blocked.** An implement or fix round that asks questions waits in
  `awaitingApproval` for their answers, and the state keeps the round's questions while they
  wait. One whose change needs paths only a person changes waits for that person
  (`handMadeChange`), whatever it asks, and one that neither asks nor needs such paths fails, for
  a person. Under `afterFailure` `replan`, a person's comment, or the end of an item that blocks
  it, plans such an item again when it has no change. An implement round on an item with no
  change that fails on an error, gives no result, gives one that does not fit, or cannot
  publish its change, fails with `roundFailed` and is planned again in the same way. A
  time-out, a usage limit with no reset and a plan that fails stay `failed`, and a comment does
  not plan them again. Under `autoWhenClean`, with `answers`
  `recommended` and a recommended option for every question, the engine answers, once in a row
  as for a plan:
  - An implement round whose one question recommends only closing the item, with its finding
    that no change is needed, ends the item in `noChange`, unless the item has a change, such as
    one made anew after a conflict, which a person closes. Any other recommended option that only
    closes the item is a person's to pick (`closeRecommended`).
  - A recommended option that changes the item's scope, such as a split, plans the item again,
    and the round that plan leads to is the one the answers started, so it may not be answered
    again. On an item with a change a person decides (`replanRecommended`), since a new plan
    would leave the change behind.
  - Otherwise the round runs again with the answers.
- **An untrusted edit stops a round or a merge.** The host marks the item with attention
  `untrusted`, in `failed` for a round and in `ready` for a merge, and the engine never marks it
  by itself, since only the host sees who edited the item. A comment does not plan it again. A
  `trust` request from a writer, sent after the stop, ends it as NexKit 6 does: an item with no
  change plans again, an item with a change starts an automatic fix round, counted, and a
  stopped merge runs its checks and review again. Where no automatic fix round is left, a
  person decides (`roundsUsedUp`) instead of a round that goes past `fixRounds`, which NexKit 6
  would start. A `trust` from anyone else, or on any other stop, changes nothing.
- **What a person does on a round's questions.** A writer's answer that picks the option that
  only closes the item drops it, on an item with no change, as for a plan; any other answer
  plans the item again. Under `autoWhenClean`, a person's comment on questions the engine could
  not answer, on an item with no change, plans it again, since the round stopped where the plan
  did not say what the comment says, and so does the end of an item that blocks it; a comment on
  questions asked again lets the engine answer them again.
- **What follows a round's checks and review.** The review runs after failing checks too, by
  `reviewAfterFailingChecks`. Its verdict and its criteria decide, as in NexKit 6: a review that
  requests changes, an `unmet` criterion or a failing check leads to a fix round, and a blocking
  finding of a review that approves does not. A fix round follows while `fixRounds` has one
  left; then a person decides, in `ready` or, by `whenRoundsRunOut`, in `failed`
  (`roundsUsedUp`). The fix round waits while every failing check fails as the base branch
  does, which the checks' result says with the item about that failure, until that failure
  ends (see *Wait* below); and while a hold is on, until it ends. It is counted when its run
  is issued, as NexKit 6 counts it when it starts it: one that waited (`rounds.due`) runs only
  while `fixRounds`, as the host gives it then, has one left, and otherwise a person decides
  (`roundsUsedUp`). A fix round that left the change as it was, having found the cause outside
  it, is followed by no other: what still fails on that version is a person's (`roundsUsedUp`),
  until a person's request starts work on it again. A change that passes is `ready`: a person
  confirms the criteria the review could not verify (`unverifiableCriterion`) and decides the
  checklist items that only a person decides (`personChecklistItem`); otherwise the accept
  policy decides, and a hold keeps the engine's accept back until it ends. A required check
  that fails at the accept (`checkFailed`) leads to a fix round as a failing check of the round
  does, waiting while it fails as the base branch does, which the accept's result says.
- **Diagnosis rounds.** The checks keep each failing check by a digest of its name and failure
  signature, at most 300 of them, which 15 checks of 20 shards each reach. When a check of the
  checks after a fix round fails with the same signature as in the checks before it, the fix round
  did not find its cause, so the next automatic fix round looks into that cause first: its
  `runAgent` names those checks in `diagnose`, as NexKit 6's `route` makes it a diagnosis round, the
  first ten of them as NexKit 6 does. The checks after an implement round, a person's push or a
  person's request for a review are no fix round's, so nothing repeats in them; a conflict round is
  a fix round, and its checks compare too. Only an automatic fix round diagnoses, and so does the
  same round run again after a wait; a person's fix round, a conflict round and the round that the
  engine's answers run again name no check. A fix round that waits for the base branch's failure
  forgets what repeated, since that failure is the base branch's, and a round whose change conflicts
  at its publish runs no checks, so the checks after the next round have none of it to compare with.
  A hold keeps a diagnosis round back as any fix round, and it diagnoses when it runs.
- **While a fix round waits** for a hold or the base branch, a person's push is checked, and so
  is a person's request for a review; a person's request for a fix runs that fix. The round
  that waited no longer waits, and none of these counts: the checks and the review that follow
  decide the next round, as in NexKit 6.
- **Which rounds count.** Only an automatic fix round counts toward `fixRounds`. One that is
  stopped before its review ends gives its count back, since it could no longer lead to an
  accept and its work was not judged: the engine stops it when a conflict round takes over, or
  when a person pushes while its agent runs, and the host may stop it (`stopped`), as GitHub does
  for an agent it may not run, a change already on the base branch, or checks that the base
  branch's move stopped. Once the round has published, a person's push keeps its count, since its
  change stays under the push and the push's checks judge it, as NexKit 6 keeps it. One whose
  agent stopped by itself, such as blocked, out of time or with an error, keeps its count.
- **From every state that has a change,** the engine reconciles the item with the latest observation
  of that change after each event, once no publish or replace is on its way. A change that was
  merged, such as by a person's hand, goes to `accepted`; one that became empty, because the same
  change landed another way, goes to `noChange`; a version other than the engine's, such as a
  person's push, withdraws or cancels what acts on the old version and goes to `checking`, or
  follows the conflict policy when that version conflicts. A split parent keeps waiting for its
  parts instead, as NexKit 6 reads a parent's parts before its pull request. A change that conflicts follows the
  conflict policy at once where nothing runs that it must wait for: in `ready` and `accepting`, and
  in `checking` when a conflict round can start (`resolve` or `redo` with rounds left), since those
  checks can no longer lead to an accept. Otherwise the policy applies when the item would next go
  to its checks or be ready, as NexKit 6 starts no conflict round while another runs, and only if
  the latest observation still says that version conflicts; a question or a failure keeps its
  attention meanwhile. A person told of a conflict on a version is told once, and the item stays in
  `ready` for them even if the conflict clears without a push. A hold keeps the round that the
  conflict policy would start back, whatever rounds are left, as NexKit 6's hold label does: the
  item waits in `checking` with nothing running, and the policy applies, with its count then, once
  the hold ends; in `ready` and `accepting` it applies when the hold ends. A round that resolves a
  conflict is no automatic fix round: the count of an automatic fix round whose change conflicts at
  its publish stands, and a conflict round, whether it resolves or makes the change again, counts
  toward `conflictRounds` alone. When no round is left, a conflict goes to a person in `ready`,
  so that conflicts that keep coming never run agents without end. An observation of
  another change changes nothing, and a publish or a replace that ends sets the change with the
  observation its result carries, which replaces the latest one: one of a change the engine replaced
  never touches the new one, even when the new one keeps its id and version. An item in `failed`,
  and one in `new`, but a review-only item that waits to start, take only a merge or an emptied
  change, so the failure's or the reopen's attention stands whatever else is observed: a new
  version waits in the observation until a person's request takes the item on, such as a review, a
  retry or a fix, which then starts its work on that version.
- **Checks that run on a branch behind its base** stop, and the branch is updated at once, when
  the latest observation says that the current version is `behind` its base, with no conflict,
  while an update is left: under any conflict policy but `person`, with no hold, on a change that
  is not a person's own, and while `rounds.updates` is below `conflictRounds`. The engine cancels
  the checks, which gives back the count of an automatic fix round whose checks stop, answers
  `updateChange`, and the item stays in `checking`, since the update's result goes to its checks
  again, as after an update at the accept. Checks that the engine asked for in the same step are
  withdrawn instead, since they never started. Under `person`, under a hold, or once the updates
  used reach `conflictRounds`, the checks run to their end, as NexKit 6 lets them, and the
  merge's own `behind` refusal decides what follows. A conflict is the conflict rounds' to answer
  instead, and wins over `behind`. A version that a person pushed and that is observed behind is
  updated as soon as it would be checked. NexKit 6 reads `behind` here only from the base
  branch's rules and a comparison with its tip (`maintain.stop_superseded`).
- **From every state that is not final:** `dropRequested` goes to `dropped`, and the engine cancels
  the step that runs. A host's `stopped` event, such as a key that its provider refused or a
  person who was disabled, goes to `failed`.
- **A review-only item** comes with its change and is never planned, fixed, replaced or accepted
  by the engine: a person's own pull request, which NexKit only checks and reviews. Each review
  request takes it to `checking`, and a drop, a merge or an emptied change ends the engine's work
  without closing it, because the person's change stays theirs. A review-only item that waits to
  start is checked at once when a push is observed; one that a person reopened waits for their
  review request, which checks the pushed version.
- **An agent that stops at a usage limit** waits for the reset its result gives, and then
  the same stage runs again, as the same round, which counts nothing again and diagnoses
  again if it did. A limit with no reset, as when the host does not resume such runs, waits
  for a person in `failed`.
- **An item whose own budget runs out** (`budgets.item`) goes to `failed` and keeps its change.
  It is checked where an implement, fix or review run would start, and where such a run stops
  without a usable result. Its attention text says whether its tokens or its time ran out. A fix
  round that the stop prevents counts nothing.
- **An agent that stops on a transient error**, without a usage limit, runs the same stage again
  after each wait of `retryWaits` in turn, as the same round, which counts nothing again and
  diagnoses again if it did. A wait of 0 runs it again in the same step. After the last wait,
  the next transient stop goes to `failed` for a person, or to `roundFailed` for an implement
  round on an item with no change. The count of runs again goes on through
  the stages of one round, so a review handed on keeps it, and starts afresh when a request is
  taken or a new plan, implement or fix run starts.
- **An accept refused because required checks have not passed yet** (`waitingForChecks`) keeps
  the item in `accepting`, with a wait for them (`requiredChecks`) and a `wait` action whose tick
  comes `CHECKS_RETRY` later, so that any host ticks the wait without a tight loop. The tick
  tries the accept again. The first such refusal on a version is kept in `checksSince`, with the
  version and the event's time, and the engine drops it when the item leaves `accepting`. A start
  recorded for another version counts as missing. A tick that ends the wait more than
  `CHECKS_END` after that start goes to `ready` with `acceptRefused` instead of trying the accept
  again, so checks that never end are looked into by a person, as NexKit 6's autopilot does. The
  time is the tick's own: the engine reads no clock, and a refusal that comes in never ends the
  wait. A branch that is behind its base keeps being tried past `CHECKS_END` while the conflict
  policy is not `person`: an update brings checks that can pass. The engine knows it from the
  latest observation, which says `behind` of the current version.
- **An accept refused because the change is behind its base** (`behind`) is brought up to date
  without an agent while an update is left: under any conflict policy but `person`, and while
  `rounds.updates` is below `conflictRounds`, the engine answers `updateChange` and the item stays
  in `accepting`. The update is the same change at a new version, so a successful result counts
  one update, sets the change with the observation it carries, and goes to `checking`, as a
  publish does. Updates count apart from conflict rounds, and `conflictRounds` limits each. Under
  `person`, with no update left, or when the update fails, the item goes to `ready` with
  `acceptRefused`. An observation that arrives while an update is on its way is no person's push.
- **An accept refused for a reason that waits for a person** (`headMoved`, `transient` and
  `needsPerson`) goes to `ready` with `acceptRefused`, and one that was not accepted
  (`notAccepted`) to `failed`. A host that read a newer version of the change sends it as an
  observation before the refusal, which the engine checks as any push: the item goes to
  `checking`, the pending accept is cancelled, and the refusal that follows changes nothing. A
  `headMoved` with no newer observation waits for a person, since the engine has no version to
  check.
- **A person's approval of a change that waits for its accept** (`changeApproved` in `ready`
  with attention `accept`, `acceptRefused` or `reopened`) accepts it under the `autoWhenPassed`
  accept policy and changes nothing under `person`, where that person accepts it themselves. An
  approval after `acceptRefused` or `reopened` is of any version of the change, as NexKit 6 tries
  a refused merge again on the head that passed. One for `accept` is of the change's version,
  and another version changes nothing. It changes nothing while the latest observation says the
  change conflicts, which is the conflict policy's, and for an item that is only reviewed. It
  goes to `accepting` with an `accept`, which a hold keeps back.
- **An agent that runs out of time** in an implement or fix stage: with splits on, as autopilot
  has them, the item asks once for a plan that splits it, and the state records that it asked;
  otherwise, the second time, and for an item at `splitDepth` levels of parts, which is not
  split, it goes to `failed` for a person, as NexKit 6 does. A host marks a part whose parent
  already asked as having asked, since NexKit 6's parts inherit that. An item that has a change
  asks too, as NexKit 6 does after a fix round, and marks the plan `supersede`. If that plan is
  too large, the split rules apply and `withdrawChange` ends the change. Any other plan goes on
  as one change on it. The mark is dropped once a fix or implement run starts, or the item is
  split.
- **A final item that a person reopens** waits for a person, with attention, as NexKit 6 does.
  One whose change went in or became empty, as its state or the latest observation says, such
  as a change merged after a person dropped the item, starts again without it and with its rounds
  counted afresh.

Three things sit beside the state, never in place of it:

- **Attention:** a person is needed, with what the person decides on, such as the plan whose
  questions are open, and the reason: a plan that waits for its approval
  (`approval`), a question with no recommended answer (`question`), a plan that asks again right
  after the engine answered it (`askedAgain`), a recommended answer that only closes the item
  (`closeRecommended`), a recommended answer to a round's questions that changes the scope of an
  item with a change (`replanRecommended`), a plan too large for an item at the deepest level of parts
  (`splitDepth`), a second plan that still has too many parts (`tooManyParts`), no parts
  (`noParts`) or parts titled by the item's title (`partTitles`), a change that needs paths
  only a person changes (`handMadeChange`), a criterion the review could not verify
  (`unverifiableCriterion`), a checklist item only a person decides (`personChecklistItem`), a
  change that waits for a person's accept (`accept`), a refused accept or a conflict
  (`acceptRefused`, about the change version for a conflict, and for required checks that did not
  pass in time), fix rounds used up
  (`roundsUsedUp`), a split that is not complete (`splitIncomplete`), a round that was blocked
  (`blocked`), a step that failed or an item whose token or time budget ran out (`failed`), an
  implement round with no change that failed on an error
  (`roundFailed`), a round that an untrusted edit stopped (`untrusted`), a final item opened
  again (`reopened`), or an
  action the host lost (`outage`).
- **Hold:** nothing new starts by itself: the sweep's start waits, while a person's own request
  still runs, as NexKit 6's hold label does. A step that runs finishes and records what it held
  back. A request that would lead to the conflict policy's work, such as a person's request for a
  review of a change that conflicts, is taken but that work still waits: the hold wins, and the item
  waits in `checking` until it ends. A hold does not stop a step that runs: a run paused at a usage
  limit or waiting after a transient stop runs again at its tick, as NexKit 6's resume does under
  the label (`maintain.resume_paused`). A fix round that is due, an accept, a conflict round
  and the start again of lost work wait until the hold ends, which starts the work again.
- **Wait:** the next step waits for something named: a request to start (`start`); a time, such
  as after a transient stop (`time`); the reset of a usage limit (`usageLimit`); an item that
  blocks it (`blocker`); the parts of a split (`parts`); a failure the base branch has
  (`baseFailure`); or required checks before a merge (`requiredChecks`), whose first start is
  kept beside the waits, as the bullet on a refused accept above says. Each wait has an id that
  is new each time, and the event that ends it names it. A tick ends only a wait for a time:
  `time`, `usageLimit` or `requiredChecks`. When a blocker, or the bug issue of a base failure,
  closes, the waits on it end, and an item does not start while a blocker is open. A host
  reports that the base branch passes again, so that its failure is gone though its item is
  open, as the end of that item too, naming it. A base failure whose record names no item of
  its own waits on the word "none", which the host ends the first time it looks, as NexKit 6
  treats such a wait as over. An end that comes before the
  engine recorded a wait on that item changes nothing, so a host sends it again, as a new
  event, each time it finds the item closed or the base passing while a wait on the item
  stands, such as on each sweep, as NexKit 6's sweep looks at each wait. When the last wait or
  hold on a state whose work does not run ends, as after a usage limit, a transient stop or a
  hold that kept a round back, the engine starts that state's work again: the same round,
  which counts nothing again, but for a fix round that waited to be issued, which is counted
  now, and checks or an accept that start again follow the latest observation of the change
  first. A request to start does the same for a state that waits to start.

## Events

| Event | Sent when |
| --- | --- |
| `created` | A work item is made, with its kind, its title and text, how deep a part it is, who does each stage and, for an item made with a plan, the plan |
| `textChanged`, `commented` | A work item's text is edited, or a person writes on it, with their role |
| `requested` | A person or the sweep asks for something: `plan`, `planFromScratch`, `go`, `fix`, `review`, `retry`, `trust` or `start`, with a note, and the item's title and depth again when the host gives them. The engine takes `trust` only from a writer on an item that an untrusted edit stopped (the attention of that name), and starts the next command as the rule on an untrusted stop says. For any other `trust` it changes nothing, since whom a host trusts is its own check, as [vouching for an issue](autopilot.md#vouching-for-an-issue) says |
| `planApproved`, `answered`, `changesRequested`, `changeApproved`, `dropRequested` | A person decides: the plan, its questions, the change, a person-only checklist item or criterion on the change's version, or the end |
| `actionFinished` | An action ended, with its id, the change version and its result |
| `actionLost` | An action the host started never reported, with any tokens it is known to have used |
| `changeObserved` | The host read the change: its version, and whether it conflicts, was merged, became empty or, when it says so, is behind its base |
| `stopped` | The host stops the item, with the outcome |
| `held`, `released` | A hold was set or taken off |
| `blockerClosed`, `partEnded` | Work that this item waits for ended |
| `reopened` | A final item was opened again |
| `tick` | The time of a wait came, naming the wait |

## Actions

| Action | The host | Its result |
| --- | --- | --- |
| `runAgent` | Runs a runner for a stage, see [runners](engine-hosts.md#runners); for a fix round, the checks it diagnoses first | The stage's result, tokens, a usage limit with its reset, a transient stop |
| `publish` | Makes the agent's work the change: a commit and a pull request, or a draft | The change's id and version, or why it could not |
| `replaceChange` | Drops the change and starts a new one from what is current | The new change |
| `updateChange` | Brings the change up to date with the branch it was made on, without an agent: the same change at a new version | The change's id and new version, or why it could not |
| `runChecks` | Runs its checks on a version of the change | Each result, its failure signature, and whether the failure matches a known failure of the base; the item about that failure when every failing check matches it |
| `accept` | Accepts the change: a merge, or an accept of a draft | `accepted`, or refused with a kind: `transient`, `waitingForChecks`, `checkFailed`, `behind`, `headMoved`, `conflict`, `notAccepted` or `needsPerson`; for `checkFailed`, the item about a failure the base branch has when every check that failed fails as it does there |
| `createParts` | Makes the parts of a split, each blocked by the earlier parts it needs | The parts' ids |
| `withdrawChange` | Withdraws the change that a split replaces, such as closing its pull request as superseded | Done |
| `close` | Ends the work item with its outcome | Done |
| `cancel` | Stops an action that runs | Done |
| `needPerson` | Tells the people who decide, once, with the reason and what they decide on (`ref`) | Done |
| `show` | Shows, where people read the work, a plan (`plan`), a round (`round`), a review (`review`), the answers the engine gave to a plan's or a round's questions (`answers`), a stop (`stop`) or a notice (`notice`) | Done |
| `wait` | Sends a `tick` naming the wait at its time | Done |

**A chain of actions.** An action may name the action that follows when it ends as expected,
such as implement, publish, checks and review. A host that runs a round as one pipeline, as
GitHub runs one workflow, carries out the chain and then sends each result in order. The engine
answers each as if it had come alone, and ignores a following action it no longer wants, by its
id. NexKit 6's round, from `route` to `report`, stays one workflow run.
