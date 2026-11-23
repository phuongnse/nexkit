# The engine

_Planned for NexKit 7. Nothing on this page is built yet. NexKit 6 runs as
[How it works](how-it-works.md) says._

NexKit 7 splits NexKit into an **engine** that decides what happens to a piece of work, and
**hosts** that store the work, carry out what the engine decides and show it to people. GitHub
is the first host, and is NexKit as it runs today. An application can be a host too: one where a
person asks an agent to change something the application holds, such as its own configuration,
and the work lives in the application's database. GitLab and Azure DevOps, and Codex and Gemini
as agents, can follow without a change to the engine.

## Why

The same loop runs wherever an agent changes something for a person: a plan the person can
read, the change, the checks, an independent review, fix rounds and an accept that a person or
a policy decides. NexKit runs that loop on GitHub issues and pull requests. An application runs
it on a request and a draft of what the application holds, with no git, no CI service and no
GitHub. Writing the loop twice gives two loops that drift apart, so each
fix and each lesson has to be made twice.

The engine holds the loop once. A host decides only where the work lives and how each step is
carried out.

## Words

- **Work item:** one piece of work, such as an issue or a request in an application.
- **Change:** what the work item changes, such as a pull request or a draft. It has an id and a
  version, such as a commit or a draft's revision, which moves each time the change does.
- **Stage:** what an agent session is for: triage, plan, implement, fix or review.
- **Action:** one thing the engine asks the host to do, such as running an agent or the checks.
- **Round:** an implement or fix stage with the checks and the review that follow it.

## The parts

| Part | What it is | Where it lives |
| --- | --- | --- |
| Engine | The loop's decisions, as pure functions | This repository, in Python |
| Protocol | The events, actions, results and state document, as versioned JSON Schema | This repository, in `nexkit/protocol/` |
| Host | Stores the work and its state, sends events, carries out actions, tells people | Its own code, in its own language |
| Runner | Runs one agent session for one stage | This repository for agent CLIs; a host may bring its own |
| Conformance suite | Events and the actions and state they must give | This repository |

A host never imports the engine's code. It runs the engine as a program, `nexkit-engine`, that
reads one JSON request on standard input and writes one JSON answer, or as a container that
answers the same requests over HTTP. A host in any language can therefore run it, and the
engine could be written again in another language without a change to any host, as long as
the conformance suite passes.

## The engine

The engine answers three calls, each a pure function:

- **`step(state, event) -> (state, actions)`** decides what happens to one work item.
- **`prompt(state, stage, inputs) -> text`** writes the prompt of a stage from the texts the host
  fetched for it, so the prompts and their rules stay in the engine while the host does the reading.
- **`sweep(items) -> starts`** chooses which waiting work items start, and in what order.

All three read no network, no files and no clock: the time is a field of each event. They hold
no secret and never see one. The policies of the host come with every call, as the host's
configuration reads at that moment, and the state keeps the ones that a running round started
with.

Every decision of NexKit 6 that is not about GitHub moves into the engine: plans, questions and
answers, splits, approval, fix and diagnosis rounds and how they are counted, the review's
rules, budgets, retries after a transient stop, usage limits, holds, waits, and when a person is
needed.

### The state document

- **The host owns it.** The state document is versioned JSON that the host stores where its work
  lives and passes back with each call. The engine keeps nothing between calls, so a host
  restarts, scales and fails over without the engine.
- **It has a revision.** Each answer raises the revision by one, and the host stores it only if
  the revision it read is still the stored one, as a compare-and-set. A host whose write loses
  reads the state again and sends the event again.
- **It holds decisions, not prose.** The state keeps what the engine decides from: the state
  name, the change, questions, criteria and checklist outcomes, findings in short, failure
  signatures, counts and budgets used. Long texts, such as a plan's prose, check output, a
  discussion and evidence, stay with the host as texts that the state names, and reach the
  engine only in `prompt`. The protocol sets the largest state document, small enough for one
  GitHub comment.
- **It is the truth.** A host may keep columns or markers that copy parts of it, for its own
  lists and pages, written in the same transaction, but the engine reads only the document.

### Events and actions arrive more than once

The protocol is built for delivery at least once, in any order a host's queues give it.

