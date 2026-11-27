# When a run fails

This page covers what NexKit does when a run fails, and which failures it retries. It follows [How it works](how-it-works.md), which holds the jobs of a run and what you see during one.

## Failure handling

| What happened | NexKit does |
|---|---|
| Comment author lacks write access, unknown command, closed issue, any command on a fork's pull request, or a command other than `/nexkit review` on a person's pull request (see *A person's pull request* under [Stages](stages.md#stages)) | Replies with the reason; no agent runs. `/nexkit trust` from someone without write access changes nothing. |
| `/nexkit go` while a NexKit PR for the issue is open | Replies: use `/nexkit fix` there or close it. |
| The issue closes while its plan or `/nexkit go` runs | Publishes nothing: no branch, pull request or plan (`issue_closed`). No later automatic round starts for it. |
| The issue closes before its approved pull request is merged | Does not merge it, and leaves it open for a person (`merge_issue_closed`). |
| A person merges or closes the approved pull request before NexKit's merge, such as an owner who approves and merges at once | Merges nothing and mentions nobody (`pull_closed`), also when GitHub refused NexKit's merge because the other one landed first. After an approval or at a sweep, the round's comment stays as it was. A pull request reopened after a `pull_closed` round waits for a person: an approval merges it. |
| Setup command fails before the agent | Reports the failing command and output on the issue or PR. NexKit counts the failure as a transient error, since a registry or mirror may have been down, so it runs the command again after each wait of [`retry.wait_minutes`](merges-and-waits.md#transient-errors), as the transient-error row below says. |
| Installing the agent's CLI fails, as when its installer cannot fetch the pinned build | Tries the install again after 10 and 30 seconds, three tries in all. When every try fails, the job fails: in an agent or review job that counts as the agent job failing as a whole, below. |
| The agent runs out of time or errors | Reports it; publishes nothing; no automatic retry. |
| The agent returns no result, or one that does not fit the stage's schema even with the empty values of the fields it left out | Asks the same session once for its result alone, with what was wrong, keeping its changed files; a result that fits then goes on as usual. When none comes, stops as after a transient error, below. See *A result that does not fit* under [Agents](agents-and-profiles.md#agents). |
| The agent stops on a transient error: an overloaded or failing API, a rate limit that is not a usage limit, a 403 that never reached the API because something in front of it turned the runner away (such as Cloudflare's challenge page for its IP address; the API's own refusal of a key comes with its request id and waits for a person), a lost connection, or a CLI that exited before the model answered; or the agent job fails as a whole, for example on a lost runner | Reports it, publishes nothing, and runs the same command again with the same note after the next wait of [`retry.wait_minutes`](merges-and-waits.md#transient-errors), by default 15, 30 and 60 minutes after the first, second and third such stop. Until then the comment shows ⏸️ with the time it runs again and which run again it is, such as 2 of 3, and from the second on the errors so far; `nexkit status` lists it as `retrying`, not under *Needs a person*. The run again is shown as `(retry N)`; once it starts, the comment shows ⏭️. A wait holds no agent runner: the run that waits for paused commands starts it, as after a [usage limit](work-without-a-command.md#usage-limits), and the hourly schedule starts what that run missed. A wait of 0 starts it at once. The runs again count against neither `fix.max_auto_rounds` nor `conflicts.max_rounds`. A stop after the last wait waits for a person, and its comment lists every error; with `[]`, the first stop does. A new command on the same issue or pull request starts only when the engine takes it on the item's stored state, so a `/nexkit go` or `/nexkit plan` is refused with the engine's reply and the wait goes on. A job a person cancelled is never run again. |
| GitHub creates no job for a run, or ends it without a report, as in an outage of GitHub Actions | With autopilot, a command autopilot started, or an automatic round, starts again at once, then 30, 60 and 120 minutes after its last start; these starts cost no agent time when no job runs, and the fifth that does not run waits for a person, with a reason that names the outage. A round or continued run whose next run never starts gets that run started once more after `autopilot.lost_after_minutes`. A person's command waits for them. |
| The agent stops at the account's usage limit | Runs the stage again on its fallback agents, if any. When all of them are at their limits, pauses the run with the earliest reset time; publishes nothing from that stage; runs the command again after the reset, unless `usage_limit.resume` is `false`. |
| An agent's secret is missing or invalid | Reports it as an error and publishes nothing; a fallback agent without its secret is skipped instead. |
| Agent returns `blocked` | Reports its reason and questions; no automatic retry. A person's answer that picks the option that only closes the issue closes it as not planned, with no agent run. A person decides, or with `autopilot.answer: "recommended"` autopilot answers when every question has a recommended option, once in a row: when the round its answers started asks again and no person commented since, a person decides. When a blocked `/nexkit go` found that no change is needed, that answer closes the issue with the round's finding, without another round. When its recommended option changes the issue's scope (`replans`), such as a split, autopilot answers with `/nexkit plan`, not `/nexkit go`. After a blocked or failed `/nexkit go` that it does not answer, autopilot plans again once a collaborator commented or an issue that blocks it closed since the round started. |
| Triage fails or names an unknown profile | Plans on the previous plan's profile, or `default_profile`; the plan comment says so. |
| The latest plan's profile is no longer configured | Implement, fix and review stop before any job runs and ask for a re-plan. |
| Patch touches a protected path | Rejects the whole change and says which paths. |
| The base branch moved while an implement round was running | Opens the pull request on the commit the agent started from; `/nexkit fix` brings it up to date. When the base branch's rules want pull requests up to date and its tip merges cleanly, `publish` merges it after the round's commit, so the checks run on a head that can merge; a fix round does the same. |
| The base branch moved while an implement or fix round was running, and its tip conflicts with the round's commit | With `conflicts.auto_resolve`, pushes the commit, skips its checks and review, and starts the conflict round at once, within `conflicts.max_rounds`; with those used up, a person decides (`conflicting`). |
| GitHub answers `publish` with a server error (5xx), a rate limit (429, or a 403 that names a rate limit) or a dropped connection: a push, a fetch, opening the pull request, posting the plan | Tries again after 10, 30 and 60 seconds, four tries in all; a refusal that another try cannot change, such as a protected branch, a push that is not a fast-forward or a missing permission, fails at once. A pull request that GitHub opened, or a comment it posted, on a try it answered with an error is used as it is, so nothing is posted twice. When every try fails, or the `publish` job ends without its outcome, the round stops as after a transient error: NexKit runs the command again after the waits of `retry.wait_minutes`, and a stop after the last waits for a person. Every other GitHub API call that NexKit makes tries twice more on the same errors, after 2 and 4 seconds. |
| The commit the implement agent started from is no longer on the base branch (force push) | Refuses to publish; run `/nexkit go` again. |
| The PR branch moved while a fix round was running | Refuses to publish. An automatic round runs once more on the new head; after a person's round, run `/nexkit fix` again, since the push was probably theirs. |
| The PR conflicts with its base branch | The next fix round merges the base branch and resolves the conflicts; a conflict that needs a choice ends the round as `blocked` with the questions. With `conflicts.auto_resolve`, NexKit starts that round itself, at most `conflicts.max_rounds` times per pull request. |
| The PR conflicts with its base branch in a protected path | Stops before the agent runs; merge the base branch yourself. |
| GitHub refuses the automatic merge because the base branch was modified meanwhile, for example by another part of a split merging at the same time | Tries the merge again, three times in all, a little later each time. |
| GitHub refuses the automatic merge because the head branch was modified, after a push to the branch since the round checked it | Starts `/nexkit review` on the new head, which checks, reviews and merges it as usual; nobody is mentioned. |
| GitHub refuses the automatic merge because required status checks have not passed yet, such as a CI workflow slower than the NexKit round | For an issue in autopilot's scope, ends the round as `merge_waiting`: each sweep tries the merge again while the head is the one approved, and merges once the checks pass, with everything a merge leads to. When GitHub refuses it because a required check failed, the sweep starts an automatic fix round, like failing checks after any round: it counts toward `fix.max_auto_rounds`, its feedback names the failed check with the commands that failed in its job and the tests its error annotations name, and it merges the base branch first when it has moved. A check that fails as the default branch does while autopilot's `bug` issue about that is open waits instead (`ci_waiting`, see *Failing checks the default branch has* under [Stages](stages.md#stages)), and with no automatic round left a person decides. A new commit on the branch gets `/nexkit review`, as when the head moved, which checks, reviews and merges it as usual. Checks that neither pass nor fail within six hours wait for a person. Otherwise a person merges it. The `maintain` job's token has `contents: write` for this; it never checks out the repository. |
| GitHub refuses the automatic merge because a required check failed, such as a CI workflow that failed before the NexKit round ended | Starts an automatic fix round at once, as when a required check fails while the merge waits for it: it counts toward `fix.max_auto_rounds`, its feedback names the failed check, and it merges the base branch first when it has moved. A check that fails as the default branch does while autopilot's `bug` issue about that is open waits instead (`ci_waiting`), and with no automatic round left a person decides. |
| GitHub refuses the automatic merge because the branch is behind its base branch, under protection that requires it to be up to date | GitHub may say so as required checks that are "expected" (such as "2 of 2 required status checks are expected."): it counts checks only on a head that contains the base branch's tip, so checks that passed on the head never pass. When a refusal for required checks that have not passed comes with GitHub reporting the pull request as `behind`, NexKit counts it as behind, also at a sweep's try of a merge that waits (`merge_waiting`), however long it waited. With `conflicts.auto_resolve`, updates the branch from the base branch on GitHub and starts `/nexkit review` on the new commit, which checks it and merges it as usual, at most `conflicts.max_rounds` times per pull request. When the pull request's own change is the one the review approved, the approval carries over and no review agent runs; otherwise it is reviewed again (see *A pull request brought up to date* under [Stages](stages.md#stages)). When the update leaves the pull request without a change of its own, the review does not run, and NexKit closes it as `already_on_base`. The update starts the repository's own CI, so the merge waits for its required checks as usual. |
| A pull request changes no file against its base branch any more, because another pull request landed the same change there | Before each round, review or merge try, closes the pull request without merging and its issue as completed (`already_on_base`), naming the commit that brought the change when it can tell one. No round runs. See *A pull request brought up to date that changes nothing* under [Stages](stages.md#stages). |
| The base branch moves while a round runs its checks, and its head now conflicts with it, or lacks its tip while the base branch's rules want pull requests up to date | Stops the round's run and starts the conflict round, or an update of the branch and `/nexkit review`, at once (`superseded`). Never a round in another job. See *Stopping a round in its checks* under [Work without a command](work-without-a-command.md#work-without-a-command). |
| GitHub refuses the automatic merge for another reason (required approvals, a merge method the repository does not allow) | Says why in the round's comment; the pull request waits for a person, and `notify` mentions them. |
| A workflow of `merge.after_workflows` cannot be started | Says why in the round's comment; the merge stays. |
| The issue cannot be closed after an automatic merge | Says why in the round's comment and links the issue; the merge stays. |
| Checks fail or the review requests changes | Starts an automatic fix round while `fix.max_auto_rounds` remain, otherwise asks for a person. After failing checks, that round first merges the base branch when it has moved, so a failure the base branch already fixed costs no agent change. |
| Checks fail only in commands, and tests, that fail on the default branch, while autopilot's `bug` issue about that is open | Starts no fix round: the round ends as `ci_waiting` and uses no automatic round. Once the workflow passes on the default branch again, or the `bug` issue is closed, a sweep starts the automatic fix round, which merges the default branch first. See *Failing checks the default branch has* under [Stages](stages.md#stages). |
| A check fails the same way before and after a fix round | The next automatic fix round is a diagnosis round, shown as `fix (auto, diagnosis)`: the agent names the cause, with evidence, before it changes code, then fixes that cause. Without a cause it changes nothing and ends as `blocked` with a question for a person; a change without a cause is rejected and publishes nothing. |
| The review itself fails | Sets `nexkit/review` to error and says why in the round's comment. A transient error, or a review job that left no result, runs `/nexkit review` again after the waits of `retry.wait_minutes`, with `nexkit/review` pending meanwhile; otherwise comment `/nexkit review` to retry, after fixing the agent's secrets if it was an authentication error. |
| NexKit itself fails in the `agent` or `review` job | Reports `NexKit stopped on <exception>` like an agent error; the job log has the traceback. |

**Why a merge was refused.** GitHub documents neither the wording of its refusals nor
how it quotes check names, so NexKit tells the refusals above apart from GitHub's data:

- *The head moved:* a response of 409, or a pull request whose head is not the approved
  commit.
- *Another merge at the same moment:* the base branch's tip moved during the try; the
  merge is tried again.
- *Behind its base:* GitHub reports the pull request as `behind`. NexKit asks again for a
  short while when GitHub has not worked it out yet (`unknown`).
- *Required checks:* those that the base branch's rulesets and classic branch protection
  name, against the check runs and commit statuses of the head, and those that GitHub
  marks as required on the pull request. A check that failed is named from that data;
  one that has not reported, or still runs, makes the merge wait.

NexKit's token may not read classic branch protection, and GitHub marks a check as
required only once it has reported. Only for a required check that has not reported in
that case does the text count: when GitHub holds the pull request back (`blocked` or
`behind`) and says its required checks are expected or pending, the merge waits.
Rulesets name every required check, so a repository that uses them needs no text at all.

**Diagnosis rounds.** A fix round that tries one change after another without the cause
spends its time and the automatic rounds on guesses. So when a check fails the same way
in two rounds in a row, NexKit asks for the cause first.

- *The same failure* is the same check with the same failure signature: the first line of
  the check's output that reports a failure (it contains `fail`, `error`, `✘` or `✗`, in
  any case, such as `FAIL`, `AssertionError` or `2 tests failed`), or the last non-empty
  line when none does. UUIDs, hex ids, durations and other numbers are removed and runs of
  whitespace become one space, so the same failure in another run keeps its signature.
- `report` compares the failing checks that a fix round was asked to fix with the checks
  after it. When a check fails with the same signature in both, the state comment records
  it, and `route` makes the next automatic fix round a diagnosis round. A round that a
  person starts with `/nexkit fix` follows that person's note instead, and a conflict
  round only merges the base branch.
- The diagnosis round runs in one session: its prompt lists the repeated failures and
  asks for the cause, with the evidence that shows it (the check output, the
  [result files](checks.md#result-files-of-failing-checks) the checks kept, the
  code), before any code change, and then for a fix of that cause. The agent writes the
  cause in `cause`.
- A diagnosis result that returns `done` with an empty `cause` and its own code changes is
  rejected as an error, and nothing is published. Changes that a merge of the base branch
  brought, and the resolution of conflicted files, do not count as its own.
- A diagnosis result with an empty `cause` and no changes ends as `blocked`, with what the
  evidence shows as its reason and the agent's questions, or else a question that asks a
  person how to go on. Like any blocked result, it publishes nothing and never starts
  another automatic round. The round itself counts toward `fix.max_auto_rounds` like any
  automatic round, because NexKit counts a round when it starts it; its blocked end uses
  no further round.
- When the round merged a base branch that had moved, and the agent changed nothing
  itself, the merge is published alone, as in any round after failing checks (see
  *Failing checks the base branch fixed* under [Stages](stages.md#stages)): the
  base branch may have fixed the failure, and the next checks show whether it did.
- Repeated review findings do not start a diagnosis round: the review already tracks them
  through its *Previous round* section, and only checks give a failure signature to
  compare.

Automatic rounds only follow concrete check failures or review findings. Agent errors and
blocked results always stop, apart from what [autopilot](work-without-a-command.md#autopilot) answers or splits, so
model usage is never spent repeating a problem the agent
cannot fix. A run paused at the usage limit is not a retry: it continues the same command
once the limit has reset. A run again after a transient error repeats a run that stopped
for a reason outside the work, before it published anything.

The reasons are in [D6](decisions/d6-agent-errors-never-retry.md).
