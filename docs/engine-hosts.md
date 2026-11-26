# Runners, policies and hosts

_Part of NexKit 7's engine, which is planned as [the engine](engine.md) says under its title, with what is built so far._

This page holds what a host brings to the engine: the runners that run each stage's agent, the
policies a host decides, and how the GitHub host and an application carry out the engine's work.
[The engine](engine.md) holds the calls, the state document, the sweep and the protocol's versions.


## Runners

A runner runs one agent session for one stage. Its contract is part of the protocol: `runAgent`
gives it the stage, the version of the change it starts from, the stage's access, the prompt that
`prompt` writes from the inputs it names, the name of the stage's result schema, the agent, the
model and effort, the budget left in tokens and time, a session id to ask the same session
again, and for a fix round the checks it looks into first (`diagnose`). Until the protocol holds the
checklist and agents policies, the host adds to that schema what they add: a checklist field, and
the names of triage's profiles. Its result gives the stage's result, the tokens as input, cached and
output, the seconds it spent, a usage limit with its reset, whether the stop was
transient, whether it ran out of time, whether no result fit the schema, and the version of the
change it produced.

Each stage has its access, which the runner enforces: none for triage, read for plan and review,
write for implement and fix. There are two kinds of runner:

- **An agent CLI in a workspace:** Claude Code today, and Codex and Gemini when their runtimes
  are added. The agent works on files, and the host publishes them. NexKit 6's `Runtime` and
  `Provider` stay the Python interface inside this runner, which this repository holds. The runner
  is `agent.run_action`. It refuses a `runAgent` action that does not fit the protocol, or that
  names another agent, model, effort or access than the configuration gives the stage, before any
  agent starts. It saves its result as `run.json`, beside the stage's `result.json`. The session
  and the change version are null: the publish job makes the commit, and nothing asks the
  same session again.
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
protocol's `policies.schema.json` names it, as the first column does below. The policies with no
name there, start, budgets, usage limits, agents and
the checklist, are not in the protocol yet: they come into it as the
engine takes over the decisions that read them, and until then the host applies them itself. The
`prompt` call already reads three of them beside the host's policies, as its schema names them:
`checklist`, the items the plan and the review answer; `profiles`, the profiles triage chooses
from; and `budgets`, of which the plan prompt names the implement stage's time. The `sweep` call
reads two more beside them, as its schema names them: `maxParallel`, the most items that run at
once, and `priority`, the labels that go first. Neither is in `policies.schema.json`, so no state
document keeps them.

| Policy | Values | GitHub | An application, for example |
| --- | --- | --- | --- |
| Start | `onRequest`, `bySweep` | `onRequest`; `bySweep` with autopilot | `onRequest`: a request starts when it is sent |
| Plan approval (`planApproval`) | `person`, `autoWhenClean` | `person`; `autoWhenClean` with autopilot | `person`, always |
| Answers (`answers`) | `never`, `recommended`; the engine answers only with `autoWhenClean` | `never`; `recommended` with `autopilot.answer` | `never` |
| Review after failing checks (`reviewAfterFailingChecks`) | `always`, `onlyAfterPassing` | `always`: one fix round takes the failures and the findings | `onlyAfterPassing` |
| Accept (`accept`) | `person`, `autoWhenPassed` | `person`; `autoWhenPassed` with `merge.auto` | `autoWhenPassed` only when the application and the request allow it, else the person who sent it |
| Fix rounds (`fixRounds`) | 0–10, and which rounds count | 2: rounds a person asked for, resumed, retried and stopped rounds do not count | 3; a request for changes and a retry start the count again |
| When fix rounds run out (`whenRoundsRunOut`) | `person`, `failed` | `person` | `person`: the draft is ready with its open findings |
| Conflicts (`conflicts`, `conflictRounds`) | `person`, `resolve`, `redo`, by who accepted; the most rounds that resolve or redo, 1–10 | `person`; `resolve` with `conflicts.auto_resolve`, at most `conflicts.max_rounds` | The agent's accept: `redo` on a new draft. A person's accept: the person asks to make it again |
| Budgets | Tokens, time and model calls, per item and per stage | Time per stage | Tokens and time per request, model calls per stage |
| Retries (`retryWaits`) | Waits in minutes after a transient stop, at most 10, each 0–330 | `retry.wait_minutes` | The lease of a claimed request |
| Lost work (`lostWaits`) | Waits in minutes before lost agent work or checks start again, at most 10, each 0–330; a person after the last, or after one more start when its agent ran | None; at once, then 30, 60 and 120 minutes with autopilot | None |
| Usage limits | Resume after the reset, and how long to wait: the host gives the reset, or none when it does not resume | `usage_limit.*` | Resume after the reset |
| Agents | Agents, models and efforts per stage, fallbacks, profiles and triage | `agents`, `stages`, `profiles` | The model the request names |
| Splits (`splits`, `splitParts`, `splitDepth`) | Off or on, with the most parts, 1–10, and the depth, 1–10; the re-plan for fewer parts, no parts or repeated titles happens only with `autoWhenClean`, and a person's approval of a split that these rules refuse goes on as one change | Off; on with autopilot, at most 10 parts, 3 levels | On: the person approves the split |
| A parent's end (`parentEnd`) | `whenPartAccepted`, `person`: whether the parent ends once every part ended and one was completed, or a person decides (`splitIncomplete`); a host that leaves it out gets `whenPartAccepted` | `whenPartAccepted` with `close_parent_issues`, else `person`: the engine's default differs from GitHub's, where `close_parent_issues` is off by default | `whenPartAccepted`: the parent ends when its parts end |
| After a failure (`afterFailure`) | `person`, `replan`: whether an item that failed because its round was blocked with nothing to answer, or because its implement round failed on an error, with no change, is planned again after a person's comment or the end of an item that blocks it | `person`; `replan` with autopilot | `person` |
| Checklist | Items the plan and the review answer, some for a person | `review.checklist` | The application's own checklist |

