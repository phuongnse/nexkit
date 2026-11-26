# D2. NexKit writes only as the App

Status: **Agreed**

## Context

GitHub's Actions token cannot start other workflows, and it cannot change files under
`.github/workflows/`. A GitHub App's writes count like a person's, so they start workflows and
may change workflow files. NexKit's work needs both, so it needs an identity that can do them.

## Decision

- NexKit writes on GitHub only as the repository's GitHub App.
- Each job that writes mints its own token, with only the permissions that job needs. The token
  expires within an hour.
- One exception: the `publish` job of `release.yml` pushes the engine's image with
  `github.token`. That job runs no repository code.

The detail is in [The GitHub App](../configuration.md#the-github-app) and
[Releases](../delivery.md#releases).

## Consequences

- Pushes and merges by NexKit start the repository's workflows, as a person's would.
- Each owner creates an App of their own. NexKit publishes no shared App, so no shared private
  key exists.
- Each further repository of a personal account needs the App's key set again, which is one more
  step to do.

## Alternatives

- A shared App that NexKit's author publishes. It would need its private key in one place for
  every repository that uses it.
- The Actions token. It is simpler, but it cannot start workflows or change workflow files,
  which NexKit needs to do.
