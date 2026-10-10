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
| `auto_merge` | `false` | Merge (squash) when every check passes and the AI review approves. Branch protection still applies. See below. |
| `after_merge_workflows` | `[]` | Workflow files, such as `["ci.yml"]`, that NexKit starts on the base branch after it merges. See below. |
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

## Automatic merge

```json
"auto_merge": true,
"after_merge_workflows": ["ci.yml"]
```

With `auto_merge: false` (the default) a person decides every merge. With `true`, the
`report` job squash-merges a NexKit pull request as soon as every check passes and the AI
review approves. It merges with the default Actions token; no extra secret is needed.

Branch protection and rulesets still apply, and the Actions token cannot bypass them. When
a rule is not met, GitHub refuses the merge, the round's comment says why, and the pull
request waits for a person. Before merging, NexKit sets the `nexkit/checks` and
`nexkit/review` statuses, so you can require those two. Rules that block every automatic
merge:

- a required approval from a person;
- a required check from your own CI, unless `NEXKIT_PUSH_TOKEN` is set, because without it
  your CI does not run on NexKit pull requests (see
  [Troubleshooting](troubleshooting.md));
- a required merge queue, or squash merging turned off for the repository.

`after_merge_workflows` lists workflow files in `.github/workflows/`. GitHub starts no
workflow for a push made with the Actions token, so the merge by NexKit does not run your
`push` CI on the base branch. Two pull requests that pass on their own can still break the
base branch together. After each merge, NexKit starts every listed workflow on the base
branch with `workflow_dispatch`:

- Each workflow needs a `workflow_dispatch` trigger without required inputs, for example
  `on: {push: {branches: [main]}, pull_request: {}, workflow_dispatch: {}}`.
  `nexkit doctor` warns when a listed file is missing or has no such trigger.
- The run uses the base branch's newest commit when it starts, normally the merge commit.
  Its event is `workflow_dispatch`, so steps limited to `push` events are skipped.
- The round's comment links each workflow it started, or says why a start failed. A
  failed start does not undo the merge.
- Nothing is started when `auto_merge` is `false` or the merge was refused.

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