**Someone else may do a stage.** `created` says who does each stage: the engine's runners, a
person, or an outside app that writes the plan and the change through the host. An item made
with its plan starts in `awaitingApproval`. A stage done outside ends with `actionFinished` for
the action the engine waits for, as a runner's would, under the host's rules of trust. The one
who made a change never reviews it. A review that could not run, because no reviewer was
available, leaves the accept to a person.

## Hosts

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
| Observed version | The head commit of the pull request's branch, read from the branch's ref; the pull request's `head.sha` once the branch is deleted, as after a merge, or for a fork | The draft's newest revision |
| Observed conflict | `mergeable` is false (`mergeable_state` is `dirty`). While `mergeable` is null, as just after a push, the host reads again before it sends; a merged pull request keeps it null, and its observation says `conflicts` false | The draft no longer applies to the latest release of what it changes |
| Observed merge | The pull request's `merged`, which needs no `mergeable` | The draft was published outside the engine |
| Observed as empty | No file differs between the base and the head | The draft changes nothing any more |
| Observed as behind | `mergeable_state` is `behind`, and after a move of the base branch the base branch's rules want pull requests up to date while the head lacks its tip; left out of an observation when it is not | The draft was made on an older release than the latest |
| Observes on | `pull_request` synchronize, closed and reopened; a push to the base, for each open change; each sweep | A change of the draft or of the release; its worker, from time to time |
| A change closed unmerged | Not an observation: the host tells a person, who drops the item or asks for the work again | The draft was discarded: the same |
| One event at a time | An issue's commands and its NexKit pull request's rounds share one concurrency group keyed by the issue, which the workflow's `item` job derives with `nexkit item` and passes on (a person's pull request is its own item). A run that waits may be replaced by a newer one, so a run in the item's group takes every event of the item that is still at its source (`host.take`): comments, reviews and hold-label changes after the state comment's cursor, oldest first, each with its stable id from its source, such as the comment's id (see [Events and actions arrive more than once](engine.md#events-and-actions-arrive-more-than-once)). A command whose run was replaced is answered by the run that ran, and a run whose writes keep losing fails and leaves its event for the next run. `nexkit route`, the first job of every run in an item's group, takes them before the run's own work, and fails the run when every write loses; a work item with no state comment has nothing to take. The sweep carries out no item's step itself: it sends a `step` event into each item's group, and that run carries the step out. The sweep still writes NexKit's own labels, CI bug issues and done notices, and its conflict rounds on pull requests, from the `autopilot` group. An approval's run in `approved-N` writes nothing: it sends one `step` event for the item's issue, and the run that takes it in the item's group merges, so that run, not the approval's, writes the item's state. The run in `resume` that waits for paused commands writes nothing either: for each resume that is due it sends a `step` event into the item's group, and that run reads the paused run comment again, starts the command and marks the comment continued. Planned in [#332](https://github.com/phuongnse/nexkit/issues/332): every run that writes an item's state shares such a group, so that the run is the state comment's only writer, which stands for the compare-and-set. Today the `autopilot`, `resume` and `approved-N` groups stay apart from the items', and none of them takes an item's events (`nexkit/templates/workflow.yml`) | The request's row, locked while its event is handled |

What exists only because of GitHub stays in the GitHub host: the GitHub App, statuses, branch
clean-up, watching CI and flaky runs, Dependabot, artifacts, carrying a review over a base merge,
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
wait of random length, a few times; then the run fails, and the event stays at its source, past
the cursor, so that the next run takes it, never dropped.
