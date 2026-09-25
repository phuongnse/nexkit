# Kiểm thử và vận hành

Chạy lại bằng Python 3.11+ và Node có `node:test`:

```sh
python3 -m unittest discover -v
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
python3 scripts/build.py
```

Unit/controller tests dùng boundary GitHub giả có nhãn rõ. Consumer tests chạy
Node CLI và HTTP server thật trong thư mục tạm. Chúng không gọi model và không
chứng minh workflow GitHub thật. Live validation sẽ bổ sung links run/issue/PR
trong acceptance report, sau khi có quyền và credentials.

`nexkit status <issue>` đọc GitHub state. `cancel` gửi lệnh được kiểm tra quyền;
nó ngăn bước có tác động chưa bắt đầu khi controller kiểm tra lại. Commit/PR/tag
đã tạo không tự rollback. `resume` giữ nguyên budget và approval; sửa spec cần
approval requirement mới. Budget hết cần quyết định quản trị, không tự reset.

Một run bị mất kết nối không mặc nhiên được khởi động lại: đọc trạng thái Actions
run_id trước. Branch/PR hoặc release draft đã tạo được dùng lại; không dùng
empty commit hay close/reopen giả để kích hoạt checks. Kết quả của commit/base/
spec/config khác không có hiệu lực với candidate hiện tại.

Artifacts giữ 7 ngày trong Actions; workspace thuộc ephemeral runner. GitHub
state chỉ giữ feedback gần nhất, reservations và candidate để tiếp tục. Không
đưa toàn bộ transcript vào consumer knowledge. Uninstall giữ source, config,
knowledge và dữ liệu GitHub; file kit đã được user sửa cũng được giữ lại.

Candidate hiện hỗ trợ text source changes tối đa 200 files/2 MB; đổi symlink,
submodule hoặc binary source sẽ dừng với lý do cụ thể. Release artifacts tối đa
100 MB/file. Chỉ ephemeral GitHub-hosted Ubuntu 24.04 đã được thiết kế; các
runner/engine khác cần integration và kiểm chứng riêng.

## Lặp lại smoke có model thật

```sh
python3 scripts/local_agent_smoke.py \
  --implement-model MODEL_BAN_CO_QUYEN_DUNG \
  --review-model MODEL_BAN_CO_QUYEN_DUNG \
  --output dist/local-smoke-lan-1
```

Script tạo fixture Git tạm, gọi hai sessions Codex độc lập, kiểm tra tool output
đọc đúng skill, regression/CLI thật và reviewer không sửa source. Kết quả, diff,
usage CLI báo và thời gian nằm ở output directory. Script dùng model usage của
account hiện tại, mỗi session mặc định tối đa 180 giây. Không ghi GitHub hoặc
release. [Nghiệm thu GitHub](live-acceptance.md) là bước riêng.

Release đang dở dùng artifact từ run gốc trong thời hạn lưu 7 ngày. Nếu artifact
hết hạn hoặc bytes/tag khác, pipeline dừng và giữ draft; không ghi đè hoặc tự
chọn candidate mới. File lớn/binary có sẵn được snapshot bằng hash; giới hạn
2 MB áp dụng cho nội dung thay đổi chuyển qua publication.
