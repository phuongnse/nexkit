# Running live acceptance when prerequisites are available

Keep all A–H criteria. Do not create unauthorized repositories, change settings,
post human approvals on someone's behalf or publish publicly to make a checklist pass.

## Authorized setup

The owner identifies two repositories: one new and one with existing real
behavior, plus the test-release scope and visibility. Select implement/review
models, reasoning effort, CLI version and usage budget. Run `nexkit survey` and use `nexkit-init` for each
repository. Derive configuration from user decisions and actual source; test
fixtures are not a project preset catalog. Pin a verified kit commit by full SHA.

Apply only accepted rules/settings from [configuration](configuration.md).
The account owner supplies OPENAI_API_KEY through GitHub Secrets or `gh secret
set`, never chat/source/logs. Run setup --apply --online, commit accepted setup
to the default branch and run doctor --online --checks. Record unavailable
capabilities honestly; an empty application must not receive fake passing tests.
Secret metadata alone does not demonstrate model authentication.

## Prompt → issue → approval → merge

Use the nexkit-request skill or:

```sh
nexkit request --title 'Concrete behavior change' --body-file request.md --key acceptance-1
nexkit intake-status --key acceptance-1
```

Close the local host after successful dispatch. Verify exactly one issue and
an Actions clarification session reading repository/skills. An authorized human
answers questions on the issue; the next session must receive both the questions
and answers. A real human reads the specification and posts `/nexkit approve HASH`.
Do not manually approve plans, PRs or test execution during delivery.

Observe configured CLI/model/skill/tool logs, implementation, actual tests/E2E,
a separate reviewer session and merge. Exercise the real CLI, HTTP or browser
path appropriate to the consumer. Record issue/PR/run URLs, spec/config/base/head/
kit identity, elapsed time, repair rounds and human interventions. Report tokens
or cost only when measured. No release should exist. Repeat on the second
consumer with different requirements/configuration without modifying the core.

## Repair, blocked paths and durability

Use a meaningful behavioral defect within the approved scope. Observe a check
or reviewer discover it, feedback reach the implementer, and the repaired
candidate receive fresh checks/review before merge. Mock agents do not establish
this live repair behavior.

Within the authorized test scope, exercise:

- Missing/wrong approval, body/title edits and reverts, and outsider comments.
- Changed base/head/config, stale/missing review, failed/zero/skipped tests and
  malformed structured output.
- Attempts to modify self-approval controls, exhausted attempts/calls/time and
  insufficient GitHub permissions.
- Duplicate keys, concurrent dispatch, interruption after a side effect,
  cancellation before publish/merge, resume with the same budget and installation
  updates/uninstall with consumer edits or symlink paths.

No case lacking required conditions may merge. Automated mocks support separate
failure injection, but real GitHub execution is still required to complete G.

## Human release decision

Merge multiple changes and verify that no tag/release was created. Commit all
source version changes before selecting the exact candidate. Prepare notes:

```sh
nexkit release --commit FULL_SHA --version SELECTED_VERSION --notes-file notes.md
nexkit intake-status --operation release --key RETURNED_KEY
nexkit approval CANDIDATE_ISSUE_NUMBER
```

A real human selects timing/candidate and posts `/nexkit release HASH`. Only in
the authorized test scope, observe verify/build/tag/publication of that exact
source, version and notes. Compare manifest and server asset hashes. Later
merges to the default branch must not change the chosen source.

Test a wrong approver, drift, retries and interruption after partial upload.
Status must explain the outcome. Resume must reuse the original run artifact
(retained seven days), without duplicate uploads, moving tags or rebuilding
different bytes. Expired artifacts/hash mismatches must block and retain the
draft. A GitHub release does not grant production deployment authority.

Record real results/links in [acceptance](acceptance.md). B requires a live AI CLI
on a runner; D/H require actual human approvals. Missing prerequisites leave the
corresponding criterion unverified rather than replacing it with an easier demo.
