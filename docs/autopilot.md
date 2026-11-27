# Configuring autopilot

This page of the [configuration reference](configuration.md) says how to set autopilot, which works through the open issues in scope without commands, and which issues it takes. [Work NexKit starts by itself](work-without-a-command.md#autopilot) says how its sweeps decide.

## Autopilot

```json
"autopilot": {
  "enabled": true,
  "scope": "milestone",
  "max_parallel": null,
  "answer": "never",
  "lost_after_minutes": 30,
  "watch_workflows": [],
  "trusted_workflows": [],
  "priority": ["nexkit:ci", "nexkit:dependabot", "nexkit:flaky"],
  "labels": {},
  "dependabot": null
}
```

With autopilot on, nobody needs to post `/nexkit plan` and `/nexkit go`. NexKit looks at
every open issue in scope, in order, and starts the one command that moves it on:

| The issue | Autopilot |
|---|---|
| Ready, with no plan yet | Starts `/nexkit plan`. |
| Its latest plan has no questions and is not too large | Starts `/nexkit go`. |
| Its latest plan's change needs protected paths (`protected_paths` in the plan) | Nothing: the plan comment says a person makes that change by hand, and `notify` is mentioned. `/nexkit go` would only stop when NexKit rejects the change. |
| Its latest plan finds that no change is needed (`no_change` in the plan), such as an outside failure that has passed since | Closes the issue as completed, with a comment that holds the plan's finding and the run that passed since. |
| Collaborators commented after the latest plan | Starts `/nexkit plan` again, so the plan holds their changes before `/nexkit go`. |
| Its plan is too large and suggests a split | Creates each part as a sub-issue, in order, with the issue's milestone and labels, marks each part *blocked by* the earlier parts it needs, closes the issue's open NexKit pull request as superseded, and plans the parts that need no other part. |
| Its plan has questions, and every question has a recommended option | With `answer: "recommended"`: posts the answers for the owner and starts `/nexkit plan` again. |
| Its last `/nexkit go` or fix round was blocked, and every question has a recommended option | With `answer: "recommended"`: posts the answers and starts `/nexkit go` or `/nexkit fix` with them as the note. |
| Its last `/nexkit go` was blocked, every question has a recommended option, and one of them changes the issue's scope, such as splitting it, doing other work first or widening it (the question's `replans`) | With `answer: "recommended"`: posts the answers and starts `/nexkit plan` with them as the note, since another `/nexkit go` cannot change the scope. When that plan is too large and suggests a split, creates the parts as for any such plan. A fix round that recommends such an option waits for a person, since a new plan would leave its pull request behind. |
| The plan or round after autopilot's answers asks again, and no person commented since | Nothing: a person decides, and `notify` mentions them. A comment by a person lets autopilot answer again. |
| A blocked `/nexkit go` found that no change is needed, and its one question recommends the option its `closes` names | Closes the issue as completed with the round's finding, with `answer: "recommended"`; no other round runs. |
| Its last `/nexkit go` or fix round was blocked, and a recommended option only closes the issue, other than as the row above says | Nothing, whatever else it recommends: a person answers, and the round's comment mentions `notify`, so a sweep does not mention them again. `nexkit status` lists it under *Needs a person*. |
| Its last `/nexkit go` or fix round was blocked because its change needs protected paths | Nothing, whatever it recommends: a person makes that change, and `notify` is mentioned. |
| Its plan's parts are titled like the issue | Creates no parts, and starts `/nexkit plan` once more with a note that names those titles. |
| Its plan is too large and suggests no parts | Starts `/nexkit plan` once more, with a note that asks for parts that each fit one session. A second such plan waits for a person. |
| Its plan splits it into more than ten parts | Creates no parts, and starts `/nexkit plan` once more with a note that names both counts and asks for fewer, larger parts, each of which may split again when it is planned. A second such plan waits for a person. Autopilot never files a split cut. |
| Its last `/nexkit go` stopped as blocked or failed, not for running out of time, and something changed since that round started: a collaborator commented, other than with a command, or an issue that blocks it closed, such as one a person marked as its blocker after the round asked to wait for it | Starts `/nexkit plan`, so the plan holds what changed, then goes on as after any plan. While nothing changed since the round started, the issue waits for a person. A blocker that closed before the round started changes nothing. |
| Its last `/nexkit go` or fix round ran out of time | Starts `/nexkit plan` once, with a note to split the issue into parts that each fit one session, and to say why the round did not finish. When that plan does not split the issue and needs nobody, starts `/nexkit go`, or `/nexkit fix` on its pull request. |
| A parent issue whose sub-issues are all closed | With `close_parent_issues`, NexKit closed it already, except a parent that waits for a person: one stored before it had sub-issues, or whose only completed sub-issue is beyond the first ten. Without the setting, it waits for a person to close it. |
| A parent issue that autopilot split, whose latest plan lists more parts than it has sub-issues, such as a split that stopped partway | Nothing, even while its parts are open: it waits for a person, who opens the missing parts as sub-issues or closes it, and NexKit does not close it when its sub-issues close. |
| In a wave above 0, with an issue of a lower wave in its milestone open | Nothing yet: it starts once they are closed. See *Labels* below. |
| On hold: a label with `"hold": true` | Nothing: no plan, no `/nexkit go`, no automatic round and no merge of its pull request. A round that runs finishes. Removing the label starts a sweep, which takes the issue again and starts what the hold kept back. See *Labels* below. |
| With a label of `autopilot.priority`, such as a bug issue that autopilot opened for a CI failure on the default branch (`nexkit:ci`) | Goes first, in the list's order, and is in scope also without a milestone. |
| Anything else that needs a person: other questions, a failed run that nothing answered since, a refused merge, a second timeout, a part that runs out of time after an issue above it asked for a split, a second plan with parts titled like the issue, a second plan that is too large and suggests no parts, a second plan with more than ten parts, a parent missing parts of its plan, an issue three levels of parts deep, a pull request that needs a person, a blocker outside the scope | Nothing: a person decides, as without autopilot, and `notify` mentions them once. `nexkit status` lists it under *Needs a person*. Other issues go on, and it does not count toward `max_parallel`. |
| A run on it is running or paused, or its pull request is open and NexKit carries it on | Nothing: the run, or the pull request's automatic rounds, carry it on. |
| Its pull request is approved, and GitHub refused the merge until required checks pass | Tries the merge again, and merges once the checks pass. A required check that fails gets an automatic fix round within `fix.max_auto_rounds`, unless it fails as the default branch does while its bug issue is open; a new commit gets `/nexkit review`. Checks that neither pass nor fail within six hours wait for a person. |
| Its pull request waits for a bug issue about the default branch, whose failure its checks repeat | Nothing while that workflow fails on the default branch and the bug issue is open; then starts the automatic fix round the pull request waited for. It keeps its place within `max_parallel`; the bug issue, and each of its parts, starts even when such issues fill it. |
| The last issue in scope closed | Mentions `notify` once, on the milestone's most recently closed issue, that the milestone is done, and asks a person to close it. With `null`, the same for each open milestone whose issues are all closed, without asking, while the other milestones go on. Any sweep says it, whichever event started it. |

