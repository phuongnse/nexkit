# Thành phần và nguồn gốc

Tra tài liệu chính thức ngày 2026-09-25 trước khi chọn interfaces. Runtime Python
không có third-party dependency; dùng Git, GitHub CLI và commands của consumer.

| Thành phần | Bản/pin đã xem | License / quyết định |
|---|---|---|
| OpenAI Codex CLI | local 0.156.1; registry 0.157.0 | Apache-2.0; user pin CLI/model trong config |
| openai/codex-action | `86365089eb2b84e0a8fb0717b304f8bdcb13b20e` | Apache-2.0; dùng official CLI wrapper, proxy/auth/safety có sẵn |
| Agent Skills | SKILL.md portable format | Nội dung NexKit tự viết, một nguồn chung |
| GitHub Spec Kit | v1.0.11 | MIT; đã đánh giá, không tích hợp lifecycle/template catalog |
| Superpowers | v6.4.1 | MIT; đã đánh giá, không sao chép methodology/approval flow |
| GitHub Agentic Workflows | v0.89.21 | MIT; chưa dùng compiler/orchestrator vì Actions + CLI đủ đường chạy |
| Ruff | 0.16.9 | MIT; chỉ dùng phát triển để format/lint |
| PyYAML | 6.0.3 | MIT; chỉ dùng kiểm tra cấu trúc workflow trong development |
| Claude Code CLI | 2.1.282 | Upstream commercial terms; chỉ cài để kiểm tra host, không phân phối binary |

Actions checkout/upload/download đều pin full SHA trong workflows. Update kit
là thay version/pin có diff, kiểm chứng lại rồi áp dụng; consumer không tự kéo main.

Nguồn API và authentication:
[Codex non-interactive](https://developers.openai.com/codex/noninteractive),
[Codex authentication](https://developers.openai.com/codex/auth),
[official action source/security](https://github.com/openai/codex-action/blob/86365089eb2b84e0a8fb0717b304f8bdcb13b20e/docs/security.md),
[GitHub event/token behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
[GitHub concurrency queue](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
[active branch rules API](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch).

Upstreams đã đánh giá:
[Spec Kit](https://github.com/github/spec-kit),
[Superpowers](https://github.com/obra/superpowers),
[Agentic Workflows](https://github.com/github/gh-aw),
[Ruff installation](https://docs.astral.sh/ruff/installation/).
