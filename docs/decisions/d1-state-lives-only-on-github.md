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
- A write of a state that several runs share must come from the state that its writer read, so a
  stale copy cannot replace a newer one. These are the pipeline's state comment, autopilot's
  record comment and each work item's state document.

The detail is in [How it works: State](../how-it-works.md#state) and
[Architecture: State](../architecture.md#state).

## Consequences

- A run can be retried or can overlap another run without losing track of an item, and a person
  can read the state on GitHub.
- No server, database or self-hosted runner is needed, so setup stays small.
- GitHub has no compare-and-set for comments. A write by someone else between a read and an edit
  can be overwritten. Reading one of those three shared comments again just before each edit
  narrows that race, and does not close it.
- A run or round comment is edited from the body its writer read earlier, without that check. A
  write that comes in between can be lost.

## Alternatives

- A database or a hosted service that holds the state. It needs hosting and a second source of
  truth, which goes against running on GitHub's own runners.
