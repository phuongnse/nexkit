# NexKit

**Agents. Skills. One workflow.**

NexKit configures an AI SDLC from your goals and your repository. You approve a
requirement on GitHub; Actions runs coding-agent CLIs to implement, independently
review, test, repair and merge within your chosen limits. Release is a separate decision.

**Status: installation candidate under validation. Live AI delivery on Actions
has not yet been demonstrated.** See the [acceptance report](docs/acceptance.md).

## Install and set up

Requirements: Python 3.11+, Git, and an authenticated GitHub CLI. From the source
checkout or an extracted installation archive:

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

Use `$nexkit-init` in Codex or `/nexkit:nexkit-init` in the Claude Code plugin,
then describe your project goals and constraints. The agent surveys the actual
repository, presents the setup decisions and verifies the accepted configuration.
See [configuration and permissions](docs/configuration.md).

## Your first request

Use the `nexkit-request` skill. GitHub intake creates an issue, and Actions
continues requirement clarification after the local host closes. The CLI returns
an intake key and an `intake-status` command to find the issue. Answer questions
on GitHub. An authorized human reviews the specification and posts the exact
`/nexkit approve <hash>` comment shown by `nexkit approval <issue>`.

Track the issue, PR and Actions with `nexkit status <issue>`. Use `nexkit cancel
<issue>` or `nexkit resume <issue>` when needed. Delivery runs without your local
terminal and requires no separate plan, task, test or PR approval.

To prepare a release, run `nexkit release --commit <sha> --version <version>
--notes-file <file>`. An authorized human chooses that candidate and posts
`/nexkit release <hash>` on its issue. Merging never starts a release.

[Architecture](docs/architecture.md) · [Verification and recovery](docs/operations.md) ·
[Dependencies and sources](docs/sources.md) · [Live acceptance](docs/live-acceptance.md)
