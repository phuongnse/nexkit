# The engine

_Planned for NexKit 7. Nothing on this page is built yet. NexKit 6 runs as
[How it works](how-it-works.md) says._

NexKit 7 splits NexKit into an **engine** that decides what happens to a piece of work, and
**hosts** that store the work, carry out what the engine decides and show it to people. GitHub
is the first host, and is NexKit as it runs today. An application platform that runs AI change
requests on its own database is the second: [Axis](https://github.com/phuongnse/axis) runs its
authoring requests on the engine. GitLab and Azure DevOps, and Codex and Gemini as agents, can
follow without a change to the engine.

## Why

The same loop runs wherever an agent changes something for a person: a plan the person can
read, the change, the checks, an independent review, fix rounds and an accept that a person or
a policy decides. NexKit runs that loop on GitHub issues and pull requests. An application
platform runs it on a request and a draft of an application's configuration, with no git, no
CI service and no GitHub. Writing the loop twice gives two loops that drift apart, so each
fix and each lesson has to be made twice.

The engine holds the loop once. A host decides only where the work lives and how each step is
carried out.

## The parts

| Part | What it is | Where it lives |
| --- | --- | --- |
| Engine | The loop's decisions, as a pure function | This repository, in Python |
| Protocol | The events a host sends, the actions the engine answers and the state document, as versioned JSON Schema | This repository |
| Host | Stores the work and its state, sends events, carries out actions, tells people | Its own code, in its own language |
| Runner | Runs one agent session for one stage | This repository for agent CLIs; a host may bring its own |
| Conformance suite | Events and the actions and state they must give | This repository |

A host never imports the engine's code. It runs the engine as a program, `nexkit-engine`, that
reads one JSON request on standard input and writes one JSON answer, or as a container that
answers the same requests over HTTP. A host in any language can therefore run it, and the
engine could be written again in another language without a change to any host, as long as
the conformance suite passes.

## The engine

The engine is a function: `(state, event) -> (state, actions)`.

- **Pure.** It reads no network, no files and no clock. The time is a field of each event.
  It holds no secret and never sees one.
- **One work item at a time.** Each call takes one work item's state and one event about it.
  A host sends a work item's events one at a time, in order, and stores the state each call
  returns before it sends the next.
- **The host owns the state.** The state document is versioned JSON, whose size the engine bounds
  so that it fits where each host keeps it. The host stores it where its work lives and passes it
  back with each event. The engine keeps nothing between calls, so a host restarts, scales and
  fails over without the engine.
- **Every decision of NexKit 6 that is not about GitHub moves into it:** plans, questions and
  answers, splits, approval, fix rounds and diagnosis rounds, the review's rules, budgets,
  retries after a transient stop, usage limits, holds, and when a person is needed.

A second function, `sweep(items, event) -> starts`, orders the work across many items: *blocked
by* links, waves, priority, scope and how many items run at once, as
[Autopilot](configuration.md#autopilot) says today. A host that runs each item as soon as it
is sent, as an application platform does, need not call it.

## States

A work item is always in one state, which the state document names. NexKit 6 has no field
for it and reads it from its comments; the engine makes it explicit.

| State | What happens | Next |
| --- | --- | --- |
| `planning` | An agent reads and writes a plan | `awaitingApproval`, `split`, `noChange` |
| `awaitingApproval` | The plan waits for its approval, or for answers to its questions | `working`, `planning` |
| `working` | An agent makes the change | `checking`, or `awaitingApproval` with questions |
| `checking` | The host's checks run on the change | `reviewing`, `fixing` |
| `reviewing` | A second agent session, with no memory of the first, reviews the change | `ready`, `fixing` |
| `fixing` | An agent works on the failures and findings | `checking` |
| `ready` | The change passed, or a person decides what is left | `accepting`, or `working` when a person asks for changes |
| `accepting` | The host accepts the change | `accepted`, `fixing` or `working` on a conflict, `ready` on a refusal |
| `accepted` | The change is in | none |
| `split` | The work is carried by its parts, and ends when they end | `accepted`, `dropped` |
| `noChange` | The plan found that nothing needs to change, and says why | none |
| `dropped` | A person ended the work | none |
| `failed` | A budget ran out, or a step failed in a way that waiting does not help | `dropped`, or `planning` when a person asks again |

Three things sit beside the state, never in place of it:

- **Attention:** a person is needed, with the reason. A question, a criterion the review could
  not verify, a checklist item only a person decides, a refused accept and a spent budget each
  set it.
- **Hold:** nothing new starts. A step that runs finishes and records what it held back.
- **Pause:** the next step waits until a time, after a transient stop or a usage limit.

## Events and actions

A host sends an event when something happens to a work item, and carries out the actions the
engine answers. The protocol defines each one's fields. These are the kinds.

| Events | Sent when |
| --- | --- |
| `created`, `textChanged` | A work item is made or its text is edited |
| `commented` | A person writes on it, with their role |
| `approved`, `answered`, `changesRequested`, `dropped` | A person decides |
| `stepFinished` | A step the engine asked for ended, with its outcome and tokens |
| `stepLost` | A step the host started never reported |
| `checksFinished` | The checks ended, with each result and its failure signature |
| `conflict` | The change no longer applies to what it was made on |
| `accepted`, `acceptRefused` | The host accepted the change, or could not |
| `held`, `released` | A hold was set or taken off |
| `blockerClosed`, `partEnded` | Work that this item waits for ended |
| `tick` | Time passed, for pauses and waits |

| Actions | The host |
| --- | --- |
| `runAgent` | Starts a runner for a stage, with the stage's access, the prompt's inputs, the result's schema and the budget left |
| `runChecks` | Runs its checks on the change |
| `publish` | Makes the agent's work the change: a commit and a pull request, or a draft |
| `replaceChange` | Drops the change and starts a new one from what is current, for a host whose conflicts are redone |
| `accept` | Accepts the change: a merge, or an accept of a draft |
| `createParts` | Makes the parts of a split, each blocked by the earlier parts it needs |
| `close` | Ends the work item with its outcome |
| `needPerson` | Tells the people who decide, once, with the reason |
| `report` | Shows a plan, a round, a review or a stop where people read the work |
| `wakeAt` | Sends a `tick` at a time |

## What a host decides

A host gives the engine its policies in its configuration. Each has a default.

| Policy | Values | GitHub's default | Axis |
| --- | --- | --- | --- |
| Plan approval | `person`, `autoWhenClean` | `person`; `autoWhenClean` with autopilot | `person`, always |
| Answers | `never`, `recommended` | `never`; `recommended` with autopilot | `never` |
| Accept | `person`, `autoWhenPassed` | `person`; `merge.auto` | `autoWhenPassed` only when the application and the request allow it |
| Fix rounds | 0–10 | 2 | 3 |
| Conflicts | `resolve`, `redo` | `resolve`: a round merges the base and keeps both sides | `redo`: a new draft from what is current |
| Budgets | Tokens, time and model calls per work item and per stage | Time per stage | Tokens, time and calls per request |
| Splits | On or off, with the most parts and the depth | On, 10 parts, depth 3 | On |
| Checklist | Items the plan and the review answer, some for a person | `review.checklist` | Axis's checklist |

**Someone else may do a stage.** A stage's result can come from outside the engine's runners:
a person's own pull request, which the engine only checks and reviews, or an outside app that
writes the plan and the change itself through the host. The engine takes such a result as it
takes a runner's, under the host's rules of trust, and never lets the one who made a change
review it.

## Runners

A runner runs one agent session for one stage and returns its outcome. The contract is
NexKit 6's `Runtime`: a `Spec` in, `Event`s while it runs, an `Outcome` at the end with the
result, the tokens, a usage limit and its reset, and whether the stop was transient. Each stage
has its access, which the runner enforces: none for triage, read for plan and review, write for
implement and fix.

There are two kinds:

- **An agent CLI in a workspace:** Claude Code today, and Codex and Gemini when their runtimes
  are added. The agent works on files, and the host publishes them. This repository holds them.
- **A model with tools:** the agent works only through the tools the host offers for the stage,
  such as an MCP server. A host that keeps its users' keys implements this runner itself, so
  that a key never leaves the host.

The engine never sees a secret. The host gives each runner only its own provider's secrets.

## Hosts

| | GitHub | Axis |
| --- | --- | --- |
| Work item | An issue | An authoring request |
| Change | A pull request on `nexkit/issue-<n>` | A draft |
| Where the state lives | A NexKit comment on the issue | The request's row in the tenant's database |
| Events from | GitHub's events, commands and the hourly schedule | Studio's routes, the MCP server and the agent host's turns |
| Runs a step in | An Actions job | The agent host |
| Checks | The repository's `checks` | Compile, package tests and scenarios |
| Accept | A squash merge | An accept through the publisher |
| People who decide | `notify` | The request's author |
| Tells people | A mention | A notification |

What exists only because of GitHub stays in the GitHub host: the GitHub App, statuses, branch
clean-up, CI and flaky-run watching, Dependabot, artifacts, carrying a review over a base
merge, and up-to-date rules.

## Conformance

`tests/conformance/` holds cases: a state, events in order, and the actions and state each must
give. Most come from `tests/test_scenarios.py`. The engine passes them in this repository, and
each host runs them through its own adapter, so a host knows it reads and writes the protocol
as the engine means it. A new rule of the engine comes with a case.

## Versions

The protocol has a version of its own. A host names the version it speaks, and the engine
answers in it. The engine moves an older state document to the current shape when it reads it,
so no host migrates states itself. A change that a host must follow is a new major version.

## Getting there

1. The engine grows inside NexKit 6: decisions move behind the protocol one by one, and the
   GitHub host calls them. Every existing test passes after each step, and
   `tests/test_scenarios.py` becomes the conformance suite as it goes.
2. NexKit 7 is released when the GitHub host runs only through the engine. Repositories on
   NexKit 6 move to it with the usual upgrade steps.
3. Axis's agent host runs its requests on the engine, with its adapter in C# and its own
   runner.
