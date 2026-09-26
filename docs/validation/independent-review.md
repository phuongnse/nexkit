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
actual runner/model execution with this setting was unverified at that review.

This is a scoped implementation review. At that point, live AI Actions, delivery
in both authorized consumer repositories, real approvals and test release were
unverified. It is not approval of the full product against those criteria; later
integration results are tracked in the [acceptance report](../acceptance.md).

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

## Consumer clarification preflight — 2026-09-26

The separate reviewer inspected the first-clarification path at `3963c1c` and
both accepted consumer configurations. It found no new blocker in workflow
routing, workspace paths, ownership handoff, schema/output paths, Luna/max
options or the 15-minute reservation. This was read-only inspection without
credential access, private-log access or model calls. The later successful
Actions runs are integration evidence recorded separately.

## Issue conversation and separate limits — 2026-09-26

The independent session statically reviewed the direct reply field, unchanged
specification handling, optional clarification cap, reservation deduplication,
legacy accounting and delivery retry calculation. It found no blocker in that
change. The implementer also added a regression proving that many clarification
calls do not prevent automatic repair when the project selects separate limits.
These controller tests use a mocked GitHub boundary; the changed conversation
behavior has not yet run on the live consumers.

That review identified an outstanding recovery boundary after the issue body
PATCH but before saving clarification completion. The follow-up below addresses
it; recovery of a missing bot comment after completion was already covered.

## Consumer workflow bindings — 2026-09-26

The independent session reviewed schema-2 settings resolution, workflow/control
hashes, executing-workflow provenance, ownership/removal, pipeline-specific
routing and schema-1 compatibility. It reproduced three defects: an unnecessary
agent-call budget on release-only pipelines, missing preview entries for empty
file additions/removals, and an explicitly null agent runner passing validation.
All three were corrected and covered by regression tests.

The final focused pass ran **82 local tests** and `git diff --check`, finding no
remaining concrete blocker within this slice. A mocked release-only round trip
also exercised preparation, one publication, idempotent retry and wrong-pipeline
rejection. No live Actions runs, model calls, credentials, private logs or GitHub
writes were involved in this review. This establishes scoped configuration,
installation and controller behavior; it does not establish variable-stage agent
delivery or live schema-2 acceptance.

## Reusable check job — 2026-09-26

The separate reviewer found that a failed setup command produced no check record,
so aggregation discarded its structured failure log. A real local regression now
runs an exit-3 setup fixture and verifies that its output reaches delivery repair
feedback while the selected check remains explicitly unexecuted and failed.
The fix and 114-test suite passed locally. The concluding review ran all nine
focused check tests and found no further blocker within the trusted producer
and exact-artifact-ID contract. No live check-job acceptance is claimed.

## Individual invocation composition — 2026-09-26

The independent session reviewed invocation reservation, authority/source
binding, per-call configuration, skills, result recording, candidate publication,
review binding and the native reusable jobs. It reproduced three workflow defects:

- Empty optional check arrays failed argument parsing before an editor could run.
- Repeating the same check after another publication in one run reused its
  artifact name and conflicted with the prior upload.
- A rejected optional artifact download skipped the finalizer and lost retry
  feedback.

All three were fixed with regressions. The final focused pass ran **103 local
tests** and `git diff --check`, and found no remaining code blocker in this scope.
The wiring example was also checked for concurrency, exact producer outputs and
`status == 'done'` gating of required task results. Subsequent implementer tests
exercise the finalizer's real shell with partial files and all download-validity
flags, and preview a selected runner override without provisioning it.

GitHub and model boundaries remain mocked. The reviewer made no implementation
edits, GitHub writes, credential access or model calls. Live composed delivery
remains unverified, and artifact trust still requires accepted consumer YAML to
use the trusted producers' exact outputs.

## Interrupted clarification publication — 2026-09-26

The independent session reviewed the pending publication record and recovery
path. It reproduced a stale-write window: a human issue edit made during config
revalidation was then overwritten by the helper's final issue read and PATCH.
The helper now compares that final read with the expected issue hash/edit time
and checks live approval, cancellation, authorized answers and source before
writing. An interleaving regression covers each human change.

The final scoped pass ran **40 local tests**, including 22 clarification cases,
plus benign mocked experiments for lost state-save responses and unchanged-spec
recovery. No additional code blocker was found. The full implementer suite has
146 tests. The reviewer made no live GitHub writes, credential access or model
calls. The final validation and PATCH remain separate operations, so this is
not an atomic issue-update guarantee.

