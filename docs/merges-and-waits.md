# Merges, conflicts and waits

This page of the [configuration reference](configuration.md) holds what NexKit does by itself around a pull request and a run that stops: merging, closing parent issues, keeping pull requests free of conflicts, mentioning people, and running a command again after a usage limit or a transient error.

## Automatic merge

```json
"merge": {"auto": true, "after_workflows": ["ci.yml"]}
```

With `merge.auto: false` (the default) a person decides every merge. With `true`, the
`report` job squash-merges a NexKit pull request as soon as every check passes and the AI
review approves. It merges as the [GitHub App](configuration.md#the-github-app). The pull request
description says which of the two applies.

The review approves only when no acceptance criterion is unmet. When it could not verify a
criterion from the diff, its tests and the check results, NexKit does not merge: the
round's comment lists the criteria for a person to confirm, and `notify` mentions its
people. Once they are confirmed, approving the pull request is enough: NexKit merges it
(see below). See [Plan, implement, fix and review](stages.md#stages).

After the merge, NexKit closes the issue the pull request implements as completed and
comments a link to the pull request and the merge commit on it, unless GitHub closed it
first through the pull request's `Closes` line. A pull request whose
issue a person closed meanwhile is not merged: the round's comment says so, and the pull
request waits for a person to merge or close it. If closing fails, the round's comment
says so and links the issue; the merge stays. A close that fails because GitHub closed the
issue at the same moment counts as done.

NexKit follows the repository's *Automatically delete head branches* setting
(`delete_branch_on_merge`, under **Settings → General → Pull Requests**) the same way:
when it is on, NexKit deletes the merged pull request's `nexkit/issue-<number>` branch
if GitHub has not done so. A branch that another open pull request
targets stays, and a delete that fails leaves the merge as it is and is named in the
round's comment. With the setting off, NexKit deletes no branch.

Branch protection and rulesets still apply, also to the App, unless you add it to their
bypass list. When
a rule is not met, GitHub refuses the merge, the round's comment says which rule and what
unblocks it, such as a code owner's review of named files, and the pull request waits for
a person (see [The automatic merge was refused](troubleshooting.md#the-automatic-merge-was-refused)).
Before merging, NexKit sets the `nexkit/checks` and `nexkit/review` statuses, so you can
require those two.

No merge by hand is needed after a person's part. NexKit merges a pull request whose
merge GitHub refused, on the head its checks and review passed:

- **after an approval** by someone with write access: the *Approve* review runs in a
  concurrency group of its own, so it never replaces a command that waits on the issue or
  its pull request, and sends a `step` run for the issue. That run merges in the issue's
  group, after the rounds and commands already queued there, and only the approved commit:
  a head that moved since merges nothing, and a person approves the new one. For a round that left criteria to a
  person, only an approval of the commit that round checked counts. It needs the workflow's `pull_request_review`
  trigger and job condition to let approvals through, and that group, as `nexkit init`
  writes them; `nexkit doctor` says when they do not;
- **at each sweep** with autopilot, once GitHub reports the pull request as mergeable
  (`clean`, `unstable` for a check it does not require, or `has_hooks` when the server runs
  hooks before a push), such as after an approval while runs were busy, or after a rule changed. A head that GitHub reported as mergeable
  and still refused, at the round or at a sweep, is not tried again by sweeps.

Each try has the guards of every merge: an issue that closed, an edit by someone without
write access, a label that holds the issue, or a base branch that already holds the change
merges nothing, and a head that moved gets `/nexkit review` instead. A refusal for another
reason stays `merge_refused`, and the new mention says why. A pull request that you
merged or closed yourself in the meantime, such as right after your approval, is left as it
is, with no mention (`pull_closed`). Rules that block every automatic merge:

- a required merge queue, a base branch that requires signed commits, since NexKit's commits
  are not signed, or squash merging turned off for the repository.

[The automatic merge was refused](troubleshooting.md#the-automatic-merge-was-refused) says
what each other refusal needs.

GitHub's *Require branches to be up to date before merging* keeps two pull requests that
pass alone from breaking the base branch together. Under it, checks count only on a head
that contains the base branch's tip. So when the base branch moved while a round's agent
worked, `publish` merges its tip after the round's commit when it merges cleanly, and the
round checks a head that can merge (see *A base branch that moved while the agent worked*
under [Plan, implement, fix and review](stages.md#stages)). NexKit reads the rule from the branch rules
API, or from classic branch protection when its token may read it. When the base branch
moves later, while the round checks, its checks cannot lead to a merge any more. With
`conflicts.auto_resolve`, NexKit then stops that round, brings the pull request up to date
with its base branch and runs its checks again; a move after the checks, which GitHub
refuses at the merge, is handled the same way. When that merge had no conflict and the pull request's
own change is the one the review approved, the approval carries over and no review agent
runs, so the rule costs a run of the checks, not a second AI review (see *A pull request
brought up to date* under [Plan, implement, fix and review](stages.md#stages)).

A merge by NexKit starts your `push` workflows on the base branch, like a person's merge.
`merge.after_workflows` is a choice to save runners, not the only way to test the default
branch: leave a costly workflow's `push` trigger off for the default branch and list it
here, usually with `every`. A workflow that has both runs twice. After each merge, NexKit
starts every listed workflow that is due on the base branch with `workflow_dispatch`:

- Each workflow needs a `workflow_dispatch` trigger whose required inputs all have a `default`,
  because NexKit starts it with no inputs; for example
  `on: {pull_request: {}, workflow_dispatch: {}}`.
  `nexkit init --after-workflow FILE`, repeated for each file, sets the list.
  `nexkit doctor` marks ✗ a listed file that is missing, has no such trigger or requires an
  input without a default, and a `push` trigger that also runs it on the default branch: by
  `branches`, by a `branches-ignore` that leaves that branch out, or by no filter.
- With autopilot, a finished run starts a sweep, so a failure is noticed within minutes;
  see *When a finished run is noticed* under [Autopilot](autopilot.md#autopilot).
- The run uses the base branch's newest commit when it starts, normally the merge commit.
  Its event is `workflow_dispatch`, so steps limited to `push` events are skipped.
- The round's comment links each workflow it started, or says why a start failed. A
  failed start does not undo the merge.
- Nothing is started when `merge.auto` is `false` or the merge was refused.

**Running a workflow every few merges.** A workflow that costs too much to run after every
merge, such as a long CI, CodeQL or a performance test, can run once every few merges
instead of on a `schedule`, which GitHub may delay or drop:

```json
"after_workflows": [
  "smoke.yml",
  {"workflow": "ci.yml", "every": 5},
  {"workflow": "codeql.yml", "every": 30}
]
```

After each merge, NexKit looks at the latest run of each such workflow on the base branch,
whatever started it and however it ended, and counts the commits on the base branch since
that run's commit. It keeps no count of its own, so a person's push that ran the workflow
starts the count again.

- When the count reaches `every`, NexKit starts the workflow, as above.
- While a run of the workflow on the base branch is queued or in progress, it starts none.
- When in doubt it starts the workflow: when the workflow has no run on the base branch,
  when that run's commit is no longer on it, or when GitHub could not tell.
- A start that failed is tried again at the next merge, since the count is still at
  `every` or above.
- The round's comment says, for a workflow it did not start, how many of its commits
  have come, or that a run has not finished yet.

The runs it starts are watched like those of every entry: with autopilot, a failure on
the base branch that repeats opens a `bug` issue, and pull requests that fail the same way
wait for it. A plain file name, or `"every": 1`, starts the workflow after every merge, even
while another run of it has not finished. `nexkit doctor` rejects an `every` that is not
an integer from 1 to 1000.

## Parent issues

```json
"close_parent_issues": true
```

GitHub does not close a parent issue when all its sub-issues are closed, because a parent
can mean more than the sum of its parts, such as an epic with work still to plan. In
repositories where a parent is only its sub-issues, for example when a too-large issue is
split, turn this on. When the last open sub-issue closes, NexKit closes the parent as
completed if at least one sub-issue was completed, and comments a list of the sub-issues
and how each closed. When all of them were closed as not planned, the parent stays open
with a note for a person, as does a parent that autopilot split whose plan lists more parts
than it has sub-issues. NexKit then checks the parent's own parent. It works the same
whether a person or NexKit merged the work, and autopilot's sweeps close such a parent too,
instead of asking a person to.

NexKit decides this on the parent's stored state, which lists at most ten sub-issues. So a
parent stays open while GitHub shows an open sub-issue that its state does not list, such as an
eleventh one or one added after the split. A parent that NexKit stored before it had
sub-issues, for example one that a person split by hand later, is left to a person: NexKit does
not close it, and its run's log and `nexkit status` say why. With more than ten sub-issues, only
the first ten count towards a completed part, so a parent whose only completed part is the
eleventh waits for a person too. Without the setting, autopilot lists a parent whose
sub-issues are all closed under *Needs a person*. See
[Work NexKit starts by itself](work-without-a-command.md#work-without-a-command).

The workflow must listen to `issues: closed`, as `nexkit init` writes it; `nexkit doctor`
warns when it does not. With the setting off and autopilot on, a closed issue's run still
stores the close in the issue's state, sends a sub-issue's end to its parent's queue and
starts a sweep. It closes no parent. Only when both settings are off does a closed issue start
a short run that ends at once.

## Mentions

```json
"notify": ["owner"]
```

NexKit mentions the listed people when a run ends in a state that needs a person, so they
get a GitHub notification without watching every run. Right after the run's comment, it
posts a comment on the issue, or on the pull request once there is one, such as:

> @owner a person is needed: answer the plan's questions with `/nexkit plan <answers>`. The
> plan asks for decisions that are the owner's to make:
>
> 1. `Which host?`
>    - `Fly.io` (recommended)
>    - `Render`

It says what to do, why NexKit does not do it itself, and where to look (see [What a
mention says](troubleshooting.md#what-a-mention-says)), with every question, checklist item
to decide and criterion to confirm whole. The mention is its own comment because GitHub
notifies people mentioned in a new comment, but not in an edited one, and the run's
comment is edited when the run ends.

The choice follows the run's outcome only, never a judgment by the agent (see
[How it works](how-it-works.md#when-a-person-is-needed)). A run mentions at most once;
nobody outside `notify` is ever mentioned. Write logins without `@`. `nexkit doctor`
warns about a listed login without write access, but NexKit still mentions it. With
`[]`, nothing is posted. With autopilot, an issue that starts to wait for a person without
a run that mentioned anyone, such as one whose command never ran, is mentioned by the next
sweep, once for its latest run. Autopilot runs unattended, so `nexkit doctor` says when it
is on and `notify` is empty; `nexkit init --notify LOGIN`, repeated for each login, sets
it.

## Conflicts with the base branch

```json
"conflicts": {"auto_resolve": true, "max_rounds": 3}
```

With `auto_resolve: false` (the default), a person comments `/nexkit fix` on a pull
request that conflicts with its base branch. With `true`, NexKit keeps its open pull requests
mergeable: after the base branch moves, each one that now conflicts gets an automatic fix
round that merges the base branch and keeps both sides. This matters most with
`merge.auto` and several issues at once, where every merge can make the other pull
requests conflict.

- NexKit's own merges, pushes to the default branch (through the `push` trigger that
  `nexkit init` writes), the end of every NexKit run and the hourly schedule start the
  check. A round that ran while the base branch moved holds its conflict round back, and
  the sweep at its end starts it, also without autopilot, when the round left the pull
  request ready for a person. A round that failed, or that ended waiting for CI, starts none:
  a person decides, or the fix round that is due merges the base branch itself. `nexkit doctor` warns when the
  `push` trigger is missing; the end of the next run, or the schedule within an hour,
  still notices.
- A pull request whose mergeability GitHub has not worked out yet is checked again by
  the next sweep.
- When an automatic merge is refused because of a conflict, the round that tried it
  starts a conflict round right away, within the same limits.
- When the base branch moves while a round runs its checks, and its head now conflicts
  with it, or lacks its tip while branch protection wants branches up to date, NexKit
  stops that round's run and starts the conflict round, or the update of the branch,
  right away, within the same limits. A round in any other job goes on. The stopped round
  counts against neither limit.
- When the base branch moved while a round's agent worked and now conflicts with the
  round's commit, the round pushes its commit but skips its checks and AI review, which
  could not lead to a merge, and starts a conflict round right away, within the same
  limits.
- When it is refused because branch protection wants the branch up to date, NexKit
  merges the base branch into it on GitHub and runs its checks again, within the same
  limits. A refusal that says required checks are "expected" on a pull request GitHub
  reports as behind counts the same, also while the merge waits for them. The AI review runs again only when the pull request's own change differs from
  the one it approved (see [Automatic merge](#automatic-merge)).
- A conflict that needs a choice ends the round as `blocked` with its questions; a person
  answers with `/nexkit fix <decisions>`.
- Each pull request gets at most `max_rounds` of these rounds (default 3), apart from
  `fix.max_auto_rounds`, and one per base commit until another round of the pull request
  ran.

See [Work NexKit starts by itself](work-without-a-command.md#work-without-a-command).

## Usage limits

```json
"usage_limit": {"resume": true, "retry_minutes": 60, "wait_minutes": 330}
```

When an agent stops because its account hit a usage limit that resets, the stage moves to
the agent's [fallback agents](configuration.md#fallback-agents), if it has any. When none is left, NexKit
pauses the run, shows the reset time in its comment and keeps `nexkit/checks` and
`nexkit/review` pending. With `resume: true` (the default), it runs the same command again
after the reset, once, with the same note; when the reset time is unknown, after
`retry_minutes` (default 60). With `false`, the comment still names the limit and the
reset time, and a person comments the command again. Each runtime says whether it reports
such limits; `claude-code` does (see [Work NexKit starts by itself](work-without-a-command.md#usage-limits)).

When the reset is at most `wait_minutes` away (330, five and a half hours, by default),
NexKit starts a run that waits for it and resumes the command within ten minutes of the
reset. The run uses no agent and no repository code, but it holds a runner while it waits:
free on public repositories, billed minutes on private ones. Set `wait_minutes` lower, or
to 0, to spend fewer minutes; the hourly schedule then resumes the command.

The schedule also covers resets further away, such as a weekly limit: once one comes
within `wait_minutes`, the schedule starts the waiting run. It needs the hourly `schedule`
trigger that `nexkit init` writes; `nexkit doctor` warns when it is missing. GitHub runs
scheduled workflows when it can, so in practice a scheduled run can come hours late. A
schedule run with nothing to resume runs only `route` and `maintain`, which take well under
a minute and run no agent. To check less often, change the `cron` line, for example to
`"23 */3 * * *"`.

## Transient errors

```json
"retry": {"wait_minutes": [15, 30, 60]}
```

A run can stop for a reason outside the work: an overloaded or failing API, a lost
connection or runner, a request that something in front of the API turned away before it
reached the API (such as Cloudflare's challenge page for the runner's IP address), GitHub's
errors while `publish` writes, or an agent session that gave no result that fits (see
[Failure handling](failure-handling.md#failure-handling)). A key that the API itself refuses is
not among them: that stop waits for a person at once. Such an outage usually lasts minutes
to hours, so NexKit runs the same command again, with the same
note, after each wait of `wait_minutes` in turn: by default 15 minutes after the first stop,
30 after the second and 60 after the third. A stop after the last wait waits for a person,
and its comment lists every error the command's runs stopped on.

- While it waits, the run's comment shows ⏸️ with the time the command runs again, which
  run again this is out of how many, and, from the second on, the errors so far.
  `nexkit/checks` and `nexkit/review` stay as the round left them, except that a review
  that waits to run again is pending. `nexkit status` lists the issue or pull request as
  `retrying`, with that time, not under *Needs a person*. The run that starts the command
  is shown as `(retry N)`.
- A wait holds no agent runner. NexKit starts the run that waits for paused commands,
  which uses no agent and no repository code and also resumes runs paused at the [usage
  limit](#usage-limits); it may wait as long as the longest of these waits. The hourly
  `schedule` starts what that run missed, so `nexkit doctor` warns when it is missing.
- A new command on the same issue or pull request replaces the wait, which then starts
  nothing. A job a person cancelled is never run again.
- The runs again count against neither `fix.max_auto_rounds` nor `conflicts.max_rounds`.
- `[0]` runs the command again once, at once; a 0 among other waits runs it again at once at
  that place. `[]` waits for a person at the first such stop.

