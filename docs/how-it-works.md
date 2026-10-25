# How NexKit works

## One run, seven jobs

Every command starts one run of the reusable workflow
[`pipeline.yml`](../.github/workflows/pipeline.yml). Jobs that do not apply are skipped.
Some other events start a run too; they run only `route` and `maintain` (see
[Work without a command](#work-without-a-command)).

| Job | Runs for | Token permissions | Secrets | Does |
|---|---|---|---|---|
| `route` | every event | read; comments, reactions, statuses; workflow runs (`actions: read`) | none | Checks the command and the author's write access, reads the config from the default branch, picks the action, and shows that the run started. Never executes repository code. |
| `agent` | plan, go, fix | read only | its stage's agents and their fallbacks | Installs the pinned runtimes the configuration uses; for go and fix, restores the [caches](configuration.md#caches) and runs `setup`; for fix, downloads the [result files](configuration.md#result-files-of-failing-checks) that the last round's failing checks kept (`actions: read`); for go and fix, downloads the [evidence of the CI runs](configuration.md#evidence-from-ci-runs) the issue names (`actions: read`, `checks: read`). Then one agent session, and at a usage limit the stage's [fallback agents](configuration.md#fallback-agents) in turn; with profiles, a plan starts with a triage call. Uploads the result and a patch. |
| `publish` | plan, go, fix | write | the push token, when `publish.push_token_secret` names one | Posts the plan, or applies the patch, rejects protected paths, commits, pushes and opens the pull request. Never executes repository code. |
| `verify` | go, fix, review | read only | none | Restores the [caches](configuration.md#caches), then runs `setup` and the checks on the published commit, and keeps the result files of the checks that failed. |
| `review` | go, fix, review | read only | the review's agent and its fallbacks | A fresh agent session with read-only access reviews the diff against the plan, the discussion, the check results and the previous review round. |
| `report` | every action | write | none | Sets commit statuses, posts the review, shows the outcome in the run's comment, starts the next automatic round or asks for a person. With `merge.auto`, merges an approved pull request, closes its issue and starts the `merge.after_workflows` on the base branch. Never checks out or executes repository code. |
| `maintain` | issue and milestone events, pushes to the default branch, the schedule, the `resume` and `autopilot` dispatches | contents (to merge); issues, comments, workflows, statuses; check annotations | none | Work that follows an event rather than a command: closing a parent issue, resuming a command paused at the usage limit, starting a round for a pull request that conflicts with its base branch, an [autopilot](#autopilot) sweep. Runs no agent, and never checks out or executes repository code. |

The agent does not commit or push. It edits the working tree, and NexKit turns those edits
into a commit in a different job. As a result:

- The session that runs the agent never holds a token that can write to the repository.
- Jobs that hold write tokens never run code from the repository or from the agent.
- Checks run in a job with no secrets at all.
- Jobs that run the agent's code restore caches but never save one, so nothing they
  produce reaches later runs.

`report` needs `contents: write` only to merge when `merge.auto` is on. Job permissions
cannot depend on the configuration, so it holds that permission in every run, but it never
pushes, and with `merge.auto: false` it never merges.

## What you see during a run

GitHub cannot show a live run inside a comment, and only `route` and `report` update the
run's comment, so NexKit updates the issue or pull request when a run starts and when it ends.

Every run gets its own NexKit comment, posted by `route` when the run starts, so it appears
right after the command or review that started it. `report` edits that comment with the
outcome. Comments of earlier runs keep their final state.

| When | Command on an issue (`plan`, `go`) | Round on a pull request (`fix`, `review`, automatic, *Request changes*) |
|---|---|---|
| Start (`route`) | 👀 on the command comment. A new NexKit comment names the command and shows ⏳ with a link to the run. | 👀 on the command comment. A new NexKit comment shows the round number, its trigger and ⏳ with a link to the run. `nexkit/checks` and `nexkit/review` turn `pending` on the current commit and link to the run. |
| End (`report`) | The same comment shows ✅, ❌ or ⏸️, the run log, the agent of each stage and the result: the plan, the pull request and its outcome, or why the run stopped. | The same comment shows ✅, ❌ or ⏸️, the run log, the agent of each stage, the round's commit, checks, review verdict and tokens (`N/A` when a stage that ran has no count), and the next step. Statuses are set on the new commit; a commit the round did not check gets its earlier statuses back. |

✅ means the run did its work, even when the checks fail or the review asks for changes
(the round's columns show that). ❌ means it stopped on an error. A run started by a
comment ends with 🚀 or 😕 on that comment. Runs started by a dispatch or by a
*Request changes* review have no comment to react to. ⏸️ means the stage's agent, and each
of its fallback agents, stopped at a usage limit; the run waits for the limit to reset
(see [Usage limits](#usage-limits)), and its command comment keeps 👀. ⏭️ `continued`
replaces ⏸️ once a later run carries the command on.

### When a person is needed

`report` decides from the run's outcome and the agents' structured output whether a
person is needed. It records the reason in the run comment's hidden marker, and with
`notify` set it mentions those people in a new comment (see
[Mentions](configuration.md#mentions)):

| Run result | A person is needed |
|---|---|
| Plan posted, and it has questions or says the issue is too large | yes, unless [autopilot](#autopilot) answers or splits it, or plans again for parts titled like the issue |
| Plan posted without either | no |
| The agent returned `blocked` | yes, unless autopilot answers it |
| The agent failed or errored, publishing failed, or the review did not complete | yes, unless the error was transient and the command runs once more, or autopilot asks for a split after an implement or fix agent ran out of time (see [limits on parts](#autopilot)) |
| Checks or review still fail and no automatic fix rounds remain | yes |
| Checks or review still fail after a fix round found nothing to change for a cause outside the code and ran them again | yes |
| Ready, with `merge.auto` off | yes |
| Ready, with an acceptance criterion the review could not verify | yes, also with `merge.auto` on |
| GitHub refused the automatic merge | yes, unless the refusal passes by itself: see [Failure handling](#failure-handling) |
| Merged, or an automatic fix or conflict round started | no |
| Paused at the usage limit, resuming by itself | no |
| Paused, and NexKit will not resume it (`usage_limit.resume: false`, or a second pause with an unknown reset time) | yes |
| The base branch changed workflow files that a round must merge, without a push token that may push them | yes |

With autopilot on, its sweeps come to the same answers for the issues in scope, and say
what else waits for a person: an issue whose pull request needs one, an author without
write access, a blocker outside the scope, a command that never ran. When an issue starts
to wait for a person and nobody was mentioned since its latest run, the sweep mentions
`notify` once, on the issue. `nexkit status` lists every such issue under *Needs a person*
with the same reason.

### One run at a time

Runs for one issue or pull request run one at a time, in order. Only commands, *Request
changes* reviews, dispatches and the short run for a closed issue wait in this queue;
other comments do not. GitHub keeps one waiting run, so a command posted while another
waits replaces it. A command that
never got 👀 did not run; post it again.

A pull request runs one NexKit round at a time. Its rounds (fix, review, automatic and
conflict rounds) share the pull request's queue, and the `/nexkit go` run that opened it is
one of its rounds too, until that run has checked, reviewed and, with `merge.auto`, merged
it. That run waits in the issue's queue, so NexKit orders the two itself:

- A conflict round does not start while the implement round runs. When the implement
  round ends and the pull request conflicts, that round starts the conflict round, on the
  branch as it left it: after a refused merge, or when it ends ready, with findings and no
  automatic round left, or without a review.
- A person's command or *Request changes* review on the pull request meanwhile gets a
  reply that the implement round still runs; comment it again once it ends.
- The rounds that a run starts as it ends, such as an automatic fix round or the one more
  try, run after it in the pull request's queue.
- A run whose workflow run ended without a report holds nothing up.

A hidden marker in each run comment holds the run's URL, so `report` edits the comment its
own run started. If that comment was deleted, `report` posts a new one.

## Logs, summary and artifacts

The run link opens the Actions run. The `agent` and `review` jobs show what the agent does
while it works.

**Log.** One line `· text` (first 240 characters) per message from the agent, and one line
`! text` per notice from the CLI or NexKit, such as a move to a fallback agent: "! The
`claude` agent stopped at its usage limit; running the implement stage on `claude-api`".
Each tool call is a collapsible group, printed when its result arrives:

```text
[03:12 #14] ▸ Bash scripts/e2e.sh --timeout 600       <- group title; expand for the rest
  Input:
  command: scripts/e2e.sh --timeout 600
  description: Run the end-to-end tests

  Result: error
  <first and last 20 lines of the output, at most 4,000 characters>
Warning: Bash failed: 2 tests failed
```

The title has the time since the agent started, the turn number, the tool and a short
detail. Inside are the input (long values, such as a file's new content, are shortened the
same way as the output), whether the result is an error, and the output. A failed tool
call also adds a warning annotation with its first line. A call still running when the
session ends shows `no result`. Tool output is printed between `::stop-commands::` markers
with a random token, so text from the repository or the agent cannot set outputs, add
masks or create annotations. The model's thinking is never printed. With
`log.tool_output: "none"` the log has one line per message and per tool call instead
(`▸ Tool detail`, 200 characters).

**Run summary.** When failing checks name result files, the `verify` job's summary says
how many it kept and lists the paths that were not found and the files that the size limit
left out. At the end of the `agent` and `review` jobs, the run's summary page shows
the stage and a one-row table of its result (done, blocked, paused or error), duration and
tokens, plus the verdict for a review. Below it are the agent that ran the stage, with its
runtime, provider and model, the profile with profiles, and when the stage moved to a
[fallback agent](configuration.md#fallback-agents), a table of every agent's run with its
model, result (`usage limit`, or `skipped` and why), duration and tokens. Then come why the
stage stopped, for a fix round that merged the base branch the files that had conflicts,
the files changed (including what the merge brought), each command the agent ran and
whether it failed, and for a review the findings.

```text
### NexKit review: done

| Result | Duration | Tokens | Verdict |
|---|---|---|---|
| done | 3m 41s | 1.24M in (1.1M cached), 15.2k out | approve |

**Agent:** review `claude` (`claude-opus-5-5`), claude-code on anthropic
```

The run comment on the issue or pull request names the agent and model of each stage that
ran, in the same format as the summary and the plan comment's profile line: "**Agents:**
triage `claude` (`claude-opus-5-5`) · plan `claude-api` (`claude-opus-5-5`) after `claude`
(`claude-opus-5-5`) hit its usage limit".

**Tokens** are the one usage number, counted the same way for every runtime: input (cached
input included), the cached part of it, and output (reasoning included), over every model
the run used. Each runtime converts what its CLI reports. NexKit shows no cost: runtimes
do not all report one, and on a subscription it is not what you pay. A count is shown only
when it is known, and a total, such as a stage that moved to a fallback agent or a round's
implement and review, only when every run in it has a count; otherwise the cell shows
`N/A`. A table cell with nothing to show is `N/A` too.

**Artifacts,** kept 14 days:

| Artifact | Contains |
|---|---|
| `nexkit-agent`, `nexkit-review` | `prompt.md` (the exact prompt), `context.json`, `result.json`, `changes.patch` (implement and fix), and with `transcript` on: `transcript.jsonl` (the CLI's event stream, including thinking) and `transcript.md` (the conversation in order: the agent's text, the CLI's notices, tool calls and results shortened as in the log, without thinking). A plan with profiles adds `triage/` with the triage call's `prompt.md` and transcripts; its result is in the plan's `result.json`. A fallback agent's transcripts are in `agent-<name>/` (for triage, `triage/agent-<name>/`). |
| `nexkit-checks` | `checks.json`: each check's command, exit code and the end of its output. `results/<check>/`: the files that each failing check names in [`results`](configuration.md#result-files-of-failing-checks), text files redacted, at most 50 MB per round. The next fix round's `agent` job downloads this artifact. |

Each stage that runs uploads its artifact, even when it fails. `report` downloads only the
artifacts of the stages that ran:

| Command | Artifacts `report` reads |
|---|---|
| `plan` | `nexkit-agent` |
| `go`, `fix` (and automatic rounds) | `nexkit-agent`; `nexkit-checks` and `nexkit-review` once a commit was published |
| `review` | `nexkit-checks`, `nexkit-review` |

A skipped stage adds no annotation. An *Artifact not found* error on `report` means a stage
ran but stopped before its upload, for example because it was cancelled; `report` still
sets the statuses and edits the comment without that stage's results.

### What is redacted

Everything NexKit prints or stores from the agent's session passes through one function,
`nexkit/redact.py`: log lines, both transcripts, `result.json`, the run summary and the
output of `setup` in the agent job. The text files that failing checks keep in `results`
pass through it too; binary files are kept as they are. It replaces with `***`:

- the values of every agent secret (see [Secrets](configuration.md#secrets)), of
  `GITHUB_TOKEN`, of the push token and of every environment variable whose name contains
  `TOKEN`, `SECRET`, `KEY`, `PASSWORD` or `CREDENTIAL` (values of 8 characters or more),
  also URL-encoded or base64-encoded;
- Anthropic keys (`sk-ant-…`), OpenAI keys (`sk-…`, `sk-proj-…`, `sk-svcacct-…`,
  `sk-admin-…`), GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`),
  JWTs, `Authorization:` and `Bearer` header values and PEM private key blocks;
- `password=` and `pwd=` values in connection strings and the password in
  `scheme://user:password@host`, in any case.

Git SHAs, UUIDs, hashes and other ordinary text stay as they are. Since `result.json` is
redacted, so are plans, pull request text and review findings published from it. The patch
is never changed.

Redaction is best-effort. It cannot recognise every secret: a value split across lines,
transformed in another way, or not shaped like a known token passes through. GitHub masks
exact secret values in job logs, but not in artifacts. **On a public repository, anyone
signed in to GitHub can read the logs and download the artifacts**, so keep real secrets out
of the agent's environment, and consider `"transcript": false` there, which stops writing
both transcripts. The real protection stays the job separation: the jobs that run agents
have no write token and no credentials besides the agents'.

## Stages

Every prompt contains the same writing guide, because people read what the agents write:
write for a person who knows the project but not this change, in plain English with short
sentences and one idea per bullet, and say what changes in behaviour, and why, before
naming classes, files or methods. The agents return fields, and NexKit adds the headings,
so plans, reviews and pull request descriptions always have the same layout.

**Plan.** Read-only access to the checkout (see [Agents](#agents)). Posted as a comment
marked `<!-- nexkit:plan -->`. The latest plan is the one `/nexkit go` implements. The part a
person approves comes first; the detail for the implementing agent is collapsed:

```markdown
## Plan
One or two sentences: what changes and why.

**Profile: hard**: plan `claude` (`opus`) · …  only with profiles: each stage's agent and model, why, and how to change it

### Since the last plan   only on a re-plan: revised or started over, and what changed
### What changes          up to six bullets about behaviour
### Decisions to check    choices the issue did not settle, each with a short reason
### Risks and limits      what could go wrong or is left out
### Acceptance criteria   one check each, at most about 30 words
### Questions             only when answers would change the work; options, one marked (recommended)
### Suggested split       only when the work is too large for one session; each part says which earlier parts it needs

<details><summary>Implementation notes</summary>
Files, signatures, commands: the detail the implementing agent needs.
</details>
```

Empty sections are left out. The implementing agent, and the reviewer, receive the whole
comment, including the implementation notes, but not the profile lines.

Each acceptance criterion is something the review can verify from what it sees: a test in
the change, or the result of a project check on the candidate commit. Steps that only the
implementing agent can run, such as running a test several times, putting the cause back
or a manual probe, go to the implementation notes instead. For a failure that happens only
at times, the plan prefers a test that reproduces the cause on every run, for example by
forcing the delay or the race, since repeated passes do not show the cause is gone. A
behaviour that no test or check can show is named under the risks, so a person knows to
confirm it.

A re-plan revises the latest plan. The agent gets that plan and the discussion since it,
keeps what nothing asks to change, and applies what the discussion and the note ask for;
where the issue now differs from the plan, the issue wins. A note that asks for a plan
from scratch, such as `/nexkit plan from scratch`, makes it plan again without the latest
plan's choices. *Since the last plan* says which happened and lists each decision,
acceptance criterion or scope item that was added, changed or removed.

**Implement and fix.** Full access to a disposable runner. The prompt contains
the plan, the issue, the discussion from collaborators, and for fix rounds the failing
check output, blocking review findings and collaborators' review comments on the current
commit. It asks the agent to run the checks before it finishes; of a
[slow check](configuration.md#slow-checks), only the tests that cover the change or that
failed, since NexKit runs every check after the round. The agent must return `done` or
`blocked`. A blocked or failed session publishes nothing.

An implement agent that finds no change is needed, such as for a failure that a commit on
the base branch already fixed, changes nothing and writes the finding, with its evidence,
in `no_change`. When the issue, the plan or the discussion says to stop without a change
in that case, it returns `done`: NexKit opens no pull request, closes the issue as
completed, and the run's comment shows the finding (outcome `no_change`). Otherwise it
returns `blocked` with a question whose recommended option is to close the issue, which
[autopilot](#autopilot) answers by closing it. A `no_change` result that changed files is
an error and publishes nothing.

When the failing checks of the last round kept
[result files](configuration.md#result-files-of-failing-checks), `route` puts the id of the
run that recorded them in the decision, and the fix round's `agent` job downloads that
run's `nexkit-checks` artifact to a folder under `$RUNNER_TEMP`, outside the working
tree. The prompt names the folder, and for each failing check its
folder and the files and folders it kept, and asks the agent to read them before it runs
anything. A download that fails, for example because the artifact expired, never fails
the round: the prompt has no such section, and the log of *Collect issue context* says
why.

Every fix round's prompt also lists the pull request's earlier fix rounds, from the state
comment: for each, whether it was automatic or requested by a person, its note, the fix
agent's summary and cause, and the checks that still failed after it, each with its
failure signature (see [Failure handling](#failure-handling)). The agent is told not to
repeat a change that did not help. Besides `summary`, a fix result has `cause`: the cause
of the failures the round addressed and the evidence that shows it, such as trace, log or
output lines, or an empty string, and `cause_outside`: whether that cause lies outside the
code, which only an automatic round after failing checks uses (see [Failing checks the
base branch fixed](#failing-checks-the-base-branch-fixed)). The round's comment and its commit message show a
cause when there is one, and so does the next review's *Previous round* section. NexKit builds the pull request description from the implement result, in this
order: *Summary* (two or three sentences, also the commit message), *What changed* (in
behaviour), *How it is tested*, and, only when needed, *Outside the plan* and *Notes for
the reviewer*. A fix round's summary becomes its commit message.

The new pull request also gets the issue's labels and milestone. The Actions bot sets them,
so no workflow starts for it, and a failure there only warns, since the pull request
already exists. Later rounds leave them to people. Autopilot takes only issues, so its
scope's milestone on the pull request changes nothing.

The pull request of an implement round starts at the base commit the agent started from,
not at the base branch's tip when `publish` runs. So the patch always applies, even when
someone merges into the base branch while the agent works. The pull request is then
behind its base branch, which GitHub shows; `/nexkit fix` brings it up to date and resolves
any conflict (see below). The one exception: when the base branch changed files under
`.github/workflows/` while the agent worked and NexKit pushes with the Actions token, which
may not change workflow files, the pull request starts at the base branch's tip instead,
or the round says that the change does not apply there and to run `/nexkit go` again.

**Conflicts with the base branch.** `/nexkit fix` also brings a pull request that no longer
merges cleanly back in line with its base branch. There is no separate command. By
default a person comments `/nexkit fix`; with `conflicts.auto_resolve`, NexKit starts such
a round by itself (see [Work without a command](#work-without-a-command)).

1. At the start of every fix round, the `agent` job checks with `git merge-tree` whether
   the branch conflicts with the base branch. Only then does it merge the base branch
   into the working tree, without committing. A branch that merges cleanly is not
   merged, so fix rounds stay small, with one exception: an automatic round started
   after failing checks merges the base branch whenever it has commits the branch lacks
   (see [Failing checks the base branch fixed](#failing-checks-the-base-branch-fixed)).
   Branch protection can require up-to-date branches if a repository wants every round
   to include the latest base.
2. The prompt lists the conflicted files. The agent resolves the conflict markers
   together with the round's other feedback; when conflicts are the only problem, the
   round only resolves them. It keeps the intent of both sides when a conflict has one
   right answer: both sides added different lines, or one side renamed something the
   other side uses.
3. When a conflict needs a choice (both sides changed the same behaviour differently,
   or one side deleted code the other side changed) and the plan, the issue, the
   feedback and the note do not settle it, the agent does not guess. It returns
   `blocked`, NexKit pushes nothing, and the round's comment lists each open conflict:
   the file, what each side changed and the question to answer. The person answers with
   a note on the same command, for example `/nexkit fix Keep main's validation in
   Program.cs and add this PR's endpoint after it.` The next round merges again, follows
   the note and resolves the rest itself.
4. `publish` commits the result as a merge commit whose parents are the branch head and
   the exact base commit the agent merged, never the base branch's newer tip. It pushes
   normally, never with force, so the reviewed commits stay. A squash merge of the pull
   request keeps the base branch's history clean.
5. The checks and the review run on the merge commit, as after any fix round. The round's
   comment says that the base branch was merged and which files had conflicts.

**Failing checks the base branch fixed.** A check can fail for a reason that the base
branch fixed after the pull request branched off, such as a flaky test. An automatic fix
round started after failing checks therefore merges the base branch first whenever the
base branch has commits the pull request lacks, conflicts or not. Without a conflict, the
prompt says that the round merged the base branch cleanly and asks the agent to fix only
what still fails on the merged code. When the agent changes nothing, `publish` pushes the
merge alone, an empty patch being valid after a merge, and the next checks run on it.
When the base branch has not moved, nothing is merged, and an empty patch is still the
error "The agent finished without changing any files.", with what the agent found, unless
the agent names a cause outside the code: a package registry, a service or the network
that failed while a check ran, with its evidence in `cause` and `cause_outside` set. Then
the round publishes nothing, and its checks and review run once more on the same commit,
in the same run and within the same automatic round. When they still fail, the round ends
as `recheck_failed`, and a person decides with the agent's finding in the mention. Rounds that a person starts with
`/nexkit fix`, and automatic rounds started only by blocking review findings while the
checks passed, merge the base branch only on a conflict, so they never bring in base
changes that nobody asked for.

**Failing checks the default branch has.** While autopilot's `bug` issue for a workflow
of `merge.after_workflows` is open (see [Autopilot](#autopilot)), every pull request
branched from the default branch holds that failure, and a fix round on each would fix it
again in its own way, or change nothing. So a pull request into the default branch whose
issue is in autopilot's scope gets no automatic fix round when its failing checks are the
default branch's failure. It waits instead:

- *The same failure* means that every failing check runs a command that failed in the
  latest failed attempt the `bug` issue records. A pull request's checks are NexKit's own
  configured checks, not that workflow's jobs and steps, which do not run on NexKit's
  branches without a push token, so NexKit compares commands: the issue's report holds,
  for each failed job, the command of each `run` step whose process failed or ran out of
  time, as the job's log starts that step, and a check is the same when the first line of
  its `run` is that command. A check whose command the workflow does not run, or runs
  written differently, is a failure of the pull request's own and gets the usual round,
  as does the `bug` issue's own pull request and those of its parts. Review findings do
  not count: the round after the wait addresses them too.
- The round ends as `ci_waiting`, uses none of `fix.max_auto_rounds`, and mentions
  nobody. Its comment, the state comment, `nexkit status` (state `waiting`) and the
  autopilot sweep name the `bug` issue. The issue keeps its place within
  `autopilot.max_parallel`.
- The wait ends when the workflow passes on the default branch again, in its newest run
  that passed or failed, which must be the reported run or a newer one, or when the `bug`
  issue is closed. The next sweep then starts the automatic fix round the pull request
  waited for, `fix (auto)`, which counts toward `fix.max_auto_rounds` like any other and
  merges the default branch first (see above). When no automatic round is left by then,
  a person decides.

Conflicts in protected paths stop the round before the agent runs, because the agent may not
change those files. Changes that the base branch brings to protected paths are fine: the
protected-path check compares the result with git's own merge of the two commits, so only
the agent's edits count. GitHub refuses pushes from the default Actions token that change
workflow files, and a merge that brings changes to `.github/workflows/` from the base
branch is such a push. Without a push token (`publish.push_token_secret`), the `agent` job
checks this before the agent runs. A merge that only brings a moved base branch, after
failing checks, is left out: the agent fixes the branch as it is, the round's comment says
which workflow files kept it from merging, and the pull request stays behind the base
branch until a person merges it. A merge the round must make, for a conflict, stops the
round before the agent runs, says that the pull request needs a manual merge or a push
token that may update workflows, and mentions `notify`. No further conflict round starts
on that pull request until a round after it runs, such as the `/nexkit review` after a
person merged the base branch, so a push to the base branch does not stop the same way
again. Such a round, and one that
`publish` refuses for the same reason, count against neither `fix.max_auto_rounds` nor
`conflicts.max_rounds`.

**Review.** Read-only access, like the plan. Returns `approve` or `request_changes`, a
status for each acceptance criterion with the test that covers it and the evidence, and
findings marked `blocking` or `suggestion`. The prompt tells the reviewer not to block on
things the implementer cannot change, such as PR text or CI files.

A criterion is `met` or `unmet` when the diff, its tests or the check results show either,
and `unverifiable` when they show neither, such as a step only the implementing agent could
run. NexKit decides from the criteria as well as the verdict:

- An `unmet` criterion counts as changes requested, whatever the verdict: `nexkit/review`
  fails, and the criterion goes to the next fix round as a blocking finding, so it starts
  an automatic fix round while `fix.max_auto_rounds` remain.
- An approved round with an `unverifiable` criterion never merges by itself, even with
  `merge.auto` on. `nexkit/review` succeeds, so a person can merge by hand; the round's
  comment lists the criteria to confirm, and `notify` mentions its people.

The pull request review starts with what a person must act on:

```markdown
### NexKit review: changes requested
One or two sentences: the main reason for the verdict.

**Things to look at**         findings, blocking first, with file and line
**Previous findings**         what happened to each earlier finding (later rounds)
**Acceptance criteria**       ✅ met, ❌ unmet or ❔ unverifiable, and the covering test
**Checks**: ✅ `test`

<details><summary>Evidence</summary>
file:line references and assertions for each criterion and previous finding
</details>
```

The reviewer judges the code against the same requests the implementing agent followed.
Its prompt has the plan, the issue and the discussion from collaborators on the issue and
the pull request, including the notes on `/nexkit` commands, such as `/nexkit go Use M6`.
It also gets the *Outside the plan* section of the pull request description: what the
implementing agent did that the plan did not ask for, and why. A deviation whose reason
holds, such as a change a collaborator asked for while approving the plan, is not a
finding. A deviation without a reason, or with a reason that the plan, the issue and the
discussion do not support, is.

Every review is a fresh session, so the reviewer stays independent of the agent that wrote
the code. When the pull request already has a NexKit review, the prompt adds a *Previous
round* section: that review's findings, each fix round since then with the note that
started it and the fix agent's summary (which may reject a finding with a reason), and the
diff from the reviewed commit to the current one, next to the full diff. The reviewer
first returns a `resolution` for each earlier finding (`resolved`, `unresolved` or
`rejection_accepted`) with evidence, does not raise an accepted rejection again, treats
changes asked for in a note as in scope, and adds new blocking findings only for real
defects. It also repeats each earlier finding's severity, so the pull request review can
list the resolutions with these marks:

| Mark | Meaning |
|---|---|
| 🛑 Blocking, unresolved | A blocking finding that is still open. It blocks the merge. |
| 💡 Suggestion, still open | A suggestion that is still open. It does not block the merge. |
| ✅ resolved | The code now handles the finding. |
| 🤝 rejection accepted | A fix round rejected the finding, and the reviewer agrees. |

🛑 marks only blocking findings, in *Things to look at* and in *Previous findings*.

After a fix round that merged the base branch, the diff since the reviewed commit
leaves out the changes the merge brought from the base branch: it starts from git's own
merge of the reviewed commit with the merged base commit.

When this run's fix round merged the base branch, the prompt also has a *Merge* section:
the conflicted files and what each side changed in them before the merge. The reviewer
checks that both sides survived in each file. A dropped side is a blocking finding, unless
a person's note asked for it.

## Work without a command

Some events start a run without a `/nexkit` command. `route` turns them into one task for
the `maintain` job, or ends the run at once when the task's setting is off. No agent runs,
and no agent credential is used.

**Closing parent issues** (`close_parent_issues`). When an issue closes and it is a
sub-issue, NexKit looks at its parent's sub-issues:

- When all of them are closed and at least one was completed, NexKit closes the parent as
  completed. Its comment lists each sub-issue and how it closed. It then checks the
  parent's own parent the same way.
- When every sub-issue was closed as not planned or as a duplicate, the parent stays open,
  and NexKit comments that a person should decide.
- A parent that is already closed, or that still has an open sub-issue, stays as it is.
  Reopening a sub-issue does not reopen its parent.
- A parent in another repository is left alone.

A merge by a person closes the issue through GitHub, and the `issues: closed` event starts
the check. When NexKit closes the issue itself after an automatic merge, the Actions token
starts no workflow, so `report` runs the same check in that run.

**Resolving conflicts** (`conflicts.auto_resolve`). When the base branch moves, NexKit
checks its open pull requests into it. GitHub's own mergeability check decides, so no
code is checked out. A pull request that conflicts gets an automatic fix round, shown as
`fix (auto, conflicts)`, with the note to merge the base branch and keep both sides; one
that merges cleanly is left alone. The round then works as any fix round that merges the
base branch, so a conflict that needs a choice ends as `blocked` with its questions.

- **What starts the check.** A push to the default branch, such as a merge by a person,
  starts it through the `push` trigger. A merge by NexKit starts no `push` workflow, so
  `report` checks the other pull requests right after its merge. The hourly schedule
  checks too, which catches anything the other two missed.
- **Refused merge.** When GitHub refuses an automatic merge because the pull request
  conflicts, the round that tried the merge starts a conflict round right away. Its own
  comment still says running while it reports, so it does not count as a running round;
  the limits below still apply.
- **Limits.** Each pull request gets at most `conflicts.max_rounds` such rounds (3 by
  default), apart from `fix.max_auto_rounds`. A round is started once for each base
  commit, so a blocked round is not repeated until the base branch moves again. A pull
  request whose latest round is paused at the usage limit is left alone, and so is one
  with a round running: that round merges the base branch itself when it conflicts, and a
  new run would replace a command waiting behind it. While the implement round that opened
  the pull request runs, the conflict round waits for it (see [One run at a
  time](#one-run-at-a-time)). The next check starts the round if it
  is still needed.
- When the round starts and the branch no longer conflicts, for example because another
  round already merged the base branch, it ends at once without running an agent.

### Autopilot

With `autopilot.enabled` (see [Autopilot](configuration.md#autopilot)), NexKit posts no
commands on its own behalf; it starts them the way it starts automatic rounds, with a
dispatch of the workflow. A *sweep* decides which:

1. It finds the scope: the open issues of the earliest open milestone, oldest first, or
   with `autopilot.scope: null` every open issue, by milestone in the same order and then
   the issues without one, each group oldest first. Issues for CI failures and flaky runs
   go before all others.
2. For each issue, it reads the markers of NexKit's run comments on the issue and its pull
   request, the latest plan, the issue's *blocked by* relationships and whether it
   has sub-issues. From these alone it decides: start `/nexkit plan`, start `/nexkit go`,
   leave it to a run or pull request that carries it on, or leave it to a person.
3. It starts what it decided, in that order, while fewer than `autopilot.max_parallel`
   issues are in progress, and records each start on the issue.

A sweep is a `workflow_dispatch` with the command `autopilot`. Its `number` input is
`autopilot` too, so callers' concurrency groups give every sweep the group
`nexkit-autopilot`: sweeps run one at a time, and GitHub keeps one waiting sweep, which
sees everything a replaced one would have. Issue and milestone events other than a closed
issue run in that group as well, and start a sweep.

- **What starts a sweep.** The end of every NexKit run, except one that started the next
  automatic round itself or waits for a usage limit. A closed issue, whether a person or
  NexKit closed it, which also lets the sweep say once that the scope is done, or with
  `null` that the issue's milestone is done, while the other milestones go on. An issue
  that opens or is reopened. For the milestone scope, an issue that comes into scope with
  a milestone, and a closed milestone, after which the next milestone becomes the scope.
  The hourly schedule, which
  catches anything the others missed. Nothing waits in a loop: with nothing to start, a
  sweep ends.
- **A command starts once.** The record on the issue holds the command, the issue or pull
  request it runs on, and the newest NexKit run comment on both when it started. A run
  that starts posts a new run comment, so the next sweep sees that the command ran.
  Until then, the issue counts as in progress and nothing else starts on it. A command
  whose run has not started after six hours, for example because `route` refused it or
  GitHub dropped it from the queue, starts once more with the same note, and the issue
  waits for a person when that one does not start either.
- **Runs that never end.** A sweep reads the workflow run behind each run comment that
  still says running. When that run has ended, its `report` never wrote the outcome: the
  sweep marks the comment as ended without a report and sets the round's pending statuses
  to errors (the `maintain` job has `statuses: write` for this). A command autopilot
  started, or an automatic round, starts once more; a person's command, or a second run
  that ended so, waits for a person. A run paused at the usage limit that NexKit does not
  resume (`usage_limit.resume: false`, no known reset, or paused more than eight days ago)
  waits for a person, and so does a continued run whose new run has not started after six
  hours. A round that started the next one by itself, such as an automatic fix round,
  whose next one has not run after six hours waits for a person too.
- **People stay in charge.** A person's command, a plan's questions, a blocked or failed
  run and a `needs_human` pull request all stop autopilot on that issue, exactly where they
  stop NexKit without it. Such an issue waits for a person, with the reason that `nexkit
  status` lists under *Needs a person*, `notify` is mentioned once for it, and it leaves its
  place within `max_parallel` to the other issues. So does an issue whose pull request
  needs a person: its round stopped and autopilot cannot answer or split it, it needs a
  human decision to merge, GitHub refused the merge, or it conflicts with the base branch
  and its conflict rounds are used up (or `conflicts.auto_resolve` is off). An issue blocked
  by an open issue outside the scope waits for a person too; one blocked inside the scope
  waits for that issue. With `null`, a blocker in another milestone is inside the scope. A collaborator's comment after the latest plan leads to a new
  plan before `/nexkit go`.
- **Answers and splits.** Each question in a plan or a blocked round has options and the
  number of the one the agent recommends, or 0 for none; a number cannot be copied loosely,
  as an option's text can, and NexKit records the option's text. With `autopilot.answer: "recommended"` and a
  recommendation for every question, autopilot posts the answers in a comment that ends
  with its own marker, and plans again, or starts the blocked command again with the
  answers as its note. The prompts include that comment as the owner's discussion; a
  comment by any other bot never counts. Autopilot answers once in a row: when the plan
  or round that its answers started asks again, with no comment by a person on the issue
  or its pull request since the answers, it waits for a person, so new wording of the
  same question cannot start another run. When a blocked `/nexkit go` found that no change
  is needed and asks one question, its recommended answer only closes the issue, so autopilot closes it as
  completed with the round's finding and starts no other round. A recommended option is
  one NexKit can carry
  out itself, a change to the plan or the code; the prompts never recommend one that
  needs a person to act outside NexKit, such as downloading files or granting access. A plan that is too large becomes sub-issues, in
  order. Each part of the plan names the earlier parts it needs (`depends_on`); NexKit
  keeps only earlier parts, so the parts cannot wait for each other in a cycle, and records
  each as a *blocked by* relationship on GitHub. Parts that need no other part start at
  once. A NexKit pull request of the split issue is closed as superseded. An implement or
  fix agent that ran out of time leads to one re-plan with a note to split the work; a
  second time waits for a person. When the re-plan after a fix round does not split the
  issue and needs nobody, autopilot starts a fix round on the pull request that follows
  the revised plan. Apart from one more try after a transient error (see [Failure
  handling](#failure-handling)), this is the only case where an agent error leads to more
  agent work, and it changes the plan rather than repeating the run. The note asks the
  plan to say why the round did not finish: too much work splits as before, and a failure
  whose cause was not found makes the first part a diagnosis that finds the cause, with
  evidence, before the change. With profiles, triage chooses a diagnosis part's profile
  like any other's; it is not forced onto `hard`.
- **Limits on parts.** A part inherits the time-outs of the issues above it, found through
  GitHub's sub-issue parents: when an issue above it already asked for a split, a part
  that runs out of time waits for a person instead of asking again, however deep it is. A
  part whose title is its parent's title, or starts with it (ignoring case, spaces and
  trailing punctuation), is not created: autopilot plans once more with a note that names
  those titles, and a second such plan waits for a person. Parts go at most three levels
  deep (the issue first split is level 0): an issue at level 3 is not split, after a plan
  that is too large or a time-out, and waits for a person. The limit is fixed, not a
  setting. `nexkit status` shows each of these reasons.
- **CI on the default branch.** Each sweep looks at the completed runs, on the default
  branch, of each workflow in `merge.after_workflows`, and at every attempt of each: the
  latest run, the runs of the last day and the runs a bug issue reports, among the newest
  100. Runs of pull requests do not count, nor runs from a fork, whose head branch can
  have the same name. When the latest run failed in its first attempt, its failed jobs run
  once more; NexKit never runs them a third time, which would hide failures. When a later
  attempt fails too, autopilot opens a `bug` issue in scope (without a milestone with
  `null`), starting with a marker that
  names the workflow, and that issue goes before all others. Its report lists the failed
  jobs and steps of every failed attempt, the failures each job's error annotations name,
  and the last lines of its log, redacted. A round on the issue downloads the evidence of
  the runs its marker records (see
  [Evidence from CI runs](configuration.md#evidence-from-ci-runs)). Each report holds a marker with its commit, so a
  commit is reported once, and a newer failing commit adds a comment to the open issue.
  When a later attempt of a reported run passes, for example after a person ran it again,
  autopilot says which attempt passed in a comment on that issue. Each report also records
  the commands that failed in the run's latest failed attempt, which pull requests that
  fail the same way wait on (see [Failing checks the default branch
  has](#failing-checks-the-default-branch-has)).
- **Flaky runs.** A run whose latest attempt passed after a failed one, and an older run
  that failed when a newer run had already started, which is never run again, are
  recorded under each of their failures, one issue per failure, so an issue stays one
  behaviour. The error annotations of a failed job name its failures (read with
  `checks: read`): test reporters write one per failing test, titled with its name and
  pointing at its file and line. An annotation names a failure by its title, else by its
  file and line; one without either, or that points at the workflow, such as the runner's
  "Process completed with exit code 1.", names nothing. A failed job without an
  annotation that names a failure is named by the job and its first failed step, and an
  attempt that failed before any job did by the workflow. Each failure's issue is `bug`,
  in scope, titled by the failure in plain words, for example 'Test "pages through more
  than twenty entries" in RecordHistory.test.tsx fails at times on main' (see [the title
  forms](configuration.md#autopilot)), starts with its own marker that names the workflow and a key for
  the failure, and is planned right after the `bug` issues for a failing default branch.
  Each record names the commit, the run, and the job, file and line, and message where
  the failure showed in each failed attempt. A later record of the same failure is a
  comment on its issue while that is open; once closed, a new issue opens that links it.
  A run whose failures would open more than five issues, likely one cause such as a
  broken runner, records those on one issue for the run instead; failures that have an
  issue still get their records there. Every bug and flaky-run report ends with a marker
  that lists the run attempts it covers, so each run and attempt is recorded once per
  failure, and a sweep reads no attempt that a marker covers. An issue from NexKit 4.3.0,
  one per workflow with every failure on it, keeps the records it has and gets no new
  ones; the runs it covers are not recorded again. The first sweep after an upgrade looks
  back one day, as every sweep does; sweeps run at least hourly. GitHub may send no
  `workflow_run` event for runs that the Actions token started, so NexKit learns that a
  run finished from the next sweep: after a run, an issue event or the hourly schedule.
- **What autopilot reads.** The plan comment ends with a hidden record of its questions and
  its split, after everything the agent wrote, and a run comment's marker holds the
  questions of a blocked round and whether the agent ran out of time.

### Usage limits

When an agent stops because its account hit a usage limit that resets, in any stage
(triage, plan, implement, fix or review), the stage starts again on the agent's next
[fallback agent](configuration.md#fallback-agents), from the beginning; implement and fix
start from the checkout the first agent found. A fallback without its secret is skipped,
and so is one when less than a minute of the stage's time is left; the run summary says
which. Without one left, the run is paused, not failed, until the earliest reset of the
agents that stopped. The next run starts on the stage's own agent again, so a fallback runs
only while the agent is at its limit.

Each runtime says whether it reports usage limits, and recognises them from its CLI's own
message. `claude-code` does: Claude subscriptions have session and weekly limits.
Fallbacks use the same runtime as the agent, and a runtime that does not report limits
cannot have them.

- The run's comment shows ⏸️, the CLI's message with the reset time, and when NexKit runs the
  command again. `nexkit/checks` and `nexkit/review` stay `pending`. Nothing is published
  from the stage that stopped.
- After the reset, NexKit starts the same command again, once, with the same note. When
  the reset is at most `usage_limit.wait_minutes` away, the run that paused starts a run
  that waits for it: a dispatch of the workflow with the command `resume`, in the
  `nexkit-resume` concurrency group, so it never holds up a command on an issue or pull
  request. Its job may run `wait_minutes` plus 15 minutes, which is why `wait_minutes` is
  at most 330: a GitHub-hosted job ends after six hours. It runs no agent and no
  repository code, looks for due commands every ten minutes until nothing is left within
  its wait, and also resumes runs that pause meanwhile. Resets further away wait for the
  hourly `schedule`, which starts the waiting run once one comes within reach; with
  `wait_minutes: 0`, the schedule resumes commands itself. GitHub runs scheduled workflows
  when it can, often hours apart, which is why NexKit does not rely on it for resets that
  are close.
- A review that stopped after its round published a commit resumes as `/nexkit review`,
  because the commit is already there. A resumed round shows `(resumed)` and does not
  count toward `fix.max_auto_rounds`. If it hits the limit again, it pauses again.
- NexKit recognises the limit only from the CLI's own message, never from what the model
  wrote, so an issue about rate limits cannot pause a run.
- When NexKit cannot read the reset time from the message, it tries again after
  `usage_limit.retry_minutes` (one hour by default), and says so. If that run stops at
  the limit again, it waits for a person, so a limit that does not reset cannot start a
  run again and again.
- Once the command runs again, the paused run's comment shows ⏭️ `continued` instead of
  ⏸️, and says how: resumed in a new run (linked), or replaced by a later command. A new
  command on the same issue or pull request replaces the paused one, which is then not
  resumed. When `/nexkit go` paused at its pull request's review, the issue's comment
  shows ⏭️ once a round starts on the pull request.
- With `usage_limit.resume: false`, the comment still names the limit and the reset
  time, and asks a person to comment the command again after it.

The paused run's comment holds the command, its note and the resume time in its hidden
marker; that is all the state there is. The waiting run and the schedule look at run
comments edited in the last eight days and run no agent.

## Agents

Each stage runs on an agent: a runtime, which is the CLI that NexKit starts, paired with a
provider, which serves the model (see [Agents](configuration.md#agents)). NexKit builds
the prompt, starts the CLI with it, reads the CLI's event stream for the log and the
transcript, and checks the structured result against the stage's schema itself before
anything is published. Everything specific to one CLI lives in `nexkit/runtimes/`.

**What a stage may affect** is the same for every runtime:

| Stage | Access | Claude Code |
|---|---|---|
| triage | none: an empty working directory | no tools |
| plan, review | read the checkout, never change it | `Read`, `Grep`, `Glob` |
| implement, fix | anything on the disposable runner, which holds only a read-only GitHub token | every tool, permissions bypassed, background tasks off |

The levels define what an agent can affect, not a set of tools: a runtime added later
meets each level in its own way and documents how here. A run ends when the agent ends its
turn, so Claude Code runs with background tasks turned off: a check started in the
background would end the run without a result. When Claude Code accepted a result but does
not repeat it at the end, NexKit uses the result it accepted.

**How secrets reach an agent.** The repository's workflow passes the pipeline
`secrets: inherit`. `route` works out which agents each job may run and names their
secrets: for the `agent` job those of its stage's agent and its fallback agents (for a
plan, triage and the plan of every profile, since triage chooses one in the job), for the
`review` job those of the review's agent and its fallback agents. The job reads each
named secret into one of 8 slots, and NexKit gives it back its name. Names that start with
`GITHUB_` or `NEXKIT_` are never read. Within the job, each CLI process gets an
environment without any agent secret, plus the ones its provider names, and setup and
check commands get none. Keys are never put on a command line or in a file.

This filtering limits what each process inherits. It is not a wall inside a job: an agent
that can run commands can read other processes on the runner, so it may see every secret
its job holds. A job holds another agent's secret only when that agent may run in the same
job, such as a plan whose triage runs on another agent, or a fallback agent of the stage.

## Profiles

With `profiles` in the configuration (see the
[configuration reference](configuration.md#profiles)), each issue runs on one profile,
which sets the models and limits of its stages.

**Triage chooses the profile.** Every `/nexkit plan` starts with a triage call in the
`agent` job: one agent session with no access to the repository, run in an empty
directory so the repository's agent settings do not load. It reads the issue, the collaborators'
discussion including the notes on earlier `/nexkit` commands, the note on this command,
each profile with its `when` text, and the previous plan's profile. It returns a profile
name and a reason. It decides in this order:

1. A collaborator's request in the discussion or the note wins; the latest one counts. A
   request in the issue itself does not, because anyone can write the issue.
2. Otherwise a re-plan keeps the previous plan's profile, so the profile does not change
   unless someone asks.
3. Otherwise triage picks the profile whose `when` text fits the issue.

The plan then runs with the profile's settings. Triage has its own run summary, with its
own tokens.

**Triage never stops the plan.** After an error, a timeout or an unknown profile name, the
plan uses the previous plan's profile, or `default_profile` when there is none, and the
plan comment says that triage failed. A profile chosen only as that default is not kept
on the next re-plan: triage chooses again.

**The plan comment shows the profile.** Below the summary: the profile, the models it uses
for plan, implement and review, the reason, and how to change it. To change it, say which
profile in a comment and comment `/nexkit plan`, or comment `/nexkit plan <request>`, for
example `/nexkit plan Use the hard profile`. There is no other way to change a profile, so
the plan comment always shows the profile its rounds run on.

**Rounds use the latest plan's profile.** `route` reads it when the run starts and passes
it, with the profile's settings, to every job of the run. `/nexkit go`, fix rounds,
automatic rounds and `/nexkit review` use it; with no plan, or a plan from before profiles
were configured, they use `default_profile`. `/nexkit go` without a plan does not run
triage. When the plan names a profile that is no longer in the configuration, the round
stops before any job runs and asks for a re-plan; it never falls back to another profile.
A re-plan while the pull request is open applies from the next round.

The job timeouts follow the profile. A stage may run for its `timeout_minutes` once for
each agent of its fallback list, at most 340 minutes; the `agent` job adds
`limits.setup_timeout_minutes`, and `review` adds time for its own steps. For a plan,
`route` does not know the profile yet, so the `agent` job allows for the triage call and
the longest plan of all profiles.

## State

All state is on GitHub: the plan comment (with profiles, a hidden record of its profile on
the line after its marker) and one NexKit comment per run on the issue, the
pull request, its commit statuses, one NexKit comment per round and one NexKit state
comment on the pull request. Run comments only show runs. The state comment, edited in
place, says how many automatic fix rounds were used and holds a hidden JSON record of the
latest feedback (with the run that holds its checks' result files, and the checks that
failed the same way before and after the last fix round), that count, the `bug` issue the
pull request waits for while it does, the last completed
review (its commit and all its findings) and the recent fix rounds (summary, cause, note,
the checks that still failed after it with their failure signatures and, after a merge,
the files that had conflicts). Recorded text is clipped, and only the last five fix
rounds are kept, so the comment and the prompts stay bounded. With autopilot, each issue
it worked on has one record comment, edited in place, that names the command it started
last. Nothing else is stored, so there is nothing to migrate or repair.

Each run comment's hidden marker is machine-readable: the command or round, its trigger,
status (running, success, failure, paused, continued), the outcome (for example `planned`,
`ready`, `merged`, `merge_refused`, `agent_blocked`, `paused`, `ci_waiting` with the `bug`
issue it waits for), the agents that ran each
stage, the reason a person is needed when one is, the resume record of a paused run, and
after an automatic merge each started `merge.after_workflows` run. A round's marker also
holds its commit and whether its checks passed. `nexkit status` reads these markers, not
the text people read, to list the open NexKit work and what each item waits for. NexKit
reads a marker only from a comment by the Actions bot, and only where it ends the comment:
text before it can quote agent output. The latest commit whose checks passed becomes the
next round's `NEXKIT_VERIFIED_SHA` (see [Skipping work that a change cannot
affect](configuration.md#skipping-work-that-a-change-cannot-affect)).

A pull request from NexKit 1.1.0 has a single status comment instead. NexKit reads its
state, leaves it as it is and saves the state in a new state comment.

## Failure handling

| What happened | NexKit does |
|---|---|
| Comment author lacks write access, unknown command, closed issue, PR not opened by NexKit | Replies with the reason; no agent runs. |
| `/nexkit go` while a NexKit PR for the issue is open | Replies: use `/nexkit fix` there or close it. |
| Setup command fails before the agent | Reports the failing command and output on the issue or PR, and runs the command once more, in case a registry or mirror was down; a second failure waits for a person. |
| The agent times out, hits its budget, errors, or returns no result or one that does not fit the stage's schema | Reports it; publishes nothing; no automatic retry. |
| The agent stops on a transient error: an overloaded or failing API, a rate limit that is not a usage limit, a lost connection, or a CLI that exited before the model answered; or the agent job fails as a whole, for example on a lost runner | Reports it, publishes nothing, and runs the same command once more with the same note, shown as `(retry)`; the comment shows ⏭️ until the new run starts. The retry counts against neither `fix.max_auto_rounds` nor `conflicts.max_rounds`. A second such stop waits for a person, and its comment shows both errors. A job a person cancelled is never run again. |
| The agent stops at the account's usage limit | Runs the stage again on its fallback agents, if any. When all of them are at their limits, pauses the run with the earliest reset time; publishes nothing from that stage; runs the command again after the reset, unless `usage_limit.resume` is `false`. |
| An agent's secret is missing or invalid | Reports it as an error and publishes nothing; a fallback agent without its secret is skipped instead. |
| Agent returns `blocked` | Reports its reason and questions; no automatic retry. A person decides, or with `autopilot.answer: "recommended"` autopilot answers when every question has a recommended option, once in a row: when the round its answers started asks again and no person commented since, a person decides. When a blocked `/nexkit go` found that no change is needed, that answer closes the issue with the round's finding, without another round. |
| Triage fails or names an unknown profile | Plans on the previous plan's profile, or `default_profile`; the plan comment says so. |
| The latest plan's profile is no longer configured | Implement, fix and review stop before any job runs and ask for a re-plan. |
| Patch touches a protected path | Rejects the whole change and says which paths. |
| The base branch moved while an implement round was running | Opens the pull request on the commit the agent started from; `/nexkit fix` brings it up to date. |
| The base branch changed workflow files while an implement round was running, and only the default Actions token can push | Opens the pull request on the base branch's tip if the change applies there; otherwise refuses to publish and says to run `/nexkit go` again. |
| The commit the implement agent started from is no longer on the base branch (force push) | Refuses to publish; run `/nexkit go` again. |
| The PR branch moved while a fix round was running | Refuses to publish. An automatic round runs once more on the new head; after a person's round, run `/nexkit fix` again, since the push was probably theirs. |
| The PR conflicts with its base branch | The next fix round merges the base branch and resolves the conflicts; a conflict that needs a choice ends the round as `blocked` with the questions. With `conflicts.auto_resolve`, NexKit starts that round itself, at most `conflicts.max_rounds` times per pull request. |
| The PR conflicts with its base branch in a protected path | Stops before the agent runs; merge the base branch yourself. |
| The merge brings workflow changes and only the default Actions token can push | Checked before the agent runs. A merge that only brings a moved base branch is left out, and the round fixes the branch as it is. A merge needed for a conflict stops the round before its agent: merge the base branch yourself or name a push token that may update workflows in `publish.push_token_secret`. Neither costs an automatic round. |
| GitHub refuses the automatic merge because the base branch was modified meanwhile, for example by another part of a split merging at the same time | Tries the merge again, three times in all, a little later each time. |
| GitHub refuses the automatic merge because the head branch was modified, after a push to the branch since the round checked it | Starts `/nexkit review` on the new head, which checks, reviews and merges it as usual; nobody is mentioned. |
| GitHub refuses the automatic merge because required status checks have not passed yet, such as a CI workflow slower than the NexKit round | For an issue in autopilot's scope, ends the round as `merge_waiting`: each sweep tries the merge again while the head is the one approved, and merges once the checks pass, with everything a merge leads to; a failing check, a new commit, or six hours without the checks passing wait for a person. Otherwise a person merges it. The `maintain` job has `contents: write` for this; it never checks out the repository. |
| GitHub refuses the automatic merge because the branch is behind its base branch, under protection that requires it to be up to date | With `conflicts.auto_resolve`, updates the branch from the base branch on GitHub and starts `/nexkit review` on the new commit, which checks, reviews and merges it as usual, at most `conflicts.max_rounds` times per pull request. GitHub starts no CI for that update when it is made with the Actions token, so required checks from the repository's own workflows run only with `publish.push_token_secret`; without it, the merge waits for them and then for a person. |
| GitHub refuses the automatic merge for another reason (required approvals, a merge method the repository does not allow) | Says why in the round's comment; the pull request waits for a person, and `notify` mentions them. |
| A workflow of `merge.after_workflows` cannot be started | Says why in the round's comment; the merge stays. |
| The issue cannot be closed after an automatic merge | Says why in the round's comment and links the issue; the merge stays. |
| Checks fail or the review requests changes | Starts an automatic fix round while `fix.max_auto_rounds` remain, otherwise asks for a person. After failing checks, that round first merges the base branch when it has moved, so a failure the base branch already fixed costs no agent change. |
| Checks fail only in commands that fail on the default branch, while autopilot's `bug` issue about that is open | Starts no fix round: the round ends as `ci_waiting` and uses no automatic round. Once the workflow passes on the default branch again, or the `bug` issue is closed, a sweep starts the automatic fix round, which merges the default branch first. See [Failing checks the default branch has](#failing-checks-the-default-branch-has). |
| A check fails the same way before and after a fix round | The next automatic fix round is a diagnosis round, shown as `fix (auto, diagnosis)`: the agent names the cause, with evidence, before it changes code, then fixes that cause. Without a cause it changes nothing and ends as `blocked` with a question for a person; a change without a cause is rejected and publishes nothing. |
| The review itself fails | Sets `nexkit/review` to error and says why in the round's comment. A transient error, or a review job that left no result, runs `/nexkit review` once more; otherwise comment `/nexkit review` to retry, after fixing the agent's secrets if it was an authentication error. |
| NexKit itself fails in the `agent` or `review` job | Reports `NexKit stopped on <exception>` like an agent error; the job log has the traceback. |

**Diagnosis rounds.** A fix round that tries one change after another without the cause
spends its time and the automatic rounds on guesses. So when a check fails the same way
in two rounds in a row, NexKit asks for the cause first.

- *The same failure* is the same check with the same failure signature: the first line of
  the check's output that reports a failure (it contains `fail`, `error`, `✘` or `✗`, in
  any case, such as `FAIL`, `AssertionError` or `2 tests failed`), or the last non-empty
  line when none does. UUIDs, hex ids, durations and other numbers are removed and runs of
  whitespace become one space, so the same failure in another run keeps its signature.
- `report` compares the failing checks that a fix round was asked to fix with the checks
  after it. When a check fails with the same signature in both, the state comment records
  it, and `route` makes the next automatic fix round a diagnosis round. A round that a
  person starts with `/nexkit fix` follows that person's note instead, and a conflict
  round only merges the base branch.
- The diagnosis round runs in one session: its prompt lists the repeated failures and
  asks for the cause, with the evidence that shows it (the check output, the
  [result files](configuration.md#result-files-of-failing-checks) the checks kept, the
  code), before any code change, and then for a fix of that cause. The agent writes the
  cause in `cause`.
- A diagnosis result that returns `done` with an empty `cause` and its own code changes is
  rejected as an error, and nothing is published. Changes that a merge of the base branch
  brought, and the resolution of conflicted files, do not count as its own.
- A diagnosis result with an empty `cause` and no changes ends as `blocked`, with what the
  evidence shows as its reason and the agent's questions, or else a question that asks a
  person how to go on. Like any blocked result, it publishes nothing and never starts
  another automatic round. The round itself counts toward `fix.max_auto_rounds` like any
  automatic round, because NexKit counts a round when it starts it; its blocked end uses
  no further round.
- When the round merged a base branch that had moved, and the agent changed nothing
  itself, the merge is published alone, as in any round after failing checks (see
  [Failing checks the base branch fixed](#failing-checks-the-base-branch-fixed)): the
  base branch may have fixed the failure, and the next checks show whether it did.
- Repeated review findings do not start a diagnosis round: the review already tracks them
  through its *Previous round* section, and only checks give a failure signature to
  compare.

Automatic rounds only follow concrete check failures or review findings. Agent errors and
blocked results always stop, apart from what [autopilot](#autopilot) answers or splits, so
model usage is never spent repeating a problem the agent
cannot fix. A run paused at the usage limit is not a retry: it continues the same command
once the limit has reset.

## Security notes

- Only collaborators with write, maintain or admin access can start runs. Prompts include
  issue text from anyone but mark it as untrusted; discussion and review feedback come only
  from collaborators.
- The implement session can run any command on the runner, so it can read the secrets
  its job holds: the credentials of the agents that job may run, such as its fallback
  agents (see [How secrets reach an agent](#agents)), and no other agent's. Treat `/nexkit go` like
  running a script that the issue describes: approve plans only for issues whose content
  you trust.
- With profiles, an issue's text can steer triage towards a more expensive profile, for
  example through prompt injection. Requests in the issue do not count as a person's
  request, and the plan comment shows the profile before anyone comments `/nexkit go`.
- Logs and artifacts can show anything the agent saw. NexKit redacts known secrets (see
  [What is redacted](#what-is-redacted)), but on a public repository treat them as public.
- Workflow and NexKit files (`.github/`, `.nexkit/`) are protected, so a NexKit change
  cannot alter its own pipeline or configuration. The default Actions token also cannot
  push workflow changes.
- Fork pull requests are never NexKit pull requests: commands on them are refused.
- Autopilot starts `/nexkit go` without a person's approval of the plan. It takes only
  issues opened by people with write access, and anyone else's issues stay untouched, so
  an outsider cannot start an implement session by opening or editing an issue. Issues
  that NexKit opens itself, the parts of a split and the issues for CI failures and flaky
  runs, are trusted by a marker at the start of their text; such an issue opened by the
  Actions token is NexKit's, unless another workflow of the repository writes untrusted
  text there.
