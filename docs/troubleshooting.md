# Troubleshooting

Start with `nexkit doctor` in the repository, then the NexKit run in the Actions tab. The
summary of the `agent` and `review` jobs shows the result, the files changed and each
command the agent ran; their logs have one collapsible group per tool call with its output.
Each stage that runs uploads its artifact (`nexkit-agent`, `nexkit-checks`, `nexkit-review`)
containing the exact prompt, the results and, unless `transcript` is off, the agent's
redacted transcript: `transcript.md` to read, `transcript.jsonl` with every event.

**Nothing happens after a `/nexkit` comment.**
The workflow file must be on the default branch. The comment's first line must start with
`/nexkit`. Edited comments are ignored; post a new one.

**A command never got 👀.** Another command replaced it while it waited. Post it again
once the running command finishes. See [one run at a time](how-it-works.md#one-run-at-a-time).

**`nexkit doctor`: "Set 'concurrency' on the 'nexkit' job".** In workflows installed before
NexKit 1.8, a plain comment can cancel a waiting command. Move the `concurrency` block in
`.github/workflows/nexkit.yml` into the `nexkit` job, as `nexkit init` writes it.

**"needs write access".** The commenter must be a collaborator with write, maintain or
admin permission.

**"GitHub Actions is not permitted to create or approve pull requests".**
Enable it under Settings > Actions > General > Workflow permissions, or run the
`gh api` command printed by `nexkit doctor`.

**My repository's CI does not run on NexKit pull requests.**
GitHub does not start workflows for events created with the default Actions token. NexKit
runs your configured checks itself. To also run your CI, store a push token as a secret
and name it in `publish.push_token_secret` (see the
[configuration reference](configuration.md#secrets)).

The same applies to a merge by NexKit with `merge.auto`: the push to the base branch
starts no `push` CI, whichever token published the pull request. List those workflows in
`merge.after_workflows` and give each a `workflow_dispatch` trigger; NexKit then starts
them on the base branch after it merges (see
[Automatic merge](configuration.md#automatic-merge)).

**"Automatic merge was not possible".** GitHub refused the merge, and the comment gives
its reason. Usually a branch protection rule or ruleset is not met, such as a required
approval or a required check that did not run. Merge the pull request yourself, or change
the rule (see [Automatic merge](configuration.md#automatic-merge)).

**"Could not start `ci.yml`" after an automatic merge.** The merge happened; only the
workflow did not start. Check that `.github/workflows/ci.yml` exists on the base branch
and has a `workflow_dispatch` trigger without required inputs; `nexkit doctor` checks both
in your working copy. To run it for this merge, use *Run workflow* on the base branch in
the Actions tab.

**"No secret for the `claude` agent".** Add the secret that the agent's configuration
names as a repository secret: for `oauth_token_secret`, the token from
`claude setup-token`; for `api_key_secret`, an Anthropic API key. Either way, `.github/workflows/nexkit.yml` must pass
them to the pipeline: `secrets: inherit`, as `nexkit init` writes it. `nexkit doctor`
checks this.
Secrets are not available to runs triggered from forks.

**Codex stopped: "401 Unauthorized" or "404 Not Found".** A 401 means the agent's
`api_key_secret` does not hold the key of the stage's deployment. A 404 means the endpoint
or the deployment is wrong: the `AZURE_OPENAI_ENDPOINT` secret is the resource URL without
a path, and the stage's `model` is the deployment name, not the model's name.

**"The AZURE_OPENAI_ENDPOINT secret must be an https URL …" or "… the host must end
with …".** The secret holds something other than the resource URL, such as the full target
URI with `/openai/...`. Set it again to `https://<name>.openai.azure.com`. The log line
"Model metadata for `<deployment>` not found" is harmless: Codex does not know the
deployment's name.

**"agents is required".** The configuration is from before agents were declared. Add the
`agents` block that the message shows (see
[Agents are declared](configuration.md#agents-are-declared)).

**"provider must be one of …".** That runtime does not support the provider. The
[Agents](configuration.md#agents) table lists the pairs NexKit supports.

**"… returned a result that does not fit the … schema".** The agent finished, but its
structured result lacks a field or has one of the wrong type. NexKit publishes nothing.
Run the command again; if it keeps happening with one model, use a stronger one for that
stage.

**"The change modifies protected paths".** The agent edited `.github/` or `.nexkit/`, or a
path you added to `protected_paths`. Make those changes yourself, then use `/nexkit fix`.

**"Conflicts with `main` that need your decision".** The pull request conflicts with its
base branch, and some conflicts need a choice the plan, the issue and your notes do not
settle. Answer every question in one comment: `/nexkit fix` followed by your decisions.
The next round merges the base branch again and follows your note.

**A pull request still conflicts with `main` with `conflicts.auto_resolve` on.** Its
latest round may be blocked on a conflict that needs your decision; answer it with
`/nexkit fix`. NexKit also stops after `conflicts.max_rounds` (3 by default) automatic
conflict rounds on one pull request,
and starts one round per commit of `main`. The `maintain` job's log lists what each check
did.

**A new NexKit pull request is behind `main`.** Something was merged into the base branch
while the agent worked. NexKit opens the pull request on the commit the agent started
from, so the work is not lost. Comment `/nexkit fix` to merge the base branch and resolve
any conflicts.

**"The agent started from … which is no longer on `main`".** The base branch was rewritten
(for example force-pushed) while the agent worked, so the commit the change is built on is
gone from it. Comment `/nexkit go` to implement the plan again on the current base branch.

**"conflicts with `main` in protected paths" or "brings changes to `.github/workflows/…`".**
NexKit cannot publish this merge. Merge the base branch into the pull request yourself,
push, and comment `/nexkit review`. For workflow files, a push token with Workflows write
access, named in `publish.push_token_secret`, also lets NexKit push the merge.

**⏸️ "The agent stopped at the account's usage limit".** The account behind the agent's
secret, such as the Claude subscription, used up its session or weekly limit. NexKit runs the same command again after the
reset time shown, through the hourly `schedule` trigger in
`.github/workflows/nexkit.yml`. If nothing happens after the reset, check that the
trigger is there (`nexkit doctor` warns when it is not) and that scheduled workflows are
not disabled for the repository. To continue earlier, or with another note, comment the
command yourself; it replaces the paused one.

**The agent timed out or stopped on its budget.** The issue is probably too large. Ask for a
new plan and use its suggested split, or raise `timeout_minutes` / `max_budget_usd` for
the `implement` stage.

**"Unable to download artifact(s): Artifact not found" on `report`.**
`report` only downloads the artifacts of stages that ran, so this means a stage ran but
stopped before its upload step, for example because the run was cancelled. Open that
stage's log. The run's comment and statuses are still set, without that stage's results.
See [which artifacts each command produces](how-it-works.md#logs-summary-and-artifacts).

**Setup failed.** The `setup` commands must work on a fresh `ubuntu-24.04` runner without
secrets. Run them in a clean container to reproduce.
