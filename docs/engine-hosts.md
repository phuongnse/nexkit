# Runners, policies and hosts

_Part of NexKit 7's engine, which is planned as [the engine](engine.md) says under its title, with what is built so far._

This page holds what a host brings to the engine: the runners that run each stage's agent, the
policies a host decides, and how hosts carry out the engine's work.
[The engine](engine.md) holds the calls, the state document, the sweep and the protocol's versions.


## Runners

A runner runs one agent session for one stage. Its contract is part of the protocol: `runAgent`
gives it the stage, the version of the change it starts from, the stage's access, the prompt that
`prompt` writes from the inputs it names, the name of the stage's result schema, the agent, the
model and effort, the budget in tokens, time and model calls, a session id to ask the same session
again, and for a fix round the checks it looks into first (`diagnose`). The agent, model and effort
are the stage's values in the `agents` policy, each null when the host gives none. The host adds to
the stage's result schema what the `prompt` call's `checklist` and `profiles` add: a checklist
field, and the names of triage's profiles. Its result gives the stage's result, the tokens as input,
cached and output, the seconds it spent, a usage limit with its reset, whether the stop was
transient, whether it ran out of time, whether no result fit the schema, whether the host stopped
the run before its agent because someone without write access edited what it reads, and the
version of the change it produced.

An agent's fallbacks stay the runner's: it runs them in turn within the one action.

Each stage has its access, which the runner enforces: none for triage, read for plan and review,
write for implement and fix. A limit on what work costs belongs to one of three places, each where
its facts are:

- **The runner** decides a limit on one session, since only it sees each model call as it
  happens. Its result says that the run stopped at it.
- **The engine** decides a limit on one item across its stages and rounds, since stopping an item
  is a move between states. The `budgets.item` policy gives an item's tokens and seconds. Its
  tokens count every run as input plus output, review included. Its time counts from the start of
  its change after a plan's approval, a request for changes, or a person's fix or retry. Each
  implement, fix or review run's budget is its stage's, lowered to what the item has left, and the
  item goes to `failed` once either runs out.
- **The host** decides a limit on many items or an account, such as a quota or who pays for a
  key. It refuses to start work, or sends a `stopped` event.

There are two kinds of runner:

