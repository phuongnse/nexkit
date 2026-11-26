# The engine against NexKit 6

Each change that moves a NexKit 6 decision into the engine keeps GitHub's behaviour of
NexKit 6 at 8bf5190, and keeps the engine's invariants
([Moving decisions into the engine](../../docs/testing.md#moving-decisions-into-the-engine)). This folder holds the checks that show it. They are a tool of the move to the engine:
they go with NexKit 6's code once 7.0 ships.

Nothing here is a test that `unittest discover` runs: the folder is no package, and no file's
name starts with `test`. Standard library only.

## The differential check

```sh
git worktree add --detach ../nexkit-v6 8bf5190
python3 tests/differential/harness.py --v6 ../nexkit-v6               # every scenario
python3 tests/differential/harness.py --v6 ../nexkit-v6 --sample 400  # what CI plays
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

`play.py ROOT FAMILY OUT --only I` plays one scenario on one tree, to look at it.

| Family | Pull request | What it plays |
|---|---|---|
| `report` | #341 | `report` after a round's checks and review |
| `stop` | #341 | a fix round or `/nexkit go` that stopped, then a sweep |
| `sweep` | #344 | sweeps over a round that waits for a hold or the base branch |
| `round` | #344 | a round after such a wait |
| `required` | #344 | required checks that fail at the merge |
| `give` | #344 | rounds that stop, and which of them count |
| `diag` | #347 | chains of rounds, each started through `route`: diagnosis rounds |
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
| `failure` | #415 | a `/nexkit go` that stopped as blocked with nothing to answer, then a comment or a blocker's close, with autopilot on or off |
| `new` | #438 | the first plan of a new issue, with a hold label, a blocker, a lower wave, a failed plan or `/nexkit go`, an author outside the repository, `max_parallel` with another issue busy, and a parent |
| `conflicted` | #423 | a merge refused for a conflict, or found conflicting at the end of the refusal, under both `conflicts.auto_resolve` values: the round's own merge, a sweep, a writer's approval, with conflict rounds used, a round on its way and `mergeable` unknown |

A family is a module with `scenarios()`, which yields each scenario as plain JSON in a fixed
order whatever tree plays it, and `run(sc)`, which plays one and returns what it recorded. A time
a scenario fixes, such as when a usage limit resets, is in 2099: the comparison keeps those and
drops the clock's.
`common.py` holds what they share. A change adds a family for what it moves, and adds to
`known.py` each difference from NexKit 6 that its pull request calls out.

## The random walks

```sh
python3 tests/differential/walk.py                 # 2,000 walks of seed 349
python3 tests/differential/walk.py --walk 17 --show
```

`walk.py` sends random events to the engine's `step`, with random policies that change now and
then, and checks the invariants that `INVARIANTS` lists after every step: the protocol's
schemas, the transitions the states table names, the outbox, every open item carried on,
counts within the policies, which rounds give their count back, `due` and `automatic`,
diagnosis, usage-limit waits, runs again after a transient stop, holds and conflict rounds, the
wait for required checks with its six hours, which items a `requested` start starts, and the
engine's answers to a round's questions.
Some walks start from the example of an item that accepts, which a walk from a new item seldom
reaches. Each rule is checked both ways where it can be: a count neither passes its policy
nor fails to come back when its round stops. It exits 1 when one breaks that `KNOWN` does not
list, or when the engine refuses more than a tenth of the events, as walks it refuses check
nothing. A change adds invariants for the rules it adds.

## Mutation checks

```sh
python3 tests/differential/mutants.py --v6 ../nexkit-v6
```

`mutants.py` breaks one rule in a copy of the tree for each of `MUTANTS`, plays the families
that cover it and walks the engine, and says whether each check that should see the broken rule
did. No CI job runs it: it takes several minutes. A change adds mutants of its own rules and
runs it.

The engine's state lives only for one call of the host while the GitHub host builds each
item's document from NexKit 6's markers. A rule that only matters across calls, such as
keeping `rounds.automatic` after a round's checks or review judged it, shows to the walker but
not on GitHub, so only the walker can see it broken.
