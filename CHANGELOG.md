# Changelog

## 1.2.2

- Publish protected administrative workflow changes with a dedicated,
  repository-scoped GitHub App token, while retaining Actions-owned PRs/checks
  and actual native administrator approval under unchanged protection.
- Verify App identity, installation and effective token permissions separately
  from administrator preflight; revoke temporary credentials after use and keep
  them out of model and consumer execution workspaces.
- Report sanitized GitHub API method, endpoint, HTTP status and permission hints.
- Recover interrupted administrative commit, ref and PR creation using the same
  exact candidate, publication binding and recorded review consumption.

## 1.2.1

- Name the bundled marketplace `nexkit`, displayed as NexKit, and install the
  plugin as `nexkit@nexkit` in the installation guides.
- Verify native plugin installation, repeated installation and removal through
  the `nexkit` marketplace.

## 1.2.0

- Publish protected setup through exact administrator proposals, bot PRs,
  scoped verification, independent review and native administrator approval.
- Persist public partial source/drafts independently of final agent results,
  discover completed checkpoints after interruption and restore fresh sessions.
- Allow exact administrator-approved additional issue capacity while preserving
  all spent usage; inspect checkpoint retention and pending decisions with dry-run.
- Require independent review to cover the approved acceptance criterion inventory.

## 1.1.0

- Select a published toolkit release with `nexkit use --version VERSION`, including
  lower versions. Verify release assets and selected capabilities before updating
  accepted workflow pins, hashes and project skills; preview with `--dry-run`.
- Link issues to the current Actions attempt and result artifacts through one
  maintained progress comment. Stream filtered public agent activity and preserve
  bounded diagnostic reports, including failures and timeouts.
- Preserve complete standalone task output within the validated result bound.
- Run PR checks once per PR event; pushes to the default branch still run checks.
- Keep project and release candidate formats at `schema: 1`; derive CI archive
  paths from the package version.

## 1.0.0

- A shared controller runtime action verifies the selected Python through sudo
  with no loader variables and registers its shared libraries from the actual
  installation prefix when required, including relocated runner tool caches.
- Managed workflows keep controller data outside the runner installation;
  Codex profiles collapse redundant private paths and probe read-only roles.
- Runner shutdown forwards signals to the official listener so it can release
  its GitHub session before a restart.
- The base image supplies both `python` and `python3` for pre-login Actions probes.
- Container verification executes the pinned setup-python and workspace actions,
  with optional checks of a consumer-selected CLI on Linux and Windows/WSL.
- Guided and manual setup from each project's code, requirements and decisions.
- Named pipelines with configurable models, reasoning, usage limits, runners,
  checks and approval steps.
- Issue intake, requirement clarification and exact specification approval.
- Implementation, real project checks, independent review and guarded merging.
- Standalone analysis tasks, project commands and recorded native jobs.
- Stage approvals and continuation without replaying completed work.
- Issue completion after merge or release, with opt-outs and recoverable closure.
- Separate release decisions, selected-source builds and publication recovery.
- Shared Linux container execution on Ubuntu and Windows with WSL 2, with
  isolated tools and host supervision.
- Reproducible installation archives with source manifests and SHA-256 checksums.
- Stable project and release candidate formats using `schema: 1`.
- Consistent plugin, CLI, package and runner image version `1.0.0`.
- Agent adapters with capability checks, optional installation pins and diagnostic
  CLI versions. Shared contract tests and one native CLI reference per integration.
- Ubuntu host capability checks and immutable source, image and evidence identities.

See [versions](docs/versions.md) for the stable baseline and immutable pins,
and [current support](docs/support.md) for implemented integrations.

The public-repository subscription runner remains an experimental integration.
See its [support boundary](docs/self-hosted.md#support-boundary) and the
[verification guide](docs/acceptance.md) for the required checks.
