# Working on NexKit

NexKit turns GitHub issues into reviewed, tested pull requests with Claude Code. It is the reusable
workflow `.github/workflows/pipeline.yml`, the package `nexkit/` that each job runs through
`bin/nexkit` (standard library only, [D8](docs/decisions/d8-standard-library-only.md), Python
3.12+), and the plugin (`.claude-plugin/`, `skills/`) that sets up repositories. Read
[How it works](docs/how-it-works.md) before changing behaviour,
[Architecture](docs/architecture.md) for how the parts are built,
[Delivery](docs/delivery.md) before taking an issue, and [Testing](docs/testing.md) for the
checks.

## Layout

The package `nexkit/`, the reusable workflows and their jobs, the tokens and secrets that each
job holds, and the plugin are described in [Architecture](docs/architecture.md). Read
[the package](docs/architecture.md#the-package) to find a module, and
[the workflows and their jobs](docs/architecture.md#the-workflows-and-their-jobs) before you change a job.

## Rules

- Jobs that run an agent or repository code have read-only tokens and no App key. Jobs with write
  tokens never run repository code or get an agent credential. `tests/test_cli.py` checks every
  workflow; extend it for new jobs. See [Tokens and secrets](docs/architecture.md#tokens-and-secrets)
  and [Repository code and CI](docs/security.md#repository-code-and-ci) and
  [D3](docs/decisions/d3-agent-jobs-hold-no-write-token.md).
- NexKit's pipeline writes on GitHub only as the App. Each writing job mints its own token. Each
  write must leave the run it starts nothing to do. The exception is the jobs that maintain
  NexKit's own repository: they run no repository code, hold no secret and write with
  `github.token`. See [Releases](docs/delivery.md#releases) and
  [D13](docs/decisions/d13-the-pipeline-writes-as-the-app-and-maintenance-jobs-with-the-actions-token.md).
- Agent output is untrusted: validate it before publishing, redact what a session prints or stores
  with `redact.py`, and print tool output with workflow commands stopped. Markers count only in the
  App's comments that no one else changed, never the Actions bot, and only at the position NexKit
  writes them. See [Agent output](docs/security.md#agent-output) and
  [Comments and markers](docs/security.md#comments-and-markers) and
  [D4](docs/decisions/d4-outside-text-is-untrusted.md).
- State lives only on GitHub: comments, pull requests, statuses and the Actions artifacts of
  NexKit's own runs. See [State](docs/how-it-works.md#state) and
  [D1](docs/decisions/d1-state-lives-only-on-github.md).
- Agent errors and `blocked` results never retry automatically. Only check failures and review
  findings do, up to `fix.max_auto_rounds`; a transient error before publishing runs again after its
  waits. See [Failure handling](docs/failure-handling.md#failure-handling) and
  [D6](docs/decisions/d6-agent-errors-never-retry.md).
- Every issue in autopilot's scope is carried on by a run that exists, or waits for a person. A
  command starts again only after a change, or when GitHub never ran it. A new automatic rule needs
  a scenario in `tests/test_scenarios.py`. See [Autopilot](docs/work-without-a-command.md#autopilot).
- Automatic actions check their needs before they spend agent time, and budgets count only rounds
  that ran to their end or published. See [Autopilot](docs/work-without-a-command.md#autopilot).
- Prompts judge code behaviour, never what an agent cannot change, such as PR text or workflows. A
  person's pull request may change workflows, but `/nexkit review` there never pushes, starts a
  round or merges. See [Reviewing your own pull
  requests](docs/configuration.md#reviewing-your-own-pull-requests) and
  [D5](docs/decisions/d5-agents-cannot-change-the-pipeline.md).
- A config key, command or prompt contract changes together with its tests, docs and skills. See
  [How the docs stay correct](docs/writing-docs.md#how-the-docs-stay-correct).
- `tests/prompts/` is NexKit 6's record of each stage's prompt. Never write it again with
  `NEXKIT_WRITE_PROMPTS` from today's code. A prompt change edits its golden by hand and says why.
  See [Which check covers what](docs/testing.md#which-check-covers-what).
- `nexkit/__init__.py` holds every pin: the agent runtimes, the image's base, the runner and Python.
  Workflows repeat only the runner and Python, as copies a test keeps equal to it. The pipeline
  installs exactly those, and tests use no matrix. `update-agents.yml` and `update-image.yml` each
  propose an update that needs its own check. See [The smoke test](docs/testing.md#the-smoke-test) and
  [D14](docs/decisions/d14-every-pin-has-one-source.md).
- Everything specific to a CLI or provider lives in `runtimes/` behind `Runtime` and `Provider`; no
  other code branches on runtime names. Settings that identify an account or resource are secret
  fields. See [Agents](docs/agents-and-profiles.md#agents) and
  [D7](docs/decisions/d7-one-tested-runtime-claude-code.md).
- NexKit reads only the secrets the configuration names, and a job that runs an agent never indexes
  `secrets` itself, since that puts every secret on its runner. Agent secret names may not start
  with `GITHUB_` or `NEXKIT_`. See [Secrets](docs/security.md#secrets) and
  [D3](docs/decisions/d3-agent-jobs-hold-no-write-token.md).
- A session outside NexKit takes only `hand-made` issues, and never merges, pushes a tag or
  releases. See [NexKit on this repository](docs/delivery.md#nexkit-on-this-repository) and
  [D11](docs/decisions/d11-nexkit-works-its-own-issues-on-a-release.md).
- A change that moves a decision of NexKit 6 into the engine keeps GitHub's behaviour, as [Moving
  decisions into the engine](docs/testing.md#moving-decisions-into-the-engine) says, and
  [D10](docs/decisions/d10-an-engine-decides-hosts-carry-out.md) gives the reason.

## Verify

```sh
python3 -m unittest discover -s tests -t .
scripts/lint.sh                            # ruff and actionlint at their pins
tests/differential/check.sh                # the engine against NexKit 6, as CI runs it
claude plugin validate --strict .
python3 scripts/smoke.py [--program PATH]  # real Claude Code: docs/testing.md#the-smoke-test says when
```
