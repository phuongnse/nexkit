# Interrupted work and additional issue budget

[Documentation](README.md) / Work recovery

An interrupted agent may leave useful source edits or a partial analysis before
its final JSON exists. NexKit saves bounded public checkpoints to Actions
artifacts independently of successful result collection. They are untrusted
work to recheck in a fresh session. They never satisfy checks, independent review,
requirement approval or merge authority.

## What is retained

Agent jobs periodically capture regular text source changes for `deliver`, and
public handover text for request/task/review roles. A handover can be a JSON
object at the prompted `output/handover.json` path with text fields `completed`,
`remaining`, `blockers`, `draft` and `verification`. Without it, a bounded complete
public progress message may supply partial context. Private reasoning, raw
logs, credentials, links, binary changes and administrative edits are excluded.

Completed `agent-checkpoint-*` artifacts contain a manifest and canonical payload
with hashes and the exact owned context. A newer failed/partial upload does not
replace an older complete artifact. State records artifact IDs, capture cadence,
time, retention and restore status, not a private workspace or model transcript.
Diagnostics and successful result artifacts remain separate.

Recovery verifies the actual native run attempt, accepted caller/control files,
kit pin, reservation and artifact identity. Preparation can discover a completed
attempt's artifacts even when its finalizer never ran. Restoration binds the
repository, issue edit/specification, configuration, execution settings, role,
invocation, base/source and input reports. New native retry IDs alone do not
change otherwise identical task findings. Changes to real input do.

Only a complete checkpoint can restore into a fresh workspace. Conflict hashes
preserve prior candidate edits, reverts, deletions and later interruptions. The
agent receives the current requirement and feedback and must recheck the work.
Fresh checks and independent review remain required before publication/merge.
Consumption retires an owned checkpoint only after a validated complete result.

## Settings and limits

Accepted defaults or pipeline settings can include:

```json
{
  "recovery": {
    "enabled": true,
    "checkpoint_seconds": 60,
    "max_checkpoints": 6,
    "retention_days": 7
  }
}
```

These are also the defaults when omitted. Bounds are 10–3600 seconds, 2–8 upload
attempts per agent job, and 1–90 retention days; GitHub retention policy also
applies. Effective cadence is the larger of the requested interval and call
timeout divided by `max_checkpoints - 1`. A slot is reserved for a best-effort
final upload, and slots remain for the normal report and diagnostics. Checkpoint
transport receives artifact credentials only, without model or GitHub write
credentials. Source commands do not receive artifact transport credentials.

A monitor waits the effective cadence after each capture/upload attempt. Snapshot
time and upload time add to this interval; each upload has at most 45 seconds.
Work after the last captured snapshot remains at risk until a newer upload
completes. Before the first complete upload, or if every capture/upload fails,
the entire interrupted call's unpublished work can be lost.

A hard runner loss can lose work since the last completed upload. Storage/network
failure, unsafe/oversized data or expired/deleted artifacts can make recovery
unavailable. Status reports that limitation; it cannot promise a final tail
checkpoint. Upload failure preserves an earlier valid checkpoint. Disabling
recovery does not turn an interrupted call into successful work.

## Inspect and resume

From a consumer checkout:

```sh
nexkit status 42
nexkit recovery 42
nexkit resume 42 --dry-run
```

These commands inspect current accepted GitHub configuration even with an older
local checkout. Discovery and dry-run do not write state or dispatch work. Dry-run
projects pending actual budget/discard decisions and reports current retention
and scope; complete invocation inputs and payload are validated before execution.
Inspect the recorded artifact and Actions attempt before deciding to continue.

If budget remains and the blocker is resolved, `nexkit resume 42` wakes the
configured work entrypoint. Cancellation and approvals still apply. New calls
consume remaining budget; `retry: never` continues to prevent unsafe step replay.

A changed or unusable checkpoint requires an explicit decision before starting
from published source. `nexkit recovery 42 --discard` prints an exact
`/nexkit discard-checkpoint HASH` command. An actual current repository
administrator posts it after inspecting the work. The controller archives the
receipt/history and preserves consumption. A stale, edited or unauthorized
decision cannot discard current work.

## Continue after exhaustion

Exhaustion stops automatic work. After reviewing progress and remaining scope,
an administrator can grant bounded additional capacity **on the same issue**:

```sh
nexkit budget 42 --agent-calls 2 --attempts 1 --minutes 30
```

For a separate clarification-count cap, use `--clarification-calls 1`. At least
one positive addition is required. Per-decision maxima are 40 calls, 20 attempts,
1440 elapsed delivery minutes and 1000 clarification calls. Adding capacity to
an already uncapped field does not create a new cap. Decision history has a
bounded issue-state size and cannot be dropped to evade consumption.

The command prints the exact `/nexkit budget HASH {…}` comment. The actual
repository administrator posts it, then inspects `resume --dry-run` and resumes.
The grant binds the current issue edit/spec, configuration, pipeline, kit,
candidate/checkpoint, work identity, consumption and earlier grants. It is applied
once. Changed/deleted approval comments or revoked administrator access block
further execution. Changed accepted inputs retire old grants into history.
If discarding a checkpoint, apply that decision before preparing a budget grant
for the new subject.

Original calls, attempts and elapsed execution remain spent. Only the recorded
interval waiting on exhausted budget is excluded from the delivery clock.
Status and budget previews exclude grants tied to earlier accepted inputs without
changing stored history, so a new decision can apply after a configuration update.
A grant does not extend a call's timeout, alter configured models, authorize source
scope changes or approve a merge. Use [administrative setup](administration.md)
when changing the accepted per-call timeout or other configuration. Consumer
comment workflows must route budget/discard commands to the same accepted work
entrypoint; broad `issue_comment: types: [created]` examples already do this.
