# How NexKit works

## One run, six jobs

Every command starts one run of the reusable workflow
[`pipeline.yml`](../.github/workflows/pipeline.yml). Jobs that do not apply are skipped.

| Job | Runs for | Token permissions | Claude credential | Does |
|---|---|---|---|---|
| `route` | every event | read; comments, reactions, statuses | no | Checks the command and the author's write access, reads the config from the default branch, picks the action, and shows that the run started. Never executes repository code. |
| `agent` | plan, go, fix | read only | yes | Installs Claude Code, runs `setup`, then one Claude session. Uploads the result and a patch. |
| `publish` | plan, go, fix | write | no | Posts the plan, or applies the patch, rejects protected paths, commits, pushes and opens the pull request. Never executes repository code. |
| `verify` | go, fix, review | read only | no | Runs `setup` and the checks on the published commit. |
| `review` | go, fix, review | read only | yes | A fresh Claude session with read-only tools reviews the diff against the plan, the discussion, the check results and the previous review round. |
| `report` | every action | write | no | Sets commit statuses, posts the review, shows the outcome in the run's comment, starts the next automatic round or asks for a person. |

The agent does not commit or push. It edits the working tree, and NexKit turns those edits
into a commit in a different job. As a result:

- The session that runs Claude never holds a token that can write to the repository.
- Jobs that hold write tokens never run code from the repository or from the agent.
- Checks run in a job with no secrets at all.

## What you see during a run

GitHub cannot show a live run inside a comment, and only `route` and `report` hold write
tokens, so NexKit updates the issue or pull request when a run starts and when it ends.

Every run gets its own NexKit comment, posted by `route` when the run starts, so it appears
right after the command or review that started it. `report` edits that comment with the
outcome. Comments of earlier runs keep their final state.

| When | Command on an issue (`plan`, `go`) | Round on a pull request (`fix`, `review`, automatic, *Request changes*) |
|---|---|---|
| Start (`route`) | 👀 on the command comment. A new NexKit comment names the command and shows ⏳ with a link to the run. | 👀 on the command comment. A new NexKit comment shows the round number, its trigger and ⏳ with a link to the run. `nexkit/checks` and `nexkit/review` turn `pending` on the current commit and link to the run. |
| End (`report`) | The same comment shows ✅ or ❌, the run log and the result: the plan, the pull request and its outcome, or why the run stopped. | The same comment shows ✅ or ❌, the run log, the round's commit, checks, review verdict and cost, and the next step. Statuses are set on the new commit; a commit the round did not check gets its earlier statuses back. |

✅ means the run did its work, even when the checks fail or the review asks for changes
(the round's columns show that). ❌ means it stopped on an error. A run started by a
comment ends with 🚀 or 😕 on that comment. Runs started by a dispatch or by a
*Request changes* review have no comment to react to.

A hidden marker in each run comment holds the run's URL, so `report` edits the comment its
own run started. If that comment was deleted, `report` posts a new one.

## Logs, summary and artifacts

The run link opens the Actions run. The `agent` and `review` jobs show what Claude does
while it works.

**Log.** One line `· text` (first 240 characters) per message from Claude. Each tool call
is a collapsible group, printed when its result arrives:

```text
[03:12 #14] ▸ Bash scripts/e2e.sh --timeout 600       <- group title; expand for the rest
  Input:
  command: scripts/e2e.sh --timeout 600
  description: Run the end-to-end tests

  Result: error
  <first and last 20 lines of the output, at most 4,000 characters>
Warning: Bash failed: 2 tests failed
```

The title has the time since Claude started, the turn number, the tool and a short
detail. Inside are the input (long values, such as a file's new content, are shortened the
same way as the output), whether the result is an error, and the output. A failed tool
call also adds a warning annotation with its first line. A call still running when the
session ends shows `no result`. Tool output is printed between `::stop-commands::` markers
with a random token, so text from the repository or the agent cannot set outputs, add
masks or create annotations. Claude's thinking is never printed. With
`log.tool_output: "none"` the log has one line per message and per tool call instead
(`▸ Tool detail`, 200 characters), as in earlier releases.

**Run summary.** At the end of the `agent` and `review` jobs, the run's summary page shows
the stage and a one-row table of its result (done, blocked or error), turns, duration and
cost, plus the verdict for a review; `-` marks a missing value. Below it are why the stage
stopped, for a fix round that merged the base branch the files that had conflicts, the
files changed (including what the merge brought), each command Claude ran and whether it failed, and for a review
the findings.

```text
### NexKit review: done

| Result | Turns | Duration | Cost | Verdict |
|---|---|---|---|---|
| done | 12 | 3m 41s | $0.84 | approve |
```

**Artifacts,** kept 14 days:

| Artifact | Contains |
|---|---|
| `nexkit-agent`, `nexkit-review` | `prompt.md` (the exact prompt), `context.json`, `result.json`, `changes.patch` (implement and fix), and with `transcript` on: `transcript.jsonl` (Claude's event stream, including thinking) and `transcript.md` (the conversation in order: Claude's text, tool calls and results shortened as in the log, without thinking). |
| `nexkit-checks` | `checks.json`: each check's command, exit code and the end of its output. |

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
output of `setup` in the agent job, which runs with the Claude credential in its
environment. It replaces with `***`:

- the values of `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`,
  `NEXKIT_PUSH_TOKEN` and of every environment variable whose name contains `TOKEN`,
  `SECRET`, `KEY`, `PASSWORD` or `CREDENTIAL` (values of 8 characters or more), also
  URL-encoded or base64-encoded;
- Anthropic keys (`sk-ant-…`), GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
  `github_pat_`), JWTs, `Authorization:` and `Bearer` header values and PEM private key
  blocks;
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
both transcripts. The real protection stays the job separation: the jobs that run Claude
have no write token and no credentials besides Claude's.

