# D7. One tested runtime, Claude Code

Status: **Agreed**

## Context

Each agent CLI has its own flags, event stream and result format. NexKit must test each version
it runs against the real CLI, so every extra runtime adds to the checks that each release needs.

## Decision

- NexKit supports one runtime, Claude Code, and tests the version that it pins.
- Everything specific to a CLI or a provider lives in `nexkit/runtimes/`, behind `Runtime` and
  `Provider`.
- No other code branches on a runtime's name.

The detail is in [Agents](../how-it-works.md#agents) and
[The smoke test](../testing.md#the-smoke-test).

## Consequences

- Changes that are specific to the agent stay in one place, and the real CLI tests them.
- A person who wants another agent CLI waits for a runtime to be written and tested.
- A new runtime needs its own module, its own smoke test and a note of how each stage level
  applies to it.

## Alternatives

- Support several CLIs at once, with a matrix of versions. It reaches more users, but each release
  then tests many more versions.
