# How to verify NexKit

[Documentation](README.md) / Verification procedure

Use this procedure when validating a NexKit change or release. The
[verification guide](acceptance.md) explains the checks and how to read run results.
Run only within the owner's accepted repositories, settings, model usage and
release scope. Approvals must come from the actual authorized person.

## Acceptance criteria

Preserve the acceptance criteria below. Run the integration scenarios against
the implementations listed in [current support](support.md).

| Group | Required evidence |
|---|---|
| A — installation | Verify the exact package, native Codex installation, skill discovery, update/reinstall and removal; preserve consumer-owned files |
| B — agent CLI on Actions | Observe actual CLI/model/skill/tool use in Actions, with separate implementation and review sessions |
| C — two consumers | Exercise one new project and one existing project with different requirements and configuration, without adding a project-specific branch to core |
| D — successful delivery | Follow a real request through clarification, exact requirement approval, real checks/E2E, independent review and automatic merge; honor any configured stage decision and create no release |
| E — repair | A real failed check or reviewer finding reaches the next agent session; the repair receives fresh checks and independent review before merge |
| F — blocked paths | Refuse unauthorized or stale work, invalid outputs, missing review, failed/empty/skipped checks, control edits and exhausted budgets |
| G — durability | Handle duplicate/concurrent requests, interrupted side effects, cancellation, resume and permission failures without losing ownership, repeating effects or resetting budgets |
| H — release | Require a separate exact-candidate decision; verify selected-source build, manifest and downloaded assets; preserve tag/asset identity on retry and recover only the original bytes |

Report live GitHub integration, live local agent runs and simulated boundaries
separately. Automated failure injection is allowed for negative and recovery
cases; it cannot replace required live CLI execution, repair or real approvals.

## Authorized setup

The owner identifies two repositories: one new and one with existing real
behavior, plus the test-release scope and visibility. Select implement/review
models, reasoning effort, any optional installation pin and usage budget.
Run `nexkit survey` and use `nexkit-init` for each
repository. Derive configuration from user decisions and actual source; test
fixtures are not a project preset catalog. Pin a verified kit commit by full SHA.

Apply only accepted rules/settings from [configuration](configuration.md).
In API mode the account owner supplies OPENAI_API_KEY through GitHub Secrets or
`gh secret set`. In subscription mode, follow [self-hosted setup](self-hosted.md),
prove admission/isolation with fake credentials, then complete an official
Codex login independently for each consumer. Run setup --apply --online, commit accepted setup
to the default branch and run doctor --online --checks. Record unavailable
capabilities honestly; an empty application must not receive fake passing tests.
Secret metadata alone does not demonstrate model authentication.

## Toolkit publication self-verification

The toolkit owns its tests and repeatable integration probes. The administrative
publication probe runs in the NexKit repository itself; it does not require a
separately maintained consumer project or install the App in any consumer.
Other consumer scenarios below verify complete delivery and adoption separately.

