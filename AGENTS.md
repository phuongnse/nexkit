# Working on NexKit

NexKit turns GitHub issues into reviewed, tested pull requests with Claude Code. It is the reusable
workflow `.github/workflows/pipeline.yml`, the package `nexkit/` that each job runs through
`bin/nexkit` (standard library only, Python 3.12+), and the plugin (`.claude-plugin/`, `skills/`)
that sets up repositories. Read [How it works](docs/how-it-works.md) before changing behaviour,
[Delivery](docs/delivery.md) before taking an issue, and [Testing](docs/testing.md) for the checks.

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
| `engine/` | The engine: `machine.py` holds the states and what moves an item between them, as pure functions; `prompt.py`, `textutil.py` and `prompts/` write each stage's prompt from the texts the host fetched; `cli.py` is `nexkit-engine` and `serve.py` its HTTP server, the program around them |
| `publish.py` | `publish`: plan comment, commit, push, pull request |
| `checks.py`, `cache.py` | Setup and checks with `NEXKIT_BASE_SHA`/`NEXKIT_VERIFIED_SHA`; the keys of cache restore and of the `cache/` action |
| `report.py`, `state.py`, `progress.py`, `refusal.py` | Statuses, review, run and state comments and their markers; why GitHub refused a merge, and what unblocks it |
| `item_state.py`, `first_state.py` | The engine's state document of each work item in a comment of the App, written only as the next revision of the one read; the first one, built from NexKit 6's markers |
| `host.py` | The GitHub host's calls of the engine's `step` for each decision the engine holds ([Getting there](docs/engine.md#getting-there) lists them): the document built from NexKit 6's markers, the event GitHub shows, and the answer that autopilot, `route`, `report` and `maintain` carry out, storing nothing |
| `maintain.py`, `usage.py`, `autopilot.py` | Work after other events; resuming runs paused at a usage limit; autopilot sweeps |
| `config.py`, `globs.py`, `scaffold.py`, `app.py`, `templates/` | Config schema and defaults, path globs; `nexkit init` and `doctor`; the GitHub App |
| `status.py`, `snapshot.py`, `pin.py`, `github.py`, `gitutil.py` | `nexkit status` and the batched reads it makes; the release that the repository's workflow pins, which `status` and `doctor` run; REST, GraphQL and git helpers |
| `scripts/` | Live smoke test, pin updates, release notes, the docs audit; `lint.sh`, the lint that CI and NexKit's checks run |

## Rules

- Jobs that run an agent or repository code have read-only tokens; jobs with write tokens never run
  repository code or receive an agent credential. `tests/test_cli.py` checks this; extend it for new
  jobs.
- NexKit writes on GitHub only as the repository's GitHub App (`github_app`). Each job that writes
  mints its own token with only what it needs; `github.token` only reads. A token lasts an hour, so
  nothing holds one through a longer wait. What the App does starts workflows, NexKit's own too, so
  each write must leave a following run nothing to do. The one exception is the `publish` job of
  `release.yml`, NexKit's own release: it pushes the engine's image with `github.token` and
  `packages: write`, since a GitHub App gets no access to packages, and it runs no repository code.
- Agent output is untrusted: validate it before publishing, pass everything printed or stored from a
  session through `redact.py`, print tool output with workflow commands stopped. Markers count only
  in comments of the App, never the Actions bot, which CI on an agent's code can write as, and only
  at the position NexKit writes them (see *Security notes* in `docs/how-it-works.md`). Agent text
  that NexKit copies where no position pins a marker opens none (`state.inert_markers`).
- State lives only on GitHub (comments, pull requests, statuses).
- Agent errors and `blocked` results never retry automatically; only check failures and review
  findings do, up to `fix.max_auto_rounds`. The one exception is a run that stopped on a transient
  error before it published anything (the service, the network, a lost runner, as `runtimes/`
  classifies it, or no result that fits even when its session was asked for the result alone): it
  runs again after each wait of `retry.wait_minutes`, and a stop after the last waits for a person.
  A command that autopilot or a round started and whose run GitHub never started, or that ended
  without a report, starts again later each time (`autopilot.AGAIN`), since such a stop says nothing
  about the work; then a person decides. Autopilot may answer a `blocked` result whose questions all
  have a recommended option (`autopilot.answer: "recommended"`), once in a row: when the round its
  answers started asks again and no person commented since, a person decides. A recommended option
  is one NexKit can carry out without a person; one that only closes the issue, after a round found
  no change is needed, closes it without another round. A person's answer that picks the option that
  only closes the issue closes it as not planned, with no agent run; agents offer closing only so.
  Autopilot asks for one split plan after an implement or fix agent ran out of time; it never
  repeats a run unchanged.
- Every issue in autopilot's scope is either carried on by a run that exists, started only when
  something changed since the same command last started (a commit, a person's comment or command, a
  new plan), or waits for a person: listed under *Needs a person* with its reason and mentioned to
  `notify` once. Each automatic action checks what it needs before it spends agent time, and its
  budget counts only rounds that ran their agent to its end or published. `tests/test_scenarios.py`
  plays this over many rounds; a new automatic rule comes with a scenario there.
- Prompts judge code behaviour, never things an agent cannot change (PR text, approvals, workflows).
  A person's pull request has no such limit on workflows, since a person wrote them; `/nexkit
  review` there never pushes, starts a round or merges.
- A config key, command or prompt contract changes together with its tests, docs and skills.
- `tests/prompts/` is NexKit 6's own record of the prompts each stage wrote before they moved into
  the engine: never write it again with `NEXKIT_WRITE_PROMPTS` from today's code. A change to a
  prompt changes its golden by hand, in the same pull request, and says why.
- Pins live only in `nexkit/__init__.py`. The pipeline installs exactly those versions; test only
  them, without matrices. `update-agents.yml` proposes runtime updates, and each needs its runtime's
  smoke test to pass; `update-image.yml` proposes the image's base, which needs the image's check.
- Everything specific to a CLI or provider lives in `runtimes/` behind `Runtime` and `Provider`;
  never branch on runtime names elsewhere. Settings that identify an account or resource (such as an
  endpoint) are secret fields.
- NexKit reads only secrets the configuration names (`agents`, `github_app`). `pipeline.yml` reads
  the names `route` lists for a job into its `SECRET_SLOTS`, in the call of `pipeline-agent.yml` and
  `pipeline-review.yml`: a job that runs an agent never indexes `secrets` itself, since that puts
  every secret on its runner. Agent secret names may not start with `GITHUB_` or `NEXKIT_`, the
  App's not with `GITHUB_`.
- A session outside NexKit takes only `hand-made` issues ([Delivery](docs/delivery.md)), and never
  merges, pushes a tag or releases. A change that moves a decision of NexKit 6 into the engine keeps
  GitHub's behaviour, as [Moving decisions into the
  engine](docs/testing.md#moving-decisions-into-the-engine) says.

## Verify

```sh
python3 -m unittest discover -s tests -t .
scripts/lint.sh                            # ruff 0.13.3 and actionlint
tests/differential/check.sh                # the engine against NexKit 6, as CI runs it
claude plugin validate --strict .
python3 scripts/smoke.py [--program PATH]  # real Claude Code: docs/testing.md#the-smoke-test says when
```
