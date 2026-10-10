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
| `profiles` | `{}` | Stage settings for kinds of issues, chosen for each issue. See below. |
| `default_profile` | `null` | The profile to use when no plan chose one. Required with `profiles`. |
| `max_auto_fixes` | `2` | Repair rounds NexKit may start by itself on one pull request (0 to 10). Rounds requested by people do not count. |
| `auto_merge` | `false` | Merge (squash) when every check passes and the AI review approves. Branch protection still applies. See below. |
| `after_merge_workflows` | `[]` | Workflow files, such as `["ci.yml"]`, that NexKit starts on the base branch after it merges. See below. |
| `close_parent_issues` | `false` | Close a parent issue when its last open sub-issue closes and at least one sub-issue was completed. See below. |
| `auto_resolve_conflicts` | `false` | Start a fix round by itself when a NexKit pull request conflicts with its base branch. See below. |
| `resume_after_usage_limit` | `true` | When Claude stops at the account's usage limit, run the same command again after the limit resets. See below. |
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

### Skipping work that a change cannot affect

Setup and checks get `NEXKIT_BASE_SHA`: the merge base of the commit being checked and
the base branch. The checkout has that commit, in public and private repositories alike.
List the paths the pull request changed with both of these commands:

```sh
{ git diff --name-only "$NEXKIT_BASE_SHA"; git ls-files --others --exclude-standard; }
```

The first lists changed tracked files, committed or not. The second lists new files that
are not committed yet: when Claude runs the checks while it works, the files it created are
still untracked, and without that line a check would skip work it should run. Changes that
the base branch made after the merge base are not listed. In a fix round that merges the
base branch, the variable is the merged base commit.

When NexKit cannot work out the base, it leaves `NEXKIT_BASE_SHA` unset and says so in
the job log. Outside NexKit it is always unset, so the rule for a check is:

- Skip work only when `NEXKIT_BASE_SHA` is set and none of the paths that work covers
  changed. Otherwise run it in full.
- Run every check in full on the base branch, for example in your own CI after a merge.

```sh
# Run the dev tooling smoke tests only when the change can affect them.
changed() {
  [ -n "${NEXKIT_BASE_SHA:-}" ] || return 0  # unknown base: run everything
  files=$(git diff --name-only "$NEXKIT_BASE_SHA" &&
          git ls-files --others --exclude-standard) || return 0
  grep -qE "$1" <<< "$files"
}
if changed '^(docker/|compose\.yaml$|scripts/dev)'; then
  ./scripts/smoke-dev.sh
else
  echo "Dev tooling unchanged since $NEXKIT_BASE_SHA: smoke tests skipped."
fi
```

## Automatic merge

```json
"auto_merge": true,
"after_merge_workflows": ["ci.yml"]
```

With `auto_merge: false` (the default) a person decides every merge. With `true`, the
`report` job squash-merges a NexKit pull request as soon as every check passes and the AI
review approves. It merges with the default Actions token; no extra secret is needed.

After the merge, NexKit closes the issue the pull request implements as completed and
comments a link to the pull request and the merge commit on it. GitHub does this itself
when a person merges, but not after a merge with the Actions token. An issue that is
already closed stays as it is. If closing fails, the round's comment says so and links the
issue; the merge stays.

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

## Parent issues

```json
"close_parent_issues": true
```

GitHub does not close a parent issue when all its sub-issues are closed, because a parent
can mean more than the sum of its parts, such as an epic with work still to plan. In
repositories where a parent is only its sub-issues, for example when a too-large issue is
split, turn this on. When the last open sub-issue closes, NexKit closes the parent as
completed if at least one sub-issue was completed, and comments a list of the sub-issues
and how each closed. When all of them were closed as not planned, the parent stays open
with a note for a person. NexKit then checks the parent's own parent. It works the same
whether a person or NexKit merged the work. See
[How it works](how-it-works.md#work-without-a-command).

The workflow must listen to `issues: closed`, as `nexkit init` writes it; `nexkit doctor`
warns when it does not. With the setting off, a closed issue starts a short run that ends
at once.

## Conflicts with the base branch

```json
"auto_resolve_conflicts": true
```

With `false` (the default), a person comments `/nexkit fix` on a pull request that
conflicts with its base branch. With `true`, NexKit keeps its open pull requests
mergeable: after the base branch moves, each one that now conflicts gets an automatic fix
round that merges the base branch and keeps both sides. This matters most with
`auto_merge` and several issues at once, where every merge can make the other pull
requests conflict.

- NexKit's own merges, pushes to the default branch (through the `push` trigger that
  `nexkit init` writes) and the hourly schedule start the check. `nexkit doctor` warns when
  the `push` trigger is missing; the schedule still notices within an hour.