- **An agent CLI in a workspace:** the agent works on files, and the host publishes them. This repository holds one, which [Hosts](#hosts) describes.
- **A model with tools:** the agent works only through the tools the host offers for the stage,
  such as an MCP server. A host that keeps its users' keys implements this runner itself, so that
  a key never leaves the host.

The engine never sees a secret. The host gives each runner only its own provider's secrets. A
usage limit belongs to an account or a key, not to one item: the item waits for the reset, and
the sweep starts no item on that account until then.

**One result schema for each stage.** The protocol holds the schema of each stage's result:
the plan with its questions, split and no-change finding, the implement and fix result, and the
review with its verdict, criteria (`met`, `unmet`, `unverifiable`), findings (`blocking`,
`suggestion`), checklist answers and earlier findings' resolutions. Every runner returns these
shapes, and a host shows them in its own words.

## What a host decides

A host gives the engine its policies in its configuration. Each has a default, and the
protocol's `policies.schema.json` names it, as the first column does below. Seven settings are not
policies of the engine, and each has its place:

- **Start** is the host's. It sends `requested` when a person or its sweep starts an item, and calls
  `sweep` only when it starts items by itself.
- **Checklist and profiles** are the `prompt` call's policies. `checklist` is the items the plan and
  the review answer, and `profiles` the profiles triage chooses from. The plan prompt also names the
  implement stage's time from `budgets`. `noChange` is the findings that show a plan or implement
  stage that no change is needed, which the engine refuses a plan or implement call without.
- **Evidence and required checks** arrive in the host's words: the evidence's name and
  description, each run's `since` text, the note on what its checks keep of result files, and
  for each required check why the host refused and what the check is. See
  [The prompt](engine.md#the-prompt).
- **Words** are the `prompt` call's `words`: how the host names its work item, its change, the
  places where people write and read, and its commands, in the prompts of every stage. The
  engine's templates hold the stage's contract and read these words in place of any host's own,
  and it refuses a stage prompt without them. An application might send "request R-42",
  "draft 3" and "in the app". See [The prompt](engine.md#the-prompt).
- **Workspace text** is the `workspace` input's `text`: the host's sentences on how the agent
  sees and edits the change, which the plan, implement and review prompts read in place of any
  host's own, and the engine refuses a `workspace` without them. An application might send
  "working through the application's tools", "the application's validation rules" and
  "`settings/limits`". See [The prompt](engine.md#the-prompt).
- **`maxParallel` and `priority`** are the `sweep` call's policies: the most items that run at
  once, and the tags that go first. Neither is in `policies.schema.json`, so no state document
  keeps them.
- **Usage limits** are in the `runAgent` result's `limit` with its `reset`, which is null when the
  host does not resume, and in the `sweep` call's `limited`.

| Policy | Values | An application, for example |
| --- | --- | --- |
| Plan approval (`planApproval`) | `person`, `autoWhenClean` | `person`, always |
| Answers (`answers`) | `never`, `recommended`; the engine answers only with `autoWhenClean` | `never` |
| Review after failing checks (`reviewAfterFailingChecks`) | `always`, `onlyAfterPassing` | `onlyAfterPassing` |
| Accept (`accept`) | `person`, `autoWhenPassed` | `autoWhenPassed` only when the application and the request allow it, else the person who sent it |
| Fix rounds (`fixRounds`) | 0–10, and which rounds count | 3; a request for changes and a retry start the count again |
| When fix rounds run out (`whenRoundsRunOut`) | `person`, `failed` | `person`: the draft is ready with its open findings |
| Conflicts (`conflicts`, `conflictRounds`) | `person`, `resolve`, `redo`, by who accepted; the most rounds that resolve or redo, 1–10 | The agent's accept: `redo` on a new draft. A person's accept: the person asks to make it again |
| Budgets (`budgets`) | Tokens, seconds and model calls per stage; tokens and seconds per item | Tokens and time per request, model calls per stage |
| Retries (`retryWaits`) | Waits in minutes after a transient stop, at most 10, each 0–330 | The lease of a claimed request |
| Lost work (`lostWaits`) | Waits in minutes before lost agent work or checks start again, at most 10, each 0–330; a person after the last, or after one more start when its agent ran | None |
| Agents (`agents`) | Agent, model and effort per stage, each a string or null; fallbacks stay the runner's | The model the request names |
| Splits (`splits`, `splitParts`, `splitDepth`) | Off or on, with the most parts, 1–10, and the depth, 1–10; the re-plan for fewer parts, no parts or repeated titles happens only with `autoWhenClean`, and a person's approval of a split that these rules refuse goes on as one change | On: the person approves the split |
| A parent's end (`parentEnd`) | `whenPartAccepted`, `person`: whether the parent ends once every part ended and one was completed, or a person decides (`splitIncomplete`); a host that leaves it out gets `whenPartAccepted` | `whenPartAccepted`: the parent ends when its parts end |
| After a failure (`afterFailure`) | `person`, `replan`: whether an item that failed because its round was blocked with nothing to answer, or because its implement round failed on an error, with no change, is planned again after a person's comment or the end of an item that blocks it | `person` |

**Someone else may do a stage.** `created` says who does each stage: the engine's runners, a
person, or an outside app that writes the plan and the change through the host. An item made
with its plan starts in `awaitingApproval`. A stage done outside ends with `actionFinished` for
the action the engine waits for, as a runner's would, under the host's rules of trust. The one
who made a change never reviews it. A review that could not run, because no reviewer was
available, leaves the accept to a person.

## Hosts

Each host maps its platform onto the engine's words. The table gives GitHub's mapping and, as an example, an application's.

| | GitHub | An application, for example |
| --- | --- | --- |
| Work item | An issue, or a person's pull request for a review-only item | A request that a person sends |
| Change | A pull request on `nexkit/issue-<n>`, its version a commit | A draft, its version its newest revision |
| Where the state lives | One comment of NexKit's App on the work item, ending with the marker `nexkit:item-state` | The request's row in the application's database |
| Events from | GitHub's events, commands and the hourly schedule | The application's routes, its tools for agents and its own workers |
| Runs actions in | Workflow runs, a round as one run | The application's worker |
| Checks | The repository's `checks` | The application's own validation and tests |
| Accept | A squash merge | The application's own accept of the draft |
| People who decide | `notify` | The person who sent the request |
| Tells people | A mention | A notification |
| Tags and group in the sweep | An issue's labels; the index of its milestone among the open milestones by due date | A request's team or release, as the application ranks them |
| Hold | A hold label, whose name is the reason. Adding one counts only when autopilot takes the issue, and removing one always | The application's own reason |
| Version | The head commit of the pull request's branch, read from the branch's ref; the pull request's `head.sha` once the branch is deleted, as after a merge, or for a fork | The draft's newest revision |
| Observed conflict | `mergeable` is false (`mergeable_state` is `dirty`). While `mergeable` is null, as just after a push, the host reads again before it sends; a merged pull request keeps it null, and its observation says `conflicts` false | The draft no longer applies to the latest release of what it changes |
| Observed merge | The pull request's `merged`, which needs no `mergeable` | The draft was published outside the engine |
| Observed as empty | No file differs between the base and the head | The draft changes nothing any more |
| Observed as behind | `mergeable_state` is `behind`, and after a move of the base branch the base branch's rules want pull requests up to date while the head lacks its tip; left out of an observation when it is not | The draft was made on an older release than the latest |
| Observed as acceptable | `mergeable_state` is `clean`, `unstable` or `has_hooks`; left out of an observation when it is not. The sweep sends it for a pull request whose merge GitHub refused, so that the engine tries the merge again | The draft would be accepted now |
| Observes on | A push to the base branch, for each open change (the push run); each sweep; a merge's report; a command's run in the item's group | A change of the draft or of the release; its worker, from time to time |
| A change closed unmerged | Not an observation: the host tells a person, who drops the item or asks for the work again | The draft was discarded: the same |
| Part | A sub-issue of the work item it was split from | A request that the application files for one step of a split |
| Blocker | Work that the item waits for, named by its *blocked by* links | A request that the item waits for |
| Trust | A writer's vouch, as [vouching for an issue](autopilot.md#vouching-for-an-issue) says | A request from a person whom the application lets ask for changes |
| Event ids | `comment-<id>` for a comment, whether it is a remark, an answer or a command; `review-<id>`; `issue-event-<id>` from the issue events API; `dispatch-<run id>`; and `run-<run id>` for an agent's result, so that the publish job, the report job and the sweep give one result the same id. An id is built only from GitHub's number, never from a text, and a comment and a review with one number stay apart by kind | The delivery's id with the kind of event, such as `<delivery>:<kind>` |
| Cursor | A hidden line before the state marker, with the time and the event id of the newest source taken | A column beside the request's state |
| State size | The state document as base64 in one comment, of at most 65,536 characters (`TEXT_LIMIT`) | A text field of the same length |
| State for `prompt` | Built from NexKit 6's state comment | The request's state row, passed on with each call |
| Stopped | Stops a run for an agent it may not run, a change already on the base branch, or checks that the base branch's move stopped | A key that its provider refused, or a person who was disabled |
| Session limit | `stages.*.timeout_minutes`, the seconds one session may run | The runner's own limit on one session |
| Runner | The agent job runs `agent.run_action` on the action that `agent.action_for` builds from the configuration; see GitHub's runner below | A model with tools, which the application runs itself, so that its users' keys never leave it |
| One event at a time | An issue's commands and its NexKit pull request's rounds share one concurrency group keyed by the issue, which the workflow's `item` job derives with `nexkit item` and passes on (a person's pull request is its own item). A run that waits may be replaced by a newer one, so a run in the item's group takes every event of the item that is still at its source (`host.take`): comments, reviews and hold-label changes after the state comment's cursor (adding a hold label only when autopilot takes the issue, as NexKit 6 reads it, and removing one always), oldest first, each with its stable id from its source, such as the comment's id (see [Events and actions arrive more than once](engine.md#events-and-actions-arrive-more-than-once)). A command whose run was replaced is answered by the run that ran, and a run whose writes keep losing fails and leaves its event for the next run. `nexkit route` then carries out the answer that `take` stored for the run's own command, and does not send the command to the engine a second time, which would be a duplicate: each command is stored as one revision. A command that an earlier run took starts nothing and gets no reply. A command whose own run a newer run replaced starts its stage in the run that ran, when that run's own event starts nothing, and the command gets 👀 as it would have in its own run; a run whose own event starts a stage or a `maintain` task starts nothing else, so the person posts the other command again. `nexkit route`, the first job of every run in an item's group, takes them before the run's own work, and fails the run when every write loses. Before taking, the first run of an open item with no state comment stores the item's import from NexKit 6's markers as the App's state comment at revision 1, with no cursor, so that what came after it is taken; a closed item stores none. A finished plan of an issue that autopilot takes, with no collaborator's comment after it, is imported as the engine's answer to the plan's result, with the plan run among the events seen. The plan's `report` sends that result once for every plan of such an issue and stores the answer, and `plan_waits_for` reads the stored state alone, stores nothing and refuses without GitHub, a configuration or an item. The sweep and the step run (`after_plan`) carry out the stored outbox after a plan: they send the engine only the plan's result, while the stored state still waits for that plan run and has seen neither its result nor a comment after the plan, and a collaborator's comment after the plan that the state has not taken, and an item whose stored state holds nothing to carry out waits for a person. A state comment that someone other than the App changed counts for nothing: the run stores and takes nothing for the item, warns, and goes on, and the item waits for a person, who deletes the comment to have the item imported again. The decisions after a plan, for a new issue's first plan, after a writer's vouch, at a merge, with the wait for required checks and the refusals of a merge, after a blocked or failed `/nexkit go`, after a stopped or timed-out round, at a usage limit or a transient error, when a resume comes due, for a lost command and about a pull request's rounds (a round's end, its conflicts, the end of a wait for a hold or the base branch, and which rounds count) read the stored state document and, in the item's group, store the engine's answer at the next revision (`host.decide`, `host.decide_all`): the `publish` job after it pushed a round's commit, the `report` after a stage, a round or at a merge, the `step` run, the approval's merge and the `step` that starts a due resume write, and a write that lost reads again, waits and tries again. A decision that sends several events in a row, such as a hold and then the stop, stores one answer, the state after the last event, at the revision it read plus one. The publish job of an implement or fix round stores its agent's result and then the publish result of a commit that does not conflict, as one write, right after it pushes the commit, each only while the stored state has that action pending, so the stored state waits for the round's checks on the published head while they run. The round's `report` finds it stored and sends nothing again, or stores it when the publish job could not; a round that the import placed in its checks stores nothing new, and a conflicting commit's publish result is sent by the decision about the conflict (`host.round_published`). The import places a NexKit 6 round whose run is in its check jobs in the same state. A round's checks and its review are two events that GitHub shows separately, so each is stored as its own revision, and a round's end decides on the stored state alone: with the round's checks pending, a hold goes first while the state is not held, then the checks' result and then the review's; with the review's agent pending, as after a usage limit or a transient stop of the review, only the review's result goes; with neither, the round waits for a person with that reason. The automatic fix rounds used and a transient review's runs again are the stored counts, and the conflict rounds used are the stored count. The sweep, `nexkit status`, the run in `resume`, the push run, the hourly schedule and another item's merge only preview them (the sweep's wait for required checks keeps the time of its first wait in the stored state) and store nothing, and an item with no state comment stores nothing from them. The conflict round that the push run, the schedule or another item's merge dispatches is counted by its own run in the item's group: that run sends the conflict through the engine, stores one more conflict round used and the pending fix round, and starts the round only when the engine answers one. The review that follows a branch update is stored in the same way: it sends the update's result for the pending update, after the observation that the old head was behind when the push run made the update, so that the update is counted and the checks of the new head follow. When the push run stopped the checks of a counted automatic fix round, the run that it started (the conflict round or that review) gives the round's count back in the stored state. The sweep carries out no item's step itself: it sends a `step` event into each item's group, and that run carries the step out. The sweep still writes NexKit's own labels, CI bug issues and done notices, and its conflict rounds on pull requests, from the `autopilot` group. An approval's run in `approved-N` writes nothing: it previews the engine's answer on the stored state and sends one `step` event for the item's issue, and the run that takes it in the item's group merges and stores the engine's answer, so that run, not the approval's, writes the item's state. A command's and a closing command's answers read the stored state alone too, with no document built from NexKit 6's markers: a writer's `/nexkit plan` or `/nexkit go` whose note picks the option that only closes the issue, among the questions that the state waits for, is taken as an `answered` event while no change is open, so that the engine closes the issue, and the run carries out the stored `close`; a command that the engine does not take on the stored state, such as `/nexkit plan` while a NexKit pull request is open or `/nexkit fix` on an item with no change, gets the engine's reply. A command on a closed issue or pull request is refused by a host check before the engine is asked, in NexKit 6's words ("The issue is closed", "The pull request is closed"), since a state comment stays after an item closes, and `take` only moves the cursor past it, so no command changes a closed item's stored state. A merge that goes through is stored as the `accept` action's `accepted` result, whether or not the issue has a parent, and the `close` action that the engine asked for is stored as finished once GitHub shows the item closed: by the merge's `report`, by the `step` run of a parent that its last part closed, and by the item's own `issues: closed` run, each in the item's own group, so that a closed item keeps no `close` pending. A part's end reaches its parent's state in the same way: the `issues: closed` run of a sub-issue previews the parent's answer, still closes the parent as before, and sends a `step` event with the note `part N` into the parent's group, and the run that takes it there reads the part back from GitHub, stores the engine's answer to the part's end and writes nothing else. An implement round that finds no change is needed, and a fix round that finds its change on the base branch, store the engine's answer in the item's own state from the `report` of the item's group. The run in `resume` that waits for paused commands writes nothing either: for each resume that is due it sends a `step` event into the item's group, and that run reads the paused run comment again, starts the command, clears the wait in the stored state and marks the comment continued. The `autopilot`, `resume` and `approved-N` groups are groups of their own, and no run in them takes an item's events (`nexkit/templates/workflow.yml`) | The request's row, locked while its event is handled |
| One writer of the state | The item's concurrency group is the state comment's only writer, which stands for the compare-and-set. Every run that writes an item's state is in that group. Runs in groups of their own only preview, or send a `step` into the item's group | The request's row lock, as in the row above |

[`tests/apphost.py`](../tests/apphost.py) is the example of an application host. It reaches the
engine only over HTTP, stores each request's state in a row and carries out only pending actions.
[`tests/test_apphost.py`](../tests/test_apphost.py) plays it against `nexkit-engine serve`. The host keeps
a clock that sends `tick` when a wait's time comes, makes parts that start when created, and carries
out every action kind, which a test checks.

What exists only because of GitHub stays in the GitHub host: the GitHub App, statuses, branch
clean-up, watching CI and flaky runs, Dependabot, shards, artifacts, carrying a review over a base merge,
and up-to-date rules. Each reaches the engine as an event or a result: a failure the base branch
has is a `runChecks` result, and a branch behind its base is an `accept` refused as `behind`,
or an observation while its checks run, which the engine answers with an `updateChange` that the
host carries out by merging the base into the branch.

The state comment's own write checks the revision too, as a backstop to the one concurrency
group per item: the GitHub host writes a state only as the next revision of the one its caller
read, and reads the comment again by its id just before it edits it, making no write when the
stored revision moved. GitHub has no compare-and-set, so a write by anyone else between that
read and the edit is still overwritten: the check narrows the race to that moment and does not
close it. A write that loses reads the state again and handles the event again, after a short
wait of random length, a few times. When every try lost, the outcome depends on the write. The
run that takes events from their sources (`host.take`) fails, and each event stays at its source,
past the cursor, so that the next run takes it, never dropped. A decision's write (`host.decide`,
`host.decide_all`) only warns, and the run carries out the answer, which was not stored, because
failing would lose the run's report or step. The next decision stores its own. The publish job's
store of a round only warns too, and the round's report stores it.

