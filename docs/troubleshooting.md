# Troubleshooting

Start with `nexkit doctor` in the repository, then the NexKit run in the Actions tab. The
summary of the `agent` and `review` jobs shows the result, the files changed and each
command the agent ran; their logs have one collapsible group per tool call with its output.
Each stage that runs uploads its artifact (`nexkit-agent`, `nexkit-checks`, `nexkit-review`)
with the exact prompt, the results and, unless `transcript` is off, the agent's redacted
transcript: `transcript.md` to read, `transcript.jsonl` with every event.

## Setup

**`nexkit status` or `nexkit doctor`: "… pins NexKit vX.Y.Z, so it runs from …".** The
installed `nexkit` is another release than the one the repository's workflow pins, so it
ran the pinned release, which judges the repository by the rules the pipeline runs (see
[Local commands](../README.md#local-commands)). Its docs are in the folder it names. To
run the installed release instead, pass `--own-release` or set `NEXKIT_OWN_RELEASE=1`.

**"cannot fetch NexKit … from …".** The pinned release could not be fetched: no network,
or a `nexkit_ref` that names no tag or branch there. Fix the ref, or pass `--own-release`
to run the installed release.

**"… pins NexKit as …, which is no release tag or branch, so this NexKit … runs".** The
`nexkit_ref` is a commit, a pull request's ref or an expression. Anyone can add a commit
or a pull request to a repository's network, so the CLI never fetches one, and runs the
installed release. Pin a release tag, as `nexkit init` writes it.

**"… runs NexKit's code from OWNER/REPO at REF, not from phuongnse/nexkit".** The workflow's
`nexkit_repository` names another repository, such as a fork, and NexKit runs another
repository's code only when you name it. If you trust it, set
`NEXKIT_KIT_REPOSITORY=OWNER/REPO`; otherwise pass `--own-release`.

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

### The GitHub App

NexKit [acts as a GitHub App](configuration.md#the-github-app). Without a working App a run
cannot write anything on GitHub, not even a reply, so what went wrong shows only in the
Actions tab, as an error on the `route` job.

**"github_app is not set".** Run `nexkit init`, which creates the App, or follow
[Moving to the GitHub App](configuration.md#moving-to-the-github-app).

**"publish.push_token_secret is gone".** NexKit no longer uses a push token. Remove `publish`
and follow [Moving to the GitHub App](configuration.md#moving-to-the-github-app).

**"NexKit could not get a token from its GitHub App".** One of these:

- **The App is not installed on the repository.** Install it at
  `https://github.com/apps/<slug>/installations/new`. `nexkit doctor --app-id ID --app-private-key FILE`
  checks this.
- **A secret is missing or wrong.** Set the secrets that `github_app` names, with
  `nexkit init --force --app-id ID --app-private-key FILE`. A workflow that lists secrets one
  by one must pass them too.
- **A permission is missing.** A job asks only for what it needs, but GitHub refuses a token
  for more than the App has. Grant it on the App's *Permissions* page, then accept the change
  on the installation, which GitHub asks for each time permissions grow. `nexkit doctor` names
  what is missing.

**"github_app.slug is …, but the App's key belongs to …".** The configuration names another
App than the secrets. NexKit would not recognise its own comments, so the run stops. Correct
`github_app.slug`.

**`nexkit doctor`: "Could not read the App" or "Could not tell whether … is installed".**
GitHub shows a private App, such as the one `nexkit init` creates, to no person's token, and
only the App may ask where it is installed. Pass `--app-id ID --app-private-key FILE`, with
the id from the App's settings page and a private key you generate there; it needs
`openssl`. This is not a problem, and doctor still passes.

**A skipped `nexkit` check on a NexKit pull request.** NexKit's review is a review by its
App, which sends `pull_request_review` like anyone's, and the NexKit workflow skips it. The
check shows that run; it never fails and needs nothing.

**`nexkit doctor`: "NexKit vX.Y.Z is available".** A newer release exists. This is not a
problem, and doctor still passes: upgrade when you choose to, with the steps in that
release's notes.

**"NexKit configuration error: …".** NexKit replies with this on the command when
`.nexkit/config.json` on the default branch is invalid; run `nexkit doctor` locally to see
the same message. Some of them:

- **"agents is required".** The configuration is from before agents were declared. Add the
  `agents` block that the message shows (see
  [Agents are declared](configuration.md#agents-are-declared)).
- **"… runtime must be one of claude-code: NexKit 4.0.0 removed the codex runtime …".**
  The configuration still declares a Codex agent. Remove it and give its stages a Claude
  agent (see [Codex removed in 4.0](configuration.md#codex-removed-in-40)).
- **"autopilot.scope "label" is gone …" or "autopilot.label is gone …".** NexKit 5.0
  removed the label scope. Set `autopilot.scope` to `null` or `"milestone"` and remove
  `autopilot.label` (see [Label scope removed in
  5.0](configuration.md#label-scope-removed-in-50)).
- **"provider must be one of …".** That runtime does not support the provider. The
  [Agents](configuration.md#agents) table lists the pairs NexKit supports.
- **"names that start with GITHUB_ or NEXKIT_ are reserved".** An agent's secret has a
  name GitHub or NexKit uses. Store it under another name and change the `*_secret` key.
- **"github_app.…: … is an agent's secret".** The App's secrets must be secrets of their
  own, because agents never hold what can write.
- **"… does not report usage limits, so a fallback would never run".** Remove `fallback`
  from that agent.
- **"… must use the … runtime too".** A fallback agent runs the stage's `model` and
  `effort`, so it must use the same runtime as the agent.
- **"… NexKit passes at most 8 to one job".** The agents a job may run, with their
  fallbacks (and for a plan with profiles, every profile's plan agent), need too many
  secrets. Use fewer agents, or agents that share secrets.

While the configuration on the default branch is invalid, NexKit refuses every command,
`/nexkit review` on the pull request that corrects it included, so that pull request gets
no `nexkit/checks` or `nexkit/review` status. Where the rules require them, someone with
the right to bypass them merges it; NexKit works again from that merge on.

**`nexkit/checks` fails with "NexKit would refuse .nexkit/config.json: …".** The pull
request is a person's, it changes `.nexkit/config.json`, and NexKit would refuse the file
at its head with that error. Merged, it would stop every command as above. `/nexkit review`
reads the file through the API and loads it as `nexkit doctor` does, whatever the checks
and the review found; the round's comment gives the whole message. Correct the file, run
`nexkit doctor` on the branch, push, and comment `/nexkit review` again. "NexKit could not
read .nexkit/config.json" means GitHub did not answer: comment `/nexkit review` again.

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
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `NEXKIT_APP_ID` and
`NEXKIT_APP_PRIVATE_KEY`. `nexkit doctor` checks this. A [fallback agent](configuration.md#fallback-agents) without its secret does
not fail the stage: it is skipped, and the run summary says so. Secrets are not available
to runs triggered from forks.

**Claude stopped with "401" or "authentication_error".** The secret the agent names does
not hold a working credential: a subscription token from `claude setup-token` that has
expired or was revoked, or an API key that was deleted or has no credit. Set the secret
again and comment the command again.

**"… returned a result that does not fit the … schema" or
"stopped (error_max_structured_output_retries)".** The agent finished, but its structured
result lacks a field or has one of the wrong type. NexKit handles it by itself (see [A
result that does not fit](how-it-works.md#agents)): a field whose empty value means the
same as leaving it out takes that value, and otherwise NexKit asks the same session for its
result alone, keeping its changes, and goes on when that result fits. When it does not,
NexKit runs the command again, as after a transient error. A person is needed only when the
run after the last wait of `retry.wait_minutes` stops the same way (see [A run kept
stopping on a transient error](#a-run-kept-stopping-on-a-transient-error)): run the command
again, and if it keeps happening with one model, use a stronger one for that stage.

**The agent timed out or stopped on its budget.** The issue is probably too large. Without
autopilot, ask for a new plan and use its suggested split; or raise `timeout_minutes` or
`max_budget_usd` for the `implement` stage. With fallback agents, each agent's run gets `timeout_minutes`,
within one stage deadline for all of them.
With autopilot on, an implement or fix agent that ran out of time leads to one new plan that
asks for a split; a second time waits for you, and so does a part of an issue that already
asked for one. A plan that is too large but suggests no parts is planned once more with a
note that asks for parts; when the second plan suggests none either, see [A plan is too
large again](#a-plan-is-too-large-again).

**Autopilot waits after a `/nexkit go` that was blocked or failed.** Answer in a comment on
the issue, or mark the issue the round waits for as its blocker (GitHub's *blocked by*):
the next sweep after your comment, or after that blocker closes, starts `/nexkit plan` by
itself, so the plan holds the change. You need not post the command. A round that ran out
of time follows the rules above instead.

**Autopilot does not start an issue.** Run `nexkit status`: its *Autopilot* section says,
for each open issue in scope, what the next sweep does and why an issue waits, such as
another issue that blocks it (GitHub's *blocked by*; "Depends on" text does not count), an author without write
access, `max_parallel`, or a question without a recommended option. Every issue that
waits for a person is also listed under *Needs a person*, and does not count toward
`max_parallel`. With the default `autopilot.scope`, an issue outside the earliest open
milestone is not in scope; `null` takes every open issue. A command that
autopilot started but that never ran starts again: once more as soon as its workflow run
ended, or after `autopilot.lost_after_minutes` (30 by default) when that run cannot be
read, then 30, 60 and 120 minutes after its last start; when the fifth does not run
either, the issue waits for you (see [A command or round did not
start](#a-command-or-round-did-not-start)). To sweep again at once:
`gh workflow run nexkit.yml -f command=autopilot -f number=autopilot`.

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

**"Note from the implement agent" in a round's comment.** The implement agent changed
files and also wrote text in `no_change`, which is only for a finding that the issue needs
no change, in a round that changes nothing. A round that changed files is a change, so
NexKit published it, put the text under *Notes for the reviewer* in the pull request, and
kept the issue open. Read the note with the review: it is often a remark such as "every
planned change was made". When it does say that the issue needs no change, close the pull
request and the issue yourself.

**"NexKit stopped on <exception>".** An error in NexKit itself, and another try usually
stops the same way. The job's log has the traceback; please report it as a NexKit issue.

## Pull requests and merges

**My repository's CI runs too often on NexKit's work.** NexKit's pushes, pull requests and
merges start your workflows, as a person's do. To save runners on the default branch, take
a costly workflow's `push` trigger off for it and list it in `merge.after_workflows` with
`every`, or give it a `schedule` and list it in `autopilot.watch_workflows` (see
[Automatic merge](configuration.md#automatic-merge) and
[Autopilot](configuration.md#autopilot)).

**A check was skipped, or ran when it could have been skipped.** A round's comment says
"`name` skipped: no path it reads changed since `abc1234`" for each check that its
[`unaffected_by`](configuration.md#checks-that-a-change-cannot-affect) let NexKit skip,
and the `route` job's summary and notices say the same. A check that should have run
means its list names a path the check reads: take that path out, and comment
`/nexkit review`, which runs every check. A check runs in full in implement and fix
rounds, in a `/nexkit review` that a person comments, in its first round after its `run`
or `setup` changed or after an upgrade, after a force-push, and when the update changed
300 files or more.

**A pull request waits for a bug issue and starts no fix round.** Its checks fail only in
commands that also fail on the default branch, in a workflow of `merge.after_workflows`
or `autopilot.watch_workflows` whose `bug` issue autopilot opened (outcome `ci_waiting`, state `waiting` in
`nexkit status`), and only in tests that fail there too, or, where neither side names
failing tests, in none. Fixing that issue fixes the pull request too, so NexKit waits
instead of fixing the same failure in every pull request. The wait ends by itself once the
workflow passes on the default branch again, or the `bug` issue is closed: the next sweep
starts one automatic fix round that merges the default branch. If the failure is the pull
request's own after all, comment `/nexkit fix <instructions>` on it. A check names its
failing tests when its `results` hold JUnit XML, and the default branch does when its
failed jobs have error annotations that name tests. When only one side names tests, the
failures differ and the pull request gets the usual round. When neither does, the command
alone decides, so a pull request that breaks another test in the same script also waits.
To tell the two apart, name the check's JUnit output in its `results` and let your CI's
test reporter annotate failures.
A pull request whose checks are not compared this way, because your CI runs its commands
differently from NexKit's checks, gets the usual fix rounds (see *Failing checks the
default branch has* under [How it works](how-it-works.md#stages)).

**"The change modifies protected paths".** The agent edited `.github/` or `.nexkit/`, or a
path you added to `protected_paths`. Make those changes yourself, then use `/nexkit fix`.

**"Automatic merge was not possible".** GitHub refused the merge, and the comment gives
its reason. NexKit handles the refusals that pass: a base branch that moved meanwhile is
tried again, required checks that have not passed yet are waited for by autopilot's
sweeps (`merge_waiting`), a branch that must be up to date is updated and checked
again with `conflicts.auto_resolve`, also when GitHub only says that its required checks
are "expected" while it reports the pull request as behind its base branch, and reviewed again only when its own change differs, and a branch that someone pushed to after the round
checked it ("Head branch was modified") gets `/nexkit review` on its new head. A required
check that failed, at the first try or while the merge waits, gets an automatic fix round
within `fix.max_auto_rounds`, and while the merge waits, a new commit on the branch gets
`/nexkit review`. What is left
is a rule that needs a person, such as a required approval, required checks that neither
passed nor failed within six hours, or a required check that failed with no automatic fix
round left. Merge the pull request yourself, comment `/nexkit fix` on it, or change the
rule (see [Automatic merge](configuration.md#automatic-merge)).

**"Could not start `ci.yml`" after an automatic merge.** The merge happened; only the
workflow did not start. Check that `.github/workflows/ci.yml` exists on the base branch
and has a `workflow_dispatch` trigger without required inputs; `nexkit doctor` checks both
in your working copy. To run it for this merge, use *Run workflow* on the base branch in
the Actions tab.

**A new NexKit pull request is behind `main`.** Something was merged into the base branch
while the agent worked. NexKit opens the pull request on the commit the agent started
from, so the work is not lost. When it conflicts, `conflicts.auto_resolve` starts a
conflict round by itself; without it, comment `/nexkit fix` to merge the base branch and
resolve the conflicts.

**"The agent started from …, which is no longer on `main`".** The base branch was
rewritten (for example force-pushed) while the agent worked, so the commit the change is
built on is gone from it. Comment `/nexkit go` to implement the plan again on the current
base branch.

**"Conflicts with `main` that need your decision".** The pull request conflicts with its
base branch, and some conflicts need a choice that the plan, the issue and your notes do
not settle. Answer every question in one comment: `/nexkit fix` followed by your
decisions. The next round merges the base branch again and follows your note.

**A pull request still conflicts with `main` with `conflicts.auto_resolve` on.** Every
sweep, and the end of every NexKit run, starts the conflict round of a pull request that
GitHub reports as conflicting while no round of it is queued or running, once for each
commit of `main` and each round of the pull request that ended since; while GitHub has
not worked out mergeability yet, the next sweep asks again (see *Resolving conflicts*
under [How it works](how-it-works.md#work-without-a-command)). So a conflict that stays
has one of these reasons: the latest round is blocked on a conflict that needs your
decision, which you answer with `/nexkit fix`; `conflicts.max_rounds` (3 by default)
rounds are used up; or the conflict needs a manual merge (below). The `maintain` job's
log lists what each check did.

**"conflicts with `main` in protected paths".** NexKit cannot publish this merge, and the
round stopped before its agent ran. Merge the base branch into the pull request yourself,
push, and comment `/nexkit review`.

## When NexKit waits for a person

At most of these stops NexKit mentions `notify`, and `nexkit status` lists the item under
*Needs a person* with its reason; a few are results a person should check. Each entry
says what the person who decides, or a session working for them, does (see [Who does
what](how-it-works.md#who-does-what)). Never guess a cause: a cause counts only when a run
log, a trace or a result file shows it.

### What a mention says

Every mention, and every reason under *Needs a person* in `nexkit status`, says three
things: what to do, why NexKit does not do it itself, and where to look. The mention for
each outcome of a run is:

| Outcome | `@owner a person is needed: …` | It also names |
|---|---|---|
| plan with questions | answer the plan's questions with `/nexkit plan <answers>`. The plan asks for decisions that are the owner's to make | the first question |
| plan too large | split the issue by hand into parts that each fit one session, with the `request` skill, or make it smaller and comment the change on it. The plan says the issue is too large for one session | |
| plan finds no change | check the plan's finding, then close the issue, or comment `/nexkit plan <what still needs changing>`. The plan finds that no change is needed, and NexKit closes an issue by itself only with autopilot | |
| plan or round needs protected paths | make the change in the protected paths (…) by hand, as the plan says. Only a person changes protected paths | the paths |
| a recommended close | answer the questions: an answer that is the option to close the issue closes it as not planned. The agent recommends closing the issue, and NexKit closes it by itself only when that is a round's one recommendation, with its finding that no change is needed | |
| `agent_blocked` | answer the questions in the run's comment with `/nexkit plan <answers>`, or `/nexkit go <answers>` when they leave the plan as it is (on a pull request: with `/nexkit fix <answers>`). The agent stopped at a decision that is the owner's to make | the first question |
| `agent_error` | read the error in the run's comment and log, remove its cause, then post the command again. The run stopped on an error, and NexKit runs a command again by itself only after an error of the service, the network or the runner | the error |
| `agent_error`, out of time | comment `/nexkit plan` to split the issue into parts that each fit one session, or raise the stage's `timeout_minutes`. The agent ran out of time before it finished | |
| `agent_failed` | read the agent job's log for why it ended, then post the command again. The agent job ended without a result, so NexKit had nothing to go on | |
| `publish_failed` | read the publish job's log, remove the cause, then post the command again. NexKit could not publish the result | the error |
| `review_failed` | comment `/nexkit review` to run the review again. The AI review did not complete, and NexKit runs it again by itself only after an error of the service, the network or the runner; after an authentication error: fix the review agent's secret first, since another try stops the same way | the error |
| `needs_human` | comment `/nexkit fix <instructions>` for what still fails, push a commit yourself, or close the pull request. Checks or review still need attention, and no automatic fix rounds remain | the failing checks, the number of blocking findings and where the first one is |
| `recheck_failed` | check what the fix agent found outside the code, then comment `/nexkit review` once it works again, or `/nexkit fix` with evidence that the code is the cause. The checks or review still fail on the same commit after the fix agent found a cause outside the code | what the agent found |
| `retry_failed` | post the command again once the cause has passed. The run stopped on a transient error again after every wait of `retry.wait_minutes`, such as `4 times, the last after every wait of retry.wait_minutes`, or at once with `retry.wait_minutes` empty | the last error |
| `conflicting` | comment `/nexkit fix` on the pull request, with any rule the conflict needs. Its commit conflicts with the base branch, and NexKit started no round to resolve it | |
| `ready`, with `merge.auto` off | review the pull request, then merge it or close it. Checks and the AI review passed, and with `merge.auto` off a person decides the merge | |
| `ready`, with checklist items a person decides | decide the checklist items in the round's comment: approve the pull request to accept them, which NexKit then merges (with `merge.auto` off, then merge it), or comment `/nexkit fix <what to change>`. Only a person decides them, and the review found them not met | the first item |
| `ready`, with criteria the review could not verify | confirm from the diff and the checks the acceptance criteria that the review could not verify, then merge the pull request; with `merge.auto`, approve the pull request, which NexKit then merges | the first criterion |
| `merge_refused` | what unblocks the merge, and the rule that refused it, such as `approve the pull request as a code owner (@owner), which NexKit then merges. GitHub requires a code owner's review for this file, and NexKit's App cannot review its own pull request: docs/reference/ui-glossary.json (+6 −0, owned by @owner)`: see [The automatic merge was refused](#the-automatic-merge-was-refused) | the rule, the files and their owners |
| `merge_issue_closed` | merge the pull request or close it. Its issue closed before NexKit merged it, and NexKit leaves the pull request of a closed issue to a person | |
| `pull_closed`, reopened | approve the pull request, which NexKit then merges, or comment `/nexkit review` on it. It was closed when its round was to merge it, and reopening it starts nothing by itself. A pull request that stays merged or closed needs nobody | |
| `agent_untrusted`, `merge_untrusted` | read the issue, and comment `/nexkit trust` on it if you trust it. Someone without write access edited it after a writer vouched for it, so NexKit does not act on its text (or merge its pull request) | |
| `close_failed`, `on_base_failed` | close the issue (and the pull request) yourself. No change is needed, or the base branch already holds this change, but GitHub refused NexKit's close | the error |
| `paused`, not resumed | post the command again once the usage limit has reset. The agent hit its usage limit, and NexKit does not resume this run | |
| `retry_waiting`, long past its time | post the command again, and check that NexKit's workflow has its `schedule` trigger. It was to run again after a transient error, and no run started it | the time it was to run |

Text that an agent, GitHub or a person wrote, such as a question, an error or a path, is
quoted in a code span on one line, and a login such as an issue's author is named in a code
span too, so that a mention reaches only `notify`. Autopilot's own reasons follow the
same form, such as `read the issue, and comment /nexkit trust on it if you trust it.
Autopilot leaves it alone: opened by @someone, who cannot write to the repository`.

### Publishing failed after `/nexkit go`

Read the run's comment and the `publish` job's log. A server error, a rate limit or a
dropped connection is not the cause: `publish` already tried four times, and NexKit ran
the command once more (see [Failure handling](how-it-works.md#failure-handling)). When the
base branch was force-pushed, the same `/nexkit go` implements the plan again on it (see
above). Otherwise find the cause, such as a branch rule or a missing App permission,
before posting `/nexkit go` again: it runs the whole implement round again, and stops the
same way while the cause stays.

### A question without a recommended option, or asked again

Answer every question in one comment, one short line each:

- A plan's questions, or those of a blocked `/nexkit go`: with autopilot, comment the
  answers on the issue, without a command; the next sweep plans again with them (see
  ["Autopilot waits after a `/nexkit go` that was blocked or failed"](#agents)). Without
  autopilot, comment `/nexkit plan <answers>`, or for a blocked `/nexkit go` whose
  answers leave the plan as it is, `/nexkit go <answers>`. An option the comment marks
  *(needs a new plan)*, such as splitting the issue, always takes `/nexkit plan`.
- A blocked round on a pull request: `/nexkit fix <answers>`, or `/nexkit plan <answers>`
  on the issue for an option marked *(needs a new plan)*.

**To close the issue through an answer,** pick the option the comment marks *(NexKit
closes the issue as not planned)*: comment `/nexkit go <that option>` or `/nexkit plan
<that option>`, or with autopilot the option alone, in a comment of someone with write
access. Write it as its text, give or take case, quotes and a final full stop, or as its
number when NexKit asked one question, such as `/nexkit go 2`. NexKit closes the issue
as not planned, with a comment that quotes your answer and links the run or plan that
asked, and runs no agent. An answer that says more, such as the option and a condition,
goes to the next round as a note. While a NexKit pull request for the issue is open,
close both yourself. Agents offer closing only as such an option; a round on a pull
request never offers it.

When a question comes back after autopilot's answers, or after yours, the recommended
option did not settle it. Read what is new in the question and why, and answer from the
plan, the issue and the repository's docs, not with the same option again. When the
answer is to wait for another issue, mark the issue *blocked by* it, with its reason line
(see the [`request` skill](../skills/request/SKILL.md)); with autopilot, the issue is
planned again once that issue closes. A question that asks a person to download or attach
a CI run's files: see [Evidence from a CI run is
missing](#evidence-from-a-ci-run-is-missing).

### A run kept stopping on a transient error

NexKit already ran the command again after each wait of `retry.wait_minutes` (by default
15, 30 and 60 minutes), and the comment lists every error. Read them and the run logs.
When the cause has passed, for example the service answers again, post the same command
again. Otherwise wait until it has: another run now stops the same way. When the stops
were results that did not fit the schema, the cause is the model, not a service: post the
command again once, and if it stops the same way, use a stronger model for that stage.
When outages of a service the repository's `setup` downloads from often last longer than
the waits, add longer waits, such as `[15, 30, 60, 120, 240]`.

While a command still waits to run again, its comment shows ⏸️ with the time, and
`nexkit status` lists it as `retrying`: nobody needs to act. A command posted meanwhile
replaces the wait.

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

The run's or plan's comment holds the finding and its evidence. Check them, for example
that the failing test no longer fails on the default branch after the commit the finding
names, or, for a failure from outside the repository, that the logs name the outside
cause and that the run the comment links as *Passed since* ran the same jobs.
When the evidence contradicts the finding, reopen the issue and comment
`/nexkit plan <what still needs changing, with the evidence>`. A blocked round closes its
issue only when its one question's recommended option is the one its `closes` names, so a
round that recommended waiting, such as for a pull request that is not merged, never
closes it.

### A pull request closed as `already_on_base` whose change is not on the base branch

NexKit closes a pull request this way only when GitHub shows it changes no file against
its base branch, so whatever it changed, its base branch has now. The round's comment
names the commit that brought the change when NexKit can tell one. When the issue still
needs a change, for example because that commit fixed only part of a CI failure, reopen
the issue and comment `/nexkit plan <what still needs changing, with the evidence>`.

### Conflict rounds used up

The pull request had `conflicts.max_rounds` automatic conflict rounds, and no more start
by themselves. Read the last one: when it was blocked on a choice, answer it with
`/nexkit fix <decisions>`. Otherwise comment `/nexkit fix` yourself, with any rule the
conflict needs, such as which side's numbering to keep. A round that ended as
`conflicting` is the same case: its commit conflicted with the base branch, so its checks
and review did not run, and no conflict round was left to start.

### A round ended as `stop_failed`

The base branch moved while the round ran its checks, so that they could not lead to a
merge, and NexKit stopped the round, but the round that should follow did not start: the
comment says why, such as an update of the branch that GitHub refused. Comment `/nexkit
fix` when the pull request conflicts with its base branch, otherwise `/nexkit review`
after merging the base branch into it.

### A round needs a manual merge

The merge the round needs, for a conflict in a protected path, is one NexKit cannot publish,
so the round stopped before its agent (see [above](#pull-requests-and-merges)):

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

### A plan is too large again

Autopilot already planned once more after a plan that was too large and suggested no
parts, or suggested parts titled like the issue (see [Autopilot](configuration.md#autopilot)),
and the second plan did the same; or the issue is three levels of parts deep, where
NexKit splits no further. Read the plan: its *What changes* and implementation notes
usually show the steps. Split the issue by hand into parts that each fit one session, with
the [`request` skill](../skills/request/SKILL.md), or make the issue smaller and comment
the change on it, which with autopilot plans it again. At three levels deep, make the
issue smaller rather than splitting it once more.

### A diagnosis round stopped as `blocked`

It found no cause in the evidence (see [Diagnosis
rounds](how-it-works.md#failure-handling)). Do not pick a cause without evidence. Read
what it found, the run log, the result files and the CI evidence the round downloaded;
its *Collect issue context* log lists it. When they show the cause, post
`/nexkit fix <the cause, with the evidence>`. Otherwise answer its question, or find the
cause first.

### A round failed because of its plan

When the run log or the result files show that the plan asked for the wrong thing, change
the plan as *Changing a plan* under [Stages](how-it-works.md#stages) says. For a
`/nexkit go` that stopped, comment the change on the issue: with autopilot, the next sweep
plans again with it; without, comment `/nexkit plan <what to change>`. On a pull request,
post `/nexkit fix <what to change>`.

### The plan's change is in protected paths

NexKit rejects a change to a protected path (see ["The change modifies protected
paths"](#pull-requests-and-merges)). A plan whose change needs one says so under *Needs a
hand-made change*, and autopilot starts no `/nexkit go` for it; a `/nexkit go` or fix
round that finds it needs one stops as `blocked` with the paths, and autopilot answers
none of its questions. A person makes the change by hand, exactly as the plan says, in a
pull request that closes the issue. NexKit's checks do not run on a pull request it did
not open, so run the repository's checks before merging it. When the plan also changes
code outside the protected paths, make the protected part first, then comment
`/nexkit plan` so the plan covers only the rest.

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

The *Restore caches* step lists each entry as missed. Check that a workflow on the default
branch uses [NexKit's cache action](configuration.md#caches), which `nexkit doctor` says,
and that it succeeds: it saves only at the end of a successful job. Its log says whether a
run saves or only restores. Hand-written `actions/cache` steps must repeat each entry's paths
and key files in the same order; the action needs none of that.

### A checklist item waits for a person's decision

The review found a [checklist item that a person decides](configuration.md#review-checklist)
not met, such as a change that accepts a risk. No fix round starts for it, since a fix
cannot meet it without undoing the change. Read the item and the review's evidence in the
round's comment. To accept it, approve the pull request: NexKit turns `nexkit/review` green
on that commit and, with `merge.auto`, merges it; with it off, merge it yourself. Approve
the commit the round reviewed; after a new commit, wait for its review and approve again.
To reject it, comment `/nexkit fix <what to change>`, or close the pull request.

### A review gave no answer for a checklist item

A review must answer every item of [`review.checklist`](configuration.md#review-checklist),
so it gave no verdict and nothing merged. Comment `/nexkit review`. When it happens again,
read the item in the run's prompt: an item that says two things at once or that the diff
cannot show is hard to answer, so reword it.

### The review job errored

NexKit already ran the review again after the waits of `retry.wait_minutes`, after a
transient error or a review that left no result. The round's comment says why. Comment
`/nexkit review` to try again, unless it names an authentication error: fix the review agent's secret first, since another try
stops the same way (see ["The AI review did not complete"](#agents)).

### The automatic merge was refused

The round's comment says why, and NexKit handles the refusals that pass (see
["Automatic merge was not possible"](#pull-requests-and-merges)). While a pull request
waits for required checks (`merge_waiting`), a required check that fails gets an
automatic fix round and a new commit gets `/nexkit review`, so nobody needs to post
either. It waits for a person only:

- when a required check failed and no automatic fix round is left: see [No automatic fix
  rounds left](#no-automatic-fix-rounds-left);
- when the checks neither passed nor failed within six hours: find out why from their
  runs, for example a workflow that never started on the branch, start it again, and
  comment `/nexkit review` once they can pass;
- when its issue left autopilot's scope, since only sweeps try the merge again: merge it
  yourself, or bring the issue back into the scope.

For any other rule, the round's comment and the mention say what unblocks the merge, under
**What unblocks it**. No merge by hand is needed after that: an approval by someone with
write access merges the pull request at once, and with autopilot, each sweep merges it once
GitHub reports it as mergeable, such as after a rule changed (see [Automatic
merge](configuration.md#automatic-merge)). An approval starts nothing while the workflow's
`pull_request_review` trigger lets only *Request changes* reviews through: update it as
`nexkit init` writes it, which `nexkit doctor` checks. NexKit reads the base branch's rules, the pull request's reviews,
files and conversations, and the repository's merge settings, and names:

- **a code owner's review**: the files that need it, each with its lines added and removed
  and its owners from the base branch's `CODEOWNERS`, leaving out a file whose owner
  approved; a code owner approves the pull request. NexKit's App cannot review its own
  pull request. A team counts as not approved, since NexKit cannot tell who is in it;
- **required approvals**: how many the rule wants and how many there are;
- **a review that requests changes**: its reviewer approves or dismisses it;
- **open review conversations**: resolve them;
- **a draft, a merge queue, signed commits, a required deployment, or squash merges turned
  off**: the step each needs.

Classic branch protection, which NexKit's token may not read, is named from GitHub's
review decision and its words. Any other refusal quotes GitHub's answer: merge the pull
request yourself or change the rule.

### An approved pull request has criteria the review could not verify

Check each listed criterion as [Reviewing a pull
request](how-it-works.md#reviewing-a-pull-request) says. When a test or a check could
show it, comment `/nexkit fix Add tests that show these criteria, so the review can
verify them: <criteria>`. When nothing can, the person who decides confirms it from the
diff and the check results, then merges.

### A run kept ending without a report

A sweep marked the run as ended without a report and started it again, at once and then
30, 60 and 120 minutes after the last start, or once more only when its agent had run to
its end, and each run ended the same way; or it was
a person's command, which NexKit never starts again (see [Runs that never
end](how-it-works.md#autopilot)). Read the workflow runs for why they ended, such as
cancelled runs, lost runners or jobs that GitHub never created, and check
[githubstatus.com](https://www.githubstatus.com) for an outage of GitHub Actions. Once
Actions works again, post the same command once more. When it ends without a report
again, find the cause in the Actions run before you try again.

### A command or round did not start

Autopilot starts a command that never ran again: at once when its workflow run ended, or
after `autopilot.lost_after_minutes` when that run cannot be read, then 30, 60 and 120
minutes after its last start. These starts cost no agent time when GitHub creates no job.
When the fifth start does not run either, the issue waits for a person, with a reason
that says GitHub created no job or ended the runs without a report, as in an outage of
GitHub Actions (see ["Autopilot does not start an issue"](#agents)). A continued run, and
an automatic round that a round started, whose run has not started after
`lost_after_minutes`, start once more, and wait for a person when that one does not start
either; a round from before NexKit recorded what it started waits six hours (see [Runs
that never end](how-it-works.md#autopilot)). Read the dispatched run in the Actions tab,
or the reply on the issue or pull request, for why it did not run, such as a refusal by
`route`, a run cancelled in the queue, or [an outage](https://www.githubstatus.com). Once
the cause is gone, post the command yourself, once.

### Evidence from a CI run is missing

NexKit downloads the evidence of the CI runs that an issue names (see [Evidence from CI
runs](configuration.md#evidence-from-ci-runs)), so never answer a question by
downloading or attaching a run's files. Read the round's *Collect issue context* log:

- When the run was not named, post the command again with the CI run's link in its note,
  such as `/nexkit fix <run link>` or `/nexkit plan <run link>`. Link the CI workflow's run, not a NexKit run: NexKit's
  own runs never count.
- When the download failed for a transient reason, post the command again.
- When the artifacts expired, the evidence is gone: reproduce the failure, or decide
  without it.

### Autopilot leaves an issue alone

Run `nexkit status`, which gives the reason (see ["Autopilot does not start an
issue"](#agents)). Autopilot never takes an issue opened by someone without write
access by itself, because its text would drive an implement session. A person with write
access reads it as untrusted text, and comments
[`/nexkit trust`](configuration.md#vouching-for-an-issue) on it when they trust it.

An issue that a workflow opened needs its workflow in `autopilot.trusted_workflows` and a
marker that checks out. The reason names what failed: a workflow that is not listed, a run
that does not exist, ran on another branch, was started by a pull request or was not running
when the issue was opened, or an edit by someone without write access. Fix the workflow or
the setting for the issues it opens next, and comment `/nexkit trust` on this one once you
have read it: see [Issues from a workflow](configuration.md#issues-from-a-workflow).

An issue that a writer vouched for is left alone again after an edit by someone without write
access. Read what changed, and comment `/nexkit trust` again if you trust it.

### An issue in a later wave does not start

`nexkit status` names the open issues of lower waves in its milestone that it waits for
(see *Labels* under [Autopilot](configuration.md#autopilot)). Each carries on by itself or
waits for a person, listed under *Needs a person*; once they are closed it starts. Without a
milestone it never starts: give it one, or take the label off.

### An issue or pull request shows as `held`

A label with `"hold": true` is on the issue, so autopilot starts nothing for it, and its
pull request gets no automatic round or merge. Remove the label when the issue may go on:
the next sweep starts what the hold kept back.

### An issue is blocked by an issue outside the scope

Autopilot does not work on the blocker, so the issue waits. With the `"milestone"` scope,
bring the blocker into the scope by giving it the scope's milestone. With `null`, only a
blocker in another repository is outside the scope: finish it there. Or remove the
*blocked by* relationship when the issue does not really need it.

### A parent's parts all closed and none was completed

NexKit leaves the parent open, since nothing it asked for was delivered (see [Parent
issues](configuration.md#parent-issues)). Decide whether the parent is done: close it, or
open new parts for what is left.

### A CI issue says no job failed

The run failed with no failed job: GitHub never created some of its jobs, which the record
names, or the run failed before any job did. NexKit ran its jobs again once and it
failed the same way, or another run did the same within the day. Check
[githubstatus.com](https://www.githubstatus.com) for an outage of GitHub Actions at the
run's time, and the run's page for GitHub's reason, such as a workflow file error, before
you look for a cause in the repository. When it was an outage, close the issue as not
planned. When the workflow file is the cause, a person fixes it, since `.github/` is
protected.

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
