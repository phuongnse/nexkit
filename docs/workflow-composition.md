# Consumer-owned workflows

The consumer's native GitHub Actions YAML owns triggers, jobs, dependencies,
matrices and additional stages. NexKit does not read a JSON stage graph or
schedule those jobs. Setup composes workflows from the actual repository and
accepted decisions; there is no catalog of project types.

## Current implementation boundary

Schema 2 supports arbitrary pipeline names, per-pipeline settings, explicit
entrypoint routing, accepted workflow/control files and installation bundles.
The existing `intake.yml`, `clarify.yml`, `delivery.yml` and `release.yml` reusable
workflows accept an optional `pipeline` input and remain compatibility adapters.
Their internal job structures are still fixed. For consumer-composed delivery,
`prepare-work.yml`, `agent-invocation.yml`, `publish-candidate.yml`,
`candidate-check.yml` and `finish-work.yml` supply individual reusable jobs.
Consumer YAML selects their order, conditions and inputs. See
[individual agent invocations](agent-invocations.md) for configuration and wiring.
Optional [stage approvals](stage-approvals.md) add issue or PR decisions through
request/resume capabilities and native continuation workflows. They do not define
a graph or fixed list of stages in the plugin.
These capabilities have local controller, command and workspace tests plus
static workflow validation. No schema-2 live delivery acceptance is claimed.

Schema 1 continues to use its existing settings, accounting and four generated
wrappers. Existing work items are not automatically migrated.

## Configuration contract

The root schema-2 object contains exactly:

| Field | Meaning |
|---|---|
| `schema` | `2` |
| `repository`, `default_branch`, `kit` | Shared identity and immutable kit pin, as in schema 1 |
| `defaults` | Explicitly accepted common settings from the configuration reference |
| `pipelines` | Map from consumer-chosen identifiers to the objects below |
| `files` | Project-wide map of accepted repository paths to `{ "sha256": "…", "managed": true/false }` |

Each pipeline requires `settings`, `entrypoints` and `agent_workflows`, with an
optional `invocations` map for individually reserved agent calls and an optional
`approvals` map for configured human decisions:

```json
{
  "maintenance": {
    "settings": {
      "models": {"review": "the-projects-review-model"},
      "limits": {"attempts": 2},
      "knowledge": ["docs/architecture.md"]
    },
    "entrypoints": {
      "intake": ".github/workflows/capture.yml",
      "clarify": ".github/workflows/discuss.yml",
      "delivery": ".github/workflows/change.yml"
    },
    "agent_workflows": [
      ".github/workflows/discuss.yml",
      ".github/workflows/change.yml"
    ]
  },
  "audit": {
    "settings": {},
    "entrypoints": {},
    "agent_workflows": []
  }
}
```

This is the `pipelines` member, not a full project configuration. The chosen
names and number of pipelines are unrestricted by a stage catalog. The four
optional entrypoint keys are NexKit command integrations; they do not describe
the workflow's jobs or their order. Omitted integrations stay absent. A native
audit pipeline can have no NexKit entrypoints, models or release settings.
A clarification-only pipeline needs its implementation model, engine,
environment and clarification budget, but no reviewer or delivery limits.
Setup commands still require an explicit `limits.command_seconds`.

Settings maps inherit recursively from `defaults`; arrays replace the complete
previous array. Scalars replace the previous value. `null` is a value, not a
deletion instruction: only a field explicitly supporting null accepts it.
Identity, file ownership and entrypoints cannot be overridden through settings.
Resolved settings retain the pipeline identity and the source configuration
digest. The candidate identity includes that resolved configuration.

## Installation and ownership

Prepare the actual workflows in a separate directory, retaining repository
paths, and hash their exact bytes into `files`. Do the same for trusted local
prompts, schemas, actions or scripts used to control a candidate. A trusted file
already owned by the consumer uses `managed: false`; accepting its hash does not
authorize deletion or overwrite. Installer-managed files are limited to direct
children of `.github/workflows/` and files under `.nexkit/controls/`.

```sh
nexkit install --config /path/to/accepted-project.json --bundle /path/to/bundle --host codex
nexkit install --config /path/to/accepted-project.json --bundle /path/to/bundle --host codex --apply
nexkit doctor --online --checks
```