The GitHub host sets each policy and its own settings as follows. The first column names each one, as [What a host decides](#what-a-host-decides) does.

| Setting | GitHub |
| --- | --- |
| Plan approval (`planApproval`) | `person`; `autoWhenClean` with autopilot |
| Answers (`answers`) | `never`; `recommended` with `autopilot.answer` |
| Review after failing checks (`reviewAfterFailingChecks`) | `always`: one fix round takes the failures and the findings |
| Accept (`accept`) | `person`; `autoWhenPassed` with `merge.auto` |
| Fix rounds (`fixRounds`) | `fix.max_auto_rounds`: rounds a person asked for, resumed, retried and stopped rounds do not count |
| When fix rounds run out (`whenRoundsRunOut`) | `person` |
| Conflicts (`conflicts`, `conflictRounds`) | `person`; `resolve` with `conflicts.auto_resolve`, at most `conflicts.max_rounds` |
| Budgets (`budgets`) | Each stage's `stages.*.timeout_minutes` as its seconds |
| Retries (`retryWaits`) | `retry.wait_minutes` |
| Lost work (`lostWaits`) | None; the waits of `autopilot.AGAIN` with autopilot, given at each loss: only the first for a start that may run only once more, none for a person's command |
| Agents (`agents`) | `agent`, `model`, `effort` and `stages`, with the profile triage chose; plan null when there are profiles |
| Splits (`splits`, `splitParts`, `splitDepth`) | Off; on with autopilot, at most `MAX_PARTS` parts (`nexkit/textutil.py`) and `autopilot.MAX_DEPTH` levels |
| A parent's end (`parentEnd`) | `whenPartAccepted` with `close_parent_issues`, else `person`: the engine's default differs from GitHub's, where `close_parent_issues` is off by default |
| After a failure (`afterFailure`) | `person`; `replan` with autopilot |
| Start | On a request, and by the sweep with autopilot |
| Checklist (`checklist`) | `review.checklist` |
| No-change findings (`noChange`) | A commit on the default branch that already fixed it, and a failure of CI from outside the repository that a newer run passed since |
| Evidence and required checks | The text of its CI runs (`the CI runs`, the folder the agent job downloaded them to, and no GitHub access in the session), a *Since* text that says whether a newer run of the workflow passed on the default branch, "GitHub refused the merge" with the sentence on branch protection, and "a check job keeps at most 50 MB of result files". It builds them from fixed text, numbers and the runner's own folder, never from what anyone wrote, and the prompts read as they always did. |
| Words (`words`) | "GitHub issue #5" for the item, "pull request #6" for the change, "on GitHub" for where people write and read, and `/nexkit go`, `/nexkit fix` and "`/nexkit` commands" for its commands. It builds them from the numbers of the issue and the pull request and from fixed text, never from what anyone wrote. |
| Workspace text (`workspace.text`) | "working in a checkout of the repository" for where the agent works, "CI configuration" and "workflows" for what runs the checks, "`.github/workflows/ci.yml`" for a protected path, and its other texts, as fixed strings, so its prompts read as they always did. It builds them from fixed text, never from what anyone wrote. |
| `maxParallel` and `priority` | `autopilot.max_parallel` and `autopilot.priority` |
| Usage limits (`limit`, `limited`) | `usage_limit.*` |
| Item budget (`budgets.item`) | None |

**GitHub's runner.** The agent job builds each stage's action from the configuration with `agent.action_for`, and `agent.run_action` runs it. It refuses an action that does not fit the protocol, or that names another agent, model, effort or access than the configuration gives the stage, before any agent starts. The engine's `runAgent` names those values because the GitHub host fills the `agents` policy from the configuration, with the profile of the item's latest plan. For the plan stage it gives null when there are profiles, since triage chooses the profile in the plan job. The runner is Claude Code today, behind NexKit 6's `Runtime` and `Provider`, which stay the Python interface inside it; Codex and Gemini follow when their runtimes are added. It saves its result as `run.json`, beside the stage's `result.json`. Its session and change version are null: the publish job makes the commit, and nothing asks the same session again.