- **Every action has an id**, and the result event of an action carries that id and the version
  of the change it applies to. The state records the actions it waits for, and the engine
  ignores a result whose action it no longer waits for, or whose change version is old. A step
  that runs twice, a late result after a cancel, and a run started again after it was lost are
  then each harmless.
- **The state is an outbox.** Actions the engine answered stay in the state as pending until
  their result arrives. A host that crashed between storing the state and carrying out its
  actions carries out the pending ones when it starts again.
- **Every action can be repeated.** A host carries out an action with the same id once: parts
  already filed are not filed again, and a person already told is not told again.

## States

A work item is always in one state. NexKit 6 has no field for it and reads it from its comments;
the engine makes it explicit. Beside the state, the item has a **change** or none, so that a
re-plan, a question or a failure keeps the pull request or draft that exists.

| State | What happens | Next |
| --- | --- | --- |
| `new` | The item waits to be started | `planning`, `working`, `checking` |
| `planning` | An agent writes a plan | `awaitingApproval`, `working` or `fixing`, `split`, `noChange` |
| `awaitingApproval` | The plan, or a split, waits for its approval or for answers | `planning`, `working` or `fixing`, `split`, `dropped` |
| `working` | An agent makes the change | `checking`, `awaitingApproval`, `planning`, `noChange`, `failed` |
| `checking` | The host's checks run on the change | `reviewing`, `fixing` |
| `reviewing` | A second agent session, with no memory of the first, reviews the change | `ready`, `fixing` |
| `fixing` | An agent works on the failures and findings, in the same change | `checking`, `awaitingApproval`, `planning`, `failed` |
| `ready` | The change passed, or a person decides what is left | `accepting`, `fixing`, `checking` |
| `accepting` | The host accepts the change | `accepted`, `ready`, `fixing`, `working`, `failed` |
| `accepted` | The change is in | none |
| `split` | The work is carried by its parts | `accepted`, `dropped` |
| `noChange` | Nothing needs to change, and the item says why | none |
| `dropped` | A person ended the work | none |
| `failed` | A step failed in a way that waiting does not help, or a token, time or money budget ran out. The change is kept | `dropped`, and by the host's policy `planning` or `fixing` |

- **Approval leads to the change that exists.** An approved plan goes to `working` when the item
  has no change, and to `fixing` when it has one, such as after a re-plan or a question in a fix
  round.
- **From every state that has a change:** a `conflict` follows the conflict policy; a change that a
  person accepted, such as a merge by hand, goes to `accepted`; a change that became empty, because
  the same change landed another way, goes to `noChange`; a change that a person pushed to goes to
  `checking`.
- **From every state that is not final:** `dropRequested` goes to `dropped`, and the engine cancels
  the step that runs. A host's `stopped` event, such as a key that its provider refused or a
  person who was disabled, goes to `failed`.
- **A review-only item** starts in `checking` and is never fixed or accepted by the engine: a
  person's own pull request, which NexKit only checks and reviews. A new review request takes it
  back to `checking`.
- **A final item that a person reopens** waits for a person, with attention, as NexKit 6 does.

Three things sit beside the state, never in place of it:

- **Attention:** a person is needed, with the reason: a question with no recommended answer, a
  criterion the review could not verify, a checklist item only a person decides, a refused accept,
  fix rounds used up, a split that is not complete.
- **Hold:** nothing new starts. A step that runs finishes and records what it held back.
- **Wait:** the next step waits for something named: a time, such as after a transient stop or a
  usage limit; an item that blocks it; a failure the base branch has; or required checks before a
  merge. Each wait has an id, and the event that ends it names it.

## Events

| Event | Sent when |
| --- | --- |
| `created` | A work item is made, with its kind, its text, who does each stage and, for an item made with a plan, the plan |
| `textChanged`, `commented` | A work item's text is edited, or a person writes on it, with their role |
| `requested` | A person or the sweep asks for something: `plan`, `planFromScratch`, `go`, `fix`, `review`, `retry`, `trust` or `start`, with a note |
| `planApproved`, `answered`, `changesRequested`, `changeApproved`, `dropRequested` | A person decides: the plan, its questions, the change, a person-only checklist item or criterion on the change's version, or the end |
| `actionFinished` | An action ended, with its id, the change version and its result |
| `actionLost` | An action the host started never reported, with any tokens it is known to have used |
| `conflict`, `changeAccepted`, `changeEmptied`, `changePushed` | Something happened to the change outside the engine |
| `stopped` | The host stops the item, with the outcome |
| `held`, `released` | A hold was set or taken off |
| `blockerClosed`, `partEnded` | Work that this item waits for ended |
| `reopened` | A final item was opened again |
| `tick` | The time of a wait came, naming the wait |

