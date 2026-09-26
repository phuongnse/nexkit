# Components and provenance

Official documentation was checked on September 25, 2026 before choosing
interfaces. NexKit's Python runtime has no third-party dependency; it uses Git,
GitHub CLI and the consumer's actual commands.

| Component | Version or pin examined | License and decision |
|---|---|---|
| OpenAI Codex CLI | Local 0.156.1; registry 0.157.0 | Apache-2.0; the user pins CLI and selects models |
| openai/codex-action | `86365089eb2b84e0a8fb0717b304f8bdcb13b20e` | Apache-2.0; reuse the official CLI wrapper, credential proxy and safety controls |
| Agent Skills | Portable SKILL.md format | Original NexKit content, one shared methodology source |
| GitHub Spec Kit | v1.0.11 | MIT; evaluated, without adopting its lifecycle/template catalog |
| Superpowers | v6.4.1 | MIT; evaluated, without copying its methodology or approval flow |
| GitHub Agentic Workflows | v0.89.21 | MIT; no compiler/orchestrator dependency because Actions plus CLI supplies the required path |
| Ruff | 0.16.9 | MIT; development formatting/linting only |
| PyYAML | 6.0.3 | MIT; development workflow structure inspection only |
| actionlint | 1.7.12; release archive SHA-256 in the core CI workflow | MIT; native Actions syntax, expression and reusable-workflow validation during development |
| Claude Code CLI | 2.1.282 | Upstream commercial terms; installed for host verification, never redistributed |
| GitHub Actions runner | 2.337.0; release archive SHA-256 in `runner/Dockerfile` | MIT; official repo-scoped listener and job hooks |
| GitHub CLI | 2.101.0; release archive SHA-256 in `runner/Dockerfile` | MIT; official repository operations |
| Node / Ubuntu runner images | 24.18.0 / 24.04; OCI digests in `runner/Dockerfile` | Upstream component licenses; base runtime packages |
| Moby seccomp profile | `85e237f1fe229a0c61c9c7d8e743fa780d3b97ca` | Apache-2.0; default profile plus six namespace/mount syscalls; license in `runner/third-party` |

Checkout/upload/download Actions use full commit pins. A kit update changes its
version/pin through a reviewable diff and verification; consumers never silently
follow upstream main. NexKit's own distribution license has not been selected
by the owner; no public package or release has been published.

API and authentication sources:
[Codex non-interactive execution](https://developers.openai.com/codex/noninteractive),
[Codex authentication](https://developers.openai.com/codex/auth),
[official action security](https://github.com/openai/codex-action/blob/86365089eb2b84e0a8fb0717b304f8bdcb13b20e/docs/security.md),
[GitHub event/token behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
[GitHub concurrency queue](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
[active branch rules API](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch).

Evaluated upstreams: [Spec Kit](https://github.com/github/spec-kit),
[Superpowers](https://github.com/obra/superpowers),
[Agentic Workflows](https://github.com/github/gh-aw),
[Ruff installation](https://docs.astral.sh/ruff/installation/).

[actionlint](https://github.com/rhysd/actionlint/tree/v1.7.12) is an upstream
development tool, not a runtime dependency or a workflow engine. Its 1.7.12
parser predates GitHub's documented `concurrency.queue` field. CI suppresses only
that exact unknown-field diagnostic; a separate YAML test enforces `queue: max`
without cancellation for the kit's serialized workflows. Other syntax and
expression diagnostics remain errors. The existing live queue probe remains
separate evidence of GitHub platform behavior.

Self-hosted integration sources:
[Codex account authentication in CI](https://learn.chatgpt.com/docs/auth/ci-cd-auth),
[native permissions](https://learn.chatgpt.com/docs/permissions),
[Actions job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts),
[pinned runner hook scheduling](https://github.com/actions/runner/blob/v2.337.0/src/Runner.Worker/JobExtension.cs),
[pinned Codex Git metadata implementation](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/git-utils/src/status.rs),
[Ubuntu user namespace restrictions](https://discourse.ubuntu.com/t/ubuntu-24-04-lts-noble-numbat-release-notes/39890),
[Moby default seccomp provenance](https://github.com/moby/profiles/tree/85e237f1fe229a0c61c9c7d8e743fa780d3b97ca).