## Native host and consumer update evidence — 2026-09-26

The independent reviewer verified the installation archive checksum, all 98
manifest hashes and both nine-skill inventories against source `37ffbb9`.
It found no inconsistency in the distinction between native installation/loading,
local migration checks and the pending Actions delivery/release acceptance.
The two new legacy-migration regressions were reviewed and all 19 focused
pipeline tests passed in the separate session. No blocker was found.

The reviewer suggested explicitly including reasoning effort in the legacy
fixture; the implementer added `max` and its preservation assertion. The review
did not rerun native hosts, use credentials, invoke models or change live consumers.

## Configurable stage approvals — 2026-09-26

The separate session reviewed approval configuration, checkpoint provenance,
native issue/PR events, continuation claims, usage accounting, branch policy and
the reusable workflows. It reproduced and rechecked these corrections:

| Finding | Correction |
|---|---|
| Lost feedback-save response or repair dispatch could strand a rejection | Persisted repair intent and repeatable default-branch recovery |
| Shared branch rules could impose an undeclared gate on another pipeline | Consistent native review count across delivery pipelines; additional unsupported rules rejected |
| External collaborator request-changes could trigger unauthorized repair | Preserve the native blocker without granting repair authority outside the configured list |
| Candidate/PR gates could protect their own prerequisites | Reject impossible publication/check/review dependencies during setup |
| A failed check guard could lose the human denial in an incomplete finalizer | Validated denial receipts survive aggregation and prevent model repair |
| Blocked rejection discarded human feedback on later explicit recovery | Feedback retained in the next delivery context |
| Windows issue-comment line endings were ignored | Normalize CRLF before exact command matching |
| Native reruns queried the currently active run instead of the older attempt | Inspect the recorded Actions attempt explicitly |
| Changed decisions or source/config drift stranded unused continuation claims | Record bounded repair or a recoverable human stop; preserve caller admission guards |
| Per-review feedback could exceed the Contents reader's size contract | Identity-only decision receipts, bounded UTF-8 feedback and a limit on all state writes |
| Candidate-gated agent/check jobs lacked private-repository PR reads | Add only `pull-requests: read` to those jobs |

Final scoped verdict: **no remaining blocker found**. The reviewer independently
ran **117 focused tests**, including all 48 approval tests, plus `git diff --check`.
It reproduced changed-decision repair and revoked-approval blocking without
additional model/round reservations, and checked caller immutability, attempt
recovery, state bounds and workflow permissions. The full implementer suite has
196 tests; all four changed skill validators, Ruff and workflow lint passed.

This review used local tests and simulated GitHub boundaries. It made no source
edits, GitHub writes, credential access or model calls. The stage-approval event
and continuation cycle remains unverified live, and neither consumer was migrated.
The documented interval between final authorization reads and GitHub writes
remains an external API boundary, not an atomic authorization guarantee.

## Live delivered consumer audit — 2026-09-26

The separate reviewer inspected both merged consumers, approved requirements,
implementation bundles, native verification reports and independent AI review
artifacts. It ran the five declared suites: Node 5 unit plus 4 subprocess cases;
Python 6 unit, 6 real HTTP and 6 package cases. All passed.

Its independent Node oracle exercised 71 valid integer vectors, including
400-digit carries, 31 invalid strings both alone and within otherwise valid
arguments, and missing operands: 134 subprocess probes in total. The Python
audit compared 27 requests against baseline and delivered services, then checked
3 health variants. Assertions examined exact results, status, output streams,
JSON headers/content length and extracted-package behavior.

Both approved requirements are fulfilled. The reviewer found no genuine
behavioral defect to justify a third repair work item. Source bundles matched
the merged files and modes, and all candidate identities agreed. Separate
Actions jobs, fresh workspaces, native ephemeral CLI execution and unchanged
review output support reviewer independence. The root agent subsequently fetched
the original PR heads and verified empty diffs to their respective merged trees,
resolving the reviewer's initial local-object availability limitation.

This audit involved no application edits, GitHub writes, credential/raw-private-log
access or new model invocation. Successful delivery and additional local probes
do not establish live repair, interruption/cancellation, release or stage approvals.

## Consumer stage-approval setup preview — 2026-09-26

The separate reviewer examined the Python consumer's proposed composed pipeline,
artifact producers, checkpoint continuation, failure finalizer and native PR
event relay. It found two approval-packet defects: the preview still described an
earlier setup command, and recovery omitted interruption after a successful merge
but before restoring the target branch policy. Both were corrected and reviewed
again from a fresh copy of the accepted starting commit.

