# Troubleshooting

Start with `nexkit doctor` in the repository, then the NexKit run in the Actions tab. The
summary of the `agent` and `review` jobs shows the result, the files changed and each
command Claude ran; their logs have one collapsible group per tool call with its output.
Each stage that runs uploads its artifact (`nexkit-agent`, `nexkit-checks`, `nexkit-review`)
containing the exact prompt, the results and, unless `transcript` is off, Claude's redacted
transcript: `transcript.md` to read, `transcript.jsonl` with every event.

**Nothing happens after a `/nexkit` comment.**
The workflow file must be on the default branch. The comment's first line must start with
`/nexkit`. Edited comments are ignored; post a new one.

**"needs write access".** The commenter must be a collaborator with write, maintain or
admin permission.

**"GitHub Actions is not permitted to create or approve pull requests".**
Enable it under Settings > Actions > General > Workflow permissions, or run the
`gh api` command printed by `nexkit doctor`.

**My repository's CI does not run on NexKit pull requests.**
GitHub does not start workflows for events created with the default Actions token. NexKit
runs your configured checks itself. To also run your CI, add a `NEXKIT_PUSH_TOKEN` secret
(see the [configuration reference](configuration.md#secrets)).

**"No Claude credential".** Add `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or
`ANTHROPIC_API_KEY` as a repository secret. Secrets are not available to runs triggered
from forks.

**"The change modifies protected paths".** The agent edited `.github/` or `.nexkit/`, or a
path you added to `protected_paths`. Make those changes yourself, then use `/nexkit fix`.

**"Conflicts with `main` that need your decision".** The pull request conflicts with its
base branch, and some conflicts need a choice the plan, the issue and your notes do not
settle. Answer every question in one comment: `/nexkit fix` followed by your decisions.
The next round merges the base branch again and follows your note.

**A new NexKit pull request is behind `main`.** Something was merged into the base branch
while the agent worked. NexKit opens the pull request on the commit the agent started
from, so the work is not lost. Comment `/nexkit fix` to merge the base branch and resolve
any conflicts.

**"The agent started from … which is no longer on `main`".** The base branch was rewritten
(for example force-pushed) while the agent worked, so the commit the change is built on is
gone from it. Comment `/nexkit go` to implement the plan again on the current base branch.

**"conflicts with `main` in protected paths" or "brings changes to `.github/workflows/…`".**
NexKit cannot publish this merge. Merge the base branch into the pull request yourself,
push, and comment `/nexkit review`. For workflow files, a `NEXKIT_PUSH_TOKEN` with
Workflows write access also lets NexKit push the merge.

**Claude timed out or stopped on its budget.** The issue is probably too large. Ask for a
new plan and use its suggested split, or raise `timeout_minutes` / `max_budget_usd` for
the `implement` stage.

**"Unable to download artifact(s): Artifact not found" on `report`.**
`report` only downloads the artifacts of stages that ran, so this means a stage ran but
stopped before its upload step, for example because the run was cancelled. Open that
stage's log. The run's comment and statuses are still set, without that stage's results.
See [which artifacts each command produces](how-it-works.md#logs-summary-and-artifacts).

**Setup failed.** The `setup` commands must work on a fresh `ubuntu-24.04` runner without
secrets. Run them in a clean container to reproduce.
