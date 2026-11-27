# D4. Outside text is untrusted

Status: **Agreed**

## Context

Agent output, comments and CI output come from people and programs that NexKit does not control.
Such text can carry secrets, steer an agent, or imitate a record that NexKit wrote.

## Decision

- NexKit checks each agent result against its stage's schema before it publishes anything.
- Everything that a session prints or stores passes through `nexkit/redact.py`. Tool output is
  printed with workflow commands stopped.
- NexKit reads one of its markers only in an App comment that no one else changed, and only where
  NexKit writes it. Where agent text stands next to a marker that NexKit writes, NexKit writes
  `<!-- nexkit:` in it as `‹!-- nexkit:`, so it cannot open one.
- Discussion and review feedback count only from collaborators.

The detail is in [Security notes](../how-it-works.md#security-notes) and
[What is redacted](../how-it-works.md#what-is-redacted).

## Consequences

- Text in a comment or an agent's output cannot change NexKit's behaviour by imitating its record.
- Redaction is best effort. It cannot know every secret, so real secrets must stay out of the
  agent's environment.
- On a public repository, anyone signed in to GitHub can read the logs and download the artifacts.

## Alternatives

- Trust agent output as written. It is simpler, but a session could then publish a forged plan or
  approval.
