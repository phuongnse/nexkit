# D5. Agents cannot change the pipeline

Status: **Agreed**

## Context

The workflows and NexKit's configuration decide what runs and what may write to GitHub. An agent
that could edit them could widen its own permissions or skip the checks, and no review would
see the change before it ran.

## Decision

- Agents may not change protected paths: `.github/`, `.nexkit/`, and any other path that
  `protected_paths` names.
- NexKit rejects an agent's patch that touches them. A change that needs them stops for a person,
  with the paths named.
- A person's own pull request may change them. `/nexkit review` on such a pull request runs the
  checks and the review. It never pushes, starts a round or merges.

The detail is in [Security notes](../how-it-works.md#security-notes) and
[Reviewing your own pull requests](../configuration.md#reviewing-your-own-pull-requests).

## Consequences

- A change to the pipeline always passes through a person, who makes it by hand.
- NexKit cannot upgrade itself. A NexKit upgrade is a person's pull request.
- The review reads protected paths like any other code, so a person sees each change to the
  pipeline before it merges.

## Alternatives

- Let agents edit the pipeline and rely on review. A review can miss a change to permissions, so
  the rule stays strict.
