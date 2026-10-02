# Troubleshooting and recovery

[Documentation](README.md) / Troubleshooting

Start with the latest bot message on the issue, then open its Actions run and
any linked PR. The run shows which job is waiting or failed. From the project
checkout, you can also read the saved state:

```sh
nexkit status 42
```

Replace `42` with your issue number. To inspect installation and GitHub settings,
run `nexkit doctor --online --checks`. That command also runs the project's
declared application checks.

## Common situations

| What you see | What to check or do |
|---|---|
| `nexkit` is not found | Add the NexKit checkout's `bin` directory to your `PATH`; see [installation](getting-started.md#2-get-nexkit-and-make-its-command-available) |
| The setup skill is missing | Start a fresh Codex session and check the plugin path and skill name in the [installation instructions](getting-started.md#3-install-the-plugin-in-codex) |
| `/nexkit start` does nothing | Check the pipeline name, your write access and whether its intake workflow accepts comments on the default branch; older pinned installations need a [project update](project-setup.md#change-settings-later) |
| The bot asks for more information | Reply on the same issue in an ordinary comment; this may use another clarification call |
| It still waits after you wrote "approved" | Copy the exact `/nexkit approve HASH` command from the current bot notice |
| A PR waits for review | Open its diff and submit the configured GitHub review; a requirement approval does not satisfy a PR approval |
| A job stays queued | Check Actions concurrency and, for a dedicated runner, its online status and labels |
| `libpython3.12.so.1.0` is missing after sudo | Use the supplied `actions/python-runtime` before controller or workspace steps; it verifies the actual interpreter with no loader variables. Reprovision an older image and update the accepted kit pin/workflows deliberately |
| Every tool fails with `bwrap: Can't mkdir parents` / `Read-only file system` | Check the private directory profile and controller path. Supplied workflows use `/tmp/nexkit`; the adapter removes redundant denied children and tests source reads before login/model use |
| `A session for this runner already exists` after restart | Use the supplied image with `RUNNER_MANUALLY_TRAP_SIG=1`; stop through the runner service so the listener gets its shutdown signal. Verify a job after stop/start |
| Model login or allowance failed | Ask the runner/account administrator to repair the configured authentication or available allowance, then inspect the saved usage before resuming |
| A call, round or time limit was reached | Review the consumed usage and decide whether to change setup; resume keeps the same counters |
| Installed config or file hashes differ | Prepare an update through `nexkit-init` and the installer; reconcile local edits with its recorded files |
| A check or review failed | Read the actual result. An allowed repair round may follow; a stopped run needs its stated blocker resolved |
| A release is partly published | Inspect the same candidate and its original artifacts; use the recovery procedure below |
| Work merged or released, but its issue is still open | Inspect `completion` in status, preview `nexkit complete ISSUE`, then use `--apply` to repair verified completion; see [issue completion](issue-completion.md) |
| A task pipeline is `completed` but has no PR | This pipeline ends with a report. Read its completion comment and artifacts; its issue remains open for follow-up |
| A project step is missing or blocked | Inspect the configured required step, its reservation and recorded result. Native job success alone does not complete a managed step |
| A project step prevents retry | `retry: never` stops replay after reservation. Investigate the recorded job and any external effect before authorizing new work |

If a start command is rejected as edited or stale, inspect the failed run and
post a new standalone command on the same issue when appropriate. Do not create
another issue to reset an existing work item's limits.

## Read progress and recover a run

The issue timeline links commits on `nexkit/state`. Their short messages describe
the current requirement, agent task, published change or next approval step.
Summaries come from recorded work and validated agent reports, with no extra
model call. They are progress updates; passing checks and review decisions still
come from the corresponding reports. Approval notices keep the requested action
and a short result visible, with technical identifiers under "Approval details".

`nexkit status <issue>` reads persistent GitHub state. `cancel` posts an
authority-checked command; controllers prevent consequential operations that
have not started when they revalidate. Already-created commits, PRs or tags are
not rolled back. `resume` preserves approval and budget. Changing the spec needs
a new requirement approval. Exhausted budgets need an administrative decision;
the pipeline never resets them automatically.

Optional [stage approvals](stage-approvals.md) persist their reviewed evidence
before ending the originating workflow. `status` identifies the waiting gate and
reports expiry; `resume` wakes its continuation without granting approval or
resetting limits. A blocked rejection retains reviewer feedback for deliberate
recovery. An interrupted claim or repair dispatch can resume before new agent
work; recorded run attempts distinguish native reruns from still-active work.

Inspect the current Actions run before restarting after a lost connection.
Existing branches, PRs and release drafts are recovered. No empty commits or
artificial PR close/reopen cycles are used to retrigger checks. Results from a
different spec/config/base/head are invalid for the current candidate.

## Detailed recovery behavior

Local calls to `nexkit.ci materialize` require a standalone checkout with a real
`.git` directory. Use a fresh clone for local probes. Linked-worktree pointers
and `.git` symlinks are rejected before scratch files are created, so scratch
checkout/merge operations cannot follow those pointers back to the source.

Clarification persists one validated pending result before updating the issue.
A later preparation run can finish that publication and restore its bot notice
without another model reservation. If the issue was already updated, recovery
does not PATCH it again or change its edit timestamp. New input or accepted
setup changes supersede an unapplied result; cancellation and approval are
checked again before writing.

The final issue read is checked against the expected source, and collaborator answers
and authority are rechecked immediately before PATCH. GitHub does not generally
support conditional writes for unsafe REST methods, so this is not an atomic
compare-and-swap against concurrent manual issue edits. Prefer comments while
the bot clarifies; the current specification and edit time are revalidated for
requirement approval and before delivery. See GitHub's
[conditional request guidance](https://docs.github.com/rest/guides/best-practices-for-integrators).

Artifacts request seven days of retention. Hosted jobs use ephemeral runners;
subscription CLI jobs use a dedicated container with per-job workspace cleanup. State
keeps recent feedback, reservations and candidate identity. Do not copy full
transcripts into project knowledge. Uninstall preserves consumer source,
configuration, knowledge, GitHub data and locally edited kit files.

## Recover a partial release

Partial releases reuse their original artifact run. Resume with `/nexkit resume`
on the issue or `nexkit resume ISSUE` to start a new run. A native GitHub rerun
can leave published assets intact while the original Actions artifacts are
unavailable. Seven-day retention does not guarantee those originals survive
a rerun. If artifacts are missing or expired, or hashes/tags differ, the
pipeline stops and retains the draft for investigation. It never overwrites
assets or silently selects a new candidate.

Changed source transfers are limited to 200 text files and 2 MB. Existing large
or binary files are hashed for comparison; changed binaries, symlinks or submodules
cannot cross the publication boundary. Release artifacts are limited to 100 MB
per file. Managed jobs use Linux/x64 with Python 3.11+; subscription hosts provide
Ubuntu and a local system Docker Engine. Ubuntu 24.04 is the CI reference;
verify the required capabilities in the actual environment using the
[verification guide](acceptance.md). [Self-hosted operation](self-hosted.md) covers runner
startup, login, isolation, updates and removal. Other engines require a separate integration.

## Developing NexKit

The commands below are for changes to NexKit itself. Run them from the NexKit
checkout with Python 3.11+ and a Node version supporting `node:test`:

```sh
python3 -m unittest discover -v
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
python3 scripts/build.py
```

Unit/controller tests use explicitly labeled simulated GitHub boundaries.
Consumer tests execute a real Node CLI and Python HTTP server in temporary
repositories. They make no model calls and cannot prove a live GitHub workflow.
Live acceptance records issue/PR/run links separately.

### Repeat the live local CLI smoke

```sh
python3 scripts/local_agent_smoke.py \
  --implement-model YOUR_ACCESSIBLE_MODEL \
  --review-model YOUR_ACCESSIBLE_MODEL \
  --output dist/local-smoke-run-1
```

The script creates a temporary Git fixture, invokes independent implementer and
reviewer Codex sessions, checks observable skill reads, tests the actual CLI and
rejects source changes by the reviewer. Results, diff, CLI-reported usage and
elapsed time are written to the output directory. It uses the current account's
model allowance, with a default 180-second limit per session. It neither writes
to GitHub nor publishes releases. A usage-limit failure is a failed smoke, not a
passing verdict; inspect its event log before retrying with available allowance.
[Live GitHub acceptance](live-acceptance.md) is a separate procedure.
