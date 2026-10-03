# Versions and artifact identity

[Documentation](README.md) / Versions

NexKit **1.2.1** retains project and candidate **schema 1** from 1.0.0.
Select a published release explicitly. Release numbers identify artifacts;
configuration and workflow capabilities determine whether a project can use them.

| Value | Purpose |
|---|---|
| NexKit `1.2.1` | Plugin, Python package, CLI and published archive identification |
| Project `schema: 1` | Validate the accepted configuration structure |
| `kit.ref` | Full commit SHA of the trusted toolkit code and reusable workflows |
| `kit.version` | Identify the selected toolkit release in installation records |
| Agent CLI version | Optional installation selection and diagnostic metadata |
| Runner image ID | Bind administration to the immutable provisioned image |
| Runner `policy_directory` | Locate the installed sandbox files |

## Select a project release

With a local NexKit CLI from 1.1.0 or later, run these commands in an installed
consumer repository. Python 3.11+ and authenticated GitHub CLI (`gh`) are required
to read the accepted toolkit repository's published release and assets.

```sh
nexkit use --version 1.2.1 --dry-run
nexkit use --version 1.2.1
```

The first command shows file diffs; the second applies them locally. You can
select a lower release with the same command:

```sh
nexkit use --version 1.0.0
```

`v1.0.0` is also accepted. There is no automatic selection of `latest`, `main`
or an unreleased commit. The command reads the repository already recorded in
`kit.repository`, resolves the exact release tag and verifies its archive,
SHA-256 checksums, complete file manifest and clean source commit. Annotated tags
are resolved to their commit.

NexKit asks the selected toolkit to validate the project configuration in a
fresh process without model or GitHub credentials. It checks that each called
reusable workflow and its supplied/required inputs exist. A lower release can
be selected whenever those actual capabilities support the current project;
unsupported settings, missing workflows or unknown inputs stop before writes.
This command does not migrate project schemas.

The update records `kit.version` and the derived `kit.ref`, changes matching
reusable-job `uses:` and `kit_ref` values, refreshes accepted file hashes and
installs the selected release's project skills. Existing triggers, job order,
models, budgets, checks, approvals and consumer source are retained. Required
permissions are added only to the corresponding reusable-call job when the new
workflow needs them; the dry run includes those changes. Existing permission
grants remain in place when selecting a lower version.

Version selection supports literal pins and block mappings in accepted workflow
files. Computed pins, YAML aliases/flow mappings in inspected bindings, or copied
controller jobs need deliberate reconciliation through [setup](project-setup.md).
Run normal native workflow validation and PR checks before merging the update.
Edited accepted files or managed skills are preserved: reconcile them with setup
first. Consumer-owned workflows retain their ownership, including on uninstall.

Commit the resulting configuration, workflow and skill changes together. Earlier
run evidence and approvals do not authorize publication after a kit change;
use the existing [recovery procedure](operations.md#read-progress-and-recover-a-run).
Version selection does not reset work state or usage counters. Local plugin
installation and provisioned runner images remain separate administrative steps;
see [installation](getting-started.md) and
[runner maintenance](self-hosted.md#runtime-and-workflow-maintenance).

## Pin trusted source

Resolve `v1.2.1` to its full commit SHA before accepting initial setup. Both the
workflow `uses:` references and `kit.ref` must name that SHA. Examples contain
placeholders that setup must replace. For an installed project, `nexkit use`
performs that resolution and updates all literal reusable-job pins together;
editing only `kit.version` cannot change the code Actions executes.

Approvals, configuration, control files, checks and results belong to an exact
candidate. Keep their hashes and revisions: changing a trusted input requires
fresh verification. These identities enforce authority and reproducibility.

## Compatibility and exact identity

Runtime compatibility follows the [agent adapter contract](agent-adapters.md).
Core logic never compares the running CLI version with a selected or reference
release. The adapter checks required execution options; native subscription
sessions also exercise the filesystem, credential and network boundaries before
using a real login or calling a model. Missing capabilities stop the session.

A consumer may use any CLI release that satisfies those requirements. An
optional installation pin selects what to install; it does not become a runtime
compatibility condition. For example:

```json
{"engine": {"name": "codex", "auth": "api-key"}}
```

To deliberately select an installation artifact, add:

```json
{"engine": {"name": "codex", "auth": "api-key", "install": {"version": "1.2.3"}}}
```

`1.2.3` illustrates the field, not a recommended release. Without a pin, the
official API action chooses its upstream default and a new subscription image
installs the current npm release. Existing subscription runners keep their
immutable installed image until deliberately reprovisioned. CLI version output
is diagnostic; an absent or unfamiliar label does not block a capability probe.

The local toolkit need not have the same version label as the consumer's kit
pin. `doctor` reports that difference as a warning and validates the current
schema. This does not promise compatibility with arbitrary older formats.

## Build and verification references

[CI](../.github/workflows/ci.yml) exercises one checksum-verified reference CLI
per integration, across the supported native platforms. The reference lives in
[the verification installer](../scripts/install_codex.py). There is no matrix of
CLI releases or list of permitted consumer versions. Adapter contract tests
cover core delegation and failure behavior without installing multiple CLIs.

The [runner Dockerfile](../runner/Dockerfile) and
[development requirements](../requirements-dev.txt) contain build dependency
pins. Overriding Actions Runner or GitHub CLI artifacts requires their matching
SHA-256 checksums. Provisioning accepts `--image` for a reviewed local image,
checks its capabilities and records its immutable ID. Login reuses that image
and the recorded `policy_directory`, without comparing version labels.

Managed Linux jobs require x64 and Python 3.11+. Subscription hosts require
Ubuntu, systemd and a local Docker Engine with the documented isolation
capabilities. Ubuntu 24.04 is the CI reference; host admission records the
actual release and checks capabilities. See [current support](support.md) and
[native verification](live-acceptance.md#native-checks-omitted-by-the-shared-suite).