Once per toolkit repository, an administrator follows
[publication authority setup](administration.md#publication-authority), installs
the dedicated App in that repository and saves `NEXKIT_ADMIN_APP_PRIVATE_KEY`.
Save its public metadata as Actions variables:

```sh
gh variable set NEXKIT_ADMIN_APP_ID --repo OWNER/NEXKIT_REPO --body APP_ID
gh variable set NEXKIT_ADMIN_APP_SLUG --repo OWNER/NEXKIT_REPO --body APP_SLUG
gh variable set NEXKIT_ADMIN_APP_INSTALLATION_ID --repo OWNER/NEXKIT_REPO --body INSTALLATION_ID
```

Enable Actions PR creation in that repository. Then run its CI against the
branch containing the selected candidate:

```sh
gh workflow run ci.yml --repo OWNER/NEXKIT_REPO --ref CANDIDATE_BRANCH --field administrative_publication=true
```

The job verifies the actual administrator and exact Actions source revision,
uses the production publication client to mint a token scoped to this one
repository, proves ordinary workflow-token writes succeed while workflow edits
fail with HTTP 403, and publishes the same workflow fixture using the App.
It verifies exact Git refs/content, Actions-authored draft PR and Actions-App
check identity, closes the draft and deletes only its unchanged temporary refs.
It also verifies native token revocation and that the default branch stayed at
its original head. No model is called and no PR is merged.

The `administrative-publication-RUN-ATTEMPT` artifact records source, scope,
temporary resources, outcome and cleanup, including failures. Missing credentials
fail the requested job; a skipped optional job is unverified. Interruption may
leave resources recorded by that attempt; inspect their exact identities before
cleanup. The shared suite tests changed resources and lost responses with
simulated APIs. This live publication probe does not claim consumer application
checks, independent consumer model review, human approval or protected merge.

## Prompt → issue → approval → merge

Use the nexkit-request skill or:

```sh
nexkit --pipeline maintenance request --title 'Concrete behavior change' --body-file request.md --key acceptance-1
nexkit --pipeline maintenance intake-status --key acceptance-1
```

Replace `maintenance` with the consumer's chosen pipeline. Close the local Codex
session after successful dispatch. Verify exactly one issue and
an Actions clarification session reading repository/skills. An authorized collaborator
answers questions on the issue; the next session must receive both the questions
and answers. That person reads the specification and posts `/nexkit approve HASH`.
For the default flow, delivery continues through merge without another approval.
When setup explicitly enables a stage approval, an eligible collaborator must
approve its current subject through the configured GitHub mechanism. Record
that configured decision separately from the default flow; never supply an
approval on the collaborator's behalf.

Observe configured CLI/model/skill/tool logs, implementation, actual tests/E2E,
a separate reviewer session and merge. Exercise the real CLI, HTTP or browser
path appropriate to the consumer. Record issue/PR/run URLs, spec/config/base/head/
kit identity, elapsed time, repair rounds and operator interventions. Report tokens
or cost only when measured. No release should exist. Repeat on the second
consumer with different requirements/configuration without modifying the core.

During an agent session, follow **View current run** from the issue's single
progress comment. Confirm the exact run attempt, live timestamped command output
and process heartbeat. After success, failure and timeout, inspect the job summary
and download the `agent-diagnostics-*` artifact. Check the full report, selected
events, metadata binding and sanitized diagnostics; reasoning and credential
canaries must be absent. Repeat a native rerun and confirm the notice updates
without another bot progress comment or reset of recorded usage. A runner that
is forcibly lost can prevent upload; distinguish that interruption from agent
success. Observability tests with a simulated model do not replace this live
consumer check.

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

No case lacking required conditions may merge. Record native GitHub event and
retry behavior alongside automated failure injection. The specification permits
mocked negative and recovery cases; identify the simulated boundaries, preserve
their assertions and report any native behavior that remains unverified.

## Work recovery and administrative setup

For checkpoint and administrative changes, also verify these cases within the
owner's accepted integration scope:

- Force a timeout before final JSON, observe an interval artifact on Actions,
  remove the old agent disk and restore source/draft in a fresh session. Repeat
  after a second interruption and when finalization is skipped; preserve usage.
- Reject changed spec/edit/config/source/role, unsafe or corrupt artifacts and
  stale/unauthorized discard decisions. Report retention and unavailable tail.
- Exhaust capacity, have the actual administrator review and post a bounded
  exact grant, inspect dry-run, then resume the same issue. Original consumption
  stays spent; no grant can approve a merge or override `retry: never`.
- Run protected setup before any default-branch administration workflow exists.
  Check initial installation and upgrades containing workflow changes with a
  real publication App. Confirm the actual token is restricted to one repository
  and Contents/Workflows write; PRs and required checks remain Actions-App-owned.
  Reject missing/suspended/wrong App installations and permission denial before
  staging or model reservation. Verify sanitized method/path/status diagnostics.
  Interrupt tree, commit, ref and PR publication and reuse the exact candidate,
  recorded review usage and native approval. Confirm App token revocation and
  that neither App key nor token enters the agent/check workspace.
  Verify real bot PR authorship, actual scoped checks, separate CLI review and
  the administrator's native exact-head approval; retain native protection.
- Verify temporary runner admission, expiry, post-merge rebind and preserved
  login/registration. An unsupported old image must block instead of bypassing
  host admission. Revalidate interrupted setup and changed default-branch cases.

`checkpoint-transport` in CI exercises the real Actions artifact service with a
forced fixture timeout and fresh restore, with zero model calls. The Docker
monitor test executes real root/Node processes and native hook cleanup with a
simulated transport. Neither establishes a live consumer model review or human
approval; record those separately.

## Release approval

Merge multiple changes and verify that no tag/release was created. Commit all
source version changes before selecting the exact candidate. Prepare notes:

```sh
nexkit --pipeline maintenance release --commit FULL_SHA --version SELECTED_VERSION --notes-file notes.md
nexkit --pipeline maintenance intake-status --operation release --key RETURNED_KEY
nexkit approval CANDIDATE_ISSUE_NUMBER
```

An authorized person selects timing/candidate and posts `/nexkit release HASH`. Only in
the authorized test scope, observe verify/build/tag/publication of that exact
source, version and notes. Compare manifest and server asset hashes. Later
merges to the default branch must not change the chosen source.

Test a wrong approver, drift, retries and interruption after partial upload.
Status must explain the outcome. Resume must reuse the original run artifact
(normally retained seven days), without duplicate uploads, moving tags or
rebuilding different bytes. Use `/nexkit resume` on the issue or `nexkit resume
ISSUE` to start a new run. Check artifact availability after a native workflow
rerun as well as after interruption during publication. Missing
or expired originals and hash mismatches must block and retain the draft.
A GitHub release does not grant production deployment authority.

B requires a live AI CLI on a runner; D/H require actual authorized approvals.
Missing prerequisites leave the corresponding criterion unverified.

## Task, step and issue completion acceptance

Use these scenarios to verify issue completion and project steps. See
[verification guide](acceptance.md) for the required evidence and result boundaries.
Delivery/release checks alone do not establish task and project-step behavior.

### Manual specification publication

1. Run `nexkit spec` in a consumer checkout. Verify it queues intake without editing
   the issue or posting a comment under the caller's account.
2. Verify the real intake run publishes the intended specification and exact
   approval instructions as `github-actions[bot]`, without calling a model.
3. Repeat the submission and retry an interrupted publication. Preserve the issue
   version, existing approval and usage; create at most one matching bot notice.
4. Verify changed queued input, revoked permissions and another pipeline cannot
   authorize an overwrite. An existing user-authored notice must not suppress the
   bot's instructions. Record simulated failure cases separately from live runs.

### Issue completion

1. Exercise automatic closure after a fresh merge and approved release through
   the updated workflows, including each per-pipeline opt-out.
2. Verify an approved multi-issue scope, independent completion evidence for each
   extra target, an explicit empty scope and references that must stay open.
3. Exercise changes before closure, reopened issues and issues already owned by
   other work; verify they are preserved.
4. Interrupt recording or deny a write, then repeat completion and recover with
   `nexkit complete`. Preserve the existing result, approval and usage; do not
   repeat a model call, merge, build or publication.
5. Verify late comments and historical completed candidates cannot restart work
   or overwrite a completed phase. Run the relevant regression suite.

### Standalone tasks and project steps

1. Exercise a standalone agent task and a command-only pipeline without dummy
   delivery settings, including successful and blocked results.
2. Exercise input/output propagation, omitted required steps, changed source or
   configuration, duplicate reservations and interrupted recording.
3. Exercise a real native job with its exact name, run attempt and report, plus
   wrong-name, skipped, failed, duplicate-name and timeout cases.
4. Exercise actual issue-stage approval and continuation that finishes recorded
   work without another model call. Verify revocation, feedback and budget
   preservation at the task and project-step boundaries.
5. Verify `retry: never` prevents replay after interruption or rejected results,
   and `safe` stays within the accepted round, time and model-call limits.
6. Run the delivery/release regression suite and a composed delivery with an
   additional candidate-bound project step.

## Linux and Windows hosts

This release uses the shared Linux container runtime.

Run local CLI contracts on actual Ubuntu 24.04 and Windows Server 2025 x64.
Run the container runtime on Ubuntu and on a real Windows host with WSL 2,
using Docker Engine inside Ubuntu. A Linux mock cannot establish WSL behavior;
a WSL container cannot establish native Windows application behavior.

1. Run the shared suite on both OSes. Check Unicode and space-containing paths,
   argument preservation, bounded UTF-8 input/output, reserved filenames and
   native process cleanup after success, timeout and controller cancellation.
2. Run `scripts/verify_docker_host.py` on each physical host, with
   `--wsl-distribution Ubuntu-24.04` from Windows. Exercise real container
   isolation with harmless credentials. Commands must use a
   separate identity, keep their accepted HOME across setup/check/build, block
   controller reads and preserve the selected offline network boundary. Check
   public network reachability as a positive control, then deny a real listener
   on the Linux host and the outer Windows host. Verify scoped cleanup leaves
   unrelated networking and containers intact. Record the actual kernel,
   Docker version, seccomp and optional AppArmor support.
3. Exercise the consumer-selected CLI with a local simulated Responses server. Verify the
   accepted role, model and reasoning reach the request, a real sandbox tool
   executes, output is collected, and review cannot edit source. Plant a consumer
   Git executable to prove the trusted parent cannot execute it. Report this as
   actual container CLI/tool execution with simulated model responses, not live
   inference.
4. Verify the actual Windows supervisor keeps WSL alive and starts the Linux
   service only after installing current Windows host addresses. Closing the
   parent or losing its heartbeat must stop the service. Refresh addresses
   before restarting after a change. Verify Linux systemd restart behavior
   separately. Any deployment that claims startup without Windows sign-in must
   demonstrate a reboot and actual tool execution in that exact launch mode.
   Test real repository registration, admitted default-branch jobs and rejected
   branch/PR jobs before adding a model login; a simulated Worker or fixture
   service is separate component evidence.
5. Build the same clean source on both OSes. Compare archive and manifest bytes,
   then run `python scripts/verify_package.py dist/nexkit-1.2.2.tar.gz
   --source-commit FULL_SHA --codex SELECTED_NATIVE_EXECUTABLE` on each OS. Verify
   native launchers, all packaged files and skills, reinstall and removal
   using fresh homes without login or inference.
6. Record any Windows-specific application checks from actual Windows jobs.
   Follow the live delivery and release procedure above on the selected container
   consumer runner, using actual authorized approvals and accepted usage. A
   package probe or local model fixture does not replace this evidence.

### Native checks omitted by the shared suite

The shared suite skips checks when their operating system or explicit setup
conditions are absent. These checks remain required for the corresponding
support claims. Use a local environment or CI runner that meets the prerequisites
for the boundary being verified:

| Boundary | Required environment | How to verify |
|---|---|---|
| Linux command account, private results, credentials and hostile file paths | Linux with separate controller and command accounts and passwordless sudo | Container verifier, or opted-in `tests.test_step_isolation` on a prepared disposable runner |
| API observation account, prompt forwarding, timeout and sudo delegation | Disposable Linux runner with passwordless sudo and `nexkit-agent` | Container verifier, or opted-in `tests.test_observability.NativeApiObserverTests` with `NEXKIT_TEST_ISOLATION=1` |
| Linux agent setup and restoration of trusted controls | Linux with a fresh disposable command account and administrative access | Container verifier, or opted-in `tests.test_agent_workspace.LinuxAgentWorkspaceTests` |
| Actual CLI tools, role permissions, credential and network isolation | Linux container built with the reference CLI, on a supported Docker host | `scripts/verify_docker_host.py` |
| Native Windows directory handles and junction paths | Windows with permission to create the tested links | Shared suite on Windows |
| Actual Windows parent termination and WSL heartbeat timeout | Windows with the configured WSL distribution, Linux systemd and required tools | Opted-in `tests.test_runner_lifecycle` on that Windows host |

On a disposable Ubuntu host with the documented Docker prerequisites, run:

```sh
python scripts/verify_docker_host.py
```

CI uses one reference CLI and the shared adapter contract suite, without a
release-version matrix. For a deliberate runtime investigation, `--codex-version`
selects the CLI to install in the disposable test image. Runtime capability
checks and isolation probes do not compare its release label.

The actual container CLI checks also exercise the public event observer across
request, task, delivery and review roles. They verify real command start/output
events and preserve the existing credential, network and role boundaries; only
model responses are simulated.

The container verifier executes the pinned `actions/setup-python` implementation
from the supplied runtime action, then executes the workspace action read from
the supplied clarification workflow. It uses the self-hosted runner's default
`/opt/actions-runner/_work/_tool` cache, rather than the Python build prefix.
The installed workflow interpreter must
run through sudo with no `LD_LIBRARY_PATH` and complete snapshot/restore. No
consumer image patch is applied. Native Codex tools must read source, deny
credential and controller canaries, and preserve read-only clarification, task
and review workspaces. A second case puts controller data inside the denied
runner tree to check that redundant child mounts cannot break the sandbox.

Repeat with the consumer-selected CLI when investigating a setup failure:

```sh
python scripts/verify_docker_host.py --codex-version 0.159.3
```

The `NexKit checks` workflow accepts the optional `codex_version` dispatch input
to run these same checks on Linux and Windows/WSL in addition to the reference.
This is an installation/test selection, not a compatibility allowlist.

For live acceptance, create a fresh consumer using the supplied image and
reusable clarification workflow at the exact candidate commit. Register the
repository-bound runner, stop/start it and verify the next admitted job without
an old-session conflict. With the owner's selected model and accepted call
limits, run clarification and inspect its actual tool events and published
specification. No consumer changes to the image or copied workflow should be
needed. Report this separately from the verifier's simulated model responses;
record model usage and GitHub run links with that candidate.

This verifies account isolation, private results, trusted workspace restoration
and CLI tool boundaries using isolated accounts and harmless credentials.
It also checks the host network boundary and removes its temporary containers,
network rules and profile. CLI/tool execution is real; model responses are
simulated.

On Windows, native filesystem checks run as part of
`python -m unittest discover -v`. With the documented WSL prerequisites prepared
on that Windows host, the lifecycle checks run with:

```powershell
$env:NEXKIT_TEST_WSL_LIFECYCLE = '1'
python -m unittest tests.test_runner_lifecycle -v
```

These commands can run locally or in CI. The workflow in
`.github/workflows/ci.yml` prepares disposable environments and runs the same
verification paths.

Require each dedicated run to execute the checks for its selected boundary
without skips. Record discovered, executed and skipped checks in the run record.
A failed or skipped native case leaves that boundary unverified. Record the
source identity, environment and actual result; the shared suite's success alone
does not establish these native boundaries.

Keep earlier consumer approvals and budgets intact. If an approved work item
has `retry: never`, do not reset its state or replay it to test a changed adapter.
Repinning the kit or configuration creates a new acceptance identity.

## Record the result

Record the exact tested commit or release, outcome, environment, issue/PR/run
links and remaining limits outside the source tree or with the corresponding
CI run or release. Distinguish static checks, automated tests with simulated
services, live local agents and live GitHub runs. Do not apply an earlier passing
result to changed source. Use the [verification guide](acceptance.md) to interpret
the evidence.

Keep verification results, command output, artifact hashes and run records outside the
source tree or with the relevant issue/PR. Save evidence needed beyond Actions
artifact retention. Use descriptive filenames without date suffixes; timestamps
belong inside a record when needed. User documentation should explain behavior
and how to verify it, without accumulating a report for every development run.