The preview includes file additions, updates, ownership adoption, removals and
the configuration change. Apply writes the accepted schema-2 configuration with
the bundle and ledger. Use a separate proposed configuration for updates; do not
edit the installed configuration behind its recorded installation. All hashes,
paths and conflicts are checked before mutation. This is not a filesystem-wide
transaction across an OS crash; inspect and reconcile an interrupted install.

Updates can remove obsolete, unchanged, previously managed workflows. Edited
obsolete files stop the update before other writes. A file transferred to
consumer ownership remains on disk and leaves the ledger. A shared workflow
stays while it remains in the accepted manifest. Uninstall preserves consumer
files, configuration, knowledge and GitHub data. A changed installation ledger
alone cannot adopt an arbitrary workflow for removal.

## Routing and execution provenance

Select a pipeline explicitly when submitting new work:

```sh
nexkit --pipeline maintenance request --title "Describe the change" --body-file request.md
```

The intake workflow must accept the native `pipeline` dispatch input and forward
it to the reusable adapter. Issue-driven callers pass the chosen pipeline as a
literal input. Work items bind that identity in the issue body, which is included
in the exact human approval hash. Resume uses the recorded binding; retry uses
its declared delivery workflow. Release candidates also bind their pipeline.
Identical request keys in different pipelines do not reuse each other's issue.

At preparation, controllers check the executing caller path and workflow SHA,
the source configuration and actual committed bytes of all accepted controls.
Revalidation checks accepted controls at the immutable source revision. A
different pipeline or control/configuration drift cannot spend a model call or
reuse a candidate. Delivery cannot publish workflow, local action or accepted
control changes; those belong to deliberate setup.

The runner's credential-bearing workflow allowlist comes from explicit
`agent_workflows`, never a wildcard. Provisioning preview shows it. Native jobs
executing untrusted consumer code still need separate runners/jobs and restricted
permissions. Workflow syntax, pinned external dependencies and permission
boundaries must be reviewed and verified during setup; a file hash alone does
not establish that a workflow is correct. `doctor` reports native execution as
unverified when it has no declared commands to execute.

The caller and revision checks use GitHub's documented
[workflow variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables).
Reusable jobs follow GitHub's native
[workflow reuse contract](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

## Reusable candidate check

`candidate-check.yml` runs one command selected by its accepted check name.
The consumer chooses its job name, dependencies and placement in native YAML.
It accepts `kit_repository`, `kit_ref`, `candidate_artifact_id` and `check`.
The kit reference must match the project's immutable pin. Pass the artifact ID
output directly from the trusted publication job that produced `candidate.json`;
do not find a candidate by an artifact-name wildcard or by "latest run". The
shared download action requires an exact numeric ID and one controller JSON
file. Missing IDs and extra files block the job.

The capability validates the current run attempt, approval, work-item candidate,
configuration and selected command before checking out or executing candidate
code. It uses a hosted runner, a separate unprivileged account, read-only GitHub
permissions and no model credential. It returns `passed` and
`report_artifact_id`. A successful artifact upload is not a passing check.
Setup failures remain failed, explicitly unexecuted checks with their real logs.

After downloading each report by the exact ID returned by the corresponding
required job, aggregate its files with:

```sh
python3 -m nexkit.ci combine-checks --context candidate.json --reports checks/unit.json checks/api.json checks/lint.json --out verification.json
```

Those filenames illustrate consumer-chosen check names. Every configured check
must appear exactly once, with the current candidate, run attempt and declared
kind. Missing, duplicate, unexpected or stale results block aggregation. Every
matrix shard needs its own configured identity and report; one final matrix
output cannot stand in for all shards. The caller must depend on all required
producer jobs and consume their outputs, not outputs from an unrelated job.
Additional advisory stages can stay outside the required report set.

The merge guard also checks the complete configured set for the compatibility
adapter, so passing test/E2E records cannot hide a missing lint/build command.
The independent review gate remains mandatory for automatic merge. This
capability has local command tests, mocked controller tests and static workflow
validation; it has not yet been exercised as a live consumer job.
