# Versions and artifact identity

[Documentation](README.md) / Versions

NexKit is released as **1.0.0** with project and candidate **schema 1**.
There is one current format. This release does not provide migrations or
compatibility paths for earlier formats.

| Value | Purpose |
|---|---|
| NexKit `1.0.0` | Plugin, Python package, CLI and published archive identification |
| Project `schema: 1` | Validate the accepted configuration structure |
| `kit.ref` | Full commit SHA of the trusted toolkit code and reusable workflows |
| `kit.version` | Identify the selected toolkit release in installation records |
| Agent CLI version | Optional installation selection and diagnostic metadata |
| Runner image ID | Bind administration to the immutable provisioned image |
| Runner `policy_directory` | Locate the installed sandbox files |

## Pin trusted source

Resolve `v1.0.0` to its full commit SHA before accepting setup. Both the
workflow `uses:` references and `kit.ref` must name that SHA. Examples contain
placeholders that setup must replace.

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