## Stages

Every prompt contains the same writing guide, because people read what the agents write:
write for a person who knows the project but not this change, in plain English with short
sentences and one idea per bullet, and say what changes in behaviour, and why, before
naming classes, files or methods. The agents return fields, and NexKit adds the headings,
so plans, reviews and pull request descriptions always have the same layout.

**Plan.** Read-only tools (`Read`, `Grep`, `Glob`). Posted as a comment marked
`<!-- nexkit:plan -->`. The latest plan is the one `/nexkit go` implements. The part a
person approves comes first; the detail for the implementing agent is collapsed:

```markdown
## Plan
One or two sentences: what changes and why.

### What changes          up to six bullets about behaviour
### Decisions to check    choices the issue did not settle, each with a short reason
### Risks and limits      what could go wrong or is left out
### Acceptance criteria   one check each, at most about 30 words
### Questions             only when answers would change the work
### Suggested split       only when the work is too large for one session

<details><summary>Implementation notes</summary>
Files, signatures, commands: the detail the implementing agent needs.
</details>
```

Empty sections are left out. The implementing agent, and the reviewer, receive the whole
comment, including the implementation notes. Plans posted by earlier releases are read the
same way.

**Implement and fix.** Full Claude Code tools on a disposable runner. The prompt contains
the plan, the issue, the discussion from collaborators, and for fix rounds the failing
check output, blocking review findings and collaborators' review comments on the current
commit. The agent must return `done` or `blocked`. A blocked or failed session publishes
nothing. NexKit builds the pull request description from the implement result, in this
order: *Summary* (two or three sentences, also the commit message), *What changed* (in
behaviour), *How it is tested*, and, only when needed, *Outside the plan* and *Notes for
the reviewer*. A fix round's summary becomes its commit message.

The pull request of an implement round starts at the base commit the agent started from,
not at the base branch's tip when `publish` runs. So the patch always applies, even when
someone merges into the base branch while the agent works. The pull request is then
behind its base branch, which GitHub shows; `/nexkit fix` brings it up to date and resolves
any conflict (see below).

**Conflicts with the base branch.** `/nexkit fix` also brings a pull request that no longer
merges cleanly back in line with its base branch. There is no separate command, and NexKit
does not notice by itself when a pull request starts to conflict: a person comments
`/nexkit fix`.

1. At the start of every fix round, the `agent` job checks with `git merge-tree` whether
   the branch conflicts with the base branch. Only then does it merge the base branch
   into the working tree, without committing. A branch that merges cleanly is not
   merged, so fix rounds stay small. Branch protection can require up-to-date branches
   if a repository wants every round to include the latest base.
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

