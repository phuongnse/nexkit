# Configuration reference

NexKit reads `.nexkit/config.json` from the repository's **default branch** on every run.
Changes on a pull request branch never affect the run that is checking that branch.
Unknown keys are rejected so typos fail loudly.

The runner (`ubuntu-24.04`), the version of each agent runtime and NexKit's Python
version are set by the NexKit release and are not configurable. See
[supported versions](../README.md#supported-versions).

A complete configuration with every key at its default, except `agents`, `setup` and
`checks`, which `nexkit init` writes:

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
  "fix": {"max_auto_rounds": 2},
  "merge": {"auto": false, "after_workflows": []},
  "conflicts": {"auto_resolve": false, "max_rounds": 3},
  "usage_limit": {"resume": true, "retry_minutes": 60, "wait_minutes": 330},
  "close_parent_issues": false,
  "autopilot": {"enabled": false, "scope": "milestone", "max_parallel": null, "answer": "never"},
  "notify": [],
  "limits": {"setup_timeout_minutes": 30, "max_patch_mb": 5},
  "protected_paths": [".github/", ".nexkit/"],
  "transcript": true,
  "log": {"tool_output": "truncated"},
  "publish": {"push_token_secret": null}
}
```

Write only the keys you change. In an object such as `merge`, the keys you leave out keep
their defaults.

**What runs and with which model**

| Key | Default | Meaning |
|---|---|---|
| `version` | `1` | Configuration format. Must be `1`. |
| `agents` | required | The agents the stages may use: a runtime, a provider, its settings and the names of the secrets it uses. See [Agents](#agents). |
| `agent` | `"claude"` | The agent for every stage. It must name an agent in `agents`; when you set it, set `model` too. |
| `model` | `"sonnet"` | Model for every stage, as the agent's runtime and provider name it. For `claude-code`: an alias (`sonnet`, `opus`, `haiku`) or a full model name. |
| `effort` | `null` | Reasoning effort for every stage, or `null` for the runtime's default. Each runtime takes its own levels; see [Agents](#agents). |
| `setup` | `[]` | Shell commands that install dependencies. They run before the agent works and before the checks. |
| `checks` | `[]` | Commands that decide whether a change is acceptable, and the files where each writes its results. See [Checks](#checks). |
| `parallel_checks` | `false` | Run the checks in parallel jobs, each check, or each `group` of checks, in a job of its own, so a round takes about as long as its slowest job. See [Parallel checks](#parallel-checks). |
| `cache` | `[]` | Caches to restore before `setup`, which the base branch's own workflow saves. See [Caches](#caches). |
| `stages` | see [Stages](#stages) | Per-stage agent, model, effort, timeout and budget. |
| `profiles` | `{}` | Stage settings for kinds of issues, chosen for each issue. See [Profiles](#profiles). |
| `default_profile` | `null` | The profile to use when no plan chose one. Required with `profiles`. |

**What NexKit does by itself**

| Key | Default | Meaning |
|---|---|---|
| `fix.max_auto_rounds` | `2` | Repair rounds NexKit may start by itself on one pull request after failing checks or blocking findings, or after a required check failed and GitHub refused an automatic merge for it (0 to 10). Rounds requested by people do not count. After failing checks, such a round first merges a base branch that has moved; see *Failing checks the base branch fixed* under [How it works](how-it-works.md#stages). After a check failed the same way twice in a row, the next one is a [diagnosis round](how-it-works.md#failure-handling). |
| `merge.auto` | `false` | Merge (squash) when every check passes and the AI review approves, unless it leaves a criterion for a person to confirm. Branch protection still applies. See [Automatic merge](#automatic-merge). |
| `merge.after_workflows` | `[]` | Workflow files, such as `["ci.yml"]`, that NexKit starts on the base branch after it merges. File names only, each listed once. |
| `conflicts.auto_resolve` | `false` | Start a fix round by itself when a NexKit pull request conflicts with its base branch. See [Conflicts with the base branch](#conflicts-with-the-base-branch). |
| `conflicts.max_rounds` | `3` | Those rounds per pull request (1 to 10), apart from `fix.max_auto_rounds`. |
| `usage_limit.resume` | `true` | When the agent stops at the account's usage limit, run the same command again after the limit resets. See [Usage limits](#usage-limits). |
| `usage_limit.retry_minutes` | `60` | When the reset time is unknown, try again after this many minutes (1 to 1440). |
| `usage_limit.wait_minutes` | `330` | Start a run that waits for the reset when it is at most this many minutes away (0 to 330); 0 leaves the resume to the hourly schedule. |
| `close_parent_issues` | `false` | Close a parent issue when its last open sub-issue closes and at least one sub-issue was completed. See [Parent issues](#parent-issues). |
| `autopilot.enabled` | `false` | Work through the open issues in scope without commands: plan each ready issue, start `/nexkit go` on a plan that needs nobody, and move on when an issue closes. See [Autopilot](#autopilot). |
| `autopilot.scope` | `"milestone"` | Which issues autopilot takes: `"milestone"`, the open issues of the earliest open milestone, or `null`, every open issue, whatever its milestone. |
| `autopilot.max_parallel` | `null` | How many issues in scope may have a NexKit run or an open NexKit pull request at once (1 to 100); `null` for no limit. |
| `autopilot.lost_after_minutes` | `30` | Start a command that autopilot started once more when it has posted no run comment and its workflow run ended, at once, or when that run cannot be read, after this many minutes (5 to 1440). A second such command waits for a person. A sweep notices a lost command: the one that a run `route` refused starts when that run ends, and the end of the run that replaced a waiting one in its concurrency group usually starts one too. Nothing notices a dispatch that names no run, as on older GitHub Enterprise Servers, before the first sweep after this time, which in a quiet repository is the hourly schedule. |
| `autopilot.answer` | `"never"` | `"recommended"` answers each question of a plan or a blocked round with the option NexKit recommends, when every question has one; `"never"` leaves every question to a person. |
| `notify` | `[]` | GitHub logins to mention when a run ends in a state that needs a person, such as `["owner"]`, each listed once. See [Mentions](#mentions). |

**Limits and safety**

| Key | Default | Meaning |
|---|---|---|
| `limits.setup_timeout_minutes` | `30` | Time each `setup` command may take (1 to 120). The agent and verify jobs allow for it. |
| `limits.max_patch_mb` | `5` | Largest change an implement or fix round may publish, in megabytes (1 to 50). |
| `protected_paths` | `[".github/", ".nexkit/"]` | Path prefixes the agent may not change. Both defaults are required; you may add more. |
| `transcript` | `true` | Write the agent's transcripts (`transcript.jsonl`, `transcript.md`) into the `nexkit-agent` and `nexkit-review` artifacts. See [Logs and transcripts](#logs-and-transcripts). |
| `log` | `{"tool_output": "truncated"}` | How the agent and review jobs print tool calls. |
| `publish.push_token_secret` | `null` | The secret with the token that pushes NexKit's branches and opens its pull requests, such as `"NEXKIT_PUSH_TOKEN"`; `null` pushes with the Actions token. It cannot be an agent's secret or start with `GITHUB_`. See [Secrets](#secrets). |

## Checks

```json
"checks": [
  {"name": "test", "run": "pytest -q", "timeout_minutes": 20},
  {"name": "lint", "run": "ruff check ."},
  {
    "name": "e2e",
    "run": "scripts/e2e.sh",
    "timeout_minutes": 40,
    "slow": true,
    "results": ["test-results", "artifacts/logs"]
  }
]
```

- `name`: short lowercase identifier, shown in reports. Two checks cannot share a name.
- `run`: a Bash command run from the repository root with `pipefail`.
- `timeout_minutes`: default 15, at most 360. Leave room to grow: once a check takes more
  than half its limit, raise the limit or make the suite faster, before rounds start to
  stop on it.
- `slow`: default `false`. See [Slow checks](#slow-checks).
- `results`: files or folders, relative to the repository root, where the check writes its
  results, such as traces, screenshots, test reports and logs. Default `[]`, at most 20.
  See [Result files of failing checks](#result-files-of-failing-checks).
- `group`: with `parallel_checks`, the job the check runs in, a short lowercase
  identifier; checks with the same `group` share a job. Without it, the check has a job of
  its own. See [Parallel checks](#parallel-checks).

`nexkit init` writes each check from `--check NAME=COMMAND`, and its other fields from
flags that name the check: `--check-timeout NAME=MINUTES`, `--check-slow NAME` and
`--check-results NAME=PATH`, repeated for each path. The example's `e2e` check comes from:

```sh
nexkit init --check "e2e=scripts/e2e.sh" --check-timeout e2e=40 --check-slow e2e   --check-results e2e=test-results --check-results e2e=artifacts/logs
```

Checks run in a separate job that has **no secrets and a read-only token**, on the exact
commit NexKit published, or with [`parallel_checks`](#parallel-checks) in several such jobs
at once. A setup failure counts as a failed check. With no checks
configured, NexKit can still run, but nothing verifies the change except the AI review.

### Slow checks

The implement and fix prompts list the checks and ask the agent to run them before it
finishes. A long suite, such as end-to-end tests, can use up the agent's time. Mark such a
check `"slow": true`:

- The prompts list slow checks apart, as checks NexKit runs after the round.
- The agent does not run a slow check in full. It runs only the tests in it that cover its
  change or that failed, as narrowly as the repository allows.
- Say in the repository's instructions (CLAUDE.md or AGENTS.md) how to run part of a slow
  check, for example `scripts/e2e.sh <spec>`.
- NexKit's checks after the round run every check in full, slow or not.
- `checks_run` in the agent's result still lists what the agent ran.

With no check marked slow, the agent runs every check, as before. The plan prompt lists
every check either way.

### Parallel checks

By default the `verify` job runs `setup`, then every check one after another, so a round
takes as long as all of its checks together. With `"parallel_checks": true`, the checks run
in several jobs at once, and a round takes about as long as its slowest job plus `setup`:

```json
"parallel_checks": true,
"checks": [
  {"name": "build", "run": "npm run build", "group": "fast"},
  {"name": "lint", "run": "npm run lint", "group": "fast"},
  {"name": "test", "run": "npm test", "group": "fast"},
  {"name": "integration", "run": "scripts/integration.sh", "timeout_minutes": 40},
  {"name": "e2e", "run": "scripts/e2e.sh", "timeout_minutes": 40, "slow": true}
]
```

- Checks with the same `group` run in one job, in the order of `checks`. Each check without
  a `group` runs in a job of its own, named after the check. The example has three jobs:
  `fast` (build, lint and test), `integration` and `e2e`. A group may take a check's name,
  and then that check joins it.
- Each job is a `verify` job, shown as `verify (<job>)`: it has no secrets and a read-only
  token, restores the [caches](#caches), runs `setup` and then its own checks, on the same
  published commit. Its time-out is `limits.setup_timeout_minutes` plus its own checks'
  `timeout_minutes`, plus 10 minutes.
- A `combine` job then joins the jobs' results, so the round's result is the same as with
  one job: one `nexkit/checks` status, one `checks.json` with every check in the order of
  `checks`, the failing checks' result files under `results/<check>/`, and the same fix
  prompt. It runs no repository code.
- A `setup` that fails in one job counts as a failed check, as with one job, and that
  job's checks do not run. A job that ended without a result, for example because its
  runner was lost, fails each of its checks, and the review does not run.
- Each job keeps its share of the round's 50 MB of [result
  files](#result-files-of-failing-checks): 25 MB each with two jobs.
- With `parallel_checks` off, or when every check would run in the same job, the single
  `verify` job runs as before. `group` is refused without `parallel_checks`.

Each job sets up the repository again, so parallel jobs use more runner minutes than one,
and are worth it when a few slow checks dominate the round. `nexkit init` writes the
setting from `--parallel-checks`, and each `group` from `--check-group NAME=GROUP`.

### Result files of failing checks

NexKit keeps only the end of a check's output: the last 12,000 characters in the round's
artifact and the last 6,000 in the fix prompt. A failing end-to-end suite often leaves its
real evidence in files instead. Name them in `results`, and the next fix round's agent
reads them before it runs anything, rather than running the slow suite again to see the
failure.

- When a check fails, the `verify` job copies the paths in its `results` that exist into
  the round's `nexkit-checks` artifact, under `results/<check>/<path>`. Paths that do not
  exist are skipped. A passing check keeps nothing, and neither does a check without
  `results`.
- Text files pass through the same redaction as the agent's output (see
  [What is redacted](how-it-works.md#what-is-redacted)). Binary files, such as zip files
  and images, are kept as they are: the `verify` job holds no secrets that could end up in
  them. Links are not followed, and a path that leads out of the repository is skipped.
- A round keeps at most 50 MB of result files, in the order of `checks`, then of each
  check's `results`, then of the file paths; with [parallel checks](#parallel-checks), each
  job keeps its share. Files that would go past the limit are left out, and the `verify`
  job's run summary lists them, with the paths that were not found.
  Playwright traces of one failed test are often 15 to 20 MB, so keep `results` to the
  folders that explain a failure.
- The next fix round's `agent` job downloads that artifact to a folder outside the working
  tree. The fix prompt names the folder and, for each failing check, its folder and each
  file or folder it kept. When the artifact cannot be downloaded, for example because it
  expired after 14 days, the round runs without the files and its log says why.
- When a kept file is a JUnit XML report (a `.xml` file whose root is `testsuites` or
  `testsuite`), the check also records the test cases that failed or erred in it, which
  autopilot compares with the default branch's failing tests (see *Failing checks the
  default branch has* under [How it works](how-it-works.md#stages)).
- Checks without `results` behave as before.

The artifact can be downloaded by anyone who can read the run, like the rest of NexKit's
logs and artifacts (see [What is redacted](how-it-works.md#what-is-redacted)).

### Evidence from CI runs

```json
"evidence": {"artifacts": ["test-results-e2e"]}
```

An issue about a failing or flaky CI run, such as a `bug` issue that autopilot opened, is
often answerable only from what that run left: a trace, a screenshot, the end of a log.
The agent session holds no GitHub token, so it cannot download them; NexKit does, before
the agent runs.

- **Which runs.** The runs of this repository that the issue names: those NexKit's own bug
  and flaky-run issues record in their markers, and links to Actions runs, attempts and
  jobs in the issue's description and in comments by the repository's owner, members and
  collaborators. Links in comments by bots never count, so the run logs that NexKit's own
  comments link take no place. Nor do runs of NexKit's own workflow, wherever they are
  linked. At most the five named last of the runs that count. Runs of NexKit's workflow,
  runs of other repositories, runs from a fork, and other links are skipped, and the log
  says which and why.
- **What.** For each run and each attempt that failed: the end of each failed job's log
  and its error and warning annotations. Then the run's artifacts that `artifacts` lists
  by name, as the repository's CI workflows upload them; NexKit's own artifacts are not
  among them. Without `artifacts`, only logs and annotations. For each run, also whether
  a newer run of the same workflow passed on the default branch since, and which: among
  the workflow's 20 latest runs there, without runs of pull requests and forks.
- **Where.** The plan, implement and fix rounds' `agent` job downloads them, with
  `actions: read` and `checks: read`, to a folder outside the working tree, one folder per
  run. Text files are redacted like the result files of failing checks; binary files are
  kept as they are. A round keeps at most 50 MB; what would go past it is left out. The
  plan agent, which may only read, may read that folder too.
- **The prompt** names the folder and, for each run, each file and artifact it holds and
  the newer run that passed since, and asks the agent to read them before anything else,
  so a plan for a CI issue starts from the cause the logs show rather than a guess. When
  a download fails, for example because an artifact expired, the round still runs: the
  prompt, the log of *Collect issue context* and the run summary say what is missing and
  why.
- **An outside failure that has passed.** When the evidence names a cause outside the
  repository, such as a download host or package registry that refused requests, or a
  lost runner, and a newer run of the workflow passed on the default branch since, a plan
  or implement round may find that no change is needed (`no_change`). The plan or the
  round's comment shows the finding and links the run that passed; see
  [Autopilot](#autopilot) for how the issue closes.

`nexkit init --evidence-artifact NAME`, repeated for each name, sets `artifacts`.

### Skipping work that a change cannot affect

Setup and checks get `NEXKIT_BASE_SHA`: the merge base of the commit being checked and
the base branch. The checkout has that commit, in public and private repositories alike.
List the paths the pull request changed with both of these commands:

```sh
{ git diff --name-only "$NEXKIT_BASE_SHA"; git ls-files --others --exclude-standard; }
```

The first lists changed tracked files, committed or not. The second lists new files that
are not committed yet: when the agent runs the checks while it works, the files it created
are still untracked, and without that line a check would skip work it should run. Changes
that the base branch made after the merge base are not listed. In a fix round that merges
the base branch, the variable is the merged base commit.

When NexKit cannot work out the base, it leaves `NEXKIT_BASE_SHA` unset and says so in
the job log. Outside NexKit it is always unset, so the rule for a check is:

- Skip work only when `NEXKIT_BASE_SHA` is set and none of the paths that work covers
  changed. Otherwise run it in full.
- Run every check in full on the base branch, for example in your own CI after a merge.

Setup and checks also get `NEXKIT_VERIFIED_SHA`: the commit of the latest earlier round
of the pull request whose checks all passed. Against `NEXKIT_BASE_SHA`, a suite that the
pull request touched once runs in every later round; against `NEXKIT_VERIFIED_SHA`, it runs
only when its paths changed since the last green round. List those paths the same way:

```sh
{ git diff --name-only "$NEXKIT_VERIFIED_SHA"; git ls-files --others --exclude-standard; }
```

- NexKit reads the commit only from its own round comments, never from agent output or
  anyone else's comments.
- It is unset, and the job log says why, when no earlier round passed (always in the
  implement round) or the checkout does not have that commit.
- After a fix round that merged the base branch, the diff also lists the base branch's
  changes, so a check stays on the safe side.
- A round passes when each check exits 0, including checks that skipped work. Skip only
  what the listed paths cannot affect: a wrong skip carries into every later round.

Prefer `NEXKIT_VERIFIED_SHA` and fall back to `NEXKIT_BASE_SHA`:

```sh
# Run the dev tooling smoke tests only when the change can affect them.
since="${NEXKIT_VERIFIED_SHA:-${NEXKIT_BASE_SHA:-}}"
changed() {
  [ -n "$since" ] || return 0  # unknown base: run everything
  files=$(git diff --name-only "$since" &&
          git ls-files --others --exclude-standard) || return 0
  grep -qE "$1" <<< "$files"
}
if changed '^(docker/|compose\.yaml$|scripts/dev)'; then
  ./scripts/smoke-dev.sh
