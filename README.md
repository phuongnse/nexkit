# NexKit

**Agents. Skills. One workflow.**

NexKit configures an AI SDLC from your goals and your repository. You approve a
requirement on GitHub; Actions runs coding-agent CLIs to implement, independently
review, test, repair and merge within your chosen limits. Release is a separate decision.

**Status: installation candidate under validation. Two live consumer deliveries
have merged; full acceptance remains incomplete.** See the [acceptance report](docs/acceptance.md).

## Install and set up

Requirements: Python 3.11+, Git, and an authenticated GitHub CLI with
`gh api --slurp` support (verified with 2.101.0). From the source checkout or an
extracted installation archive:

```sh
python3 scripts/build.py
export PATH="$PWD/bin:$PATH"
nexkit --version
```

In Codex, register the package directory with `codex plugin marketplace add
/absolute/path/to/nexkit`, then run `codex plugin add nexkit@personal`. In Claude
Code, use `claude --plugin-dir /absolute/path/to/nexkit/plugins/nexkit`.
The [host guide](docs/compatibility.md) distinguishes installation, interactive
use and CI engines. The NexKit installer can also place portable project skills.

In Codex, type `$` and select `nexkit:nexkit-init` from the installed plugin;
project skills installed directly use `$nexkit-init`. In the Claude Code plugin,
use `/nexkit:nexkit-init`. Describe your project goals and constraints. The agent
surveys the repository, presents the setup decisions and verifies the accepted configuration.
See [configuration and permissions](docs/configuration.md).

Setup defines the project's pipelines and native workflow structure. Compose
the [reusable capabilities](docs/agent-invocations.md) with consumer-chosen tasks,
skills, models and limits; pipeline count and job order are not fixed by NexKit.
Setup can also add [human approval at selected stages](docs/stage-approvals.md),
including native PR review, with project-specific reviewers and rejection behavior.

Each project selects its own runner and authentication. API mode uses an Actions
secret; [subscription mode](docs/self-hosted.md) uses a dedicated runner and an
official Codex ChatGPT login owned by that project. Installing the plugin gives
no access to another user's VPS, runner or account.

## Your first request

Use the `nexkit-request` skill. GitHub intake creates an issue, and Actions
continues requirement clarification after the local host closes. The CLI returns
an intake key and an `intake-status` command to find the issue. Ask questions or
give feedback in ordinary issue comments; the bot replies and updates the
requirement when needed, within the conversation settings selected during setup.
An authorized human reviews the specification and posts the exact
`/nexkit approve <hash>` comment shown by `nexkit approval <issue>`.
The hash identifies the requirement content; it is not the issue number.

Track the issue, PR and Actions with `nexkit status <issue>`. Use `nexkit cancel
<issue>` or `nexkit resume <issue>` when needed. Delivery runs without your local
terminal. By default, requirement and release are the human decisions. Additional
stage approvals follow the policy chosen during project setup.

To prepare a release, run `nexkit release --commit <sha> --version <version>
--notes-file <file>`. An authorized human chooses that candidate and posts
`/nexkit release <hash>` on its issue. Merging never starts a release.

[Architecture](docs/architecture.md) · [Verification and recovery](docs/operations.md) ·
[Dependencies and sources](docs/sources.md) · [Live acceptance](docs/live-acceptance.md)
