# Troubleshooting

Start with `nexkit doctor` in the repository, then the NexKit run in the Actions tab. The
summary of the `agent` and `review` jobs shows the result, the files changed and each
command the agent ran; their logs have one collapsible group per tool call with its output.
Each stage that runs uploads its artifact (`nexkit-agent`, `nexkit-checks`, `nexkit-review`)
with the exact prompt, the results and, unless `transcript` is off, the agent's redacted
transcript: `transcript.md` to read, `transcript.jsonl` with every event.

## Setup

**`nexkit doctor`: "Set 'concurrency' on the 'nexkit' job".** In workflows installed before
NexKit 1.8, a plain comment can cancel a waiting command. Move the `concurrency` block in
`.github/workflows/nexkit.yml` into the `nexkit` job, as `nexkit init` writes it.

**`nexkit doctor`: "calls the pipeline at …, but nexkit_ref is …".** Only one of the two
refs was changed. The jobs check out NexKit's code at `nexkit_ref`, so they would run the
code of another release than the pipeline. Set the `uses:` ref and `nexkit_ref` to the
same tag.

**`nexkit doctor`: "NexKit vX.Y.Z is available".** A newer release exists. This is not a
problem, and doctor still passes: upgrade when you choose to, with the steps in that
release's notes.

**"GitHub Actions is not permitted to create or approve pull requests".** Enable it under
Settings > Actions > General > Workflow permissions, or run the `gh api` command printed by
`nexkit doctor`.

**"NexKit configuration error: …".** NexKit replies with this on the command when
`.nexkit/config.json` on the default branch is invalid; run `nexkit doctor` locally to see
the same message. Some of them:

