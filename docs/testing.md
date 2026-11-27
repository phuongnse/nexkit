# Testing

This page says how NexKit itself is tested: which check covers what, when each one runs, the rules
for the tests a change adds, when a check may take an exception, and what a change that moves a
decision of NexKit 6 into the engine must prove. It is for whoever works on
NexKit. The commands are in [AGENTS.md](../AGENTS.md#verify), and how a repository that uses NexKit
runs its own checks is in [the checks page](checks.md).

## Which check covers what

Each check owns one kind of mistake, so a change knows which checks it adds to and which must pass.
The workflows under `.github/workflows/` run them on pull requests and on `main`, and NexKit's
rounds on this repository run the commands that `checks` in `.nexkit/config.json` lists.

| Check | Where | What it covers |
| --- | --- | --- |
| Unit tests | `tests/test_*.py`, run by `python3 -m unittest discover -s tests -t .` | Each module's behaviour, most of it against `FakeGitHub`, with the standard library only. `tests/test_cli.py` also reads the workflows and checks which job holds which token and secret, and `tests/test_answers.py` reads every module outside `nexkit/engine/` and fails on a write that no engine answer comes before |
| Scenarios | `tests/test_scenarios.py` | Autopilot over many rounds and sweeps: every issue in scope is carried on or waits for a person, and budgets count as they should |
| Conformance | `tests/conformance/` | Each rule of the engine, as a case of events and the actions and state they must give; every policy value, event kind and action kind of the protocol's schemas has a case that plays it; each kind of import (`first_state.KINDS`) has a case in `tests/conformance/imports/`; the `sweep` call has its cases in `tests/conformance/sweeps/`; `scripts/conformance.py` writes the cases as one file and plays it against an engine |
| Prompt goldens | `tests/prompts/` | The prompt each stage writes, compared with NexKit 6's own record of it. A change to a prompt changes its golden by hand and says why. Never write them again with `NEXKIT_WRITE_PROMPTS` from today's code: set it only to add a case's file from NexKit 6's code, or to change a prompt on purpose |
| Agent schemas | `tests/golden/agent-schemas.json`, read by `tests/test_protocol.py` | The result schema each stage gives its agent, as NexKit 6 gave it |
| Random walks | `tests/walk.py`, a fixed sample in `tests/test_walk.py` | The engine against its invariants, which stays after 7.0. Unit tests play a fixed sample of walks of seed 349 in a few seconds, some with an application's policies, and fail on a broken invariant that `KNOWN` does not list. The script plays longer walks, and `check.sh` plays 1,000 of them until 7.0 |
| Engine mutation check | `tests/mutants.py`, run by hand | Each mutant breaks one rule of the engine in a copy of the tree, and the conformance suite or the walker must see it. It needs no checkout of NexKit 6 and stays after 7.0. `tests/test_mutants.py` checks that each mutant still applies, since the check runs by hand |
| Protocol compatibility | `tests/golden/protocol/`, written by `scripts/protocol_freeze.py`, and `tests/golden/state/`, both read by `tests/test_protocol.py` | The schemas of the last released minor version, which the tree may change only as [versions](engine.md#versions) allows, and a state document of each state and each schema version, which the engine must read and answer at the current one. The copy of the schemas is empty until the release of 7.0; the test proves its rules on copies it makes |
| Application host | `tests/apphost.py`, played by `tests/test_apphost.py` | A host other than GitHub that carries requests from plan to end, or to a wait for a person, over HTTP to `nexkit-engine serve` alone. After every step each action it carried out was pending, and each request that is not final has work, a wait, a hold or attention. It imports only the standard library, which a test checks with `ast`. A test fails on an action kind of the protocol that no scenario carried out |
| Differential check | `tests/differential/`, run by `tests/differential/check.sh` | The engine against NexKit 6, until 7.0: `check.sh` plays a fixed sample of scenarios (`harness.py`), then the random walks. Its `mutants.py` plays the mutants that only this check sees, and the engine's too, with the families that cover them. It takes minutes, so a change that moves a rule runs it by hand, as [its README](../tests/differential/README.md) says |
| Lint | `scripts/lint.sh` | ruff on the Python code, and actionlint on the workflows |
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
  `tests/conformance/`, invariants in `tests/walk.py` and mutants in `tests/mutants.py`. A new
  workflow job extends `tests/test_cli.py`, which checks which token and secret each job holds. A
  prompt change edits its golden as the *Prompt goldens* row of [the
  table](#which-check-covers-what) says.
- **A move into the engine proves more.** A change that moves a decision of NexKit 6 into the engine
  also keeps GitHub's behaviour, as [Moving decisions into the
  engine](#moving-decisions-into-the-engine) says.

A change that needs an exception to a check follows [Exceptions to a check](#exceptions-to-a-check)
instead.

## Exceptions to a check

A change adds no exception to a check and loosens none: no new entry in
`tests/differential/known.py` or in the walker's `KNOWN`, nothing that the differential check stops
comparing, and no rule of a check or of its test removed, narrowed or skipped. A change that needs
one says why in its pull request. Only the owner accepts it: the review's checklist leaves it to the
owner, starts no fix round for it, and the owner's approval of the reviewed commit accepts it.

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
- Walks the engine at random against invariants (`tests/walk.py`), including the invariants of its
  own rules. `INVARIANTS` in that file says what each one guards, and names them: `schema`,
  `transition`, `outbox`, `carried`, `counts`, `budget`, `give-back`, `due`, `automatic`,
  `diagnosis`, `usage-limit`, `retry`, `lost`, `hold`, `answers`, `questions`, `after-failure`,
  `split`, `parent-end`, `trust`, `start`, `accept-conflict`, `sweep`, `required-checks`,
  `accept-behind`, `behind-checks`, `accept-again`, `approvals` and `refusals`.
- Adds to `tests/differential/` ([its README](../tests/differential/README.md)), which holds the
  differential check and a CI job that plays a fixed sample of it on every pull request: a family
  of scenarios for what it moves, and each difference it calls out in `known.py`.
- Adds invariants of its rules to `tests/walk.py` and mutants of its rules to `tests/mutants.py`.
  These two checks need no NexKit 6 and stay after 7.0, so a change to an engine rule adds to
  them after 7.0 as before it. A mutant of a rule that only the differential check sees goes to
  `tests/differential/mutants.py` instead. Each scenario stores its items' imports as the state
  comment before its first event, which the check does not compare with NexKit 6, and
  `--fallbacks` lists the decisions of `host.resync` that still come from the markers'
  document, and `--forbid FUNCTION` fails the check when a named host function still falls back
  to it (the [README](../tests/differential/README.md) says how). With a checkout of
  8bf5190 for the first command:

  ```sh
  python3 tests/differential/harness.py --v6 PATH       # every scenario; --sample K for fewer
  python3 tests/differential/harness.py --v6 PATH --fallbacks   # and each decision that still
                                                        # decides from NexKit 6's markers
  python3 tests/walk.py                                 # 2,000 walks of the engine
  python3 tests/mutants.py                              # each broken rule is seen
  python3 tests/differential/mutants.py --v6 PATH       # each host rule is seen
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
