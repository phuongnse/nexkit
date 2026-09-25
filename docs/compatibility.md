# Host compatibility — kiểm tra ngày 2026-09-25

| Sản phẩm/biến thể | Bản kiểm tra | Cài/nạp NexKit | Tương tác bằng model | Engine Actions |
|---|---|---|---|---|
| OpenAI Codex CLI | 0.156.1 | Native marketplace add + plugin add + list thực sự thành công; enabled rc.1 | Live local implement/reviewer riêng: đọc skills, tools sửa code, regression và CLI thật qua | Đã implement adapter chính thức; chưa có live run |
| Anthropic Claude Code CLI | 2.1.282 | Native marketplace install/details thành công, nhận đủ 8 skills | Chưa có auth để chạy model | Không cung cấp trong candidate này |
| GitHub Copilot CLI | Chưa cài | Đã đối chiếu `.github/skills` với tài liệu | Chưa xác minh | Không cung cấp |
| Google Gemini CLI | 0.61.0 được tra từ registry; chưa cài | Đã đối chiếu `.gemini/skills` | Chưa xác minh | Không cung cấp |
| Cursor desktop Agent | Chưa có phiên bản cài thực tế | Đã đối chiếu `.cursor/skills` | Chưa xác minh | Không cung cấp |
| Google Antigravity IDE | Chưa có phiên bản cài thực tế | Đã đối chiếu `.agents/skills` | Chưa xác minh | Không cung cấp |

Đã sinh file cho một host không có nghĩa là host đó đã được nghiệm thu. Host có
thể gửi yêu cầu sang engine Codex đã cấu hình; không phải mọi host cần làm CI engine.

Từ source/archive, Claude Code có thể nạp native plugin bằng:

```sh
claude --plugin-dir /absolute/path/to/nexkit/plugins/nexkit
```

Với Codex và các host khác, installer đặt portable skills đúng đường dẫn của
project khi chạy `nexkit setup ... --host <host> --apply`. Codex gọi `$nexkit-init`;
Claude Code khi nạp plugin gọi `/nexkit:nexkit-init`. Các host khác có thể chọn
skill theo tên qua cơ chế discovery chính thức. Không có slash-command tự phát minh.

Native install đã được thử trong config directories riêng dưới
`/tmp/nexkit-host-validation`, không thay cấu hình host cá nhân. Kết quả cài/nạp
không chứng minh task tương tác hoặc CI headless chạy thành công.

Nguồn chính thức:
[Codex skills](https://developers.openai.com/codex/skills),
[OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins),
[Claude Code plugins](https://code.claude.com/docs/en/plugins),
[Copilot CLI skills](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference),
[Gemini CLI skills](https://geminicli.com/docs/cli/skills/),
[Cursor skills](https://prod.cursor.com/docs/skills),
[Antigravity skills](https://antigravity.google/docs/skills).

[Live local Codex evidence](validation/local-codex-2026-09-25.json) dùng model
`gpt-6-astra` trong cấu hình local hiện có. Điều này không chứng minh API auth
trên Actions hoặc khả năng tương tác của các host chưa chạy model.
