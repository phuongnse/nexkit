# Acceptance status: incomplete

The full goal is recorded in [bootstrap issue #1](https://github.com/phuongnse/nexkit/issues/1).
Candidate `0.1.0-rc.1` contains an implementation and installable archive. Evidence
is insufficient to claim the complete SDLC. Human approvals are never fabricated;
simulated GitHub boundaries are distinct from live integration.

| Group | Evidence available | Still unverified |
|---|---|---|
| A — package/hosts | Native Codex 0.156.1 install/list and Claude Code 2.1.282 install/details with 8 skills; extracted archive and validators | Other target hosts have documentation checks only; no model-use claim |
| B — CLI on Actions | Live local Codex used skills/tools, changed code and ran a separate reviewer; GitHub platform probe passed | Live AI runner, API authentication and independence from the local host on Actions |
| C — two consumers | Real local Node CLI and Python HTTP API tests with distinct configurations; two authorized private GitHub repositories prepared, one empty and one with a working HTTP baseline | NexKit setup and autonomous delivery in both GitHub consumers |
| D — success | Simulated controller issue→approval→review/checks→merge; the kit's own CI ran on GitHub | Real human approval and autonomous consumer merge without release |
| E — repair | Live local agent reproduced/fixed a sign bug; independent reviewer verified regressions against the original function; controller passes feedback between rounds | Feedback→AI repair→fresh checks/review→merge on Actions |
| F — blocked | Tests cover unauthorized/edit-revert/stale identity/invalid output/missing review/zero-skipped-failed tests/exhausted budgets/control edits | Failure injection in the live environment and final guards |
| G — durability | Ownership, reinstall/uninstall, cancel/resume guards, duplicate intake/reservation, orphan/merged recovery, base ancestry; GitHub accepted native queue syntax | Live concurrent work, repeated events, interruptions, cancellation and insufficient consumer permissions |
| H — release | Tests cover wrong approver/drift/changed bytes or tags/cancel/deadline/retry/lost upload response with original artifact recovery | Real human release decision and publication within the authorized test scope |

## Executed verification

- 60 local tests passed after independent review. Consumer commands, unit cases
  and CLI/HTTP behavior are real; GitHub/controller boundaries are simulated.
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
- GitHub returned HTTP 403 for the rulesets API in both private consumers with
  the message "Upgrade to GitHub Pro or make this repository public to enable
  this feature." Required branch rules cannot currently be configured there.
  Both repositories remain private; account changes need a separate decision.

## External prerequisites still missing

- GitHub support for the required branch rules in the authorized private
  consumers, followed by accepted setup settings.
- An explicit test-release scope; permission to create the two consumers does
  not constitute approval of a release candidate.
- An Actions `OPENAI_API_KEY`, accepted models by role and CI usage limits.
  Local ChatGPT login is not copied to CI. Further local model validation also
  requires available account allowance after the observed usage-limit failure.
- Real humans approving the requirement and release candidate in live acceptance.

The [rerun procedure](live-acceptance.md) preserves every criterion. No public
release or package-registry publication has occurred. Local/CI artifacts are
installation candidates for validation, not a production-readiness claim.
