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
| `context.py`, `evidence.py`, `agent.py` | `agent` and `review`: the texts a prompt needs, CI runs' evidence, agent run, patch |
| `runtimes/` | Everything specific to one CLI or model provider |
| `schema.py`, `redact.py`, `runlog.py` | Result validation, secret removal, log and run summary |
| `protocol/` | The engine's protocol as JSON Schemas (`v1/`), stage result schemas included, with an example of each; `size.py`, the largest state document and how what comes in is kept within it; see [The engine](docs/engine.md) |
| `engine/` | The engine: `machine.py` holds the states and what moves an item between them, as pure functions; `prompt.py`, `textutil.py` and `prompts/` write each stage's prompt from the texts the host fetched; `cli.py` is `nexkit-engine` and `serve.py` its HTTP server, the program around them. Its rules come with a case in `tests/conformance/`, its prompts with NexKit 6's in `tests/prompts/`, a record of NexKit 6 that is never written again |
| `publish.py` | `publish`: plan comment, commit, push, pull request |
| `checks.py`, `cache.py` | Setup and checks with `NEXKIT_BASE_SHA`/`NEXKIT_VERIFIED_SHA`; the keys of cache restore and of the `cache/` action |
| `report.py`, `state.py`, `progress.py` | Statuses, review, run and state comments and their markers |
| `item_state.py`, `first_state.py` | The engine's state document of each work item in a comment of the App, written only as the next revision of the one read; the first one, built from NexKit 6's markers. Each kind of import (`first_state.KINDS`) comes with a case in `tests/conformance/imports/` |
| `host.py` | The GitHub host's calls of the engine's `step`: for each decision the engine holds, so far what follows a plan, a blocked `/nexkit go`, every round's checks and review (a review that paused or failed and a person's own pull request too), a round whose change conflicts and the end of the hold on its conflict round, which checks a fix round diagnoses, a blocked fix round, a round that ran out of time, the end of a wait for a hold or the base branch, required checks that fail at the merge and which stopped rounds count, the document built from NexKit 6's markers, the event GitHub shows, and the answer that autopilot, `route`, `report` and `maintain` carry out, storing nothing |
| `refusal.py` | Why GitHub refused a merge, and what unblocks it |
| `maintain.py`, `usage.py`, `autopilot.py` | Work after other events; resuming runs paused at a usage limit; autopilot sweeps |
| `config.py`, `globs.py`, `scaffold.py`, `app.py`, `templates/` | Config schema and defaults, path globs; `nexkit init` and `doctor`; the GitHub App |
| `pin.py` | `status` and `doctor` run the release the repository's workflow pins |
| `status.py`, `snapshot.py`, `github.py`, `gitutil.py` | `nexkit status` and the batched reads it makes; REST, GraphQL and git helpers |
| `scripts/` | Live smoke test, pin updates, release notes; `lint.sh`, the lint that CI and NexKit's checks run |

## Rules

- Jobs that run an agent or repository code have read-only tokens; jobs with write tokens
  never run repository code or receive an agent credential. `tests/test_cli.py` checks
  this; extend it for new jobs.
- NexKit writes on GitHub only as the repository's GitHub App (`github_app`). Each job that
  writes mints its own token with only what it needs; `github.token` only reads. A token
  lasts an hour, so nothing holds one through a longer wait. What the App does starts
  workflows, NexKit's own too, so each write must leave a following run nothing to do.
  The one exception is the `publish` job of `release.yml`, NexKit's own release: it pushes
  the engine's image with `github.token` and `packages: write`, since a GitHub App gets no
  access to packages, and it runs no repository code.
- Agent output is untrusted: validate it before publishing, pass everything printed or
  stored from a session through `redact.py`, print tool output with workflow commands
  stopped. Markers count only in comments of the App, never the Actions bot, which CI on an
  agent's code can write as, and only at the position NexKit writes them (see *Security
  notes* in `docs/how-it-works.md`). Agent text that NexKit copies where no position pins
  a marker opens none (`state.inert_markers`).
- State lives only on GitHub (comments, pull requests, statuses).
- Agent errors and `blocked` results never retry automatically; only check failures and
  review findings do, up to `fix.max_auto_rounds`. The one exception is a run that stopped
  on a transient error before it published anything (the service, the network, a lost
  runner, as `runtimes/` classifies it, or no result that fits even when its session was
  asked for the result alone): it runs again after each wait of `retry.wait_minutes`,
  and a stop after the last waits for a person. A command that autopilot or a round
  started and whose run GitHub never started, or that ended without a report, starts again later each time
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
- `tests/prompts/` is NexKit 6's own record of the prompts each stage wrote before they moved into the engine: never write it again with `NEXKIT_WRITE_PROMPTS` from today's code. A change to a prompt changes its golden by hand, in the same pull request, and says why.
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

## Moving decisions into the engine

