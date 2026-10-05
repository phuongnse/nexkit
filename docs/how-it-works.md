# How NexKit works

## One run, six jobs

Every command starts one run of the reusable workflow
[`pipeline.yml`](../.github/workflows/pipeline.yml). Jobs that do not apply are skipped.

| Job | Runs for | Token permissions | Claude credential | Does |
|---|---|---|---|---|
| `route` | every event | read, issue comments | no | Checks the command and the author's write access, reads the config from the default branch, picks the action. |
| `agent` | plan, go, fix | read only | yes | Installs Claude Code, runs `setup`, then one Claude session. Uploads the result and a patch. |
| `publish` | plan, go, fix | write | no | Posts the plan, or applies the patch, rejects protected paths, commits, pushes and opens the pull request. Never executes repository code. |
| `verify` | go, fix, review | read only | no | Runs `setup` and the checks on the published commit. |
| `review` | go, fix, review | read only | yes | A fresh Claude session with read-only tools reviews the diff against the plan and the check results. |
| `report` | every action | write | no | Sets commit statuses, posts the review and status comment, starts the next automatic round or asks for a person. |

The agent does not commit or push. It edits the working tree, and NexKit turns those edits
into a commit in a different job. As a result:

- The session that runs Claude never holds a token that can write to the repository.
- Jobs that hold write tokens never run code from the repository or from the agent.
- Checks run in a job with no secrets at all.

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

## State

All state is on GitHub: the plan comment on the issue, the pull request, its commit
statuses and one NexKit status comment on the pull request. That comment holds a table of
rounds and a hidden JSON record of the latest feedback and how many automatic fix rounds
were used. Nothing else is stored, so there is nothing to migrate or repair.

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
