# D15. Agent errors and blocked results never retry automatically

Status: **Proposed**

## Context

Running an agent again after it failed inside the work spends model usage on the same problem. An
agent that cannot fix a problem with its inputs will not fix it by trying again. Some stops are not
a problem with the inputs, such as an outage or a session that ended without a result that fits.

## Decision

- An agent error or a `blocked` result stops the round. NexKit starts no automatic round after it,
  apart from what autopilot answers or splits, and the two retries below.
- Autopilot plans again after a blocked or failed round once a person comments, or an issue that
  blocks it closes. That plan follows a change, not a repeat of the same inputs.
- After a failure, automatic rounds follow only check failures and review findings, up to
  `fix.max_auto_rounds`.
- With `conflicts.auto_resolve`, NexKit also starts a round by itself when a pull request conflicts
  with its base branch. That round follows a change of the base branch, not a failure of the work.
  It counts against its own limit, `conflicts.max_rounds`. See [Conflicts with the base
  branch](../merges-and-waits.md#conflicts-with-the-base-branch).
- A transient error runs again after the waits of
  [`retry.wait_minutes`](../merges-and-waits.md#transient-errors), before or after publishing, and
  the review included. The stop came from outside the work, so a repeat can succeed.
- An agent run with no result that fits its stage's schema is asked once more, in the same session,
  for its result alone, when its runtime can resume the session and time is left. When no fitting
  result comes, it runs again as after a transient error. A missing result is not a problem the
  agent cannot fix.

The detail is in [Failure handling](../failure-handling.md#failure-handling).

## Consequences

- Model usage is not spent repeating a problem that the agent cannot fix.
- A person answers more stops, and autopilot answers the questions it is allowed to answer.
- A transient error from outside the work, such as a platform outage, retries after its waits, so
  one outage does not end a round.
- A session that ended without its result costs one short request for it, then the runs of
  `retry.wait_minutes`.

## Alternatives

- Retry each agent error a few times. It is simpler, but each repeat costs usage and rarely
  changes the result.
- Treat a missing result as an agent error. That would make a person answer a stop that says
  nothing about the work.
