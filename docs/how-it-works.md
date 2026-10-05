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
| `review` | go, fix, review | read only | yes | A fresh Claude session with read-only tools reviews the diff against the plan, the check results and the previous review round. |
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

## Stages

**Plan.** Read-only tools (`Read`, `Grep`, `Glob`). Produces a summary, approach,
acceptance criteria, questions and, for work that is too large, a suggested split into
smaller issues. Posted as a comment marked `<!-- nexkit:plan -->`. The latest plan is the
one `/nexkit go` implements.

**Implement and fix.** Full Claude Code tools on a disposable runner. The prompt contains
the plan, the issue, the discussion from collaborators, and for fix rounds the failing
check output, blocking review findings and collaborators' review comments on the current
commit. The agent must return `done` or `blocked`. A blocked or failed session publishes
nothing.

**Review.** Read-only tools. Returns `approve` or `request_changes`, a verdict for each
acceptance criterion, and findings marked `blocking` or `suggestion`. The prompt tells the
reviewer not to block on things the implementer cannot change, such as PR text or CI files.

Every review is a fresh session, so the reviewer stays independent of the agent that wrote
the code. When the pull request already has a NexKit review, the prompt adds a *Previous
round* section: that review's findings, each fix round since then with the note that
started it and the fix agent's summary (which may reject a finding with a reason), and the
diff from the reviewed commit to the current one, next to the full diff. The reviewer
first returns a `resolution` for each earlier finding (`resolved`, `unresolved` or
`rejection_accepted`) with evidence, does not raise an accepted rejection again, treats
changes asked for in a note as in scope, and adds new blocking findings only for real
defects. The pull request review lists these resolutions.

## State

All state is on GitHub: the plan comment and one NexKit comment per run on the issue, the
pull request, its commit statuses, one NexKit comment per round and one NexKit state
comment on the pull request. Run comments only show runs. The state comment, edited in
place, says how many automatic fix rounds were used and holds a hidden JSON record of the
latest feedback, that count, the last completed review (its commit and all its findings)
and the recent fix rounds (summary and note). Recorded text is clipped so the comment and
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
| The PR branch moved while a fix round was running | Refuses to publish; run `/nexkit fix` again. |
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
- Workflow and NexKit files (`.github/`, `.nexkit/`) are protected, so a NexKit change
  cannot alter its own pipeline or configuration. The default Actions token also cannot
  push workflow changes.
- Fork pull requests are never NexKit pull requests: commands on them are refused.
