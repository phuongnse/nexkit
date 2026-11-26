# How NexKit works

This page says who does what, how a run is built from jobs, what you see during a run, where the
logs go, where the state lives and what to decide about security. Four more pages follow from it:

- [Plan, implement, fix and review](stages.md): the stages of a run, from the plan to the merge.
- [Work NexKit starts by itself](work-without-a-command.md): events that start a run, autopilot
  and usage limits.
- [Agents and profiles](agents-and-profiles.md): which agent runs each stage, and the profiles that
  choose them.
- [When a run fails](failure-handling.md): what NexKit does about each failure, and what it retries.

## Who does what

Three parts are played around NexKit:

- **The person who decides:** the people in [`notify`](merges-and-waits.md#mentions), whom
  NexKit mentions when a run needs a person. They answer the questions that have no
  recommended option, and those asked again after autopilot answered them. They decide
  each merge while `merge.auto` is off, confirm the acceptance criteria that the review
  could not verify, and decide the checklist items that only a person decides. They change what NexKit may not change: the settings in
  `.nexkit/`, the workflow and anything else in `protected_paths`, in pull requests they
  make by hand, which [`/nexkit review`](stages.md#stages) checks and reviews on request.
  [Troubleshooting](troubleshooting.md#when-nexkit-waits-for-a-person) says
  what to do at each stop.
- **A local agent session:** a Claude Code session with NexKit's plugin, working for the
  person who decides. It writes issues and the *blocked by* relationships between them
  with the `request` skill, reads each plan as it is posted and comments the changes it
  needs (see *Changing a plan* under [Stages](stages.md#stages)), reviews pull requests (below),
  watches the work with the `status` skill, and steps in where autopilot stops. It asks
  the person who decides wherever the choice is theirs.
- **NexKit's agents** plan, implement, fix and review inside the pipeline (see [One run,
  ten jobs](#one-run-ten-jobs)). They read the repository's agent instructions, such
  as CLAUDE.md or AGENTS.md, and never hold a token that can write to the repository.

Everything NexKit writes on GitHub, it writes as the repository's
[GitHub App](configuration.md#the-github-app). The reasons are in
[D2](decisions/d2-nexkit-writes-only-as-the-app.md).

NexKit carries the work on by itself wherever these docs say it does: automatic fix and
conflict rounds, runs again after growing waits after a transient error, and with autopilot its plans,
answers and splits, a new plan once a stopped `/nexkit go` has an answer, more starts,
later each time, of a command that never ran or a run that ended without a report, one
more start of a run that a run started but that never started, and the fix rounds and
reviews of a pull request whose merge waits for required checks. So nobody needs to act
until NexKit mentions `notify`, or `nexkit status` lists an item under *Needs a person*.

### Reviewing a pull request

A person reviews a NexKit pull request before merging one that waits for them, and after
NexKit merged one with `merge.auto`, opening an issue for anything wrong. Check that:

- The change does what the issue and its latest plan ask, with the changes commented
  since. What the pull request did outside the plan has a reason in its *Outside the
  plan* section.
- The checks that the default branch requires are green. Rulesets and classic branch
  protection list them:

  ```sh
  gh api repos/{owner}/{repo}/rules/branches/<branch> \
    --jq '.[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context'
  gh api repos/{owner}/{repo}/branches/<branch>/protection/required_status_checks --jq '.contexts[]'
  ```

- Nothing in a protected path changed. NexKit rejects such a change from its agents, so
  this matters when someone pushed to the branch by hand.
- When the change alters what `setup` downloads, such as a new toolchain or a lock file in
  a new place, the [`cache`](configuration.md#caches) entries change with it. They are in a
  protected path, so a person makes that change by hand.
- An approved pull request whose review marked a criterion ❔ unverifiable does not merge
  by itself (see [Review](stages.md#stages)). Check each listed criterion against the diff and the
  check results before merging. When a test could show a criterion, `/nexkit fix Add tests
  that show these criteria, so the review can verify them: <criteria>` lets the review
  verify it. When nothing can show it, note what the diff and the check results do show,
  and the person who decides confirms it.

## One run, ten jobs

Every command starts one run of the reusable workflow
[`pipeline.yml`](../.github/workflows/pipeline.yml). Jobs that do not apply are skipped.
Some other events start a run too; they run only `route` and `maintain` (see
[Work without a command](work-without-a-command.md#work-without-a-command)).

| Job | Runs for | Does |
|---|---|---|
| `route` | every event | Checks the command and the author's write access, reads the config from the default branch, and on a person's pull request that changes `.nexkit/config.json` loads that file from its head through the API, picks the action, and shows that the run started. In an automatic review, leaves out of the check matrix the checks whose [`unaffected_by`](checks.md#checks-that-a-change-cannot-affect) lists every path changed since they passed, from GitHub's comparison. Never executes repository code. |
| `agent` | plan, go, fix | Installs the pinned runtimes the configuration uses; for go and fix, restores the [caches](configuration.md#caches) and runs `setup`; for fix, downloads the [result files](checks.md#result-files-of-failing-checks) that the last round's failing checks kept (`actions: read`); for go and fix, downloads the [evidence of the CI runs](checks.md#evidence-from-ci-runs) the issue names (`actions: read`, `checks: read`). Then one agent session, and at a usage limit the stage's [fallback agents](configuration.md#fallback-agents) in turn; with profiles, a plan starts with a triage call. Uploads the result and a patch. |
| `publish` | plan, go, fix | Posts the plan, or applies the patch, rejects protected paths, commits, pushes and opens the pull request. When the base branch moved while the agent worked, it merges the base branch's tip after the round's commit if the base branch's rules want pull requests up to date, and finds with git whether that tip conflicts with the commit (see [Stages](stages.md#stages)). Never executes repository code. |
| `shards` | go, fix, review, with [sharded checks](checks.md#parallel-checks) | Runs the `count` of each sharded check on the published commit, before `setup`, and gives `verify` the matrix of its shards, each a check of its own with `NEXKIT_SHARD=k/N`. In an automatic review, also leaves out the shards whose `unaffected_by` lists every path changed since they passed with the same N, from git. A `count` that fails or prints no number from 1 to `max` fails its check, and no shard of it runs. |
| `verify` | go, fix, review | Restores the [caches](configuration.md#caches), then runs `setup` and the checks on the published commit, and keeps the result files of the checks that failed. With [`parallel_checks`](checks.md#parallel-checks), one such job runs for each check job, at once. Skipped, with the review, for a published commit that conflicts with the base branch, and by itself when `route` skipped every check. |
| `combine` | go, fix, review, with `parallel_checks` | Joins the results of the check jobs into the one `nexkit-checks` artifact that the review, `report` and the next fix round read, and writes each check job's time to the [run summary](#logs-summary-and-artifacts), from the Actions API. Never checks out or executes repository code. |
| `review` | go, fix, review | A fresh agent session with read-only access reviews the diff against the plan, the discussion, the check results and the previous review round. After NexKit brought an approved pull request up to date with its base branch, it first checks with git whether the approval [carries over](stages.md#stages), and then runs no agent. |
| `report` | every action | Sets commit statuses, posts the review, shows the outcome in the run's comment, starts the next automatic round or asks for a person. With `merge.auto`, merges an approved pull request, closes its issue, deletes its branch when the repository deletes head branches on merge, and starts the `merge.after_workflows` that are due on the base branch. Never checks out or executes repository code. |
| `maintain` | issue and milestone events, pushes to the default branch, finished runs of other workflows, the schedule, the `resume`, `autopilot` and `step` dispatches, `/nexkit trust` | Work that follows an event rather than a command: closing a parent issue, resuming a command paused at the usage limit or one that waits to run again after a transient error, starting a round for a pull request that conflicts with its base branch, stopping a round whose checks a move of the base branch left without a purpose, an [autopilot](work-without-a-command.md#autopilot) sweep, or one issue's autopilot step. Runs no agent, and never checks out or executes repository code. |
| `finish` | a command whose `report` did not finish | When `report` failed, was cancelled or lost its runner, starts a sweep that waits for the run to end and then handles its run comment ([autopilot](work-without-a-command.md#autopilot)) and the conflict round held back while it ran ([Resolving conflicts](work-without-a-command.md#work-without-a-command)). Not for a round NexKit stopped in its checks after the base branch moved: that round's `report` and `finish` do not run, and the next round has started already. Runs no agent, and never checks out or executes repository code. |

The agent does not commit or push. It edits the working tree, and another job turns those edits
into a commit. So no job that runs an agent or repository code holds a token that can write.
[Tokens and secrets](architecture.md#tokens-and-secrets) says which job holds which token and
secret, and why.

Since NexKit writes as an App, what it does starts workflows, as a person's actions do: a
merge starts `push` workflows, a pull request starts `pull_request` workflows, and a run it
starts sends `workflow_run`. That includes NexKit's own workflow. An issue it closes or opens
starts `route` and `maintain`, and a merge starts the conflict check. These runs find their
work done or nothing to do: a parent issue closed already, or a conflict round on the same
base commit, which `route` drops. NexKit's own comments start nothing, since `route` ignores
bots, and its reviews are comments, never *Request changes*.

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
(see [Usage limits](work-without-a-command.md#usage-limits)), and its command comment keeps 👀. ⏭️ `continued`
replaces ⏸️ once a later run carries the command on.

### When a person is needed

`report` decides from the run's outcome and the agents' structured output whether a
person is needed. It records the reason in the run comment's hidden marker, and with
`notify` set it mentions those people in a new comment (see
[Mentions](merges-and-waits.md#mentions)). The reason says what to do, why NexKit does not do
it itself, and where to look, such as every question to answer, each checklist item to
decide with what the review found, or, for a refused merge, the rule and the files a code
owner reviews (see [What a mention
says](troubleshooting.md#what-a-mention-says)):

| Run result | A person is needed |
|---|---|
| Plan posted, and it has questions or says the issue is too large | yes, unless [autopilot](work-without-a-command.md#autopilot) answers it with an option that does more than close the issue, or splits it, or plans again for parts it lacks, for fewer than its more than ten parts, or for parts titled like the issue |
| Plan posted without either | no |
| The agent returned `blocked` | yes, unless autopilot answers it |
| The agent failed or errored, publishing failed, or the review did not complete | yes, unless the error was transient and the command runs again after its wait of [`retry.wait_minutes`](merges-and-waits.md#transient-errors), or autopilot asks for a split after an implement or fix agent ran out of time (see [limits on parts](work-without-a-command.md#autopilot)) |
| Checks fail only as the default branch does while autopilot's `bug` issue about that is open (`ci_waiting`) | no: an automatic fix round starts once the default branch passes (see *Failing checks the default branch has* under [Stages](stages.md#stages)) |
| Checks or review still fail and no automatic fix rounds remain | yes |
| Checks or review still fail after a fix round found nothing to change for a cause outside the code and ran them again | yes |
| Ready, with `merge.auto` off | yes |
| Ready, with an acceptance criterion the review could not verify | yes, also with `merge.auto` on |
| Ready, with a checklist item not met that a person decides | yes, also with `merge.auto` on; their approval merges |
| GitHub refused the automatic merge | yes, unless the refusal passes by itself: see [Failure handling](failure-handling.md#failure-handling). The person's part, such as a code owner's approval, is enough: NexKit merges after an approval, or with autopilot once GitHub reports the pull request as mergeable (see [Automatic merge](merges-and-waits.md#automatic-merge)) |
| Merged, or an automatic fix or conflict round started | no |
| The round's commit conflicts with the base branch, and no conflict round is left to start (`conflicting`) | yes |
| Paused at the usage limit, resuming by itself | no |
| Paused, and NexKit will not resume it (`usage_limit.resume: false`, or a second pause with an unknown reset time) | yes |

With autopilot on, its sweeps come to the same answers for the issues in scope, and say
what else waits for a person: an issue whose pull request needs one, an author without
write access, a blocker outside the scope, a command that did not run after its more
starts. When an issue starts to wait for a person and nobody was mentioned since its latest
run, the sweep mentions `notify` once, on the issue, unless the issue closed while the
sweep ran. `nexkit status` lists every such issue under *Needs a person* with the same
reason.

### One run at a time

Runs for one work item run one at a time, in order. A work item is an issue together with
its NexKit pull request (`nexkit/issue-<n>`), or a person's pull request on its own. The
installed workflow's first job, `item`, runs `nexkit item`, which names the item of the
event: the issue's number for the issue, its NexKit pull request and a dispatch that names
either, and the pull request's own number for a person's pull request, or one from a fork,
which cannot join an issue's queue by naming its branch like NexKit's. The `nexkit` job
puts its run in the group `nexkit-<item>`. If GitHub's API fails while `item` reads a pull
request, the item is the number itself.

Only commands, *Request changes* reviews, dispatches and the short run for a closed issue
wait in this queue; other comments do not. GitHub keeps one waiting run, so a command
posted while another waits replaces it, also one on the issue that waits behind a round of
its pull request. A command that never got 👀 did not run; post it again. How the engine's
host will take such a command from its comment instead is in [Hosts](engine.md#hosts).

Four kinds of run keep a group of their own, so they never replace a command that waits:
sweeps and the issue and milestone events that bring issues into scope share
`nexkit-autopilot`, the run that waits for paused commands is `nexkit-resume`, an approval
of a pull request is `nexkit-approved-<pr>`, and each other event, such as the schedule, a
push or a finished workflow run, forms a group per event.

A pull request runs one NexKit round at a time. Its rounds (fix, review, automatic and
conflict rounds) share the item's queue with the issue's commands, and the `/nexkit go` run
that opened it is one of its rounds too, until that run has checked, reviewed and, with
`merge.auto`, merged it. The queue already orders them, and NexKit orders them itself as a
backstop, for workflows installed before the `item` job, whose runs may overlap:

- A conflict round does not start while the implement round runs. When the implement
  round ends and the pull request conflicts, that round starts the conflict round, on the
  branch as it left it: after a refused merge, or when it ends ready, with findings and no
  automatic round left, or without a review.
- A person's command or *Request changes* review on the pull request meanwhile gets a
  reply that the implement round still runs; comment it again once it ends.
- The rounds that a run starts as it ends, such as an automatic fix round or the one more
  try, run after it in the item's queue.
- A conflict round does not start while any other round of the pull request runs either.
  When the base branch moved meanwhile, the sweep at the end of that round starts it,
  however the round ended (see *Resolving conflicts* under [Work without a
  command](work-without-a-command.md#work-without-a-command)). The one exception is a round in its checks, whose
  checks can no longer lead to a merge: NexKit stops it and starts the next round at once
  (see *Stopping a round in its checks* there).
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

With [`parallel_checks`](checks.md#parallel-checks), the `combine` job's summary
shows the round's checks as a whole, from the Actions API and each job's `checks.json`: a
line with the run's time so far, how long it waited before its first job started, and the
time from the first check job's start to the last one's end; then one row per check job,
longest first, with its result, its time and how much of it its checks ran (without
`setup` and the cache restore). A job that ended last without being the first row, as
after waiting for a runner, is marked with how late it started. A job that failed, was
cancelled or uploaded no result is listed too, with what happened; a job whose checks `route` all skipped ran on no runner and has no row. When
the Actions API cannot be read, the summary says why and the times are `N/A`.

```text
### NexKit check jobs

The run so far: **29m 07s**; waited **11m 51s** before its first job started; check jobs: **15m 50s** from the first start to the last end.

| Check job | Result | Time | Checks |
|---|---|---|---|
| `e2e-4` | passed | 15m 13s | 14m 02s |
| `integration-5` (ended last: started 6m 40s late) | passed | 9m 10s | 7m 31s |
| `test-2` | 1 of 3 failed | 8m 56s | 6m 50s |
| `smoke-1` | cancelled, no result | 5m 49s | N/A |
```

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
| `nexkit-agent`, `nexkit-review` | `prompt.md` (the exact prompt), `context.json`, `texts/` (each whole text the prompt [shortened or left out](agents-and-profiles.md#agents), and the whole kept output of each failing check it shows), `result.json`, `run.json` (the [runner's](engine.md#runners) result, built from the redacted `result.json`), `changes.patch` (implement and fix), and with `transcript` on: `transcript.jsonl` (the CLI's event stream, including thinking) and `transcript.md` (the conversation in order: the agent's text, the CLI's notices, tool calls and results shortened as in the log, without thinking). A plan with profiles adds `triage/` with the triage call's `prompt.md` and transcripts; its result is in the plan's `result.json`, and the runner's in `triage/run.json`. A fallback agent's transcripts are in `agent-<name>/` (for triage, `triage/agent-<name>/`). |
| `nexkit-checks` | `checks.json`: each check's command, exit code, the last 12,000 characters of its output (`output`) and the first 20 lines before them that report a failure (`failure_lines`, when there are any). `results/<check>/`: the files that each failing check names in [`results`](checks.md#result-files-of-failing-checks), text files redacted, at most 50 MB per round; with [`parallel_checks`](checks.md#parallel-checks), at most the larger of each job's even share of 50 MB and 25 MB per job. The next fix round's `agent` job downloads this artifact. With [`parallel_checks`](checks.md#parallel-checks), each check job uploads its own part as `nexkit-checks-<job>`, with `checked.json`, the commit it checked, and `combine` joins them into this one. |

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
`nexkit/redact.py`: log lines, both transcripts, `result.json`, `run.json`, the run summary and the
output of `setup` in the agent job. The text files that failing checks keep in `results`
pass through it too; binary files are kept as they are. It replaces with `***`:

- the values of every agent secret (see [Secrets](configuration.md#secrets)), of
  `GITHUB_TOKEN`, which holds the App's token in the jobs that write, and of every
  environment variable whose name contains
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

## State

All state is on GitHub: the plan comment (with profiles, a hidden record of its profile on
the line after its marker) and one NexKit comment per run on the issue, the
pull request, its commit statuses, one NexKit comment per round and one NexKit state
comment on the pull request. Run comments only show runs. The state comment, edited in
place, says how many automatic fix rounds were used and holds a hidden JSON record of the
latest feedback (with the run that holds its checks' result files, and the checks that
failed the same way before and after the last fix round), that count, the `bug` issue the
pull request waits for while it does, the last completed
review (its commit and up to 30 of its findings: past 30, the blocking ones first, then the
others in the reviewer's order, with how many were left out) and the recent fix rounds (summary, cause, note,
the checks that still failed after it with their failure signatures and, after a merge,
the files that had conflicts). Recorded text is clipped, and only the last five fix
rounds are kept, so the comment and the prompts stay bounded. Of a passed check, the
record keeps its name, command and exit code, not its output, which no prompt reads. The
comment always fits GitHub's 65,536 characters: when the record would not, NexKit first
leaves out any passed check's output an older release recorded, then shortens, in this
order and each to the most that fits, the failing checks' outputs (keeping their end, down to 2,000
characters each, with the lines that report a failure from what it cut joining their
failure lines), the findings other than blocking ones (keeping their start, down to 300
characters), the failure lines, the outputs to nothing, the blocking findings, and last the
fix rounds. Each failing check's failure signature is recorded first, so a shortened
output still compares as before, and the fix prompt says that NexKit kept less of the
output. With autopilot, each issue
it worked on has one record comment, edited in place, that names the command it started
last. Nothing else is stored, so there is nothing to migrate or repair.

Each run comment's hidden marker is machine-readable: the command or round, its trigger,
status (running, success, failure, paused, continued), the outcome (for example `planned`,
`ready`, `merged`, `merge_refused`, `agent_blocked`, `paused`, `ci_waiting` with the `bug`
issue it waits for, `held` with what it kept back), the agents that ran each
stage, the reason a person is needed when one is, the resume record of a paused run, and
after an automatic merge each started `merge.after_workflows` run. A round's marker also
holds its commit, whether its checks passed, and which of them passed on that commit,
each with a digest of its `run` command, and a digest of its `setup` commands. `nexkit status` reads these markers, not
the text people read, to list the open NexKit work and what each item waits for. NexKit
reads a marker only where [Comments and markers](security.md#comments-and-markers) says. The
latest commit whose checks passed becomes the next round's `NEXKIT_VERIFIED_SHA`, and for each check the latest commit on which it passed
with its current command and the current `setup` becomes its `NEXKIT_CHECK_VERIFIED_SHA`
(see [Skipping work that a change cannot
affect](checks.md#skipping-work-that-a-change-cannot-affect)). In an automatic
review, `route` compares the round's commit with that commit to skip the checks that a
change cannot affect.

The markers are NexKit's own format and can change between releases. Read them through
`nexkit status --json`, whose `attention` field gives the reason an item needs a person.
Names a person meets in run comments and in that output:

| Name | Meaning |
|---|---|
| `too_large` | The plan says the work does not fit one agent session, and suggests a split when it can. |
| `conflicting` | The round's commit conflicts with the base branch, so its checks and review did not run, and NexKit started no conflict round, because `conflicts.max_rounds` are used up. |
| `branch_updated` | GitHub refused the automatic merge because the branch was behind its base branch; NexKit merged the base branch into it and started `/nexkit review` on the new commit. |
| `carried` | The round carried over the approval of that commit, after a branch update that left the pull request's change as it was. |
| `head_moved` | GitHub refused the automatic merge because the branch moved after the round checked it; NexKit started `/nexkit review` on the new head. When GitHub says it moved and the pull request still shows the head the round checked, or NexKit cannot read the head, the outcome is `merge_refused` and a person decides. |
| `superseded` | NexKit stopped the round in its checks because the base branch moved so that they could not lead to a merge, and started the conflict round, or an update of the branch and `/nexkit review`. See *Stopping a round in its checks* under [Work without a command](work-without-a-command.md#work-without-a-command). |
| `stop_failed` | NexKit stopped the round in its checks for the same reason, but the next round could not start; a person starts it. |
| `merge_waiting` | The automatic merge waits for required checks; see [Failure handling](failure-handling.md#failure-handling). |
| `recheck_failed` | The checks failed again after a fix round named a cause outside the code; see *Failing checks the base branch fixed* under [Stages](stages.md#stages). |
| `already_on_base` | The base branch already holds the pull request's change, so it changes no file against it. NexKit closed the pull request without merging and its issue as completed; the round's marker names the commit that brought the change when NexKit can tell one. See *A pull request brought up to date that changes nothing* under [Stages](stages.md#stages). |
| `issue_closed` | The issue closed while its plan or `/nexkit go` ran, so NexKit published nothing. |
| `merge_issue_closed` | Checks and review approved the pull request, but its issue is closed, so NexKit did not merge it; a person merges or closes it. |
| `pull_closed` | Checks and review approved the pull request, but a person had already merged or closed it, so NexKit did not merge it. Nobody is mentioned. Reopened, it waits for a person's approval or `/nexkit review`. |
| `agent_untrusted` | Someone without write access edited a vouched issue, so a round NexKit started ran no agent. A new `/nexkit trust` starts it again; it counts against neither `fix.max_auto_rounds` nor `conflicts.max_rounds`. See [Vouching for an issue](autopilot.md#vouching-for-an-issue). |
| `merge_untrusted` | The same edit stopped a merge. A new `/nexkit trust` starts `/nexkit review`, which merges as usual. |
| `person_reviewed` | `/nexkit review` checked and reviewed a person's pull request. NexKit changes nothing on it, whatever the round found. See *A person's pull request* under [Stages](stages.md#stages). |
| `no_change` | An implement round found that no change is needed. The issue closes with its finding, at once or through autopilot's answer (see [Stages](stages.md#stages)). A plan's finding closes it through autopilot. |
| `attention` | The reason a person is needed, when one is. |
| `conflicts`, `base_sha` | The round was a conflict round, started for that commit of the base branch. |

A pull request from NexKit 1.1.0 has a single status comment instead. NexKit reads its
state, leaves it as it is and saves the state in a new state comment.

The reasons are in [D1](decisions/d1-state-lives-only-on-github.md).

## Security notes

What NexKit trusts, and the rule and test for each input it does not, are in [Security](security.md).
These are the choices that remain with whoever runs NexKit on a repository.

- Treat `/nexkit go` like running a script that the issue describes: approve plans only for issues
  whose content you trust, since the implement session can read the secrets its job holds (see
  [What the rules do not stop](security.md#what-the-rules-do-not-stop)).
- With [profiles](configuration.md#profiles), an issue's text can steer triage towards a more
  expensive profile. Requests in the issue do not count as a person's request, and the plan comment
  shows the profile before anyone comments `/nexkit go`.
- Logs and artifacts can show anything the agent saw, and redaction is best-effort (see
  [What is redacted](#what-is-redacted)). On a public repository treat them as public, and consider
  `transcript` and `log.tool_output` ([Logs and
  transcripts](configuration.md#logs-and-transcripts)).
- Add to `protected_paths` the paths no agent may change, beyond `.github/` and `.nexkit/`, which
  are required (see the [configuration reference](configuration.md)).
- List in `autopilot.trusted_workflows` only workflows whose output you would act on unread (see
  [Issues from a workflow](autopilot.md#issues-from-a-workflow)), and vouch for any other issue
  that autopilot should take with [`/nexkit trust`](autopilot.md#vouching-for-an-issue).

The reasons are in [D4](decisions/d4-outside-text-is-untrusted.md) and
[D5](decisions/d5-agents-cannot-change-the-pipeline.md).
