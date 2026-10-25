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

**`nexkit doctor`: "does not give the pipeline checks: read".** A pipeline job asks for a
permission that `.github/workflows/nexkit.yml` does not give, and GitHub then starts no run
of the workflow at all. Workflows from NexKit 4.3.0 or earlier lack `checks: read`, which
autopilot needs to read the annotations that name the failures of flaky runs. Add each
permission named under `permissions:`, as `nexkit init` writes it.

**`nexkit doctor`: "autopilot.enabled is on, but publish.push_token_secret is not set".**
The same for `conflicts.auto_resolve`. Rounds that NexKit starts by itself push with the
Actions token, which may not push changes to `.github/workflows/`. When a person merges
such a change into the base branch, an automatic fix round leaves the merge out and fixes
the pull request as it is, and a round that must merge it for a conflict stops before its
agent and waits for a person (see below); neither uses up an automatic round. This is not
a problem, and doctor still passes.
To let those rounds go on by themselves, name a fine-grained token with Contents, Pull
requests and Workflows write access in `publish.push_token_secret` (see
[Secrets](configuration.md#secrets)).

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

**"Artifact not found" on *Download check results* in the `agent` job.** A fix round
downloads the `nexkit-checks` artifact of the run whose failing checks kept
[result files](configuration.md#result-files-of-failing-checks). Artifacts expire after 14
days, so a round started later runs without those files; the step does not fail the round,
and the *Collect issue context* log says that the prompt has no section about them.

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
asks for a split; a second time waits for you, and so does a part of an issue that already
asked for one.

**Autopilot does not start an issue.** Run `nexkit status`: its *Autopilot* section says,
for each open issue in scope, what the next sweep does and why an issue waits, such as
another issue that blocks it (GitHub's *blocked by*; "Depends on" text does not count), an author without write
access, `max_parallel`, or a question without a recommended option. Every issue that
waits for a person is also listed under *Needs a person*, and does not count toward
`max_parallel`. With the default `autopilot.scope`, an issue outside the earliest open
milestone is not in scope; `null` takes every open issue. A command that
autopilot started but that never ran starts once more after six hours; when that one does
not run either, the issue waits for you, and you can post the command yourself. To sweep
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

**"The agent finished without changing any files".** The implement or fix agent found
nothing to change; the comment says what it found. When an automatic fix round after
failing checks names a cause outside the code, such as a package registry or a service
that failed while a check ran (`cause_outside`), NexKit does not stop: it runs the checks
and the review once more on the same commit, within the same automatic round, and merges
or reports as usual when they pass. When they fail again, the round ends as
`recheck_failed` and waits for a person, and the mention gives the agent's finding. Check
the service, then comment `/nexkit review`, or `/nexkit fix <instructions>` if the
failure does come from the code.

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
its reason. NexKit handles the refusals that pass: a base branch that moved meanwhile is
tried again, required checks that have not passed yet are waited for by autopilot's
sweeps (`merge_waiting`), a branch that must be up to date is updated and reviewed
again with `conflicts.auto_resolve`, and a branch that someone pushed to after the round
checked it ("Head branch was modified") gets `/nexkit review` on its new head. What is left is a rule that needs a person, such as
a required approval, or required checks that failed or never reported within six hours.
Merge the pull request yourself, or change the rule (see
[Automatic merge](configuration.md#automatic-merge)).

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
NexKit cannot publish this merge, and the round stopped before its agent ran, without using
up an automatic round. Merge the base branch into the pull request yourself, push, and
comment `/nexkit review`. **"Did not merge `main`, which changed `.github/workflows/…`"**
means the round fixed the pull request without the merge; it stays behind `main` until
you merge it. For workflow files, a push token with Workflows write
access, named in `publish.push_token_secret`, also lets NexKit push the merge.

## When NexKit waits for a person

At most of these stops NexKit mentions `notify`, and `nexkit status` lists the item under
*Needs a person* with its reason; a few are results a person should check. Each entry
says what the person who decides, or a session working for them, does (see [Who does
what](how-it-works.md#who-does-what)). Never guess a cause: a cause counts only when a run
log, a trace or a result file shows it.

### Publishing failed after `/nexkit go`

Read the run's comment and the `publish` job's log. When the base branch was
force-pushed, the same `/nexkit go` implements the plan again on it (see above). Otherwise
find the cause before posting `/nexkit go` again: it runs the whole implement round again,
and stops the same way while the cause stays.

### A question without a recommended option, or asked again

Answer every question in one comment, one short line each:

- A plan's questions: comment `/nexkit plan <answers>` on the issue. With autopilot, a
  comment with the answers is enough: the next sweep plans again.
- A blocked `/nexkit go`: `/nexkit go <answers>`, or `/nexkit plan <answers>` when the
  answers change the plan.
- A blocked round on a pull request: `/nexkit fix <answers>`.

When a question comes back after autopilot's answers, or after yours, the recommended
option did not settle it. Read what is new in the question and why, and answer from the
plan, the issue and the repository's docs, not with the same option again. When the
answer is to wait for another issue, mark the issue *blocked by* it, with its reason line
(see the [`request` skill](../skills/request/SKILL.md)). A question that asks a person to
download or attach a CI run's files: see [Evidence from a CI run is
missing](#evidence-from-a-ci-run-is-missing).

### A run stopped twice on a transient error

NexKit already ran the command once more, and the comment shows both errors. Read them
and the run log. When the cause has passed, for example the service answers again, post
the same command again. Otherwise wait until it has: another run now stops the same way.

### No automatic fix rounds left

Read the failing checks' output and the result files in the round's `nexkit-checks`
artifact, or the review's findings. When the same check failed the same way in the last
rounds, ask for the cause first: `/nexkit fix Find the cause of the <check> failure from
the result files first, with evidence, then change code only for that cause.` Otherwise
post `/nexkit fix` with the review's blocking findings and its unmet criteria. When this
stop comes often, `fix.max_auto_rounds` may be too low.

### A round ended as `recheck_failed`

The fix round found a cause outside the code, and the checks failed again on the same
commit (see ["The agent finished without changing any
files"](#agents)). Check the service or registry that the finding names, in the run log.
When it works again, comment `/nexkit review`. When the result files show that the
failure comes from the code after all, post `/nexkit fix` with that evidence.

### A command got the reply that the implement round still runs

The command, or *Request changes* review, did not run. Post it again once the
`/nexkit go` run that opened the pull request has ended (see [One run at a
time](how-it-works.md#one-run-at-a-time)).

### An issue closed as `no_change` that still needs a change

The run's comment holds the finding and its evidence. Check them, for example that the
failing test no longer fails on the default branch after the commit the finding names.
When the evidence contradicts the finding, reopen the issue and comment
`/nexkit plan <what still needs changing, with the evidence>`.

### Conflict rounds used up

The pull request had `conflicts.max_rounds` automatic conflict rounds. Read the last one:
when it was blocked on a choice, answer it with `/nexkit fix <decisions>`. Otherwise
comment `/nexkit fix` yourself, with any rule the conflict needs, such as which side's
numbering to keep.

### A round needs a manual merge (`needs_merge`)

The merge the round needs, for a conflict in a protected path or for a conflict whose
merge brings workflow changes without a push token that may push them, is one NexKit
cannot publish, so the round stopped before its agent (see [above](#pull-requests-and-merges)):

1. Merge the base branch into the pull request's branch yourself, in a separate worktree,
   and resolve the conflicts.
2. Run the repository's `setup` and `checks` commands locally.
3. Push to the branch, then comment `/nexkit review`.

### An implement or fix round ran out of time again

NexKit asked for one split plan already, and a part inherits the time-outs of the issues
above it (see [Autopilot](configuration.md#autopilot)). Do not split it again blindly.
Read the plan, the run log and the result files in the round's `nexkit-checks` artifact.
When they show why the round did not finish, comment
`/nexkit plan <the cause, with the evidence, and the smallest change that removes it>`.
When they show no cause, find it first, for example by running the slow check locally;
then split the issue by hand with the `request` skill, or comment a smaller plan. An
issue three levels of parts deep is not split again.

### A diagnosis round stopped as `blocked`

It found no cause in the evidence (see [Diagnosis
rounds](how-it-works.md#failure-handling)). Do not pick a cause without evidence. Read
what it found, the run log, the result files and the CI evidence the round downloaded;
its *Collect issue context* log lists it. When they show the cause, post
`/nexkit fix <the cause, with the evidence>`. Otherwise answer its question, or find the
cause first.

### A round failed because of its plan

When the run log or the result files show that the plan asked for the wrong thing, change
the plan as [Changing a plan](how-it-works.md#stages) says: `/nexkit plan <what to change>`
before `/nexkit go`, or `/nexkit fix <what to change>` on the pull request.

### The plan's change is in protected paths

NexKit rejects a change to a protected path (see ["The change modifies protected
paths"](#pull-requests-and-merges)). A person makes it by hand, exactly as the plan says,
in a pull request that closes the issue. NexKit's checks do not run on a pull request it
did not open, so run the repository's checks before merging it.

### The plan comment says triage failed

The plan ran on the previous plan's profile, or on `default_profile` (see
[Profiles](how-it-works.md#profiles)). When that profile is wrong for the issue, comment
`/nexkit plan Use the <profile> profile`.

### The plan names a profile that no longer exists

The round stopped before any job ran. Comment `/nexkit plan`, so that triage chooses a
profile that exists, then post the command that stopped again. Read the new plan before
you post `/nexkit go`: it may differ.

### A usage-limit pause with an unknown reset time

NexKit does not resume it (see the [usage limit entry](#agents)). Comment the command
again after the reset.

### Every run misses a cache

The *Restore caches* step lists each entry as missed. Compare the `cache` entries with
the CI workflow's `actions/cache` steps: the same paths in the same order, and the same
key files in the same order (see [Caches](configuration.md#caches)). Check that CI on the
default branch succeeds: `actions/cache` saves only at the end of a successful job.

### The review job errored

NexKit already ran the review once more after a transient error or a review that left no
result. The round's comment says why. Comment `/nexkit review` to try again, unless it
names an authentication error: fix the review agent's secret first, since another try
stops the same way (see ["The AI review did not complete"](#agents)).

### The automatic merge was refused

The round's comment says why, and NexKit handles the refusals that pass (see
["Automatic merge was not possible"](#pull-requests-and-merges)). For a pull request that
waited for required checks (`merge_waiting`) and now waits for a person:

- When a check failed, post `/nexkit fix` naming the failing check.
- When a new commit came and no review started for it, comment `/nexkit review`.
- When the checks did not pass within six hours, find out why from their runs.

For any other rule, such as a required approval, merge the pull request yourself or
change the rule.

### An approved pull request has criteria the review could not verify

Check each listed criterion as [Reviewing a pull
request](how-it-works.md#reviewing-a-pull-request) says. When a test or a check could
show it, comment `/nexkit fix Add tests that show these criteria, so the review can
verify them: <criteria>`. When nothing can, the person who decides confirms it from the
diff and the check results, then merges.

### A run ended without a report twice

A sweep marked the run as ended without a report and started it once more, and that run
ended the same way; or it was a person's command (see [Runs that never
end](how-it-works.md#autopilot)). Read the workflow run for why it ended, such as a
cancelled run or a lost runner, then post the same command once more. When it ends
without a report again, find the cause in the Actions run before you try again.

### Evidence from a CI run is missing

NexKit downloads the evidence of the CI runs that an issue names (see [Evidence from CI
runs](configuration.md#evidence-from-ci-runs)), so never answer a question by
downloading or attaching a run's files. Read the round's *Collect issue context* log:

- When the run was not named, post the command again with the CI run's link in its note,
  such as `/nexkit fix <run link>`. Link the CI workflow's run, not a NexKit run: NexKit's
  own runs never count.
- When the download failed for a transient reason, post the command again.
- When the artifacts expired, the evidence is gone: reproduce the failure, or decide
  without it.

### Autopilot leaves an issue alone

Run `nexkit status`, which gives the reason (see ["Autopilot does not start an
issue"](#agents)). Autopilot never takes an issue opened by someone without write
access, because its text would drive an implement session. A person with write access
reads it as untrusted text, and opens a new issue with what they trust.

### An issue is blocked by an issue outside the scope

Autopilot does not work on the blocker, so the issue waits. Bring the blocker into the
scope, for example by giving it the scope's milestone, or remove the *blocked by*
relationship when the issue does not really need it.

### A parent's parts all closed and none was completed

NexKit leaves the parent open, since nothing it asked for was delivered (see [Parent
issues](configuration.md#parent-issues)). Decide whether the parent is done: close it, or
open new parts for what is left.

### A CI issue holds records of several failures

An issue for a run whose failures would open more than five issues, or one from an older
release that holds every failure of a workflow, can hold records of several failures
while its plan and pull request cover only some (see [Flaky
runs](configuration.md#autopilot)). Before it closes, account for every record as one of:

- fixed by a named commit, whose change matches the failure's message or log line;
- tracked by an issue of its own. When no issue covers it, open one in the CI issue's
  milestone, if it has one, from the record's run, job, attempt and failure annotation
  (`gh api repos/{owner}/{repo}/check-runs/<job id>/annotations` names the failing test,
  its file and line). Guess no cause: ask for the cause, with evidence, first. Link the
  run and the job in the issue, so NexKit hands their evidence to its rounds;
- an outside failure, such as a lost runner or an unreachable package source, named as
  such.

When a record matches none of these and its evidence is gone, say so in the new issue
instead of guessing. Do this through new issues, not comments on the CI issue while a
NexKit run for it is queued or running.