## Actions

| Action | The host | Its result |
| --- | --- | --- |
| `runAgent` | Runs a runner for a stage, see [runners](#runners) | The stage's result, tokens, a usage limit, a transient stop |
| `publish` | Makes the agent's work the change: a commit and a pull request, or a draft | The change's id and version, or why it could not |
| `replaceChange` | Drops the change and starts a new one from what is current | The new change |
| `runChecks` | Runs its checks on a version of the change | Each result, its failure signature, and whether the failure matches a known failure of the base |
| `accept` | Accepts the change: a merge, or an accept of a draft | `accepted`, or refused with a kind: `transient`, `waitingForChecks`, `checkFailed`, `behind`, `headMoved`, `conflict`, `notAccepted` or `needsPerson` |
| `createParts` | Makes the parts of a split, each blocked by the earlier parts it needs | The parts' ids |
| `close` | Ends the work item with its outcome | Done |
| `cancel` | Stops an action that runs | Done |
| `needPerson` | Tells the people who decide, once, with the reason | Done |
| `show` | Shows a plan, a round, a review or a stop where people read the work | Done |
| `wait` | Sends a `tick` naming the wait at its time | Done |

**A chain of actions.** An action may name the action that follows when it ends as expected,
such as implement, publish, checks and review. A host that runs a round as one pipeline, as
GitHub runs one workflow, carries out the chain and then sends each result in order. The engine
answers each as if it had come alone, and ignores a following action it no longer wants, by its
id. NexKit 6's round, from `route` to `report`, stays one workflow run.

## Runners

A runner runs one agent session for one stage. Its contract is part of the protocol: `runAgent`
gives it the stage, the stage's access, the prompt, the schema of the stage's result, the agent,
the model and effort, the budget left in tokens, time and money, and a session id to ask the same
session again. Its result gives the stage's result, the tokens as input, cached and output, a
usage limit with its reset, whether the stop was transient, whether no result fit the schema, and
the version of the change it produced.

Each stage has its access, which the runner enforces: none for triage, read for plan and review,
write for implement and fix. There are two kinds of runner:

- **An agent CLI in a workspace:** Claude Code today, and Codex and Gemini when their runtimes
  are added. The agent works on files, and the host publishes them. NexKit 6's `Runtime` and
  `Provider` stay the Python interface inside this runner, which this repository holds.
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

A host gives the engine its policies in its configuration. Each has a default.

| Policy | Values | GitHub | An application, for example |
| --- | --- | --- | --- |
| Start | `onRequest`, `bySweep` | `onRequest`; `bySweep` with autopilot | `onRequest`: a request starts when it is sent |
| Plan approval | `person`, `autoWhenClean` | `person`; `autoWhenClean` with autopilot | `person`, always |
| Answers | `never`, `recommended` | `never` (`autopilot.answer`) | `never` |
| Review after failing checks | `always`, `onlyAfterPassing` | `always`: one fix round takes the failures and the findings | `onlyAfterPassing` |
| Accept | `person`, `autoWhenPassed` | `person`; `autoWhenPassed` with `merge.auto` | `autoWhenPassed` only when the application and the request allow it, else the person who sent it |
| Fix rounds | 0–10, and which rounds count | 2: rounds a person asked for, resumed, retried and stopped rounds do not count | 3; a request for changes and a retry start the count again |
| When fix rounds run out | `person`, `failed` | `person` | `person`: the draft is ready with its open findings |
| Conflicts | `resolve`, `redo`, by who accepted | `resolve` on a person's request; by itself with `conflicts.auto_resolve`, at most `conflicts.max_rounds` | The agent's accept: `redo` on a new draft. A person's accept: the person asks to make it again |
| Budgets | Tokens, time, money and model calls, per item and per stage | Time and money per stage | Tokens and time per request, model calls per stage |
| Retries | Waits after a transient stop; when a lost action starts again | `retry.wait_minutes`, `autopilot.lost_after_minutes` | The lease of a claimed request |
| Usage limits | Resume after the reset, and how long to wait | `usage_limit.*` | Resume after the reset |
| Agents | Agents, models and efforts per stage, fallbacks, profiles and triage | `agents`, `stages`, `profiles` | The model the request names |
| Splits | Off or on, with the most parts and the depth | On with autopilot, at most 10 parts, 3 levels | On: the person approves the split |
| A parent's end | When its parts' ends end it | `close_parent_issues`, when one part was completed | When its parts end |
| After a failure | `person`, `replan`, `newItem` | `person`, then `replan` on a comment | `newItem`: the person sends the request again |
| Checklist | Items the plan and the review answer, some for a person | `review.checklist` | The application's own checklist |

**Someone else may do a stage.** `created` says who does each stage: the engine's runners, a
person, or an outside app that writes the plan and the change through the host. An item made
with its plan starts in `awaitingApproval`. A stage done outside ends with `actionFinished` for
the action the engine waits for, as a runner's would, under the host's rules of trust. The one
who made a change never reviews it. A review that could not run, because no reviewer was
available, leaves the accept to a person.

## The sweep

Waits, holds and *blocked by* links live in each item's state, so `step` decides whether an item
may start. `sweep` only chooses among the items that may: it orders them by priority, wave,
milestone and age, applies how many items run at once and which accounts are at a usage limit,
and answers the items to start. The host sends each a `requested` event of the kind `start`.
A host that starts each item when it is made, as an application may, needs no sweep, but still sends
`blockerClosed` when a part's earlier part ends.

## Hosts

| | GitHub | An application, for example |
| --- | --- | --- |
| Work item | An issue, or a person's pull request for a review-only item | A request that a person sends |
| Change | A pull request on `nexkit/issue-<n>`, its version a commit | A draft, its version its newest revision |
| Where the state lives | NexKit's comments, as today's markers do | The request's row in the application's database |
| Events from | GitHub's events, commands and the hourly schedule | The application's routes, its tools for agents and its own workers |
| Runs actions in | Workflow runs, a round as one run | The application's worker |
| Checks | The repository's `checks` | The application's own validation and tests |
| Accept | A squash merge | The application's own accept of the draft |
| People who decide | `notify` | The person who sent the request |
| Tells people | A mention | A notification |

What exists only because of GitHub stays in the GitHub host: the GitHub App, statuses, branch
clean-up, watching CI and flaky runs, Dependabot, artifacts, carrying a review over a base merge,
and up-to-date rules. Each reaches the engine as an event or a result: a failure the base branch
has is a `runChecks` result, and a branch behind its base is an `accept` refused as `behind`.

## Conformance

`tests/conformance/` holds cases: a state, events in order, and the actions and state each must
give. They come from the decisions that `tests/test_scenarios.py` plays today. Its scenarios that
are about GitHub alone, such as vouching, Dependabot, shards, branch updates and carried reviews,
stay as tests of the GitHub host.

After every call, the suite also checks NexKit's rule: every item that is not final has an action
that runs, a wait, a hold or attention, and a round counts only when its agent ran to its end or
published. The engine passes the suite in this repository, and each host runs it through its own
adapter. A new rule of the engine comes with a case.

## Versions

- **Two versions.** The protocol has a version, and the state document has a schema version of its
  own. The engine moves an older state document to the current schema when it reads one, and
  refuses one that is newer than it knows, so an engine rolled back never guesses.
- **A new action kind is a new major version** of the protocol, because a host cannot carry out an
  action it does not know. A host refuses an answer with an action it does not know, and the
  engine's error names it.
- **A host's first state document is the host's to build.** The GitHub host builds each open
  item's document from NexKit 6's markers, open pull requests, paused runs and waits included, and
  an application that had a loop of its own builds each request's from its records. A conformance
  case checks each import.

## Getting there

1. The engine grows inside NexKit 6: decisions move behind the protocol one by one, and the
   GitHub host calls them. Every existing test passes after each step, and the engine's part of
   `tests/test_scenarios.py` becomes the conformance suite as it goes.
2. NexKit 7 is released when the GitHub host runs only through the engine. Repositories on
   NexKit 6 move to it with the usual upgrade steps, and the GitHub host imports their open work.
3. An application can then be a host, with its adapter in its own language and, when it keeps
   its users' keys, its own runner.
