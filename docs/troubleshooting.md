# Troubleshooting

Start with `nexkit doctor` in the repository, then the NexKit run in the Actions tab.
Each run uploads `nexkit-agent`, `nexkit-checks` and `nexkit-review` artifacts containing
the exact prompt, Claude's full event transcript and the results.

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

**Setup failed.** The `setup` commands must work on a fresh `ubuntu-24.04` runner without
secrets. Run them in a clean container to reproduce.
