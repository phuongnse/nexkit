# Supported platform

[Documentation](README.md) / Support scope

NexKit **1.1.0** supports **GitHub and Codex**, using the single stable
[schema-1 format](versions.md).

| Part | Supported choice |
|---|---|
| Repository, issues and PRs | GitHub |
| Pipeline execution | GitHub Actions |
| Interactive setup and requests | Codex CLI with the NexKit plugin or installed project skills |
| Pipeline agent | Official Codex CLI |
| Model authentication | OpenAI API key, or the dedicated Codex subscription runner |

For installation, follow [guided setup](getting-started.md) in Codex or
[manual setup](manual-setup.md) with the NexKit CLI. Model, reasoning, pipeline
structure, usage limits and approval steps remain project choices.

## Installation

The Codex plugin uses `plugins/nexkit/.codex-plugin/plugin.json` and the bundled
marketplace. The project installer copies skills into `.agents/skills/` by
default. Existing commands with `--host codex` continue to work; Codex is the
only accepted host value.

In Codex, type `$` and select `nexkit:nexkit-init` for the plugin, or
`$nexkit-init` for directly installed project skills. Start a new Codex session
after updating the installed plugin so it can load the changed skills.

## Pipeline execution

GitHub Actions invokes the official Codex CLI. Each project pins trusted
NexKit source and selects an available model. A CLI installation pin is optional. Installing the plugin locally
does not configure the pipeline's runner or model access.

API mode uses the repository's `OPENAI_API_KEY` secret. Subscription mode uses
the dedicated Linux or Windows runner and Codex login described in the
[runner guide](self-hosted.md). Public-repository subscription mode is an
experimental integration; its support boundary is documented there.

### Linux and Windows

Version 1.1.0 uses one Linux runtime for managed jobs:

| Scope | Linux | Windows |
|---|---|---|
| Managed agent/check/command/release jobs | Linux/x64, Python 3.11+, explicit Ubuntu release label | Linux container inside Ubuntu on WSL 2 |
| Subscription runner host | Ubuntu x64 with Docker Engine and systemd | Windows x64, WSL 2 and Ubuntu with Docker Engine; Docker Desktop optional |
| Runner lifetime | systemd | Windows supervisor keeps WSL and its Linux service running |
| Application-specific native jobs | Ordinary Linux workflow jobs | Ordinary Windows workflow jobs, recorded through project steps |
| Local CLI | `bin/nexkit` | `bin/nexkit.cmd`, or `python bin/nexkit` |

Verify the runtime on the actual host before making a production claim.
Ubuntu 24.04 is the CI reference environment. Host admission checks the local
Engine, systemd, architecture and isolation capabilities rather than requiring
that exact Ubuntu version. Other releases need their own applicable native checks.
Starting without Windows sign-in after reboot needs its own lifecycle check
in that deployment's launch mode.

Model access, reasoning, budgets, approvals, result validation and retry rules
use the same project configuration. Managed commands must fit the Linux
toolchain. Portable local CLI contracts run on actual Linux and Windows.
Container checks on a Windows host establish its WSL runtime, not native
Windows application behavior. Use a Windows job to test the latter. ARM and
macOS are outside this version's scope.

See [Windows host setup](windows-runner.md) and the
[verification guide](acceptance.md).

## Verification requirements

The [verification guide](acceptance.md) explains how to distinguish:

- Native Codex installation and discovery of the packaged plugin skills.
- Live local Codex implementation and independent review.
- Live GitHub Actions clarification, delivery, checks, PR approval and merge.
- Local tests that simulate GitHub or model responses.

Each consumer may use any CLI release that satisfies the adapter contract.
Subscription provisioning accepts an optional installation pin; runtime checks
required options and the native sandbox before using the real login. CI uses
one reference CLI per integration and shared contract tests, without a matrix
of releases. Verify plugin commands and runtime boundaries when updating a CLI. See [compatibility and identity](versions.md#compatibility-and-exact-identity).

Keep verification results with their run or release, outside the source tree.
Each existing consumer keeps its accepted pin until a deliberate update.

The current source adds [protected administrative proposals](administration.md),
[public work checkpoints and issue budget grants](recovery.md). Their native
runtime and integration verification is reported with the specific candidate
run; an older published pin or runner image does not gain these capabilities
from a local plugin update.

Verify a small request after setting up each project. Installation and container
tests do not establish a complete live delivery with your project's model access,
workflow, checks and approvals. See the [verification procedure](live-acceptance.md).