- **Scope.** `"milestone"` takes the open issues of the earliest open milestone: by due
  date, then the oldest, skipping milestones with no issues at all. When all its issues are
  closed, autopilot stops there until a person closes the milestone, so it never starts
  the next milestone by itself. `null` takes every open issue, in every milestone and
  without one, so the *blocked by* relationships alone decide what starts, across
  milestones; to hold an issue back, do not open it yet. Issues outside the scope are
  never touched.
- **Changing the scope.** Work under way is not stopped. A run that is running ends as
  usual, and an open pull request keeps its automatic fix and conflict rounds, which do not
  depend on the scope. Three things then wait for a person: an approved pull request whose
  issue left the scope and whose merge waits for checks, because only sweeps retry that
  merge and they look at the scope alone; a pull request whose issue left the scope while
  it waits for a `bug` issue about the default branch (see *CI on the default branch*),
  because only sweeps end that wait; and an issue in scope blocked by an open issue that
  left it. Issues that left the scope and have not started stay open and wait until
  they are in scope again.
- **Order.** The issues with a label of `autopilot.priority` go first, in the list's
  order; an issue with several takes the highest. By default that is NexKit's own issues:
  bug issues for CI failures, then issues for Dependabot advisories, then issues for flaky
  runs. Then, with `null`, come the issues of each open milestone, in the milestone scope's
  order, then the issues without one; each group oldest first. With `max_parallel`, an
  earlier milestone's issues start first.
