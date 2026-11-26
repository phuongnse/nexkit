# D2. NexKit's pipeline writes only as the App

Status: **Agreed**

## Context

GitHub's Actions token cannot start other workflows, and it cannot change files under
`.github/workflows/`. A GitHub App's writes count like a person's, so they start workflows and
may change workflow files. NexKit's work needs both, so it needs an identity that can do them.

## Decision

- NexKit's pipeline writes on a repository only as that repository's GitHub App.
- Each job that writes mints its own token, with only the permissions that job needs. The token
  expires within an hour.
- One kind of exception: the jobs that maintain NexKit's own repository write with
  `github.token`. They run no repository code and hold no secret. They publish the engine's
  image, attach its conformance suite to the release, propose pin updates, file the docs audit's
  issues and upload code scanning results.
  - The package registry takes no App token.
  - Storing the App's key for these jobs would put it on more runners, for writes that need no
    workflow to start: the update jobs start CI on their branch themselves, and autopilot's
    sweep takes the issues the audit opens.

The detail is in [The GitHub App](../configuration.md#the-github-app),
[Releases](../delivery.md#releases) and
[Issues from a workflow](../autopilot.md#issues-from-a-workflow).

## Consequences

- Pushes and merges by NexKit start the repository's workflows, as a person's would.
- The pull requests and issues of NexKit's maintenance jobs start no workflow, so those jobs
  start what must follow.
- Each owner creates an App of their own. NexKit publishes no shared App, so no shared private
  key exists.
- Each further repository of a personal account needs the App's key set again, which is one more
  step to do.

## Alternatives

- A shared App that NexKit's author publishes. It would need its private key in one place for
  every repository that uses it.
- The Actions token. It is simpler, but it cannot start workflows or change workflow files,
  which NexKit needs to do.
