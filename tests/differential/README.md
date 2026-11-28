# The engine against NexKit 6

Each change that moves a NexKit 6 decision into the engine keeps GitHub's behaviour of
NexKit 6 at 8bf5190, and keeps the engine's invariants
([Moving decisions into the engine](../../docs/testing.md#moving-decisions-into-the-engine)). This folder holds the checks that show it. They are a tool of the move to the engine:
they go with NexKit 6's code once 7.0 ships.

Nothing here is a test that `unittest discover` runs: the folder is no package, and no file's
name starts with `test`. The one test that reads it is `tests/test_differential.py`, which loads
the families that play a round's report. The engine's mutation check reads nothing of it. Standard library only.

## The differential check

```sh
git worktree add --detach ../nexkit-v6 8bf5190
python3 tests/differential/harness.py --v6 ../nexkit-v6               # every scenario
python3 tests/differential/harness.py --v6 ../nexkit-v6 --sample 400  # what check.sh plays
```

`harness.py` plays every family of `families/` on NexKit 6, which must be a checkout of
8bf5190, and on this tree (`--tree` for another), each scenario on a fresh `FakeGitHub` of the tree it plays, in shards across
`--jobs` processes. It compares what each records: the command's result, everything on GitHub
after it (comments with NexKit's markers decoded and the clock's times removed, statuses, reviews,
dispatches with their inputs, merges, issues, pull requests, labels), the preview and
`nexkit status`. It prints each family's count, the differences that `known.py` lists, and every
other difference grouped by where it differs, with an example, and exits 1 when there is one.
`--family` plays only some families, `--sample K` the same K scenarios of each every time, and
`--keep DIR` keeps the records.

Before its first event, each scenario stores the import of every open item on the tree as the App's
state comment at revision 1 (`common.seed`), as a run in the item's group does
(`host.store_import`): the open issues, and the open pull requests that are not a
`nexkit/issue-<n>` branch of the repository. On NexKit 6 nothing is stored, but the same comment ids
are used up, so that every later comment has the same id on both trees. The state comment is not
compared, since NexKit 6 never posts it. The harness records each write of it apart, and counts a
problem when an item's revision moves backward within a scenario. The line of each family says how
many of its scenarios stored an item (`seeded k of n`). The `stop`, `pause` and `ended` families,
and every family whose report plays an implement, fix or review round, store the import just
after the round's `progress.start`, so that the stored state holds the agent run on its way, as
it does when a run in the item's group stores it. The scripted agents of those rounds give output
that fits the stage's result schema, so that the report stores the agent result and the publish
result (`host.round_published`) before it decides on the stored state. A fix round publishes a new
head, unless it changed nothing: the pull request is at the head the round started from until the
import, and at the published head after it. The `diag` family's `route` stores each round itself
before its `progress.start`, and `refusals` stores the import before the patches that make GitHub
fail. An implement round starts from the import of a pull request already in its checks.

`play.py ROOT FAMILY OUT --only I` plays one scenario on one tree, to look at it.

| Family | Pull request | What it plays |
|---|---|---|
| `report` | #341 | `report` after a round's checks and review |
| `stop` | #341 | a fix round or `/nexkit go` that stopped, then a sweep |
| `sweep` | #344 | sweeps over a round that waits for a hold or the base branch |
| `round` | #344 | a round after such a wait |
| `required` | #344 | required checks that fail at the merge |
| `give` | #344 | rounds that stop, and which of them count |
| `diag` | #347 | chains of rounds, each started through `route` with the configuration: diagnosis rounds |
| `many` | #347 | the same with 40 checks |
| `conflict` | #347 | a round whose commit conflicts, held or not, then released |
| `review` | #347 | a review that failed, paused or is missing, then runs again |
| `chains` | #347 | random chains of rounds with long, sharded and repeated check names |
| `released` | #347 | a held conflict round released while `mergeable` flips |
| `waits` | #390 | a merge that waits for required checks and its six hours: the round's own merge, a sweep, a writer's approval |
| `refusals` | #391 | a merge refused because the head moved, or for a reason only a person clears: the round's own merge, a sweep, a writer's approval |
| `pause` | #398 | a plan, implement or fix stage that stopped at a usage limit or on a transient error, then the resume that comes due, under a hold or not |
| `ended` | #393 | what a merge finds and a parent's end: a pull request a person merged or closed, a change the base branch holds, a head pushed while the merge waits, and the parent after the merge |
| `replan` | #413 | a pull request's round that ran out of time, then the plan it asked for: too large or not, parts, titles, depth, autopilot's record, questions, no change, protected paths |
| `failure` | #415, #537 | a `/nexkit go` that failed with no pull request (blocked with nothing to answer, an agent error, `agent_failed`, `publish_failed`, `retry_failed` or timed out), then a comment or a blocker's close, with autopilot on or off |
| `new` | #438 | the first plan of a new issue, with a hold label, a blocker, a lower wave, a failed plan or `/nexkit go`, an author outside the repository, `max_parallel` with another issue busy, and a parent |
| `conflicted` | #423 | a merge refused for a conflict, or found conflicting at the end of the refusal, under both `conflicts.auto_resolve` values: the round's own merge, a sweep, a writer's approval, with conflict rounds used, a round on its way and `mergeable` unknown |
| `behind` | #424 | a merge refused because the branch is behind its base, under both `conflicts.auto_resolve` values: the round's own merge, a sweep that waited 0 hours, 1 hour or past six, a writer's approval, with updates used, `conflicts.max_rounds` 1 or 3, an update or a review's dispatch that GitHub refuses, and required checks pending or passed |
| `start` | #437 | which waiting issues start, and in what order: priority labels, two milestones or none, waves, the milestone or every-issue scope, `max_parallel`, busy issues, and a bug issue or a part of one that pull requests wait for. 400 scenarios drawn from a fixed seed |
| `moved` | #530 | the base branch moved under an open NexKit pull request: a push to it, the hourly schedule, NexKit's merge of another pull request, or an implement round's report, with the pull request conflicting, behind or neither, its round in its checks, in its agent or ended, the rules wanting up-to-date branches or not, conflict rounds and updates used, `conflicts.max_rounds` 1 or 3, a hold label, and for an implement round's report, a review that passed or failed, and for a push or the schedule, a latest round that ended ready, failed, lost its run or chained a round that never ran. 481 scenarios |
| `parents` | #531 | how every parent ends, at every level and however its last part closed: a person's close, the round's merge, no change needed, a change the base branch holds, or autopilot's sweep, with one or two levels of parents, a part closed completed, not planned or with no reason, a sibling part open, completed or not planned, more than ten sub-issues, a part never filed, an open part of the grandparent, and `close_parent_issues` on or off |
| `vouch` | #538 | a writer's vouch after an untrusted edit stopped an issue's `/nexkit go`, a round of its pull request or its merge, with no vouch, one before the stop, a reader's or a writer's after it, and automatic fix rounds left or used up, for an automatic round or a person's |
| `cleared` | #570 | a merge that GitHub refused, which the sweep tries again once GitHub reports the pull request as mergeable: `mergeable_state` `clean`, `unstable`, `has_hooks`, `blocked`, `behind` or `dirty`, a head that moved, a head refused while mergeable (`refused_clean`), `merge.auto` on or off, and the retried merge going through or refused again. A sweep's merge with no `waiting_since` is the `waits` family's, and a parent whose parts all closed before any close run is the `parents` family's |
| `approvals` | #569 | a writer's approval of a NexKit pull request, which the approval's `step` runs as the `approved` task: after a merge GitHub refused, after a ready round, a ready round with a checklist item a person decides, a pull request reopened after its round found it closed and a round that needs a person, with `merge.auto` on and off, the approval of the head, of no commit and of another one, a hold label, an implement round that still runs and a head that moved. NexKit 6 has no run that only sends the `step`, so only the merge is played on both trees. 240 scenarios |
| `command` | #568 | a person's command, a *Request changes* review and NexKit's own dispatch (an automatic fix round, a conflict round, a review), for plan, go, fix, review, trust and an unknown command, on an open issue with a plan or none, a closed one, one whose pull request is open or closed, a NexKit pull request that is open or closed (with the round that the implement run which opened it posts), whose base branch holds its change or whose implement round runs, a person's pull request that is open, closed or on the base branch, a fork's, and a note that picks the option that only closes the issue. 323 scenarios |

A family is a module with `scenarios()`, which yields each scenario as plain JSON in a fixed
order whatever tree plays it, and `run(sc)`, which plays one and returns what it recorded. A time
a scenario fixes, such as when a usage limit resets, is in 2099: the comparison keeps those and
drops the clock's.
`common.py` holds what they share. A change adds a family for what it moves, and adds to
`known.py` each difference from NexKit 6 that its pull request calls out.

## The random walks

The engine's random walks are no longer a part of this folder: `tests/walk.py` plays them, with
no NexKit 6, and they stay after 7.0. `tests/test_walk.py` plays a fixed sample of them in the
unit tests, and `check.sh` plays 1,000 more here until 7.0. A change adds the invariants of its
rules to `tests/walk.py`. Its docstring says what the walker checks, and
[Which check covers what](../../docs/testing.md#which-check-covers-what) lists it.

## Mutation checks

```sh
python3 tests/differential/mutants.py --v6 ../nexkit-v6
```

`mutants.py` breaks one rule in a copy of the tree for each mutant, plays the families that cover
it and says whether the differential check saw it. No CI job runs it: it takes several minutes.

The mutants of the engine's rules live in `tests/mutants.py`, which needs no NexKit 6 and stays
after 7.0: the conformance suite or the walker must see each one. A change adds mutants of its
engine rules there. Only the mutants that the differential check alone sees stay here: those of
host code, whose rules are NexKit 6's GitHub behaviour. This file plays the engine's mutants too,
with the families of `ENGINE` that cover them, so that a change proves its rules against NexKit 6
until 7.0. It lists `start-resumes-a-blocked-item-on-a-conflict` as well, which no check sees
today.

The GitHub host decides every event on the item's stored state, or on the import in memory when
none is stored, and stores its answer in the item's state comment. Only the first import builds a
document from NexKit 6's markers. Every family stores the import of its items before its first
event (`common.seed`), so a decision meets a stored state. The `lost` family seeds a stored state
whose count of losses the import keeps, since a lost command counts its losses from the stored
`again.lost`. The import of a finished plan of an issue that autopilot takes plays the plan's
result through the engine, so that state holds the engine's answer, which `after_plan` carries
out. Each family that plays a round's report stores the item's state just after the round's
`progress.start`, with a scripted agent whose output fits the stage, so that the report stores the
round before it decides the checks. The `chains` family, which dispatches rounds in any order,
stores the import again as the next revision for each round after the first, as its dispatch
stores the round in progress. The counts of conflict rounds and automatic fix rounds are the
stored ones, so a round that the stored state counts and NexKit 6's markers do not yet can
differ, which `known.py` lists with its reason.

A rule that only matters across calls, such as keeping `rounds.automatic` after a round's checks
or review judged it, shows to the walker but not on GitHub, so only the walker can see it broken.
