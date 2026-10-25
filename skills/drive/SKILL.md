---
name: drive
description: Keep NexKit's issues and pull requests moving where autopilot stops for a person, in an unattended session on a schedule (such as a Claude Code routine), within the limits the repository's own instructions set. Use when a scheduled session should play the person who decides while they are away. Not for an attended session, which uses the status and request skills, and not for agents inside the NexKit pipeline.
---

# Drive NexKit while nobody watches

Autopilot moves each issue on by itself and stops, on purpose, where a person is needed
(see [Autopilot](../../docs/how-it-works.md#autopilot)). This skill plays that person, in
a session that runs on a schedule, within the limits that the repository sets. It does
one pass over the issues and pull requests in autopilot's scope, then waits and looks
again.

Each stop below links the [Troubleshooting](../../docs/troubleshooting.md#when-nexkit-waits-for-a-person)
entry that says what a person does there. Apply it, with the bounds this skill adds. What
NexKit does by itself is in its docs, not here: read them at the release the repository
pins.

## What the repository decides

The repository's own instructions for the driver, such as the routine's prompt or a skill
of its own, decide:

- which choices count as confirmed in advance by the person who decides, so the driver
  may take them, and which it never takes, such as approving, merging, or a choice that
  rests on a design decision still open;
- whether it may push hand merges to NexKit's branches and open hand-made pull requests,
  and on which branch. When the pull request of the driver's own branch has merged, start
  that branch again from the default branch;
- what it runs before it pushes;
- whom it notifies, through what, and for which cases besides those under
  [Notify](#notify).

Where the instructions say nothing, the driver does not act and notifies instead.

## Setup

1. Check out the default branch, up to date, and read the repository's instructions for
   the driver.
2. Read `nexkit_repository` and `nexkit_ref` in `.github/workflows/nexkit.yml`. At that
   ref, read this skill and the docs it links: in How it works, [Who does
   what](../../docs/how-it-works.md#who-does-what), [One run at a
   time](../../docs/how-it-works.md#one-run-at-a-time),
   [Autopilot](../../docs/how-it-works.md#autopilot) and [Failure
   handling](../../docs/how-it-works.md#failure-handling); the configuration's
   [Autopilot](../../docs/configuration.md#autopilot); and Troubleshooting, [When NexKit
   waits for a person](../../docs/troubleshooting.md#when-nexkit-waits-for-a-person). The
   [`status` skill](../status/SKILL.md) shows how to run the pinned release's CLI and read
   its docs.
3. Run `nexkit status --json` from the pinned release. Its `needs_person` items, and the
   autopilot items that wait with a person needed, are the stops to act on, each with its
   reason. The CLI needs Python 3.12 or newer; when `python3` is older, use a newer
   `python3.x`. When it cannot run, read NexKit's run comments; [State](../../docs/how-it-works.md#state)
   names what they hold.
4. Use `gh` or the GitHub REST API for comments, issues, sub-issues, *blocked by*
   relationships, mergeability and workflow dispatch, or a GitHub MCP server's tools where
   the session has them, with REST for what they lack. In a cloud session, `gh api` can
   work through the session's proxy even when `gh auth status` says it is signed out: try
   the call.
5. NexKit's queued and running runs are the runs of `nexkit.yml` whose status is `queued`
   or `in_progress` (`gh run list --workflow nexkit.yml --status in_progress`).
6. Post as a person with write access, so that NexKit counts the driver's comments as a
   collaborator's (see [Evidence from CI
   runs](../../docs/configuration.md#evidence-from-ci-runs)).

## Principles

- Keep every issue and NexKit pull request in scope moving until it is merged or closed.
  Act only where NexKit waits for a person: what it carries on by itself needs nothing.
- Never guess a cause. A cause counts only when a run log, a trace or a result file shows
  it.
- NexKit stops on purpose where a cause is missing: a diagnosis round that found none, a
  second time-out, a part three levels deep. Do not route around those stops by splitting
  or by answering with a guess.
- One command per state. Never post a command on an issue or pull request while a NexKit
  run for it is queued or running, and never post the same command twice for the same
  state. When a command of the driver's did not run (see [One run at a
  time](../../docs/how-it-works.md#one-run-at-a-time)), post it again once the run ahead
  of it has ended.
- Do not post `/nexkit plan` or `/nexkit go` on an issue whose autopilot state is `start`
  or `active`, nor where a comment without a command lets autopilot plan again (see the
  configuration's [Autopilot](../../docs/configuration.md#autopilot)). Start a sweep
  instead, at most one every 10 minutes:
  `gh workflow run nexkit.yml -f command=autopilot -f number=autopilot`.
- Write one comment per issue or pull request per pass, with every answer and at most one
  command, since each comment by a collaborator can lead to a new plan.
- Do not review plans or merged pull requests. NexKit's review checks every round, and a
  person's review belongs to an attended session (see [Who does
  what](../../docs/how-it-works.md#who-does-what)).
- NexKit's issues for CI failures and flaky runs are normal work, not duplicates.
- Go through the issues in autopilot's order, oldest first within it.

## At each stop

Act on an item when it waits for a person and no newer command started for it. Leave
what NexKit still carries on by itself, such as a pending `(retry)` (⏭️), a `merge_waiting`
or `waiting` pull request that needs nobody, or a command autopilot started that has not
run yet. Re-read the item's latest comments first. Then apply its entry, within these
bounds:

- **[Questions](../../docs/troubleshooting.md#a-question-without-a-recommended-option-or-asked-again):**
  answer with the recommended option, or from the plan, the issue and the repository's
  docs where the repository lets the driver take that choice, in the form the entry gives.
  Answer the same question at most twice per issue, then notify. When the answer is to
  wait for another issue, mark the issue *blocked by* it with its reason line. A diagnosis
  round's question belongs to the diagnosis stop below.
- **[A transient failure after NexKit's retry](../../docs/troubleshooting.md#a-run-stopped-twice-on-a-transient-error):**
  post the command once more only when the log shows the cause has passed; otherwise
  notify.
- **[`recheck_failed`](../../docs/troubleshooting.md#a-round-ended-as-recheck_failed):**
  comment `/nexkit review` once when the service works again, or `/nexkit fix` with the
  evidence when the failure comes from the code. When the service is still down, notify.
- **[A time-out that NexKit leaves waiting](../../docs/troubleshooting.md#an-implement-or-fix-round-ran-out-of-time-again):**
  never split it again. Act only on a cause that the evidence shows; otherwise notify.
- **[A plan that is too large again](../../docs/troubleshooting.md#a-plan-is-too-large-again):**
  split the issue by hand with the [`request` skill](../request/SKILL.md) only when the
  plan's steps give parts that each fit one session, and never at three levels deep;
  otherwise notify.
- **[No automatic fix rounds left](../../docs/troubleshooting.md#no-automatic-fix-rounds-left):**
  post one round: the cause first when the same check keeps failing the same way, or the
  review's blocking findings and unmet criteria. When that round leaves the pull request
  in the same stop, notify.
- **[Criteria the review could not verify](../../docs/troubleshooting.md#an-approved-pull-request-has-criteria-the-review-could-not-verify):**
  when a test or a check could show them, post the round that adds those tests, once per
  pull request. When nothing can show a criterion, or a later approved round still leaves
  one unverifiable, notify with what the diff and the check results show for each.
- **[The review job errored](../../docs/troubleshooting.md#the-review-job-errored):**
  comment `/nexkit review` once. For an authentication error, notify.
- **[A refused merge](../../docs/troubleshooting.md#the-automatic-merge-was-refused):**
  only what NexKit leaves to a person. When the checks neither passed nor failed within
  six hours, act on what their runs show, or notify. For a rule on the default branch, or
  an issue that left the scope, notify. Never merge.
- **[A diagnosis round with no cause](../../docs/troubleshooting.md#a-diagnosis-round-stopped-as-blocked):**
  act only on a cause that the evidence shows; otherwise notify.
- **[A `no_change` closing](../../docs/troubleshooting.md#an-issue-closed-as-no_change-that-still-needs-a-change):**
  check each issue closed as `no_change` since the last pass against its evidence. When
  the evidence contradicts the finding, reopen the issue, comment, and notify.
- **[A blocker outside the scope](../../docs/troubleshooting.md#an-issue-is-blocked-by-an-issue-outside-the-scope),
  or [a parent whose parts all closed without one completed](../../docs/troubleshooting.md#a-parents-parts-all-closed-and-none-was-completed):**
  these are the person's call. Notify, with the blocker, or the parts and how each closed.
- **[A CI issue with records of several failures](../../docs/troubleshooting.md#a-ci-issue-holds-records-of-several-failures):**
  whenever it has a new plan, pull request or records, account for every record before it
  closes. Start a sweep after opening issues.
- **[A run that ended without a report twice](../../docs/troubleshooting.md#a-run-ended-without-a-report-twice):**
  post the command once more; when it ends so again, notify.
- **[A command or round that did not start](../../docs/troubleshooting.md#a-command-or-round-did-not-start):**
  post it once when the reason it did not run has passed; otherwise notify.
- **[A usage-limit pause with an unknown reset time](../../docs/troubleshooting.md#a-usage-limit-pause-with-an-unknown-reset-time):**
  post the command once, in a pass after the pause; when it pauses so again, notify.
- **[Every run misses a cache](../../docs/troubleshooting.md#every-run-misses-a-cache):**
  the fix is in protected paths: treat it as a change in protected paths below.
- **[Evidence from a CI run is missing](../../docs/troubleshooting.md#evidence-from-a-ci-run-is-missing):**
  post the command again once; when the artifacts expired, notify.
- **[A round failed because of its plan](../../docs/troubleshooting.md#a-round-failed-because-of-its-plan)**,
  [conflict rounds used up](../../docs/troubleshooting.md#conflict-rounds-used-up),
  [a command that got the still-runs reply](../../docs/troubleshooting.md#a-command-got-the-reply-that-the-implement-round-still-runs)
  and [a profile that no longer exists](../../docs/troubleshooting.md#the-plan-names-a-profile-that-no-longer-exists):
  as the entry says.
- **[A manual merge](../../docs/troubleshooting.md#a-round-needs-a-manual-merge-needs_merge)
  and [a change in protected paths](../../docs/troubleshooting.md#the-plans-change-is-in-protected-paths):**
  only when the repository lets the driver push or open hand-made pull requests, running
  what it says before each push. Otherwise notify.
- **[Publishing failed after `/nexkit go`](../../docs/troubleshooting.md#publishing-failed-after-nexkit-go):**
  post `/nexkit go` again only for a force-pushed base branch; otherwise notify with what
  the log shows.
- **[An issue opened without write access](../../docs/troubleshooting.md#autopilot-leaves-an-issue-alone):**
  notify. The driver never opens an issue from untrusted text.
- **[A missing relationship](../../docs/troubleshooting.md#agents):** "Depends on" text
  does not count. When an issue's text has a `Depends on #N: …` line, #N is open, and the
  issue lacks that *blocked by* relationship, add it as the [`request`
  skill](../request/SKILL.md) shows, and start a sweep.

## Notify

Notify only for:

- a choice that the repository does not let the driver take, with no basis in the plan,
  the issue or the docs;
- a diagnosis or a time-out whose evidence shows no cause;
- a split that would go below three levels;
- an authentication error;
- a merge refused by a rule on the default branch, or one that waits while its issue is
  outside the scope;
- an approved pull request with a criterion that no test or check can show, or that
  stayed unverifiable after the round that adds tests;
- a question still asked after two answers;
- a transient failure that has not passed after NexKit's retry, or a service still down
  after `recheck_failed`;
- an issue reopened after a wrong `no_change`;
- a `merge_waiting` pull request whose checks neither passed nor failed within six hours,
  for a reason the driver cannot act on;
- a plan too large again that the driver may not split;
- a blocker outside the scope, or a parent whose parts all closed without one completed;
- an issue opened by someone without write access;
- a CI run's evidence that expired;
- a command paused again at a usage limit with an unknown reset time;
- a run that ended without a report twice, a command that did not start for a reason
  that has not passed, or a command or dispatch of the driver's that failed twice in a
  row;
- any stop above that the repository does not let the driver handle, and the cases the
  repository adds.

Do not notify for what you handled. NexKit's own mentions of `notify` are no reason to
notify. A notification leads with the issue or pull request number and what is needed, in
one sentence, then the links.

## The loop

After a pass, wait about 10 minutes. In a Claude Code session where a foreground `sleep`
is blocked, run `sleep 600` in the background and go on when it ends. Then do another
pass, reading again only the issues, pull requests and runs that changed. Stop before the
next scheduled run would start, so that two drivers never overlap. Stop sooner when
nothing in scope is queued, running or movable, and no hand-made pull request of the
driver waits for its CI.
