# Plan, implement, fix and review

This page covers the stages of a run: plan, implement and fix, review, and merge and close. It follows [How it works](how-it-works.md), which holds the jobs of a run and what you see during one.

## Stages

Every prompt contains the same writing guide, because people read what the agents write:
write for a person who knows the project but not this change, in plain English with short
sentences and one idea per bullet, and say what changes in behaviour, and why, before
naming classes, files or methods. The agents return fields, and NexKit adds the headings,
so plans, reviews and pull request descriptions always have the same layout.

**Plan.** Read-only access to the checkout (see [Agents](agents-and-profiles.md#agents)). Posted as a comment
marked `<!-- nexkit:plan -->`. The latest plan is the one `/nexkit go` implements. The part a
person approves comes first; the detail for the implementing agent is collapsed:

```markdown
## Plan
One or two sentences: what changes and why.

**Profile: hard**: plan `claude` (`opus`) · …  only with profiles: each stage's agent and model, why, and how to change it

### Since the last plan   only on a re-plan: revised or started over, and what changed
### What changes          up to six bullets about behaviour
### Decisions to check    choices the issue did not settle, each with a short reason
### Risks and limits      what could go wrong or is left out
### Acceptance criteria   one check each, at most about 30 words
### No change needed      only when the evidence shows the issue needs none; the finding, and the run that passed since
### Needs a hand-made change  only when the change needs protected paths, which a person changes
### Questions             only when answers would change the work; options, one marked (recommended)
### Suggested split       only when the work is too large for one session: at most ten parts, each saying which earlier parts it needs

<details><summary>Implementation notes</summary>
Files, signatures, commands: the detail the implementing agent needs.
</details>
```

Empty sections are left out. The implementing agent, and the reviewer, receive the whole
comment, including the implementation notes, but not the profile lines.

The comment always fits GitHub's 65,536 characters, with its record whole. A plan longer
than that has each text longer than the most that fits cut to it, marked `[…]`, and the
comment says so: the summary, each bullet, decision, criterion and note, each part's
body. Questions and their options, part titles, checklist items and protected paths are
never cut; NexKit bounds each. The record holds the same text, so the parts autopilot
files are what the comment shows. A plan that does not fit even with each text cut to
500 characters is not posted: its run fails and a person decides.

A plan for an issue that names CI runs gets the same [evidence of those
runs](configuration.md#evidence-from-ci-runs) as the implement round: the end of the
failed jobs' logs, their annotations, and whether a newer run of the workflow passed on
the default branch since. So the plan starts from the cause the logs show. The plan
prompt lists the protected paths: a plan whose change must modify them lists them in
`protected_paths`, and only paths that really are protected count. The comment then says
that a person makes that change by hand, and autopilot starts no `/nexkit go` (see
[Troubleshooting](troubleshooting.md#the-plans-change-is-in-protected-paths)). A plan
that finds no change is needed, such as for a failure from outside the repository that
has passed since, writes the finding in `no_change`; with autopilot, the issue closes
with it. A `no_change` with no text once quotes and whitespace are removed, such as `""`,
is no finding, in a plan or a round: the plan or round goes on as with an empty string.

Each acceptance criterion is something the review can verify from what it sees: a test in
the change, or the result of a project check on the candidate commit. Steps that only the
implementing agent can run, such as running a test several times, putting the cause back
or a manual probe, go to the implementation notes instead. For a failure that happens only
at times, the plan prefers a test that reproduces the cause on every run, for example by
forcing the delay or the race, since repeated passes do not show the cause is gone. A
behaviour that no test or check can show is named under the risks, so a person knows to
confirm it.

A re-plan revises the latest plan. The agent gets that plan and the discussion since it,
keeps what nothing asks to change, and applies what the discussion and the note ask for;
where the issue now differs from the plan, the issue wins. A note that asks for a plan
from scratch, such as `/nexkit plan from scratch`, makes it plan again without the latest
plan's choices. *Since the last plan* says which happened and lists each decision,
acceptance criterion or scope item that was added, changed or removed.

**Changing a plan.** The plan is the contract: it holds the acceptance criteria that the
review checks the pull request against, and it is the record a later reader looks at. A
change that lives only in a follow-up issue is missing from both, so every change to a
plan, however small, goes into the plan or the round that implements it:

- Before `/nexkit go`, or after a `/nexkit go` that stopped, comment the change on the
  issue, so the next plan holds it. With autopilot, a collaborator's comment after the
  latest plan, or after the stopped `/nexkit go`, makes it plan again; without, comment
  `/nexkit plan`. This is also how to change an answer that autopilot gave.
- Once `/nexkit go` has started, post the change on the pull request as
  `/nexkit fix <change>`.
- State only the changes. A re-plan keeps what no newer comment changes, so choices
  accepted as they are need no comment. Write one short line per change, with the exact
  values, names and rules, and nothing that the issue or the docs already say: link them
  instead. For example: `Change: limit 2,000 characters; trim before saving; no new
  endpoint.`
- To change the profile, comment `/nexkit plan Use the <profile> profile` (see
  [Profiles](agents-and-profiles.md#profiles)).
- After the re-plan, check that *Since the last plan* lists every change that was asked
  for, and that nothing else changed without a reason.
- Use `/nexkit plan from scratch` only when the plan has gone wrong. It drops the latest
  plan's choices, so first comment every choice to keep.

**Implement and fix.** Full access to a disposable runner. The prompt contains
the plan, the issue, the discussion from collaborators, and for fix rounds the failing
check output, blocking review findings and collaborators' review comments on the current
commit. It asks the agent to run the checks before it finishes; of a
[slow check](configuration.md#slow-checks), only the tests that cover the change or that
failed, since NexKit runs every check after the round. The agent must return `done` or
`blocked`. A blocked or failed session publishes nothing.

An implement agent that finds no change is needed, such as for a failure that a commit on
the base branch already fixed, or a CI failure from outside the repository, such as a
download host that refused requests, after which a newer run of the workflow passed on
the default branch, changes nothing and writes the finding, with its evidence and the run
that passed, in `no_change`. The run's comment adds a link to each run that passed since.
An implement or fix agent whose change must modify a protected path leaves it unchanged
and stops as `blocked` with the paths in `protected_paths`; autopilot answers none of its
questions, and a person decides. When the issue, the plan or the discussion says to stop without a change
in that case, it returns `done`: NexKit opens no pull request, closes the issue as
completed, and the run's comment shows the finding (outcome `no_change`). Otherwise it
returns `blocked` with a question whose recommended option is to close the issue, and
names that option in the question's `closes`, which [autopilot](work-without-a-command.md#autopilot) answers by
closing it. A round blocked for any other reason leaves `no_change` empty. A round that
changed files and returned `done` is a change, whatever `no_change` holds: NexKit
publishes it as usual, adds the text to the pull request's *Notes for the reviewer*, shows
it in the round's comment as a note from the implement agent, and keeps the issue open.
Only a round that changed no file closes the issue with its finding.

When the failing checks of the last round kept
[result files](configuration.md#result-files-of-failing-checks), `route` puts the id of the
run that recorded them in the decision, and the fix round's `agent` job downloads that
run's `nexkit-checks` artifact to a folder under `$RUNNER_TEMP`, outside the working
tree. The prompt names the folder, and for each failing check its
folder and the files and folders it kept, and asks the agent to read them before it runs
anything. A download that fails, for example because the artifact expired, never fails
the round: the prompt has no such section, and the log of *Collect issue context* says
why.

Every fix round's prompt also lists the pull request's earlier fix rounds, from the state
comment: for each, whether it was automatic or requested by a person, its note, the fix
agent's summary and cause, and the checks that still failed after it, each with its
failure signature (see [Failure handling](failure-handling.md#failure-handling)). The agent is told not to
repeat a change that did not help. Besides `summary`, a fix result has `cause`: the cause
of the failures the round addressed and the evidence that shows it, such as trace, log or
output lines, or an empty string, and `cause_outside`: whether that cause lies outside the
code, which only an automatic round after failing checks uses (see *Failing checks the
base branch fixed* under [Stages](#stages)). The round's comment and its commit message
show a cause when there is one, and so does the next review's *Previous round* section. NexKit builds the pull request description from the implement result, in this
order: *Summary* (two or three sentences, also the commit message), *What changed* (in
behaviour), *How it is tested*, and, only when needed, *Outside the plan* and *Notes for
the reviewer*. A fix round's summary becomes its commit message.

The new pull request also gets the issue's labels and milestone. A failure there only
warns, since the pull request already exists. Later rounds leave them to people. Autopilot takes only issues, so its
scope's milestone on the pull request changes nothing.

The pull request of an implement round starts at the base commit the agent started from,
not at the base branch's tip when `publish` runs. So the patch always applies, even when
someone merges into the base branch while the agent works. The pull request is then
behind its base branch, which GitHub shows; `/nexkit fix` brings it up to date and resolves
any conflict (see below).

**A base branch that moved while the agent worked.** Before it pushes the round's commit,
of an implement or a fix round, `publish` compares it with the base branch's tip as it
fetched it, with git and without running any repository code:

- **Up-to-date rules.** When the base branch's rules want pull requests up to date
  (GitHub's *Require branches to be up to date before merging*), checks count only on a
  head that contains the base branch's tip. When that tip merges cleanly, `publish` adds a
  merge of it after the round's commit and pushes that merge, so `verify` checks a head
  that can merge. The round's comment names the commit it merged. NexKit reads the rule
  from GitHub's branch rules API and from classic branch protection's
  `required_status_checks.strict`, which takes a permission the App does not have; then,
  as when GitHub cannot answer, the rule counts as off.
- **A conflict.** A head that conflicts with the base branch's tip cannot merge, under any
  rules. With `conflicts.auto_resolve`, `publish` still pushes the round's commit, but the
  round runs neither `verify` nor `review`: `nexkit/checks` and `nexkit/review` show an
  error that says why, the round's comment says that its checks were skipped because the
  head conflicts, and `report` starts the conflict round, `fix (auto, conflicts)`, at once,
  within `conflicts.max_rounds`, as after a merge refused because of a conflict. That round
  resolves the conflict in its `agent` job as any fix round does. With its conflict rounds
  used up, no round starts: the round ends as `conflicting`, says so, and a person decides.
  Without `conflicts.auto_resolve`, the round checks and reviews the head as before.
- Otherwise, such as when the rules do not want pull requests up to date, or the base
  branch has not moved, the round publishes its commit alone, as before.

The review agent judges the pull request's own change against the base branch, so the
extra merge commit changes nothing in what it reviews.

**Conflicts with the base branch.** `/nexkit fix` also brings a pull request that no longer
merges cleanly back in line with its base branch. There is no separate command. By
default a person comments `/nexkit fix`; with `conflicts.auto_resolve`, NexKit starts such
a round by itself (see [Work without a command](work-without-a-command.md#work-without-a-command)).

1. At the start of every fix round, the `agent` job checks with `git merge-tree` whether
   the branch conflicts with the base branch. Only then does it merge the base branch
   into the working tree, without committing. A branch that merges cleanly is not
   merged, so fix rounds stay small, with one exception: an automatic round started
   after failing checks merges the base branch whenever it has commits the branch lacks
   (see *Failing checks the base branch fixed* under [Stages](#stages)).
   Branch protection can require up-to-date branches if a repository wants every round
   to include the latest base: `publish` then merges the base branch's tip after the
   round's commit when it merges cleanly (see *A base branch that moved while the agent
   worked* above).
2. The prompt lists the conflicted files. The agent resolves the conflict markers
   together with the round's other feedback; when conflicts are the only problem, the
   round only resolves them. It keeps the intent of both sides when a conflict has one
   right answer: both sides added different lines, or one side renamed something the
   other side uses.
3. When a conflict needs a choice (both sides changed the same behaviour differently,
   or one side deleted code the other side changed) and the plan, the issue, the
   feedback and the note do not settle it, the agent does not guess. It returns
   `blocked`, NexKit pushes nothing, and the round's comment lists each open conflict:
   the file, what each side changed and the question to answer. The person answers with
   a note on the same command, for example `/nexkit fix Keep main's validation in
   Program.cs and add this PR's endpoint after it.` The next round merges again, follows
   the note and resolves the rest itself.
4. `publish` commits the result as a merge commit whose parents are the branch head and
   the exact base commit the agent merged, never the base branch's newer tip. It pushes
   normally, never with force, so the reviewed commits stay. A squash merge of the pull
   request keeps the base branch's history clean.
5. The checks and the review run on the merge commit, as after any fix round. The round's
   comment says that the base branch was merged and which files had conflicts.

The fix prompt tells the agent not to merge anything itself. When NexKit merged nothing and
the agent merged a commit of the base branch anyway, as git's `MERGE_HEAD` shows after the
agent ran, NexKit treats that commit as the round's merge: `publish` makes it the second
parent, the protected-path check counts only what differs from git's own merge, and the
round's comment says the base branch was merged. When the agent merged any other commit,
or another commit than the one NexKit merged, the round ends with an error and publishes
nothing.

A round whose own commit conflicts with the base branch's tip when `publish` pushes it
skips its checks and review, and with `conflicts.auto_resolve` starts the conflict round at
once (see *A base branch that moved while the agent worked* above).

**Failing checks the base branch fixed.** A check can fail for a reason that the base
branch fixed after the pull request branched off, such as a flaky test. An automatic fix
round started after failing checks therefore merges the base branch first whenever the
base branch has commits the pull request lacks, conflicts or not. Without a conflict, the
prompt says that the round merged the base branch cleanly and asks the agent to fix only
what still fails on the merged code. When the agent changes nothing, `publish` pushes the
merge alone, an empty patch being valid after a merge, and the next checks run on it.
When the base branch has not moved, nothing is merged, and an empty patch is still the
error "The agent finished without changing any files.", with what the agent found, unless
the agent names a cause outside the code: a package registry, a service or the network
that failed while a check ran, with its evidence in `cause` and `cause_outside` set. Then
the round publishes nothing, and its checks and review run once more on the same commit,
in the same run and within the same automatic round. When they still fail, the round ends
as `recheck_failed`, and a person decides with the agent's finding in the mention. Rounds that a person starts with
`/nexkit fix`, and automatic rounds started only by blocking review findings while the
checks passed, merge the base branch only on a conflict, so they never bring in base
changes that nobody asked for.

**Failing checks the default branch has.** While autopilot's `bug` issue for a workflow
of `merge.after_workflows` or `autopilot.watch_workflows` is open (see [Autopilot](work-without-a-command.md#autopilot)), every pull request
branched from the default branch holds that failure, and a fix round on each would fix it
again in its own way, or change nothing. So a pull request into the default branch whose
issue is in autopilot's scope gets no automatic fix round when its failing checks are the
default branch's failure. It waits instead:

- *The same failure* means that every failing check runs a command that failed in the
  latest failed attempt the `bug` issue records and fails the same way: when both sides
  name failing tests, it fails only tests that failed there, so at least one test failing
  in the check failed on the default branch, and none failing in the check passed there;
  when only one side names failing tests, the failures differ. A pull request's
  checks are NexKit's own configured checks, not that workflow's jobs and steps, so NexKit
  compares commands and tests:
  - The issue's report holds, for each failed job, the command of each `run` step whose
    process failed or ran out of time, as the job's log starts that step, and the tests
    that the job's error annotations name, as a test reporter titles them. A check is
    the same when the first line of its `run` is one of those commands, and is compared
    with the tests of the jobs that ran it.
  - The check names its failing tests from the JUnit XML files in its `results` (see
    [Result files of failing checks](configuration.md#result-files-of-failing-checks)):
    the `verify` job, which runs the check, reads the test cases that failed or erred and
    records their names with the check's result. The `report` job, which decides and
    holds a write token, reads only that record and parses no file the repository's code
    wrote.
  - A name is compared without case or runs of whitespace. A test named as code, such as
    `Shop.Tests.OrderTests.Totals(count: 2)` or a JUnit `classname` and `name`, is its
    class and method, `ordertests.totals`. Any other is the last segment of its title,
    after its describe blocks (`Orders > adds tax`), with the name of its file without
    folders, line or column: the annotation's path, or the JUnit `file`, or a
    `classname` that names a file. Two titles match when their files match, or when one
    side does not say which file.
  - A side names no test for a build or lint step, a check without JUnit results,
    results that name no failed case, more than 200 failing tests, or a report from
    before NexKit recorded tests. When neither side names a test, the command alone
    decides. When only one side does, the check fails otherwise than the default branch
    and gets the usual round. GitHub keeps at most
    10 error annotations of a step, so a check that fails more tests than the default
    branch's annotations name gets the usual round.

  A check whose command the workflow does not run, or runs written differently, or that
  fails a test the default branch passes, is a failure of the pull request's own and
  gets the usual round, as does the `bug` issue's own pull request and those of its
  parts. Review findings do not count: the round after the wait addresses them too.
- A required check that failed when NexKit merged, at the round's first try or while the
  merge waits for it (`merge_waiting`), is compared the same way. It is a job of the repository's CI, not a NexKit check, so its commands
  are those of its `run` steps that failed, from the job's log, and its tests those its
  error annotations name, as for the default branch. A required check that is no job of
  a workflow run, such as a commit status, names no command and gets the usual round.
- The round ends as `ci_waiting`, uses none of `fix.max_auto_rounds`, and mentions
  nobody. Its comment, the state comment, `nexkit status` (state `waiting`) and the
  autopilot sweep name the `bug` issue. The issue keeps its place within
  `autopilot.max_parallel`, and the `bug` issue it waits for never waits for a place,
  nor do its parts when it is split: it starts even when the issues that wait for it fill
  `max_parallel`, and counts toward it once started, so nothing else starts beyond the
  limit.
- The wait ends when the workflow passes on the default branch again, in its newest run
  that passed or failed, which must be the reported run or a newer one, or when the `bug`
  issue is closed. The next sweep then starts the automatic fix round the pull request
  waited for, `fix (auto)`, which counts toward `fix.max_auto_rounds` like any other and
  merges the default branch first (see above). When no automatic round is left by then,
  a person decides.

Conflicts in protected paths stop the round before the agent runs, because the agent may not
change those files. Changes that the base branch brings to protected paths are fine: the
protected-path check compares the result with git's own merge of the two commits, so only
the agent's edits count, and the App may push changes to `.github/workflows/` that a merge
brings from the base branch.

**Review.** Read-only access, like the plan. Returns `approve` or `request_changes`, a
status for each acceptance criterion with the test that covers it and the evidence,
findings marked `blocking` or `suggestion`, and an answer for each item of the
[`review.checklist`](configuration.md#review-checklist). The prompt tells the reviewer not to block on
things the implementer cannot change, such as PR text or CI files.

A criterion is `met` or `unmet` when the diff, its tests or the check results show either,
and `unverifiable` when they show neither, such as a step only the implementing agent could
run. NexKit decides from the criteria as well as the verdict:

- An `unmet` criterion counts as changes requested, whatever the verdict: `nexkit/review`
  fails, and the criterion goes to the next fix round as a blocking finding, so it starts
  an automatic fix round while `fix.max_auto_rounds` remain.
- An approved round with an `unverifiable` criterion never merges by itself, even with
  `merge.auto` on. `nexkit/review` succeeds, so a person can merge by hand; the round's
  comment lists the criteria to confirm, and `notify` mentions its people.
- A [checklist item that a person decides](configuration.md#review-checklist) and that is
  not met is no finding and starts no fix round. When nothing else blocks, the round ends
  `ready` without a merge: `nexkit/review` fails until a writer approves that commit, which
  accepts the items, turns it green and, with `merge.auto`, merges the pull request.

**A pull request brought up to date.** With GitHub's *Require branches to be up to date
before merging*, a pull request that checks and review approved cannot merge once its base
branch has moved. With `conflicts.auto_resolve`, NexKit then merges the base branch into it
on GitHub and starts `/nexkit review` on the new commit. A merge that has no conflict
leaves the pull request's own change as it was, so a second AI review would spend agent
time and find nothing new. Before the review agent starts, the `review` job compares, with
git and without running any repository code:

- The new commit must be a merge of a commit of the base branch into the approved commit,
  as GitHub's update makes it. A commit pushed on top, such as a person's, or a merge of
  anything else, runs the review.
- The pull request's change against the base commit it merged must be the change the
  review approved against the base commit that one started from, whatever the line
  numbers. Git's patch id of both diffs decides, with binary content and whitespace
  counted, so a merge commit that changed anything of the pull request's own, or a base
  change next to its lines, runs the review.

When both hold, the review carries over: no agent runs, `nexkit/review` succeeds with
"AI review of `abc1234` carried over", the earlier findings stay the last review's, and
the round's comment says that the review was carried over. The checks run on the new
commit as after any round, but a check whose
[`unaffected_by`](configuration.md#checks-that-a-change-cannot-affect) lists every path
that changed since it passed gets no runner and counts as passed. When they pass, NexKit
merges it with the earlier approval; when they fail, an automatic fix round starts as after any failing checks, counted in
`fix.max_auto_rounds`, and its own review runs as usual. Otherwise the review runs as
before, and the round's comment says why. A merge that has a conflict is a conflict round
instead (see *Conflicts with the base branch* above), which the review always follows. A run
again after a transient error compares the same way. A `/nexkit review` that a person
comments never carries an approval over. A merge that leaves the pull request without a
change of its own closes it instead, before any review (see below).

**A pull request brought up to date that changes nothing.** Another pull request can land
the same change on the base branch first. Once NexKit merges the base branch into the
pull request, in a fix round or by bringing the branch up to date for a merge, the pull
request changes no file against its base branch any more. So before each round, review
or merge try on a pull request, NexKit asks GitHub whether it still changes a file
against its base branch, as its *Files changed* tab shows. When it does not:

- No round runs and nothing merges. NexKit closes the pull request without merging, and
  its issue as completed, as a merge would, with its parents under `close_parent_issues`.
  An issue that a commit of the base branch closes at the same moment counts as closed.
  It deletes the branch as after a merge (see *Branches of closed work* below). The outcome
  is `already_on_base`; nobody is mentioned.
- The pull request's round comment and a comment on the issue say that the base branch
  already holds the change. They name the commit that brought it when NexKit can tell
  one: outside a fix round, the only commit of the base branch, merged into the pull
  request since it started, that changed a file the pull request's own commits changed.
- A command on such a pull request, by a person or by NexKit, such as the `/nexkit
  review` after a branch update, goes to the `maintain` job, which closes it and shows
  the outcome on the pull request's latest round. A merge try closes it in the round
  that tries it, or in the autopilot sweep that tries a waiting merge again.
- A fix round checks once more in its `agent` job, before setup and the agent, since the
  base branch may have moved after the round started: when git's merge of the branch
  with the base branch's tip gives the base branch's own files, the round runs nothing,
  and `report` closes the pull request and its issue. The round gives back its
  automatic round, and git names the commit: the oldest one of the base branch that
  already holds everything the branch changes.
- `nexkit status` lists it under the pull requests NexKit closed without merging, not as
  waiting for a merge or a person.

A pull request whose own change differs from the base branch, however small, goes on as
usual, and so does one that GitHub cannot compare.

**A person's pull request.** A change to `protected_paths`, such as a NexKit upgrade, is a
person's pull request, and those are the changes that matter most to review. So `/nexkit
review`, from someone with write access, also works on a pull request that NexKit did not
open, from a branch of this repository. The run is the review round of a NexKit pull request,
shown as `review (person)`: `verify` runs `setup` and the checks on its head, as the
configuration on the default branch says, without secrets, and the `review` job reviews it.
Both statuses are set on that head.

- It pushes nothing, starts no fix or conflict round and never merges, also with `merge.auto`
  on. A round whose review gave a verdict ends as `person_reviewed`, whatever the checks and
  the review found, and nobody is mentioned; the review says that a person makes any change.
  A review that gave none ends as on any pull request: it runs again after a transient
  error, after the waits of `retry.wait_minutes`, and otherwise `notify` hears of it.
- The reviewer gets no plan. It judges the change against the pull request's title and
  description, and it reviews `.github/`, `.nexkit/` and every other protected path like any
  other code, since a person wrote them. With no plan there are no acceptance criteria:
  NexKit drops any the reviewer still gives, so none blocks the review or waits for a
  person.
- When it changes `.nexkit/config.json`, `route` also reads that file at the head through
  the API and loads it as `nexkit doctor` does, with NexKit's own code: nothing of the
  head runs, and the checks still come from the default branch's configuration. NexKit
  reads its configuration only from the default branch, so a file it refuses would stop
  every command once it merged, the one on the pull request that corrects it included.
  Such a file fails `nexkit/checks`, whose description and the round's comment name the
  error, whatever the checks and the review found, so the pull request cannot merge where
  the rules require that status. A file that loads changes nothing in the round. When
  GitHub does not answer, the status fails too, and says so.
- The state comment keeps the review, so the next `/nexkit review` sees its findings.
- `/nexkit fix` is refused there, a pull request that changes no file has nothing to review,
  and a pull request from a fork runs nothing, since its checks would run code that nobody
  with write access pushed. Each reply says why.
- Autopilot and `nexkit status` leave such a pull request out. Each review takes a new comment,
  so a new commit needs `/nexkit review` again. No sweep looks after a run lost on the way: its
  comment says running and both statuses stay `pending`, also where the rules require them,
  until a writer comments the command again.

**After an automatic merge.** NexKit closes the issue the pull request implements, unless
GitHub closed it first through the pull request's `Closes` line. GitHub's close can land
while NexKit closes it, and GitHub then refuses NexKit's close; NexKit reads the issue
again, and a closed issue counts as closed, parents included, with nothing left for a
person. When the repository's
*Automatically delete head branches* setting (`delete_branch_on_merge`) is on, NexKit
deletes the pull request's head branch, if GitHub has not. It deletes
only a `nexkit/issue-<number>` branch of this repository, never the base branch, and keeps
one that an open pull request targets or another comes from, since deleting it would close
that pull request.
When the delete fails, the round's comment says why and that the branch stays; the merge
and the issue's close stay. With the setting off, every branch stays.

**Branches of closed work.** The same setting and the same guards apply when NexKit closes
its own pull request without merging it: `already_on_base`, a pull request that a split
superseded, and one whose issue autopilot closed because the plan after a round found that
no change is needed. It keeps a branch that another open pull request comes from, as it does
after a merge. A person who closes a NexKit pull request leaves its branch, and so
does an issue that closes while its pull request stays open, as `merge_issue_closed` does
for a person to decide.

Each sweep then picks up what is left: it deletes up to three `nexkit/issue-<number>`
branches whose issue is closed and whose pull requests are all closed or merged. It keeps a
branch that an open pull request targets. It also keeps one with no pull request, and one
that moved after its pull request ended, since nothing else holds those commits; GitHub can
restore any other from its pull request. A branch it could not delete gets one comment on
its issue that says why, and stays until a person acts.

**An issue closed while its round runs.** `route` checks that the issue is open when a
command starts, and NexKit checks again before it acts on it. When a person closed the
issue while a plan or `/nexkit go` ran, `publish` pushes no branch, opens no pull request
and posts no plan, and the run's comment says that the issue closed while the round ran
and that nothing was published (outcome `issue_closed`). Nobody is mentioned, and
autopilot starts no further round for the issue. When it is reopened, it waits for a
person, since nothing changed since `/nexkit go` started. A pull request whose issue is
closed is never merged automatically: the round's comment says so, the pull request stays
open for a person to merge or close (outcome `merge_issue_closed`), and `notify` mentions
its people. Every merge first reads the pull request itself: one that a person merged or
closed first, which may have closed its issue through its `Closes` line, ends the round as
`pull_closed`, and nobody is mentioned; so does a merge that GitHub refused because one
landed while NexKit tried. A pull request reopened after such a round waits for a person,
who approves it, which NexKit then merges, or comments `/nexkit review`.

The pull request review starts with what a person must act on:

```markdown
### NexKit review: changes requested
One or two sentences: the main reason for the verdict.

**Things to look at**         findings, blocking first, with file and line
**Previous findings**         what happened to each earlier finding (later rounds)
**Acceptance criteria**       ✅ met, ❌ unmet or ❔ unverifiable, and the covering test
**Checks**: ✅ `test`

<details><summary>Evidence</summary>
file:line references and assertions for each criterion and previous finding
</details>
```

The reviewer judges the code against the same requests the implementing agent followed.
Its prompt has the plan, the issue and the discussion from collaborators on the issue and
the pull request, including the notes on `/nexkit` commands, such as `/nexkit go Keep the old endpoint`.
It also gets the *Outside the plan* section of the pull request description: what the
implementing agent did that the plan did not ask for, and why. A deviation whose reason
holds, such as a change a collaborator asked for while approving the plan, is not a
finding. A deviation without a reason, or with a reason that the plan, the issue and the
discussion do not support, is.

Every review is a fresh session, so the reviewer stays independent of the agent that wrote
the code. When the pull request already has a NexKit review, the prompt adds a *Previous
round* section: that review's findings as the state comment keeps them (see
[State](how-it-works.md#state); it says how many were left out of a longer review, and the reviewer
reports only on those listed), each fix round since then with the note that
started it and the fix agent's summary (which may reject a finding with a reason), and the
diff from the reviewed commit to the current one, next to the full diff. The reviewer
first returns a `resolution` for each earlier finding (`resolved`, `unresolved` or
`rejection_accepted`) with evidence, does not raise an accepted rejection again, treats
changes asked for in a note as in scope, and adds new blocking findings only for real
defects. It also repeats each earlier finding's severity, so the pull request review can
list the resolutions with these marks:

| Mark | Meaning |
|---|---|
| 🛑 Blocking, unresolved | A blocking finding that is still open. It blocks the merge. |
| 💡 Suggestion, still open | A suggestion that is still open. It does not block the merge. |
| ✅ resolved | The code now handles the finding. |
| 🤝 rejection accepted | A fix round rejected the finding, and the reviewer agrees. |

🛑 marks only blocking findings, in *Things to look at* and in *Previous findings*.

After a fix round that merged the base branch, the diff since the reviewed commit
leaves out the changes the merge brought from the base branch: it starts from git's own
merge of the reviewed commit with the merged base commit.

When this run's fix round merged the base branch, the prompt also has a *Merge* section:
the conflicted files and what each side changed in them before the merge. The reviewer
checks that both sides survived in each file. A dropped side is a blocking finding, unless
a person's note asked for it.
