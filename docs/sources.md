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
| Claude Code CLI | 2.1.282 | Upstream commercial terms; installed for host verification, never redistributed |

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
