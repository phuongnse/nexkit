# Configuration reference

NexKit reads `.nexkit/config.json` from the repository's **default branch** on every run.
Changes on a pull request branch never affect the run that is checking that branch.
Unknown keys are rejected so typos fail loudly.

The runner, the version of each agent runtime and NexKit's Python version are set by the
NexKit release and are not configurable. See
[supported versions](../README.md#supported-versions).

A complete configuration with every key at its default, except `agents`, `setup`, `checks` and
`github_app`, which `nexkit init` writes:

```json
{
  "version": 1,
  "agents": {
    "claude": {"runtime": "claude-code", "provider": "anthropic", "oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN"}
  },
  "agent": "claude",
  "model": "sonnet",
  "effort": null,
  "setup": ["npm ci"],
  "checks": [{"name": "test", "run": "npm test"}],
  "parallel_checks": false,
  "cache": [],
  "stages": {},
  "profiles": {},
  "default_profile": null,
  "review": {"checklist": []},
  "fix": {"max_auto_rounds": 2},
  "merge": {"auto": false, "after_workflows": []},
  "conflicts": {"auto_resolve": false, "max_rounds": 3},
  "usage_limit": {"resume": true, "retry_minutes": 60, "wait_minutes": 330},
  "retry": {"wait_minutes": [15, 30, 60]},
  "close_parent_issues": false,
  "autopilot": {
    "enabled": false,
    "scope": "milestone",
    "max_parallel": null,
    "answer": "never",
    "lost_after_minutes": 30,
    "watch_workflows": [],
    "trusted_workflows": [],
    "priority": ["nexkit:ci", "nexkit:dependabot", "nexkit:flaky"],
    "labels": {},
    "dependabot": null
  },
  "notify": [],
  "limits": {"setup_timeout_minutes": 30, "max_patch_mb": 5},
  "protected_paths": [".github/", ".nexkit/"],
  "transcript": true,
  "log": {"tool_output": "truncated"},
  "github_app": {
    "slug": "nexkit-acme",
    "app_id_secret": "NEXKIT_APP_ID",
    "private_key_secret": "NEXKIT_APP_PRIVATE_KEY"
  },
  "evidence": {"artifacts": []}
}
```

Write only the keys you change. In an object such as `merge`, the keys you leave out keep
their defaults.

Checks, autopilot, and merges and waits have pages of their own: [Configuring checks](checks.md), [Configuring autopilot](autopilot.md) and [Merges, conflicts and waits](merges-and-waits.md). The tables below link each setting's section.

**What runs and with which model**

| Key | Default | Meaning |
|---|---|---|
| `version` | `1` | Configuration format. Must be `1`. |
| `agents` | required | The agents the stages may use: a runtime, a provider, its settings and the names of the secrets it uses. See [Agents](#agents). |
| `agent` | `"claude"` | The agent for every stage. It must name an agent in `agents`; when you set it, set `model` too. |
| `model` | `"sonnet"` | Model for every stage, as the agent's runtime and provider name it. For `claude-code`: an alias (`sonnet`, `opus`, `haiku`) or a full model name. |
| `effort` | `null` | Reasoning effort for every stage, or `null` for the runtime's default. Each runtime takes its own levels; see [Agents](#agents). |
| `setup` | `[]` | Shell commands that install dependencies. They run before the agent works and before the checks. A service that a command starts in the background keeps running for the agent or the checks. |
| `checks` | `[]` | Commands that decide whether a change is acceptable, and the files where each writes its results. See [Checks](checks.md#checks). |
| `parallel_checks` | `false` | Run the checks in parallel jobs, each check, or each `group` of checks, in a job of its own, so a round takes about as long as its slowest job. See [Parallel checks](checks.md#parallel-checks). |
| `cache` | `[]` | Caches to restore before `setup`, which the base branch's own workflow saves. See [Caches](#caches). |
| `evidence.artifacts` | `[]` | Names of the CI artifacts that NexKit downloads for the plan, implement and fix agents, from the CI runs an issue names. A name ending in `*` matches every artifact that starts with the rest. See [Evidence from CI runs](checks.md#evidence-from-ci-runs). |
| `stages` | see [Stages](#stages) | Per-stage agent, model, effort and timeout. |
| `profiles` | `{}` | Stage settings for kinds of issues, chosen for each issue. See [Profiles](#profiles). |
| `default_profile` | `null` | The profile to use when no plan chose one. Required with `profiles`. |

**What NexKit does by itself**

| Key | Default | Meaning |
|---|---|---|
| `fix.max_auto_rounds` | `2` | Repair rounds NexKit may start by itself on one pull request after failing checks or blocking findings, or after a required check failed and GitHub refused an automatic merge for it (0 to 10). Rounds requested by people do not count. After failing checks, such a round first merges a base branch that has moved; see *Failing checks the base branch fixed* under [Plan, implement, fix and review](stages.md#stages). After a check failed the same way twice in a row, the next one is a [diagnosis round](failure-handling.md#failure-handling). |
| `merge.auto` | `false` | Merge (squash) when every check passes and the AI review approves, unless it leaves a criterion for a person to confirm. Branch protection still applies. See [Automatic merge](merges-and-waits.md#automatic-merge). |
| `merge.after_workflows` | `[]` | Workflow files, such as `["ci.yml"]`, that NexKit starts on the base branch after it merges. File names only, each listed once. An entry `{"workflow": "ci.yml", "every": 5}` starts it once 5 commits reached the base branch since its latest run there (1 to 1000; a file name alone is `"every": 1`). See *Running a workflow every few merges* under [Automatic merge](merges-and-waits.md#automatic-merge). |
| `conflicts.auto_resolve` | `false` | Start a fix round by itself when a NexKit pull request conflicts with its base branch. See [Conflicts with the base branch](merges-and-waits.md#conflicts-with-the-base-branch). |
| `conflicts.max_rounds` | `3` | Those rounds per pull request (1 to 10), apart from `fix.max_auto_rounds`. |
| `usage_limit.resume` | `true` | When the agent stops at the account's usage limit, run the same command again after the limit resets. See [Usage limits](merges-and-waits.md#usage-limits). |
| `usage_limit.retry_minutes` | `60` | When the reset time is unknown, try again after this many minutes (1 to 1440). |
| `usage_limit.wait_minutes` | `330` | Start a run that waits for the reset when it is at most this many minutes away (0 to 330); 0 leaves the resume to the hourly schedule. |
| `retry.wait_minutes` | `[15, 30, 60]` | After a run stops on a transient error, run the same command again after each of these waits in turn, in minutes (at most 10 waits, each 0 to 330); a stop after the last wait waits for a person. `[0]` runs it again once at once, `[]` never. See [Transient errors](merges-and-waits.md#transient-errors). |
| `close_parent_issues` | `false` | Close a parent issue when its last open sub-issue closes and at least one sub-issue was completed. See [Parent issues](merges-and-waits.md#parent-issues). |
| `autopilot.enabled` | `false` | Work through the open issues in scope without commands: plan each ready issue, start `/nexkit go` on a plan that needs nobody, and move on when an issue closes. See [Autopilot](autopilot.md#autopilot). |
| `autopilot.scope` | `"milestone"` | Which issues autopilot takes: `"milestone"`, the open issues of the earliest open milestone, or `null`, every open issue, whatever its milestone. |
| `autopilot.max_parallel` | `null` | How many issues in scope may have a NexKit run or an open NexKit pull request at once (1 to 100); `null` for no limit. |
| `autopilot.lost_after_minutes` | `30` | How long autopilot waits before it starts again a command it started whose workflow run cannot be read, or a run that a round started next that has not started (5 to 1440). See *One command per step* under [Autopilot](autopilot.md#autopilot). |
| `autopilot.answer` | `"never"` | `"recommended"` answers each question of a plan or a blocked round with the option NexKit recommends, when every question has one; `"never"` leaves every question to a person. |
| `autopilot.priority` | `["nexkit:ci", "nexkit:dependabot", "nexkit:flaky"]` | Labels, highest first, whose issues go before the others, and are in scope also without a milestone or outside the scope's milestone. See *Labels* under [Autopilot](autopilot.md#autopilot). |
| `autopilot.labels` | `{}` | What a label does: `{"wave": N}` holds an issue until every issue of a lower wave in its milestone is closed; `{"hold": true}` makes autopilot start nothing for it. See *Labels* under [Autopilot](autopilot.md#autopilot). |
| `autopilot.trusted_workflows` | `[]` | Workflow files, such as `["docs-audit.yml"]`, whose issues autopilot takes when their marker names a run that checks out. See [Issues from a workflow](autopilot.md#issues-from-a-workflow). |
| `autopilot.dependabot` | `null` | The lowest severity of Dependabot alert that gets an issue: `"low"`, `"medium"`, `"high"` or `"critical"`. `null` opens none. It needs an extra permission of the App. See *Dependabot advisories* under [Autopilot](autopilot.md#autopilot). |
| `autopilot.watch_workflows` | `[]` | Workflow files, such as `["ci.yml"]`, whose runs on the default branch autopilot watches as it watches `merge.after_workflows`, whatever started them, but which NexKit does not start after its merges. File names only, each listed once, none also in `merge.after_workflows`. See *Watching CI that NexKit does not start* under [Autopilot](autopilot.md#autopilot). |
| `notify` | `[]` | GitHub logins to mention when a run ends in a state that needs a person, such as `["owner"]`, each listed once. See [Mentions](merges-and-waits.md#mentions). |

**Limits and safety**

| Key | Default | Meaning |
|---|---|---|
| `limits.setup_timeout_minutes` | `30` | Time each `setup` command may take (1 to 120). The agent and verify jobs allow for it. |
| `limits.max_patch_mb` | `5` | Largest change an implement or fix round may publish, in megabytes (1 to 50). |
| `protected_paths` | `[".github/", ".nexkit/"]` | Path prefixes the agent may not change. Both defaults are required; you may add more. |
| `review.checklist` | `[]` | Short rules, in your own words, that every plan and review answers one by one; `{"text": …, "person": true}` for one that a person decides. See [Review checklist](#review-checklist). |
| `transcript` | `true` | Write the agent's transcripts (`transcript.jsonl`, `transcript.md`) into the `nexkit-agent` and `nexkit-review` artifacts. See [Logs and transcripts](#logs-and-transcripts). |
| `log` | `{"tool_output": "truncated"}` | How the agent and review jobs print tool calls. |
| `github_app` | none, required | The GitHub App that NexKit acts as: its `slug`, as in `github.com/apps/<slug>`, and the names of the secrets that hold its id (`app_id_secret`) and its private key (`private_key_secret`). `nexkit init` sets it. The secrets cannot be an agent's or start with `GITHUB_`. See [The GitHub App](#the-github-app). |

## Caches

```json
"cache": [
  {
    "name": "gradle",
    "paths": ["~/.gradle/caches", "~/.gradle/wrapper"],
    "key_files": ["android/gradle/libs.versions.toml", "android/gradle/wrapper/gradle-wrapper.properties"]
  },
  {"name": "npm", "paths": ["~/.npm"], "key_files": ["web/package-lock.json"]}
]
```

Without caches, every agent and `verify` job installs toolchains and dependencies from
scratch. With them, both jobs restore each entry before `setup`, so `setup` only adds
what changed. NexKit **only restores**; your own workflow on the base branch saves.

- `name`: short lowercase identifier, part of the key. Two names cannot be the same, and
  one cannot be another followed by `-`, because they would share keys.
- `paths`: what to restore, as for `actions/cache`. Each path starts with `~/` or `/`
  (a `!` before it excludes). NexKit checks the repository out in a subdirectory, so a
  path inside the repository would not land where your workflow saved it. Cache what
  lives outside it: package manager caches (`~/.npm`, `~/.cargo/registry`), toolchains
  (`~/.rustup`, an SDK under `~`) and downloaded browsers (`~/.cache/ms-playwright`).
- `key_files`: patterns inside the repository, as for `hashFiles`, usually lock files and
  version files. Their contents decide the key; `!` excludes.
- At most 8 entries. The agent job restores them for `go` and fix rounds; a plan runs no
  `setup`.

**Why NexKit never saves.** The agent and `verify` jobs run code the agent wrote, and
NexKit's runs use the base branch's cache scope. A cache saved there would reach every
later run, including your CI on the base branch. Restoring needs no token and no secret,
so `verify` keeps none.

**The key.** For each entry NexKit restores

```text
nexkit-<runner.os>-<name>-<hashFiles(key_files)>
```

and, when that key has no cache, the newest cache whose key starts with
`nexkit-<runner.os>-<name>-`. With an older cache, `setup` installs what changed since.

**Who saves.** A workflow of your repository that runs on the base branch saves the caches,
for example the CI that runs on pushes to the default branch. It uses NexKit's action, which
reads the entries from `.nexkit/config.json`, so you state your caches once. Add one step,
after the checkout and before your setup, at the tag your NexKit workflow uses:

```yaml
- uses: actions/checkout@v4
- uses: phuongnse/nexkit/cache@vX.Y.Z
- run: scripts/setup.sh
```

The action restores each entry and saves it at the end of a successful job when its key had
no cache, under the key NexKit restores. It saves only on the base branch, since caches
saved on other branches are not visible to NexKit; on any other branch or pull request it
only restores, so your CI starts warm there too. For NexKit to find what it saves:

- The step comes before your setup, so the key files are hashed as committed, as NexKit
  hashes them.
- The job runs on the runner that [supported versions](../README.md#supported-versions) names,
  like NexKit, or on `ubuntu-latest`.
- With the checkout in a folder, name it with `repo:`. A run on a schedule may not say which
  branch is the default one; give it with `base: main`, or it only restores.

`nexkit doctor` reports a ✗ when `cache` is set and no workflow saves the caches, since every
run then starts cold. A hand-written `actions/cache` step that repeats an entry exactly also
works. When a workflow has a key that starts with `nexkit-<runner.os>-`, doctor passes it and
suggests the action instead.

**A miss is not a failure.** A missing cache, or a restore that fails, never fails a run.
The *Restore caches* step in the job log lists each entry as restored, restored from an
older key, or missed, with the key it looked for.

## Review checklist

A repository has rules that every pull request must meet beyond its checks, for example that
the docs change with the code. Written in CLAUDE.md or AGENTS.md they depend on the agent
remembering them. `review.checklist` makes each one a question that needs an answer:

```json
"review": {
  "checklist": [
    "Every doc section the change makes false is fixed in the same change",
    "A new setting has a default that keeps today's behaviour",
    {"text": "The change adds no exception to a check and loosens none", "person": true}
  ]
}
```

- **The plan** states how it meets each item, in its *Checklist* section.
- **The review** answers each item as `met`, `not_met` or `not_applicable`, with its evidence
  from the diff. The review comment lists each item with its answer, and the evidence
  collapsed.
- **An item not met** requests changes as a blocking finding does, whatever verdict the
  review returned, so an automatic fix round addresses it, within `fix.max_auto_rounds`.
- **An item a person decides**, written `{"text": "...", "person": true}`, is one that a
  fix cannot meet without undoing the change, such as one that accepts a risk. The review
  answers it like any other, and NexKit marks it ⚖️ when it is not met, but it is no finding
  and starts no fix round. When nothing else blocks, the round ends `ready`: NexKit does not
  merge it, `nexkit/review` fails with "A person decides …", the round's comment lists the
  items with their evidence, and `notify` hears of it once. A writer's approval of that
  commit accepts them: `nexkit/review` turns green, and with `merge.auto` NexKit merges it;
  an approval of an older commit does nothing, and a new commit needs a new review and a new
  approval. Beside other blocking findings or failing checks, the fix round starts for
  those, as for any blocking finding.
- **A review that leaves an item out** is incomplete: it gives no verdict, `nexkit/review`
  shows an error, nothing merges, and `/nexkit review` tries again. A plan that leaves one out
  is posted as it is, since the review decides.

Write each item as something an agent can check in the code and change, in at most 300
characters; a list holds at most 20. A list stays short and covers more when each item names the
doc, or the section of it, that holds a kind of rules, such as "The change keeps every rule of
docs/testing.md: read it and check the diff against each of its rules": a new rule then goes into
that doc, not into the list. An item about what agents cannot change, such as an
approval or the pull request's text, can never be met. An empty list, the default, leaves
plans and reviews as they are.

## Agents

An agent is a **runtime**, the CLI that runs the tool loop, paired with a **provider**,
where the model is served and how it authenticates. Every agent is declared here, with the
names of the secrets it uses; stages choose one by name. `nexkit init` declares `claude`:
Claude Code with a Claude subscription token. Declare as many agents as you need, for
example a second Claude account for some stages:

```json
"agents": {
  "claude": {
    "runtime": "claude-code",
    "provider": "anthropic",
    "oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN"
  },
  "team": {
    "runtime": "claude-code",
    "provider": "anthropic",
    "api_key_secret": "TEAM_ANTHROPIC_KEY"
  }
},
"default_profile": "standard",
"profiles": {
  "standard": {
    "when": "Clear spec, one layer, follows an existing pattern.",
    "stages": {
      "plan":      {"model": "claude-opus-5-5"},
      "implement": {"model": "claude-sonnet-5-5"},
      "review":    {"agent": "team", "model": "claude-opus-5-5"}
    }
  },
  "hard": {
    "when": "Touches security or access control, storage schema or execution.",
    "stages": {
      "plan":      {"model": "claude-opus-5-5"},
      "implement": {"model": "claude-sonnet-5-5", "effort": "high"},
      "review":    {"model": "claude-fable-5-1"}
    }
  }
}
```

- An agent name is a short lowercase identifier. `runtime` and `provider` are required;
  the other keys are `fallback` (see [Fallback agents](#fallback-agents)) and the
  provider's settings, and unknown keys are rejected.
- `agent` layers like `model`: `profiles.<profile>.stages.<stage>`, then
  `stages.<stage>`, then the top-level `agent`. Wherever `agent` is set, at the top level,
  in a stage or in a profile, `model` must be set next to it, because a model name means
  something only to its agent.
- Each runtime checks the settings of the stages it runs, in every profile. A setting it
  cannot honour, such as an effort level it does not know, is a configuration error.

The runtimes and providers NexKit supports and tests today. Each is a module in
`nexkit/runtimes/`, so more can be added; a runtime is supported only with a live smoke
test in CI.

| Runtime | Providers | `effort` |
|---|---|---|
| `claude-code` | `anthropic` | `low`, `medium`, `high`, `xhigh`, `max` |

Every runtime reports the tokens of its runs in the same form, which the run summary and
the run comments show; see [Logs, summary and
artifacts](how-it-works.md#logs-summary-and-artifacts).

| Provider | Settings | Secret |
|---|---|---|
| `anthropic` | none | Exactly one of `oauth_token_secret`, the secret that holds a Claude subscription token from `claude setup-token`, and `api_key_secret`, the secret that holds an Anthropic API key. Claude Code receives it under its own variable, whatever the secret's name. |

`claude-code` reports usage limits, so its runs pause at one or move to a fallback; see
[Usage limits](merges-and-waits.md#usage-limits).

### Fallback agents

An agent can name other agents that take over when it stops at a usage limit, such as an
Anthropic API key behind a Claude subscription:

```json
"agents": {
  "claude": {
    "runtime": "claude-code",
    "provider": "anthropic",
    "oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN",
    "fallback": ["claude-api"]
  },
  "claude-api": {
    "runtime": "claude-code",
    "provider": "anthropic",
    "api_key_secret": "ANTHROPIC_API_KEY"
  }
}
```

- A stage always starts on its own agent. Only when that run stops at a usage limit does
  the stage start again, from the beginning, on the next agent of `fallback`, and so on.
  Every run starts on the agent itself again, so once its limit resets NexKit is back on
  it. When every agent stopped at its limit, the run pauses until the earliest reset (see
  [Usage limits](merges-and-waits.md#usage-limits)).
- Nothing else moves to a fallback: an error, a timeout or a `blocked` result ends the
  stage as it does without one.
- Before implement or fix starts again, NexKit returns the checkout to where the first
  agent started: the setup baseline, and in a fix round the merge of the base branch with
  its conflicts. What the agent at its limit changed is dropped.
- Fallback agents use the same runtime as the agent, because the stage's `model` and
  `effort` are meant for it. Only the stage's agent's own list counts: when `claude-api`
  has a `fallback` too, it applies to the stages that choose `claude-api`.
- `timeout_minutes` applies to each agent's run, and the job allows for every agent of
  the list, up to 340 minutes for a stage. The stage's tokens add up every agent's run.
- A fallback agent's secrets count like any agent's: the job that may run it receives
  them, they count toward the 8 per job, and `nexkit doctor` checks them. A fallback
  whose secret is not set is skipped, and the run summary says so.
- `fallback` is a non-empty list of other agents, each named once. It is rejected for a
  runtime that does not report usage limits, since it would never run.

The run summary names the agent of each stage, and when a fallback ran it lists every
agent's run with its result, duration and tokens. The run comment on the issue or pull
request names the agent and model of each stage, for example
"**Agents:** implement `claude-api` (`claude-sonnet-5-5`) after `claude`
(`claude-sonnet-5-5`) hit its usage limit · review `claude` (`claude-opus-5-5`)".

### Settings and secrets

A provider declares which of its settings are secrets: keys, and anything that
identifies your own account or resource, such as an endpoint. A secret
lives in a repository secret, and the configuration holds only its name, in a key that
ends in `_secret`. Choose the names freely, except names that start with `GITHUB_` or
`NEXKIT_`, which belong to GitHub and NexKit (the secrets of `github_app`, such as
`NEXKIT_APP_ID`, are NexKit's own and may use the prefix). A value that is not a secret name, such as a
key pasted into the configuration, is rejected. `nexkit doctor` checks that each secret is
set and that the repository's NexKit workflow passes it.

## Stages

```json
"stages": {
  "plan":      {"timeout_minutes": 15},
  "implement": {"model": "opus", "timeout_minutes": 45},
  "review":    {"model": "opus", "effort": "high", "timeout_minutes": 20}
}
```

Fix rounds use the `implement` settings. `triage` is a fourth stage, allowed only with
`profiles` (see below). Each stage accepts:

| Key | Meaning |
|---|---|
| `agent` | Overrides the top-level `agent`. Needs `model` next to it. |
| `model` | Overrides the top-level `model`. |
| `effort` | Overrides the top-level `effort`. |
| `timeout_minutes` | Stop the agent after this long (plan 15, implement 45, review 20 and triage 5 by default; triage at most 30, other stages at most 340). With [fallback agents](#fallback-agents), each agent's run gets this long. |

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
- Settings are layered: the top-level `agent`, `model` and `effort`, then
  `stages.<stage>`, then `profiles.<profile>.stages.<stage>`. What a profile leaves out
  comes from `stages`. A profile can also run a stage on another agent, such as a second
  Claude account.
- `stages.triage` sets the agent, model, effort and timeout of the triage call. It
  takes the top-level `agent`, `model` and `effort` unless it sets its own. A profile
  cannot set it, because triage runs before there is a profile.
- `default_profile` is used when no plan chose a profile: `/nexkit go` without a plan, a
  plan that chose no profile, and a plan whose triage failed. The cheapest
  profile is a safe choice, so a failure never costs more.
- Write `when` in terms that an issue shows: the parts of the system it touches, how
  clear the request is. Triage reads only the issue and its discussion, not the code.

**Writing profiles.**

- Order the profiles by risk and novelty: the more a mistake would cost, or the less the
  repository has done such work before, the stronger the profile.
- Make the cheap profile's `when` text narrow, naming only work that repeats what the
  repository already does. Anything it does not name then goes to the stronger profile,
  so a new kind of work lands there without a change to the texts.
- Make plan and review at least as strong as implement. The plan is the contract, and the
  review is the last check before a merge.
- A `when` text says what a mistake would cost, not where the code is. Name no files,
  classes, doc sections or milestones: they go stale, and triage sees only the issue.
- Look at the profiles again when a new kind of work starts and when new models come out.
  Compare each profile's share of issues whose rounds stopped on an error, ran out of time
  or failed their checks: a cheap profile that fails more often than a stronger one has a
  `when` text that is too wide. Check whether newer models make a profile's models out of
  date.

[Agents and profiles](agents-and-profiles.md#profiles) describes how triage chooses and how to change a
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

`.github/workflows/nexkit.yml` passes the pipeline `secrets: inherit`, as `nexkit init`
writes it, so the pipeline can read each agent's secrets by the names in
`.nexkit/config.json`. It passes a job only the secrets of the agents its stages use, at
most 8 per job, and each agent's CLI only its own; setup and check commands get none. See
[Agents and profiles](agents-and-profiles.md#agents) for what this does and does not isolate. An older
workflow that lists secrets one by one can pass only `CLAUDE_CODE_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY`, `NEXKIT_APP_ID` and `NEXKIT_APP_PRIVATE_KEY`; `nexkit doctor` says when
an agent or the App needs `secrets: inherit`.

| Secret | Required | Used by |
|---|---|---|
| The names an agent's `*_secret` keys give | for each agent a stage uses; a fallback agent without its secret is skipped | Agent and review jobs, only for the stages that run on that agent. For `anthropic`: the subscription token (`claude setup-token`, Claude Pro or Max) or the API key. |
| The names `github_app` gives, such as `NEXKIT_APP_ID` and `NEXKIT_APP_PRIVATE_KEY` | yes | The step of each job that writes that mints its token, and nothing else: never a job that runs an agent or repository code, and never an agent's secret. See [The GitHub App](#the-github-app). |

## The GitHub App

NexKit acts on GitHub only as a GitHub App: every push, pull request, merge, issue, comment,
label, reaction, status and workflow dispatch. That makes its work count like a person's:
its pushes and merges start your workflows, its runs send `workflow_run`, and it can change
files under `.github/workflows/`, none of which the Actions token (`github.token`) may do.
The App belongs to no person, and its tokens expire within an hour.

**Each owner brings an App of their own.** NexKit runs inside your repository's Actions and
has no server, so it publishes no shared App: that would need its private key in one place
for every repository. The owner of a repository, a person or an organization, creates one
private App under their account and installs it on every repository that uses NexKit.

**Your first repository.** `nexkit init` creates the App with GitHub's App manifest flow: it
opens your browser on a page that holds exactly the permissions below, you confirm the name,
and it stores the App id and the private key as the repository's secrets. Then install the
App on the repository at the link it prints. A name is unique across GitHub, and "NexKit" is
taken: the page proposes "NexKit <owner>", which you can change there, or choose one with
`nexkit init --app-name NAME`. Its comments then show as `<slug>[bot]`.

**Every other repository.** The first repository needed no key by hand, because the manifest
flow hands the key to `nexkit init` once, and it goes straight into that repository's
secrets. GitHub shows a key only then, and no one can read a secret back, so each later
repository of a personal account needs a new key. An organization can hold the secrets once
instead.

1. Install the App on the repository, at `https://github.com/apps/<slug>/installations/new`.
2. Open the App's settings page: `https://github.com/settings/apps/<slug>`, or for an
   organization `https://github.com/organizations/<org>/settings/apps/<slug>`. The App ID is
   under *About*, and *Generate a private key* is under *Private keys*, near the bottom.
   This is not the page of the App's installation
   (`https://github.com/settings/installations/<id>`), which GitHub links from the repository
   and from the list of Apps and which shows neither.
3. Run `nexkit init --app-id ID --app-private-key FILE`, which checks that the App is
   installed and sets both secrets. An App can have several keys, so a new one leaves the
   other repositories as they are.

An organization can set both as organization secrets once, then run `nexkit init --app-slug
SLUG` in each repository. When the repository lacks either secret, that command stops before it
writes anything, also for an organization, and prints the two links and the command above.
When GitHub cannot say, it goes on, and `nexkit doctor` checks the secrets later.

**Permissions.** NexKit needs these repository permissions, and each job asks for only what
it uses:

| Permission | Level | For |
|---|---|---|
| Actions | write | Starting rounds, sweeps and `merge.after_workflows`; stopping rounds; reading runs |
| Checks | read | The failures of required checks and of runs on the default branch |
| Contents | write | Pushing branches, merging pull requests |
| Issues | write | Comments, labels, reactions; opening and closing issues |
| Pull requests | write | Opening, updating and reviewing pull requests |
| Commit statuses | write | `nexkit/checks` and `nexkit/review` |
| Workflows | write | Pushing and merging changes to `.github/workflows/` |
| Dependabot alerts | read | Only with [`autopilot.dependabot`](autopilot.md#dependabot-advisories): reading open alerts. `nexkit init` leaves it out, so add it by hand. |

**Who holds what.** Each job that writes mints an installation token for this repository in a
step of its own, with
[`actions/create-github-app-token`](https://github.com/actions/create-github-app-token), and
[Tokens and secrets](architecture.md#tokens-and-secrets) lists the token each job holds. A
job that runs an agent or repository code holds neither the key nor a token: its runner gets
only the secrets its stages need, which is why the `agent` and `review` jobs are reusable
workflows of their own. Those jobs, and the run's wait for paused commands, read with the
job's `github.token`, which is why the workflow file still gives read permissions.

**Your CI runs what agents wrote.** NexKit's pushes start your `pull_request` and `push`
workflows on its branches, on code an agent wrote. Give those workflows only what they need,
with `permissions:` or a read-only default under *Settings → Actions → General*, as for a
contributor's pull request. Their secrets are reachable from that code.

**Checking it.** `nexkit doctor` checks that the settings and the secrets are there and that
the App has the permissions. GitHub lets only the App itself ask where it is installed, so
`nexkit doctor --app-id ID --app-private-key FILE` also checks the App, which GitHub shows to
no person's token when it is private, and its installation on this repository; the key stays
in its file. Without that option doctor says that it could not tell. A run without
`github_app` stops at its start with what to do, and so does one that gets no token.

**Comments of the Actions bot.** NexKit counts only what its App wrote and no one else
changed, so what the Actions bot wrote proves nothing. See
[The Actions bot](security.md#the-actions-bot).

## Reviewing your own pull requests

NexKit's agents may not change `protected_paths`, so a change to `.github/` or `.nexkit/`,
such as a NexKit upgrade, is a person's pull request. Comment `/nexkit review` on it, from an
account with write access. NexKit runs its checks and its review on the head and sets
`nexkit/checks` and `nexkit/review`, as for a NexKit pull request, and reviews the protected
paths like any other code. See *A person's pull request* under [How it
works](stages.md#stages).

So a repository whose rules on the default branch require those two statuses needs no admin
bypass for these changes: comment the command, read the review, and merge. A new commit needs
the command again.

It works for a pull request from a branch of this repository. It never pushes, starts a fix
round or merges, also with `merge.auto` on, so a person makes any change and the merge. The
checks and `setup` come from `.nexkit/config.json` on the default branch, so a change to them
counts from its merge on. The workflow file that `nexkit init` writes lets the command
through, as it does every `/nexkit` comment.

**CI that runs the same checks.** Many CI workflows run on every push to any branch, as the
only check of a person's branch. With `/nexkit review`, the commands of `checks` then run
twice on such a branch: once in CI on the push,
once in NexKit's `verify` on the command. `nexkit doctor` names each workflow whose `run`
steps call a command of `checks` and whose `push` trigger reaches a branch other than the
default branch and NexKit's, or whose `pull_request` trigger runs for a pull request into
the default branch, with the trigger and the checks it repeats. Let such a workflow run on
the default branch only, and on a pull request only when it changes the workflow itself:

```yaml
on:
  push:
    branches: [main]
  pull_request:
    paths: [.github/workflows/ci.yml]
```

When the default branch's rules require that workflow's jobs, require `nexkit/checks` and
`nexkit/review` instead, which `/nexkit review` sets. A workflow that runs other commands
than `checks`, such as a deployment, needs no change, nor does a CodeQL scan
(`github/codeql-action`), whose build serves its analysis.

## Workflow file

`nexkit init` writes `.github/workflows/nexkit.yml`, which calls
`phuongnse/nexkit/.github/workflows/pipeline.yml` at a fixed release tag. Its triggers start
NexKit for these events:

| Trigger | Starts NexKit for |
|---|---|
| `issue_comment` | A comment that starts with `/nexkit`, on an issue or a pull request. |
| `pull_request_review` | A writer's *Request changes* review starts a fix round, and an approval merges a pull request that GitHub refused to merge, by a `step` run in the issue's queue, both only on a `nexkit/` branch. See [Automatic merge](merges-and-waits.md#automatic-merge). |
| `issues` | A closed issue (for `close_parent_issues` and `autopilot`), and an opened, reopened, milestoned, labelled or unlabelled issue (for `autopilot`). The `null` scope needs no milestone events, and only `autopilot.priority` and `autopilot.labels` need label events. |
| `milestone` | A closed milestone (for `autopilot`, when the next one comes into scope). |
| `push` | A push to the default branch (for `conflicts.auto_resolve`). |
| `schedule` | An hourly run, for `usage_limit.resume`, `retry.wait_minutes`, `conflicts.auto_resolve` and `autopilot`. |
| `workflow_run` | A finished run on the default branch of a workflow in `merge.after_workflows` or `autopilot.watch_workflows`, for `autopilot`. It is written as a comment unless `nexkit init --after-workflow` or `--watch-workflow` is given. See *When a finished run is noticed* under [Autopilot](autopilot.md#autopilot). |
| `workflow_dispatch` | A run that a person starts by hand, or that NexKit starts with its `auto` input set, for automatic, conflict, resumed, retried and autopilot runs. |

With those settings off, these events start short runs that end at once; remove the triggers
you do not use if you prefer. Issue and milestone events other than a closed issue join the
concurrency group `nexkit-autopilot`, so they never replace a command that waits for its
issue. Each starts a sweep, even a milestone event with the `null` scope, since its run may
have replaced a sweep that waited.

The workflow's first job, `item`, names the work item of each run for its concurrency
group (see [One run at a time](how-it-works.md#one-run-at-a-time)). It checks out NexKit's
code at the release tag and runs `nexkit item`, with a read-only token, no GitHub App key
and no checkout of your repository. So the commands on an issue and the rounds on its
NexKit pull request wait in one queue. `nexkit doctor` reports a workflow without the
`item` job: its runs may overlap. To upgrade, change three refs to the new tag: the
`uses:` ref, `nexkit_ref` and the `ref:` of the `item` job's checkout (or rerun `nexkit
init --force --kit-ref vX.Y.Z` and restore your config); see [the upgrade
steps](#upgrading-from-earlier-releases). `nexkit doctor` reports a problem when they
differ, and mentions a newer release without failing: staying on a release is your choice.

NexKit's pull requests come from branches named `nexkit/issue-<number>`. The job's `if:`
in the workflow file lets a *Request changes* review or an approval start a run only on a
branch that starts with `nexkit/`. A `/nexkit` comment gets through on any pull request, and
`route` says what it does there.

## Upgrading from earlier releases

The workflow file and `.nexkit/` are protected paths, so a person makes an upgrade, in one
pull request:

1. Read the notes and the changelog of every release after the pinned one, not only the
   latest. Each release's notes list its own upgrade steps.
2. Pin the `uses:` ref, `nexkit_ref` and the `ref:` of the `item` job's checkout in the
   workflow file to the new tag.
3. Compare this reference's key tables and sections at the old and the new ref. Set every
   new key, or new value of a key, that the repository has a value for, such as the
   names of its own CI workflows or artifacts.
4. Check that the workflow file grants every permission the release asks for.
5. Run `nexkit doctor` in the checkout of that pull request's branch, and fix every ✗
   line. It runs the release the workflow now pins (see [Local
   commands](../README.md#local-commands)).
6. Comment [`/nexkit review`](#reviewing-your-own-pull-requests) on the pull request, which
   checks and reviews it and sets the statuses your rules may require.

A configuration from an earlier release that no longer fits is rejected with what to
change.

### Moving to the GitHub App

NexKit now writes only as a [GitHub App](#the-github-app), so an existing repository needs
one before it upgrades. Runs of the new release stop at their start without it, and
`publish.push_token_secret` is refused.

Do this between rounds. NexKit no longer reads the comments of earlier releases, so an issue
with a plan is planned again, a pull request starts its automatic rounds afresh, and a run
that is under way when you upgrade is not picked up. Bug issues that autopilot opened as the
Actions bot are left alone: close them, and the next failure opens a new one.

Parts that an earlier release created were opened as the Actions bot too, and autopilot leaves
them alone. They hold planned work, and an open pull request is tied to each by its
`nexkit/issue-<n>` branch, so do not close them: `nexkit status` lists each one under *Needs a
person* with the command, and [`/nexkit trust`](autopilot.md#vouching-for-an-issue) on it makes autopilot
carry it on, with its pull request. Each part needs its own comment, also when a writer opened
its parent.

1. Create the App once per owner. Run `nexkit init --force` in a clone and let it create
   the App, then restore your `.nexkit/config.json` and workflow file and keep only the new
   `github_app` block. By hand, create an App under your account or organization with the
   [permissions above](#the-github-app), no webhook, and *Only on this account*, and
   generate a private key.
2. Install the App on the repository.
3. Set the secrets, with `nexkit init --force --app-id ID --app-private-key FILE` or
   `gh secret set NEXKIT_APP_ID` and `gh secret set NEXKIT_APP_PRIVATE_KEY < key.pem`.
4. In `.nexkit/config.json`, remove `publish` and add `github_app`:

   ```json
   "github_app": {
     "slug": "<the App's slug>",
     "app_id_secret": "NEXKIT_APP_ID",
     "private_key_secret": "NEXKIT_APP_PRIVATE_KEY"
   }
   ```

5. In the workflow file, `permissions:` may shrink to `contents`, `issues`, `pull-requests`,
   `actions` and `checks`, all `read`, as `nexkit init` writes it. A workflow that lists
   secrets one by one passes `NEXKIT_APP_ID` and `NEXKIT_APP_PRIVATE_KEY`.
6. Delete what the App makes unnecessary: the `NEXKIT_PUSH_TOKEN` secret and the personal
   token behind it, each `nexkit-sweep` job, the workaround that left NexKit's branches out
   of your CI, and *Allow GitHub Actions to create and approve pull requests* if nothing
   else needs it.
7. Add the App to the bypass list of any rule that the Actions token passed by another way,
   and check the rules that restrict who may push.
8. Expect more of your own CI: pushes and pull requests by NexKit now start it. Workflows
   you listed in `merge.after_workflows` also start from a merge when they have a `push`
   trigger for the default branch, so remove one of the two. `nexkit doctor` names each such
   workflow, whether by `branches`, by a `branches-ignore` that leaves the default branch
   out, or by no filter.

### Wave labels replace the last-in-milestone label in 7.0

NexKit 6.x could hold an issue until the rest of its milestone closed, with
`autopilot.last_in_milestone_label`. NexKit 7.0 rejects a label name there and says what to
set instead. `"last_in_milestone_label": null`, as the full example of 6.x showed, is still
accepted and does nothing. Remove the key, and give the label a wave in `autopilot.labels`:

```json
"autopilot": {"labels": {"<label>": {"wave": 1}}}
```

Issues with that label then run side by side, so give one of them a higher wave to keep
them in order. See *Labels* under [Autopilot](autopilot.md#autopilot).

### Money limits removed

NexKit rejects `max_budget_usd` in every stage and profile stage, because NexKit sets no
money limit. `timeout_minutes` limits each agent's run instead. Remove the key, and lower
`timeout_minutes` where a stage needs a tighter limit. See [Stages](#stages).

### Bug issues of earlier releases

Autopilot records each failure on a `bug` issue, as *Flaky runs* under
[Autopilot](autopilot.md#autopilot) describes. An issue that NexKit 5.0 or earlier opened for
one job's step keeps getting that job's records. An issue from NexKit 4.3.0, which holds every
failure of a workflow, gets no new records.

### Label scope removed in 5.0

NexKit 4.x could limit autopilot to the open issues with one label (`"scope": "label"`
with `autopilot.label`). NexKit 5.0 rejects `"scope": "label"` and a label name in
`autopilot.label` with what to set instead; `"label": null`, as the full example of 4.x
showed, is still accepted and does nothing. Set `autopilot.scope` to `null`, which takes
every open issue and lets the *blocked by* relationships order the work, or to
`"milestone"`, and remove `autopilot.label`. Issues
for CI failures no longer get a scope label.

### Codex removed in 4.0

NexKit 3.x could run stages on Codex with OpenAI models on Azure AI Foundry. NexKit 4.0
supports and tests only Claude Code, so an agent with `"runtime": "codex"` or
`"provider": "azure-openai"` is rejected with what to do. Remove such agents from `agents`,
and give the stages and profiles that chose them a Claude agent and a Claude model, for
example `"review": {"agent": "claude", "model": "claude-opus-5-5"}`, or drop `agent` to use
the default. The Azure secrets are no longer read; delete them when nothing else uses them.

### Agents are declared

NexKit 2.1 and earlier ran every stage on a Claude agent that the configuration did not
mention. Since 3.0, every agent is declared in `agents`, with the names of the secrets it
uses, so the configuration shows every secret NexKit reads. A configuration without
`agents` is rejected with what to add:

```json
"agents": {
  "claude": {"runtime": "claude-code", "provider": "anthropic", "oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN"}
}
```

With an Anthropic API key, write `"api_key_secret": "ANTHROPIC_API_KEY"` instead of
`oauth_token_secret`.

### Keys renamed in 2.0

NexKit 1.10 and earlier had three of these keys at the top level. A configuration that
still uses them is rejected with the new name, so move them:

| Before | Now |
|---|---|
| `"max_auto_fixes": 2` | `"fix": {"max_auto_rounds": 2}` |
| `"auto_merge": true` | `"merge": {"auto": true}` |
| `"after_merge_workflows": ["ci.yml"]` | `"merge": {"after_workflows": ["ci.yml"]}` |
