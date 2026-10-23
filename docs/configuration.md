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
  "cache": [],
  "stages": {},
  "profiles": {},
  "default_profile": null,
  "fix": {"max_auto_rounds": 2},
  "merge": {"auto": false, "after_workflows": []},
  "conflicts": {"auto_resolve": false, "max_rounds": 3},
  "usage_limit": {"resume": true, "retry_minutes": 60, "wait_minutes": 330},
  "close_parent_issues": false,
  "autopilot": {"enabled": false, "scope": "milestone", "label": null, "max_parallel": null, "answer": "never"},
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
| `cache` | `[]` | Caches to restore before `setup`, which the base branch's own workflow saves. See [Caches](#caches). |
| `stages` | see [Stages](#stages) | Per-stage agent, model, effort, timeout and budget. |
| `profiles` | `{}` | Stage settings for kinds of issues, chosen for each issue. See [Profiles](#profiles). |
| `default_profile` | `null` | The profile to use when no plan chose one. Required with `profiles`. |

**What NexKit does by itself**

| Key | Default | Meaning |
|---|---|---|
| `fix.max_auto_rounds` | `2` | Repair rounds NexKit may start by itself on one pull request after failing checks or blocking findings (0 to 10). Rounds requested by people do not count. After failing checks, such a round first merges a base branch that has moved; see [How it works](how-it-works.md#failing-checks-the-base-branch-fixed). After a check failed the same way twice in a row, the next one is a [diagnosis round](how-it-works.md#failure-handling). |
| `merge.auto` | `false` | Merge (squash) when every check passes and the AI review approves. Branch protection still applies. See [Automatic merge](#automatic-merge). |
| `merge.after_workflows` | `[]` | Workflow files, such as `["ci.yml"]`, that NexKit starts on the base branch after it merges. File names only, each listed once. |
| `conflicts.auto_resolve` | `false` | Start a fix round by itself when a NexKit pull request conflicts with its base branch. See [Conflicts with the base branch](#conflicts-with-the-base-branch). |
| `conflicts.max_rounds` | `3` | Those rounds per pull request (1 to 10), apart from `fix.max_auto_rounds`. |
| `usage_limit.resume` | `true` | When the agent stops at the account's usage limit, run the same command again after the limit resets. See [Usage limits](#usage-limits). |
| `usage_limit.retry_minutes` | `60` | When the reset time is unknown, try again after this many minutes (1 to 1440). |
| `usage_limit.wait_minutes` | `330` | Start a run that waits for the reset when it is at most this many minutes away (0 to 330); 0 leaves the resume to the hourly schedule. |
| `close_parent_issues` | `false` | Close a parent issue when its last open sub-issue closes and at least one sub-issue was completed. See [Parent issues](#parent-issues). |
| `autopilot.enabled` | `false` | Work through the open issues in scope without commands: plan each ready issue, start `/nexkit go` on a plan that needs nobody, and move on when an issue closes. See [Autopilot](#autopilot). |
| `autopilot.scope` | `"milestone"` | Which issues autopilot takes: `"milestone"`, the open issues of the earliest open milestone, or `"label"`, the open issues with `autopilot.label`. |
| `autopilot.label` | `null` | The label of the issues to take, required with the `"label"` scope. |
| `autopilot.max_parallel` | `null` | How many issues in scope may have a NexKit run or an open NexKit pull request at once (1 to 100); `null` for no limit. |
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
- `timeout_minutes`: default 15, at most 360.
- `slow`: default `false`. See [Slow checks](#slow-checks).
- `results`: files or folders, relative to the repository root, where the check writes its
  results, such as traces, screenshots, test reports and logs. Default `[]`, at most 20.
  See [Result files of failing checks](#result-files-of-failing-checks).

Checks run in a separate job that has **no secrets and a read-only token**, on the exact
commit NexKit published. A setup failure counts as a failed check. With no checks
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
  check's `results`, then of the file paths. Files that would go past the limit are left
  out, and the `verify` job's run summary lists them, with the paths that were not found.
  Playwright traces of one failed test are often 15 to 20 MB, so keep `results` to the
  folders that explain a failure.
- The next fix round's `agent` job downloads that artifact to a folder outside the working
  tree. The fix prompt names the folder and, for each failing check, its folder and each
  file or folder it kept. When the artifact cannot be downloaded, for example because it
  expired after 14 days, the round runs without the files and its log says why.
- Checks without `results` behave as before.

The artifact can be downloaded by anyone who can read the run, like the rest of NexKit's
logs and artifacts (see [What is redacted](how-it-works.md#what-is-redacted)).

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
- a required check from your own CI, unless `publish.push_token_secret` names a push
  token, because without one your CI does not run on NexKit pull requests (see
  [Troubleshooting](troubleshooting.md));
- a required merge queue, or squash merging turned off for the repository.

`merge.after_workflows` lists workflow files in `.github/workflows/`. GitHub starts no
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
"autopilot": {"enabled": true, "scope": "milestone", "max_parallel": null, "answer": "never"}
```

With autopilot on, nobody needs to post `/nexkit plan` and `/nexkit go`. NexKit looks at
every open issue in scope, oldest first, and starts the one command that moves it on:

| The issue | Autopilot |
|---|---|
| Ready, with no plan yet | Starts `/nexkit plan`. |
| Its latest plan has no questions and is not too large | Starts `/nexkit go`. |
| Collaborators commented after the latest plan | Starts `/nexkit plan` again, so the plan holds their changes before `/nexkit go`. |
| Its plan is too large and suggests a split | Creates each part as a sub-issue, in order, with the issue's milestone and labels, marks each part *blocked by* the earlier parts it needs, closes the issue's open NexKit pull request as superseded, and plans the parts that need no other part. |
| Its plan has questions, and every question has a recommended option | With `answer: "recommended"`: posts the answers for the owner and starts `/nexkit plan` again. |
| Its last `/nexkit go` or fix round was blocked, and every question has a recommended option | With `answer: "recommended"`: posts the answers and starts `/nexkit go` or `/nexkit fix` with them as the note. |
| Its last `/nexkit go` or fix round ran out of time | Starts `/nexkit plan` once, with a note to split the issue into parts that each fit one session. |
| A bug issue that autopilot opened for a CI failure on the default branch | Goes first: it is planned before every other issue in scope. |
| Anything else that needs a person: other questions, a failed run, a refused merge, a second timeout | Nothing: a person decides, as without autopilot, and `notify` mentions them. Other issues go on. |
| A run on it is running or paused, or its pull request is open | Nothing: the run, or the pull request's automatic rounds, carry it on. |
| The last issue in scope closed | Mentions `notify` once, on that issue, that the scope is done. |

- **Scope.** `"milestone"` takes the open issues of the earliest open milestone: by due
  date, then the oldest, skipping milestones with no issues at all. When all its issues are
  closed, autopilot stops there until a person closes the milestone, so it never starts
  the next milestone by itself. `"label"` takes the open issues with `autopilot.label`.
  Issues outside the scope are never touched.
- **Ready.** An issue is ready when no issue that blocks it (GitHub's *blocked by*
  relationships) is open. The order of sub-issues does not count, so sub-issues without
  such relationships start at the same time; text such as "Depends on #12" is not read. A
  parent issue with sub-issues is never planned itself: its sub-issues are.
- **Trust.** Autopilot skips a person's approval of the plan, so the issue text drives an
  implement session, which can read the secrets of its job. It takes only issues opened by
  someone with write access to the repository; only they and other writers can edit them.
  `nexkit status` shows the issues it leaves alone and why.
- **At most `max_parallel` at once.** An issue counts while a run on it is running or
  paused, while its pull request is open, and while a command that autopilot started has
  not run yet. With `null`, every ready issue starts at once; NexKit's conflict rounds
  (see [Conflicts with the base branch](#conflicts-with-the-base-branch)) keep the pull
  requests mergeable.
- **One command per step.** Autopilot keeps one comment on each issue that names the
  command it started last, and for which run. It never starts the same command twice for
  the same state of the issue, so a command that `route` refused, or that never ran, is
  not started again; after six hours, `nexkit status` shows it as not run.

- **Answers.** Plans, and implement and fix rounds that stop as `blocked`, give each
  question its options and the one NexKit recommends, if any; the plan comment marks it
  `(recommended)`. With `answer: "recommended"`, autopilot answers only when every
  question has a recommendation, in a NexKit comment that the agents read as the owner's
  discussion. Comments by other bots never count. When autopilot takes over like this, or
  splits an issue, `notify` is not mentioned.
- **CI on the default branch.** With workflows in `merge.after_workflows`, each sweep also
  looks at the latest run of each of them on the default branch, after NexKit's merges and
  people's alike. A failed run gets its failed jobs run once more. When it fails again,
  autopilot opens a `bug` issue in scope, with the failed jobs and steps and the run link,
  mentions `notify` in it, and plans it before every other issue. A later failure on a
  newer commit adds a comment to that issue while it is open; a commit is reported once.
  Sweeps notice a finished run at the next event or on the hourly schedule.
- **Parts.** Parts that autopilot creates are trusted because they start with a NexKit
  marker and are sub-issues of an issue it trusts. A part's own plan that is too large is
  split again.

Commands that autopilot starts are dispatches, shown as `/nexkit plan (autopilot)`. A
person can still post any command, and autopilot carries on from what it leads to.
Autopilot pairs well with `merge.auto`, `conflicts.auto_resolve`, `close_parent_issues` and
`notify`; without `merge.auto`, an issue moves on when a person merges its pull request.

Sweeps, the runs that decide this, start after every NexKit run, when an issue opens,
closes, is reopened or comes into scope, when a milestone closes, and on the hourly
schedule, which catches anything the others missed. They run one at a time, run no agent
and use no agent credential. The workflow must listen to those events, as `nexkit init`
writes it; `nexkit doctor` warns when it does not. To start a sweep yourself, for example
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
`[]`, nothing is posted.

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
  `nexkit init` writes) and the hourly schedule start the check. `nexkit doctor` warns when
  the `push` trigger is missing; the schedule still notices within an hour.
- When an automatic merge is refused because of a conflict, the round that tried it
  starts a conflict round right away, within the same limits.
- A conflict that needs a choice ends the round as `blocked` with its questions; a person
  answers with `/nexkit fix <decisions>`.
- Each pull request gets at most `max_rounds` of these rounds (default 3), apart from
  `fix.max_auto_rounds`, and one per base commit.

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
| The name `publish.push_token_secret` gives, such as `NEXKIT_PUSH_TOKEN` | no | Publish job only, never a job that runs an agent, and never an agent's secret: pushes the branch and opens the pull request, so the repository's own CI workflows also run. A fine-grained token with Contents and Pull requests write access. Add Workflows write access if fix rounds should merge base branch changes to `.github/workflows/`; the default Actions token cannot push those. |

## Workflow file

`nexkit init` writes `.github/workflows/nexkit.yml`, which calls
`phuongnse/nexkit/.github/workflows/pipeline.yml` at a fixed release tag. It starts NexKit
for `/nexkit` comments, *Request changes* reviews on NexKit pull requests, dispatches,
closed issues (for `close_parent_issues` and `autopilot`), opened, reopened, labeled and
milestoned issues and closed milestones (for `autopilot`), pushes to the default branch
(for `conflicts.auto_resolve`) and an hourly schedule (for `usage_limit.resume`,
`conflicts.auto_resolve` and `autopilot`). With those settings off, these events start
short runs that end at once; remove the triggers you do not use if you prefer. Issue and
milestone events other than a closed issue join the concurrency group
`nexkit-autopilot`, so they never replace a command that waits for its issue. To upgrade, change both the
`uses:` ref and `nexkit_ref` to the new tag (or rerun `nexkit init --force --kit-ref
vX.Y.Z` and restore your config); each release's notes list its upgrade steps. `nexkit
doctor` reports a problem when the two refs differ, and mentions a newer release without
failing: staying on a release is your choice.

## Upgrading from earlier releases

A configuration from an earlier release that no longer fits is rejected with what to
change. Each release's notes list its own upgrade steps.

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
