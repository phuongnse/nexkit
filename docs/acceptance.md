# Acceptance status: incomplete

The full goal is recorded in [bootstrap issue #1](https://github.com/phuongnse/nexkit/issues/1).
Candidate `0.1.0-rc.1` contains an implementation and installable archive. Evidence
is insufficient to claim the complete SDLC. Human approvals are never fabricated;
simulated GitHub boundaries are distinct from live integration.

| Group | Evidence available | Still unverified |
|---|---|---|
| A — package/hosts | Native Codex 0.156.1 install/list and Claude Code 2.1.282 install/details with 8 skills; extracted archive and validators | Other target hosts have documentation checks only; no model-use claim |
| B — CLI on Actions | Live local Codex used skills/tools, changed code and ran a separate reviewer; GitHub platform and self-hosted admission probes passed | Live AI runner authentication and delivery independent of the local chat session |
| C — two consumers | Real local Node CLI and Python HTTP API tests with distinct configurations; two authorized public GitHub repositories prepared, one empty and one with a working HTTP baseline | NexKit setup and autonomous delivery in both GitHub consumers |
| D — success | Simulated controller issue→approval→review/checks→merge; the kit's own CI ran on GitHub | Real human approval and autonomous consumer merge without release |
| E — repair | Live local agent reproduced/fixed a sign bug; independent reviewer verified regressions against the original function; controller passes feedback between rounds | Feedback→AI repair→fresh checks/review→merge on Actions |
| F — blocked | Tests cover unauthorized/edit-revert/stale identity/invalid output/missing review/zero-skipped-failed tests/exhausted budgets/control edits | Failure injection in the live environment and final guards |
| G — durability | Ownership, reinstall/uninstall, cancel/resume guards, duplicate intake/reservation, orphan/merged recovery, base ancestry; GitHub accepted native queue syntax | Live concurrent work, repeated events, interruptions, cancellation and insufficient consumer permissions |
| H — release | Tests cover wrong approver/drift/changed bytes or tags/cancel/deadline/retry/lost upload response with original artifact recovery | Real human release decision and publication within the authorized test scope |

## Executed verification

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
  in the empty repository. Neither repository has completed NexKit setup.
- GitHub initially returned HTTP 403 for the rulesets API in both private consumers with
  the message "Upgrade to GitHub Pro or make this repository public to enable
  this feature." The owner then authorized public visibility for both consumers
  and the sample release. Both repositories are now public and ruleset listing
  succeeds. Actual rules still need to be applied during consumer setup.
- The owner approved the test-release scope in
  `phuongnse/nexkit-validation-existing`, using tag `v0.1.0-test.1` for the sample
  application. This authorizes preparing and exercising that release flow.
  The eventual source commit, version and notes still require the real human
  candidate approval enforced by NexKit. No release has been created.
- The owner selected `gpt-6-luna` with `max` reasoning for implementation and
  independent review. The official model documentation confirms this effort
  level. The kit forwards per-role effort to the pinned official Codex action.
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

## External prerequisites still missing

- Each consumer still needs its own official Codex login and a live model run.
  The subscription integration is implemented, while OpenAI's account-cache CI
  guide excludes public repositories. This custom deployment was explicitly
  selected by the owner and is not an officially recommended public CI setup.
- Accepted invocation/time limits, available subscription allowance, consumer
  setup and the required branch rules. The prior local usage-limit failure
  remains part of the evidence. The later Luna/max probe succeeded, but it does
  not establish sufficient remaining allowance for full live acceptance.
- Real humans approving the requirement and exact release candidate in live
  acceptance; the approved test scope does not substitute for either event.

The [rerun procedure](live-acceptance.md) preserves every criterion. No public
release or package-registry publication has occurred. Local/CI artifacts are
installation candidates for validation, not a production-readiness claim.
