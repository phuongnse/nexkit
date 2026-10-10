# Changelog

## Unreleased

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
