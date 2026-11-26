# D13. The pipeline writes as the App, and maintenance jobs with the Actions token

Status: **Agreed**

## Context

GitHub's Actions token cannot start other workflows, and it cannot change files under
`.github/workflows/`. A GitHub App's writes count like a person's, so they start workflows and
may change workflow files. NexKit's pipeline needs both, so it needs an identity that can do them.

NexKit's own repository also runs jobs that maintain it: they publish the engine's image,
attach its conformance suite to the release, propose pin updates, file the docs audit's issues
and upload code scanning results. None of them
needs to start a workflow by its write or to change a workflow file. An App's private key acts on
every repository where the App is installed, and it does not expire.

## Decision

- NexKit's pipeline writes on a repository only as that repository's GitHub App.
- Each job that writes mints its own token, with only the permissions that job needs. The token
  expires within an hour.
- The jobs that maintain NexKit's own repository write with `github.token`. They run no
  repository code, hold no secret and never read the App's key.

The detail is in [The GitHub App](../configuration.md#the-github-app),
[Releases](../delivery.md#releases) and
[Issues from a workflow](../autopilot.md#issues-from-a-workflow).

## Consequences

- Pushes and merges by the pipeline start the repository's workflows, as a person's would.
- Each owner creates an App of their own. NexKit publishes no shared App, so no shared private
  key exists.
- Each further repository of a personal account needs the App's key set again, which is one more
  step to do.
- The pull requests and issues of the maintenance jobs start no workflow, so those jobs start
  what must follow: the update jobs start CI on their branch, and autopilot's sweep takes the
  issues the audit opens.
- A broken action in a maintenance job reaches at most this repository, and only while its job
  runs.
- A test fails when a job of the pipeline's workflows can write with `github.token`, and when a
  job that can holds a secret or an App token.

## Alternatives

- A shared App that NexKit's author publishes. It would need its private key in one place for
  every repository that uses it.
- The Actions token for the pipeline. It is simpler, but it cannot start workflows or change
  workflow files, which the pipeline needs to do.
- An App token for the maintenance jobs too. Their pull requests and issues would start
  workflows by themselves. But each of those jobs would hold the App's key, so a broken action in
  one of them could act on every repository where the App is installed, for as long as the key
  is valid. The package registry takes no App token either, so publishing the image would still
  write with `github.token`.
