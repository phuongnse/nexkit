# D3. Agent jobs hold no write token

Status: **Agreed**

## Context

An agent runs commands inside its job. A mistake, or an instruction hidden in an issue, can make
it act with whatever that job holds. A write token, or the App's key, would let a session change
GitHub directly.

## Decision

- A job that runs an agent or repository code has a read-only token and no App key.
- Only the jobs that write hold a write token, and they never run repository code or an agent.
- A job holds only the agent secrets that its stage can use, passed in numbered slots. Names that
  start with `GITHUB_` or `NEXKIT_` are never read.

The rules are in [Tokens and secrets](../architecture.md#tokens-and-secrets) and
[How secrets reach an agent](../agents-and-profiles.md#agents).

## Consequences

- The agent edits a working tree, and a separate job publishes its change. A session cannot push,
  comment or merge.
- Filtering limits which secrets each process inherits. It is not a wall: an agent that can run
  commands can read other processes on the runner, so it may see every secret its job holds.
- Each writing job needs its own token step, which adds lines to the workflows.

## Alternatives

- Give the agent's job a write token. It is simpler, but a session could then write to GitHub
  directly.
