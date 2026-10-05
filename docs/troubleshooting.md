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
