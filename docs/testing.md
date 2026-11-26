# Testing

This page says how NexKit itself is tested: which check covers what, when each one runs, and what a
change that moves a decision of NexKit 6 into the engine must prove. It is for whoever works on
NexKit. The commands are in [AGENTS.md](../AGENTS.md#verify), and how a repository that uses NexKit
runs its own checks is in [the checks page](checks.md).

## Which check covers what

Each check owns one kind of mistake, so a change knows which checks it adds to and which must pass.
The workflows under `.github/workflows/` run them on pull requests and on `main`, and NexKit's
rounds on this repository run the commands that `checks` in `.nexkit/config.json` lists.

| Check | Where | What it covers |
| --- | --- | --- |
| Unit tests | `tests/test_*.py`, run by `python3 -m unittest discover -s tests -t .` | Each module's behaviour, most of it against `FakeGitHub`, with the standard library only. `tests/test_cli.py` also reads the workflows and checks which job holds which token and secret |
| Scenarios | `tests/test_scenarios.py` | Autopilot over many rounds and sweeps: every issue in scope is carried on or waits for a person, and budgets count as they should |
| Conformance | `tests/conformance/` | Each rule of the engine, as a case of events and the actions and state they must give; each kind of import (`first_state.KINDS`) has a case in `tests/conformance/imports/`; the `sweep` call has its cases in `tests/conformance/sweeps/` |
| Prompt goldens | `tests/prompts/` | The prompt each stage writes, compared with NexKit 6's own record of it. A change to a prompt changes its golden by hand and says why. Never write them again with `NEXKIT_WRITE_PROMPTS` from today's code: set it only to add a case's file from NexKit 6's code, or to change a prompt on purpose |
| Agent schemas | `tests/golden/agent-schemas.json`, read by `tests/test_protocol.py` | The result schema each stage gives its agent, as NexKit 6 gave it |
| Differential check | `tests/differential/`, run by `tests/differential/check.sh` | The engine against NexKit 6: `check.sh` plays a fixed sample of scenarios (`harness.py`) and random walks of the engine (`walk.py`). It does not run the mutants of `mutants.py`, which take minutes: a change that moves a rule runs them by hand, as [its README](../tests/differential/README.md) says |
| Lint | `scripts/lint.sh` | ruff on the Python code, and actionlint on the workflows |
| Plugin | `claude plugin validate --strict .` | The plugin's manifest and skills |
| Docs | `tests/test_docs.py` | No doc, README, AGENTS.md or skill repeats a pinned version: each pin that [D9](decisions/d9-pins-live-in-one-place.md) governs, or a lint tool's version from `scripts/lint.sh`. The map lists every doc, with the decisions listed through [their index](decisions.md), and its list of the plugin's skills is compared with `skills/`. The links of every page under `docs/`, of the README, of AGENTS.md and of the plugin's skills resolve. The links of the maintenance skills under `.claude/skills/` resolve too. Each link to [how it works](how-it-works.md) and to the pages split from it resolves, from any of those pages. Each task of the `nexkit-maintain` skill links a section of [the delivery page](delivery.md). Each decision file follows the template, its index row matches it, and a rule or a page links each decision. Each test the security page names exists. Each untrusted input in the security page's table links a rule of that page and names a test. Each rule of AGENTS.md links a section of a page under `docs/`. AGENTS.md stays within 800 words, and each of its rules within 60, as [writing the docs](writing-docs.md#how-the-docs-stay-correct) says. It also compares the job table of [the architecture page](architecture.md) with the three pipeline workflows, and its module table with `nexkit/`. It compares the job table of [how it works](how-it-works.md#one-run-ten-jobs) with the jobs of `pipeline.yml`, the permissions its rows name, and the number in its heading. The outcome table of [how it works](how-it-works.md) lists each outcome the code defines and no other. The mention table of [troubleshooting](troubleshooting.md#what-a-mention-says) lists each outcome that needs a person and no other, with the code's text for what to do and why. The command table of [the README](../README.md#use-it) lists each comment command the code accepts, on an issue or a pull request, and no other. The key tables and the complete configuration of [the configuration reference](configuration.md) list each key the code defines and no other, with its default and range. The table of a stage's keys under `## Stages` on the same page lists each key the code accepts and no other. The runtime table of the same page lists each runtime the code defines and no other, with its providers, effort levels and budget support, and which runtimes report usage limits. The provider table of the same page lists each provider the code defines and no other, with its settings and secret fields. The refusals of `unaffected_by` that [checks](checks.md#checks-that-a-change-cannot-affect) lists name each refusal of `nexkit/globs.py` and `config._unaffected_by`, and no other, and the code refuses each one. The list of a check's fields on [the checks page](checks.md#checks) lists each key the code defines and no other. The list of a cache entry's fields in [configuration](configuration.md#caches) lists each key the code defines and no other. The table of the App's permissions in [configuration](configuration.md#the-github-app) matches the permissions and levels `nexkit/app.py` asks for. The trigger table of [the workflow file](configuration.md#workflow-file) lists each trigger of `nexkit/templates/workflow.yml` and no other. The supported versions table of [the README](../README.md#supported-versions) is compared with the pins in `nexkit/__init__.py`, and each `ubuntu-NN.NN` in the README with the pinned runner. The local commands table of [the README](../README.md#local-commands) lists each command `nexkit/cli.py` defines for people, those whose help is not marked `(pipeline)`, and no other, and each option a row names exists. The configuration example of [the README](../README.md#configuration) is what `nexkit init` writes for the README's own setup command. The architecture page has a section for each part: the package, the workflows and their jobs, tokens and secrets, state, the engine and its hosts, and the plugin. The plan and review layouts of [stages](stages.md) list the sections that `publish.render_plan` and `report.review_body` write. Each value that [agents and profiles](agents-and-profiles.md), [how it works](how-it-works.md), [work without a command](work-without-a-command.md) and [failure handling](failure-handling.md) repeat equals its constant in the code, `VALUES` in `tests/test_docs.py`. The words that [agents and profiles](agents-and-profiles.md) and [failure handling](failure-handling.md#failure-handling) list as marking a failing line are those of `FAILURE_LINE` in `nexkit/engine/textutil.py`. The tools that [agents and profiles](agents-and-profiles.md#agents) lists for the plan and review stages are those of `READ_ONLY_TOOLS` in `nexkit/runtimes/claude_code.py`. |
| The engine's image | `release.yml` | The image builds from the pinned base and answers over HTTP as `nexkit-engine` answers on standard input. `update-image.yml` proposes a new base, and its pull request needs this check |
| Code scanning | `codeql.yml` | CodeQL on the Python code and the workflows |
| Smoke test | `scripts/smoke.py` | Real Claude Code at its pin, as [the smoke test](#the-smoke-test) says |

## The smoke test

The smoke test runs every agent stage against real Claude Code, at the version that
`nexkit/__init__.py` pins, on a tiny repository. NexKit tests only the versions it pins, with no
matrix of versions. It exercises the CLI's real flags, event stream and structured results, which
the unit tests replace with a fake. It runs each stage through [the runner](engine-hosts.md#runners)
and fails when the runner's result, or its output, does not fit the protocol's schemas. It needs a Claude login or credential,
`CLAUDE_CODE_OAUTH_TOKEN` in CI, and costs a real session, so it runs only when a change touches
what it covers.

Run it when changing `scripts/smoke.py`, `engine/prompts/`, `engine/prompt.py`,
`engine/textutil.py`, `agent.py`, `schema.py`, `runtimes/` or a pin. CI runs it on such a pull
request from a branch of this repository, never from a fork, because a fork's pull request gets no
secrets. CI also runs it on the pull request of `update-agents.yml` for the runtime it updates. A
ruleset on `main` requires its job, `smoke-claude-code`, which GitHub counts as passed when CI
skips it. So a pull request from a branch of this repository that breaks real Claude Code does not
merge.

The reasons for pinning each version the pipeline installs in one place, and testing only the pinned versions, are in
[D9](decisions/d9-pins-live-in-one-place.md).

## Moving decisions into the engine

Until NexKit 7.0 ships, changes move NexKit 6's decisions into the engine one by one
([Getting there](engine.md#getting-there)). Each such change:

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
- Adds to `tests/differential/` ([its README](../tests/differential/README.md)), which holds both
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
  [The engine](engine.md) and *Getting there* up to date.
- Gets an independent review before it merges. A separate session reviews it, given the issue and
  the diff but not the author's conclusions. The review builds or extends the checks above on
  its own, adds mutation checks of the new rules, and writes a verdict.
  - Only wrong behaviour or instability blocks the merge: a decision that differs from NexKit 6, an
    item stuck or brought back, a round counted wrongly, a crash, a lost event or action, work on
    the wrong change, a transition the states table does not name, or the engine contradicting
    [The engine](engine.md).
  - The author fixes these and checks each fix with the reviewer's own reproducers.
  - Other findings are fixed when cheap. The rest go into one issue in the milestone, blocked by
    the change's issue and blocking the milestone's release issue (#320 for 7.0), and get no
    further review round.
  - The pull request then gains an *Independent review* section: what the review ran, what it
    found, and how each finding was settled.

Its pull request has the sections *What moved*, *Left for #N* when the issue stays open,
*Differential check*, *Random walks*, *Existing tests that changed*, *Independent review* and
*Checks*: the commands under [Verify](../AGENTS.md#verify), and the smoke test when
[the smoke test](#the-smoke-test) asks for it.
