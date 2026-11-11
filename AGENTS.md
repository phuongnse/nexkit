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
| `checks.py`, `cache.py` | Setup and checks with `NEXKIT_BASE_SHA`/`NEXKIT_VERIFIED_SHA`; the keys of cache restore and of the `cache/` action |
| `report.py`, `state.py`, `progress.py` | Statuses, review, run and state comments and their markers |
| `refusal.py` | Why GitHub refused a merge, and what unblocks it |
| `maintain.py`, `usage.py`, `autopilot.py` | Work after other events; resuming runs paused at a usage limit; autopilot sweeps |
| `config.py`, `globs.py`, `scaffold.py`, `app.py`, `templates/` | Config schema and defaults, path globs; `nexkit init` and `doctor`; the GitHub App |
| `pin.py` | `status` and `doctor` run the release the repository's workflow pins |
| `status.py`, `snapshot.py`, `github.py`, `gitutil.py` | `nexkit status` and the batched reads it makes; REST, GraphQL and git helpers |
| `scripts/` | Live smoke test, pin updates, release notes |

## Rules

- Jobs that run an agent or repository code have read-only tokens; jobs with write tokens
  never run repository code or receive an agent credential. `tests/test_cli.py` checks
  this; extend it for new jobs.
- NexKit writes on GitHub only as the repository's GitHub App (`github_app`). Each job that
  writes mints its own token with only what it needs; `github.token` only reads. A token
  lasts an hour, so nothing holds one through a longer wait. What the App does starts
  workflows, NexKit's own too, so each write must leave a following run nothing to do.
- Agent output is untrusted: validate it before publishing, pass everything printed or
  stored from a session through `redact.py`, print tool output with workflow commands
  stopped. Markers count only in comments of the App, never the Actions bot, which CI on an
  agent's code can write as, and a run marker only where it ends the comment.
- State lives only on GitHub (comments, pull requests, statuses).
- Agent errors and `blocked` results never retry automatically; only check failures and
  review findings do, up to `fix.max_auto_rounds`. The one exception is a run that stopped
  on a transient error before it published anything (the service, the network, a lost
  runner, as `runtimes/` classifies it, or no result that fits even when its session was
  asked for the result alone): it runs once more, and a second such stop waits
  for a person. A command that autopilot or a round started and whose run GitHub never
  started, or that ended without a report, starts again later each time
  (`autopilot.AGAIN`), since such a stop says nothing about the work; then a person
  decides. Autopilot may answer a `blocked` result
  whose questions all have a recommended option (`autopilot.answer: "recommended"`), once
  in a row: when the round its answers started asks again and no person commented since,
  a person decides. A recommended option is one NexKit can carry out without a person;
  one that only closes the issue, after a round found no change is needed, closes it
  without another round. A person's answer that picks the option that only closes the
  issue closes it as not planned, with no agent run; agents offer closing only so. Autopilot asks for one split plan after an implement or fix agent ran out of time; it
  never repeats a run unchanged.
- Every issue in autopilot's scope is either carried on by a run that exists, started only
  when something changed since the same command last started (a commit, a person's
  comment or command, a new plan), or waits for a person: listed under *Needs a person*
  with its reason and mentioned to `notify` once. Each automatic action checks what it
  needs before it spends agent time, and its budget counts only rounds that ran their
  agent to its end or published. `tests/test_scenarios.py` plays this over many rounds;
  a new automatic rule comes with a scenario there.
- Prompts judge code behaviour, never things an agent cannot change (PR text, approvals,
  workflows). A person's pull request has no such limit on workflows, since a person wrote
  them; `/nexkit review` there never pushes, starts a round or merges.
- A config key, command or prompt contract changes together with its tests, docs and skills.
- Pins live only in `nexkit/__init__.py`. The pipeline installs exactly those versions;
  test only them, without matrices. `update-agents.yml` proposes runtime updates, and each
  needs its runtime's smoke test to pass.
- Everything specific to a CLI or provider lives in `runtimes/` behind `Runtime` and
  `Provider`; never branch on runtime names elsewhere. Settings that identify an account or
  resource (such as an endpoint) are secret fields.
- NexKit reads only secrets the configuration names (`agents`, `github_app`).
  `pipeline.yml` reads the names `route` lists for a job into its `SECRET_SLOTS`, in the
  call of `pipeline-agent.yml` and `pipeline-review.yml`: a job that runs an agent never
  indexes `secrets` itself, since that puts every secret on its runner. Agent
  secret names may not start with `GITHUB_` or `NEXKIT_`, the App's not with `GITHUB_`.

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
