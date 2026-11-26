# D11. NexKit works its own issues on a release

Status: **Agreed**

## Context

A session and NexKit must never work the same issue, or they overwrite each other's work. A new
NexKit version also has to prove itself on other repositories before NexKit runs it on its own.

## Decision

- NexKit works this repository's open issues itself, pinned to a release and never to `main`.
- A session outside NexKit takes only issues labelled `hand-made`. It never merges, pushes a tag or
  releases.
- Only the owner releases. The owner moves the pin to a release once it runs well elsewhere.

The detail is in [NexKit on this repository](../delivery.md#nexkit-on-this-repository).

## Consequences

- A change reaches this repository a release after it reaches `main`.
- The owner decides when each release ships, and no agent does.
- Labels divide the issues between NexKit and sessions, so each label must be set correctly.

## Alternatives

- Run `main` on this repository. Changes arrive sooner, but without proof from other repositories.
