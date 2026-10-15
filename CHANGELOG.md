# Changelog

## 3.1.0

- Runs show tokens the same way for every runtime, and no cost (#65). Every runtime
  reports `input` (cached input included), `cached` and `output` (reasoning included);
  Claude Code now reports its tokens too, over every model the session used. Cost and
  turns are no longer shown: Codex reports no cost, a subscription's cost is not what you
  pay, and a Codex turn is not a Claude Code turn. `max_budget_usd` is still a limit. The
  run summary is `Result | Duration | Tokens` (and `Verdict` for a review), and a round's
  comment is `Commit | Checks | AI review | Tokens`.
- A total is shown only when every run in it has a count: a round whose review reports no
  tokens shows `N/A`, not the implement stage's tokens as the whole. A table cell with no
  data shows `N/A` instead of `-`. The plan no longer adds triage's numbers to its own;
  triage has its own run summary.
- The plan comment names the agent and model of every stage, like the run comment's
  **Agents:** line, for example "**Profile: standard**: plan `claude` (`opus`) · implement
  `claude` (`sonnet`) · review `astra` (`gpt-6-astra`)".

## 3.0.0

- **Breaking:** every agent is declared in the new `agents` key, with the names of the
  secrets it uses, so the configuration names every secret NexKit reads. There is no
  built-in Claude agent any more, and a configuration without `agents` is rejected with
  the block to add. A `NEXKIT_PUSH_TOKEN` secret is used only when
  `publish.push_token_secret` names it (default `null`: the Actions token). **Upgrade:**
  see [Agents are declared](docs/configuration.md#agents-are-declared); `nexkit doctor`
  warns about a push token secret the configuration does not name.
- An agent is a runtime (the CLI) and a provider (where the model is served). `agent`
  chooses one at the top level, for a stage or in a profile, and layers like `model`;
  wherever `agent` is set, `model` is set next to it. Each runtime checks the settings of
  the stages it runs, such as an effort it does not know. See
  [Agents](docs/configuration.md#agents).
- New runtime `codex` with the provider `azure-openai`, for OpenAI models deployed on
  Azure AI Foundry. The endpoint and each deployment's key live in secrets, never in the
  configuration. Codex has no spending limit, so it rejects `max_budget_usd`, and the run
  summary shows its tokens instead of a cost. See
  [OpenAI models on Azure AI Foundry](docs/configuration.md#openai-models-on-azure-ai-foundry).
- Secrets can have any name except those starting with `GITHUB_` or `NEXKIT_`. Each job
  receives only the secrets of the agents its stages use, and each agent's process only
  its own; `setup` and check commands no longer see an agent credential. **Upgrade:**
  pass `secrets: inherit` to the pipeline in `.github/workflows/nexkit.yml`, as
  `nexkit init` writes it. A workflow that passes `CLAUDE_CODE_OAUTH_TOKEN` or
  `ANTHROPIC_API_KEY` one by one still works for agents that use those names. `nexkit
  doctor` checks, for each agent the configuration uses, that its secret is set and
  passed.
- New `fallback` key on an agent: other agents of the same runtime that take over when it
  stops at a usage limit, such as an Anthropic API key behind a Claude subscription.
  Errors, timeouts and `blocked` never move to a fallback. A stage always starts on its own
  agent; implement and fix restart from a clean checkout. When every agent is at its
  limit, the run pauses until the earliest reset. The run summary and the run comments
  name the agent of each stage. See [Fallback agents](docs/configuration.md#fallback-agents).
- Paused runs resume within about ten minutes of the reset instead of whenever GitHub runs
  the hourly schedule. When the reset is at most `usage_limit.wait_minutes` away (new key,
  default 330, 0 to turn it off), NexKit starts a run that waits for it; it uses no agent
  and no repository code, but holds a runner (billed minutes on private repositories). A
  run whose command carried on now shows ⏭️ `continued` instead of staying ⏸️.
- Only the `claude-code` runtime reports usage limits, so only its runs pause or fall back;
  an Azure rate or quota error ends a Codex run as an error.
- A structured result that does not fit its stage's schema is now an error, checked by
  NexKit before anything is published.
- The docs, the setup skill and the CLI's own text describe agents in general rather than
  Claude only. `nexkit init` names the secret to set next, and
  [Troubleshooting](docs/troubleshooting.md) covers the configuration errors that are new
  in this release.

## 2.1.0

- New `cache` key (default `[]`): caches to restore before `setup`, so the agent job (for
  `go` and fix rounds) and `verify` stop installing toolchains and dependencies from scratch
  in every round. Each entry has a `name`, the `paths` to restore (outside the repository,
  such as `~/.npm` or `~/.gradle/caches`) and the `key_files` whose contents decide the key
  `nexkit-<runner.os>-<name>-<hash>`; an older cache with the same prefix is used when the
  key has none. NexKit only restores: both jobs run code the agent wrote, so a cache they
  saved would reach every later run on the base branch. The repository's own workflow on
  the base branch saves the caches; see
  [Caches](docs/configuration.md#caches) for the key format and an `actions/cache` step
  that matches it. A missing or failed cache never fails a run, and the job log lists each
  entry as restored, restored from an older key, or missed.

## 2.0.0

- **Breaking:** the settings for what NexKit does by itself are grouped by feature.
  `max_auto_fixes` is now `fix.max_auto_rounds`, `auto_merge` is now `merge.auto`, and
  `after_merge_workflows` is now `merge.after_workflows`. A configuration with the old
  keys is rejected with a message that names the new ones; see
  [Keys renamed in this release](docs/configuration.md#keys-renamed-in-20).
- Values that were fixed in the code can now be configured: `limits.setup_timeout_minutes`
  (default 30) for each `setup` command, and `limits.max_patch_mb` (default 5) for the
  largest change a round may publish. The job timeouts allow for the setup timeout.
- After an automatic merge, NexKit closes the issue the pull request implements, as
  GitHub does when a person merges. GitHub left it open after a merge with the Actions
  token. NexKit comments a link to the pull request and the merge commit on the issue; an
  issue that is already closed stays as it is, and a failure to close it is reported in
  the round's comment without undoing the merge.
- New `close_parent_issues` key (default `false`): when the last open sub-issue of an
  issue closes, NexKit closes the parent as completed and lists how each sub-issue closed.
  When every sub-issue was closed as not planned, the parent stays open with a note for a
  person. NexKit then checks the parent's own parent. It works whether a person or NexKit
  merged the work. A new `maintain` job does this without an agent or a repository
  checkout. **Upgrade:** add `issues: {types: [closed]}` to the triggers in
  `.github/workflows/nexkit.yml` and `github.event_name == 'issues'` to the job's `if`, as
  `nexkit init` writes them; `nexkit doctor` warns when the setting is on and the trigger
  is missing.
- A run that stops at the Claude account's usage limit is paused instead of failed. Its
  comment shows ⏸️, the reset time and when NexKit runs the command again;
  `nexkit/checks` and `nexkit/review` stay pending. After the reset, NexKit runs the same
  command again, once, with the same note (a review that stopped after its commit was
  published resumes as `/nexkit review`). A resumed round does not count toward
  `fix.max_auto_rounds`, and a new command replaces a paused one. The comment used to blame
  the stage, for example "The AI review did not complete". New keys:
  `usage_limit.resume` (default `true`) turns the resume off, and
  `usage_limit.retry_minutes` (default 60) sets when to try again if the reset time is
  unknown; a run resumed that way that stops again waits for a person. **Upgrade:** add the
  hourly `schedule` trigger to `.github/workflows/nexkit.yml`, add `schedule` to the
  events in the job's `if`, and add `|| github.event_name` to its concurrency group, as
  `nexkit init` writes them; `nexkit doctor` warns when the trigger is missing.
- New `conflicts.auto_resolve` key (default `false`): when the base branch moves, NexKit
  starts an automatic fix round, `fix (auto, conflicts)`, for each of its open pull
  requests that now conflicts, with the note to merge the base branch and keep both sides.
  NexKit's own merges, pushes to the default branch and the hourly schedule start the
  check, and an automatic merge refused because of a conflict starts the round too. A
  conflict that needs a choice still ends as `blocked`. Each pull request gets at most
  `conflicts.max_rounds` such rounds (default 3), apart from `fix.max_auto_rounds`, one
  per base commit, and none while a round is running on it; a round whose branch no longer conflicts ends without running Claude.
  **Upgrade:** add `push: {branches: [<default branch>]}` to the triggers and `push` to
  the events in the job's `if`, as `nexkit init` writes them; `nexkit doctor` warns when
  the trigger misses the default branch.
- New `notify` key (default `[]`): GitHub logins that NexKit mentions when a run ends in a
  state that needs a person, such as a plan with questions, a blocked or failed run, no
  automatic fix rounds left, a pull request ready for a person while `merge.auto` is off,
  or a refused automatic merge. The choice follows the run's outcome only. The mention is
  one line in a new comment right after the run's comment, because GitHub does not notify
  for mentions added by an edit. An automatic merge that GitHub refuses now ends as
  `merge_refused` instead of `ready`. `nexkit doctor` warns about a listed login without
  write access.
- New local command `nexkit status [--json]`: one line per open issue NexKit worked on
  (planning, planned with the plan's link and profile, implementing, paused, failed) and
  per open NexKit pull request (running with its round, ready, blocked, failing,
  conflicting, merge refused, paused, failed), each with the link to its last round; the
  pull requests NexKit merged recently, newest first by merge time, with the state of each
  `merge.after_workflows` run; and a last section with only the items that need a person,
  and why. States come from NexKit's markers, which now also record each run's outcome,
  the reason a person is needed and the started after-merge runs. It reads GitHub with
  the `gh` login or `GITHUB_TOKEN` and changes nothing.

## 1.10.0

- Checks can tell which files a pull request changed, so a slow check can skip work that
  the change cannot affect. Setup and check commands get `NEXKIT_BASE_SHA`, the merge
  base of the commit being checked and the base branch, in the `verify` job and when
  Claude runs the checks. `verify` now checks out the full history (with its read-only
  token, as before), so this works in private repositories too. When the base is unknown
  the variable is unset and the log says so: run everything then. See
  [Configuration](docs/configuration.md#skipping-work-that-a-change-cannot-affect).

## 1.9.0

- `auto_merge: true` can now merge. The `report` job merges with the default Actions
  token, which needs `contents: write`; it had only read access, so every automatic merge
  was refused. Branch protection still applies: a refused merge is explained in the
  round's comment, as before.
- New `after_merge_workflows` key (default `[]`): workflow files, such as `["ci.yml"]`,
  that NexKit starts on the base branch with `workflow_dispatch` after it merges. GitHub
  starts no `push` workflow for a merge made with the Actions token, so the base branch's
  CI did not run after an automatic merge. The round's comment links each started
  workflow or says why a start failed; a failed start does not undo the merge.
- `nexkit doctor` warns when a workflow in `after_merge_workflows` is missing or has no
  `workflow_dispatch` trigger.

## 1.8.0

- A re-plan revises the latest plan instead of starting over. The plan agent gets the
  latest plan and the discussion since it, keeps what nothing asks to change, and applies
  what is asked. The plan comment's *Since the last plan* section lists what changed. A
  note such as `/nexkit plan from scratch` plans again without the latest plan's choices.
- A plain comment no longer cancels a waiting `/nexkit` command: `nexkit init` sets
  `concurrency` on the `nexkit` job. In repositories set up earlier, move that block in
  `.github/workflows/nexkit.yml` into the job; `nexkit doctor` reports it.

## 1.7.0

- Profiles choose the models for each issue. A repository can define `profiles` in
  `.nexkit/config.json`, each with a `when` text that says which issues belong in it and
  the same stage settings as `stages`. At the start of `/nexkit plan`, a triage step, one
  Claude call without tools, reads the issue and its discussion and chooses a profile. The
  plan runs on that profile's settings, and implement, fix and review rounds use the
  profile of the latest plan. A collaborator changes the profile with a comment and a
  re-plan, for example `/nexkit plan Use the hard profile`. A re-plan keeps the profile
  unless a collaborator asks for another one.
- The plan comment shows the profile, its models and why it was chosen. When triage
  fails, the plan runs on the previous plan's profile or on `default_profile`, and says
  so. A round whose plan names a profile that is no longer configured stops and asks for
  a re-plan.
- A configuration without `profiles` works as before.

## 1.6.0

- The review sees why an implementation leaves the plan. Its prompt now has the
  collaborators' discussion on the issue and the pull request, including notes on
  `/nexkit` commands, and the *Outside the plan* section of the pull request description.
  A change a collaborator asked for after the plan, for example while approving it, is no
  longer blocked as a deviation, so an automatic fix round no longer reverts it.

## 1.5.1

- An implement round no longer fails when the base branch moves while the agent works.
  `publish` opens the pull request on the base commit the agent started from, so the
  patch always applies; the pull request may then be behind its base branch, and
  `/nexkit fix` brings it up to date. When that commit is no longer on the base branch,
  for example after a force push, publish stops with a clear message instead of a
  `git apply` error.

## 1.5.0

- 🛑 marks only blocking findings. In a follow-up review, a suggestion that is still open
  shows as "💡 Suggestion, still open" instead of "🛑 unresolved", and a blocking finding
  that is still open shows as "🛑 Blocking, unresolved". The review result has a new
  `severity` field for each previous finding, copied from the previous review.
- `/nexkit fix` resolves conflicts with the base branch. When the pull request no longer
  merges cleanly, the fix round merges the base branch and the agent resolves the
  conflicts, together with any other feedback. NexKit pushes a merge commit whose parents
  are the branch head and the exact base commit merged, never with force. The round's
  comment says that the base branch was merged and lists the files that had conflicts.
- A conflict that needs a choice the plan, the issue and the feedback do not settle ends
  the round as `blocked`. The round's comment lists each open conflict with what each side
  changed and the question to answer; `/nexkit fix <your decision>` answers it. The fix
  result has a new `open_conflicts` field.
- The review after a merge gets the conflicted files and what each side changed in them,
  and checks that both sides survived. Its "changes since the last review" diff leaves out
  the changes the merge brought from the base branch.
- The protected-path check ignores changes that the base branch brings through a merge.
  Conflicts in protected paths stop the round before Claude runs. A merge that brings
  workflow changes stops with a clear message when only the default Actions token can
  push.
- The `agent` job checks out the full history for fix rounds.

## 1.4.0

- Plans, reviews and pull request descriptions are written for people. Every prompt has
  the same writing guide: plain English, short sentences, one idea per bullet, and
  behaviour before class and file names.
- Plan comments show *What changes*, *Decisions to check*, *Risks and limits* and
  *Acceptance criteria*, and keep the detail for the implementing agent in a collapsed
  *Implementation notes* list. The plan result has the fields `changes`, `decisions`,
  `risks` and `implementation_notes` instead of `approach`. The implementing agent still
  receives the whole comment.
- Reviews list the findings first, blocking ones before suggestions, then one line per
  acceptance criterion with ✅ or ❌ and the test that covers it. The evidence is collapsed.
  Review criteria have a new `test` field.
- Pull request descriptions have a fixed order: *Summary*, *What changed*, *How it is
  tested*, then *Outside the plan* and *Notes for the reviewer* when needed. The implement
  result has the fields `changes`, `testing`, `outside_plan` and `reviewer_notes`.

## 1.3.1

- The run summary table has a header with the column names (`Result`, `Turns`,
  `Duration`, `Cost`, and `Verdict` for reviews) and one row of values, instead of an
  empty first row.
- `report` downloads only the artifacts of the stages that ran, so skipped stages no
  longer add *Artifact not found* error annotations. `verify` uploads `nexkit-checks` even
  when `nexkit checks` fails, so a missing-artifact error on `report` now always means a
  stage stopped before its upload.

## 1.3.0

- Secrets are redacted from everything NexKit prints or stores from an agent session: the
  Actions log, transcripts, `result.json`, the run summary and `setup` output in the
  agent job. Redacted: values of Claude and GitHub credentials and of environment
  variables named like secrets, known token shapes (Anthropic, GitHub, JWT, bearer
  headers, PEM private keys) and passwords in connection strings. Git SHAs, UUIDs and
  hashes are kept.
- New `transcript` setting (default `true`): `false` stops writing transcripts into the
  `nexkit-agent` and `nexkit-review` artifacts.
- The agent and review logs show each tool call as a collapsible group, titled with the
  elapsed time and turn, holding its input, whether it failed and its output (first and
  last 20 lines, at most 4,000 characters). Failed tool calls add a warning annotation.
  Tool output is printed with workflow commands stopped, so it cannot set outputs, add
  masks or create annotations. `log.tool_output: "none"` keeps the one-line log.
- The agent and review jobs write a run summary: result, turns, duration, cost, files
  changed, commands run and, for reviews, the verdict and findings.
- The artifacts contain a readable `transcript.md` next to `transcript.jsonl`.

## 1.2.0

- Every run gets its own NexKit comment, posted when it starts, right after the command
  or review that started it: one per `plan` or `go` run on an issue, one per round on a
  pull request. `report` finds the comment by a hidden marker that holds the run URL and
  edits it with the outcome, or posts a new one if it was deleted. Earlier runs' comments
  are no longer edited.
- A round's comment shows its commit, checks, AI review, cost and the next step, which
  was a separate comment before. The pull request state stays in one state comment,
  edited in place, that no longer has a rounds table.
- Status comments from 1.1.0 are left as they are. NexKit reads the state of a 1.1.0 pull
  request and continues it in a new state comment.

## 1.1.0

- Every accepted command gets 👀, on pull requests too: `route` now has
  `pull-requests: write`. A failed reaction is logged instead of ignored.
- Runs are visible while they run. A command on an issue gets one NexKit status comment,
  edited in place, with a link to the run and then its outcome. A pull request round
  appears in the status comment as running, with the run link, and `nexkit/checks` and
  `nexkit/review` turn `pending` on the current commit.
- The command comment gets 🚀 when the run finishes its work and 😕 when it stops on an
  error.
- A review after an earlier NexKit review gets the previous round: the earlier findings,
  the fix rounds since (summary and note) and the diff since the reviewed commit. It
  reports a `resolution` for each earlier finding (new `previous_findings` field in the
  review result), accepts justified rejections, and treats changes asked for in a note as
  in scope.
- The review prompt names the base branch of its diff (it showed `$base`).

## 1.0.0

First release.

- Issue commands `/nexkit plan` and `/nexkit go`; pull request commands `/nexkit fix` and
  `/nexkit review`; a *Request changes* review starts a fix round.
- One reusable workflow with separated jobs: Claude runs without GitHub write access,
  publishing never runs repository code, and checks run without secrets.
- Independent read-only Claude review against the plan's acceptance criteria and the check
  results, posted as a pull request review and `nexkit/checks` / `nexkit/review` statuses.
- Bounded automatic fix rounds for failing checks and blocking findings; agent errors and
  blocked results stop and ask for a person.
- State kept in GitHub comments; no state branch or external storage.
- `nexkit init` and `nexkit doctor`; Claude Code plugin with `setup`, `request` and
  `status` skills.
- Runs on GitHub-hosted `ubuntu-24.04` with Claude Code 2.1.285 and Python 3.12, both
  installed by the pipeline; the CLI needs Python 3.12 or newer. A weekly workflow proposes
  new stable Claude Code versions, tested with a live smoke test before release.
- Works with a Claude subscription token or an Anthropic API key.
