# Configuring checks

This page of the [configuration reference](configuration.md) says how to set the checks that decide whether a change is acceptable: how they run, alone, in parallel or in shards, what NexKit keeps when they fail, the evidence of CI runs, and how a check skips work a change cannot affect.

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
  stop on it. A check or setup command ends when its shell exits, with the shell's exit
  code. A process it leaves running, such as a service that `setup` starts for the checks,
  keeps running; while it still holds the command's output, NexKit reads that output for at
  most 5 more seconds, then goes on and says so in the output. At the time-out, NexKit kills
  the command's process group and fails it with exit code 124, keeping the output so far; a
  process that left the group (through `setsid`, `sudo` with a terminal, or a daemon) is
  not killed.
- `slow`: default `false`. See [Slow checks](#slow-checks).
- `results`: files or folders, relative to the repository root, where the check writes its
  results, such as traces, screenshots, test reports and logs. Default `[]`, at most 20.
  See [Result files of failing checks](#result-files-of-failing-checks).
- `group`: with `parallel_checks`, the job the check runs in, a short lowercase
  identifier; checks with the same `group` share a job. Without it, the check has a job of
  its own. See [Parallel checks](#parallel-checks).
- `unaffected_by`: globs of the paths the check never reads. Default `[]`, at most 50. In
  an automatic review, a check whose paths did not change since it passed runs on no
  runner. See [Checks that a change cannot affect](#checks-that-a-change-cannot-affect).

`nexkit init` writes each check from `--check NAME=COMMAND`, and its other fields from
flags that name the check: `--check-timeout NAME=MINUTES`, `--check-slow NAME`,
`--check-results NAME=PATH`, repeated for each path, and `--check-unaffected NAME=GLOB`,
repeated for each pattern in order (quote it, so the shell leaves `*` and `!` alone). The
example's `e2e` check comes from:

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

With no check marked slow, the agent runs every check. The plan prompt lists every check
either way.

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
  token, restores the [caches](configuration.md#caches), runs `setup` and then its own checks, on the same
  published commit. Its time-out is `limits.setup_timeout_minutes` plus its own checks'
  `timeout_minutes`, plus 10 minutes.
- A `combine` job then joins the jobs' results, so the round's result is the same as with
  one job: one `nexkit/checks` status, one `checks.json` with every check in the order of
  `checks`, the failing checks' result files under `results/<check>/`, and the same fix
  prompt. It runs no repository code. Its run summary has each check job's result and
  time, and marks the job the run waited for (see [Logs, summary and
  artifacts](how-it-works.md#logs-summary-and-artifacts)).
- A `setup` that fails in one job counts as a failed check, as with one job, and that
  job's checks do not run. A job that ended without a result, for example because its
  runner was lost, fails each of its checks, and the review does not run.
- Each job keeps its own share of [result files](#result-files-of-failing-checks), and a
  passing job keeps nothing, so the round's artifact grows only with the number of failing
  jobs.
- With `parallel_checks` off, or when every check would run in the same job, a single
  `verify` job runs every check one after another. `group` is refused without
  `parallel_checks`.

**Shards.** A check that splits its own work, such as an end-to-end suite that its
scripts split by measured durations, can run in as many shards as a command says when the
round starts, instead of being listed once per shard:

```json
{"name": "e2e", "run": "scripts/e2e.sh", "timeout_minutes": 20,
 "shards": {"count": "node scripts/lib/e2e-shard.mjs --count", "max": 6}}
```

- A `shards` job runs `count` on the head the checks run on, before `setup`, so it may use
  only what the runner has and the repository's files. It has no secrets and a read-only
  token, like the `verify` jobs, gets their environment (`NEXKIT_BASE_SHA` and
  `NEXKIT_VERIFIED_SHA`), and may take 5 minutes per sharded check; the job may take 10
  minutes more. A `shards` job that fails as a whole, such as on a lost runner, runs the
  command once more, as a `verify` job does.
- The last line `count` prints must be a whole number N from 1 to `max` (2 to 20).
  NexKit then runs N checks, `e2e-1` to `e2e-N`, each in a `verify` job of its own, with
  `NEXKIT_SHARD=k/N` in its environment: `k` from 1 to N. Each is a check of its own:
  its status in the review and the round's comment, its result files under
  `results/e2e-k/`, its time-out (`timeout_minutes`), its `unaffected_by` and its
  `NEXKIT_CHECK_VERIFIED_SHA`, which a shard gets only from rounds that counted the same
  N, since another N splits the work otherwise. A fix round's prompt names the shard that
  failed with its `NEXKIT_SHARD`.
- A `count` that fails, prints no number, or prints one outside 1 to `max` fails the
  check `e2e` with the end of its output, as a failing check does, and no shard runs. NexKit never
  guesses a number, so no test is left out silently.
- `shards` needs `parallel_checks`, and a sharded check has no `group`. A sharded check's
  name may be at most 36 characters. No other check may take the name of one of its shards,
  `e2e-1` to `e2e-<max>`. Shards listed by hand, one check each, need no `shards`.

**Many checks at once.** The checks after a fix round are compared with the checks before it
to find those that failed the same way, which the next automatic fix round diagnoses (see
[Failure handling](failure-handling.md#failure-handling)). The comparison reads at most the first
300 failing results of the checks before the round, which 15 checks of 20 shards each reach.
A check whose failing result comes after those is not compared, so it never makes the next
automatic fix round a diagnosis round. NexKit keeps at most 150 checks that repeated, and a
diagnosis round looks into the first ten of them. The engine's [state](engine-states.md#states)
holds no more than these bounds.

Each job sets up the repository again, so parallel jobs use more runner minutes than one,
and are worth it when a few slow checks dominate the round. `nexkit init` writes the
setting from `--parallel-checks`, and each `group` from `--check-group NAME=GROUP`.

### Result files of failing checks

NexKit keeps only the end of a check's output in the round's artifact: the last 12,000
characters, and the first 20 lines before them that report a failure. The fix prompt shows
the last 6,000 characters and the review prompt the last 3,000, each after the lines that
report a failure (at most 20 in the fix prompt, 10 in the review prompt), and each names a
file outside the working tree with all that NexKit kept (see
[Agents](agents-and-profiles.md#agents)). A failing end-to-end suite often leaves its real
evidence in files instead. Name them in `results`, and the next fix round's agent
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
- A round whose checks run in one job keeps at most 50 MB of result files, in the order
  of `checks`, then of each check's `results`, then of the file paths. With [parallel
  checks](#parallel-checks), each job keeps up to the larger of its even share of 50 MB
  and 25 MB, in the same order: 25 MB per job with two jobs or more. Files that would go
  past the limit are left out, and the `verify` job's run summary lists them, with the
  paths that were not found.
  Playwright traces of one failed test are often 15 to 20 MB, so keep `results` to the
  folders that explain a failure.
- The next fix round's `agent` job downloads that artifact to a folder outside the working
  tree. The fix prompt names the folder and, for each failing check, its folder and each
  file or folder it kept. When the artifact cannot be downloaded, for example because it
  expired after 14 days, the round runs without the files and its log says why.
- When a kept file is a JUnit XML report (a `.xml` file whose root is `testsuites` or
  `testsuite`), the check also records the test cases that failed or erred in it, which
  autopilot compares with the default branch's failing tests (see *Failing checks the
  default branch has* under [Plan, implement, fix and review](stages.md#stages)).

The artifact can be downloaded by anyone who can read the run, like the rest of NexKit's
logs and artifacts (see [What is redacted](how-it-works.md#what-is-redacted)).

### Evidence from CI runs

```json
"evidence": {"artifacts": ["test-results-e2e-*", "performance-report"]}
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
- **What.** For each run and each attempt that failed: the end of each failed job's log,
  its last 2 MB, and its error and warning annotations. A log that NexKit cut starts with
  a line that says so. Then the run's artifacts that `artifacts` lists,
  as the repository's CI workflows upload them; NexKit's own artifacts are not among
  them. Without `artifacts`, only logs and annotations. For each run, also whether
  a newer run of the same workflow passed on the default branch since, and which: among
  the workflow's 20 latest runs there, without runs of pull requests and forks.
- **Which artifacts.** A name matches the artifact of that exact name. A name ending in
  `*` matches every artifact of the run whose name starts with the part before it, so
  `test-results-e2e-*` takes each shard's `test-results-e2e-1`, `test-results-e2e-2` and
  so on, however many shards the check has today. `*` may only end a name. The list may
  hold any number of names, and an artifact that several of them match is downloaded
  once. What bounds a round's download is the five runs and the 250 MB below, not the
  number of names: artifacts are taken in the order the list names them, and those of
  one name in the order of their names, until the 250 MB are used.
- **Where.** The plan, implement and fix rounds' `agent` job downloads them, with
  `actions: read` and `checks: read`, to a folder outside the working tree, one folder per
  run. Text files are redacted like the result files of failing checks; binary files are
  kept as they are. A round keeps at most 250 MB, more than a check job keeps
  of [result files](#result-files-of-failing-checks), since the folder is not uploaded
  again; what would go past it is left out. The
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
  [Autopilot](autopilot.md#autopilot) for how the issue closes.

`nexkit init --evidence-artifact NAME`, repeated for each name or pattern, sets
`artifacts`.

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
- That round may not have run a check added since, or may have run it with another `run`
  command, so a check compares with its own `NEXKIT_CHECK_VERIFIED_SHA` (below) instead.

Each check also gets `NEXKIT_CHECK_VERIFIED_SHA`, in its own environment only: the head of
the latest earlier round of the pull request on which that check passed with the same
`run` command and the same `setup` commands. It counts rounds whose other checks failed, and rounds that NexKit stopped
in their checks because the base branch moved (`superseded`, see *Stopping a round in its
checks* under [Work NexKit starts by itself](work-without-a-command.md#work-without-a-command)), so after a stop or
a failed round, a check whose paths did not change since it passed can skip its work
while the others run again.

- A round's comment records which checks passed on its head. A stopped round records the
  checks of the check jobs that had finished when NexKit stopped it, so only with
  [`parallel_checks`](#parallel-checks): the one job without it records its results when
  it ends. A check that failed, or never finished, gets no commit from that round.
- A check whose `run` changed gets no commit from rounds that ran another command, and no
  check gets one from rounds that ran other `setup` commands, or that recorded none.
- It is unset, and the check's part of the job log says why, when no earlier round passed
  that check with this `run` and `setup`, or the checkout does not have the commit. Setup
  does not get it.
- The check decides what to skip, and the round passes or fails as its checks exit,
  exactly as without the variable. NexKit itself skips a check only by its
  [`unaffected_by`](#checks-that-a-change-cannot-affect).

Prefer `NEXKIT_CHECK_VERIFIED_SHA`, and fall back to `NEXKIT_BASE_SHA`. Never fall back to
`NEXKIT_VERIFIED_SHA`: when the check's own variable is unset because its `run` or `setup`
changed or the check is new, the last round whose checks all passed never ran it this way,
and a check that compared with it would skip work that never passed.

```sh
# Run the dev tooling smoke tests only when the change can affect them.
since="${NEXKIT_CHECK_VERIFIED_SHA:-${NEXKIT_BASE_SHA:-}}"
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

### Checks that a change cannot affect

A check that skips its own work still holds a runner while NexKit checks out the
repository and runs setup. A check that names the paths it never reads in `unaffected_by`
gets no runner at all when none of the others changed:

```json
{
  "name": "integration",
  "run": "scripts/integration.sh",
  "unaffected_by": ["docs/**", "**/*.md", ".github/**", "web/**", "!web/package.json"]
}
```

`nexkit init` writes it from `--check-unaffected`, once for each pattern:

```sh
nexkit init --check "integration=scripts/integration.sh" --check-unaffected "integration=docs/**" --check-unaffected "integration=web/**" --check-unaffected "integration=!web/package.json"
```

- Paths are relative to the repository root. `*` matches any characters but `/`, `?` one
  of them, `[abc]` and `[!abc]` one character of a set, and `**`, only as a whole path
  segment, any number of folders: `docs/**` is everything under `docs/`, `**/*.md` every
  Markdown file.
- A pattern that starts with `!` takes paths back out of the list. The last pattern that
  matches a path decides, as in the `paths` filters of GitHub Actions; a path that no
  pattern matches is one the check reads. The example lists everything under `web/`
  except `web/package.json`.
- NexKit refuses, and `nexkit doctor` says why, a pattern that is empty, starts or ends
  with `/`, has `**` inside a segment, an unclosed or empty `[`, or a `.` or `..` segment,
  and a list whose first pattern starts with `!`.

Only an **automatic `/nexkit review`** skips a check: the one after NexKit
[brought the branch up to date](merges-and-waits.md#conflicts-with-the-base-branch), after a new commit while
a merge waits, or its one more try. There `route` knows the commit the checks would run
on before any check job starts. A `/nexkit review` that a person comments runs every
check. Implement and fix rounds get their commit after the check jobs are fixed, and run
every check.

`route` compares the round's commit with the check's own record: the head of the latest
round of the pull request on which the check passed with the same `run` and the same
`setup` commands. When every path that changed between them, both names of a renamed
file, is in the list, the check is skipped:

- Its job is left out of the check matrix. With [`parallel_checks`](#parallel-checks), a
  job of several checks is left out only when all of them are skipped; in a job that runs,
  a skipped check does not run either. When every check is skipped, no `verify` job runs,
  and the review still does.
- It counts as passed, and the round records it as passed on its head with its `run`, so
  the next round compares with the new head.
- The round's comment, the run summary and a notice annotation of the `route` job say
  "`integration` skipped: no path it reads changed since `41533e7`". The AI review's
  comment shows it as ⏭️.

At any doubt the check runs in full: when no round passed it with this `run` and
`setup` (such as a check added since), when GitHub cannot compare the commits, when the
round's commit does not descend from the recorded one (as after a force-push), or when
GitHub lists 300 changed files, the most it lists. NexKit never falls back to `NEXKIT_VERIFIED_SHA` here: a round whose checks all
passed may not have run this check at all.

`route` holds write tokens, but it decides from the paths GitHub lists and runs no
repository code. The list comes from `.nexkit/config.json` on the default branch, like
every setting, so a pull request cannot widen it for its own checks. A wrong list skips a
check that should have run, in every later round. Keep `.nexkit/**` out of a list unless
the check depends on nothing there: a change of `setup` gives the check a new record, but
other settings do not.

