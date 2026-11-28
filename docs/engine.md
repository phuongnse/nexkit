# The engine

_Planned for NexKit 7. Built so far: the protocol's schemas, in `nexkit/protocol/`; the engine's
`step` call with its states, whose decisions are a frame that takes NexKit 6's own rules one group
at a time, and its `prompt` call, which writes each stage's prompt as NexKit 6 does, as
`nexkit-engine step` and `nexkit-engine prompt` and over HTTP as `nexkit-engine serve`; its
`sweep` call, which orders and limits the items that start, as `nexkit-engine sweep`, which
NexKit 6's autopilot takes its starts from; the
GitHub host's state comment, its import of NexKit 6's open work, which the first run in an open
item's concurrency group stores as the item's state comment, and its taking of an item's events
from their sources (`host.take`), which `route` calls at the start of each run in that group; and
the GitHub host's decisions, which NexKit 6's autopilot, `route`, `report` and `maintain` take from
`step` and which store the engine's answer in the item's concurrency group (see
[getting there](#getting-there) for the decisions that read the stored state); and the GitHub host
runs the engine only as that program, for `step`, `sweep` and `prompt`, and imports none of its code. The rest of this
page is not built yet, and NexKit 6 runs as [How it works](how-it-works.md) says._

NexKit 7 splits NexKit into an **engine** that decides what happens to a piece of work, and
**hosts** that store the work, carry out what the engine decides and show it to people.
[Hosts](engine-hosts.md#hosts) maps each host onto the engine's words. An application can be a
host too: one where a person asks an agent to change something the application holds, such as
its own configuration, and the work lives in the application's database. Other forges, and other
agents, can follow without a change to the engine.

## Why

The same loop runs wherever an agent changes something for a person: a plan the person can
read, the change, the checks, an independent review, fix rounds and an accept that a person or
a policy decides. A forge runs that loop on its work items and their changes. An application runs
it on a request and a draft of what the application holds, with no repository and no CI service.
Writing the loop twice gives two loops that drift apart, so each fix and each lesson has to be
made twice.

The engine holds the loop once. A host decides only where the work lives and how each step is
carried out.

The reasons for this split are in [D10](decisions/d10-an-engine-decides-hosts-carry-out.md). Why
the engine and its protocol assume no host and no kind of change, so that a change may be files
or records, is in [D16](decisions/d16-the-engine-assumes-no-host-and-no-kind-of-change.md).

## Words

- **Work item:** one piece of work, such as a ticket in a tracker or a request in an application.
- **Change:** what the work item changes, such as a set of commits or a draft. It has an id and a
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
| Conformance suite | Events and the actions and state they must give | This repository; each release attaches it as `nexkit-engine-conformance-<version>.json` |

A host never imports the engine's code. It runs the engine as a program, `nexkit-engine <call>`,
that reads one JSON request on standard input and writes one JSON answer, or runs
`nexkit-engine serve` beside it, which answers the same requests over HTTP. A host in any
language can therefore run it, and the engine could be written again in another language
without a change to any host, as long as the conformance suite passes.
[D12](decisions/d12-a-host-runs-the-engine-as-a-program-or-over-http.md) gives the reasons.

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
  Python image pinned by digest; every week a proposed change sets the digest its tag names now, when that moved, so the image takes the base's security fixes. A release never
  replaces an image: one whose version is already in the registry fails before it pushes.
  Each image carries an attestation of its build provenance, so a host can check the commit
  and workflow that built it with
  `gh attestation verify oci://ghcr.io/phuongnse/nexkit-engine:<version> --repo phuongnse/nexkit`.
  Every proposed change, push to `main` and release builds the image and plays the whole
  [conformance suite](#conformance) against it over HTTP; an answer that differs fails the build,
  so no such image is published.

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
running round started with, but `lostWaits`, `budgets` and `agents`, which apply as the host gives
them at each step.

The states of a work item, and the events and actions that move it between them, are in [states,
events and actions](engine-states.md). The runners, the policies a host decides and the hosts are in
[runners, policies and hosts](engine-hosts.md).

### The prompt

`runAgent` names the inputs its stage reads, by kind, and the host fetches each before it calls
`prompt`:

| Input | What the host fetched |
| --- | --- |
| `item` | The work item's title and text, its change, whether it is a person's change reviewed as its own item, and what the change did outside the plan |
| `discussion` | The comments on the item and on its change, oldest first, each a person's comment, a note on a command or the answers a policy gave, and what people said on the change's version: their reviews and their comments on a line. The host leaves out what nobody it trusts wrote |
| `notes` | The note of the request that started the stage |
| `plan`, `previousPlan` | The approved plan; for a new plan, the latest one, where it falls in the discussion and the profile it used |
| `checks` | The checks' results, with the end of each output, the required checks that refused an accept, each with the host's words for why it refused and what the check is, and the checks a diagnosis round looks into, each with the failure it repeated |
| `linked` | The texts the item links to, the items it was split from with their discussions, and each link not read, with why in the host's words. The host bounds how many it lists |
| `evidence` | What the host kept of the runs the item names, with the host's own description: its `name` for the heading ("the CI runs") and a paragraph on where the files are and what they hold, each run's files, what is missing from it and its `since` text, which says whether a newer run passed, and the most bytes the host keeps; and the result files of failing checks, with the host's note on how much its checks keep of them. The engine writes no word about the kind of evidence |
| `parts` | The earlier parts of the same split that a part relies on |
| `diff` | The change against its base, since the last review, the merge of the base that the newest fix record made, and the files a merge conflicted in |
| `workspace` | The checks the agent can run, what only a person changes (`protected`), and the host's texts on how the agent sees and edits the change |

- **The host's words.** The call's `words` object names, as the host's people see them, what the
  prompts say in place of one host's own: `item`, the work item in a sentence ("request R-42");
  `itemHeading`, the item as a heading ("Request R-42"); `itemKind` and `itemKinds`, the noun in
  the rules and its plural ("request", "requests"); `change`, the change with its id ("draft 3"),
  "" when the item has none; `changeKind` ("draft"); `source`, where people wrote the texts, and
  `readers`, where they read the agent's text ("in the app"); `approve` and `fixRound`, the words
  that approve a plan and ask for a fix round; and `commands`, the commands people write on the
  item, with their noun. Each is a string of at most 200 characters, and every field is
  required. The engine refuses a stage's prompt without `words` and names the missing field, as
  in `request.words is missing`; a call with `unfit` needs none. The words sit outside the
  `<untrusted>` tags, as the host's own text, so a host builds them only from its own fixed text
  and ids, never from what people wrote. The engine breaks `item` and `changeKind` at their last
  space where a template breaks a line inside them, so a host's prompts keep the line breaks
  they had. [Hosts](engine-hosts.md#hosts) gives each host's words.
- **What only a person changes.** The `workspace` input's `protected` is a list of the host's own
  text, one string for each thing an agent may not change, such as a path or a record. The
  plan, implement and fix prompts list them, and the engine keeps them without reading them. A
  plan, implement or fix result names what the change needs of them `protected`, a list of the
  same kind. When a plan holds one, is not too large and finds that a change is needed, or when
  a blocked implement or fix round holds one, the item waits for a person to make the change by
  hand. The state document keeps a plan's list in `plan.protected`. The engine refuses a `workspace` without `protected` and names the missing
  field.
- **The host's workspace text.** The `workspace` input's `text` object holds the host's own
  sentences on how the agent sees and edits the change. Each replaces one span of a line in the
  plan, implement, fix and review prompts, a review of a person's change included, and in the
  sections about a merge of the base and the round before. `location`, where the agent works
  ("working through the application's tools on request R-12"); `reviewLocation`, what the review
  sees ("The review sees draft 3 as it is now."); `candidate`, the version the checks ran on
  ("the candidate draft"); `instructions`, where the project's instructions are ("(the
  application's style notes)"); `saving`, the rule on how the agent's edits are kept, a whole
  sentence; `leftovers`, the rule on what the agent leaves behind, a whole sentence;
  `summaryUse`, what the agent's summary becomes ("the draft's note"); `checksOn`, where the
  checks ran ("run on this exact draft"); `diffHeading`, the heading of the diff ("Changes
  against"); `pipeline`, what runs the checks ("the application's validation rules");
  `pipelineFiles` and `pipelineFile`, what a person's change to it touches, in the plural and
  with an article ("validation rules", "a validation rule"); `protectedExample`, an example of a
  protected path; and `personChange`, where a person's change is ("in this draft"). The fix
  stage reads the same `instructions`, `saving`, `leftovers` and `summaryUse`, and these:
  `fixLocation`, where the fix agent works, which ends before the change's name ("working
  through the application's tools on"); `earlierWork`, a sentence that the change already holds
  earlier work ("The draft already holds earlier edits."); `mergeRule`, the rule against merging
  by hand, a whole sentence; `versionKind` ("version") and `baseKind` ("published request"), the
  nouns for one version of the change and for what it is merged with, which the fix prompt, the
  merge sections, the review's previous round and the plan and implement prompts' description of
  `no_change` use ("version 3a4b5c6", "merged the published
  request"); `mergeInto` ("into the draft, without saving it"), `lastMerged` ("this draft last
  took it in") and `mergeUndo` ("do not discard or publish it."), how the base was merged, when
  it last reached the change and how the agent leaves a merge in place; `sideHistory`, how the
  agent sees what each side changed in a conflicted file, a whole sentence; `sideNote`, a
  sentence that a side's note is not an answer; `otherVersions`, a sentence for a review after no
  fix round ("Any later versions were saved without a fix round."); `workspaceName`, where
  the agent reads the change, which the engine's notes name when a version or a diff is not
  there ("this draft's history"); and `locationForm`, how the agent writes where a review finding
  or an open conflict of a fix round points, after "`location` is" in the review and fix prompts ("the record and its field, such as `form
  Customer › field Phone`, or an empty string when it is about no record"). Each is a string of at most 500 characters, and every field is
  required in every call with a `workspace`. A text holds no line break, except `mergeRule` and
  `sideHistory`: each is one sentence that a template wraps over two lines, and a host whose
  text spans lines indents each further line by two spaces, as the bullet it sits in does. The
  engine refuses a call whose `workspace` has no `text`, or lacks one of its texts, and names the
  missing field, as in `request.inputs.workspace.text.location is missing`. The texts sit outside
  the `<untrusted>` tags, as the host's own text, so a host builds them only from its own fixed
  text, never from what people wrote. [Hosts](engine-hosts.md#hosts) gives each host's texts.
- **What a stage needs.** Every stage needs `item`, and every stage but triage `workspace`.
  Implement, fix and review need `plan`, which is null when the item has none, and fix and review
  need `diff`. The engine refuses a call without them.
- **Earlier rounds come from the state.** The fix and review prompts read the fix records and the
  last review from the request's `state`, and a request without one shows none. The newest fix
  record is the round a review follows, and `diff.merged` holds the merge of the base that it made.
  The fix prompt shows the last review's blocking findings only when that review read the change's
  current version, `change.version`. A host that does not keep the state builds one for each call from what it stores.
- **Limits.** The engine applies them: how much of each text and block a prompt keeps, how many
  comments, and in what order. A text that a prompt keeps only the start of, or leaves out, goes
  whole to a file in the folder that `texts` names, an absolute path outside the working tree,
  POSIX or Windows, where the agent reads it. The engine names the file of an item or a linked
  change from the host's noun for it (`itemKind`, or `changeKind` for a change) and its id, such
  as `request-12.md`. A comment keeps its own name, such as `comment-4.md`. It adds a short hash when
  the name holds a character a file name cannot, such as the space in a noun like "pull
  request", so two names never share a file. So the texts come whole, but a diff and a check's output come cut to what
  a prompt shows of them, and the schema refuses more.
- **The answer** holds the prompt, the texts it shortened or left out, the links it lists as not
  read, and the files with their texts, which the host writes before the agent runs. A host
  that writes them first checks that each path is inside its `texts` folder, and writes none
  when one is not.
- **Policies.** Beside the host's policies, the call reads two of its own, as its schema names
  them: `checklist`, the items the plan and the review answer; `noChange`, the findings that
  show a plan or implement stage that no change is needed, one to five texts, each with its own
  line breaks, under a bullet that the engine writes and, for the implement stage, after a count
  that it writes ("Two findings count"); and `profiles`, the profiles triage chooses from, each
  with its `when` as a string. A plan needs `budgets.implement.seconds` as an integer, from the
  protocol's `budgets` policy, and the engine refuses one without it. It also refuses a plan or
  implement call without `noChange`, as in `request.policies.noChange is missing`.
- **A result that did not fit.** With `unfit`, why a session's result did not fit its schema, the
  call writes the prompt that asks the same session for its result alone.

Every decision of the loop is the engine's: plans, questions and answers, splits, approval, fix and diagnosis rounds and how they are counted, the review's rules, budgets, retries after a transient stop, usage limits, holds, waits, and when a person is needed. A host only observes and carries out.

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
  outcomes, findings in short, failure signatures, counts, and the tokens and time its
  agents spent. It also keeps the history that the next fix and review prompts show: the latest
  fix rounds' records, each with its number, who started it, whether it diagnosed, its note, the
  agent's summary and cause, and the checks that still failed after it with their failure
  signatures, and the last review's round, version, verdict and findings with severity, location
  and body. A location is the host's own text for where a finding points, which the agent writes in
  the form that `locationForm` gives. The engine keeps it as it came, clipped, without reading it,
  and shows it to the next fix and review prompts, or shows "general" when it is empty. The question that the engine asks for an open conflict of a fix round starts with the conflict's location, clipped, in backticks. The engine records a fix round when its agent returns a result that is not blocked, and
  the checks after it complete the record. A round under way keeps who started it, whether it
  diagnoses and its note until then (`rounds.fixRound`). Other long texts, such as a plan's prose,
  check output, a discussion and evidence, stay with the host as texts that the state names, and
  reach the engine only in `prompt`.
- **It has a size.** The protocol sets the largest state document: 48,000 bytes as JSON in UTF-8
  with its keys sorted and no spaces, so that its base64 fits a text field of 65,536 characters. What results and events bring is bounded as it comes in. Each list keeps at most as
  many items as `maxItems` in its schema says: 10 questions of 6 options each, 20 protected paths,
  10 split parts, each needing only earlier parts, 30 findings with the blocking ones first, 30
  failure signatures, 300 failing checks, each kept as a 12-digit digest of its name and signature,
  150 checks that repeated, and 50 criteria and 50 checklist answers with those a decision reads
  first. A split of more parts keeps none of them, only how many it named, so a split is never filed
  cut. Each text is clipped to what a person needs to read: a question at 1,500 characters, an option at 500,
  the item's title and a part's at 256, a path at 300, a finding at 300, a finding's location at 300, a reason for a person at
  1,000 and a check's name at 40, enough to compare failures by. The history is clipped as the
  prompts clip it: a note at 2,000 characters, a summary at 4,000, a cause at 2,000, a finding's
  body at 1,500 and the failure signature of a fix record at 200. The state keeps the latest 5 fix
  records, each with at most 30 failing checks, and the last review's 30 findings, in the
  reviewer's order, or the blocking ones first when there are more, with how many were left out.
  A long failure signature is kept as its hash, never cut, because rounds compare signatures.
  When a document would still pass the limit, the engine shortens texts that a person reads in full
  where the host keeps them, each to the most that fits, in this order: the findings, the questions
  and their options, the protected paths, the attention's text, and the names of repeated checks
  past the 10 that a diagnosis round looks into, which only the host keeps for people. Only
  then does it shorten the history: the last review's suggestions and then its blocking findings,
  the fix records' notes, summaries and causes and the note of the round under way, the check
  names, failure signatures and conflicting files of the fix records, and the locations of the
  last review's findings. If the document still does not fit, the oldest fix records leave out the
  checks that still failed after them, which a prompt then shows as unknown. It never shortens a
  count, a status, an id, a title or a signature, which decisions read, and a host that builds a
  first document keeps it the same way. The engine never answers a state that its own schema refuses, such as an outbox past its
  `maxItems` when a host never reports its actions: it refuses that event instead.
- **It is the truth.** A host may keep columns or markers that copy parts of it, for its own
  lists and pages, written in the same transaction, but the engine reads only the document.

### Events and actions arrive more than once

The protocol is built for delivery at least once, in any order a host's queues give it.

- **Every event has an id**, the same each time the host sends that event again and different
  for every other event. One delivery from the host's source may give several events for one item, so an id
  names the event, such as `<delivery>:<kind>`. The state keeps the ids of the 20 latest events
  that changed it, and the engine takes an event whose id it holds as a duplicate that changes
  nothing.
  A host that takes events from its sources keeps a cursor beside the state, with the time and the event id of the newest source it took. It answers only the sources after the cursor, since `recentEvents` keeps the ids of events that changed the state, and run results would push a source's id out of it. The cursor moves past a source even when its answer changed
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
    queue, and from a read that sees every write the host made itself, such as the change itself rather than a cached copy. A stale read would take the change back to a version it
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
    finds closed unmerged is sent so, and the host tells a person, as
    [the hosts' table](engine-hosts.md#hosts) says for a change closed unmerged; the engine's work
    on the change goes on until that person decides. The
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
  An id is never one that a pending action has, since a host that stores several events as one
  revision hands the next event a state that is still at the revision before them.
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
  transient stop. A lost publish, replace, update, accept or parts creation fails the step, for a
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

## The sweep

Waits, holds and *blocked by* links live in each item's state, so `step` decides whether an item
may start. `sweep` only chooses among the items that may: it orders them by priority, group,
wave and age, applies how many items run at once and which accounts are at a usage limit,
and answers the items to start. The host sends each a `requested` event of the kind `start`,
which `step` takes only for an item in `new` that waits to start and is not held, so a late or
repeated start changes nothing. A host that starts each item when it is made, as an application
may, needs no sweep, but still sends `blockerClosed` when a part's earlier part ends, and still
observes every open change from time to time.

The `sweep` call trusts its items. The host lists, oldest first, only the items that may start,
and gives each its `tags`, the rank of its group in the host's order (`group`, none when it has no
group), its wave within the group, whether other changes wait for it (`awaited`) and its account.
The call refuses only a request that breaks its schema or names one item twice. It orders the items
by the first of the `priority` tags that an item has, then by group with no group last, then by
wave among items of the same group, then by the order the host listed them in. An item with no
group ignores its wave.

Starts stay within `maxParallel`, counting the items that already run. Each start takes a place. An
`awaited` item starts even when the limit is reached, and its start is flagged `beyondLimit`. An
item on an account in `limited` never starts, takes no place, and waits even when it is awaited.
Each item that does not start gets a wait: `for` is `place` or `usageLimit`, and `running` is how
many items ran at that point, those that already ran and the starts before it. A host shows that
count as its own words say, such as "4 items are in progress".

## Conformance

`tests/conformance/` holds cases: a state, events in order, and the actions and state each must
give. They come from the decisions that `tests/test_scenarios.py` plays today. Its scenarios about one host's own work stay as that host's tests.

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
every transition of the [states table](engine-states.md#states) is played, no case makes one that
that table does not name for its policies and for whether the item had a change, and an item is
dropped from every state that is not final. The cases also play everything the protocol's schemas
define, which the suite reads from them: every value of each policy, every kind of event and every
kind of action. The engine passes the suite in this repository, and each
host runs it through its own adapter. A new rule of the engine comes with a case.

The cases are short forms that only the test runner reads. `python3 scripts/conformance.py build OUT`
writes the cases of both folders as one JSON file that anyone can read. It names the protocol's
version and NexKit's version. Each case has its name and its requests in order. A sweep case's name
starts with `sweeps/`. Each request is whole and comes with the exact text of the engine's
answer, a refusal's included. The cases in `tests/conformance/imports/` stay out, since an import is one host's work and not a call of the engine. The file fits
`nexkit/protocol/v1/conformance.schema.json`. It is not committed, because it is large and carries
NexKit's version. Build it when you need it, or take the suite of the version you pin: each release
attaches the file that its image passed as `nexkit-engine-conformance-<version>.json`, which
`gh release download v<version> --repo phuongnse/nexkit --pattern 'nexkit-engine-conformance-*'`
fetches.

`python3 scripts/conformance.py check FILE` plays a file against an engine. With `--url URL` it sends
each request to `POST URL/<call>`, as `nexkit-engine serve` answers. With `--program COMMAND` it runs
`COMMAND <call>` and writes the request on its standard input, as `nexkit-engine` reads it. It prints
each answer that differs from the file's, naming the case and the request's number, and exits 1. An
answer differs when its text is not the file's, or when its status is not 200 or 0 for an answer and
400 or 1 for a refusal. It exits 0 when every answer matches. A file that does not fit
`nexkit/protocol/v1/conformance.schema.json` makes `check` print its problems and exit 2, without
sending any request.

An engine written again in another language passes when it gives the same answers: the same text for
each request, refusals included, word for word. A host takes the requests as the inputs of its
adapter's tests and the answers as what its adapter carries out.

## Versions

- **Integers carry no fraction.** A field the protocol types as an integer refuses `60.0`, though
  JSON Schema 2020-12 takes it as one: the engine and NexKit count and index with these values,
  and so does every check of `schema.problems`, the agents' results included.
- **Two versions.** The protocol has a version, and the state document has a schema version of its
  own. The engine moves an older state document to the current schema when it reads one, and
  refuses one that is newer than it knows, so an engine rolled back never guesses. A document of
  schema version 1 moves to version 2, which gives each finding of the last review a `location`
  in place of its `file` and `line`, and names the plan's list of protected paths `protected`
  in place of `protectedPaths`. The `workspace` input and the plan, implement and fix results
  name that list `protected` too. A finding's location is its `file` and `line` as `file:line`, its
  `file` alone when the line is 0, and an empty string when it has no file (`protocol.upgrade`).
  Before 1.0, a document of schema version 1 that still holds `used.usd` is read without it.
  `tests/golden/state/v<N>/` keeps a document of each state for each schema version, from 1, and
  a test steps each one with a `tick`: the engine must read it and answer at the current version.
- **Major and minor.** Every message carries the protocol's major version, `"protocol": 1`.
  Version 1 may change freely until NexKit 7.0 ships; at that release it is 1.0. From then on,
  each additive change raises the minor version, and every field it adds carries
  `"x-since": "1.<minor>"` in its schema; a new value of a field is a new major version, below.
  `protocol.MINOR` in `nexkit/protocol/` is the one place the minor version lives, and `load`
  leaves `x-since` out, as it does `$comment`.
- **A test holds each released minor version.** `scripts/protocol_freeze.py` writes the schemas to
  `tests/golden/protocol/`: in the release of 7.0, and in each release that raises
  the minor version. Whenever that folder holds a copy, a test compares the tree with it. It
  allows a new optional property whose `x-since` is above the copy's minor version, and a changed
  `$comment`. It refuses every other change, a changed `description` or `default` included: a
  removed or renamed property, a new required one, a changed type, enum, `const` or limit, and a
  new or removed schema file, such as an event or action kind. `state.schema.json` instead needs a
  new state schema version. The script refuses to replace a copy that the tree breaks.
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
- **A host's first state document is the host's to build.** A host that had a loop of its own builds each open item's document from its records, within the protocol's schema and size (see [the state document](#the-state-document)). The engine then moves the item on from it as from any other.

## Getting there

1. The engine grew inside NexKit 6: decisions moved behind the protocol one by one, and every
   decision of the loop is now its own. Every decision of the GitHub host decides on the item's
   stored state document, or on the import in memory when none is stored, and stores the engine's
   answer in the item's group, a row of events as one write. Only the first import builds a
   document from NexKit 6's markers, as its import reads them. A person's command and a closing
   command decide on the stored state too. `tests/test_host.py` guards this: it fails any `step`
   that NexKit's code asks for on another state, which is anything but the stored document, the
   state the row's previous event gave, the import in memory while nothing is stored, or a call
   inside the first import.
   The plan's report sends the plan's result to the engine once for every plan of an issue that
   autopilot takes, a clean plan and one that finds no change too, and stores the answer. Asking
   what a plan waits for reads the stored state alone and stores nothing. The sweep's and the step
   run's decision after a plan (`after_plan`) decides on the stored state alone too, and sends the
   engine only what it has not seen: the plan's result, while the stored state waits for that plan
   run and has seen neither its result nor a comment after the plan, and a collaborator's comment
   after the plan that the state has not taken. It then carries out what the stored outbox holds (a
   close, a split, a start or a wait for a person), and an item that holds nothing to carry out
   waits for a person with the reason.
   A blocked or failed `/nexkit go`, a fix round that stopped as blocked and a round that ran out
   of time decide on the stored document alone, including its `depth` and `asked`: `report` sends
   the round's result on the state that waits for the round's agent run, and the sweep sends it
   only while the state still waits for that run and does not hold the result, and otherwise
   carries out what the state holds.
   A usage limit, a transient error and a resume that comes due decide on the stored document
   alone: its pending agent run, its count of runs again and its wait. So does a lost command:
   it counts the losses from the stored `again.lost`, sends only the tick of a wait that the
   stored state holds already, and carries out an `outage` that waits for a person. A writer's
   vouch after an untrusted edit also decides on the stored state: when the item's own run already
   took it, the step carries out what the stored state holds, and a plan that an untrusted edit
   stopped, which never reached the engine, starts again. A command that NexKit dispatches carries
   out the action that the stored state has pending for it, and takes its diagnosed checks from
   that action; with nothing pending it is a new request, `dispatch-<run id>`, which for an
   automatic fix round says so, so that the engine counts it as its own round, and the round
   takes its diagnosed checks from the fix run that the engine started for it. A fix round that
   carries out a person's pending fix, and a retried or resumed one with nothing pending,
   diagnose NexKit 6's repeated checks, as many as the engine diagnoses at most, and ask the
   engine nothing.
   The automatic fix rounds that a round's end counts are the stored count alone, and so are a
   transient review's runs again. The conflict rounds are the stored count. The run of a conflict round that the push run, the
   schedule or another item's merge dispatches counts it before the round starts. The review that
   follows a branch update stores the update, and the run that the push run starts after it stopped
   a counted automatic fix round's checks gives that round's count back. The publish
   job of an implement or fix round stores the agent's result and the publish result of a commit
   that does not conflict, in one write, right after it pushes, so that the state waits for the
   round's checks while they run, and the round's checks and review then decide on the stored
   state alone: with the checks pending, the checks' result and then the review's go, and with the
   review's agent pending, as after a usage limit or a transient stop of the review, only the review's
   result goes. A round whose stored state waits for neither waits for a person with that reason. The `report` finds the round stored, and stores it only when that job could not. The `report` of a round or a merge that an untrusted
   edit stopped stores that stop too: the round's result says `untrusted`, or the accept is
   refused as `untrusted`, and the engine answers `failed` or `ready` with attention `untrusted`.
   The host sends the event that GitHub shows, and carries out the answer in NexKit 6's words,
   comments and statuses. A start, a merge, a branch update, a close,
   a cancel or a rerun follows an answer of the engine. `tests/test_answers.py` keeps it so: it
   reads every module outside `nexkit/engine/` except `nexkit/github.py`, which defines the writes,
   and fails on a write that no call of the engine comes before. The writes that serve no item are
   named with their reasons in `NAMED`, the table of `tests/answers.py` that `test_answers.py`
   imports: the sweep's own dispatch, the workflows on the base branch after
   a merge, the default branch's CI reruns and the wait dispatch. So are the writes that carry out
   an answer that an earlier job took from the engine. The detail of each move is in the docstring
   of `nexkit/host.py`.

   What the host observes stays with it, since only GitHub shows it. The engine decides from
   these facts, and the host gives them as they are:

   - Which reset a usage limit gives, and what counts as a lost run, with the six-hour window that
     selects autopilot's record for the import; the count after it is the stored one.
   - A pull request's `mergeable_state`: `acceptable` for `clean`, `unstable` or `has_hooks`,
     `behind` and `dirty`.
   - A hold label, a fork, a writer's vouch, an edit after a vouch, a person's own pull request
     and an unknown command.
   - Parts that were never filed as sub-issues, and an implement round that still runs.

   Three differences from NexKit 6 are known on purpose. A vouch on a pull request with no
   automatic fix round left waits for a person, where NexKit 6 started a counted round. A person
   whose command the engine refuses gets a reply, which NexKit 6 never gave. After a blocked
   `/nexkit go` whose recommended answer only closes the issue, the report mentions the person
   and the sweep does not mention them again. The other differences are in
   `tests/differential/known.py`. Every existing test passes after each step, and the engine's
   part of `tests/test_scenarios.py` became the conformance suite.

   The GitHub host builds each open item's first document from NexKit 6's markers, open pull
   requests, paused runs and waits included. The import reads NexKit 6's markers only from
   NexKit's App, each only where NexKit writes it. The change's
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
   engine refuses fails for a person. A fix round of an issue autopilot takes that stopped as
   blocked is placed the same way, and so is a round, a `/nexkit go` or a fix round, that ran out
   of time, which the engine answers with the plan that asks for a split, or with `failed` for a
   part that is at the deepest level or whose issue already asked. Answers that the engine would
   give, and that split plan, leave the plan or round they start waiting for the sweep to start
   it. The plan that autopilot asked for after a pull request's round ran out of time is imported
   with `supersede`, so its split withdraws the pull request. A split that an issue above the
   item asked for is imported into `asked`, so a part that runs out of time again waits for a
   person. An issue autopilot does not take keeps a time-out in `failed` and a blocked fix round
   waiting for answers. A finished plan of an issue autopilot takes, with no collaborator's comment
   after it, is imported as the engine's answer to the plan's result, played under the item's own
   policies: its outbox holds what follows the plan, such as the implement run of a clean plan, and
   the plan run's id is among the events seen, so the result is never sent again. Any other
   finished plan waits for its approval, its answers or a hand-made change. A NexKit 6 run on its
   way, or a command that autopilot started and that
   has not run yet, becomes the action the engine waits for. A run keeps the count of its runs again after a transient error when it is one
   (`again.transient`, as a paused one does). Either keeps autopilot's count of lost starts
   (`again.lost`, and `again.once` when the record says the start runs once more), from the record
   of the same command and target started within six hours. An automatic fix round on its way, with a count, is marked
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
   (`rounds.fixed`) is not imported, since every imported fix round in its agent still publishes
   and the engine sets it then. A round whose run is in its check jobs (`shards`, `verify`,
   `combine`) is imported waiting for its checks on the pull request's head, and its report
   sends the checks' result on that stored state; a counted automatic fix round of it is marked automatic
   (`rounds.automatic`), so that a stop gives its count back.
   What a round decided on its head keeps that head as the change's version, and a head pushed
   since is in the observation, so the engine checks it before it goes on. A run that waits to run again runs its own command again, and a lost round
   runs a fix round's command or a review, as NexKit 6 does; autopilot's record of tries counts
   only for the same command and target within six hours, and allows one start more once the
   agent ran. Once stored, the count is the state's `again.lost`: a later loss counts from it, and
   the six-hour window only selects which record the import reads. Each imported item is one the engine moves on from: a case in
   `tests/conformance/imports/` takes a NexKit 6 situation in, gives the state document out, and
   sends the events that carry the item on from there.
2. NexKit 7 is released when the GitHub host runs only through the engine. Repositories on
   NexKit 6 move to it with the usual upgrade steps, and the GitHub host imports their open work.
3. An application can then be a host, with its adapter in its own language and, when it keeps
   its users' keys, its own runner.