- **Ready.** An issue is ready when no issue that blocks it (GitHub's *blocked by*
  relationships) is open. The order of sub-issues does not count, so sub-issues without
  such relationships start at the same time; text such as "Depends on #12" is not read. A
  parent issue with sub-issues is never planned itself: its sub-issues are. Mark an issue
  *blocked by* another only for a real prerequisite, such as something new that the other
  issue adds: a mark that is not needed only slows the work, and touching the same files
  is no reason, since NexKit resolves conflicts. The [`request`
  skill](../skills/request/SKILL.md) has the rules.
- **Labels.** Labels steer the flow, each declared once:

  ```json
  "autopilot": {
    "priority": ["nexkit:ci", "nexkit:dependabot", "bug", "nexkit:flaky"],
    "labels": {
      "journey":           {"wave": 1},
      "last-in-milestone": {"wave": 2},
      "on-hold":           {"hold": true}
    }
  }
  ```

  - **`priority`** lists labels, highest first. Among the issues that are ready, those with
    a listed label go first, in the list's order, and then the rest in the order above. An
    issue with a listed label is in scope also without a milestone, or outside the
    earliest milestone with the `"milestone"` scope, so a person's `bug` issue, such as a
    red dependency scan on the default branch, does not wait behind the milestone. NexKit
    labels the issues it opens `nexkit:ci`, `nexkit:dependabot` and `nexkit:flaky`,
    `nexkit init` creates those labels, and each sweep adds them to NexKit's open issues
    that lack them; until then an issue counts by its marker. The default lists those
    three, so a repository that sets nothing keeps NexKit's order; `[]` puts its issues
    with the rest.
  - **`wave`** is a number, 0 for an issue whose labels set none; with several, the highest
    counts. An issue of wave N runs only once every issue of a lower wave in its milestone
    is closed, read at each sweep, so an issue opened later and the parts of a split count
    without any *blocked by* mark; issues of other milestones do not hold it back. Issues
    of the same wave run side by side, so a milestone's journey (wave 1) runs before its
    docs sweep (wave 2) whatever order they were opened in. A part keeps its parent's
    labels, and so that nothing waits in a circle, an issue never waits for an issue above
    it, or for one that a person marked as *blocked by* it, an issue above it, or an issue
    of its wave or a higher one. `nexkit
    status` shows a waiting issue and names the issues it waits for. An issue with a wave
    and no milestone waits: with the `null` scope, or a label of `priority`, for a person;
    otherwise it is outside the scope, and `nexkit status` says why.
  - **`hold`** makes autopilot start nothing for the issue, a label on the issue itself: no
    plan, no `/nexkit go`, no automatic fix or conflict round and no merge of its pull
    request. A round that is already running finishes, also when it runs again after a
    transient error or resumes after the usage limit, and ends as `held`, with what it kept
    back; one that cannot read the label holds too. Removing the label starts a sweep,
    which takes the issue again and starts that: the automatic fix round, within
    `fix.max_auto_rounds`, the merge, or the round that resolves conflicts, or
    `/nexkit review` when the pull request got a new commit meanwhile or no longer
    conflicts. A held issue takes no place within `max_parallel` once no run on it is
    under way, and a person's own commands still run. `nexkit status` shows it as `held`. It needs autopilot to take
    the issue, since only its sweeps start what was kept back.
  - **Labels never grant trust.** Anyone with triage access can add one, so trust stays
    with markers, authors and [`/nexkit trust`](#vouching-for-an-issue).
  - `nexkit doctor` reports a label in `priority` or `labels` that the repository lacks,
    since no issue can carry it, and one spelled otherwise, since NexKit compares labels
    exactly; GitHub creates NexKit's own labels the first time NexKit uses them.
- **Trust.** Autopilot skips a person's approval of the plan, so the issue text drives an
  implement session, which can read the secrets of its job. It takes only issues opened by
  someone with write access to the repository; only they and other writers can edit them.
  It also takes the issues NexKit opens itself, and those of a [trusted
  workflow](#issues-from-a-workflow). `nexkit status` shows the issues it leaves alone and
  why.
- **At most `max_parallel` at once.** An issue counts while a run on it is running or
  paused, while its pull request is open and needs nobody, and while a command that
  autopilot started has not run yet. An issue that waits for a person does not count. A
  bug issue for a CI failure that pull requests wait for (see the table above), and each
  of its parts, starts even at the limit, since only it ends their wait; it counts once
  started. With `null`, every ready issue starts at once; NexKit's conflict rounds
  (see [Conflicts with the base branch](merges-and-waits.md#conflicts-with-the-base-branch)) keep the pull
  requests mergeable.
- **One command per step.** Autopilot keeps one comment on each issue that names the
  command it started last, with its note, and for which run. It starts a command once for
  a state of the issue. A command that did not run, such as one `route` refused, starts
  again a few times, each time later, and then the issue waits for a person.
  `lost_after_minutes` is how long a sweep waits when it cannot read the command's workflow
  run, and how long a run that a round started next may go unstarted. A run whose comment
  still says running after its workflow run ended is marked as ended without a report. A
  run that NexKit started itself starts again in the same way, and a person's command waits
  for a person. The details are under [Work NexKit starts by itself](work-without-a-command.md#autopilot),
  in *A command starts once* and *Runs that never end*.

- **Answers.** Plans, and implement and fix rounds that stop as `blocked`, give each
  question its options and the one NexKit recommends, if any; the plan comment marks it
  `(recommended)`. With `answer: "recommended"`, autopilot answers only when every
  question has a recommendation, in a NexKit comment that the agents read as the owner's
  discussion. Comments by other bots never count. When autopilot takes over like this, or
  splits an issue, `notify` is not mentioned. Autopilot answers once in a row: when the
  plan or round that its answers started asks again, and no person commented on the issue
  or its pull request since, it waits for a person, whatever the new questions are. The
  prompts never recommend an option that needs a person to act outside NexKit, such as
  downloading files or granting access, so such a question waits for a person at once.
  The code does not rest on that alone: a plan or blocked round whose change needs
  protected paths names them (`protected_paths`), only paths that really are protected
  count, and autopilot then answers nothing and starts no `/nexkit go`.
  When a blocked `/nexkit go` found that no change is needed (its `no_change` finding)
  and its one question recommends the option that closes the issue (the question's
  `closes`), autopilot starts no other round: it closes the issue
  as completed with a comment that holds the finding. A round whose recommended answer
  is to wait or do more is answered as any blocked round, whatever its `no_change` says.
  An option that changes the issue's scope, such as splitting it, doing other work first
  or widening it, is one only a new plan carries out: the question names it in
  `replans`, and the comment marks it. When a blocked `/nexkit go` recommends one,
  autopilot answers with `/nexkit plan`, not `/nexkit go`, and that plan's `/nexkit go`
  counts as the round its answers started; a blocked fix round that recommends one waits
  for a person. The prompts list an option that waits for work no issue holds yet as such
  an option, so autopilot never waits for an issue that does not exist.
  A question's option that only closes the issue, which the comment marks, closes it as
  not planned when a person picks it, with `/nexkit go`, `/nexkit plan` or a plain
  comment, and no agent runs; autopilot never picks it without a round's finding.
- **CI on the default branch.** With workflows in `merge.after_workflows` or
  `autopilot.watch_workflows`, each sweep also looks at the runs of each of them on the
  default branch, after NexKit's merges and people's alike, on a schedule or a dispatch,
  and at every attempt of each run; runs of pull requests, even from a
  fork's branch of the same name, do not count. When the latest run fails, its failed
  jobs run once more, never more often, with those GitHub never created. A run in which
  no job failed, because GitHub never created some of its jobs or the run failed before
  any job did, and that then passed, or that a newer run overtook, is not recorded,
  unless another run of the workflow did the same within the day; then both are
  recorded, with the jobs GitHub never created. NexKit never runs an overtaken run again,
  which could deploy an old commit over a newer one. When it fails again, autopilot opens a `bug` issue
  in scope (in the current milestone, or without one with `null`), with the run link and the failed jobs and steps of every failed attempt,
  mentions `notify` in it, and plans it before every other issue. A later failure on a
  newer commit adds a comment to that issue while it is open; a commit is reported once.
  When a later attempt of that run passes, for example because a person ran it again, a
  comment on the issue says which attempt passed. While that issue is open, a pull request
  into the default branch whose failing checks all fail as the workflow's latest failed
  attempt did, or whose required checks do when NexKit merges it, gets no
  automatic fix round: it waits, using none of
  `fix.max_auto_rounds`, and `nexkit status` shows it as `waiting` for that issue. Once
  the workflow passes on the default branch again, or the issue is closed, a sweep starts
  its automatic fix round, which merges the default branch first. A check fails the same
  way when the first line of its `run` is the command of a failed step, so this works
  when your CI runs the same commands as NexKit's checks, such as `scripts/test.sh` in
  both, and, when both sides name failing tests, when every test failing in the check
  failed there too. The check names them from the JUnit XML files in its `results`, the
  workflow from its failed jobs' error annotations, so with a test reporter that writes
  JUnit files and annotates failures, a pull request that breaks another test in the same
  script gets its fix round at once. See *Failing checks the default branch has* under
  [Plan, implement, fix and review](stages.md#stages).
- **Flaky runs.** A run that passed only when run again, and an older run that failed when
  a newer run had already started, are recorded under each of their failures, on one
  `bug` issue per failure, labelled `nexkit:flaky`, which mentions `notify` and is planned
  after the `bug` issues above (`nexkit:ci`) and the issues for Dependabot advisories, in the
  order of the default `autopilot.priority` (see *Labels* above). An older run is never run again. The error annotations of the failed jobs
  name the failures, such as each failing test, by title or else by file and line; the
  runner's own "Process completed with exit code" names nothing. A job without such an
  annotation is named by its first failed step and the message of its first error
  annotation, such as a download host's refusal, not by the job: the same step failing
  the same way in several jobs, or in several workflows that autopilot watches, is one
  failure with one issue, whose records list each job and workflow. Each record
  names the commit, the run, and where the failure showed in each failed attempt. While a
  failure's issue is open, its later records are comments on it; once closed, a new issue
  opens that links it. A run whose failures would open more than five issues records
  those on one issue for the run. Each run and attempt is recorded once per failure.
  Sweeps look at the runs of the last day and at the runs a `bug` issue reports. To know
  what is recorded, a sweep reads every open issue NexKit opened and every one updated in
  the last day, however many other issues and pull requests NexKit opened since. Reading
  annotations needs `checks: read` in the workflow's permissions, as `nexkit init` writes
  them.
- **When a finished run is noticed.** Within minutes, in two ways that together cover
  every run. First, a `workflow_run` trigger that names each workflow of
  `merge.after_workflows` and `autopilot.watch_workflows` by its `name:` (or its path,
  without one). `nexkit init --after-workflow FILE` and `--watch-workflow FILE` write it
  with those names; without those flags it writes the trigger as a comment. `nexkit
  doctor` says when one is missing:

  ```yaml
  on:
    workflow_run:
      workflows: [CI]
      types: [completed]
      branches: [main]
  ```

  The `nexkit` job's `if` must let `"workflow_run"` through, as `nexkit init` writes it.
  A run that passed at once starts no sweep, unless a `bug` issue about its workflow is
  open. GitHub sends this event also for the runs that NexKit starts, since it starts them
  as its App, so a repository's workflows need no job of their own to start a sweep.
  Without the trigger, the end of the next NexKit run notices a finished run, also a run
  that started the next automatic round itself, and else the hourly schedule.
- **Watching CI that NexKit does not start.** `merge.after_workflows` both starts a
  workflow after each merge and has autopilot watch it. `autopilot.watch_workflows` only
  watches: the runs come from the workflow's own triggers, such as a `schedule`, a
  `push`, also the push of a merge by NexKit, or `workflow_dispatch`. Choose it when a run after every merge mostly
  repeats work: when the rules want pull requests up to date and NexKit squash-merges, the
  merged tree is the one the pull request's checks just passed, so a run on the default
  branch after each merge adds little more than catching flaky tests and checks that
  skipped work they should have done. A run every few hours, or nightly, gives that at a
  fraction of the runner time:

  ```yaml
  # .github/workflows/ci.yml
  on:
    pull_request:
    schedule:
      - cron: "17 */4 * * *"
    workflow_dispatch:
  ```

  ```json
  "autopilot": {"enabled": true, "watch_workflows": ["ci.yml"]}
  ```

  The cost is time: a failure on the default branch is found at the next run, not
  minutes after the merge that caused it. Until then no `bug` issue is open, so pull
  requests that fail the same way get automatic fix rounds, and once one is open they wait
  until it closes or the next run passes. `nexkit doctor` warns when a listed file is
  missing or has no `schedule`, `push` or `workflow_dispatch` trigger. A workflow is in
  one of the two lists, not both.
- **Titles of CI issues.** An issue for a CI failure says what fails, where, and
  "fails at times on" the branch for a flaky failure, or "fails on" it when the branch is
  red, in plain words. Double quotes mark a test, job or step name; there are no
  backticks, and a name is never shortened. A test is named by the last segment of its
  annotation's title, after the last ` › ` or ` > `, and its file name. A title is cut only
  past GitHub's 256 characters, at a word boundary. The body keeps the full name, file and
  line. A red branch's title names the failures of its latest attempt: one, or up to three
  of one kind, else how many.

  | Failure | Title |
  |---|---|
  | A test | Test "a customer pays for an order and the shop shows the receipt" in order-checkout.spec.ts fails at times on main |
  | A Vitest test | Test "pages through more than twenty orders" in OrderHistory.test.tsx fails at times on main |
  | An annotation with only a file and line | Line 12 of src/orders.ts fails at times on main |
  | An annotation with only a file | src/orders.ts fails at times on main |
  | A job, or a step named like its job | Job "Integration tests" in ci.yml fails at times on main |
  | A step | Step "Unit tests" of job "Lint and unit tests" in ci.yml fails at times on main |
  | A step that failed the same way in several jobs | Step "Run actions/setup-dotnet@v5" in 3 jobs of ci.yml fails at times on main |
  | Runs with no failed job, two within a day | ci.yml does not start at times on main |
  | A run with more than five new failures | Run 123 of ci.yml failed in 7 ways at once on main |
  | A red branch | Steps "Lint" and "Unit tests" in ci.yml fail on main |
  | A red branch, failures of several kinds or more than three | ci.yml fails in 4 ways on main |
  | A red branch with no failed job, also when run again | ci.yml does not start on main |

  The marker, not the title, ties a failure to its issue, so an issue opened with an
  older title keeps getting its records.
- **Parts.** Parts that autopilot creates are trusted because they start with a NexKit
  marker and are sub-issues of an issue it trusts, such as one opened by a person with
  write access or a bug issue NexKit opened for a CI failure. A part's own plan that is too large is
  split again, within these limits:
  - A part inherits the time-outs of the issues above it. When one of them already asked
    for a split, a part that runs out of time waits for a person instead.
  - When a part is titled like its parent, or starts with its parent's title, autopilot
    creates no part of that split. It asks for the plan once more, and then waits for a
    person.
  - Parts go at most three levels deep: an issue at level 3 (its parent's parent's parent
    was the issue first split) is not split again; a person splits it by hand into
    sub-issues, which read every issue above them, or implements it. This limit is not a
    setting.

  After a time-out, the plan says why the round did not finish. For a failure whose cause
  was not found, its first part is a diagnosis that finds the cause before the change.
  With profiles, triage chooses that part's profile as for any other issue.

Commands that autopilot starts are dispatches, shown as `/nexkit plan (autopilot)`. A
person can still post any command, and autopilot carries on from what it leads to.
Autopilot pairs well with `merge.auto`, `conflicts.auto_resolve`, `close_parent_issues` and
`notify`; without `merge.auto`, an issue moves on when a person merges its pull request.

Sweeps, the runs that decide this, start at the end of NexKit runs, when issues change,
when a watched workflow finishes on the default branch, and on the hourly schedule, which
catches anything the others missed. [Work NexKit starts by itself](work-without-a-command.md#autopilot)
lists them under *What starts a sweep*. They run one at a time, run no agent and use no agent
credential. The workflow must listen to the events that start them, as `nexkit init` writes
it, and to `workflow_run` as *When a finished run is noticed* shows; `nexkit doctor` warns
when it does not. To start a sweep yourself, for example
right after turning autopilot on:

```sh
gh workflow run nexkit.yml -f command=autopilot -f number=autopilot
```

To move one issue on by one step, dispatch `step` with the issue's number:

```sh
gh workflow run nexkit.yml -f command=step -f number=<issue>
```

The run reads the issue again and does what a sweep sends it to do. A sweep sends this
dispatch for each issue whose step writes, and then waits for those runs to end. The run
starts a command, splits, closes, merges, shows a run that ended without a report as
ended, or mentions `notify` once. It does nothing when the issue is closed, is a pull request, is out of
scope, or has no step to take, such as an issue that is held or waits for another. It
runs in the issue's own queue, so it waits behind the issue's commands and the rounds on
its pull request. A command that waits in that queue is replaced by it, since GitHub
keeps one waiting run per queue. It does not apply `autopilot.max_parallel`, which only a
sweep can keep. After a split, it starts a sweep, which sends the parts their steps. It
runs only with `autopilot.enabled`.

See [Work NexKit starts by itself](work-without-a-command.md#autopilot).

### Issues from a workflow

A workflow can find work and file it, such as a dependency scan that turns alerts into
issues, or an audit that reads the docs against the code. `github-actions[bot]` as an
issue's author proves little: any workflow of the repository can write as it, also on a
pull request. So autopilot leaves such issues alone, unless the setting lists the workflow:

```json
"autopilot": {"trusted_workflows": ["docs-audit.yml"]}
```

The default is `[]`: no issue that a workflow opens is taken. A listed workflow writes a
marker as the first line of the issue's text, with its file name and its run:

```yaml
- run: |
    body="<!-- nexkit:workflow docs-audit.yml $GITHUB_RUN_ID -->
    $(cat findings.md)"
    gh issue create --title "Docs audit: stale sections" --body "$body"
  env:
    GH_TOKEN: ${{ github.token }}
```

The marker counts only on the first line, so text the workflow quotes cannot forge it. The
workflow file is a change to `.github/`, which `protected_paths` keeps from the agents and
your rules keep for your writers, so a run of it on the default branch is as trusted as a
writer. NexKit checks that through the API. It takes an issue opened as `github-actions[bot]`,
or as NexKit's [App](configuration.md#the-github-app) by a workflow that holds its token, when its marker
names a run that:

- belongs to a listed workflow file, the one the marker names;
- ran on the default branch of this repository;
- was started by a schedule, a push, a writer's `workflow_dispatch`, or NexKit's own
  dispatch of a workflow in `merge.after_workflows`, never by a pull request;
- was running when the issue was opened.

An edit of the issue's title or text by anyone but a writer or a workflow of the repository
makes autopilot leave it alone too; a bot's edit cannot be told apart from the workflow's
own. An issue that fails any check waits for a person, and `nexkit status` says which one.

The check proves that the run opened the issue, not what its text says. A workflow that puts
outside text in an issue, such as a package's changelog, hands that text to an implement
session, so list only workflows whose output you would act on unread.

GitHub starts no workflow for an issue opened with `github.token`, so the next sweep takes
it: at the hourly schedule, or sooner when the workflow dispatches one with
`gh workflow run nexkit.yml -f command=autopilot -f number=autopilot`. Parts of a split of
such an issue are trusted like the issue.

### Vouching for an issue

An issue that autopilot leaves alone waits for a person, and `nexkit status` lists it under
*Needs a person* with the reason and the command. A person with write access who has read the
issue comments on it:

```text
/nexkit trust
```

Autopilot then takes the issue at the next sweep, like one a writer opened, which the command
starts. That goes for any issue it left alone: one that someone without write access opened,
or a workflow that `autopilot.trusted_workflows` does not list, or a part of an earlier
release.

- The comment itself is the record, read at each sweep. It counts only from someone who has
  write access at that sweep, never from a bot, and only on the issue itself. The same comment
  from anyone else changes nothing, and NexKit says why.
- The command takes no note, since words after it could say the opposite: NexKit refuses it,
  and such a comment vouches for nothing.
- It covers the issue as the writer read it, and its author can still edit it. An edit of its
  title or text by anyone but a writer or NexKit's App after the comment makes autopilot leave
  the issue alone again, and `nexkit status` names who edited it. Commenting the command again
  vouches for the new text. A bot's edit counts too, since any workflow of the repository can
  write as one.
- A sweep is not the only check. Each plan, implement or fix round that NexKit started looks
  for such an edit right after it read the text, so its agent never acts on it: the round ends
  as `agent_untrusted` before setup and any agent, and gives its automatic round back. A merge
  looks too, and ends as `merge_untrusted`. Commenting the command again starts the same work:
  a plan, the fix round, or `/nexkit review` before the merge. The engine decides that start.
  When the stopped round was a fix round and no automatic fix round is left under
  `fix.max_auto_rounds`, the comment starts nothing: the pull request waits for a person, who
  comments `/nexkit fix`. A round that a person started with a command goes on, since that
  person answers for the text.
- Parts that autopilot creates from a split of the issue inherit the trust, and lose it with
  the issue. An issue that is a part but was not opened by NexKit's App inherits nothing, so
  it needs the comment itself.
- With autopilot off, the command says so and does nothing.

### Dependabot advisories

Autopilot can turn Dependabot alerts into issues, as it does for a failure of CI on the
default branch, without a relay of your own:

```json
"autopilot": {"enabled": true, "dependabot": "high"}
```

Each sweep then opens one issue for each advisory of an open alert at or above that severity,
as NexKit's App, in no milestone, with a marker of the advisory id. Its text names the
advisory, the packages, their manifests and the patched versions, and mentions `notify`.
With `autopilot.dependabot` set, autopilot takes it in every scope, whatever
`autopilot.priority` lists. It is labelled `nexkit:dependabot`, so with the default
`autopilot.priority` it is planned right after the `bug` issues for CI on the default branch
and before the issues for flaky runs.

- An advisory that an open issue marks gets no other issue, whatever its alerts.
- A sweep opens at most five, so that the first one on a repository with old alerts stays
  short; the next sweeps open the rest. It reads the oldest 100 alerts of those severities.
- A split of such an issue keeps its parts in scope in the same way, without a milestone.
- The issue holds the advisory's summary as quoted text, and its title holds only the packages
  and the advisory's id. It asks the agent to ask a person about a package with no patched
  version, so such a plan waits for one.
- A closed issue does not lead to another for the same alerts, so a fix that an alert
  outlives is not repeated: a day after the issue closed, one comment on it mentions `notify`.
  Reopen the issue or dismiss the alerts. An alert that no issue covered, such as in another
  manifest, opens a new issue that links the closed one.

This takes the App's *Dependabot alerts: read* permission, which NexKit asks for only in a
sweep with this setting, and in a token of its own. `nexkit init` does not grant it: add it on
the App's settings page and accept it on the installation (see [The GitHub
App](configuration.md#the-github-app)).

Without the permission, or with Dependabot alerts turned off for the repository, the sweep
opens nothing and the rest of its work goes on. It leaves what is missing as a
`nexkit/dependabot` commit status on the default branch's tip, since nothing else can tell the
two apart: `nexkit status` shows it under *Needs a person* and `nexkit doctor` reports it.
Doctor also asks GitHub whether alerts are on, and with `--app-id ID --app-private-key FILE`
checks the permission itself. The status turns green once a sweep can read them. It belongs to one commit, so on a new tip
nothing shows until the next sweep looks.

