# Nghiệm thu: chưa hoàn tất

Mục tiêu đầy đủ được giữ trong [bootstrap issue #1](https://github.com/phuongnse/nexkit/issues/1).
Không tạo human approval giả trên GitHub, không coi mock là live integration.

| Nhóm | Bằng chứng hiện có | Còn phải chứng minh |
|---|---|---|
| A — package/hosts | Validators; Codex 0.156.1 native plugin install/list; Claude Code 2.1.282 install/details nhận 8 skills | Build archive và smoke hành vi host từ gói cuối |
| B — CLI trên Actions | Reusable workflow gọi pinned official Codex action, hai jobs/sessions riêng | Live runner nạp skill, agent dùng tools, CLI output và local-session independence |
| C — hai consumer | `tests/test_consumers.py`: Node CLI mới, HTTP API Python có sẵn, commands/hành vi khác nhau | Hai GitHub consumer được cho phép, setup/live delivery thực tế |
| D — success | `tests/test_delivery.py`: controller simulation issue→approval→candidate→checks/review→merge | Human approval và auto merge trên GitHub thật, không release |
| E — repair | Real local signed-sum regression fail trước/pass sau; controller chuyển reviewer feedback sang vòng kế | Live AI phát hiện/sửa meaningful bug trên Actions |
| F — blocked | Unit/controller tests: unauthorized, body drift/revert, stale identity, invalid output, missing reviewer, zero tests, exhausted budget, control edit | Failure injection trên live environment với guards cuối |
| G — durability | Local install/reinstall/uninstall, symlink/ownership guards, bounded reservations, orphan branch recovery | Live duplicate dispatch/concurrency/cancel/resume/permission failures và interruption |
| H — release | Code đã tách source/approval/build/artifact publication | Release regression suite và live test release đúng phạm vi được cấp phép |

Independent reviewer chạy trong agent session riêng, đọc code/Actions/đặc tả,
chạy tests và các reproductions. Những lỗi tìm thấy đã dẫn đến sửa API quyền,
workspace, approval edit history, uninstall paths, environment lifetime và retry.
Đây chưa phải approval độc lập cho candidate cuối; sẽ review lại sau khi đóng findings.

## Điều kiện bên ngoài đang thiếu

- Tên/quyền hai private consumer repositories và phạm vi test release.
- API secret cho Actions, model theo role và giới hạn sử dụng được chấp nhận.
- Human thật duyệt requirement và release candidate trong các nghiệm thu live.

Không có token/cost measurement của model run để báo cáo. Test timings lấy từ
test runner; reserved invocation counts là giới hạn, không phải billable usage.
Chưa tạo public release hoặc công bố package.