- **"agents is required".** The configuration is from before agents were declared. Add the
  `agents` block that the message shows (see
  [Agents are declared](configuration.md#agents-are-declared)).
- **"… runtime must be one of claude-code: NexKit 4.0.0 removed the codex runtime …".**
  The configuration still declares a Codex agent. Remove it and give its stages a Claude
  agent (see [Codex removed in 4.0](configuration.md#codex-removed-in-40)).
- **"provider must be one of …".** That runtime does not support the provider. The
  [Agents](configuration.md#agents) table lists the pairs NexKit supports.
- **"names that start with GITHUB_ or NEXKIT_ are reserved".** An agent's secret has a
  name GitHub or NexKit uses. Store it under another name and change the `*_secret` key.
- **"publish.push_token_secret: … is an agent's secret".** The push token must be a
  secret of its own, because agents never hold a token that can write.
- **"… does not report usage limits, so a fallback would never run".** Remove `fallback`
  from that agent.
- **"… must use the … runtime too".** A fallback agent runs the stage's `model` and
  `effort`, so it must use the same runtime as the agent.
- **"… NexKit passes at most 8 to one job".** The agents a job may run, with their
  fallbacks (and for a plan with profiles, every profile's plan agent), need too many
  secrets. Use fewer agents, or agents that share secrets.

**Setup failed.** The `setup` commands must work on a fresh `ubuntu-24.04` runner without
secrets. Run them in a clean container to reproduce.

## Commands and runs

**Nothing happens after a `/nexkit` comment.** The workflow file must be on the default
branch. The comment's first line must start with `/nexkit`. Edited comments are ignored;
post a new one.

**A command never got 👀.** Another command replaced it while it waited. Post it again
once the running command finishes. See [One run at a time](how-it-works.md#one-run-at-a-time).

**"needs write access".** The commenter must be a collaborator with write, maintain or
admin permission.

**"Unable to download artifact(s): Artifact not found" on `report`.** `report` only
downloads the artifacts of stages that ran, so a stage ran but stopped before its upload
step, for example because the run was cancelled. Open that stage's log. The run's comment
and statuses are still set, without that stage's results. See
[which artifacts each command produces](how-it-works.md#logs-summary-and-artifacts).

## Agents

**"No secret for the `<name>` agent".** Add the secret that the agent's configuration
names as a repository secret: for `oauth_token_secret`, the token from
`claude setup-token`; for `api_key_secret`, an Anthropic API key.
`.github/workflows/nexkit.yml` must pass it to the pipeline with `secrets: inherit`, as
`nexkit init` writes it; an older workflow that lists secrets one by one can pass only
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and `NEXKIT_PUSH_TOKEN`. `nexkit doctor`
checks this. A [fallback agent](configuration.md#fallback-agents) without its secret does
not fail the stage: it is skipped, and the run summary says so. Secrets are not available
to runs triggered from forks.

**Claude stopped with "401" or "authentication_error".** The secret the agent names does
not hold a working credential: a subscription token from `claude setup-token` that has
expired or was revoked, or an API key that was deleted or has no credit. Set the secret
again and comment the command again.

**"… returned a result that does not fit the … schema".** The agent finished, but its
structured result lacks a field or has one of the wrong type. NexKit publishes nothing.
Run the command again; if it keeps happening with one model, use a stronger one for that
stage.

**The agent timed out or stopped on its budget.** The issue is probably too large. Ask for
a new plan and use its suggested split, or raise `timeout_minutes` or `max_budget_usd` for
the `implement` stage. With fallback agents, each agent's run gets `timeout_minutes`,
within one stage deadline for all of them.
With autopilot on, an implement or fix agent that ran out of time leads to one new plan that
asks for a split; a second time waits for you.

**Autopilot does not start an issue.** Run `nexkit status`: its *Autopilot* section says,
for each open issue in scope, what the next sweep does and why an issue waits, such as
another issue that blocks it, an earlier sub-issue of its parent, an author without write
access, `max_parallel`, or a question without a recommended option. An issue outside the
earliest open milestone (or without `autopilot.label`) is not in scope. A command that
autopilot started but that never ran is not started again; post it yourself. To sweep
again at once: `gh workflow run nexkit.yml -f command=autopilot -f number=autopilot`.

**⏸️ "The agent stopped at the account's usage limit" or "Every agent stopped at its usage
limit".** The account behind the agent's secret, such as a Claude subscription, used up its
session or weekly limit, and so did each of its
[fallback agents](configuration.md#fallback-agents). To keep working through a limit, add
a fallback, such as an Anthropic API key behind a subscription. NexKit runs the same
command again after the reset time shown:

- When the reset is at most `usage_limit.wait_minutes` away, a dispatched NexKit run with
  the command `resume` waits for it in the Actions tab. Otherwise the hourly `schedule`
  trigger in `.github/workflows/nexkit.yml` picks it up, which GitHub can delay by hours.
- If nothing happens after the reset, look for that run and its log, check that the
  trigger is there (`nexkit doctor` warns when it is not) and that scheduled workflows are
  not disabled for the repository.
- To continue earlier, or with another note, comment the command yourself; it replaces the
  paused one, whose comment then shows ⏭️.
- When the comment says "The reset time is unknown" and asks you to comment the command
  after the reset, NexKit will not resume it by itself: a resumed run stopped at the limit
  again without a reset time.

**"The AI review did not complete: …".** The round's comment and the `nexkit/review`
status say why the review stopped. When it is a 401, 403 or another authentication error,
another `/nexkit review` stops the same way: fix the review agent's secret first, or point
the review at another agent in `.nexkit/config.json`. A timeout or a review that returned
no verdict can be tried again with `/nexkit review`.

**"NexKit stopped on <exception>".** An error in NexKit itself, and another try usually
stops the same way. The job's log has the traceback; please report it as a NexKit issue.

## Pull requests and merges

**My repository's CI does not run on NexKit pull requests.** GitHub does not start
workflows for events created with the default Actions token. NexKit runs your configured
checks itself. To also run your CI, store a push token as a secret and name it in
`publish.push_token_secret` (see [Secrets](configuration.md#secrets)). A
`NEXKIT_PUSH_TOKEN` secret that the configuration does not name is not used, and
`nexkit doctor` warns about it.

The same applies to a merge by NexKit with `merge.auto`: the push to the base branch
starts no `push` CI, whichever token published the pull request. List those workflows in
`merge.after_workflows` and give each a `workflow_dispatch` trigger; NexKit then starts
them on the base branch after it merges (see
[Automatic merge](configuration.md#automatic-merge)).

**"The change modifies protected paths".** The agent edited `.github/` or `.nexkit/`, or a
path you added to `protected_paths`. Make those changes yourself, then use `/nexkit fix`.

**"Automatic merge was not possible".** GitHub refused the merge, and the comment gives
its reason. Usually a branch protection rule or ruleset is not met, such as a required
approval or a required check that did not run. Merge the pull request yourself, or change
the rule (see [Automatic merge](configuration.md#automatic-merge)).

**"Could not start `ci.yml`" after an automatic merge.** The merge happened; only the
workflow did not start. Check that `.github/workflows/ci.yml` exists on the base branch
and has a `workflow_dispatch` trigger without required inputs; `nexkit doctor` checks both
in your working copy. To run it for this merge, use *Run workflow* on the base branch in
the Actions tab.

**A new NexKit pull request is behind `main`.** Something was merged into the base branch
while the agent worked. NexKit opens the pull request on the commit the agent started
from, so the work is not lost. Comment `/nexkit fix` to merge the base branch and resolve
any conflicts.

**"The agent started from …, which is no longer on `main`".** The base branch was
rewritten (for example force-pushed) while the agent worked, so the commit the change is
built on is gone from it. Comment `/nexkit go` to implement the plan again on the current
base branch.

**"Conflicts with `main` that need your decision".** The pull request conflicts with its
base branch, and some conflicts need a choice that the plan, the issue and your notes do
not settle. Answer every question in one comment: `/nexkit fix` followed by your
decisions. The next round merges the base branch again and follows your note.

**A pull request still conflicts with `main` with `conflicts.auto_resolve` on.** Its latest
round may be blocked on a conflict that needs your decision; answer it with `/nexkit fix`.
NexKit also stops after `conflicts.max_rounds` (3 by default) automatic conflict rounds on
one pull request, and starts one round per commit of `main`. The `maintain` job's log lists
what each check did.

**"conflicts with `main` in protected paths" or "brings changes to `.github/workflows/…`".**
NexKit cannot publish this merge. Merge the base branch into the pull request yourself,
push, and comment `/nexkit review`. For workflow files, a push token with Workflows write
access, named in `publish.push_token_secret`, also lets NexKit push the merge.