Until NexKit 7.0 ships, changes move NexKit 6's decisions into the engine one by one
([Getting there](docs/engine.md#getting-there)). Each such change:

- Keeps GitHub's behaviour: NexKit 6 at 8bf5190 and the change behave alike on GitHub, unless
  the change fixes a NexKit 6 bug, which its pull request calls out. A differential check proves
  it. Scenarios of what the change moves run on both trees, each on a fresh `FakeGitHub`. The
  check compares the command's result, everything on GitHub after it (comments with markers
  decoded, statuses, reviews, dispatches with their inputs, merges, issues, pull requests,
  labels), the preview and `nexkit status`. Sequences of several rounds and sweeps matter most.
  The pull request gives the scenario counts and explains every difference.
- Walks the engine at random against invariants, including the invariants of its own rules:
  - the protocol's schemas;
  - only the transitions that the states table names;
  - the outbox;
  - every open item is carried on, so none is stuck or brought back;
  - counts within the policies.
- Adds to `tests/differential/` ([its README](tests/differential/README.md)), which holds both
  checks and a CI job that plays a fixed sample of them on every pull request: a family of
  scenarios for what it moves, each difference it calls out in `known.py`, the invariants of its
  rules in `walk.py`, and mutants of its rules in `mutants.py`. With a checkout of 8bf5190:

  ```sh
  python3 tests/differential/harness.py --v6 PATH       # every scenario; --sample K for fewer
  python3 tests/differential/walk.py                    # 2,000 walks of the engine
  python3 tests/differential/mutants.py --v6 PATH       # each broken rule is seen
  ```
- Comes with a conformance case for each engine rule it adds or changes. Every other test passes
  unchanged, or the pull request says why one changed. It brings its rules in
  [The engine](docs/engine.md), *Getting there* and the Layout's `host.py` row up to date.
- Gets an independent review before it merges. A separate session reviews it, given the issue and
  the diff but not the author's conclusions. The review builds or extends the checks above on
  its own, adds mutation checks of the new rules, and writes a verdict.
  - Only wrong behaviour or instability blocks the merge: a decision that differs from NexKit 6, an
    item stuck or brought back, a round counted wrongly, a crash, a lost event or action, work on
    the wrong change, a transition the states table does not name, or the engine contradicting
    [The engine](docs/engine.md).
  - The author fixes these and checks each fix with the reviewer's own reproducers.
  - Other findings are fixed when cheap. The rest go into one issue in the milestone, blocked by
    the change's issue and blocking the milestone's release issue (#320 for 7.0), and get no
    further review round.
  - The pull request then gains an *Independent review* section: what the review ran, what it
    found, and how each finding was settled.

Its pull request has the sections *What moved*, *Left for #N* when the issue stays open,
*Differential check*, *Random walks*, *Existing tests that changed*, *Independent review* and
*Checks*: the commands under [Verify](#verify), and the smoke test when that section asks for it.

## Issues and pull requests

- A pull request closes at most one issue, with one closing keyword. One that does only part of
  its issue says "Part of #N", lists what is left under *Left for #N*, and writes "#N stays
  open". A closing keyword closes its issue even inside a negation, so never write "does not
  close #N".
- A pull request that builds on one not merged yet branches from it, targets its branch, and
  says near the top "Stacked on #N: review and merge that first." Pull requests are
  squash-merged. After a squash merge, the next pull request merges `main` into its branch,
  without a rebase, so that its reviewed commits stay, and then targets `main`.
- A session's next work is the open `hand-made` issues of the milestone that no open issue blocks (GitHub's
  blocked-by), where a blocking issue with an open pull request counts as done and the work
  stacks on that pull request. Of those, take the lowest number, unless the owner names
  another. A question
  for the owner is a comment on the issue it concerns, and work goes on with the issues that do
  not depend on it. Issues, pull requests and their links hold the state of this work, never a
  local note.
- NexKit works this repository's issues itself (`.nexkit/config.json`), pinned to a release,
  never `main`: autopilot takes every open issue but those labelled `on-hold` or `hand-made`,
  and merges its own pull requests once their checks and review pass. A session outside NexKit
  takes only `hand-made` issues, those that change `.github/` or `.nexkit/` among them, so the
  two never work the same issue; it never merges. No agent pushes a tag or releases: the owner
  does, and moves the pin to a release only once it runs well elsewhere.
- Every issue and pull request is in the milestone of the feature it builds, as
  [the roadmap](docs/roadmap.md) lists them. It carries at least one label of its kind
  (`enhancement`, `bug`, `refactor`, `process` or `documentation`) and every other label whose
  description on GitHub fits it, such as `security`. A release pull request carries `release`,
  and one that updates a pin `dependencies`. A pull request that closes an issue carries that
  issue's labels, but for the holds `on-hold` and `hand-made`, and its milestone: NexKit sets
  them on the pull requests it opens, and a session sets them on its own.
- Code, docs, commits, issues and pull requests are in English. NexKit's docs describe NexKit for
  any repository and name no product that uses it, except the README's *Built with NexKit* list
  and the changelog, which may cite a case. Issues, pull requests and code comments may name one
  as context.

## Releases

Feature pull requests change neither the version nor `CHANGELOG.md`. A release pull request
sets the version in `nexkit/__init__.py`, `pyproject.toml`, `.claude-plugin/plugin.json` and
the README's `pipx install` line, and adds a `## X.Y.Z` changelog entry, ending with
`### Upgrade` steps when the configuration, caller workflow or permissions must change. Its
description holds the output of `python3 scripts/release_notes.py X.Y.Z`; after the merge,
release `vX.Y.Z` (titled `NexKit X.Y.Z`) on the merge commit with those notes. Publishing
it runs `release.yml`, which builds `Dockerfile` and publishes the engine's image
`ghcr.io/phuongnse/nexkit-engine:X.Y.Z`. It refuses a tag that is not `v` and the version in
`nexkit/__init__.py`, and a commit that is not on `main`. Every pull request builds and
checks the image the same way, without publishing it.

## Verify

```sh
python3 -m unittest discover -s tests -t .
scripts/lint.sh                            # ruff 0.13.3 and actionlint
tests/differential/check.sh                # the engine against NexKit 6, as CI runs it
claude plugin validate --strict .
python3 scripts/smoke.py [--program PATH]  # real Claude Code at the pinned version
```

Run the smoke test when changing `engine/prompts/`, `engine/prompt.py`, `engine/textutil.py`,
`agent.py`, `schema.py`, `runtimes/` or a pin.
