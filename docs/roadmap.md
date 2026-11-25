# Roadmap

NexKit's work is grouped by feature. Each feature has one milestone on GitHub, and every issue and
pull request belongs to one of them. A milestone is open while work on its feature is planned or
under way, and closed when its feature is done. What each release changed is in the
[changelog](../CHANGELOG.md).

## Done

These features make up NexKit 6, released from 1.0.0 (2026-10-05) to 6.12.2 (2026-11-20).

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

## Under way

| Milestone | What it delivers | Status |
| --- | --- | --- |
| [Engine and hosts](https://github.com/phuongnse/nexkit/milestone/1) | The loop's decisions in one engine that any host runs, as [The engine](engine.md) describes. NexKit 7.0 ships when the GitHub host runs only through it (#320) | In progress |

Built so far: the protocol's schemas, `nexkit-engine step`, `prompt` and `serve`, the state
document and its import from NexKit 6's markers, the differential check against NexKit 6, and the
decisions that follow a plan, a blocked `/nexkit go` and a round on a pull request. Left: the
accept, the waits, the sweep, the runner and the rest that [Getting there](engine.md#getting-there)
lists.

## Later

- Hosts beyond GitHub, such as an application whose users ask an agent to change what the
  application holds, as [Why](engine.md#why) describes. Each runs the same engine.