else
  echo "Dev tooling unchanged since $since: smoke tests skipped."
fi
```

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

**Who saves.** A workflow of your repository that runs on the base branch saves each entry
under the same key, for example the CI that runs on pushes to the default branch, which
NexKit also starts after its merges with `merge.after_workflows`. Add one `actions/cache`
step per entry, after the checkout and before your setup:

```yaml
- uses: actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0
  with:
    path: |
      ~/.gradle/caches
      ~/.gradle/wrapper
    key: nexkit-${{ runner.os }}-gradle-${{ hashFiles('android/gradle/libs.versions.toml', 'android/gradle/wrapper/gradle-wrapper.properties') }}
    restore-keys: nexkit-${{ runner.os }}-gradle-
- run: scripts/setup.sh
```

`actions/cache` restores the entry and saves it at the end of a successful job when the
key had no cache. For NexKit to find what it saves:

- `path` lists exactly the `paths` of the entry, in the same order. The list is part of
  the cache's version, and a cache saved with another list is not found.
- `hashFiles` gets exactly the `key_files`, in the same order.
- The step comes before your setup, so the key files are hashed as committed, as NexKit
  hashes them.
- The job runs on `ubuntu-24.04` or `ubuntu-latest`, like NexKit.
- It runs on the base branch. Caches saved on other branches are not visible to NexKit.

**A miss is not a failure.** A missing cache, or a restore that fails, never fails a run.
The *Restore caches* step in the job log lists each entry as restored, restored from an
older key, or missed, with the key it looked for.

## Automatic merge

```json
"merge": {"auto": true, "after_workflows": ["ci.yml"]}
```

With `merge.auto: false` (the default) a person decides every merge. With `true`, the
`report` job squash-merges a NexKit pull request as soon as every check passes and the AI
review approves. It merges with the default Actions token; no extra secret is needed. The
pull request description says which of the two applies.

The review approves only when no acceptance criterion is unmet. When it could not verify a
criterion from the diff, its tests and the check results, NexKit does not merge: the
round's comment lists the criteria for a person to confirm before merging by hand, and
`notify` mentions its people. See [How it works](how-it-works.md#stages).

After the merge, NexKit closes the issue the pull request implements as completed and
comments a link to the pull request and the merge commit on it. GitHub does this itself
when a person merges, but not after a merge with the Actions token. A pull request whose
issue a person closed meanwhile is not merged: the round's comment says so, and the pull
request waits for a person to merge or close it. If closing fails, the round's comment
says so and links the issue; the merge stays.

NexKit follows the repository's *Automatically delete head branches* setting
(`delete_branch_on_merge`, under **Settings → General → Pull Requests**) the same way:
when it is on, NexKit deletes the merged pull request's `nexkit/issue-<number>` branch,
which GitHub deletes only after a person's merge. A branch that another open pull request
targets stays, and a delete that fails leaves the merge as it is and is named in the
round's comment. With the setting off, NexKit deletes no branch.

Branch protection and rulesets still apply, and the Actions token cannot bypass them. When
a rule is not met, GitHub refuses the merge, the round's comment says why, and the pull
request waits for a person. Before merging, NexKit sets the `nexkit/checks` and
`nexkit/review` statuses, so you can require those two. Rules that block every automatic
merge:

- a required approval from a person;
- a required check from your own CI, unless `publish.push_token_secret` names a push
  token, because without one your CI does not run on NexKit pull requests (see
  [Troubleshooting](troubleshooting.md));
- a required merge queue, or squash merging turned off for the repository.

GitHub's *Require branches to be up to date before merging* keeps two pull requests that
pass alone from breaking the base branch together. With `conflicts.auto_resolve`, NexKit
then brings a pull request that is behind up to date with its base branch and runs its
checks again. When that merge had no conflict and the pull request's own change is the one
the review approved, the approval carries over and no review agent runs, so the rule costs
a run of the checks, not a second AI review (see *A pull request brought up to date* under
[How it works](how-it-works.md#stages)).

`merge.after_workflows` lists workflow files in `.github/workflows/`. GitHub starts no
workflow for a push made with the Actions token, so the merge by NexKit does not run your
`push` CI on the base branch. Two pull requests that pass on their own can still break the
base branch together. After each merge, NexKit starts every listed workflow on the base
branch with `workflow_dispatch`:

- Each workflow needs a `workflow_dispatch` trigger without required inputs, for example
  `on: {push: {branches: [main]}, pull_request: {}, workflow_dispatch: {}}`.
  `nexkit init --after-workflow FILE`, repeated for each file, sets the list.
- With autopilot, each workflow also ends with a job that starts a sweep, so a failure is
  noticed within minutes; see *When a finished run is noticed* under
  [Autopilot](#autopilot).
  `nexkit doctor` warns when a listed file is missing or has no such trigger.
- The run uses the base branch's newest commit when it starts, normally the merge commit.
  Its event is `workflow_dispatch`, so steps limited to `push` events are skipped.
- The round's comment links each workflow it started, or says why a start failed. A
  failed start does not undo the merge.
- Nothing is started when `merge.auto` is `false` or the merge was refused.

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

## Autopilot

```json
"autopilot": {
  "enabled": true,
  "scope": "milestone",
  "max_parallel": null,
  "answer": "never",
  "lost_after_minutes": 30
}
```

With autopilot on, nobody needs to post `/nexkit plan` and `/nexkit go`. NexKit looks at
every open issue in scope, in order, and starts the one command that moves it on:

| The issue | Autopilot |
|---|---|
| Ready, with no plan yet | Starts `/nexkit plan`. |
| Its latest plan has no questions and is not too large | Starts `/nexkit go`. |
| Its latest plan's change needs protected paths (`protected_paths` in the plan) | Nothing: the plan comment says a person makes that change by hand, and `notify` is mentioned. `/nexkit go` would only stop when NexKit rejects the change. |
| Its latest plan finds that no change is needed (`no_change` in the plan), such as an outside failure that has passed since | Closes the issue as completed, with a comment that holds the plan's finding and the run that passed since. |
| Collaborators commented after the latest plan | Starts `/nexkit plan` again, so the plan holds their changes before `/nexkit go`. |
| Its plan is too large and suggests a split | Creates each part as a sub-issue, in order, with the issue's milestone and labels, marks each part *blocked by* the earlier parts it needs, closes the issue's open NexKit pull request as superseded, and plans the parts that need no other part. |
| Its plan has questions, and every question has a recommended option | With `answer: "recommended"`: posts the answers for the owner and starts `/nexkit plan` again. |
| Its last `/nexkit go` or fix round was blocked, and every question has a recommended option | With `answer: "recommended"`: posts the answers and starts `/nexkit go` or `/nexkit fix` with them as the note. |
| The plan or round after autopilot's answers asks again, and no person commented since | Nothing: a person decides, and `notify` mentions them. A comment by a person lets autopilot answer again. |
| A blocked `/nexkit go` found that no change is needed and recommends closing the issue | Closes the issue as completed with the round's finding, with `answer: "recommended"`; no other round runs. |
| Its last `/nexkit go` or fix round was blocked because its change needs protected paths | Nothing, whatever it recommends: a person makes that change, and `notify` is mentioned. |
| Its plan's parts are titled like the issue | Creates no parts, and starts `/nexkit plan` once more with a note that names those titles. |
| Its plan is too large and suggests no parts | Starts `/nexkit plan` once more, with a note that asks for parts that each fit one session. A second such plan waits for a person. |
| Its last `/nexkit go` stopped as blocked or failed, not for running out of time, and something changed since that round started: a collaborator commented, other than with a command, or an issue that blocks it closed, such as one a person marked as its blocker after the round asked to wait for it | Starts `/nexkit plan`, so the plan holds what changed, then goes on as after any plan. While nothing changed since the round started, the issue waits for a person. A blocker that closed before the round started changes nothing. |
| Its last `/nexkit go` or fix round ran out of time | Starts `/nexkit plan` once, with a note to split the issue into parts that each fit one session, and to say why the round did not finish. When that plan does not split the issue and needs nobody, starts `/nexkit go`, or `/nexkit fix` on its pull request. |
| A parent issue whose sub-issues are all closed | With `close_parent_issues`, NexKit closed it already; without, it waits for a person to close it. |
| A bug issue that autopilot opened for a CI failure on the default branch | Goes first: it is planned before every other issue in scope. |
| An issue that autopilot opened for a failure of flaky runs on the default branch | Goes right after those bug issues. |
| Anything else that needs a person: other questions, a failed run that nothing answered since, a refused merge, a second timeout, a part that runs out of time after an issue above it asked for a split, a second plan with parts titled like the issue, a second plan that is too large and suggests no parts, an issue three levels of parts deep, a pull request that needs a person, a blocker outside the scope | Nothing: a person decides, as without autopilot, and `notify` mentions them once. `nexkit status` lists it under *Needs a person*. Other issues go on, and it does not count toward `max_parallel`. |
| A run on it is running or paused, or its pull request is open and NexKit carries it on | Nothing: the run, or the pull request's automatic rounds, carry it on. |
| Its pull request is approved, and GitHub refused the merge until required checks pass | Tries the merge again, and merges once the checks pass. A required check that fails gets an automatic fix round within `fix.max_auto_rounds`, unless it fails as the default branch does while its bug issue is open; a new commit gets `/nexkit review`. Checks that neither pass nor fail within six hours wait for a person. |
| Its pull request waits for a bug issue about the default branch, whose failure its checks repeat | Nothing while that workflow fails on the default branch and the bug issue is open; then starts the automatic fix round the pull request waited for. It keeps its place within `max_parallel`. |
| The last issue in scope closed | Mentions `notify` once, on the milestone's most recently closed issue, that the milestone is done, and asks a person to close it. With `null`, the same for each open milestone whose issues are all closed, without asking, while the other milestones go on. Any sweep says it, whichever event started it. |

- **Scope.** `"milestone"` takes the open issues of the earliest open milestone: by due
  date, then the oldest, skipping milestones with no issues at all. When all its issues are
  closed, autopilot stops there until a person closes the milestone, so it never starts
  the next milestone by itself. `null` takes every open issue, in every milestone and
  without one, so the *blocked by* relationships alone decide what starts, across
  milestones; to hold an issue back, do not open it yet. Issues outside the scope are
  never touched.
- **Changing the scope.** Work under way is not stopped. A run that is running ends as
  usual, and an open pull request keeps its automatic fix and conflict rounds, which do not
  depend on the scope. Three things then wait for a person: an approved pull request whose
  issue left the scope and whose merge waits for checks, because only sweeps retry that
  merge and they look at the scope alone; a pull request whose issue left the scope while
  it waits for a `bug` issue about the default branch (see *CI on the default branch*),
  because only sweeps end that wait; and an issue in scope blocked by an open issue that
  left it. Issues that left the scope and have not started stay open and wait until
  they are in scope again.
- **Order.** Bug issues for CI failures go first and issues for flaky runs next. Then,
  with `null`, come the issues of each open milestone, in the milestone scope's order,
  then the issues without one; each group oldest first. With `max_parallel`, an earlier
  milestone's issues start first.
- **Ready.** An issue is ready when no issue that blocks it (GitHub's *blocked by*
  relationships) is open. The order of sub-issues does not count, so sub-issues without
  such relationships start at the same time; text such as "Depends on #12" is not read. A
  parent issue with sub-issues is never planned itself: its sub-issues are. Mark an issue
  *blocked by* another only for a real prerequisite, such as something new that the other
  issue adds: a mark that is not needed only slows the work, and touching the same files
  is no reason, since NexKit resolves conflicts. The [`request`
  skill](../skills/request/SKILL.md) has the rules.
- **Trust.** Autopilot skips a person's approval of the plan, so the issue text drives an
  implement session, which can read the secrets of its job. It takes only issues opened by
  someone with write access to the repository; only they and other writers can edit them.
  `nexkit status` shows the issues it leaves alone and why.
- **At most `max_parallel` at once.** An issue counts while a run on it is running or
  paused, while its pull request is open and needs nobody, and while a command that
  autopilot started has not run yet. An issue that waits for a person does not count. With `null`, every ready issue starts at once; NexKit's conflict rounds
  (see [Conflicts with the base branch](#conflicts-with-the-base-branch)) keep the pull
  requests mergeable.
- **One command per step.** Autopilot keeps one comment on each issue that names the
  command it started last, with its note, and for which run. It starts a command once for
  a state of the issue. A command that has posted no run comment after
  `lost_after_minutes`, while its workflow run is neither queued nor running, because
  `route` refused it or GitHub dropped it from the queue, starts once more with the same
  note; if that one does not run either, the issue waits for a person. The first sweep
  after that time notices it, at the end of any NexKit run or on the hourly schedule. A
  run whose comment still says running after its workflow run ended, for example because
  the runner was lost, is marked as ended without a report and its pending statuses
  become errors, within minutes: the run's `finish` job starts a sweep that waits for the
  run to end. When NexKit
  started that run itself, autopilot starts it once more; a person's command, or a second
  such run, waits for a person. A run paused at the usage limit that NexKit will not
  resume, and a continued run whose new run has not started after six hours, wait for a
  person too.

- **Answers.** Plans, and implement and fix rounds that stop as `blocked`, give each
  question its options and the one NexKit recommends, if any; the plan comment marks it
  `(recommended)`. With `answer: "recommended"`, autopilot answers only when every
  question has a recommendation, in a NexKit comment that the agents read as the owner's
  discussion. Comments by other bots never count. When autopilot takes over like this, or
  splits an issue, `notify` is not mentioned. Autopilot answers once in a row: when the
  plan or round that its answers started asks again, and no person commented on the issue
  or its pull request since, it waits for a person, whatever the new questions are. The
  prompts never recommend an option that needs a person to act outside NexKit, such as
  downloading files or granting access, so such a question waits for a person at once.
  The code does not rest on that alone: a plan or blocked round whose change needs
  protected paths names them (`protected_paths`), only paths that really are protected
  count, and autopilot then answers nothing and starts no `/nexkit go`.
  When a blocked `/nexkit go` found that no change is needed (its `no_change` finding)
  and its one question recommends closing the issue, autopilot starts no other round: it closes the issue
  as completed with a comment that holds the finding.
- **CI on the default branch.** With workflows in `merge.after_workflows`, each sweep also
  looks at the runs of each of them on the default branch, after NexKit's merges and
  people's alike, and at every attempt of each run; runs of pull requests, even from a
  fork's branch of the same name, do not count. When the latest run fails, its failed
  jobs run once more, never more often. When it fails again, autopilot opens a `bug` issue
  in scope (in the current milestone, or without one with `null`), with the run link and the failed jobs and steps of every failed attempt,
  mentions `notify` in it, and plans it before every other issue. A later failure on a
  newer commit adds a comment to that issue while it is open; a commit is reported once.
  When a later attempt of that run passes, for example because a person ran it again, a
  comment on the issue says which attempt passed. While that issue is open, a pull request
  into the default branch whose failing checks all fail as the workflow's latest failed
  attempt did, or whose required checks do when NexKit merges it, gets no
  automatic fix round: it waits, using none of
  `fix.max_auto_rounds`, and `nexkit status` shows it as `waiting` for that issue. Once
  the workflow passes on the default branch again, or the issue is closed, a sweep starts
  its automatic fix round, which merges the default branch first. A check fails the same
  way when the first line of its `run` is the command of a failed step, so this works
  when your CI runs the same commands as NexKit's checks, such as `scripts/test.sh` in
  both, and, when both sides name failing tests, when every test failing in the check
  failed there too. The check names them from the JUnit XML files in its `results`, the
  workflow from its failed jobs' error annotations, so with a test reporter that writes
  JUnit files and annotates failures, a pull request that breaks another test in the same
  script gets its fix round at once. See *Failing checks the default branch has* under
  [How it works](how-it-works.md#stages).
- **Flaky runs.** A run that passed only when run again, and an older run that failed when
  a newer run had already started, are recorded under each of their failures, on one
  `bug` issue per failure, which mentions `notify` and is planned right after the `bug`
  issues above. An older run is never run again. The error annotations of the failed jobs
  name the failures, such as each failing test, by title or else by file and line; the
  runner's own "Process completed with exit code" names nothing. A job without such an
  annotation is named by its first failed step and the message of its first error
  annotation, such as a download host's refusal, not by the job: the same step failing
  the same way in several jobs, or in several workflows of `after_workflows`, is one
  failure with one issue, whose records list each job and workflow. An issue opened by
  NexKit 5.0 or earlier for one job's step keeps getting that job's records. Each record
  names the commit, the run, and where the failure showed in each failed attempt. While a
  failure's issue is open, its later records are comments on it; once closed, a new issue
  opens that links it. A run whose failures would open more than five issues records
  those on one issue for the run. Each run and attempt is recorded once per failure; an
  issue from NexKit 4.3.0, with every failure of a workflow on it, gets no new records.
  Sweeps look at the runs of the last day, including the first sweep after an upgrade,
  and at the runs a `bug` issue reports. To know what is recorded, a sweep reads every
  open issue NexKit opened and every one updated in the last day, however many other
  issues and pull requests NexKit opened since. Reading annotations needs `checks: read` in the workflow's
  permissions, as `nexkit init` writes them.
- **When a finished run is noticed.** Within minutes, in two ways that together cover
  every run. First, a `workflow_run` trigger that names each workflow of
  `merge.after_workflows` by its `name:` (or its path, without one). `nexkit init
  --after-workflow FILE` writes it with those names; without that flag it writes the
  trigger as a comment. `nexkit doctor` says when one is missing:

  ```yaml
  on:
    workflow_run:
      workflows: [CI]
      types: [completed]
      branches: [main]
  ```

  The `nexkit` job's `if` must let `"workflow_run"` through, as `nexkit init` writes it.
  A run that passed at once starts no sweep, unless a `bug` issue about its workflow is
  open. GitHub sends no such event for a run that the Actions token started, such as one
  NexKit starts after its merge, but the Actions token may start a workflow with
  `workflow_dispatch`. So, second, each workflow of `merge.after_workflows` ends with a
  job that starts a sweep. It needs every other job of the workflow, runs on the default
  branch whatever they concluded, and names the run, so the sweep waits for the run to
  end before it looks:

  ```yaml
  jobs:
    # ... the workflow's own jobs, here test and lint ...
    nexkit-sweep:
      # NexKit autopilot looks at this run as soon as it ends (docs/configuration.md).
      needs: [test, lint]
      if: always() && github.ref_name == github.event.repository.default_branch
      runs-on: ubuntu-24.04
      permissions:
        actions: write
      steps:
        - run: >-
            gh workflow run nexkit.yml --repo "$GITHUB_REPOSITORY" --ref "$GITHUB_REF_NAME"
            -f command=autopilot -f number=autopilot -f note="after run $GITHUB_RUN_ID"
          env:
            GH_TOKEN: ${{ github.token }}
  ```

  `nexkit init --after-workflow FILE` prints this job with that workflow's jobs in
  `needs`; NexKit never edits your workflows. `nexkit doctor` says when a workflow has no
  such job, or one that does not need every other job or lacks `always()`, and says when
  it cannot tell, for a workflow that calls another one. Without either, the end of the
  next NexKit run notices a finished run, also a run that started the next automatic
  round itself, and else the hourly schedule.
- **Titles of CI issues.** An issue for a CI failure says what fails, where, and
  "fails at times on" the branch for a flaky failure, or "fails on" it when the branch is
  red, in plain words. Double quotes mark a test, job or step name; there are no
  backticks, and a name is never shortened. A test is named by the last segment of its
  annotation's title, after the last ` › ` or ` > `, and its file name. A title is cut only
  past GitHub's 256 characters, at a word boundary. The body keeps the full name, file and
  line. A red branch's title names the failures of its latest attempt: one, or up to three
  of one kind, else how many.

  | Failure | Title |
  |---|---|
  | A test | Test "a customer pays for an order and the shop shows the receipt" in order-checkout.spec.ts fails at times on main |
  | A Vitest test | Test "pages through more than twenty orders" in OrderHistory.test.tsx fails at times on main |
  | An annotation with only a file and line | Line 12 of src/orders.ts fails at times on main |
  | A job, or a step named like its job | Job "Integration tests" in ci.yml fails at times on main |
  | A step | Step "Unit tests" of job "Lint and unit tests" in ci.yml fails at times on main |
  | A step that failed the same way in several jobs | Step "Run actions/setup-dotnet@v5" in 3 jobs of ci.yml fails at times on main |
  | A run that failed before any job | ci.yml does not start at times on main |
  | A run with more than five new failures | Run 123 of ci.yml failed in 7 ways at once on main |
  | A red branch | Steps "Lint" and "Unit tests" in ci.yml fail on main |
  | A red branch, failures of several kinds or more than three | ci.yml fails in 4 ways on main |
  | A red branch, before any job | ci.yml does not start on main |

  The marker, not the title, ties a failure to its issue, so an issue opened with an
  older title keeps getting its records.
- **Parts.** Parts that autopilot creates are trusted because they start with a NexKit
  marker and are sub-issues of an issue it trusts. A part's own plan that is too large is
  split again, within these limits:
  - A part inherits the time-outs of the issues above it. When one of them already asked
    for a split, a part that runs out of time waits for a person instead.
  - A part titled like its parent, or starting with its parent's title, is not created.
    Autopilot asks for the plan once more, and then waits for a person.
  - Parts go at most three levels deep: an issue at level 3 (its parent's parent's parent
    was the issue first split) is not split again. This limit is not a setting.

  After a time-out, the plan says why the round did not finish. For a failure whose cause
  was not found, its first part is a diagnosis that finds the cause before the change.
  With profiles, triage chooses that part's profile as for any other issue.

Commands that autopilot starts are dispatches, shown as `/nexkit plan (autopilot)`. A
person can still post any command, and autopilot carries on from what it leads to.
Autopilot pairs well with `merge.auto`, `conflicts.auto_resolve`, `close_parent_issues` and
`notify`; without `merge.auto`, an issue moves on when a person merges its pull request.

Sweeps, the runs that decide this, start after every NexKit run, when an issue opens,
closes or is reopened, when an issue gets a milestone or a milestone closes (only for the
`"milestone"` scope), when a run of a workflow in `merge.after_workflows` finishes on the
default branch, and on the hourly schedule, which catches anything the others
missed. They run one at a time, run no agent
and use no agent credential. The workflow must listen to those events, as `nexkit init`
writes it, and to `workflow_run` as *When a finished run is noticed* shows; `nexkit
doctor` warns when it does not. To start a sweep yourself, for example
right after turning autopilot on:

```sh
gh workflow run nexkit.yml -f command=autopilot -f number=autopilot
```

See [How it works](how-it-works.md#autopilot).

## Mentions

```json
"notify": ["owner"]
```

NexKit mentions the listed people when a run ends in a state that needs a person, so they
get a GitHub notification without watching every run. Right after the run's comment, it
posts one line such as `@owner a person is needed: the plan has questions.` on the issue,
or on the pull request once there is one. The line is its own comment because GitHub
notifies people mentioned in a new comment, but not in an edited one, and the run's
comment is edited when the run ends.

The choice follows the run's outcome only, never a judgment by the agent (see
[How it works](how-it-works.md#when-a-person-is-needed)). A run mentions at most once;
nobody outside `notify` is ever mentioned. Write logins without `@`. `nexkit doctor`
warns about a listed login without write access, but NexKit still mentions it. With
`[]`, nothing is posted. With autopilot, an issue that starts to wait for a person without
a run that mentioned anyone, such as one whose command never ran, is mentioned by the next
sweep, once for its latest run. Autopilot runs unattended, so `nexkit doctor` says when it
is on and `notify` is empty; `nexkit init --notify LOGIN`, repeated for each login, sets
it.

## Conflicts with the base branch

```json
"conflicts": {"auto_resolve": true, "max_rounds": 3}
```

With `auto_resolve: false` (the default), a person comments `/nexkit fix` on a pull
request that conflicts with its base branch. With `true`, NexKit keeps its open pull requests
mergeable: after the base branch moves, each one that now conflicts gets an automatic fix
round that merges the base branch and keeps both sides. This matters most with
`merge.auto` and several issues at once, where every merge can make the other pull
requests conflict.

- NexKit's own merges, pushes to the default branch (through the `push` trigger that
  `nexkit init` writes), the end of every NexKit run and the hourly schedule start the
  check. A round that ran while the base branch moved holds its conflict round back, and
  the sweep at its end starts it, also without autopilot. `nexkit doctor` warns when the
  `push` trigger is missing; the end of the next run, or the schedule within an hour,
  still notices.
- A pull request whose mergeability GitHub has not worked out yet is checked again by
  the next sweep.
- When an automatic merge is refused because of a conflict, the round that tried it
  starts a conflict round right away, within the same limits.
- When it is refused because branch protection wants the branch up to date, NexKit
  merges the base branch into it on GitHub and runs its checks again, within the same
  limits. A refusal that says required checks are "expected" on a pull request GitHub
  reports as behind counts the same, also while the merge waits for them. The AI review runs again only when the pull request's own change differs from
  the one it approved (see [Automatic merge](#automatic-merge)).
- A conflict that needs a choice ends the round as `blocked` with its questions; a person
  answers with `/nexkit fix <decisions>`.
- Each pull request gets at most `max_rounds` of these rounds (default 3), apart from
  `fix.max_auto_rounds`, and one per base commit until another round of the pull request
  ran.

See [How it works](how-it-works.md#work-without-a-command).

## Usage limits

```json
"usage_limit": {"resume": true, "retry_minutes": 60, "wait_minutes": 330}
```

When an agent stops because its account hit a usage limit that resets, the stage moves to
the agent's [fallback agents](#fallback-agents), if it has any. When none is left, NexKit
pauses the run, shows the reset time in its comment and keeps `nexkit/checks` and
`nexkit/review` pending. With `resume: true` (the default), it runs the same command again
after the reset, once, with the same note; when the reset time is unknown, after
`retry_minutes` (default 60). With `false`, the comment still names the limit and the
reset time, and a person comments the command again. Each runtime says whether it reports
such limits; `claude-code` does (see [How it works](how-it-works.md#usage-limits)).

When the reset is at most `wait_minutes` away (330, five and a half hours, by default),
NexKit starts a run that waits for it and resumes the command within ten minutes of the
reset. The run uses no agent and no repository code, but it holds a runner while it waits:
free on public repositories, billed minutes on private ones. Set `wait_minutes` lower, or
to 0, to spend fewer minutes; the hourly schedule then resumes the command.

The schedule also covers resets further away, such as a weekly limit: once one comes
within `wait_minutes`, the schedule starts the waiting run. It needs the hourly `schedule`
trigger that `nexkit init` writes; `nexkit doctor` warns when it is missing. GitHub runs
scheduled workflows when it can, so in practice a scheduled run can come hours late. A
schedule run with nothing to resume runs only `route` and `maintain`, which take well under
a minute and run no agent. To check less often, change the `cron` line, for example to
`"23 */3 * * *"`.

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

| Runtime | Providers | `effort` | `max_budget_usd` |
|---|---|---|---|
| `claude-code` | `anthropic` | `low`, `medium`, `high`, `xhigh`, `max` | yes |

Every runtime reports the tokens of its runs in the same form, which the run summary and
the run comments show; see [Logs, summary and
artifacts](how-it-works.md#logs-summary-and-artifacts).

| Provider | Settings | Secret |
|---|---|---|
| `anthropic` | none | Exactly one of `oauth_token_secret`, the secret that holds a Claude subscription token from `claude setup-token`, and `api_key_secret`, the secret that holds an Anthropic API key. Claude Code receives it under its own variable, whatever the secret's name. |

`claude-code` reports usage limits, so its runs pause at one or move to a fallback; see
[Usage limits](#usage-limits).

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
  [Usage limits](#usage-limits)).
- Nothing else moves to a fallback: an error, a timeout or a `blocked` result ends the
  stage as it does without one.
- Before implement or fix starts again, NexKit returns the checkout to where the first
  agent started: the setup baseline, and in a fix round the merge of the base branch with
  its conflicts. What the agent at its limit changed is dropped.
- Fallback agents use the same runtime as the agent, because the stage's `model` and
  `effort` are meant for it. Only the stage's agent's own list counts: when `claude-api`
  has a `fallback` too, it applies to the stages that choose `claude-api`.
- `timeout_minutes` and `max_budget_usd` apply to each agent's run, and the job allows
  for every agent of the list, up to 340 minutes for a stage. The stage's tokens add up
  every agent's run. An API key is billed per token, so set `max_budget_usd` if a
  fallback should not spend more than a limit.
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
`NEXKIT_`, which belong to GitHub and NexKit (the push token, such as `NEXKIT_PUSH_TOKEN`,
is NexKit's own and may use the prefix). A value that is not a secret name, such as a
key pasted into the configuration, is rejected. `nexkit doctor` checks that each secret is
set and that the repository's NexKit workflow passes it.

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
| `agent` | Overrides the top-level `agent`. Needs `model` next to it. |
| `model` | Overrides the top-level `model`. |
| `effort` | Overrides the top-level `effort`. |
| `timeout_minutes` | Stop the agent after this long (plan 15, implement 45, review 20 and triage 5 by default; triage at most 30, other stages at most 340). With [fallback agents](#fallback-agents), each agent's run gets this long. |
| `max_budget_usd` | A spending limit for each agent's run, for runtimes that have one (Claude Code's `--max-budget-usd`); `null` means no limit. |

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
- `stages.triage` sets the agent, model, effort, timeout and budget of the triage call. It
  takes the top-level `agent`, `model` and `effort` unless it sets its own. A profile
  cannot set it, because triage runs before there is a profile.
- `default_profile` is used when no plan chose a profile: `/nexkit go` without a plan, a
  plan from before profiles were configured, and a plan whose triage failed. The cheapest
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

`.github/workflows/nexkit.yml` passes the pipeline `secrets: inherit`, as `nexkit init`
writes it, so the pipeline can read each agent's secrets by the names in
`.nexkit/config.json`. It passes a job only the secrets of the agents its stages use, at
most 8 per job, and each agent's CLI only its own; setup and check commands get none. See
[How it works](how-it-works.md#agents) for what this does and does not isolate. An older
workflow that lists secrets one by one can pass only `CLAUDE_CODE_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY` and `NEXKIT_PUSH_TOKEN`; `nexkit doctor` says when an agent or the push
token needs `secrets: inherit`.

| Secret | Required | Used by |
|---|---|---|
| The names an agent's `*_secret` keys give | for each agent a stage uses; a fallback agent without its secret is skipped | Agent and review jobs, only for the stages that run on that agent. For `anthropic`: the subscription token (`claude setup-token`, Claude Pro or Max) or the API key. |
| The name `publish.push_token_secret` gives, such as `NEXKIT_PUSH_TOKEN` | no | Publish job only, never a job that runs an agent, and never an agent's secret: pushes the branch and opens the pull request, so the repository's own CI workflows also run. A fine-grained token with Contents and Pull requests write access. Add Workflows write access if fix rounds should merge base branch changes to `.github/workflows/`; the default Actions token cannot push those. With `autopilot.enabled` or `conflicts.auto_resolve` and no push token, `nexkit doctor` says that such a round waits for a person; it still passes, since running without a push token is a choice. |

## Workflow file

`nexkit init` writes `.github/workflows/nexkit.yml`, which calls
`phuongnse/nexkit/.github/workflows/pipeline.yml` at a fixed release tag. It starts NexKit
for `/nexkit` comments, *Request changes* reviews on NexKit pull requests, dispatches,
closed issues (for `close_parent_issues` and `autopilot`), opened, reopened and
milestoned issues and closed milestones (for `autopilot`; the `null` scope needs no
milestone events), pushes to the default branch
(for `conflicts.auto_resolve`) and an hourly schedule (for `usage_limit.resume`,
`conflicts.auto_resolve` and `autopilot`). With those settings off, these events start
short runs that end at once; remove the triggers you do not use if you prefer. Issue and
milestone events other than a closed issue join the concurrency group
`nexkit-autopilot`, so they never replace a command that waits for its issue. Each starts
a sweep, even a milestone event with the `null` scope, since its run may have replaced a
sweep that waited. To upgrade, change both the
`uses:` ref and `nexkit_ref` to the new tag (or rerun `nexkit init --force --kit-ref
vX.Y.Z` and restore your config); see [the upgrade steps](#upgrading-from-earlier-releases). `nexkit
doctor` reports a problem when the two refs differ, and mentions a newer release without
failing: staying on a release is your choice.

NexKit's pull requests come from branches named `nexkit/issue-<number>`. The job's `if:`
in the workflow file lets a *Request changes* review start a run only on a branch that
starts with `nexkit/`.

## Upgrading from earlier releases

The workflow file and `.nexkit/` are protected paths, so a person makes an upgrade, in one
pull request:

1. Read the notes and the changelog of every release after the pinned one, not only the
   latest. Each release's notes list its own upgrade steps.
2. Pin both the `uses:` ref and `nexkit_ref` in the workflow file to the new tag.
3. Compare this reference's key tables and sections at the old and the new ref. Set every
   new key, or new value of a key, that the repository has a value for, such as the
   names of its own CI workflows or artifacts.
4. Check that the workflow file grants every permission the release asks for.
5. Run `nexkit doctor` at the new release, and fix every ✗ line.

A configuration from an earlier release that no longer fits is rejected with what to
change.

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
`oauth_token_secret`. If you set a `NEXKIT_PUSH_TOKEN` secret, also add
`"publish": {"push_token_secret": "NEXKIT_PUSH_TOKEN"}`: NexKit uses a push token only
when the configuration names it, and `nexkit doctor` warns about one that it does not.

### Keys renamed in 2.0

NexKit 1.10 and earlier had three of these keys at the top level. A configuration that
still uses them is rejected with the new name, so move them:

| Before | Now |
|---|---|
| `"max_auto_fixes": 2` | `"fix": {"max_auto_rounds": 2}` |
| `"auto_merge": true` | `"merge": {"auto": true}` |
| `"after_merge_workflows": ["ci.yml"]` | `"merge": {"after_workflows": ["ci.yml"]}` |
