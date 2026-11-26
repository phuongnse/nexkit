# D1. State lives only on GitHub

Status: **Agreed**

## Context

A run can start again, or start while another run works on the same item. Each run therefore
has to read the state of a work item from one place that every run shares. NexKit has no server
of its own to hold that state.

## Decision

- All NexKit state lives on GitHub: in comments on the issue or pull request, and in commit
  statuses.
- Nothing outside GitHub holds state that a later run needs.
- A state write must come from the state that its writer read, so a stale copy cannot replace a
  newer one.

The detail is in [How it works: State](../how-it-works.md#state) and
[Architecture: State](../architecture.md#state).

## Consequences

- A run can be retried or can overlap another run without losing track of an item, and a person
  can read the state on GitHub.
- No server, database or self-hosted runner is needed, so setup stays small.
- GitHub has no compare-and-set for comments. A write by someone else between a read and an edit
  can be overwritten. The revision check narrows that race, and does not close it.

## Alternatives

- A database or a hosted service that holds the state. It needs hosting and a second source of
  truth, which goes against running on GitHub's own runners.
