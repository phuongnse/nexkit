# Acceptance status: incomplete

The full goal is recorded in [bootstrap issue #1](https://github.com/phuongnse/nexkit/issues/1).
Candidate `0.1.0-rc.1` contains an implementation and installable archive. Evidence
is insufficient to claim the complete SDLC. Human approvals are never fabricated;
simulated GitHub boundaries are distinct from live integration.

| Group | Evidence available | Still unverified |
|---|---|---|
| A — package/hosts | Native Codex 0.156.1 install/reinstall/update/remove and fresh discovery of 9 skills; Claude Code 2.1.282 validation and native loading of all 9 skills from the archive | Other target hosts have documentation checks only; no model-use claim from installation |
| B — CLI on Actions | Both consumers completed live Actions clarification with Codex 0.156.1, Luna/max, installed request skills and actual terminal calls; separate local implementation/review evidence is also available | Code edits and independent review on Actions; autonomous delivery after the local host closes |
| C — two consumers | Distinct Node CLI and existing Python HTTP configurations installed in two public GitHub repositories; runner, permissions and strict branch rules audited; existing app passed 17 local cases | Approved application delivery in both consumers; the initially empty consumer still has no application behavior to verify |
| D — success | Simulated controller issue→approval→review/checks→merge; the kit's own CI ran on GitHub | Real human approval and autonomous consumer merge without release |
| E — repair | Live local agent reproduced/fixed a sign bug; independent reviewer verified regressions against the original function; controller passes feedback between rounds | Feedback→AI repair→fresh checks/review→merge on Actions |
| F — blocked | Live unapproved delivery stopped before any model call or PR; tests cover unauthorized/edit-revert/stale identity/invalid output/missing review/zero-skipped-failed tests/exhausted budgets/control edits | Remaining failure injection in the live environment and final guards |
| G — durability | Live overlapping intake events for an existing request were serialized, reused one issue and made no additional model reservation; native Codex reinstall/update/uninstall preserved the package and consumer knowledge; local tests cover installer ownership, cancel/resume, orphan/merged recovery and base ancestry | Concurrent first creation and approved delivery, interruptions, cancellation and insufficient consumer permissions |
| H — release | Tests cover wrong approver/drift/changed bytes or tags/cancel/deadline/retry/lost upload response with original artifact recovery | Real human release decision and publication within the authorized test scope |

## Executed verification

- [Concurrent intake check, September 26](validation/actions-concurrent-intake-2026-09-26.json):
  two native dispatches for the existing Node request entered GitHub's queue
  together. Their receiver jobs ran serially, both reused issue #1, and both
  resulting clarification runs skipped model execution. Persistent state and
  revision, specification/edit time, comments and main were unchanged; no PR
  appeared and the model reservation count stayed at one. A native rerun of the
  first intake (attempt 2) and its follow-up clarification also passed with the
  same observations. This proves overlapping intake and retry for an existing
  unapproved request, not concurrent first creation or approved agent delivery.
- [Native host check, September 26](validation/host-install-2026-09-26.json): the
  clean archive at `37ffbb9` passed installation/loading on Codex 0.156.1 and
  Claude Code 2.1.282 with all nine skills. Codex discovery verified installed
  skill hashes and the development update in a fresh process. No model was
  called, no host credentials were mounted, and live consumer pins were unchanged.
- [Consumer update check, September 26](validation/consumer-update-2026-09-26.json):
  temporary copies of both real consumers migrated from schema 1 to an explicit
  schema-2 pipeline. Preview wrote nothing; an edited workflow blocked the whole
  update; reinstall produced no changes; uninstall preserved consumer files.
  Effective model, effort, engine and the shared six-call limit stayed unchanged.
  Native workflow lint passed, and the Python consumer still passed all 17 real
  unit/HTTP/package cases. This was a local installer check: pending GitHub work,
  live configuration and runner admission settings were not migrated.
  Two repeatable migration regressions cover legacy-wrapper retirement and
  rejection of edited wrappers before writes. The full local suite passes
  148 tests; GitHub and model boundaries in that suite remain simulated.
- 60 local tests passed after independent review. Consumer commands, unit cases
  and CLI/HTTP behavior are real; GitHub/controller boundaries are simulated.
- Reasoning configuration adds six regression tests: validation, per-role
  outputs, clarification inheritance, workflow wiring and candidate invalidation.
  All 66 local tests and Ruff pass. These tests do not make model calls.
- [GitHub CI](https://github.com/phuongnse/nexkit/actions/runs/36117084919) passed
  all 60 tests, Ruff and archive build at `720278e`. Follow subsequent runs in
  [the checks workflow](https://github.com/phuongnse/nexkit/actions/workflows/ci.yml).
- [Platform probe](https://github.com/phuongnse/nexkit/actions/runs/36115701671)
  passed with Contents/Issues/Metadata read, reading issue edit history and
  Actions App ID 15368. The kit repo has no ruleset: this proves API permissions
  and `queue: max` syntax, not merge protection or model authentication.
- [Independent review](validation/independent-review.md) used a separate session,
  reproductions and regression tests. Its final conclusion found no new code
  blocker in the reviewed scope, without claiming complete live acceptance.
- [First live local Codex smoke](validation/local-codex-2026-09-25.json): two
  sessions, four passing unit cases, 11 independent reviewer CLI cases, unchanged
  source during review and confirmed regression failures on the original function.
  Each session had a 180-second limit. CLI-reported input/output tokens were
  137238/2572 for implementation and 104836/4248 for review; input includes cached
  tokens. No actual monetary cost was available.
- The later repeatable-script run completed implementation and seven real tests,
  but its reviewer hit the account usage limit before a final structured verdict.
  That rerun is incomplete, not an additional passing smoke. An earlier harness
  argument mismatch was fixed before this rerun. No provider/budget was changed.
- The owner authorized creating private acceptance consumers on 2026-09-25.
  [nexkit-validation-new](https://github.com/phuongnse/nexkit-validation-new)
  starts empty; [nexkit-validation-existing](https://github.com/phuongnse/nexkit-validation-existing)
  contains a Python standard-library HTTP service committed before NexKit setup.
  At baseline `f22abc7`, six unit tests and five HTTP tests passed locally. The
  HTTP tests start the actual service process and make real TCP requests.
  `nexkit survey` read the existing conventions/commands and found no application
  in the empty repository. Their later setup is recorded below.
- GitHub initially returned HTTP 403 for the rulesets API in both private consumers with
  the message "Upgrade to GitHub Pro or make this repository public to enable
  this feature." The owner then authorized public visibility for both consumers
  and the sample release. Both repositories are now public and ruleset listing
  succeeds. Active rules were subsequently applied during the accepted setup.
- The owner approved the test-release scope in
  `phuongnse/nexkit-validation-existing`, using tag `v0.1.0-test.1` for the sample
  application. This authorizes preparing and exercising that release flow.
  The eventual source commit, version and notes still require the real human
  candidate approval enforced by NexKit. No release has been created.
- The existing consumer now has a deterministic local ZIP builder at
  [ccf1b4b](https://github.com/phuongnse/nexkit-validation-existing/commit/ccf1b4b77c10e1e5f80d0a3e9a2cde64c01430b2).
  It packages exact Git blobs with source hashes. All 17 consumer tests passed,
  including starting the extracted HTTP application. NexKit's local release-build
  helper ran the draft checks, collected the archive and reproduced identical bytes
  on retry. Independent review found and rechecked fixes for the returned digest
  and reused-file permissions. This is administrative release preparation, not
  a live approval or publication. See the [local build record](validation/consumer-package-2026-09-25.json).
- The owner selected `gpt-6-luna` with `max` reasoning for implementation and
  independent review. The official model documentation confirms this effort
  level. The kit forwards per-role effort to the official Codex CLI, through
  the pinned official action in API mode or the subscription wrapper.
- A [local ChatGPT authentication probe](validation/chatgpt-luna-auth-2026-09-25.json)
  succeeded with Codex 0.156.1, `gpt-6-luna`, and `max` reasoning in 13.3 seconds.
  The CLI executed one terminal tool and returned the verified fixture marker
  through a JSON output schema. API-key and access-token environment variables
  were removed; the existing ChatGPT login was used without copying credentials.
  This confirms model access for that local login, not the account's exact plan,
  available capacity for a full delivery, or authentication on Actions.
- The owner selected public consumers, repo-scoped runners on the authorized VPS,
  and official Codex ChatGPT login for each consumer. Both isolated containers
  are registered and online. No existing local login cache was copied.
- On 2026-09-26, the owner completed a separate official device login for each
  consumer. Both CLI processes returned `Successfully logged in`; subsequent
  `codex login status` commands under each runner's dedicated account reported
  `Logged in using ChatGPT`. Both services restarted and GitHub reported both
  runners online. This verifies stored login, not a live model invocation or
  sufficient subscription allowance for delivery.
- Live admission accepted a
  [default-branch run](https://github.com/phuongnse/nexkit-validation-existing/actions/runs/36150561858),
  rejected a
  [non-default branch](https://github.com/phuongnse/nexkit-validation-existing/actions/runs/36151206117)
  and a [PR](https://github.com/phuongnse/nexkit-validation-existing/actions/runs/36151214794)
  before any workflow steps, including an `always()` marker, then accepted the
  [next main run](https://github.com/phuongnse/nexkit-validation-existing/actions/runs/36151508626).
  Workflow-defined forged GitHub context did not change the hook's decision.
  The probe PR was closed without merging. After the runner image update, branch
  [rejection](https://github.com/phuongnse/nexkit-validation-existing/actions/runs/36152763487)
  and main [recovery](https://github.com/phuongnse/nexkit-validation-existing/actions/runs/36152767603)
  passed again. These runs made zero model calls and had no model credential.
- Fake-credential native sandbox probes passed eight boundaries with public
  Internet available outside the sandbox. A host listener was reachable from the
  host and blocked from the runner bridge. Missing Worker ancestry terminated a
  throwaway pinned container with exit 138 instead of returning to workflow steps.
- The installed native sandbox ran six actual existing-consumer unit tests and
  read Git status; the parent Git shim returned 127 as intended. HTTP tests could
  not start their service because tool sockets are disabled: zero HTTP cases ran,
  so this is a capability limitation, not E2E acceptance. HTTP E2E remains required
  in the separate verification job. See [self-hosted operation](self-hosted.md).
- 79 local tests, Ruff and the updated skill validator passed. The archive builds
  with the new runner assets. Independent static/local review checked account
  separation, workspace restoration, lock order and the pinned CLI's optional
  Git metadata path. It did not independently exercise live account login.
- [CI at a7d5fd5](https://github.com/phuongnse/nexkit/actions/runs/36153378829)
  passed all 79 tests, Ruff and packaging. The clean local archive and CI artifact
  have the same SHA-256, `6507ab7ac40938ab85cef5d773630dabdd2d012be00a507bc00efa3c67e9433c`.
  The extracted archive installed successfully through Codex's native marketplace
  and plugin commands in a fresh isolated host configuration; no model call was used.

## Live consumer setup and clarification — 2026-09-26

The owner accepted the concrete setup and invocation/time limits before they
were applied. Both consumers now use trusted kit commit `3963c1c`, separate
repo-scoped subscription runners, `gpt-6-luna` and `max` reasoning. Each work item
has at most six CLI invocations shared by clarification and delivery, two
clarification sessions and two delivery rounds. The authorized acceptance scope
is three application work items across both repositories. Two have been submitted.
See the [setup record](validation/consumer-setup-2026-09-26.json) and
[limit semantics](configuration.md).

Both `main` branches require a pull request, strict `NexKit verification` and
`NexKit review` checks from GitHub Actions App 15368, with no bypass actors and
no additional human PR-review gate. Default workflow permissions are read-only;
the required jobs request their own permissions. The live settings audit passed.
The existing consumer passed six unit, five real HTTP and six package cases locally.
The new consumer's missing application/test files correctly kept application
readiness false; missing suites were not counted as passing.

Live intake created the [Python health work item](https://github.com/phuongnse/nexkit-validation-existing/issues/2)
and the [Node CLI work item](https://github.com/phuongnse/nexkit-validation-new/issues/1).
The [Python clarification run](https://github.com/phuongnse/nexkit-validation-existing/actions/runs/36205233883)
and [Node clarification run](https://github.com/phuongnse/nexkit-validation-new/actions/runs/36205236201)
both completed successfully. Each invoked the official CLI using its own
ChatGPT login and reported using `nexkit-request`, with successful terminal
commands referencing its installed skill path. Both used tools to inspect the
repository. Trusted collection confirmed unchanged source and validated each
structured result before publishing the specification.

The CLI reported 12 successful terminal commands for Python and seven for Node.
Input/output token totals were 92,159/3,918 and 79,815/4,388 respectively; input
totals include cached input. No monetary cost was measured. Only selected
numeric metadata and known skill-path matches were extracted from private CLI
logs; raw logs and credentials were not exported. The
[clarification record](validation/actions-clarification-2026-09-26.json) includes
run/job identities, limitations and observed CLI error-item counts.

These runs prove live requirement clarification, with one reserved model call
per work item. They do not prove implementation, independent Actions review,
autonomous merge or release. The actual requirement approvals remain human
actions on the exact published specifications.

Two additional [live guard probes](validation/actions-guards-2026-09-26.json)
made no model calls. A [delivery dispatch without approval](https://github.com/phuongnse/nexkit-validation-existing/actions/runs/36205759010)
recorded the missing-approval reason and skipped implementation, publication,
verification, review and merge; the main commit and PR count were unchanged.
A [duplicate intake](https://github.com/phuongnse/nexkit-validation-new/actions/runs/36205793228)
reused the same issue. Its [clarification continuation](https://github.com/phuongnse/nexkit-validation-new/actions/runs/36205803914)
skipped the agent because no new requirement input existed. There was one issue,
one clarification notice and still one model reservation. These expected skips
demonstrate guard behavior, not application verification.

## Local composition follow-up

The owner approved consumer-defined native workflow composition. Schema 2 now
implements pipeline-specific settings, explicit workflow routing and a hashed
installation bundle, with ownership-preserving update/removal. **105 local tests
passed** for that slice; the independent reviewer ran 82 focused tests and verified three fixes.
These controller boundaries are mocked. Native-only and clarification-only
pipelines do not require unused delivery fields. See the current
[implementation boundary](workflow-composition.md).

The [core CI run at 68963cd](https://github.com/phuongnse/nexkit/actions/runs/36209633932)
passed all 105 tests, Ruff and package creation for that first composition slice.
The next slice provides an isolated reusable check job, exact configured-check
completeness at merge and aggregation of individual job reports. **114 local
tests pass**, including actual local commands and a setup failure with its log
preserved for repair feedback. GitHub state in those tests is mocked. The
independent review identified the lost setup-failure report; its regression is
fixed, and the concluding review found no further blocker in that scope. Static
actionlint validation passes with only its documented `concurrency.queue`
parser gap covered by a dedicated structural test. This is not live execution
of the new reusable check job or complete agent-stage composition.

The next composition slice adds consumer-defined invocation tasks, accepted
skills, per-call models/reasoning/time/runner settings and individual persistent
call reservations. Five reusable capabilities can be wired through native YAML
for preparation, isolated agents, publication, checks and completion/retry.
The independent reviewer ran 103 focused local tests and verified fixes for
empty optional arguments, repeated-check artifact naming and finalization after
failed downloads. Root verification now includes **138 local tests**, Ruff,
actionlint on workflows and the wiring example, plus both changed skill
validators. Actual workspace restoration, local shell execution and runner
preview are exercised; GitHub and model boundaries are mocked. No additional
live model calls, human approvals, merges or releases are implied.

The [check-capability CI run at 7f38a99](https://github.com/phuongnse/nexkit/actions/runs/36210254042)
passed all 114 tests, formatting/lint, actionlint and package creation. The
[individual-invocation CI run at 22dcffc](https://github.com/phuongnse/nexkit/actions/runs/36212376745)
passed all 138 tests, Ruff, actionlint and package creation. Live composed
consumer delivery is still unverified.

A subsequent durability fix persists validated clarification output before
PATCH and recovers interrupted publication without another model reservation.
Eight new regressions cover lost responses/state writes, current human changes,
approval/cancellation, input/config drift and notice recovery. **146 local tests
pass**. The independent reviewer ran 40 focused tests and extra mocked recovery
experiments, finding no remaining blocker in this slice. GitHub issue writes
still do not have an atomic compare-and-swap guarantee; the documented final-read
and authority guards are not represented as one. These are mocked failure
injections, not additional live clarification sessions.

The separate issue-conversation update passed [GitHub CI at c621443](https://github.com/phuongnse/nexkit/actions/runs/36208620238),
including 88 tests, Ruff and the installation archive build. This is core CI,
not another live consumer model invocation. Both live consumer pins, pending
requirement approvals and accepted usage limits remain at their prior setup.

## External prerequisites still missing

- Both consumers still need approved live delivery with independent review,
  actual verification and merge. Successful clarification establishes Actions
  model access, not enough remaining subscription allowance for all acceptance.
  OpenAI's account-cache CI
  guide excludes public repositories. This custom deployment was explicitly
  selected by the owner and is not an officially recommended public CI setup.
- The prior local usage-limit failure remains part of the evidence. Approved
  invocation/time limits remain in force; retries must not reset counters or
  increase the accepted usage scope.
- Real humans approving the requirement and exact release candidate in live
  acceptance; the approved test scope does not substitute for either event.

The [rerun procedure](live-acceptance.md) preserves every criterion. No public
release or package-registry publication has occurred. Local/CI artifacts are
installation candidates for validation, not a production-readiness claim.
