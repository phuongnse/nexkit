# Roadmap

NexKit's work is grouped by feature. Each feature has one milestone on GitHub, which holds the
issues and pull requests that its row below names; the rest, such as a fix of a release or a
refactor, have only the label of their kind, as [Delivery](delivery.md#labels-and-milestones) says.
A milestone is open while work on its feature is planned or under way, and closed when its feature
is done. What each release changed is in the [changelog](../CHANGELOG.md).

Labels give each issue and pull request its kind (`enhancement`, `bug`, `refactor`, `process`
or `documentation`) and say more where they fit, such as `security`. Each label's description on
GitHub says when it fits.

## Done

These features make up NexKit 6, released from 1.0.0 (2026-10-05) to 6.13.1 (2026-11-26).

| Milestone | What it delivers | Since |
| --- | --- | --- |
| [Commands and rounds](https://github.com/phuongnse/nexkit/milestone/8?closed=1) | `/nexkit plan`, `go` and `fix` and their rounds; publishing a change as a pull request; a comment for each run with its progress; the state comment; what an agent reads of its issue | 1.0 |
| [Review](https://github.com/phuongnse/nexkit/milestone/9?closed=1) | An independent review of each round: findings, a status for each acceptance criterion, the repository's review checklist, and `/nexkit review` of a person's pull request | 1.0 |
| [Checks](https://github.com/phuongnse/nexkit/milestone/10?closed=1) | `setup` and checks with caches, the paths a pull request changed, slow checks, parallel jobs and shards, result files, and skipping what has not changed | 1.0 |
| [Fix rounds and CI failures](https://github.com/phuongnse/nexkit/milestone/11?closed=1) | Bounded automatic fix rounds and diagnosis rounds, CI evidence, and issues for CI failures and flaky runs on the default branch | 1.0 |
| [Merge and conflicts](https://github.com/phuongnse/nexkit/milestone/12?closed=1) | Automatic merge and merges GitHub refused, conflict rounds, keeping a pull request up to date with its base, closing issues and parent issues after a merge | 1.9 |
| [Agents and runtimes](https://github.com/phuongnse/nexkit/milestone/14?closed=1) | Profiles that choose models for each issue; agents as a runtime and a provider, with fallbacks; usage limits; runs again after transient stops | 1.7 |
| [Autopilot](https://github.com/phuongnse/nexkit/milestone/13?closed=1) | Autopilot through every open issue: sweeps, priority, waves and holds, answers and splits, lost runs, and what waits for a person | 4.1 |
| [GitHub App and security](https://github.com/phuongnse/nexkit/milestone/15?closed=1) | Jobs that keep the agent away from writes, secrets named in the configuration, redaction, markers, and writes only as the repository's GitHub App | 1.0 |
| [Setup, status and skills](https://github.com/phuongnse/nexkit/milestone/16?closed=1) | `nexkit init`, `doctor` and `status`, the release the CLI runs, the plugin's skills and the docs for people | 1.0 |
| [Releases and upkeep](https://github.com/phuongnse/nexkit/milestone/17?closed=1) | Release pull requests, runtime pin updates, docs sweeps and NexKit's own tests | 1.0 |
| [Foundation](https://github.com/phuongnse/nexkit/milestone/18?closed=1) | NexKit's own docs, delivery and checks at the standard it asks of the repositories it runs on: a map of the docs, short agent instructions, architecture, security, decisions, CodeQL, a docs audit, the owner's maintenance skill, and NexKit working its own issues | 6.13 |

## Under way

| Milestone | What it delivers | Status |
| --- | --- | --- |
| [Engine and hosts](https://github.com/phuongnse/nexkit/milestone/1) | The loop's decisions in one engine that any host runs, as [The engine](engine.md) describes. NexKit 7.0 ships when the GitHub host runs only through it | In progress |

Built so far: the protocol's schemas, `nexkit-engine step`, `prompt`, `sweep` and `serve`, the state
document and its import from NexKit 6's markers. The
GitHub host runs the engine only as the `nexkit-engine` program and imports none of its code. It
sends the engine every decision that moves an item. That covers a person's command and
a closing command, a plan and what follows it, and a new issue's first plan. It also covers a
blocked or failed `/nexkit go`, a writer's vouch and approval, a round on a pull request, and a
stopped or timed-out round. Conflicts after the base branch moves are sent too, as is the accept of
a merge. GitHub's refusals, the merge's result, the waits for required checks and for the base
branch, the retries after a transient stop and the resumes after a usage limit are sent as well.
So are a lost command, the runner's `run.json`, the ends of an item with no change or with its
change on the base branch, and a parent's end. The sweep sends its lost commands, released
conflicts, first plans and order of starts. Left: some decisions still build the item's document
from NexKit 6's markers. [Getting there](engine.md#getting-there) gives the detail.

## Later

- Hosts beyond GitHub, such as an application whose users ask an agent to change what the
  application holds, as [Why](engine.md#why) describes. Each runs the same engine.
