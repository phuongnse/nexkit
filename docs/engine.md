# The engine

_Planned for NexKit 7. Built so far: the protocol's schemas, in `nexkit/protocol/`; the engine's
`step` call with its states, whose decisions are a frame that takes NexKit 6's own rules one group
at a time, and its `prompt` call, which writes each stage's prompt as NexKit 6 does, as
`nexkit-engine step` and `nexkit-engine prompt` and over HTTP as `nexkit-engine serve`; its
`sweep` call, which orders and limits the items that start, as `nexkit-engine sweep`, which
NexKit 6's autopilot takes its starts from; the
GitHub host's state comment, its import of NexKit 6's open work and its taking of an item's events
from their sources (`host.take`), which nothing calls yet; and
the decisions that follow a plan, a blocked `/nexkit go` and a round on a pull request, its
review, diagnosis and conflict rounds, the end of a round's wait and which rounds count, and the
start of a new issue's first plan, which
NexKit 6's autopilot, `route`, `report` and `maintain` take from `step` (see
[getting there](#getting-there)). The rest of this page is not built yet, and NexKit 6 runs as
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

The reasons for this split are in [D10](decisions/d10-an-engine-decides-hosts-carry-out.md).

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

A host never imports the engine's code. It runs the engine as a program, `nexkit-engine <call>`,
that reads one JSON request on standard input and writes one JSON answer, or runs
`nexkit-engine serve` beside it, which answers the same requests over HTTP. A host in any
language can therefore run it, and the engine could be written again in another language
without a change to any host, as long as the conformance suite passes.

- **Over HTTP**, `POST /<call>` takes the request as its body and answers with the same JSON
  text as standard input: status `200`, or `400` when the engine refuses the request. Both read
  the request as UTF-8 and refuse one that nests deeper than 64 arrays and objects. An error
  answer names at most 20 ways in which a request breaks the protocol. `serve` answers each
  request on its own and keeps nothing between them. On `SIGTERM` it exits at once: a request
  in flight gets no answer, and its host sends it again, which the engine allows since it keeps
  no state and answers a request the same each time. It refuses a body over the limit of its
  call, `MAX_BODY` or for `prompt` the larger `MAX_BODIES`, without one `Content-Length` (zeros
  before its number do not count) or that stops coming, a method other than `POST`, a call it
  does not know and a request that HTTP does not allow. A host whose `prompt` request is over its
  limit sends the same request to `nexkit-engine prompt` on standard input, which has no limit
  and answers it the same. Such a request holds only the input kinds `runAgent` names, with
  diffs and outputs cut, so only whole texts and many comments make it large. Each refusal is an
  error answer (`calls/error.schema.json`) with its own status. It listens on `127.0.0.1:8080`
  unless `--host` and `--port` say otherwise.
- **Only for its host.** `serve` asks no one who they are, and does not limit how many clients
  connect or how slowly they send. Put it where only its host reaches it, such as the host's
  own machine or a private network beside it.
- **The image.** Each release publishes `ghcr.io/phuongnse/nexkit-engine:<version>`, built from
  the release's commit on `main`. It runs `serve` on port 8080 of the container. Its base is a
  Python image pinned by digest; every week a pull request proposes the digest its tag names
  now, when that moved, so the image takes the base's security fixes. A release never
  replaces an image: one whose version is already in the registry fails before it pushes.
  Each image carries an attestation of its build provenance, so a host can check the commit
  and workflow that built it with
  `gh attestation verify oci://ghcr.io/phuongnse/nexkit-engine:<version> --repo phuongnse/nexkit`.

## The engine

The engine answers three calls, each a pure function, and `describe`, which says which version
of the protocol it speaks (see [versions](#versions)):

- **`step(state, event) -> (state, actions)`** decides what happens to one work item.
- **`prompt(state, stage, inputs, policies, texts) -> (text, shortened, notRead, files)`** writes
  the prompt of a stage from the texts the host fetched for it, so the prompts and their rules
  stay in the engine while the host does the reading.
- **`sweep(items, running, limited, policies) -> (starts, waits)`** chooses which waiting work
  items start, and in what order.

All three read no network, no clock and no files but the engine's own prompt templates: the time is
a field of each event. They hold no secret and never see one. The policies of the host come with
every call, as the host's configuration reads at that moment, and the state keeps the ones that a
running round started with, but `lostWaits`, which applies as the host gives it at each loss.

### The prompt

`runAgent` names the inputs its stage reads, by kind, and the host fetches each before it calls
`prompt`:

| Input | What the host fetched |
| --- | --- |
| `item` | The work item's title and text, its change, whether it is a person's change reviewed as its own item, and what the change did outside the plan |
| `discussion` | The comments on the item and on its change, oldest first, each a person's comment, a note on a command or the answers a policy gave, and what people said on the change's version: their reviews and their comments on a line. The host leaves out what nobody it trusts wrote |
| `notes` | The note of the request that started the stage |
| `plan`, `previousPlan` | The approved plan; for a new plan, the latest one, where it falls in the discussion and the profile it used |
| `checks` | The checks' results, with the end of each output, the required checks that refused an accept, and the checks a diagnosis round looks into, each with the failure it repeated |
| `linked` | The texts the item links to, the items it was split from with their discussions, and each link not read, with why in the host's words. The host bounds how many it lists |
| `evidence` | What the CI runs the item names left, and the result files of failing checks, with what the host keeps of each |
| `parts` | The earlier parts of the same split that a part relies on |
| `diff` | The change against its base, since the last review, the merge of the base that the newest fix record made, and the files a merge conflicted in |
| `workspace` | The checks the agent can run, and the paths only a person changes |

- **What a stage needs.** Every stage needs `item`, and every stage but triage `workspace`.
  Implement, fix and review need `plan`, which is null when the item has none, and fix and review
  need `diff`. The engine refuses a call without them.
- **Earlier rounds come from the state.** The fix and review prompts read the fix records and the
  last review from the request's `state`, and a request without one shows none. The newest fix
  record is the round a review follows, and `diff.merged` holds the merge of the base that it made.
  The fix prompt shows the last review's blocking findings only when that review read the change's
  current version, `change.version`. A host that does not keep the state builds one for each call:
  the GitHub host builds it from NexKit 6's state comment.
- **Limits.** The engine applies them: how much of each text and block a prompt keeps, how many
  comments, and in what order. A text that a prompt keeps only the start of, or leaves out, goes
  whole to a file in the folder that `texts` names, an absolute path outside the working tree,
  POSIX or Windows, where the agent reads it. The engine names each file from the host's id,
  and adds a short hash of the id when the id holds a character a file name cannot, so two ids
  never share a file. So the texts come whole, but a diff and a check's output come cut to what
  a prompt shows of them, and the schema refuses more.
- **The answer** holds the prompt, the texts it shortened or left out, the links it lists as not
  read, and the files with their texts, which the host writes before the agent runs. A host
  that writes them first checks that each path is inside its `texts` folder, and writes none
  when one is not.
- **Policies.** Beside the host's policies, the call reads three that the protocol does not hold
  yet, as its schema names them: `checklist`, the items the plan and the review answer;
  `profiles`, the profiles triage chooses from, each with its `when` as a string; and `budgets`,
  of which the plan prompt names the implement stage's time. A plan needs
  `budgets.implement.seconds` as an integer, and the engine refuses one without it.
- **A result that did not fit.** With `unfit`, why a session's result did not fit its schema, the
  call writes the prompt that asks the same session for its result alone.

Every decision of NexKit 6 that is not about GitHub moves into the engine: plans, questions and
answers, splits, approval, fix and diagnosis rounds and how they are counted, the review's
rules, budgets, retries after a transient stop, usage limits, holds, waits, and when a person is
needed.

### The state document

- **The host owns it.** The state document is versioned JSON that the host stores where its work
  lives and passes back with each call. The engine keeps nothing between calls, so a host
  restarts, scales and fails over without the engine.
- **It has a revision.** Each answer that changes the state raises the revision by one, and the
  host stores it only if the revision it read is still the stored one, as a compare-and-set. A
  host whose write loses reads the state again and sends the event again. A host whose every try
  loses leaves the event at its source, past the host's cursor, so that the next run takes it
  (see [Events and actions arrive more than once](#events-and-actions-arrive-more-than-once)). An
  answer that changes nothing, such as to a duplicate result, says so and gives the state back as it
  came, with no actions, so the host writes nothing.
- **It holds decisions and the history that the prompts show, not other prose.** The state keeps
  what the engine decides from: the state name, the change, questions, criteria and checklist
  outcomes, findings in short, failure signatures, counts, and the tokens, time and money its
  agents spent. It also keeps the history that the next fix and review prompts show: the latest
  fix rounds' records, each with its number, who started it, whether it diagnosed, its note, the
  agent's summary and cause, and the checks that still failed after it with their failure
  signatures, and the last review's round, version, verdict and findings with file, line and
  body. The engine records a fix round when its agent returns a result that is not blocked, and
  the checks after it complete the record. A round under way keeps who started it, whether it
  diagnoses and its note until then (`rounds.fixRound`). Other long texts, such as a plan's prose,
  check output, a discussion and evidence, stay with the host as texts that the state names, and
  reach the engine only in `prompt`.
- **It has a size.** The protocol sets the largest state document: 48,000 bytes as JSON in UTF-8
  with its keys sorted and no spaces, so that its base64 fits one GitHub comment of 65,536
  characters. What results and events bring is bounded as it comes in. Each list keeps at most as
  many items as `maxItems` in its schema says: 10 questions of 6 options each, 20 protected paths,
  10 split parts, each needing only earlier parts, 30 findings with the blocking ones first, 30
  failure signatures, 300 failing checks, each kept as a 12-digit digest of its name and signature,
  150 checks that repeated, and 50 criteria and 50 checklist answers with those a decision reads
  first. A split of more parts keeps none of them, only how many it named, so a split is never filed
  cut. Each text is clipped as NexKit 6 clips it: a question at 1,500 characters, an option at 500,
  the item's title and a part's at 256, a path at 300, a finding at 300, a reason for a person at
  1,000 and a check's name at 40, as long as NexKit's own may be. The history is clipped as the
  prompts clip it: a note at 2,000 characters, a summary at 4,000, a cause at 2,000, a finding's
  body at 1,500 and the failure signature of a fix record at 200. The state keeps the latest 5 fix
  records, each with at most 30 failing checks, and the last review's 30 findings, in the
  reviewer's order, or the blocking ones first when there are more, with how many were left out.
  A long failure signature is kept as its hash, never cut, because rounds compare signatures.
  When a document would still pass the limit, the engine shortens texts that a person reads in full
  where the host keeps them, each to the most that fits, in this order: the findings, the questions
  and their options, the protected paths, the attention's text, and the names of repeated checks
  past the ten that a diagnosis round looks into, which no name of NexKit's own ever needs. Only
  then does it shorten the history: the last review's suggestions and then its blocking findings,
  the fix records' notes, summaries and causes and the note of the round under way, the check
  names, failure signatures and conflicting files of the fix records, and the files of the last
  review's findings. If the document still does not fit, the oldest fix records leave out the
  checks that still failed after them, which a prompt then shows as unknown. It never shortens a
  count, a status, an id, a title or a signature, which decisions read, and a host that builds a
  first document keeps it the same way. The engine never answers a state that its own schema refuses, such as an outbox past its
  `maxItems` when a host never reports its actions: it refuses that event instead.
- **It is the truth.** A host may keep columns or markers that copy parts of it, for its own
  lists and pages, written in the same transaction, but the engine reads only the document.

### Events and actions arrive more than once

The protocol is built for delivery at least once, in any order a host's queues give it.

- **Every event has an id**, the same each time the host sends that event again and different
  for every other event. One webhook delivery may give several events for one item, so an id
  names the event, such as `<delivery>:<kind>`. The state keeps the ids of the 20 latest events
  that changed it, and the engine takes an event whose id it holds as a duplicate that changes
  nothing.
  The GitHub host names each event by its source on GitHub (`host.event_id`): `comment-<id>`
  for a comment, whether it is a remark, an answer or a command, `review-<id>`,
  `issue-event-<id>` from the issue events API, `dispatch-<run id>`, and `run-<run id>` for an
  agent's result, so the report job and the sweep give one result the same id. An id is built
  only from GitHub's number, never from a text, and its kind keeps a comment and a review with
  one number apart.
  The state comment also keeps the host's own cursor, a hidden line before the state marker with the
  time and the event id of the newest source that the host took. The host answers only the sources
  after it, since `recentEvents` keeps the ids of events that changed the state, and run results
  would push a comment's id out of it. The cursor moves past a source even when its answer changed
  nothing, in a write of its own on the same revision, so that an old remark is never answered later
  on a state that moved on. Another writer's write keeps it.
- **A person's decision names what it decides on.** `planApproved`, and a `go`, name the plan by
  its text's ref; `answered` names the questions by the ref that asked them, as the state's
  attention has it: a plan's text ref, or a round's agent action; `changeApproved` names the change
  and its version. A decision on a plan, questions or a version that is no longer the item's changes
  nothing. The host names what the person acted on: the plan or questions they replied to, or the
  latest ones shown to them before they acted, and the version they saw; never what the state
  holds when the host handles the event, which would make a late decision look current.
- **The host observes the change, and the engine reconciles.** A host does not report what
  happened to a change but what the change is: `changeObserved` gives its version, whether it
  conflicts with what it was made on, and whether it was merged or became empty, as the host
  reads them while it handles the event. A host sends one whenever something may have changed,
  such as a push, a move of the base, a merge or a sweep, and may send one at any time. The state
  keeps the latest observation, and after each event the engine reconciles the item with it,
  even after an event that changes nothing else, so the next observation, or a repeated one,
  mends one that was lost. A failed item waits for a person, and a split parent waits for its
  parts, whatever is observed, but for a merge or an emptied change.
- **A host's duties make the latest observation the truth.**
  - It handles one item's events one at a time, in the order it handles them, and stores each
    answer with the revision's compare-and-set. When its write loses, it reads the state again and
    handles the event again, reading the change again for an observation rather than sending the
    old one. When every try loses, the run fails and the event stays at its source, past the
    cursor, for the next run to take once.
  - It reads an observation while it handles the event, never from a payload that waited in a
    queue, and from a read that sees every write the host made itself, such as the branch's ref
    rather than a cached pull request. A stale read would take the change back to a version it
    left.
  - It sends an observation only when it knows each field. While a forge still computes whether
    the change conflicts, the host waits and reads again, for a bounded time such as a few
    minutes, since the item's other events wait meanwhile; then it sends no observation, or a
    result with `observed` null, and observes again later. A merged change needs no reading of
    conflicts: the host sends `conflicts` false.
  - It observes every open change from time to time, such as on each sweep, since only a next
    observation mends one that was lost. After a reopen it observes the change, since a final
    item takes no observation.
  - Its own actions change the change outside that order. A publish or a replace is the one the
    engine waits for, so the host reads the change when it handles that action's result and
    carries the observation in the result. A result with a change and `observed` null leaves the
    item with no observation, so nothing reconciles until the next one; a change that the host
    finds closed unmerged is sent so, and the host tells a person, as for a change closed
    unmerged below; the engine's work on the change goes on until that person decides. The
    engine refuses a result whose observation names another change; the host then reports the
    action lost, which fails the item for a person. An accept may be observed as merged before
    its result: the engine then cancels it, and a host treats the cancel of an action that
    already finished as done.
- **Every action has an id**, and the result event of an action carries that id and the action's
  own change version: the one it acts on, or null for an action that has none, such as `publish`
  or an agent started with no change. The version an agent produced is in its result. The state
  records the actions it waits for, and the engine ignores a result whose action it no longer
  waits for, such as one it cancelled when the change moved on. A step that runs twice, a late
  result after a cancel, and a run started again after it was lost are then each harmless.
  A result for an action that is still pending names the version that action acts on; one that
  names another is an error the engine refuses.
- **Leaving a state cancels its work.** An item that moves on cancels the actions of the state it
  left that still run, such as its checks when a person pushes to the change, so their late
  results find nothing pending. One it asked for earlier in the same step is withdrawn from the
  answer instead, since the host never got it. A lost action that only tells, shows, waits,
  ends the item or withdraws its change is carried out again under a new id while it still stands: a lost notice is not sent again
  once attention has another reason or is about something else. A lost agent run or lost checks
  that the engine started by itself, in the state that runs them, start again as the same round,
  which counts nothing again, after each wait of `lostWaits` in turn: a wait of 0 in the same
  step, another at its tick. After the last wait the item goes to `failed` for a person
  (`outage`), and its text says how often the work was lost. A lost run whose event reports
  tokens spent had its agent run, so it starts only once more. The count goes in `again` and
  starts afresh when a request is taken or work other than a `wait` finishes. Work that a person
  started, and the rest of that round, which carries `by: "person"` on its actions, waits for a
  person at once when lost, and so does a run of a person's that waits after a usage limit or a
  transient stop. A lost publish, replace, accept or parts creation fails the step, for a
  person, and so does every lost action that works under the default `lostWaits`, which is
  empty.
- **The state is an outbox.** Each action the engine answered stays in the state, whole, as
  pending until its result arrives, the engine cancels it, or the host reports it lost. A host
  that crashed between storing the state and carrying out its actions carries out the pending
  ones when it starts again, from the state alone. A late result of a cancelled action changes
  nothing, so the result of the `cancel` says what the cancelled action spent. A notice to a
  person stays pending only while the item's attention says it: when attention changes, the
  engine cancels a notice it answered earlier, or withdraws one of the same answer, so every
  pending action is still one to carry out.
- **Every action can be repeated.** A host carries out an action with the same id once: parts
  already filed are not filed again, and a person already told is not told again.

## States

A work item is always in one state. NexKit 6 has no field for it and reads it from its comments;
the engine makes it explicit. Beside the state, the item has a **change** or none, so that a
re-plan, a question or a failure keeps the pull request or draft that exists.

| State | What happens | Next |
| --- | --- | --- |
| `new` | The item waits to be started | `planning`, `working`, `checking`, and `fixing` for an item reopened with its change |
| `planning` | An agent writes a plan | `awaitingApproval`, `working` or `fixing`, `split`, `noChange` |
| `awaitingApproval` | The plan, or a split, waits for its approval or for answers, or a round's questions wait for answers | `planning`, `working` or `fixing`, `split`, `noChange`, `dropped` |
| `working` | An agent makes the change | `checking`, `awaitingApproval`, `planning`, `noChange`, `failed` |
| `checking` | The host's checks run on the change | `reviewing`, `fixing`, `ready` |
| `reviewing` | A second agent session, with no memory of the first, reviews the change | `ready`, `accepting`, `fixing` |
| `fixing` | An agent works on the failures and findings, in the same change, or its round waits to start | `checking`, `awaitingApproval`, `planning`, `working`, `ready`, `failed` |
| `ready` | The change passed, or a person decides what is left | `accepting`, `fixing`, `checking` |
| `accepting` | The host accepts the change | `accepted`, `ready`, `checking`, `fixing`, `working`, `failed` |
| `accepted` | The change is in | `new`, when a person reopens it |
| `split` | The work is carried by its parts | `accepted`, `dropped` |
| `noChange` | Nothing needs to change, and the item says why | `new`, when a person reopens it |
| `dropped` | A person ended the work | `new`, when a person reopens it |
| `failed` | A step failed in a way that waiting does not help, or a token, time or money budget ran out. The change is kept, and the item waits for a person whatever is observed of it, but a merge or an emptied change | `dropped`, and by the host's policy `planning` or `fixing` |

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
  it, plans such an item again when it has no change. Under `autoWhenClean`, with `answers`
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
- **An agent that stops on a transient error**, without a usage limit, runs the same stage again
  after each wait of `retryWaits` in turn, as the same round, which counts nothing again and
  diagnoses again if it did. A wait of 0 runs it again in the same step. After the last wait,
  the next transient stop goes to `failed` for a person. The count of runs again goes on through
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
  (`blocked`), a step that failed (`failed`), a final item opened again (`reopened`), or an
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
| `requested` | A person or the sweep asks for something: `plan`, `planFromScratch`, `go`, `fix`, `review`, `retry`, `trust` or `start`, with a note, and the item's title and depth again when the host gives them |
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
| `runAgent` | Runs a runner for a stage, see [runners](#runners); for a fix round, the checks it diagnoses first | The stage's result, tokens, a usage limit with its reset, a transient stop |
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
| `show` | Shows a plan, a round, a review or a stop where people read the work | Done |
| `wait` | Sends a `tick` naming the wait at its time | Done |

**A chain of actions.** An action may name the action that follows when it ends as expected,
such as implement, publish, checks and review. A host that runs a round as one pipeline, as
GitHub runs one workflow, carries out the chain and then sends each result in order. The engine
answers each as if it had come alone, and ignores a following action it no longer wants, by its
id. NexKit 6's round, from `route` to `report`, stays one workflow run.

## Runners

A runner runs one agent session for one stage. Its contract is part of the protocol: `runAgent`
gives it the stage, the version of the change it starts from, the stage's access, the prompt that
`prompt` writes from the inputs it names, the name of the stage's result schema, the agent, the
model and effort, the budget left in tokens, time and money, a session id to ask the same session
again, and for a fix round the checks it looks into first (`diagnose`). Until the protocol holds the
checklist and agents policies, the host adds to that schema what they add: a checklist field, and
the names of triage's profiles. Its result gives the stage's result, the tokens as input, cached and
output, the seconds and money it spent, a usage limit with its reset, whether the stop was
transient, whether it ran out of time, whether no result fit the schema, and the version of the
change it produced.

Each stage has its access, which the runner enforces: none for triage, read for plan and review,
write for implement and fix. There are two kinds of runner:

- **An agent CLI in a workspace:** Claude Code today, and Codex and Gemini when their runtimes
  are added. The agent works on files, and the host publishes them. NexKit 6's `Runtime` and
  `Provider` stay the Python interface inside this runner, which this repository holds. The runner
  is `agent.run_action`. It refuses a `runAgent` action that does not fit the protocol, or that
  names another agent, model, effort or access than the configuration gives the stage, before any
  agent starts. It saves its result as `run.json`, beside the stage's `result.json`. The session,
  the change version and the money are null: the publish job makes the commit, and nothing asks
  the same session again.
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
| Budgets | Tokens, time, money and model calls, per item and per stage | Time and money per stage | Tokens and time per request, model calls per stage |
| Retries (`retryWaits`) | Waits in minutes after a transient stop, at most 10, each 0–330 | `retry.wait_minutes` | The lease of a claimed request |
| Lost work (`lostWaits`) | Waits in minutes before lost agent work or checks start again, at most 10, each 0–330; a person after the last, or after one more start when its agent ran | None; at once, then 30, 60 and 120 minutes with autopilot | None |
| Usage limits | Resume after the reset, and how long to wait: the host gives the reset, or none when it does not resume | `usage_limit.*` | Resume after the reset |
| Agents | Agents, models and efforts per stage, fallbacks, profiles and triage | `agents`, `stages`, `profiles` | The model the request names |
| Splits (`splits`, `splitParts`, `splitDepth`) | Off or on, with the most parts, 1–10, and the depth, 1–10; the re-plan for fewer parts, no parts or repeated titles happens only with `autoWhenClean`, and a person's approval of a split that these rules refuse goes on as one change | Off; on with autopilot, at most 10 parts, 3 levels | On: the person approves the split |
| A parent's end (`parentEnd`) | `whenPartAccepted`, `person`: whether the parent ends once every part ended and one was completed, or a person decides (`splitIncomplete`); a host that leaves it out gets `whenPartAccepted` | `whenPartAccepted` with `close_parent_issues`, else `person`: the engine's default differs from GitHub's, where `close_parent_issues` is off by default | `whenPartAccepted`: the parent ends when its parts end |
| After a failure (`afterFailure`) | `person`, `replan`: whether an item that failed because its round was blocked with nothing to answer, with no change, is planned again after a person's comment or the end of an item that blocks it | `person`; `replan` with autopilot | `person` |
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
and answers the items to start. The host sends each a `requested` event of the kind `start`,
which `step` takes only for an item in `new` that waits to start and is not held, so a late or
repeated start changes nothing. A host that starts each item when it is made, as an application
may, needs no sweep, but still sends `blockerClosed` when a part's earlier part ends, and still
observes every open change from time to time.

The `sweep` call trusts its items. The host lists, oldest first, only the items that may start,
and gives each its labels, the index of its milestone in the host's milestone order (none when it
has no milestone), its wave, whether other changes wait for it (`awaited`) and its account. The
call refuses only a request that breaks its schema or names one item twice. It orders the items by
the first of the `priority` labels that an item has, then by milestone with no milestone last, then
by wave among items of the same milestone, then by the order the host listed them in. An item with
no milestone ignores its wave.

Starts stay within `maxParallel`, counting the items that already run. Each start takes a place. An
`awaited` item starts even when the limit is reached, and its start is flagged `beyondLimit`. An
item on an account in `limited` never starts, takes no place, and waits even when it is awaited.
Each item that does not start gets a wait: `for` is `place` or `usageLimit`, and `running` is how
many items ran at that point, those that already ran and the starts before it. A host shows that
count as its own words say, such as "4 issues are in progress".

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
| Observed as behind | `mergeable_state` is `behind`; left out of an observation when it is not | The draft was made on an older release than the latest |
| Observes on | `pull_request` synchronize, closed and reopened; a push to the base, for each open change; each sweep | A change of the draft or of the release; its worker, from time to time |
| A change closed unmerged | Not an observation: the host tells a person, who drops the item or asks for the work again | The draft was discarded: the same |
| One event at a time | An issue's commands and its NexKit pull request's rounds share one concurrency group keyed by the issue, which the workflow's `item` job derives with `nexkit item` and passes on (a person's pull request is its own item). A run that waits may be replaced by a newer one, so a run in the item's group takes every event of the item that is still at its source (`host.take`): comments, reviews and hold-label changes after the state comment's cursor, oldest first, each with its stable id from its source, such as the comment's id (see [Events and actions arrive more than once](#events-and-actions-arrive-more-than-once)). A command whose run was replaced is answered by the run that ran, and a run whose writes keep losing fails and leaves its event for the next run. The live pipeline does not call `host.take` yet. The sweep carries out no item's step itself: it sends a `step` event into each item's group, and that run carries the step out. The sweep still writes NexKit's own labels, CI bug issues and done notices, and its conflict rounds on pull requests, from the `autopilot` group. Planned in [#332](https://github.com/phuongnse/nexkit/issues/332): every run that writes an item's state shares such a group, and an approval sends one event into the item's group rather than writing states itself, so that the run is the state comment's only writer, which stands for the compare-and-set. Today the `autopilot`, `resume` and `approved-N` groups stay apart from the items' (`nexkit/templates/workflow.yml`) | The request's row, locked while its event is handled |

What exists only because of GitHub stays in the GitHub host: the GitHub App, statuses, branch
clean-up, watching CI and flaky runs, Dependabot, artifacts, carrying a review over a base merge,
and up-to-date rules. Each reaches the engine as an event or a result: a failure the base branch
has is a `runChecks` result, and a branch behind its base is an `accept` refused as `behind`,
which the engine answers with an `updateChange` that the host carries out by merging the base
into the branch.

The state comment's own write checks the revision too, as a backstop to the one concurrency
group per item: the GitHub host writes a state only as the next revision of the one its caller
read, and reads the comment again by its id just before it edits it, making no write when the
stored revision moved. GitHub has no compare-and-set, so a write by anyone else between that
read and the edit is still overwritten: the check narrows the race to that moment and does not
close it. A write that loses reads the state again and handles the event again, after a short
wait of random length, a few times; then the run fails, and the event stays at its source, past
the cursor, so that the next run takes it, never dropped.

## Conformance

`tests/conformance/` holds cases: a state, events in order, and the actions and state each must
give. They come from the decisions that `tests/test_scenarios.py` plays today. Its scenarios that
are about GitHub alone, such as vouching, Dependabot, shards, branch updates and carried reviews,
stay as tests of the GitHub host.

`tests/conformance/sweeps/` holds the cases of the `sweep` call: the items, the running count, the
limited accounts and the policies, and the exact starts and waits. A test sends each to
`nexkit-engine sweep` on standard input and to `POST /sweep`, and checks that both give the same
text, which fits the answer's schema.

A case may start from an example state of the protocol. After every call, the suite checks the
answer against the protocol, that the same request gives the same answer, and the outbox: every
action of the answer is pending, and an action leaves `pending` only with its result, its cancel
or its loss. It also checks NexKit's rule: every item that is not final has an action that works
on it (`runAgent`, `runChecks`, `publish`, `accept`, `createParts`, `replaceChange`, `updateChange` or `wait`), a
wait, a hold or attention. The rule that a round counts only when its agent ran to its end or
published joins these checks with the engine's rules of rounds, and so does the rule that a fix
round waits to be issued (`rounds.due`) only in `fixing`. A state keeps a round's questions only
while they wait for answers: in `awaitingApproval`, with attention naming them. Across the cases,
every transition of the states table is played, no case makes one that the page does not name for
its policies and for whether the item had a change, and an item is dropped from every state that is
not final. The engine passes the suite in this repository, and each host runs it through its own
adapter. A new rule of the engine comes with a case.

## Versions

- **Integers carry no fraction.** A field the protocol types as an integer refuses `60.0`, though
  JSON Schema 2020-12 takes it as one: the engine and NexKit count and index with these values,
  and so does every check of `schema.problems`, the agents' results included.
- **Two versions.** The protocol has a version, and the state document has a schema version of its
  own. The engine moves an older state document to the current schema when it reads one, and
  refuses one that is newer than it knows, so an engine rolled back never guesses.
- **Major and minor.** Every message carries the protocol's major version, `"protocol": 1`.
  Version 1 may change freely until NexKit 7.0 ships; at that release it is 1.0. From then on,
  each additive change raises the minor version, and every field it adds carries
  `"x-since": "1.<minor>"` in its schema; a new value of a field is a new major version, below.
  `protocol.MINOR` in `nexkit/protocol/` is the one place the minor version lives, and `load`
  leaves `x-since` out, as it does `$comment`.
- **A host asks which version it talks to.** The `describe` call carries the major version the
  host speaks, and answers the protocol's major and minor version, the engine's own version and
  the calls it answers; an engine that does not serve that major version answers an error. A host
  calls it and refuses an engine whose minor version is below what the host needs.
- **A new optional field is a minor change.** Every reader ignores a field it does not know, so
  the schemas of events, actions, results, calls and policies leave out
  `additionalProperties: false`, and an engine or host that is older takes a message with a new
  field. Such a field must be one that a reader that ignores it still acts on correctly.
- **Anything a reader must act on is a new major version** of the protocol: a new action or event
  kind, because a reader cannot act on one it does not know, a new value of a field, a new
  required field, or a field whose absence changes what a reader does. A host refuses an answer
  with an action it does not know, and the engine's error names it. When version 2 comes, the
  engine still serves version 1 for one more NexKit major release, so hosts move on their own
  time.
- **The state document is closed.** Only the engine writes it, so its schema refuses a field it
  does not name, its copy of the policies included, and a new field is a new schema version of
  the state document. The one part that stays open is `pending`: each entry is an action as the
  engine answered it, so it follows the action's schema, and a new optional field of an action
  is the protocol's change, not the state's.
- **A host's first state document is the host's to build.** The GitHub host builds each open
  item's document from NexKit 6's markers, open pull requests, paused runs and waits included, and
  an application that had a loop of its own builds each request's from its records. The import reads
  NexKit 6's markers only from NexKit's App, each only where NexKit writes it. The change's
  observation is what NexKit 6 knows of the open pull request: its head commit, and whether it
  conflicts or was merged when GitHub said so; none when there is no pull request. A part of a
  split is imported with how many levels of parts it is below the issue first split, so a part at
  the deepest level whose plan is too large waits for a person. Autopilot's answers are kept in
  `asked` only while the plan or round they started is on its way: a person's comment, or a run
  that reported and changed the item, ends them. A `/nexkit go` that stopped as blocked is placed
  where the engine puts the round's result, under the GitHub host's policies, and automatic
  approval applies only to an issue autopilot takes. Its questions wait for answers, with the
  ones that only a new plan carries out kept, and a change that needs protected paths waits for a
  person whatever the round asks. A round with nothing to answer fails, one whose only question
  recommends closing with a finding that no change is needed ends in `noChange`, and one that the
  engine refuses fails for a person. Answers that the engine would give leave the plan or round
  they start waiting for the sweep to start it. The plan that autopilot asked for after a pull
  request's round ran out of time is imported with `supersede`, so its split withdraws the pull
  request. A NexKit 6 run on its way becomes the action the
  engine waits for, and an automatic fix round on its way, with a count, is marked
  `rounds.automatic`, so that a stop gives its count back. A run paused at a usage limit, or until
  it runs again after a transient stop, becomes a wait with its time, the second keeping the
  count of runs again, unless NexKit 6 would no longer resume it, which fails for a person; an automatic run that ended without a report waits
  to start again, later each time. A merge that waits for required checks is `accepting` with that
  wait and, when NexKit 6 recorded when they first had not passed (`waiting_since`), with that
  time as `checksSince`, so that their end counts from the same moment, and a round that waits
  for a failure the base branch has is `fixing` with that wait. An
  item without a pull request that a lower wave of its milestone holds back waits for each open
  issue of that wave as for a blocker, read once at the import; an issue opened later in the
  wave is not waited for. A
  round that a hold label kept back is in the state of the step it kept back, held, or waiting to
  start once the label is gone; a conflict round waits in its checks, where the conflict policy
  applies with its count, and a fix round that a hold or a base failure kept back waits to be
  issued (`rounds.due`), counted when it starts or left to a person when none is left then.
  The checks that NexKit 6 found failing the same way before and after the last fix round are
  kept for the next automatic fix round to diagnose (`rounds.repeated`). So are the failing
  checks of NexKit 6's state comment (`rounds.failing`), which the checks after the next fix
  round compare with, as NexKit 6 compares them. The fix records and the last review of NexKit 6's
  state comment are imported with NexKit 6's round numbers, which count every run on the pull
  request, so the engine numbers the rounds after them from the next number. The version a fix
  round published
  (`rounds.fixed`) is not imported, since every imported fix round still publishes and the
  engine sets it then.
  What a round decided on its head keeps that head as the change's version, and a head pushed
  since is in the observation, so the engine checks it before it goes on. A run that waits to run again runs its own command again, and a lost round
  runs a fix round's command or a review, as NexKit 6 does; autopilot's record of tries counts
  only for the same command and target within six hours, and allows one start more once the
  agent ran. Each imported item is one the engine moves on from: a case in
  `tests/conformance/imports/` takes a NexKit 6 situation in, gives the state document out, and
  sends the events that carry the item on from there.

## Getting there

1. The engine grows inside NexKit 6: decisions move behind the protocol one by one, and the
   GitHub host calls them. While NexKit 6's code still moves an item between the decisions that
   the engine holds, the GitHub host builds the item's document for each of them from NexKit 6's
   markers, as its import reads them, sends the event that GitHub shows, and carries out the
   answer with NexKit 6's code and in its words. So far these are the decisions that follow a
   plan, a split after a round that ran out of time on its pull request included, whose
   `withdrawChange` the host carries out by closing the pull request as superseded. They are also the
   decisions that follow a `/nexkit go` that stopped as blocked with questions or with paths
   only a person changes, a writer's command that answers its questions with the option that
   only closes the issue, the reason a plan waits for without autopilot, what follows the
   checks and the review of a round on a pull request (`report`), what follows a fix round
   that stopped as blocked with questions or protected paths and a round that ran out of time,
   the end of a fix round's or a merge's wait for a hold or for the base branch's failure
   (`autopilot`'s sweep), which rounds count after a round that NexKit stopped (`report`,
   `maintain`), what follows required checks that fail at the merge (`report`), and the wait
   for required checks that have not passed yet, with its end after a time (`report` and the
   sweep's `autopilot`). `host.accept_answer` sends a merge's answer to `step` as an `accept`
   result and carries out what the engine answers. So far it sends a merge refused for required
   checks that have not passed yet, a merge refused because the head moved, which first sends
   the new head as an observation, so that the engine checks it, the last refusal after the
   base branch kept moving, the guards that stop a merge before it is tried (an issue closed,
   edited after a vouch or unreadable), and GitHub's other refusals that only a person clears.
   A merge refused for a conflict is sent too, with the conflict rounds used: under
   `conflicts.auto_resolve` the engine answers a conflict round while `conflicts.max_rounds`
   has one left, and otherwise a person. A pull request that GitHub reports as `dirty` only at
   the end of the refusal is sent as a conflict without `conflicts.auto_resolve` and as
   `needsPerson` with it. A merge refused because the branch is behind its base is sent as
   `behind`, with the updates used: under `conflicts.auto_resolve` the engine answers an update
   while `conflicts.max_rounds` has one left, which the host carries out
   (`maintain.update_branch`) and starts the review that carries the approval over, and
   otherwise a person; an update that GitHub refuses goes back to the engine, which hands it to
   a person. A merge that waited more than six hours for required checks, and whose branch
   GitHub reports as behind its base, keeps being tried under `conflicts.auto_resolve`: the
   engine makes that exception. The merge's observations reach the engine too
   (`host.change_seen`): a pull request that a person merged first ends the item as `accepted`, a
   change that the base branch already holds ends it as `noChange`, and a commit that a person
   pushes while the merge waits is checked. After NexKit's own merge, `host.part_ended` sends the
   parent its part's end, and the parent closes only when the engine answers `close`: it does so
   under `parentEnd` `whenPartAccepted`, which the GitHub host gives with `close_parent_issues`
   and a parent of at most as many parts as a state document holds. `report`
   sends every round's checks and review to `step`: a review that paused at a usage limit,
   with the reset when NexKit 6 resumes it, a review that failed, a person's own pull request
   as an item that is only reviewed, and a round whose commit conflicts with its base branch,
   whose conflict round a hold keeps back until the sweep sends the hold's end
   (`autopilot._conflicts_released`). The engine finds the checks that failed the same way
   before and after a fix round, which the state comment keeps, and `route` asks it which of
   them the fix round it starts diagnoses. The engine decides the runs again after a
   transient stop, under `retryWaits`, and lets a paused run go on under a hold as NexKit 6 does.
   `report` sends it a plan, implement or fix stage whose agent stopped at a usage limit or on a
   transient error, and `maintain.resume_paused` sends the tick of each due resume; the host
   carries out the wait, or leaves the run to a person. Which reset the host gives stays the
   host's, and so do a review that NexKit 6 runs again after a transient error and a transient
   stop of publish. After a plan, a stopped round and a review, `report` also sends the tokens,
   seconds, money and session that the runner's `run.json` gives. It checks the file against the
   protocol first. The rest of the result stays NexKit 6's, so the engine decides as before, and
   a file that is missing or does not fit sends none. It also decides what follows a command that
   autopilot started and lost, when its run never started, ended without a report, or handed the work on to a run that never
   started (`autopilot._again`, called by `_reportless` and `_handed_on`): `lost_again` sends
   `actionLost` at the time of the last start, with autopilot's count of losses, and the host
   carries out the start, the wait or the person. What counts as lost and the six-hour window of
   the count stay the host's. A person's own lost command waits for that person. After a blocked `/nexkit go` or fix round whose recommended
   answer only closes the issue, `report` mentions the person, as for any other wait, and the
   sweep does not mention them again; NexKit 6 said autopilot carries on and left the mention
   to the sweep, and this differs from it on purpose. A conflict that the base branch's move makes, found
   after an implement round or on a push to the base branch (`maintain`), stays NexKit 6's,
   as does the merge itself (`report.merge_pull`) but for the refusals above: those for required
   checks that fail or have not passed yet, a head that moved, a base branch that kept moving,
   a reason only a person clears, a conflict and a branch behind its base, a change that a
   person merged, one that became empty, a commit pushed while the merge waits, and the end of
   the direct parent. The parents above it, and a parent's end outside the merge, stay NexKit
   6's, and so does the update of a branch after the base branch moved
   (`maintain.stop_superseded`). The import ends a merge that waited more than six hours by
   itself, even when its branch is behind, since it reads no `mergeable_state`. A
   person's push during the wait for the base branch's failure is not sent yet: NexKit 6's
   sweep does not read the head then either. A `/nexkit go`
   that stopped as blocked with neither is the engine's too: it waits for a person, and with
   autopilot, under `afterFailure`, a comment or the close of an issue that blocks it plans it
   again. Only a `/nexkit go` that ended in an agent error stays NexKit 6's
   (`autopilot._since_stop`). Autopilot's start of the first plan of a new issue is the engine's
   too (`host.first_plan`): the host builds the document that the import builds for an issue with
   no plan, `new` and waiting to start, sends a `requested` start with no person, and dispatches
   `/nexkit plan` only when the engine answers with a plan run. Otherwise the issue waits for a
   person. NexKit 6's checks of a hold, trust, blockers and waves come first, so the engine is
   never asked for a held or blocked issue, and the other starts that autopilot makes stay
   NexKit 6's. Which of the issues that may start do, and in what order, is the engine's too
   (`host.sweep`, called by `autopilot.preview`): the host sends the start steps without a pull
   request as a `sweep` call, with the busy count as `running`, the index of each issue's
   milestone, its wave and whether pull requests wait for it as a bug issue about the default
   branch, and says each answer in NexKit 6's words. It names no account at a usage limit, so
   that rule shows only in the conformance cases. Every existing test
   passes after each step, and the engine's part of `tests/test_scenarios.py` becomes the
   conformance suite as it goes.
2. NexKit 7 is released when the GitHub host runs only through the engine. Repositories on
   NexKit 6 move to it with the usual upgrade steps, and the GitHub host imports their open work.
3. An application can then be a host, with its adapter in its own language and, when it keeps
   its users' keys, its own runner.
