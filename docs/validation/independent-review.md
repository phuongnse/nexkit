# Independent review — 2026-09-25

Reviewer: a separate agent session, `independent_review`, instructed to inspect
specification, code, workflows and tests without changing source or GitHub.
It ran tests and adversarial reproductions. Early snapshots were explicitly
not approved; the implementer did not override the reviewer's verdict.

| Reproduced finding | Correction |
|---|---|
| Runtime branch-protection API needed unavailable admin permission | Active rules metadata API; full audit stays in setup; live read probe passed |
| Workspace ownership/traverse and sudo CODEX_HOME mismatch | Dedicated home permissions and actual default `.codex` path |
| Setup could plant HOME skills; Copilot controls were writable through delivery | Reset HOME/workspace host controls; all-host protected-file regression |
| Ledger/symlink uninstall could target consumer files | Managed-path allowlist, hashes and symlink-component rejection |
| Body/title edit-revert revived approval | Exact hash plus last body edit and native rename event |
| Orphan branch and merged interruption recovery failed | Reuse actual branch/tree/PR state; no duplicate merge or trigger-only commits |
| Same-tree reuse missed changed base ancestry | Verify ancestry and create a genuine integration commit when needed |
| Dependency HOME lifetime and child cleanup were incorrect | Shared HOME and kill processes of dedicated CI account |
| Partial release retry rebuilt different bytes | Persist original manifest/run locator; download original artifact on retry |
| Clarification depended on local host staying open | Actions clarification with bounded state and shared delivery queue |
| Short answer lost its question | Carry pending questions with human answers; answer B regression |
| Outsider comment spent existing approval budget | Check event actor before reservation |
| Two-call minimum could not cover normal flow | Minimum three calls and documented accounting |
| Unchanged files above 2 MB blocked read-only work | Streaming hash snapshots; changed bundle limits retained |

Final review: **60 tests passed in 3.141 seconds; no new code blocker found in
the reviewed scope**. The reviewer independently checked CI/platform-probe links
at 21f68b7. New fixes require CI again after commit.

The reasoning-effort follow-up review found no concrete defect in validation,
role-specific outputs or the three official action invocations. It checked the
pinned action's handling of an empty effort, preserving the CLI default, and
reproduced rejection of effort drift in delivery, clarification and release
guards. **66 tests and Ruff passed.** No model calls or secret access were used.
The selected model's `max` support was checked against official documentation;
actual runner/model execution with this setting remains unverified.

This is a scoped implementation review. Live AI Actions, delivery in both
authorized consumer repositories, real approvals and test release remain unverified. It is not
approval of the full product against those missing acceptance criteria.

## Subscription runner follow-up

The independent session identified pre-job failure handling that allowed later
`always()` steps, HOME dependency loss, untrusted setup Git metadata, ownership
regression in API mode, replaced workspace directories, optional native Git
metadata execution outside the tool sandbox, and a lock acquired after cleanup.
The implementation now terminates rejected Workers, preserves dependencies,
restores trusted metadata with correct ownership, verifies directory identity,
disables parent Git metadata through a fixed PATH shim and acquires the lock first.

The final functional/static pass ran all 13 runner-specific tests and a benign
workspace fixture. Dependencies, executable modes, Git ownership and reviewer
controls were retained correctly. It inspected pinned Codex source to confirm
metadata failures are optional and `apply_patch` does not require that Git path.
No further functional blocker was found in that scope. A broader earlier probe
session was stopped by a tool safety filter; its later follow-up was restricted
to static and benign functional checks. No claim of exhaustive security review
is made. Live login, full delivery and missing-Worker shutdown were not independently
executed by this reviewer; root-agent probe evidence is recorded separately.

## Consumer release preparation

The existing consumer's local package builder received a separate functional
review. The reviewer found that source metadata overwrote the returned ZIP hash
and that reuse preserved unreadable permissions on an older archive. Both were
fixed with direct regression checks. The final pass ran all six package tests,
including the extracted application's real HTTP response, and found no remaining
blocker within that scope. This review made no credential access, model calls,
GitHub writes or release-approval claim.
