# Working on NexKit

NexKit turns GitHub issues into reviewed, tested pull requests with coding agents (currently
Claude Code, and Codex on Azure AI Foundry). It has three parts: the reusable workflow
`.github/workflows/pipeline.yml`, the Python package `nexkit/` (standard library only,
Python 3.12 or newer) that each job calls through `bin/nexkit`, and the Claude Code plugin
(`.claude-plugin/`, `skills/`) for setting up repositories.
Read [How it works](docs/how-it-works.md) before changing behaviour.

## Layout

| Module | Job / purpose |
|---|---|
| `__init__.py` | NexKit's version and the pins: runner, Python, each runtime |
| `cli.py` | entry point for every command, local and pipeline |
| `route.py` | `route`: event → action, authorization |
| `context.py` | collects issue, plan and feedback text before an agent runs |
| `agent.py`, `prompts/*.md` | `agent` and `review`: build prompt, run the stage's agent, collect patch |
| `runtimes/` | agent runtimes (CLIs) and model providers; everything specific to one CLI |
| `schema.py` | checks an agent's structured result against its stage schema |
| `runlog.py` | `agent` and `review`: Actions log, transcripts, run summary |
| `redact.py` | removes secrets from everything printed or stored from an agent session |
| `publish.py` | `publish`: plan comment, commit, push, pull request |
| `checks.py` | `verify` and agent setup: run configured commands |
| `cache.py`, `.github/actions/restore-caches/` | `agent` and `verify`: restore caches before setup, never save |
| `report.py`, `state.py` | `report`: statuses, review, state comment, next round |
| `progress.py` | `route` and `report`: reactions, one comment per run with its link |
| `maintain.py` | `maintain`: work that follows other events, without an agent |
| `usage.py` | when to resume a run paused at a usage limit; each runtime recognises its own |
| `config.py` | `.nexkit/config.json` schema and defaults |
| `scaffold.py`, `templates/` | `nexkit init` and `nexkit doctor` |
| `status.py` | `nexkit status`: open NexKit work from the comment markers |
| `github.py`, `gitutil.py` | REST and git helpers |
| `scripts/smoke.py`, `scripts/agent_version.py` | live smoke test per runtime; reading and updating the pins |

## Rules to keep

- Keep the job separation in `pipeline.yml`: jobs that run an agent or repository code have
  read-only tokens; jobs with write tokens never run repository code and never receive an
  agent credential. `tests/test_cli.py` checks this; extend it if you add jobs.
- Agent output is untrusted. Python validates it before anything is published. Pass
  anything printed or stored from an agent session through `redact.py`, and print tool
  output only with workflow commands stopped.
- State lives only on GitHub (comments, PRs, statuses). Do not add a state store.
- Agent errors and `blocked` results never trigger automatic retries; only check failures
  and review findings do, up to `fix.max_auto_rounds`.
- Prompts judge code behaviour. Never make an agent responsible for something it cannot
  change (PR text, approvals, workflows).
- When a config key, command or prompt contract changes, update the code, tests, docs and
  skills together.
- Versions live in `nexkit/__init__.py`. The pipeline installs exactly `RUNNER`, Python
  `PYTHON` and the pinned version of each runtime it uses (such as `CLAUDE_CODE`, `CODEX`); the
  local CLI accepts Python `PYTHON` or newer. Test only those versions: no matrices. Each
  runtime's pin is read from `nexkit/__init__.py` everywhere; never repeat it.
  `update-agents.yml` proposes new stable versions, one pull request per runtime, and that
  runtime's CI smoke test (one per runtime, such as `smoke-claude-code`; `scripts/smoke.py`) must pass
  before one is merged. CI runs a runtime's smoke test on pull requests that change
  `agent.py`, `schema.py`, the shared code in `runtimes/`, that runtime's module or pin,
  `prompts/` or the smoke test.
- Everything specific to one CLI or model provider lives in `runtimes/`, behind `Runtime`
  and `Provider`. Add a runtime or provider there and in the registry; never branch on
  runtime names elsewhere in `nexkit/`. A provider setting that identifies a person's own
  account or resource, such as an endpoint, is a secret field, never a plain setting:
  configurations and logs of public repositories are public. Agent secrets have the names
  the configuration gives them; the caller passes `secrets: inherit`, and `pipeline.yml`
  reads only the names `route` lists for a job into its slots (`SECRET_SLOTS`). Agent
  secret names starting with `GITHUB_` or `NEXKIT_` are reserved; the push token may not
  start with `GITHUB_`. Every secret NexKit reads is named in
  the configuration: each agent's in `agents`, the push token in
  `publish.push_token_secret`. Never read a secret the configuration does not name.

## Verify

```sh
python3 -m unittest discover -s tests -t .
ruff check . && ruff format --check .
actionlint .github/workflows/*.yml
claude plugin validate --strict .
python3 scripts/smoke.py   # real Claude Code; required when changing prompts, agent.py, schema.py, runtimes/ or a pin
python3 scripts/smoke.py --runtime codex   # real Codex on Azure: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT
```
