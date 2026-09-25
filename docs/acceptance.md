# Nghiệm thu: chưa hoàn tất

Mục tiêu đầy đủ ở [bootstrap issue #1](https://github.com/phuongnse/nexkit/issues/1).
Candidate `0.1.0-rc.1` có implementation và gói cài, chưa đủ bằng chứng để xác nhận
trọn SDLC. Không tạo human approval giả và không coi mock là live integration.

| Nhóm | Bằng chứng hiện có | Còn phải chứng minh |
|---|---|---|
| A — package/hosts | Native Codex 0.156.1 install/list và Claude Code 2.1.282 install/details nhận 8 skills; archive và validators | Các host khác mới đối chiếu tài liệu; không quảng cáo đã chạy model |
| B — CLI trên Actions | Live local Codex dùng skills/tools, sửa code, reviewer session riêng; GitHub platform probe qua | Live AI runner, API auth và độc lập terminal trên Actions |
| C — hai consumer | Tests chạy Node CLI mới và HTTP API Python có sẵn, cấu hình khác nhau, không thêm preset | Hai GitHub consumer được cho phép và setup/live delivery thực tế |
| D — success | Controller simulation issue→approval→review/checks→merge; CI của kit chạy thật | Human approval và tự merge consumer trên GitHub, không release |
| E — repair | Live local agent tái hiện sign bug, sửa bằng tools; reviewer chạy regression với hàm gốc; controller chuyển feedback qua vòng kế | Feedback→AI sửa→checks/review mới→merge trên Actions |
| F — blocked | Tests unauthorized, edit/revert, stale identity, invalid/missing output, zero/skip/fail tests, exhausted budgets, control edits | Failure injection trên live environment với guards cuối |
| G — durability | Ownership install/reinstall/uninstall; cancel/resume guards; duplicate intake/reservation; orphan/merged recovery; base ancestry; native queue syntax qua | Live concurrent work, duplicate events, interruption, cancellation và quyền thiếu trên consumer |
| H — release | Tests wrong approver, drift, bytes/tag khác, cancel/deadline, retry không trùng, lost upload response dùng lại artifact gốc | Human duyệt và test release đúng phạm vi được phép |

## Kết quả đã chạy

- 60 tests local qua sau independent review. Hai consumer chạy code/unit/CLI/HTTP
  thật; GitHub/controller boundaries là mô phỏng có nhãn rõ.
- [CI GitHub đầu tiên](https://github.com/phuongnse/nexkit/actions/runs/36115605127)
  chạy 55 tests, Ruff và build tại `21f68b7`. Các runs mới nằm trong
  [workflow checks](https://github.com/phuongnse/nexkit/actions/workflows/ci.yml).
- [Platform probe](https://github.com/phuongnse/nexkit/actions/runs/36115701671)
  qua với Contents/Issues/Metadata read; đọc được issue edit history và Actions
  App ID 15368. Repo kit không có ruleset: probe chứng minh quyền API/cú pháp
  `queue: max`, chưa chứng minh merge protection hay model auth.
- [Independent review](validation/independent-review.md) dùng session riêng,
  reproductions và regression. Kết luận cuối: không tìm thấy blocker code mới
  trong phạm vi review; không kết luận đạt live acceptance.
- [Live Codex local đầu tiên](validation/local-codex-2026-09-25.json): 2 sessions,
  4 unit cases qua; reviewer chạy 11 ca CLI, không đổi source và tái hiện lỗi trên
  hàm gốc. Mỗi session giới hạn 180 giây. CLI báo implement input/output
  137238/2572 tokens, review 104836/4248; input bao gồm cache, xem JSON chi tiết.
  Không có số tiền thực đo. Script tái chạy ghi riêng thời gian và usage.

## Điều kiện bên ngoài còn thiếu

- Hai consumer repositories được cho phép và phạm vi test release.
- `OPENAI_API_KEY` trong Actions secrets, model theo role và mức usage CI đã chốt.
  Login ChatGPT local không được sao chép vào CI.
- Người thật duyệt requirement và release candidate trong nghiệm thu live.

[Quy trình chạy lại](live-acceptance.md) giữ nguyên tiêu chí. Chưa public release
hoặc publish package registry. Local/CI artifact là candidate để kiểm tra,
không phải tuyên bố sẵn sàng production.
