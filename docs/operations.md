# Verification and recovery

Run the checks with Python 3.11+ and a Node version supporting `node:test`:

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
resetting limits. A blocked rejection retains human feedback for deliberate
recovery. An interrupted claim or repair dispatch can resume before new agent
work; recorded run attempts distinguish native reruns from still-active work.

Inspect the current Actions run before restarting after a lost connection.
Existing branches, PRs and release drafts are recovered. No empty commits or
artificial PR close/reopen cycles are used to retrigger checks. Results from a
different spec/config/base/head are invalid for the current candidate.

Clarification persists one validated pending result before updating the issue.
A later preparation run can finish that publication and restore its bot notice
without another model reservation. If the issue was already updated, recovery
does not PATCH it again or change its edit timestamp. New input or accepted
setup changes supersede an unapplied result; cancellation and approval are
checked again before writing.

The final issue read is checked against the expected source, and human answers
and authority are rechecked immediately before PATCH. GitHub does not generally
support conditional writes for unsafe REST methods, so this is not an atomic
compare-and-swap against concurrent manual issue edits. Prefer comments while
the bot clarifies; the current specification and edit time are revalidated for
requirement approval and before delivery. See GitHub's
[conditional request guidance](https://docs.github.com/rest/guides/best-practices-for-integrators).

Artifacts are retained for seven days. Hosted jobs use ephemeral runners;
subscription CLI jobs use a dedicated container with per-job workspace cleanup. State
keeps recent feedback, reservations and candidate identity. Do not copy full
transcripts into project knowledge. Uninstall preserves consumer source,
configuration, knowledge, GitHub data and locally edited kit files.

Partial releases reuse their original artifact run within that retention period.
If artifacts expire or hashes/tags differ, the pipeline stops and retains the
draft for investigation. It never overwrites assets or silently selects a new candidate.

Changed source transfers are limited to 200 text files and 2 MB. Existing large
or binary files are hashed for comparison; changed binaries, symlinks or submodules
cannot cross the publication boundary. Release artifacts are limited to 100 MB
per file. GitHub-hosted Ubuntu 24.04 and the pinned Ubuntu 24.04 container runner
are integrated; their different live verification status is recorded in
[acceptance](acceptance.md). [Self-hosted operation](self-hosted.md) covers runner
startup, login, isolation, updates and removal. Other engines require a separate integration.

## Repeat the live local CLI smoke

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