- When an automatic merge is refused because of a conflict, the same round starts.
- A conflict that needs a choice ends the round as `blocked` with its questions; a person
  answers with `/nexkit fix <decisions>`.
- Each pull request gets at most 3 of these rounds, apart from `max_auto_fixes`, and one
  per base commit.

See [How it works](how-it-works.md#work-without-a-command).

## Usage limits

```json
"resume_after_usage_limit": false
```

When Claude stops because the account hit its usage limit, NexKit pauses the run, shows
the reset time in its comment and keeps `nexkit/checks` and `nexkit/review` pending.
With `true` (the default), it runs the same command again after the reset, once, with
the same note; when the reset time is unknown, after one hour. With `false`, the comment
still names the limit and the reset time, and a person comments the command again. See
[How it works](how-it-works.md#usage-limits).

The resume needs the hourly `schedule` trigger that `nexkit init` writes;
`nexkit doctor` warns when it is missing. A schedule run with nothing to resume runs
only `route` and `maintain`, which take well under a minute and run no agent. To check
less often, change the `cron` line, for example to `"23 */3 * * *"`.

## Stages

```json
"stages": {
  "plan":      {"timeout_minutes": 15},
  "implement": {"model": "opus", "timeout_minutes": 45, "max_budget_usd": 10},
  "review":    {"model": "opus", "effort": "high", "timeout_minutes": 20}
}
```

Fix rounds use the `implement` settings. `triage` is a fourth stage, allowed only with
`profiles` (see below). Each stage accepts:

| Key | Meaning |
|---|---|
| `model` | Overrides the top-level `model`. |
| `effort` | Overrides the top-level `effort`. |
| `timeout_minutes` | Stop Claude after this long (plan 15, implement 45, review 20 and triage 5 by default; triage at most 30). |
| `max_budget_usd` | Passed to Claude Code as `--max-budget-usd`; `null` means no limit. |

## Profiles

Profiles let a repository use cheaper models on easy issues and stronger models on hard
ones, without choosing by hand on every issue.

```json
"stages": {
  "triage": {"model": "claude-fable-5-1"}
},
"default_profile": "standard",
"profiles": {
  "standard": {
    "when": "Clear spec, one layer, follows an existing pattern.",
    "stages": {
      "plan":      {"model": "claude-opus-5-5"},
      "implement": {"model": "claude-sonnet-5-5"},
      "review":    {"model": "claude-opus-5-5"}
    }
  },
  "hard": {
    "when": "Touches security or access control, storage schema or execution.",
    "stages": {
      "plan":      {"model": "claude-fable-5-1"},
      "implement": {"model": "claude-opus-5-5", "timeout_minutes": 60},
      "review":    {"model": "claude-fable-5-1"}
    }
  }
}
```

- A profile name is a short lowercase identifier. Each profile has a `when` text, which
  triage reads to decide which issues belong in it, and optional `stages` with the same
  keys as the top-level `stages`, for `plan`, `implement` and `review`.
- Settings are layered: the top-level `model` and `effort`, then `stages.<stage>`, then
  `profiles.<profile>.stages.<stage>`. What a profile leaves out comes from `stages`.
- `stages.triage` sets the model, effort, timeout and budget of the triage call. It takes
  the top-level `model` and `effort` unless it sets its own. A profile cannot set it,
  because triage runs before there is a profile.
- `default_profile` is used when no plan chose a profile: `/nexkit go` without a plan, a
  plan from before profiles were configured, and a plan whose triage failed. The cheapest
  profile is a safe choice, so a failure never costs more.
- Write `when` in terms that an issue shows: the parts of the system it touches, how
  clear the request is. Triage reads only the issue and its discussion, not the code.

[How it works](how-it-works.md#profiles) describes how triage chooses and how to change a
plan's profile.

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
`phuongnse/nexkit/.github/workflows/pipeline.yml` at a fixed release tag. It starts NexKit
for `/nexkit` comments, *Request changes* reviews on NexKit pull requests, dispatches,
closed issues (for `close_parent_issues`), pushes to the default branch (for
`auto_resolve_conflicts`) and an hourly schedule (for `resume_after_usage_limit` and
`auto_resolve_conflicts`). With those settings off, these events start short runs that
end at once; remove the triggers you do not use if you prefer. To upgrade,
change both the `uses:` ref and `nexkit_ref` to the new tag (or rerun
`nexkit init --force --kit-ref vX.Y.Z` and restore your config).
