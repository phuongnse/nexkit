# Working on NexKit

NexKit turns GitHub issues into reviewed, tested pull requests with Claude Code. It has
three parts: the reusable workflow `.github/workflows/pipeline.yml`, the Python package
`nexkit/` (standard library only, Python 3.12 or newer) that each job calls through `bin/nexkit`,
and the Claude Code plugin (`.claude-plugin/`, `skills/`) for setting up repositories.
Read [How it works](docs/how-it-works.md) before changing behaviour.

## Layout

| Module | Job / purpose |
|---|---|
| `route.py` | `route`: event → action, authorization |
| `context.py` | collects issue, plan and feedback text before an agent runs |
| `agent.py`, `prompts/*.md` | `agent` and `review`: build prompt, run `claude -p`, collect patch |
| `publish.py` | `publish`: plan comment, commit, push, pull request |
| `checks.py` | `verify` and agent setup: run configured commands |
| `report.py`, `state.py` | `report`: statuses, review, status comment, next round |
| `progress.py` | `route` and `report`: reactions, run links, running rounds |
| `config.py` | `.nexkit/config.json` schema and defaults |
| `scaffold.py`, `templates/` | `nexkit init` and `nexkit doctor` |
| `github.py`, `gitutil.py` | REST and git helpers |

## Rules to keep

- Keep the job separation in `pipeline.yml`: jobs that run Claude or repository code have
  read-only tokens; jobs with write tokens never run repository code and never receive the
  Claude credential. `tests/test_cli.py` checks this; extend it if you add jobs.
- Agent output is untrusted. Python validates it before anything is published.
- State lives only on GitHub (comments, PRs, statuses). Do not add a state store.
- Agent errors and `blocked` results never trigger automatic retries; only check failures
  and review findings do, up to `max_auto_fixes`.
- Prompts judge code behaviour. Never make an agent responsible for something it cannot
  change (PR text, approvals, workflows).
- When a config key, command or prompt contract changes, update the code, tests, docs and
  skills together.
- Versions live in `nexkit/__init__.py`. The pipeline installs exactly `RUNNER`, Python
  `PYTHON` and `CLAUDE_CODE`; the local CLI accepts Python `PYTHON` or newer. Test only
  those versions: no matrices. The Claude Code pin is read from `nexkit/__init__.py`
  everywhere; never repeat it. `update-claude-code.yml` proposes new stable versions, and
  the CI `live` job (`scripts/smoke.py`) must pass before one is merged.

## Verify

```sh
python3 -m unittest discover -s tests -t .
ruff check . && ruff format --check .
actionlint .github/workflows/*.yml
claude plugin validate --strict .
python3 scripts/smoke.py   # real Claude Code; required when changing prompts, agent.py or the pin
```