The complete preview and applied result now agree on 20 migration items; the Git
diff has 21 files including the installation ledger. All nine accepted workflow
and control hashes match. Bootstrap policy changes only the required check names;
the target restores both NexKit checks and raises the native review count from
zero to one. Strictness, main targeting, deletion/force-push protection, other PR
options and the empty bypass list remain intact. Recovery accounts for ambiguous
responses and concurrent administrative drift. No further blocker was found in
this scoped review.

The frozen draft setup is PR #4 at `41cf32c2c75faa19b591d7e61df92b9b36e1d67a`,
based on `d2516347d796e0c3bc7bbd97de9a7ed52e9cd036`. The reviewer checked local
6/6/6 results; the root agent separately collected the matching successful native
setup report from run 36247864390. Main and branch policy remain unchanged at
this checkpoint. Live migration and human stage-approval acceptance remain pending.

## Applied consumer stage-approval setup — 2026-09-26

After the owner authorized the reviewed administrative rollout, the separate
reviewer audited actual main at `439ec14ccb0b2135be3bd9c50b2cd3996a2465b4`, merged
PR #4, live rule parameters and the authenticated saved response showing no bypass
actors. The reviewed setup-head-to-merge tree diff is empty. Both installed and
original consumer worktrees are clean, all nine accepted hashes match, and the
administrative PR changed no application files. Online doctor reported no problems
with 6 unit, 6 HTTP and 6 package cases passing.

Native delivery guard run 36250155039 completed one hosted preparation job and
skipped all eight downstream jobs. Continuation run 36250157391 completed one
hosted resume job and skipped finish. The old issue's state revision, three
reserved calls, one round, three-PR count and zero releases remained unchanged.
No discrepancy was found. These results establish live setup and guards; full
composed delivery, a real human stage approval, live repair and release remain
unverified.

The separate review also exercised the one-off rollout operator outside NexKit.
It reproduced and verified a fix for a merge completing during policy rollback;
all 21 local mocked cases passed. Its separate GET/PUT administrative race is
documented, and the actual rollout required exclusive administrative execution.
Those mocks do not establish live interruption or recovery acceptance.

## Third work-item clarification — 2026-09-26

The separate reviewer checked the new clarification record against native intake
run 36250408466, clarification run 36250424673, artifact metadata and saved JSON
hashes. Both runs succeeded at consumer `439ec14` and kit `ace93ccb`. The dedicated
ChatGPT CLI step succeeded; the API-key step skipped. Source remained unchanged.
Issue #5 contained only the bot's exact-version approval notice, and the state
recorded one shared CLI reservation with no delivery attempt. The reviewed scope
contains no submitted human requirement or stage approval.

The reviewer found no blocking discrepancy. One ambiguous phrase was corrected
to distinguish the configured native PR-review requirement from an actual human
review. These checks do not establish complete composed delivery or the human
stage-approval cycle.

## Composed delivery candidate before human review — 2026-09-26

The separate reviewer audited Python PR #6 at exact candidate
`24a961ac1b8f215ad17af975cd909aa24284c4c5`, based on `439ec14`, against the actual
owner-approved issue #5. All 6 unit, 7 HTTP and 6 package cases passed locally.
Raw TCP responses through EOF confirmed HEAD /health returns HTTP 200 with the
same JSON type and Content-Length 16 as GET, and zero body bytes. The exact-commit
extracted package passed the same checks. All 44 GET comparisons matched the
baseline; both new endpoint regressions failed against the old 501 response, and
the raw helper captured a known body fixture.

Repeated local package builds produced identical bytes, with verified source
metadata and readable permissions. The source diff changes only the endpoint,
README and HTTP/package regression coverage; controls and runtime dependencies
are unchanged. No behavioral defect or requirement gap was found. These are
independent local application and regression checks, separate from the root's
live Actions source/check/review/checkpoint provenance audit. They do not grant
human PR approval or prove completed continuation, merge or live AI repair.

The reviewer subsequently checked the combined delivery evidence record. All nine
artifact hashes, invocation context/result and verification receipts, checkpoint
digest and intentional source freeze matched. Public metadata confirmed 13
successful jobs, PR #6 open with no human reviews, and three shared CLI calls in
one round. Saved runner metadata showed it online and idle. Wording about merged
deliveries was scoped to the earlier schema-1 runs so it does not include the
new candidate. No provenance discrepancy was found.
