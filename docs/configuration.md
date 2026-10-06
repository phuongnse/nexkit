# Configuration reference

NexKit reads `.nexkit/config.json` from the repository's **default branch** on every run.
Changes on a pull request branch never affect the run that is checking that branch.
Unknown keys are rejected so typos fail loudly.

The runner (`ubuntu-24.04`), Claude Code version and NexKit's Python version are set by
the NexKit release and are not configurable. See [supported versions](../README.md#supported-versions).

| Key | Default | Meaning |
|---|---|---|
| `version` | `1` | Configuration format. Must be `1`. |
| `model` | `"sonnet"` | Claude model for every stage: an alias (`sonnet`, `opus`, `haiku`) or a full model name. |
| `effort` | `null` | Reasoning effort for every stage: `low`, `medium`, `high`, `xhigh`, `max`, or `null` for the Claude Code default. |
| `setup` | `[]` | Shell commands that install dependencies. They run before the agent works and before the checks. |
| `checks` | `[]` | Commands that decide whether a change is acceptable. See below. |
| `stages` | see below | Per-stage overrides. |
| `max_auto_fixes` | `2` | Repair rounds NexKit may start by itself on one pull request (0 to 10). Rounds requested by people do not count. |
| `auto_merge` | `false` | Merge (squash) when every check passes and the AI review approves. Branch protection still applies. |
| `protected_paths` | `[".github/", ".nexkit/"]` | Path prefixes the agent may not change. Both defaults are required; you may add more. |
| `transcript` | `true` | Write Claude's transcripts (`transcript.jsonl`, `transcript.md`) into the `nexkit-agent` and `nexkit-review` artifacts. See below. |
| `log` | `{"tool_output": "truncated"}` | How the agent and review jobs print tool calls. See below. |

## Checks

```json
"checks": [
  {"name": "test", "run": "pytest -q", "timeout_minutes": 20},
  {"name": "lint", "run": "ruff check ."}
]
```

- `name`: short lowercase identifier, shown in reports.
- `run`: a Bash command run from the repository root with `pipefail`.
- `timeout_minutes`: default 15.

Checks run in a separate job that has **no secrets and a read-only token**, on the exact
commit NexKit published. A setup failure counts as a failed check. With no checks
configured, NexKit can still run, but nothing verifies the change except the AI review.

## Stages

```json
"stages": {
  "plan":      {"timeout_minutes": 15},
  "implement": {"model": "opus", "timeout_minutes": 45, "max_budget_usd": 10},
  "review":    {"model": "opus", "effort": "high", "timeout_minutes": 20}
}
```

Fix rounds use the `implement` settings. Each stage accepts:

| Key | Meaning |
|---|---|
| `model` | Overrides the top-level `model`. |
| `effort` | Overrides the top-level `effort`. |
| `timeout_minutes` | Stop Claude after this long (plan 15, implement 45, review 20 by default). |
| `max_budget_usd` | Passed to Claude Code as `--max-budget-usd`; `null` means no limit. |

## Logs and transcripts

```json
"transcript": false,
"log": {"tool_output": "none"}
```

- `transcript`: `false` stops writing the transcripts, so the artifacts hold only the
  prompt, context, result and patch. Consider it for a public repository, where anyone
  signed in to GitHub can download artifacts for 14 days.
- `log.tool_output`: `"truncated"` (default) prints each tool call as a collapsible group
  with its input and the first and last 20 lines of its output. `"none"` prints one line
  per message and per tool call, without output.

Both are redacted either way. [How it works](how-it-works.md#logs-summary-and-artifacts)
shows what the log, run summary and artifacts contain and what is redacted.

## Secrets

| Secret | Required | Used by |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | one of these two | Agent and review jobs. Create it with `claude setup-token` (Claude Pro or Max). |
| `ANTHROPIC_API_KEY` | one of these two | Agent and review jobs. Takes precedence when both are set. |
| `NEXKIT_PUSH_TOKEN` | no | Publish job only: pushes the branch and opens the pull request, so the repository's own CI workflows also run. A fine-grained token with Contents and Pull requests write access. Add Workflows write access if fix rounds should merge base branch changes to `.github/workflows/`; the default Actions token cannot push those. |

## Workflow file

`nexkit init` writes `.github/workflows/nexkit.yml`, which calls
`phuongnse/nexkit/.github/workflows/pipeline.yml` at a fixed release tag. To upgrade,
change both the `uses:` ref and `nexkit_ref` to the new tag (or rerun
`nexkit init --force --kit-ref vX.Y.Z` and restore your config).