Conflicts in protected paths stop the round before Claude runs, because the agent may not
change those files. Changes that the base branch brings to protected paths are fine: the
protected-path check compares the result with git's own merge of the two commits, so only
the agent's edits count. GitHub refuses pushes from the default Actions token that change
workflow files, and a merge that brings changes to `.github/workflows/` from the base
branch is such a push. Without a `NEXKIT_PUSH_TOKEN`, NexKit stops before pushing and says
that the pull request needs a manual merge or a push token that may update workflows.

**Review.** Read-only tools. Returns `approve` or `request_changes`, a verdict for each
acceptance criterion with the test that covers it and the evidence, and findings marked
`blocking` or `suggestion`. The prompt tells the reviewer not to block on things the
implementer cannot change, such as PR text or CI files. The pull request review starts
with what a person must act on:

```markdown
### NexKit review: changes requested
One or two sentences: the main reason for the verdict.

**Things to look at**         findings, blocking first, with file and line
**Previous findings**         what happened to each earlier finding (later rounds)
**Acceptance criteria**       ✅ or ❌ and the covering test, one line each
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

## State

All state is on GitHub: the plan comment and one NexKit comment per run on the issue, the
pull request, its commit statuses, one NexKit comment per round and one NexKit state
comment on the pull request. Run comments only show runs. The state comment, edited in
place, says how many automatic fix rounds were used and holds a hidden JSON record of the
latest feedback, that count, the last completed review (its commit and all its findings)
and the recent fix rounds (summary, note and, after a merge, the files that had
conflicts). Recorded text is clipped so the comment and
the prompts stay bounded. Nothing else is stored, so there is nothing to migrate or repair.

Issues and pull requests from NexKit 1.1.0 have a single status comment. NexKit leaves it
as it is. On a pull request it still reads that comment's state, then saves the state in a
new state comment.

## Failure handling

| What happened | NexKit does |
|---|---|
| Comment author lacks write access, unknown command, closed issue, PR not opened by NexKit | Replies with the reason; no agent runs. |
| `/nexkit go` while a NexKit PR for the issue is open | Replies: use `/nexkit fix` there or close it. |
| Setup command fails before the agent | Reports the failing command and output on the issue or PR. |
| Claude times out, hits its budget, errors, or returns no result | Reports it; publishes nothing; no automatic retry. |
| Agent returns `blocked` | Reports its reason; no automatic retry. A person decides. |
| Patch touches a protected path | Rejects the whole change and says which paths. |
| The base branch moved while an implement round was running | Opens the pull request on the commit the agent started from; `/nexkit fix` brings it up to date. |
| The commit the implement agent started from is no longer on the base branch (force push) | Refuses to publish; run `/nexkit go` again. |
| The PR branch moved while a fix round was running | Refuses to publish; run `/nexkit fix` again. |
| The PR conflicts with its base branch | The next fix round merges the base branch and resolves the conflicts; a conflict that needs a choice ends the round as `blocked` with the questions. |
| The PR conflicts with its base branch in a protected path | Stops before Claude runs; merge the base branch yourself. |
| The merge brings workflow changes and only the default Actions token can push | Refuses to publish; merge the base branch yourself or add a `NEXKIT_PUSH_TOKEN` that may update workflows. |
| Checks fail or the review requests changes | Starts an automatic fix round while `max_auto_fixes` remain, otherwise asks for a person. |
| The review itself fails | Sets `nexkit/review` to error; comment `/nexkit review` to retry. |

Automatic rounds only follow concrete check failures or review findings. Agent errors and
blocked results always stop, so model usage is never spent repeating a problem the agent
cannot fix.

## Security notes

- Only collaborators with write, maintain or admin access can start runs. Prompts include
  issue text from anyone but mark it as untrusted; discussion and review feedback come only
  from collaborators.
- The implement session can run any command on the runner, and Claude Code's credential is
  in its environment. Treat `/nexkit go` like running a script that the issue describes:
  approve plans only for issues whose content you trust.
- Logs and artifacts can show anything the agent saw. NexKit redacts known secrets (see
  [What is redacted](#what-is-redacted)), but on a public repository treat them as public.
- Workflow and NexKit files (`.github/`, `.nexkit/`) are protected, so a NexKit change
  cannot alter its own pipeline or configuration. The default Actions token also cannot
  push workflow changes.
- Fork pull requests are never NexKit pull requests: commands on them are refused.
