# Working on NexKit

NexKit turns GitHub issues into reviewed, tested pull requests with Claude Code. It is the
reusable workflow `.github/workflows/pipeline.yml`, the package `nexkit/` that each job runs
through `bin/nexkit` (standard library only, Python 3.12+), and the Claude Code plugin
(`.claude-plugin/`, `skills/`) that sets up repositories. Read
[How it works](docs/how-it-works.md) before changing behaviour.

## Layout

| Module | Purpose |
|---|---|
| `__init__.py` | Version and pins: runner, Python, each runtime |
| `cli.py` | Every command, local and pipeline |
| `route.py` | `route`: event → action, authorization, `verified_sha` |
| `context.py`, `evidence.py`, `agent.py`, `prompts/` | `agent` and `review`: context, CI runs' evidence, prompt, agent run, patch |
| `runtimes/` | Everything specific to one CLI or model provider |
| `schema.py`, `redact.py`, `runlog.py` | Result validation, secret removal, log and run summary |
| `publish.py` | `publish`: plan comment, commit, push, pull request |
| `checks.py`, `cache.py` | Setup and checks with `NEXKIT_BASE_SHA`/`NEXKIT_VERIFIED_SHA`; cache restore |
| `report.py`, `state.py`, `progress.py` | Statuses, review, run and state comments and their markers |
| `maintain.py`, `usage.py`, `autopilot.py` | Work after other events; resuming runs paused at a usage limit; autopilot sweeps |
| `config.py`, `scaffold.py`, `templates/` | Config schema and defaults; `nexkit init` and `doctor` |
| `status.py`, `github.py`, `gitutil.py` | `nexkit status`; REST and git helpers |
| `scripts/` | Live smoke test, pin updates, release notes |

## Rules

- Jobs that run an agent or repository code have read-only tokens; jobs with write tokens
  never run repository code or receive an agent credential. `tests/test_cli.py` checks
  this; extend it for new jobs.
- Agent output is untrusted: validate it before publishing, pass everything printed or
  stored from a session through `redact.py`, print tool output with workflow commands
  stopped. Markers count only in Actions bot comments, and a run marker only where it ends
  the comment.
- State lives only on GitHub (comments, pull requests, statuses).
- Agent errors and `blocked` results never retry automatically; only check failures and
  review findings do, up to `fix.max_auto_rounds`. The one exception is a run that stopped
  on a transient error before it published anything (the service, the network, a lost
  runner, as `runtimes/` classifies it): it runs once more, and a second such stop waits
  for a person. Autopilot may answer a `blocked` result
  whose questions all have a recommended option (`autopilot.answer: "recommended"`), once
  in a row: when the round its answers started asks again and no person commented since,
  a person decides. A recommended option is one NexKit can carry out without a person;
  one that only closes the issue, after a round found no change is needed, closes it
  without another round. Autopilot asks for one split plan after an implement or fix agent ran out of time; it
  never repeats a run unchanged.
- Every issue in autopilot's scope is either carried on by a run that exists, started only
  when something changed since the same command last started (a commit, a person's
  comment or command, a new plan), or waits for a person: listed under *Needs a person*
  with its reason and mentioned to `notify` once. Each automatic action checks what it
  needs before it spends agent time, and its budget counts only rounds that ran their
  agent to its end or published. `tests/test_scenarios.py` plays this over many rounds;
  a new automatic rule comes with a scenario there.
- Prompts judge code behaviour, never things an agent cannot change (PR text, approvals,
  workflows).
- A config key, command or prompt contract changes together with its tests, docs and skills.
- Pins live only in `nexkit/__init__.py`. The pipeline installs exactly those versions;
  test only them, without matrices. `update-agents.yml` proposes runtime updates, and each
  needs its runtime's smoke test to pass.
- Everything specific to a CLI or provider lives in `runtimes/` behind `Runtime` and
  `Provider`; never branch on runtime names elsewhere. Settings that identify an account or
  resource (such as an endpoint) are secret fields.
- NexKit reads only secrets the configuration names (`agents`, `publish.push_token_secret`).
  `pipeline.yml` reads the names `route` lists for a job into its `SECRET_SLOTS`. Agent
  secret names may not start with `GITHUB_` or `NEXKIT_`, the push token not with `GITHUB_`.

## Releases

Feature pull requests change neither the version nor `CHANGELOG.md`. A release pull request
sets the version in `nexkit/__init__.py`, `pyproject.toml`, `.claude-plugin/plugin.json` and
the README's `pipx install` line, and adds a `## X.Y.Z` changelog entry, ending with
`### Upgrade` steps when the configuration, caller workflow or permissions must change. Its
description holds the output of `python3 scripts/release_notes.py X.Y.Z`; after the merge,
release `vX.Y.Z` (titled `NexKit X.Y.Z`) on the merge commit with those notes.

## Verify

```sh
python3 -m unittest discover -s tests -t .
ruff check . && ruff format --check .      # ruff 0.13.3
actionlint .github/workflows/*.yml
claude plugin validate --strict .
python3 scripts/smoke.py [--program PATH]  # real Claude Code at the pinned version
```

Run the smoke test when changing `prompts/`, `agent.py`, `schema.py`, `runtimes/` or a pin.
