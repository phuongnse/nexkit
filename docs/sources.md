# Components and provenance

The links below document the integrated interfaces and pinned components.
NexKit's Python runtime has no third-party dependency; it uses Git,
GitHub CLI and the consumer's actual commands.

| Component | Version or pin | License and decision |
|---|---|---|
| OpenAI Codex CLI | Optional `engine.install.version`; one verification reference in source | Apache-2.0; runtime compatibility follows capabilities |
| openai/codex-action | `86365089eb2b84e0a8fb0717b304f8bdcb13b20e` | Apache-2.0; reuse the official CLI wrapper, credential proxy and safety controls |
| Agent Skills | Portable SKILL.md format | Original NexKit content, one shared methodology source |
| Ruff | 0.16.9 | MIT; development formatting/linting only |
| PyYAML | 6.0.3 | MIT; development workflow structure inspection only |
| actionlint | 1.7.12; release archive SHA-256 in the core CI workflow | MIT; native Actions syntax, expression and reusable-workflow validation during development |
| GitHub Actions runner | 2.337.0; release archive SHA-256 in `runner/Dockerfile` | MIT; official repo-scoped listener and job hooks |
| GitHub CLI | 2.101.0; release archive SHA-256 in `runner/Dockerfile` | MIT; official repository operations |
| Node / Ubuntu runner images | 24.18.0 / 24.04; OCI digests in `runner/Dockerfile` | Upstream component licenses; base runtime packages |
| Moby seccomp profile | `85e237f1fe229a0c61c9c7d8e743fa780d3b97ca` | Apache-2.0; default profile with namespace/mount permissions; license in `runner/third-party` |

Checkout/upload/download Actions use full commit pins. A kit update changes its
version/pin through a reviewable diff and verification; consumers never silently
follow upstream main. NexKit's own distribution license has not been selected
by the owner. The first stable distribution is the `v1.0.0` GitHub release;
no package registry publication is configured.

API and authentication sources:
[Codex non-interactive execution](https://developers.openai.com/codex/noninteractive),
[Codex authentication](https://developers.openai.com/codex/auth),
[official action security](https://github.com/openai/codex-action/blob/86365089eb2b84e0a8fb0717b304f8bdcb13b20e/docs/security.md),
[GitHub event/token behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
[GitHub concurrency queue](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
[active branch rules API](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch).

[actionlint](https://github.com/rhysd/actionlint/tree/v1.7.12) is an upstream
development tool, not a runtime dependency or a workflow engine. Its 1.7.12
parser predates GitHub's documented `concurrency.queue` field. CI suppresses only
that exact unknown-field diagnostic; a separate YAML test enforces `queue: max`
without cancellation for the kit's serialized workflows. Other syntax and
expression diagnostics remain errors. Verify native queue behavior separately
against the actual platform.

Self-hosted integration sources:
[Codex account authentication in CI](https://learn.chatgpt.com/docs/auth/ci-cd-auth),
[native permissions](https://learn.chatgpt.com/docs/permissions),
[Actions job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts),
[pinned runner hook scheduling](https://github.com/actions/runner/blob/v2.337.0/src/Runner.Worker/JobExtension.cs),
[pinned Codex Git metadata implementation](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/git-utils/src/status.rs),
[Ubuntu user namespace restrictions](https://discourse.ubuntu.com/t/ubuntu-24-04-lts-noble-numbat-release-notes/39890),
[Moby default seccomp provenance](https://github.com/moby/profiles/tree/85e237f1fe229a0c61c9c7d8e743fa780d3b97ca),
[Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/),
[WSL installation](https://learn.microsoft.com/en-us/windows/wsl/install),
[WSL on Windows Server](https://learn.microsoft.com/en-us/windows/wsl/install-on-server),
[WSL systemd and instance lifetime](https://learn.microsoft.com/en-us/windows/wsl/systemd),
[Windows IPv4 interface inspection](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getipaddrtable).
