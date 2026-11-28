# Testing

This page says how NexKit itself is tested: which check covers what, when each one runs, the rules
for the tests a change adds, and when a check may take an exception. It is for whoever works on
NexKit. The commands are in [AGENTS.md](../AGENTS.md#verify), and how a repository that uses NexKit
runs its own checks is in [the checks page](checks.md).

## Which check covers what

Each check owns one kind of mistake, so a change knows which checks it adds to and which must pass.
The workflows under `.github/workflows/` run them on pull requests and on `main`, and NexKit's
rounds on this repository run the commands that `checks` in `.nexkit/config.json` lists.

| Check | Where | What it covers |
| --- | --- | --- |
| Unit tests | `tests/test_*.py`, run by `python3 -m unittest discover -s tests -t .` | Each module's behaviour, most of it against `FakeGitHub`, with the standard library only. `tests/test_host.py` fails any `step` that NexKit's code asks for on a state that is not the item's stored one, the state the row's previous event gave, the import in memory or a call inside the first import. `tests/test_cli.py` also reads the workflows and checks which job holds which token and secret, and `tests/test_answers.py` reads every module outside `nexkit/engine/` except `nexkit/github.py`, which defines the writes, and fails on a write that no engine answer comes before, and `tests/test_engine.py` reads every module outside `nexkit/engine/`, and `bin/nexkit`, and fails on an import of the engine |
| Scenarios | `tests/test_scenarios.py` | Autopilot over many rounds and sweeps: every issue in scope is carried on or waits for a person, and budgets count as they should |
| Conformance | `tests/conformance/` | Each rule of the engine, as a case of events and the actions and state they must give; every policy value, event kind and action kind of the protocol's schemas has a case that plays it; each kind of import (`first_state.KINDS`) has a case in `tests/conformance/imports/`; the `sweep` call has its cases in `tests/conformance/sweeps/`; `scripts/conformance.py` writes the cases as one file and plays it against an engine |
| Prompt goldens | `tests/prompts/` | The prompt each stage writes, compared with NexKit 6's own record of it. A change to a prompt changes its golden by hand and says why. Never write them again with `NEXKIT_WRITE_PROMPTS` from today's code: set it only to add a case's file from NexKit 6's code |
| Application prompt goldens | `tests/app-prompts/`, read by `tests/test_apphost.py` | The plan, implement, fix and review prompts that the application host of `tests/apphost.py` gets from `nexkit-engine serve`, in its own words and workspace texts. Written once with `NEXKIT_WRITE_APP_PROMPTS=1`, which writes only a golden that does not exist yet. A change to a prompt edits them by hand and says why |
| Agent schemas | `tests/golden/agent-schemas.json`, read by `tests/test_protocol.py` | The result schema each stage gives its agent, as NexKit 6 gave it |
| Random walks | `tests/walk.py`, a fixed sample in `tests/test_walk.py` | The engine against its invariants. Unit tests play a fixed sample of walks of seed 349 in a few seconds, some with an application's policies, and fail on a broken invariant that `KNOWN` does not list. The script plays longer walks: the `walks` job of `ci.yml` and NexKit's `walks` check play 1,000 of them |
| Engine mutation check | `tests/mutants.py`, run by hand | Each mutant breaks one rule of the engine in a copy of the tree, and the conformance suite or the walker must see it. `tests/test_mutants.py` checks that each mutant still applies, since the check runs by hand |
| Protocol compatibility | `tests/golden/protocol/`, written by `scripts/protocol_freeze.py`, and `tests/golden/state/`, both read by `tests/test_protocol.py` | The schemas of the last released minor version, which the tree may change only as [versions](engine.md#versions) allows, and a state document of each state at each schema version, which the engine must read and answer at the current one. The copy of the schemas is empty until the release of 7.0; the test proves its rules on copies it makes |
| Application host | `tests/apphost.py`, played by `tests/test_apphost.py` | A host other than GitHub that carries requests from plan to end, or to a wait for a person, over HTTP to `nexkit-engine serve` alone. After every step each action it carried out was pending, and each request that is not final has work, a wait, a hold or attention. It imports only the standard library, which a test checks with `ast`. A test fails on an action kind of the protocol that no scenario carried out |
| Lint | `scripts/lint.sh` | ruff on the Python code, and actionlint on the workflows |
| Caches | the jobs `cache-save` and `cache-restore` of `ci.yml` | The composite action `.github/actions/restore-caches`, which the agent and verify jobs run. `cache-save` saves a cache as [caches](configuration.md#caches) says, and `cache-restore` checks that the action finds it and skips a missing one |
| Plugin | `claude plugin validate --strict .` | The plugin's manifest and skills |
| Docs | `tests/test_docs.py` | Each check that rule 9 of [how the docs stay correct](writing-docs.md#how-the-docs-stay-correct) lists, one test each. A test compares that list with the file. |
| The engine's image | `release.yml` | The image builds from the pinned base and gives every answer of the [conformance suite](engine.md#conformance) over HTTP. `update-image.yml` proposes a new base, and its pull request needs this check |
| Code scanning | `codeql.yml` | CodeQL on the Python code and the workflows |
| Smoke test | `scripts/smoke.py` | Real Claude Code at its pin, as [the smoke test](#the-smoke-test) says |

## Rules for tests

These rules say what a change adds to the checks above, so that each check keeps owning its kind of
mistake. A plan and a review check a change against each of them.

- **A test for each behaviour.** Every behaviour that a change adds or changes has a test that fails
  without it, in the check that [the table](#which-check-covers-what) names for that kind of
  mistake.
- **The test of its kind.** A new automatic rule comes with a scenario in `tests/test_scenarios.py`,
  played over many rounds. A new or changed rule of the engine comes with a case in
  `tests/conformance/`, invariants in `tests/walk.py` and mutants in `tests/mutants.py`.
  `INVARIANTS` in that file says what each one guards, and names them: `schema`, `transition`,
  `outbox`, `carried`, `counts`, `budget`, `give-back`, `due`, `automatic`, `diagnosis`,
  `usage-limit`, `retry`, `lost`, `hold`, `answers`, `questions`, `after-failure`, `split`,
  `parent-end`, `trust`, `start`, `accept-conflict`, `sweep`, `required-checks`, `accept-behind`,
  `behind-checks`, `accept-again`, `approvals` and `refusals`. A rule of the GitHub host, which
  only the host observes, comes with a unit test that fails when that rule is broken. A new
  workflow job extends `tests/test_cli.py`, which checks which token and secret each job holds. A
  prompt change edits its golden as the *Prompt goldens* and *Application prompt goldens* rows of
  [the table](#which-check-covers-what) say.

A change that needs an exception to a check follows [Exceptions to a check](#exceptions-to-a-check)
instead.

## Exceptions to a check

A change adds no exception to a check and loosens none: no new entry in the walker's `KNOWN`, and
no rule of a check or of its test removed, narrowed or skipped. A change that needs one says why in
its pull request. Only the owner accepts it: the review's checklist leaves it to the owner, starts
no fix round for it, and the owner's approval of the reviewed commit accepts it.

## The smoke test

The smoke test runs every agent stage against real Claude Code, at the version that
`nexkit/__init__.py` pins, on a tiny repository. NexKit tests only the versions it pins, with no
matrix of versions. It exercises the CLI's real flags, event stream and structured results, which
the unit tests replace with a fake. It runs each stage through [the runner](engine-hosts.md#runners)
and fails when the runner's result, or its output, does not fit the protocol's schemas. It needs a Claude login or credential,
`CLAUDE_CODE_OAUTH_TOKEN` in CI, and costs a real session, so it runs only when a change touches
what it covers.

Run it when changing `scripts/smoke.py`, `engine/prompts/`, `engine/prompt.py`,
`textutil.py`, `protocol/stages.py`, `agent.py`, `schema.py`, `runtimes/` or a runtime's pin in `nexkit/__init__.py`.
CI runs it on such a pull request from a branch of this repository, never from a fork, because a
fork's pull request gets no secrets. CI also runs it on the pull request of `update-agents.yml` for
the runtime it updates. The other pins, of the image's base, the runner and Python, start no smoke
test. Neither does the pull request of `update-image.yml`, which starts CI with no runtime: the
image's base has its own check, `release.yml`. A ruleset on `main` requires its job,
`smoke-claude-code`, which GitHub counts as passed when CI skips it. So a pull request from a branch
of this repository that breaks real Claude Code does not merge.

The reasons for keeping every pin the pipeline installs in one source, and testing only the pinned versions, are in
[D14](decisions/d14-every-pin-has-one-source.md).
