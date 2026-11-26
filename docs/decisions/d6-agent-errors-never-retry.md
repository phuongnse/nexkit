# D6. Agent errors never retry

Status: **Agreed**

## Context

Running an agent again after it failed inside the work spends model usage on the same problem. An
agent that cannot fix a problem with its inputs will not fix it by trying again.

## Decision

- An agent error or a `blocked` result stops the round. NexKit starts no automatic round after it,
  apart from what autopilot answers or splits.
- Automatic rounds follow only check failures and review findings, up to `fix.max_auto_rounds`.
- A transient error before publishing runs again after the waits of `retry.wait_minutes`. The stop
  came from outside the work, so a repeat can succeed.

The detail is in [Failure handling](../failure-handling.md#failure-handling).

## Consequences

- Model usage is not spent repeating a problem that the agent cannot fix.
- A person answers more stops, and autopilot answers the questions it is allowed to answer.
- A transient error from outside the work, such as a platform outage, retries after its waits, so
  one outage does not end a round.

## Alternatives

- Retry each agent error a few times. It is simpler, but each repeat costs usage and rarely
  changes the result.
