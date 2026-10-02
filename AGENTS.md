# Working on NexKit

NexKit is a Python toolkit that coordinates requirements, coding agents,
verification, approvals and publication. A **consumer** is a repository using
NexKit: its `.nexkit/project.json` records accepted settings, and its GitHub
Actions workflows schedule the work. This repository contains the toolkit's
CLI, control code, reusable workflows, agent skills and runner implementation.

Start with the [project overview](README.md) and [architecture](docs/architecture.md).
Use [current support](docs/support.md) for implemented integrations and platform
boundaries, and the [documentation index](docs/README.md) to find additional guides.

## Where to work

| Area | Start here |
|---|---|
| CLI and project setup | [cli.py](nexkit/cli.py), [project.py](nexkit/project.py); [configuration reference](docs/configuration.md) |
| Pipeline configuration and routing | [pipelines.py](nexkit/pipelines.py); [workflow composition](docs/workflow-composition.md) |
| Requirements and decisions | [intake.py](nexkit/intake.py), [clarify.py](nexkit/clarify.py), [policy.py](nexkit/policy.py), [approvals.py](nexkit/approvals.py); [stage approvals](docs/stage-approvals.md) |
| Delivery and standalone tasks | [delivery.py](nexkit/delivery.py), [tasks.py](nexkit/tasks.py), [invocations.py](nexkit/invocations.py), [steps.py](nexkit/steps.py); [agent calls](docs/agent-invocations.md), [project steps](docs/project-steps.md) |
| Actions integration | [ci.py](nexkit/ci.py), [reusable workflows](.github/workflows/), [composite actions](actions/), [workflow examples](docs/examples/) |
| Agent integration and result contracts | [adapters](nexkit/adapters/), [agent_session.py](nexkit/agent_session.py), [plugin skills](plugins/nexkit/skills/), [result schemas](schemas/); [adapter contract](docs/agent-adapters.md), [agent call reference](docs/agent-invocations.md) |
| Workspaces and runner isolation | [agent_workspace.py](nexkit/agent_workspace.py), [command_workspace.py](nexkit/command_workspace.py), [workspace.py](nexkit/workspace.py), [runner](runner/), [runner_host.py](nexkit/runner_host.py); [runner guide](docs/self-hosted.md), [Windows host guide](docs/windows-runner.md) |
| Checks and regression coverage | [checks.py](nexkit/checks.py), [tests](tests/), [shared test fixtures](tests/support.py); [verification guide](docs/acceptance.md) |
| Releases and installation archives | [release.py](nexkit/release.py), [build.py](scripts/build.py), [verify_package.py](scripts/verify_package.py); [versions and pins](docs/versions.md) |

Trace a change through its caller, validation and result consumer before editing.
When a CLI option, configuration field, workflow input or agent result changes,
update the affected code, schemas, skills, examples and documentation together.
Use the corresponding `tests/test_*.py` modules and shared fixtures to check the
observable behavior.

## Design rules to preserve

- **The consumer owns workflow composition.** Native Actions YAML defines
  triggers, job order, dependencies and continuation. NexKit validates inputs,
  accounts for usage and records results. Keep models, limits, checks and
  project decisions in accepted configuration; avoid hardcoded branches for
  individual consumers or frameworks.
- **Control code enforces authority.** Skills guide agents, and agent reports
  are untrusted inputs. Permission, approval, budget and publication decisions
  must remain enforced by Python control code. Keep implementation and review
  in separate sessions; read-only task results cannot authorize a merge.
- **Agent integration follows a contract.** Keep provider commands, settings,
  authentication and capability checks in its adapter. Core decisions use roles,
  budgets and validated results. CLI versions are optional installation pins or
  diagnostic metadata; never use a release label to decide compatibility. Extend
  the shared adapter tests when adding an integration; do not add version matrices.
- **Evidence belongs to an exact candidate.** Preserve bindings to the approved
  specification, configuration, accepted control files, kit pin and code
  revision. Revalidate before publishing, merging or resuming work; changed
  inputs invalidate earlier evidence. Ordinary source delivery cannot edit
  workflows, local actions or accepted controls to authorize itself.
- **Execution and credentials stay separated.** Consumer commands and checks
  run without GitHub write tokens or model credentials. Publishing jobs process
  validated results without executing consumer code. Preserve trusted workspace
  restoration, role permissions and credential isolation when changing runners.
- **Retries preserve ownership and usage.** Duplicate events, interruption,
  cancellation and continuation must retain the recorded work identity and
  budgets. Respect `retry: never`; do not reset state to make a run pass.
- **Checks and approvals must be real.** Missing, empty, failed or skipped test
  results cannot satisfy required checks. Human approvals come from the actual
  authorized collaborator and apply to the current subject. Preparing a release
  candidate does not authorize publication; releases need their separate
  exact-candidate approval.

## Local verification

Use Python 3.11+ in a virtual environment. Consumer integration tests also need
Node.js with `node:test` support. From the repository root:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest tests.test_policy -v  # Example: choose the affected module.
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -v
```

Run focused tests while working, then the shared suite and lint/format checks
for code changes. For a documentation-only edit, verify links, command accuracy
and `git diff --check`.

- Workflow changes also need `actionlint`; use the pinned version and options
  in the [CI workflow](.github/workflows/ci.yml), including the workflow examples.
- Packaging changes need the [archive build](scripts/build.py) and
  [package verification](scripts/verify_package.py). See the CI workflow for
  native plugin installation and Linux/Windows package comparison.
- Runner, workspace and live integration changes need the applicable native
  or live checks in the [acceptance procedure](docs/live-acceptance.md).
  A passing shared suite does not cover skipped environment-specific checks.

Preserve the [acceptance criteria](docs/live-acceptance.md#acceptance-criteria).
Report static checks, simulated boundaries, actual tool execution, live local
agents and live GitHub integration separately, as described in the
[verification guide](docs/acceptance.md).

## Documentation and evidence

Keep durable behavior, prerequisites and repeatable commands in the existing
guides, and use relative Markdown links when referring to repository files.
Keep toolkit docs and skills reusable; consumer business context belongs in
the consumer's knowledge, and provider-specific details belong in the relevant
integration guide.

Keep verification results, logs, environment snapshots and development journals
outside the source tree or attached to the corresponding CI run or release.
Record measured totals with that run. Use descriptive document names without
date suffixes; timestamps belong inside run records.
