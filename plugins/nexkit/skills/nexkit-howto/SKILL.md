---
name: nexkit-howto
description: Explain installation, setup, the first requirement, configured human decisions and where to find NexKit results in the current host.
---

# NexKit howto

Run `nexkit howto` and read the installed kit README for installation commands.
Use the host's real skill selector: in Codex, type `$` and select
`nexkit:nexkit-init` from the native plugin, or `$nexkit-init` for directly installed
project skills. The Claude Code plugin uses `/nexkit:nexkit-init`. For other hosts,
consult the kit compatibility matrix. Skills installed locally do not install the runner.

Explain the relevant next user action in the current project: setup, submit a
requirement, review its issue, inspect delivery, or select a release candidate.
The default human decisions are requirement approval and release. Read the
project's actual approval settings before explaining any additional stage gates.
After requirement approval, Actions drives implementation, independent review,
verification, repairs and merge without the local session, pausing at configured
human decisions. For issue gates, show the bot's exact command for that checkpoint;
for PR gates, explain GitHub Approve and Request changes. Ordinary "approved"
comments do not grant authority. Waiting retains no runner or model call.
See `docs/stage-approvals.md` for the configuration and event flow. Setup permissions and
policy changes are administrative work. Cite actual doctor/run results when
describing readiness, and distinguish installation from tested live operation.
