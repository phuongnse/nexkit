# Protected administrative installation and updates

[Documentation](README.md) / Administrative setup

Use this path when branch protection requires `NexKit verification` and
`NexKit review` for changes to accepted configuration, workflows or installed
skills. Ordinary source delivery refuses these changes. A sole maintainer can
approve the resulting bot-authored PR through GitHub's native review mechanism.
An administrator's proposal push authorizes preparation and bounded independent
review; merging still requires an actual administrator's approval of the exact PR
head and all existing native branch requirements.

## Prepare the proposal

Start from the committed, current default branch of the consumer. Prepare the
proposed schema-1 project and its workflow/control bundle as described in
[project setup](project-setup.md). Select an immutable kit commit containing
`administration.yml`. The selected pipeline must have an accepted reviewer:
either explicit `models.review` or one `review` invocation. For an update, the
old accepted configuration selects the reviewer integration. Initial setup uses
the proposed configuration.

```sh
nexkit --pipeline maintenance administration \
  --config /path/to/proposed-project.json \
  --bundle /path/to/proposed-bundle \
  --review-minutes 10 --review-calls 2 \
  --out /tmp/nexkit-setup-proposal --online
```

The output directory must be fresh and outside the checkout. This command
previews the actual installer, ownership/removals, project digest, ledger and
runner impact. It produces `.nexkit/administration/proposal.json` and an exact
bootstrap workflow. It does not install into the consumer, spend issue budgets,
push a branch or approve anything. Optional `--include-doc docs/setup.md`
includes a prepared explanation from the checkout.

`--online` checks the current default branch, accepted controls, native rules,
Actions PR creation permission and registered reviewer runner labels. It does
not establish live model access. Unsupported additional required checks or
rules block preflight until an explicitly accepted integration/migration exists.
Native approval-count changes need a separately accepted ruleset
migration; this path preserves the current count and never grants bypass.

Review the complete proposal JSON and output. Then the repository administrator
creates the displayed `nexkit/setup-HASH_PREFIX` branch from the recorded base,
copies **only those two generated files**, commits them and records its full SHA.
For example, substitute the actual branch and base reported by the command:

```sh
git switch --create nexkit/setup-HASH_PREFIX FULL_BASE_SHA
mkdir -p .nexkit/administration .github/workflows
cp /tmp/nexkit-setup-proposal/.nexkit/administration/proposal.json .nexkit/administration/proposal.json
cp /tmp/nexkit-setup-proposal/.github/workflows/nexkit-administration.yml .github/workflows/nexkit-administration.yml
git add .nexkit/administration/proposal.json .github/workflows/nexkit-administration.yml
git commit -m 'Propose NexKit administrative setup'
git rev-parse HEAD
```

The administrator pushes this branch after any required runner admission below.
The exact `push` workflow runs even before the consumer has a default-branch
administration workflow, avoiding a first-install `workflow_dispatch` dependency.
GitHub policy must allow the administrator's proposal workflow to run.

## Subscription runner admission

Subscription runners normally admit accepted default-branch workflows only.
An administrator can temporarily admit this exact proposal and staging commit:

```sh
python3 scripts/manage_runner.py stop --config /path/to/accepted-project.json --pipeline maintenance
python3 scripts/manage_runner.py admit-administration \
  --config /path/to/accepted-project.json --pipeline maintenance \
  --proposal /tmp/nexkit-setup-proposal/.nexkit/administration/proposal.json \
  --proposal-sha FULL_STAGING_SHA --expires-minutes 120
```

Review the preview, then repeat the admission command with `--apply` and start
the runner. Use `--invocation REVIEW_ID` if the reviewer selects a distinct
runner. On a Windows host, also select its `--wsl-distribution`; use `run` from
Windows instead of Linux `start`.

Applying admission requires a stopped service, its recorded immutable image and
an actual credential-free capability probe of that image. Admission accepts
only the generated workflow, repository, proposal branch, push event and exact
SHA before expiry. It preserves registration, image selection and login data.
An older image lacking this capability blocks explicitly. Upgrade it through
the existing [runner maintenance procedure](self-hosted.md#execution-and-maintenance)
and verify it before admitting administrative work; an installed plugin alone
cannot upgrade a runner image. Initial subscription setup also needs separately
provisioned and authenticated infrastructure.

## Review and merge

Push the staging branch as the actual administrator. Hosted control jobs create
the exact candidate tree and bot-authored PR. Administrative verification checks
the base/tree, configuration contract, control hashes, ownership ledger and
installed skills from the selected kit. It reports its administrative scope;
it does not claim application test or E2E execution.

A fresh read-only agent session independently reviews the actual diff and
`ADM1`–`ADM3` criteria, including authority, credentials, preserved checks,
in-flight usage and runner impact. Candidate setup scripts are not executed.
Write-enabled jobs consume validated results without executing consumer code.

The administrator inspects the bot PR and submits an actual GitHub approval on
its exact head. Rerun the staging workflow after approval. A validated review
is reused for the same candidate without another model call, and GitHub's
native protection decides whether the merge is allowed. Changed base, proposal,
authority or candidate blocks; prepare a fresh reviewed proposal. A closed PR
is not recreated. Interrupted creation/merge recovers the owned branch, PR and
recorded evidence; bounded review reservations remain spent on failure.

After merge, update the local checkout and inspect `nexkit doctor --online --checks`.
For each affected subscription runner, stop it, preview and apply:

```sh
python3 scripts/manage_runner.py rebind \
  --config /path/to/new-accepted-project.json --pipeline maintenance
```

Repeat with `--apply`, then restart. This refreshes accepted workflow admission
and removes temporary administrative admission while preserving registration
and login. Existing issue identities, calls, attempts and elapsed usage are
retained. Changed specification/configuration/base invalidates earlier candidate
evidence and checkpoints; inspect them before resuming work.
