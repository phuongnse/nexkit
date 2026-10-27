# Changelog

## 5.2.0

NexKit leaves alone an issue that a person closed while its round ran, cleans up the
branches of the pull requests it merges, and no longer closes an issue on an empty
finding. See [Automatic merge](docs/configuration.md#automatic-merge) and
[Stages](docs/how-it-works.md#stages).

- A plan or `/nexkit go` whose issue a person closed while it ran publishes nothing: no
  branch, no pull request, no plan (#162). The run ends as `issue_closed` and needs
  nobody. When the issue is reopened, it waits for a person. NexKit never merges a pull
  request whose issue is closed: the round ends as `merge_issue_closed`, the pull request
  stays open for a person, and `notify` is mentioned.
- When the repository's *Automatically delete head branches* setting is on, NexKit
  deletes the `nexkit/issue-<number>` branch of each pull request it merges, as GitHub
  does after a person's merge (#163). A branch that an open pull request targets stays,
  and a delete that fails leaves the merge as it is.
- A `no_change` with no text once quotes and whitespace are removed, such as `""`, is no
  finding (#164). Such a plan shows no *No change needed* section and goes on to its
  change, where autopilot closed the issue before.

## 5.1.0

Fewer runs for nothing after a failure outside the repository, such as a download host
that refused requests for an hour. Plans read the CI runs' logs, a change that only a
person can make stops for that person, a failure that has passed since closes its issue,
and one failure opens one issue. See
[Evidence from CI runs](docs/configuration.md#evidence-from-ci-runs) and
[Autopilot](docs/configuration.md#autopilot).

- A plan for an issue that names CI runs gets the same evidence as the implement round:
  the end of the failed jobs' logs and their annotations (#154). The read-only plan agent
  may read the evidence folder, so a plan starts from the cause the logs show.
- The plan prompt lists the protected paths. A plan whose change needs them names them
  in the plan's new `protected_paths`; only paths that really are protected count (#155).
  The plan comment says a person makes that change by hand, and autopilot waits for a
  person instead of starting `/nexkit go`.
- An implement or fix round whose change needs protected paths stops as `blocked` with
  them in `protected_paths`, and autopilot answers none of its questions, whatever it
  recommends (#156).
- The evidence of each CI run says whether a newer run of its workflow passed on the
  default branch since (#157). A plan or implement round may then find that a failure
  from outside the repository has passed and needs no change (`no_change`, now in plans
  too). Autopilot closes the issue with the finding, and the comments link the run that
  passed.
- A failed step without an annotation that names the failure is keyed by the step and
  its first error annotation, not by its job (#158). The same failure in several jobs,
  or in several workflows of `merge.after_workflows`, opens one issue whose records list
  each job and workflow, titled like 'Step "Run actions/setup-dotnet@v5" in 3 jobs of
  ci.yml fails at times on main'. An issue opened for one job's step keeps getting that
  job's records.
- Sweeps read every open issue NexKit opened and every one updated in the last day,
  instead of its 100 newest issues and pull requests, so a run that an issue recorded is
  never recorded again on a new issue (#159).

## 5.0.0

Autopilot can take every open issue, across milestones, with *blocked by* relationships
as the only order. Several stops for a person and several hour-long waits are gone. And
what people, and a scheduled agent playing them, do around NexKit is now in NexKit's own
docs and skills. See [Autopilot](docs/configuration.md#autopilot),
[Who does what](docs/how-it-works.md#who-does-what) and
[Troubleshooting](docs/troubleshooting.md#when-nexkit-waits-for-a-person).

- `autopilot.scope: null` takes every open issue (#143). Issues for CI failures come
  first, then flaky-run issues, then each open milestone in order, then issues without a
  milestone. A blocker in another milestone is waited for, not "outside the scope". Each
  finished milestone gets one done notice, posted from state. Issues for CI failures get
  no milestone under `null`. The `label` scope is removed.
- A pull request whose failing checks and tests also fail on the default branch, for an
  open CI issue, waits as `ci_waiting` instead of spending fix rounds (#144). When the
  branch passes again or the issue closes, it gets one fix round that merges the base
  branch first.
- Lost commands, stuck *running* comments and finished after-merge runs are handled
  within minutes, not at the hourly schedule (#148). A new `finish` job, a
  `workflow_run` trigger and a `nexkit-sweep` job at the end of each after-merge
  workflow start the sweep. The new `autopilot.lost_after_minutes` (default 30) bounds a
  dispatch that never ran.
- Sweeps start the conflict round of every conflicting pull request with nothing
  running, also with autopilot off (#149). A round held back while another ran was not
  released when that round stopped without a merge.
- Autopilot plans once more after a too-large plan without parts, and after a stopped
  `/nexkit go` once a collaborator commented or a blocker closed (#150).
- A pull request waiting to merge gets an automatic fix round when a required check
  fails, also at the first merge attempt, and `/nexkit review` for a new commit (#151).
- The `request` skill marks only real prerequisites, gives each mark a reason line,
  asks for a test that finds every case, and gives a readiness verdict before work is
  broken down (#145).
- How it works, Configuration and Troubleshooting say who does what, how to change a
  plan, how to review a pull request, how to write profiles, how to upgrade, and what a
  person does at each stop (#146).
- A new `drive` skill runs one pass of a scheduled agent that plays the person at
  autopilot's stops, within limits the repository sets (#147).

### Upgrade

- **Remove the label scope.** A configuration with `"scope": "label"` or a label name
  in `autopilot.label` fails. Set `autopilot.scope` to `null` or `"milestone"` and
  remove `autopilot.label`; `"label": null` is still accepted. See
  [Label scope removed in 5.0](docs/configuration.md#label-scope-removed-in-50).
- **With autopilot and `merge.after_workflows`:**
  - add `"workflow_run"` to the event list in the `nexkit` job's `if:`;
  - add a `workflow_run` trigger that names each after-merge workflow, with
    `types: [completed]` and the default branch;
  - end each after-merge workflow with the `nexkit-sweep` job, which needs
    `actions: write`.

  `nexkit init --after-workflow <file>` prints both, and `nexkit doctor` checks them.
  See [Autopilot](docs/configuration.md#autopilot).
- **Remove `labeled`** from the `issues:` trigger types, as `nexkit init` now writes
  them.
- On the first sweep, every finished milestone that is still open gets one done notice.

## 4.7.0

Fewer rounds that end with a person doing what NexKit could have done itself. See
[How it works](docs/how-it-works.md#one-run-at-a-time) and
[Failure handling](docs/how-it-works.md#failure-handling).

- A pull request runs one NexKit round at a time (#138). A conflict round no longer
  starts while the `/nexkit go` run that opened the pull request still checks, reviews
  and merges it; when that round ends and the pull request conflicts, it starts the
  conflict round itself. A person's command or *Request changes* review on the pull
  request meanwhile gets a reply to post it again once the round ends. A merge refused
  because the head branch was modified starts `/nexkit review` on the new head, as
  `head_moved`, instead of waiting for a person. The `route` job reads workflow runs
  (`actions: read`), which the caller workflow already grants.
- An automatic fix round after failing checks whose agent changes nothing and names a
  cause outside the code, such as a package registry that failed, runs the checks and
  the review once more on the same commit, within the same automatic round (#139). The
  fix result's new `cause_outside` says so. When they still fail, the round ends as
  `recheck_failed` and waits for a person. An empty patch for any other reason stays an
  error, and both now give the agent's finding in the comment and the mention.
- A round that finds no change is needed closes the issue (#140). The implement result's
  new `no_change` holds the finding and its evidence. When the issue, its plan or the
  discussion says to stop without a change in that case, NexKit opens no pull request
  and closes the issue as completed with the finding, as `no_change`. Otherwise the
  round blocks with closing as its one recommended answer, and autopilot, with
  `autopilot.answer: "recommended"`, closes the issue instead of starting `/nexkit go`
  again.
- Issues for CI failures get one plain title form that keeps the whole failing name
  (#137): what fails, where, and "fails at times on main", or "fails on main" when the
  branch is red, with names in double quotes and no backticks. A test is named by the
  last segment of its annotation's title and its file name. Issues opened with an older
  title keep getting their records. See
  [Autopilot](docs/configuration.md#autopilot).

## 4.6.1

- A round's evidence from CI runs takes the runs the issue records again, also after many
  NexKit rounds (#134). The run-log links in NexKit's own comments counted as runs the
  issue names, so after a few rounds they filled the five places and the failed CI runs
  that the issue linked were never downloaded. Links in comments by bots no longer count,
  and runs of NexKit's own workflow are skipped wherever they are linked, without taking a
  place. See [Evidence from CI runs](docs/configuration.md#evidence-from-ci-runs).

## 4.6.0

Autopilot carries each issue in scope on by itself, or waits for a person with a reason,
and nothing in between. See [Autopilot](docs/how-it-works.md#autopilot) and
[Failure handling](docs/how-it-works.md#failure-handling).

- Autopilot answers a plan's or a blocked round's questions once in a row (#125): when
  the plan or round that its answers started asks again, and no person commented on the
  issue or its pull request since, it waits for a person. The prompts recommend only an
  option that a change to the plan or the code carries out, and a diagnosis round adds
  logging or narrows a test itself before it asks a person.
- A fix or conflict round checks its merge of the base branch before the agent runs
  (#126). Without a push token that may change workflow files, a merge that only brings
  a moved base branch with workflow changes is left out, and the round fixes the branch
  as it is; a merge needed for a conflict stops the round before its agent, as
  `needs_merge`. Neither counts against `fix.max_auto_rounds` or `conflicts.max_rounds`,
  and no further conflict round starts until a person merged.
- An issue whose pull request needs a person waits for that person and leaves its place
  in `autopilot.max_parallel` to the other issues (#129). Every issue that waits for a
  person is listed under *Needs a person* in `nexkit status` with the same reason, and a
  sweep mentions `notify` once for it. After a fix round ran out of time, a revised plan
  that does not split the issue is followed by a fix round on the pull request. A
  blocker outside the scope, and a parent issue whose sub-issues are all closed, wait for
  a person. `nexkit doctor` says when autopilot is on and `notify` is empty, and
  `nexkit init --notify LOGIN` sets it. See [Mentions](docs/configuration.md#mentions).
- A command autopilot started that has not run after six hours starts once more, with
  the same note, then waits for a person (#130). A run comment that still says running
  after its workflow run ended is marked as ended without a report, its pending statuses
  become errors, and a run NexKit started itself starts once more. A pause NexKit will
  not resume, a continued run whose new run never started, and a round whose next round
  never ran wait for a person.
- A run that stopped on a transient error, before it published anything, runs once more
  with the same note, shown as `(retry)` (#127): an overloaded or failing API, a lost
  connection, a CLI that exited before the model answered, a job that failed as a whole,
  a failed setup, a review without a result, and an automatic fix round whose branch
  moved meanwhile. A second such stop waits for a person, and its comment shows both
  errors. The retry counts against no automatic round limit.
- An automatic merge that GitHub refuses because the base branch was modified meanwhile
  is tried again, three times in all (#128). One refused until required checks pass ends
  the round as `merge_waiting` for an issue in autopilot's scope, and the next sweeps
  merge it once they pass; a branch that branch protection wants up to date is updated
  on GitHub and reviewed again, within `conflicts.max_rounds`. Other refusals wait for a
  person, as before.
- Implement and fix rounds get the evidence of the CI runs their issue names (#124):
  runs that NexKit's bug and flaky-run issues record, and links to runs in the issue and
  collaborators' comments. Before the agent runs, the agent job downloads the end of
  each failed job's log, its annotations, and the artifacts that the new
  `evidence.artifacts` lists, to a folder outside the working tree, redacted, at most
  50 MB per round, and the prompt names each file. The `bug` issues that autopilot opens
  for a failing default branch list each failed job's annotations and the end of its
  log. `nexkit init --evidence-artifact NAME` sets the setting. See
  [Evidence from CI runs](docs/configuration.md#evidence-from-ci-runs).
- `tests/test_scenarios.py` plays autopilot over many rounds and checks, after every
  sweep, that each issue is carried on or waits for a person, listed and mentioned (#131).
- The `maintain` job asks for `contents: write`, to merge a pull request whose required
  checks passed since, and `statuses: write`; the `agent` job asks for `checks: read`.
  The workflow `nexkit init` writes has granted all three since 4.4.0.

## 4.5.0

- The review gives each acceptance criterion a status of met, unmet or unverifiable, in
  place of met or not (#119). An unmet criterion counts as changes requested, whatever the
  verdict: `nexkit/review` fails, and the criterion goes to the next automatic fix round
  as a blocking finding. An approved round with an unverifiable criterion, one that the
  diff, its tests and the check results show neither way, never merges by itself, even
  with `merge.auto` on. `nexkit/review` stays green, the round's comment lists the
  criteria a person confirms before merging, and `notify` mentions its people. The review
  marks them ✅, ❌ and ❔. A review from before this keeps its verdict. See
  [How it works](docs/how-it-works.md#stages).
- Plans write only acceptance criteria that the review can verify: a test in the change,
  or the result of a project check (#120). Steps only the implementing agent can run, such
  as running a test several times or putting the cause back, go to the implementation
  notes. For a failure that happens only at times, the plan prefers a test that
  reproduces the cause on every run, and names under its risks a behaviour that nothing
  can show.
- The description of a pull request NexKit opens says who merges it: NexKit with
  `merge.auto` on, a person with it off.
- A pull request NexKit opens gets the labels and milestone of its issue (#121), so it
  shows in the milestone's view and in the issue's label filters.

## 4.4.0

- Each failure of a flaky run on the default branch gets its own `bug` issue, titled by
  the failure, instead of one issue per workflow (#115). The error annotations of the
  failed jobs name the failures: a test reporter's title, such as the test's name, else
  the file and line. The runner's own "Process completed with exit code" names nothing,
  and a job without such an annotation is named by the job and its first failed step. A
  later run with the same failure adds a comment to its open issue, or opens a new issue
  that links the closed one. A run whose failures would open more than five issues
  records those on one issue for the run. An issue from 4.3.0 keeps its records and gets
  no new ones. See [Autopilot](docs/configuration.md#autopilot).
- Autopilot's check of `merge.after_workflows` counts only runs of the default branch
  itself (#117). Runs of pull requests, including those from a fork whose head branch has
  the default branch's name, no longer lead to a re-run, a `bug` issue or a flaky-run
  record.
- `nexkit doctor` reports a workflow that does not give the pipeline a permission that
  its jobs ask for, as `nexkit init` writes them. GitHub starts no run of such a workflow.
  See [Troubleshooting](docs/troubleshooting.md#setup).

### Upgrade

- **Add `checks: read`** to the `permissions:` of `.github/workflows/nexkit.yml`, as
  `nexkit init` writes it. The `maintain` job reads the annotations of failed jobs with
  it. Without it, GitHub starts no run of the workflow, autopilot or not:

  ```yaml
  permissions:
    contents: write
    issues: write
    pull-requests: write
    statuses: write
    actions: write
    checks: read
  ```

## 4.3.0

- A check can be marked `"slow": true` (#103). The implement and fix prompts list slow
  checks apart, as checks NexKit runs after the round, and ask the agent to run only the
  tests in them that cover its change or that failed, as narrowly as the repository's
  instructions allow. NexKit's own checks still run every check in full. Without a slow
  check, the prompts are as before. See [Slow checks](docs/configuration.md#slow-checks).
- A check can name the files or folders where it writes its results, such as traces,
  screenshots and logs: `"results": ["test-results"]` (#101). When the check fails, the
  round's `nexkit-checks` artifact keeps them, text files redacted, at most 50 MB per
  round. The next fix round's `agent` job downloads them to a folder outside the working
  tree, and the prompt asks the agent to read them before it runs anything. See
  [Result files of failing checks](docs/configuration.md#result-files-of-failing-checks).
- Every fix prompt lists the pull request's earlier fix rounds: the note, the summary,
  the cause and the checks that still failed after each (#102). When a check fails the
  same way before and after a fix round, the next automatic round is a diagnosis round,
  shown as `fix (auto, diagnosis)`: the agent names the cause, with evidence, in the new
  `cause` field before it changes code, then fixes it. A diagnosis that changes code
  without a cause is rejected; one that finds no cause and changes nothing ends as
  `blocked` with a question for a person. See
  [Failure handling](docs/how-it-works.md#failure-handling).
- `nexkit init` sets every field of a check: `--check-timeout NAME=MINUTES`,
  `--check-slow NAME` and `--check-results NAME=PATH` name a check from `--check` (#111).
- A part of a split issue inherits its parent's time-outs (#104): once an issue above it
  asked for a split, a part whose agent runs out of time waits for a person instead of
  splitting again. A part titled like its parent is not created, and the plan is asked
  for once more. Parts go at most three levels deep. After a time-out, the plan says why
  the round did not finish, and a failure whose cause was not found makes the first part a
  diagnosis. See [Autopilot](docs/how-it-works.md#autopilot).
- Autopilot's check of `merge.after_workflows` on the default branch looks at every
  attempt of the recent runs, not only the latest (#105). A run that passed only when run
  again, and an older failed run that a newer run overtook, are recorded on one `bug`
  issue per workflow for flaky runs, planned right after the issues for a failing default
  branch. A later attempt that passes after a `bug` issue was opened says so there. Each
  run and attempt is recorded once.
- With `autopilot.enabled` or `conflicts.auto_resolve` and no
  `publish.push_token_secret`, `nexkit doctor` says that a round merging changes to
  `.github/workflows/` from the base branch waits for a person, and which token to add
  (#113). Doctor still passes. See [Secrets](docs/configuration.md#secrets).

## 4.2.0

- An automatic fix round started after failing checks merges the base branch first
  whenever the base branch has commits the pull request lacks, conflicts or not (#97). A
  failure that the base branch already fixed, such as a flaky test, then costs no agent
  change: the prompt says that the round merged the base branch and asks the agent to fix
  only what still fails, and when it changes nothing, the merge is published alone and
  the next checks run on it. When the base branch has not moved, nothing changes. Rounds
  that a person starts with `/nexkit fix`, and automatic rounds started only by review
  findings while the checks passed, still merge the base branch only on a conflict. See
  [Failing checks the base branch fixed](docs/how-it-works.md#failing-checks-the-base-branch-fixed).
- With `conflicts.auto_resolve`, an automatic merge refused because of a conflict starts
  its conflict round at once (#96). The round that tried the merge counted itself as a
  running round and skipped the pull request, which then waited for a person or the
  hourly schedule. The limits stay: one round per base commit, at most
  `conflicts.max_rounds`.

## 4.1.0

- **Autopilot** works through the open issues in scope by itself (#86, #87, #88, #89):
  with `"autopilot": {"enabled": true}`, it plans each ready issue, starts `/nexkit go` on
  a plan that needs nobody, splits a plan that is too large into sub-issues, and moves on
  when an issue closes. It stops where a person is needed. The scope is the earliest open
  milestone, or the issues with `autopilot.label`; an issue is ready when no issue that
  blocks it is open. With `answer: "recommended"`, it answers questions that all have a
  recommended option. With `merge.after_workflows`, a failure of those workflows on the
  default branch is re-run once, then gets a `bug` issue that is planned first. Sweeps run
  in the `maintain` job, without an agent or a repository checkout, after every NexKit
  run, on issue and milestone events and on the hourly schedule. Autopilot is off by
  default. See [Autopilot](docs/configuration.md#autopilot).
- Plan questions, and questions of a `blocked` round, carry their options and the one
  NexKit recommends; the plan comment marks it `(recommended)` (#88, #94). Each part of a
  suggested split names the earlier parts it needs, shown as "(after 1 and 2)", and
  autopilot records them as GitHub *blocked by* relationships, so parts that do not need
  each other run at the same time (#91).
- The `request` skill writes a split as a parent issue with sub-issues, *blocked by*
  relationships only for real prerequisites, and the milestone or label of autopilot's
  scope (#92).
- Setup and checks get `NEXKIT_VERIFIED_SHA`: the head of the latest earlier round of the
  pull request whose checks all passed, unset when there is none (#82). A check can diff
  against it to skip work that later rounds did not touch. See
  [Skipping work that a change cannot affect](docs/configuration.md#skipping-work-that-a-change-cannot-affect).
  Round markers now count only at the end of a NexKit comment, so agent output quoted in
  a comment cannot pose as a round whose checks passed.
- A review survives diffs that are not valid UTF-8 (#81): invalid bytes become U+FFFD. A
  stage that stops on an unexpected exception says so, as
  `NexKit stopped on <Type>: <message>` in the round's comment and the `nexkit/review`
  status, instead of "the review job left no result".
- `nexkit status` shows what the next autopilot sweep does, and `nexkit doctor` checks
  autopilot's triggers and concurrency group when it is on.
- The documentation is reorganized: migration notes are under
  [Upgrading from earlier releases](docs/configuration.md#upgrading-from-earlier-releases),
  and [Troubleshooting](docs/troubleshooting.md) is grouped by topic.

### Upgrade

- **To use autopilot**, give `.github/workflows/nexkit.yml` the triggers and the
  concurrency group that `nexkit init` writes, then turn it on in `.nexkit/config.json`.
  The workflow needs:

  ```yaml
  issues:
    types: [closed, opened, reopened, labeled, milestoned]
  milestone:
    types: [closed]
  ```

  `milestone` in the job's `if:` list of events, and the group:

  ```yaml
  group: nexkit-${{ (github.event_name == 'milestone' || (github.event_name == 'issues' && github.event.action != 'closed')) && 'autopilot' || github.event.issue.number || github.event.pull_request.number || inputs.number || github.event_name }}
  ```

  Without autopilot, the workflow can stay as it is.

## 4.0.0

- **Breaking:** NexKit supports and tests one runtime, Claude Code. The `codex` runtime
  and the `azure-openai` provider are removed: Codex on Azure AI Foundry hit token rate
  limits and authentication errors in practice, and it could not be smoke-tested. A
  configuration that still declares a Codex agent is rejected with what to do. Agents stay
  as they are: declare as many as you need, each with its own secrets, chosen per stage
  and profile, with fallbacks; a runtime can be added again as a module in
  `nexkit/runtimes/` with its own smoke test. See
  [Codex removed in 4.0](docs/configuration.md#codex-removed-in-40).
- A review that does not complete says why, in the round's comment, the issue's run
  comment and the `nexkit/review` status (#74). An authentication error points to
  [Troubleshooting](docs/troubleshooting.md) instead of suggesting `/nexkit review`, which
  would stop the same way.
- Claude Code keeps a result that it accepted even when the model writes more afterwards,
  instead of ending with "finished without the required structured result" (#75).
  Background tasks are turned off, and the implement and fix prompts say to run commands
  in the foreground: a check left running in the background used to end the run without
  a result. A run that still ends that way says so.
- An implement round publishes when the base branch changed files under
  `.github/workflows/` while the agent worked (#76). Pushing with the Actions token, the
  pull request then starts at the base branch's tip instead of the commit the agent
  started from; if the change does not apply there, the round says to run `/nexkit go`
  again instead of showing git's error.
- Every comment and summary names each stage's agent and model the same way (#78), for
  example "**Agents:** plan `claude` (`claude-opus-5-5`) · implement `claude`
  (`claude-sonnet-5-5`) · review `claude` (`claude-fable-5-1`)": the run comment, the
  plan comment's profile line and the job summary, including fallbacks and pauses.

### Upgrade

- **Remove Codex agents** from `.nexkit/config.json`, if any: delete each agent with
  `"runtime": "codex"` from `agents`, and give the stages and profiles that chose it a
  Claude agent and model, or drop `agent` there to use the default. For example:

  ```json
  "review": {"agent": "claude", "model": "claude-opus-5-5"}
  ```

  NexKit 4.0.0 no longer reads the Azure secrets; delete them when nothing else uses
  them. See [Codex removed in 4.0](docs/configuration.md#codex-removed-in-40).

## 3.2.0

- `nexkit doctor` reports a problem when the `uses:` ref and `nexkit_ref` in
  `.github/workflows/nexkit.yml` differ, or `nexkit_ref` is missing: the jobs check out
  NexKit's code at `nexkit_ref`, so they would run another release's code than the
  pipeline (#69). See [Troubleshooting](docs/troubleshooting.md).
- `nexkit doctor` mentions a NexKit release newer than the pinned tag, with a link to its
  notes. Staying on a release is a choice, so this is a ✓ line and doctor still passes.
- Release notes always end with the upgrade steps and the install commands again (#68).
  They are printed by `scripts/release_notes.py` from this changelog; the 3.0.0 entry now
  lists its upgrade steps.

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

### Upgrade

- In `.github/workflows/nexkit.yml`, replace the `secrets:` block that passes
  `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and `NEXKIT_PUSH_TOKEN` one by one with
  `secrets: inherit`.
- **Declare your agent** in `.nexkit/config.json`: NexKit 3.0.0 rejects a configuration
  without `agents`. For a Claude subscription:

  ```json
  "agents": {
    "claude": {"runtime": "claude-code", "provider": "anthropic", "oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN"}
  }
  ```

  With an Anthropic API key, write `"api_key_secret": "ANTHROPIC_API_KEY"` instead of
  `oauth_token_secret`. See [Agents are declared](docs/configuration.md#agents-are-declared).
- If you set a `NEXKIT_PUSH_TOKEN` secret, add
  `"publish": {"push_token_secret": "NEXKIT_PUSH_TOKEN"}`. Without it, NexKit pushes with
  the Actions token. `nexkit doctor` warns about a push token secret the configuration
  does not name.

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
