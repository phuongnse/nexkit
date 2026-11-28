# Work NexKit starts by itself

This page covers the work NexKit starts without a `/nexkit` command: events that start a run, autopilot and usage limits. It follows [How it works](how-it-works.md), which holds the jobs of a run and what you see during one.

## Work without a command

Some events start a run without a `/nexkit` command. `route` turns them into one task for
the `maintain` job, or ends the run at once when the task's setting is off. No agent runs,
and no agent credential is used.

**Closing parent issues** (`close_parent_issues`). When an issue closes and it is a
sub-issue, NexKit looks at its parent's sub-issues:

- When all of them are closed and at least one was completed, NexKit closes the parent as
  completed, unless it waits for a person (see the last item). Its comment lists each sub-issue and how it closed. It then checks the
  parent's own parent the same way.
- When every sub-issue was closed as not planned or as a duplicate, the parent stays open,
  and NexKit comments that a person should decide.
- When autopilot filed parts of the parent and its latest plan lists more parts than it
  has sub-issues, such as a split that stopped partway, the parent stays open, and NexKit
  comments both counts and that a person should open the missing parts as sub-issues or
  close it. A parent that a person split by hand has the parts that person chose, but see
  the next item for one that NexKit stored before it was split.
- NexKit decides on the parent's stored state, which lists at most ten sub-issues. A parent
  with an open sub-issue that its state does not list stays open. A parent that NexKit stored
  before it had sub-issues, such as one a person split by hand after a plan that was too
  large, waits for a person, and so does a parent whose only completed sub-issue is the
  eleventh. See [Parent issues](merges-and-waits.md#parent-issues).
- A parent that is already closed, or that still has an open sub-issue, stays as it is.
  Reopening a sub-issue does not reopen its parent.
- A parent in another repository is left alone.

A merge by a person closes the issue through GitHub, and the `issues: closed` event starts
the check. When NexKit closes the issue itself after an automatic merge, `report` runs the
same check in that run, for its comment, and the event's run finds nothing left. With
[autopilot](#autopilot), each sweep also sends a `step` run that closes an open parent in
scope whose sub-issues are all closed, one of them completed, by the same rules, in case
that check did not run or failed: it never asks a person to close it. The run of a part
that closed also sends its end to the parent's queue, where a `step` run stores it in the
parent's state and writes nothing else. A parent missing
parts of its plan waits for a person from the sweep that finds it, even while its other
parts are open. Whichever comes first closes the parent, with
one comment, posted after its close; a run whose close another request beat leaves the
parent, and the comment, to it.

**Merging after an approval** (`merge.auto`). A writer's approval of a NexKit pull request
whose latest round ended as `merge_refused`, as `ready` for criteria the review could not
verify or for checklist items a person decides, or as `pull_closed` before it was reopened,
merges it on the head its checks and review passed, with the guards of every merge. An
approval of that head accepts the checklist items a person decides and turns
`nexkit/review` green there, also with `merge.auto` off, which then leaves the merge to the
person;
a head that moved since the checks gets `/nexkit review`. The approval's own run has a
concurrency group of its own, `nexkit-approved-<pr>`, so it never replaces or waits behind a
command. It writes nothing: it sends a `step` run for the pull request's issue, which joins
the issue's group `nexkit-<item>` (see [One run at a
time](how-it-works.md#one-run-at-a-time)), and that run reads the approving review back from
GitHub and merges there, behind the rounds and commands already queued for the issue. A
commit newer than the approved one, such as one a round pushed meanwhile, merges nothing and
starts no review: approve the new head. An approval while a round runs, or of a pull request
whose round ended otherwise, sends no step and starts nothing. A pull request that a person
merged or closed before the step ran, as an owner who approves and merges in the same minute
does, is left as it is: its round comment stays as it was, and nobody is mentioned. With autopilot, each sweep also sends a `step` run that merges a
`merge_refused` pull request that GitHub reports as mergeable (see [Automatic merge](merges-and-waits.md#automatic-merge)).

**Resolving conflicts** (`conflicts.auto_resolve`). When the base branch moves, NexKit
checks its open pull requests into it. GitHub's own mergeability check decides, so no
code is checked out. A pull request that conflicts gets an automatic fix round, shown as
`fix (auto, conflicts)`, with the note to merge the base branch and keep both sides; one
that merges cleanly is left alone. The round then works as any fix round that merges the
base branch, so a conflict that needs a choice ends as `blocked` with its questions.

- **What starts the check.** A push to the default branch, such as a merge by a person,
  starts it through the `push` trigger. A merge by NexKit starts no `push` workflow, so
  `report` checks the other pull requests right after its merge. The end of every NexKit
  run, except one that waits for a usage limit or started the next round itself, starts
  a sweep that checks every open NexKit pull request, as autopilot's sweeps do; a run
  whose `report` did not finish starts it from its `finish` job, once the run ended. So
  a round held back while another ran starts when that round ends, unless it failed. The
  hourly schedule checks too, which catches anything the others missed. NexKit decides from
  the item's stored state alone, so a pull request whose round failed, or ended waiting
  for CI, gets no conflict round: a person decides, or the fix round that is due merges the
  base branch itself.
- **Unknown mergeability.** GitHub works out whether a pull request conflicts in the
  background after the base branch moves. While it has not (`mergeable` is `null` for
  half a minute), the check decides nothing for that pull request, and the next sweep or
  check asks again.
- **Refused merge.** When GitHub refuses an automatic merge because the pull request
  conflicts, the round that tried the merge starts a conflict round right away. Its own
  comment still says running while it reports, so it does not count as a running round;
  the limits below still apply.
- **Limits.** Each pull request gets at most `conflicts.max_rounds` such rounds (3 by
  default), apart from `fix.max_auto_rounds`. A round starts once for each base commit
  and each round of the pull request that ended since, so a blocked round is not
  repeated until the base branch moves again or another round ran. The start is
  recorded on the pull request's latest round, so a sweep that looks before the new
  round posts its comment does not start it twice. When two starters look at the same
  moment, such as a push to the default branch and a sweep, both may start one; the
  second waits behind the first in the item's concurrency group, and when the
  first ended on the same base commit, `route` lets the second run nothing. A pull
  request whose latest round is
  paused at the usage limit is left alone, and so is one with a round running or on its
  way, such as an automatic fix round that a round started: that round merges the base
  branch itself when it conflicts, and a new run would replace a command waiting behind
  it. A round that started the next one, which has not run within six hours, holds
  nothing up. While the implement round that opened the pull request runs, the conflict
  round waits for it (see [One run at a time](how-it-works.md#one-run-at-a-time)). A sweep leaves alone
  a pull request that autopilot started a command on in the same sweep. The end of the
  round that held it back starts the round if it is still needed.
- When the round starts and the branch no longer conflicts, for example because another
  round already merged the base branch, it ends at once without running an agent.

**Stopping a round in its checks.** Each of these checks (the `push` trigger, the check
after NexKit's own merge, a sweep) also looks at the round of each open NexKit pull
request that is running, or at the implement round that opened it. When that round's run
is in its checks, so that only `shards`, `verify` or `combine` jobs still run, and the base branch
moved so that those checks can no longer lead to a merge, NexKit stops it and starts the
round that can, at once:

- **Conflicts.** The head under check conflicts with the base branch: NexKit cancels the
  run and starts the conflict round, without waiting for the run to end.
- **Behind.** The base branch's rules want pull requests up to date, and the head under
  check no longer contains the base branch's tip: NexKit cancels the run, brings the
  branch up to date and starts `/nexkit review`, as for a merge refused because the
  branch is behind, with the approval carried over when the change is the same.
- A round in `agent`, `publish`, `review` or `report` is never stopped: its agent time,
  its patch and its review are worth more than the checks, and it goes on as before. Nor
  is a run still queued, which has checked nothing yet, or one whose head still contains
  the base branch's tip or whose base branch does not want pull requests up to date.

NexKit cancels the run with the jobs that would follow a cancellation, so neither its
`report` nor its `finish` job runs, and it shows the round itself: outcome `superseded`,
checks and review `not run`, and a comment that says why it stopped and what started
instead; `nexkit/checks` and `nexkit/review` show an error on its commits. It is not a
run a person cancelled. It counts against neither `fix.max_auto_rounds`, which an
automatic fix round gives back, nor `conflicts.max_rounds`, and nothing retries it. The
round it starts counts against `conflicts.max_rounds` as usual; when none is left, NexKit
leaves the round to end by itself. When the next round cannot start after all, such as
when GitHub refuses the update, the stopped round ends as `stop_failed`, and a person
starts the next round. Its result is not kept, which is fine: under these rules its
checks could not lead to a merge, and a failure of the pull request's own code shows again
on the new head. What it keeps is which checks had passed: with `parallel_checks`, before
it cancels the run, NexKit reads the `nexkit-checks-<job>` artifact of each check job that
finished, and the round's marker records each check that passed there on the head NexKit
stopped, as its comment says. A job that checked another commit, or whose artifact cannot
be read, adds nothing. Each of those checks gets that commit in the next rounds'
`NEXKIT_CHECK_VERIFIED_SHA`, so it can skip work its paths do not need (see [Skipping work
that a change cannot affect](checks.md#skipping-work-that-a-change-cannot-affect));
it still runs, and decides by itself.

### Autopilot

With `autopilot.enabled` (see [Autopilot](autopilot.md#autopilot)), NexKit posts no
commands on its own behalf; it starts them the way it starts automatic rounds, with a
dispatch of the workflow. A *sweep* decides which, and sends a `step` for each issue whose
next step writes, which carries it out:

1. It finds the scope: the open issues of the earliest open milestone, oldest first, or
   with `autopilot.scope: null` every open issue, by milestone in the same order and then
   the issues without one, each group oldest first. Issues for CI failures, then for
   [Dependabot advisories](autopilot.md#dependabot-advisories), then for flaky runs go
   before all others. An issue for an advisory has no milestone and is in either scope.
2. For each issue, it reads the markers of NexKit's run comments on the issue and its pull
   request, the latest plan, the issue's *blocked by* relationships and whether it
   has sub-issues. From these alone it decides: start `/nexkit plan`, start `/nexkit go`,
   leave it to a run or pull request that carries it on, or leave it to a person.
3. It sends a `step` for each issue whose step writes, in that order, while fewer than
   `autopilot.max_parallel` issues are in progress: a start, a split, a close, a merge, a
   run that ended without a report to show as ended, or a person to mention. It writes none
   of these itself. The `step` run carries the step out in the issue's group, and records
   each start on the issue. A `bug` issue about the default branch that pull requests wait
   for starts even when they fill the limit. After sending its steps, the sweep waits for
   those runs to end, within a limit (`END_POLL` and `END_TRIES` in `maintain.py`).
4. With `conflicts.auto_resolve`, it then starts the conflict round of each open NexKit
   pull request that conflicts with its base branch, as described in *Resolving
   conflicts* under [Work without a command](#work-without-a-command). With
   `conflicts.auto_resolve` and autopilot off, sweeps do only this.
5. It deletes the branches of closed work (see *Branches of closed work* under
   [Stages](stages.md#stages)).

Only the *blocked by* relationships make one issue wait for another, so a relationship
belongs only where an issue really needs another first; a needless one only slows the
work. The
[`request` skill](../skills/request/SKILL.md) says when to make one.

A sweep is a `workflow_dispatch` with the command `autopilot`. Its `number` input is
`autopilot` too, so `nexkit item` names it that and every sweep gets the group
`nexkit-autopilot`: sweeps run one at a time, and GitHub keeps one waiting sweep, which
sees everything a replaced one would have. A replaced sweep's note changes little: what
is done comes from GitHub (see *Done notices* below), and a sweep that replaced one
after a run whose report did not finish does not wait for that run to end, so it or a
later sweep handles the run once it ended.
Issue and milestone events other than a closed issue run in that group as well, and each
starts a sweep, even one that changes nothing in scope, such as a milestone event with
the `null` scope, since it may have replaced a sweep that waited.

A `workflow_dispatch` with the command `step` and an issue's number carries out that one
issue's step, which a sweep sends it. Its `number` is the issue, so `nexkit item` gives it
the issue's group, together with the issue's commands and the rounds on its pull request.
It reads the issue again and changes nothing when the issue is closed, out of scope or has
no step to take. It applies no `autopilot.max_parallel`, which only a sweep that sees every
issue can keep; a start still goes through the engine's sweep, so an engine that refuses it
leaves the issue waiting for a person, who is mentioned. After a split, it starts a sweep,
and that sweep sends the parts their steps: they start one sweep after the split. A `step`
that GitHub replaced or never ran leaves the issue as it was, so the next sweep finds the
same step and sends it again.

- **What starts a sweep.** The end of every NexKit run, except one that waits for a usage
  limit, and one that started the next automatic round itself while no finished run on
  the default branch needs a sweep (see *CI on the default branch*). A run whose `report`
  did not finish, because it failed, was cancelled or lost its runner, starts one from its
  `finish` job, and a run whose `route` refused a command that NexKit started by itself
  starts one from its `maintain` job, both waiting for the run to end. A closed issue,
  whether a person or NexKit closed it. An issue
  that opens or is reopened. An issue that gets a milestone, and a closed milestone: for
  the milestone scope, the issue comes into scope, or the next milestone becomes the
  scope.
  An issue that gets or loses a label. A writer's `/nexkit trust` on an issue, whose
  `maintain` run starts one.
  A finished run on the default branch of a workflow in `merge.after_workflows` or
  `autopilot.watch_workflows`, through a `workflow_run` trigger, and for the first,
  through the job that ends such a workflow and dispatches a sweep that waits for its run
  to end. The hourly schedule, which
  catches anything the others missed. Nothing waits in a loop: with nothing to start, a
  sweep ends.
- **Done notices.** Every sweep looks at each open milestone in scope: the current one
  for the milestone scope, every open one with `null`. One that has issues, all of them
  closed, gets a notice once, on its most recently closed issue, which mentions `notify`;
  a sweep that finds NexKit's notice on that issue already says nothing. For the
  milestone scope it asks a person to close the milestone, which stays current until
  then; with `null`, the other milestones go on. The notice comes from what GitHub
  shows, not from the note of the sweep that the closed issue started, so a sweep that
  replaced that one in the queue says it as well.
- **A command starts once.** The record on the issue holds the command, the issue or pull
  request it runs on, and the newest NexKit run comment on both when it started. A run
  that starts posts a new run comment, so the next sweep sees that the command ran. The
  `step` run that starts it records it, and the sweep waits for its `step` runs to end,
  so the next sweep sees that record and sends no second `step` that would replace a
  command waiting in the issue's queue. Until then, the issue counts as in progress and nothing else starts on it. A command
  that has posted no run comment and whose workflow run, which the dispatch names, ended
  did not run, for example because `route` refused it, a newer run in its concurrency
  group replaced it in the queue, or GitHub created no job for it. It starts again with
  the same note: at once, then 30, 60 and 120 minutes after its last start, so an outage
  of GitHub Actions costs no person's time and no agent time; when the fifth start does
  not run either, the issue waits for a person, with a reason that names the outage. A
  sweep notices it at once: a
  run whose `route` refuses a command that NexKit started by itself starts a sweep that
  waits for the run to end, and the run that replaced it starts one at its end, unless it
  waits for a usage limit or started the next round itself (see *What starts a sweep*). A
  dispatch that names no run, as on older GitHub Enterprise Servers,
  or whose run cannot be read, counts as lost after `autopilot.lost_after_minutes` (30
  by default), and only the first sweep after that time notices it: at the end of any
  NexKit run, or on the hourly schedule.
- **Runs that never end.** A sweep reads the workflow run behind each run comment that
  still says running. When that run has ended, its `report` never wrote the outcome. The
  run's own `finish` job starts a sweep that waits for the run to end, so this is handled
  within minutes; any later sweep handles it too. The sweep sends a `step` run, which marks
  the comment as ended without a report and sets the round's pending statuses to errors (the `maintain` job
  has `statuses: write` for this). A command autopilot started, or an automatic round,
  starts again, as the same kind of round, so one after failing checks merges a base
  branch that moved first, as for a command that did not run. When its agent or review job ran to its end, so that
  only the report was lost, it starts once more only, since these starts count toward no
  budget. A person's command waits for a person. With autopilot off and `conflicts.auto_resolve` on, a sweep, the schedule or a
  push to the default branch ends such a comment too when it is the latest round of a
  pull request that conflicts, and then starts its conflict round. A run paused at the
  usage limit that NexKit does not resume (`usage_limit.resume: false`, no known reset, or paused more than eight days ago)
  waits for a person. A continued run, or a round that started the next one by itself,
  such as an automatic fix round or a run again after a transient error, records
  the command it started; when that command's run has posted nothing after
  `autopilot.lost_after_minutes`, a sweep starts it once more, and the issue waits for a
  person when that one does not start either. A round from before NexKit recorded this
  waits for a person after six hours.
- **People stay in charge.** A person's command, a plan's questions, a blocked or failed
  run and a `needs_human` pull request all stop autopilot on that issue, exactly where they
  stop NexKit without it. Such an issue waits for a person, with the reason that `nexkit
  status` lists under *Needs a person*, `notify` is mentioned once for it, and it leaves its
  place within `max_parallel` to the other issues. So does an issue whose pull request
  needs a person: its round stopped and autopilot cannot answer or split it, it needs a
  human decision to merge, GitHub refused the merge, or it conflicts with the base branch
  and its conflict rounds are used up (or `conflicts.auto_resolve` is off). An issue blocked
  by an open issue outside the scope waits for a person too; one blocked inside the scope
  waits for that issue. With `null`, a blocker in another milestone is inside the scope. A collaborator's comment after the latest plan leads to a new
  plan before `/nexkit go`. After a `/nexkit go` that stopped as blocked or failed, not
  for running out of time, the issue waits for a person until something changed since that
  round started: a collaborator's comment, other than a command, or an issue that blocks
  it and closed after the round started, such as one a person marked as its blocker after
  the round asked to wait for it. Either leads to a new `/nexkit plan`, so the plan holds
  what changed. A blocker that closed before the round started changes nothing.
- **Answers and splits.** Each question in a plan or a blocked round has options and the
  number of the one the agent recommends, or 0 for none; a number cannot be copied loosely,
  as an option's text can, and NexKit records the option's text. With `autopilot.answer: "recommended"` and a
  recommendation for every question, autopilot posts the answers in a comment that ends
  with its own marker, and plans again, or starts the blocked command again with the
  answers as its note. The prompts include that comment as the owner's discussion; a
  comment by any other bot never counts. Autopilot answers once in a row: when the plan
  or round that its answers started asks again, with no comment by a person on the issue
  or its pull request since the answers, it waits for a person, so new wording of the
  same question cannot start another run. When a blocked `/nexkit go` found that no change
  is needed and asks one question, whose `closes` names its recommended option, that
  answer only closes the issue, so autopilot closes it as completed with the round's
  finding and starts no other round. Text in `no_change` alone never closes an issue: a
  round that recommends waiting or more work is answered as any other, and autopilot
  answers no round or plan that recommends closing otherwise, which a person decides.
  A question may offer one option that only closes the issue, named by its `closes`,
  which the comment marks; a person who picks it, with `/nexkit go` or `/nexkit plan` or
  with autopilot in a plain comment, has NexKit close the issue as not planned, with no
  agent run (see
  [Troubleshooting](troubleshooting.md#a-question-without-a-recommended-option-or-asked-again)). A recommended option is
  one NexKit can carry
  out itself, a change to the plan or the code; the prompts never recommend one that
  needs a person to act outside NexKit, such as downloading files or granting access.
  A blocked round's question also names, in `replans`, the options that change the
  issue's scope, such as splitting it, doing other work first or widening it, which the
  comment marks; an option that waits for work no issue holds yet is one of them.
  Another `/nexkit go` cannot carry such an option out, so when a blocked `/nexkit go`
  recommends one, autopilot posts the answers and starts `/nexkit plan` with them as
  its note, and the plan splits the issue as any plan that is too large. The `/nexkit go`
  after that plan counts as the round the answers started, so autopilot still answers
  once in a row. A blocked fix round that recommends such an option waits for a person,
  since a new plan would leave its pull request behind. A plan that is too large becomes sub-issues, in
  order: every part it lists, or none. A plan that is too large but suggests no parts is planned once more, with a note
  that asks for parts that each fit one session; a second such plan waits for a person. Each part of the plan names the earlier parts it needs (`depends_on`); NexKit
  keeps only earlier parts, so the parts cannot wait for each other in a cycle, and records
  each as a *blocked by* relationship on GitHub. Parts that need no other part start
  together, in the sweep that the `step` run that splits the issue starts. Each part is reviewed only against its own acceptance criteria, so the plan
  prompt asks that a part that later parts depend on has criteria that prove end to end
  the behaviour they rely on, such as a request that goes through every service a later
  part calls, not only that the part starts or answers. The plan of a part that needs
  earlier parts gets the text, state and latest plan of at most six of those parts, the
  oldest first, found among the first twelve blockers it lists, and must name what it
  assumes those parts do and check it in the code before it plans on top of them: each
  assumption goes in `decisions` with the code that shows it, and one the code does not
  bear out, or a part still open, goes in `risks` or `questions`. A NexKit pull request
  of the split issue is closed as superseded. An implement or
  fix agent that ran out of time leads to one re-plan with a note to split the work; a
  second time waits for a person. When the re-plan after a fix round does not split the
  issue and needs nobody, autopilot starts a fix round on the pull request that follows
  the revised plan. Apart from the runs again after a transient error (see [Failure
  handling](failure-handling.md#failure-handling)), this is the only case where an agent error leads to more
  agent work, and it changes the plan rather than repeating the run. The note asks the
  plan to say why the round did not finish: too much work splits as before, and a failure
  whose cause was not found makes the first part a diagnosis that finds the cause, with
  evidence, before the change. With profiles, triage chooses a diagnosis part's profile
  like any other's; it is not forced onto `hard`.
- **Limits on parts.** A split has at most ten parts, as the plan prompt says, and
  autopilot never files one cut. A plan with more is planned once more, with a note that
  names both counts and asks for fewer, larger parts, each of which may split again when
  it is planned; a second such plan waits for a person. A part inherits the time-outs of the issues above it, found through
  GitHub's sub-issue parents: when an issue above it already asked for a split, a part
  that runs out of time waits for a person instead of asking again, however deep it is. A
  part whose title is its parent's title, or starts with it (ignoring case, spaces and
  trailing punctuation), is not created: autopilot plans once more with a note that names
  those titles, and a second such plan waits for a person. Parts go at most three levels
  deep (the issue first split is level 0): an issue at level 3 is not split, after a plan
  that is too large or a time-out, and waits for a person, who splits it on by hand or
  implements it: a sub-issue that someone who can write files under it reads every issue
  above it, as NexKit's parts do. The limit is fixed, not a setting. `nexkit status` shows each of these reasons.
- **CI on the default branch.** Each sweep looks at the completed runs, on the default
  branch, of each workflow in `merge.after_workflows` and `autopilot.watch_workflows`,
  whatever started them, and at every attempt of each: the
  latest run, the runs of the last day and the runs a bug issue reports, among the newest
  100. Runs of pull requests do not count, nor runs from a fork, whose head branch can
  have the same name. When the latest run failed in its first attempt, its failed jobs run
  once more, with those GitHub never created; NexKit never runs them a third time, which
  would hide failures, and never runs an overtaken run again, which could deploy an old
  commit over a newer one. A run whose failures all came with no failed job, because
  GitHub never created some of its jobs or the run failed before any job did, as in an
  outage of GitHub Actions, and that passed when run again or that a newer run overtook,
  is not recorded, and the sweep's log says so: no code of the repository explains such a
  failure. When another run of the workflow did the same within the day, each such run is
  recorded as a flaky run. A run that GitHub refuses to run again is recorded at once.
  When a later
  attempt fails too, autopilot opens a `bug` issue in scope (without a milestone with
  `null`), starting with a marker that
  names the workflow, and that issue goes before all others. Its report lists the failed
  jobs and steps of every failed attempt, the failures each job's error annotations name,
  and the last lines of the output of its failed step, redacted: from the steps of the
  job's log, the first step whose command failed or ran out of time, else the first with an
  error, never the post steps and cleanup that follow it. When it finds neither, it
  shows the end of the log before the post steps and says so; a log without steps shows
  its end. NexKit reads the last 64 MB of a longer log, and says it cut it. A report, in
  the issue or a comment, always fits GitHub's 65,536 characters, with its markers whole:
  when the whole report does not, the log excerpts share what is left, each at most as
  long as the others; when it does not fit even without them, each job lists fewer error
  annotations, then each attempt fewer failed jobs. A shortened report says what it left
  out, and the links of the run, its attempts and its jobs lead to all of it. A round on
  the issue downloads the evidence of
  the runs its marker records (see
  [Evidence from CI runs](checks.md#evidence-from-ci-runs)). Each report holds a marker with its commit, so a
  commit is reported once, and a newer failing commit adds a comment to the open issue.
  When a later attempt of a reported run passes, for example after a person ran it again,
  autopilot says which attempt passed in a comment on that issue. Each report also records
  the commands that failed in the run's latest failed attempt, and the tests its failed
  jobs' error annotations name, which pull requests that fail the same way wait on (see
  *Failing checks the default branch has* under [Stages](stages.md#stages)).
- **Flaky runs.** A run whose latest attempt passed after a failed one, and an older run
  that failed when a newer run had already started, which is never run again, are
  recorded under each of their failures, one issue per failure, so an issue stays one
  behaviour, and one failure has one issue, also when it shows in several jobs or
  workflows. The error annotations of a failed job name its failures (read with
  `checks: read`): test reporters write one per failing test, titled with its name and
  pointing at its file and line. An annotation names a failure by its title, else by its
  file and line; one without either, or that points at the workflow, such as the runner's
  "Process completed with exit code 1.", names nothing. A failed job without an
  annotation that names a failure is named by its first failed step and the message of
  its first error annotation, so one step that fails the same way in several jobs, or in
  several workflows, such as a setup step whose download host refused requests, is one
  failure. An attempt with no failed job is named by the workflow, and its
  record names the jobs that the newest run that passed had and it lacks, and says to look
  for an outage of GitHub Actions before a cause in the repository. Each failure's issue
  is `bug`,
  in scope, titled by the failure in plain words, for example 'Test "pages through more
  than twenty orders" in OrderHistory.test.tsx fails at times on main' (see [the title
  forms](autopilot.md#autopilot)), starts with its own marker that names the workflow and a key for
  the failure, labelled `nexkit:flaky`, and is planned right after the `bug` issues for a
  failing default branch, labelled `nexkit:ci` (see *Labels* under
  [Autopilot](autopilot.md#autopilot)).
  Each record names the commit, the run, and the job, file and line, and message where
  the failure showed in each failed attempt. A later record of the same failure is a
  comment on its issue while that is open; once closed, a new issue opens that links it.
  A run whose failures would open more than five issues, likely one cause such as a
  broken runner, records those on one issue for the run instead; failures that have an
  issue still get their records there. Every bug and flaky-run report ends with a marker
  that lists the run attempts it covers, so each run and attempt is recorded once per
  failure, and a sweep reads no attempt that a marker covers. The first sweep after an upgrade looks
  back one day, as every sweep does; sweeps run at least hourly. To know which runs are
  recorded, a sweep reads the open issues NexKit opened and those updated within
  that day, at most 1,000 of each: a record is written after its run started,
  so its issue was updated since. A finished run starts a
  sweep within minutes through the `workflow_run` trigger, when the run is one a sweep
  acts on: it failed or was run again, or it passed while a `bug` issue about its
  workflow is open. That goes for the runs NexKit starts too (see [When a
  finished run is noticed](autopilot.md#autopilot)). Without the trigger, the end of the
  next NexKit run checks the newest finished run of each workflow the same way, also a run
  that started the next round itself; the hourly schedule catches the rest.
- **What autopilot reads.** The plan comment ends with a hidden record of its questions and
  its split, after everything the agent wrote, and a run comment's marker holds the
  questions of a blocked round and whether the agent ran out of time.

### Usage limits

When an agent stops because its account hit a usage limit that resets, in any stage
(triage, plan, implement, fix or review), the stage starts again on the agent's next
[fallback agent](configuration.md#fallback-agents), from the beginning; implement and fix
start from the checkout the first agent found. A fallback without its secret is skipped,
and so is one when less than a minute of the stage's time is left; the run summary says
which. Without one left, the run is paused, not failed, until the earliest reset of the
agents that stopped. The next run starts on the stage's own agent again, so a fallback runs
only while the agent is at its limit.

Each runtime says whether it reports usage limits, and recognises them from its CLI's own
message. `claude-code` does: Claude subscriptions have session and weekly limits.
Fallbacks use the same runtime as the agent, and a runtime that does not report limits
cannot have them.

- The run's comment shows ⏸️, the CLI's message with the reset time, and when NexKit runs the
  command again. `nexkit/checks` and `nexkit/review` stay `pending`. Nothing is published
  from the stage that stopped.
- After the reset, NexKit starts the same command again, once, with the same note. When
  the reset is at most `usage_limit.wait_minutes` away, the run that paused starts a run
  that waits for it: a dispatch of the workflow with the command `resume`, in the
  `nexkit-resume` concurrency group, so it never holds up a command on an issue or pull
  request. That run writes nothing on the issue: for each command that is due, it sends a
  `step` into the item's own group, and that run starts the command. Its job may run `wait_minutes` plus 15 minutes, which is why `wait_minutes` is
  at most 330: a GitHub-hosted job ends after six hours. The same run sends the steps for commands that
  wait to run again after a transient error (see [Failure handling](failure-handling.md#failure-handling)),
  within the longest wait of `retry.wait_minutes`, which may be at most 330 minutes too;
  its job may run as long as the longer of the two plus 15 minutes. It runs no agent and no
  repository code, looks for due commands every ten minutes until nothing is left within
  its wait, and also resumes runs that pause meanwhile. Resets further away wait for the
  hourly `schedule`, which starts the waiting run once one comes within reach; with
  `wait_minutes: 0`, the schedule sends the steps itself. GitHub runs scheduled workflows
  when it can, often hours apart, which is why NexKit does not rely on it for resets that
  are close.
- A review that stopped after its round published a commit resumes as `/nexkit review`,
  because the commit is already there. A resumed round shows `(resumed)` and does not
  count toward `fix.max_auto_rounds`. If it hits the limit again, it pauses again.
- NexKit recognises the limit only from the CLI's own message, never from what the model
  wrote, so an issue about rate limits cannot pause a run.
- When NexKit cannot read the reset time from the message, it tries again after
  `usage_limit.retry_minutes` (one hour by default), and says so. If that run stops at
  the limit again, it waits for a person, so a limit that does not reset cannot start a
  run again and again.
- Once the command runs again, the paused run's comment shows ⏭️ `continued` instead of
  ⏸️, and says how: resumed in a new run (linked), or replaced by a later command. A new
  command on the same issue or pull request replaces the paused one, which is then not
  resumed. When `/nexkit go` paused at its pull request's review, the issue's comment
  shows ⏭️ once a round starts on the pull request.
- With `usage_limit.resume: false`, the comment still names the limit and the reset
  time, and asks a person to comment the command again after it.

The paused run's comment holds the command, its note and the resume time in its hidden
marker; that is all the state there is. The waiting run and the schedule look at run
comments edited in the last eight days and run no agent.
