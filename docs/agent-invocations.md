# Individual agent invocations

Schema 2 can bind consumer-chosen agent calls without defining a stage graph in
JSON. Native GitHub Actions YAML decides which calls run and when. NexKit
reserves their usage, installs accepted methods and validates their output.

## Accepted configuration

Add `invocations` to a pipeline with a `delivery` entrypoint. Each key is a
consumer-chosen identifier, unique within that pipeline. For example:

```json
{
  "investigate": {
    "contract": "task",
    "model": "the-projects-analysis-model",
    "minutes": 5,
    "task": ".nexkit/controls/investigate.md",
    "skills": [".nexkit/controls/skills/domain-check/SKILL.md"]
  },
  "change-source": {
    "contract": "deliver",
    "model": "the-projects-coding-model",
    "reasoning_effort": "max",
    "minutes": 15,
    "task": ".nexkit/controls/change-source.md"
  },
  "evaluate": {
    "contract": "review",
    "model": "the-projects-review-model",
    "minutes": 10,
    "task": ".nexkit/controls/evaluate.md"
  }
}
```

This illustrates the map; it imposes no job order or required analysis step.
Add or omit task calls according to the accepted workflow. The task file and
every skill/support file copied to an agent must be declared in the project's
accepted `files` manifest. They are fetched from the approved run's immutable
base and checked against their accepted SHA-256 hashes. Candidate code cannot
replace the kit methods or discover additional host-control directories.

| Field | Meaning |
|---|---|
| `contract` | `deliver` edits source; `review` independently assesses a published candidate; `task` performs a read-only task and returns context |
| `model` | Explicit accessible model for this call |
| `reasoning_effort` | Optional supported effort; omission uses the CLI model default |
| `minutes` | Explicit CLI timeout, 1–60 and no greater than the pipeline delivery deadline |
| `task` | Accepted UTF-8 task file, up to 64 KB |
| `skills` | Optional accepted `SKILL.md` paths; accepted support files in the same skill directory are copied too |
| `agent_runner` | Optional override of `environment.agent_runner` |
| `setup` | Optional replacement of `environment.setup` argument arrays |

The pipeline selects engine version, authentication and total limits. A delivery
pipeline with invocations does not need dummy global implementation/review models.
If it also clarifies issues, that integration still needs its implementation
model and optional reasoning setting. `doctor` shows each resolved invocation.
To preview a distinct subscription runner override, use
`python3 scripts/provision_runner.py --config accepted-project.json --pipeline maintenance --invocation change-source`.
Only explicitly accepted runner provisioning uses `--apply`.
Setup changes invalidate previous candidate evidence through the config digest.

Contracts describe permissions and machine-readable outputs. They do not name
pipeline stages. Automatic source delivery still needs an editor and a separate
reviewer. A `task` result cannot become a merge verdict. Additional reviewers
or source-editing calls may be declared under distinct identifiers.

## Reusable jobs

All capabilities take `kit_repository` and a full immutable `kit_ref`. The
selected kit must match the accepted project pin. Caller workflows execute from
the default branch and are themselves accepted control files.

| Workflow | Main inputs | Outputs |
|---|---|---|
| `prepare-work.yml` | `pipeline`; the event supplies the issue | `ready`, `context_artifact_id` |
| `agent-invocation.yml` | `context_artifact_id`, `invocation`; optional `input_artifact_ids`, `check_artifact_ids` | `context_artifact_id`, `report_artifact_id`, `status` |
| `publish-candidate.yml` | The editor's reserved `context_artifact_id` and recorded `report_artifact_id` | `candidate_artifact_id`, `head` |
| `candidate-check.yml` | `candidate_artifact_id`, configured `check` name | `report_artifact_id`, `passed` |
| `finish-work.yml` | Original round context, optional candidate/check/review artifact IDs, `jobs_succeeded` | Persisted `status`: `merged`, `retry` or `blocked` |

`input_artifact_ids` and `check_artifact_ids` are comma-separated exact IDs from
the corresponding native `needs` job outputs. Each report artifact contains
one JSON file. Multiple downloads keep separate directories, preventing two
reports from overwriting each other. Missing IDs, duplicate IDs and unexpected
file counts block consumption. An empty optional list downloads nothing.
The pinned upstream download action's [source](https://github.com/actions/download-artifact/blob/634f93cb2916e3fdff6788551b99b062d0335ce0/src/download-artifact.ts)
only warns about partially missing IDs; NexKit checks completeness explicitly.

The [wiring example](examples/composed-delivery.yml) demonstrates native
dependencies and failure handling. Replace its placeholder commit, pipeline,
invocation names and check names with accepted consumer decisions. It is a
connection example, not an installed project preset. Setup creates the actual
workflow from the repository's needs.

Use workflow-level `concurrency: {group: nexkit-work, queue: max}` to serialize
work against the shared integration branch. The individual reusable workflows
do not acquire this lock separately. The finalizer must use `always()` and
depend on every required job, including preparation. Pass its original round
context even if no candidate was published; this allows recording a failure
and dispatching a bounded retry. Compute `jobs_succeeded` from native job
results and require `status == 'done'` for each required agent task. A blocked
task can successfully record its report; job success alone does not make that
task complete. Do not let an agent supply the finalizer's boolean.

Checks and review may run after publication in the order chosen by the caller.
The review invocation consumes all configured command reports for its current
candidate. The finalizer requires the same recorded review and command results.
The model verdict is additional evidence; the controller still validates exact
approval, source, configuration, required check completeness and branch rules.

## Usage and isolation

Preparation reserves one delivery round. Each selected invocation reserves one
CLI call before starting; unused optional calls cost no reservation. Failed
calls retain their reservation. The same invocation ID can run once per run
attempt; reruns cannot reuse an older reservation. Total calls, rounds and
elapsed time persist across automatic retries. Separate clarification settings
keep human conversation accounting independent when selected during setup.

Each call has a fresh workspace and CLI session. Task/review calls are read-only.
An editor must finish and publish before another editor starts. New source
invalidates earlier review. All started calls must finish before merge. The
invocation reservation has a five-minute allowance for job setup and collection
around the configured CLI timeout, bounded by the overall delivery deadline.

The authorization and recording jobs run on hosted controllers with state-write
permission. The actual agent job has read-only GitHub permission and its selected
model credential. Publication and merge run separately, without executing source
code. Candidate checks run without a model credential on hosted runners. The
ChatGPT runner remains owned, provisioned and authenticated by each consumer.

`task` uses the installed `nexkit-task` skill and a small common result schema:
status, summary, skills used, commands and limitations. Its summary supplies
findings to later calls. Consumer tasks/skills cannot alter approval, publish
controls or output contracts. This version accepts text skill assets only and
does not load custom CLI engines or arbitrary output schema interpreters.

These capabilities have local functional tests with mocked GitHub/CLI boundaries,
real workspace restoration tests and static YAML validation. Live composed
delivery on a consumer is still required. Existing schema-1 work items keep
their accepted adapter and budget until an explicit setup migration.
