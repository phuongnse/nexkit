---
name: nexkit-howto
description: Explain NexKit installation in Codex, GitHub project setup, the first requirement, configured approval steps and where to find results.
---

# NexKit howto

Run `nexkit howto` and read the installed kit README for installation commands.
In Codex, type `$` and select
`nexkit:nexkit-init` from the native plugin, or `$nexkit-init` for directly installed
project skills. Read `docs/support.md` in the selected kit source for its
supported installation and execution paths. Skills installed locally do not
install the runner.

Present setup as three separate steps: install the plugin, configure the
repository, and provision the runner/model access. Link the applicable runner
guide and `docs/operations.md` for Python loader, sandbox and session failures.
Explain that a project owning copied native jobs also maintains their runtime
and private-directory boundaries; updating local skills does not update an
accepted kit pin, workflow files or an existing runner image.

Explain the relevant next user action in the current project: setup, submit a
requirement, review its issue, inspect delivery, or select a release candidate.
For a requirement already on GitHub, explain `/nexkit start <pipeline>` or
`nexkit --pipeline <pipeline> start <issue>` after checking the installed intake
entrypoint. Keep the existing issue. See `docs/issue-intake.md` for the required
workflow connection and retry behavior.
Address the reader directly; name actions such as "requirement approval" and
"PR review" instead of labeling them "human" steps.
The default approval steps are requirement approval and release. Read the
project's actual approval settings before explaining any additional stage gates.
After requirement approval, Actions drives implementation, independent review,
verification, repairs and merge without the local session, pausing at configured
approval steps for delivery. A standalone `tasks` pipeline instead runs its
configured tasks and project steps, then reports completion without a PR.
Read the selected kit's `docs/project-steps.md` when explaining custom steps.
For issue gates, show the bot's exact command for that checkpoint;
for PR gates, explain GitHub Approve and Request changes. Ordinary "approved"
comments do not grant authority. Waiting retains no runner or model call.
See `docs/stage-approvals.md` for the configuration and event flow. Setup permissions and
policy changes are administrative work. Cite actual doctor/run results when
describing readiness, and distinguish installation from tested live operation.
