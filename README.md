# NexKit

**Agents. Skills. One workflow.**

NexKit thiết lập AI SDLC từ mục tiêu của bạn và repository thực tế. Bạn duyệt
requirement trên GitHub; Actions gọi coding-agent CLI để implement, review độc
lập, kiểm thử, sửa lỗi và merge trong giới hạn đã chọn. Release là quyết định riêng.

**Trạng thái: candidate đang được kiểm chứng. Chưa chứng minh live AI delivery
trên Actions.** Xem [bằng chứng và phần còn thiếu](docs/acceptance.md).

## Cài và thiết lập

Cần Python 3.11+, Git và GitHub CLI đã đăng nhập. Từ source hoặc gói đã giải nén:

```sh
python3 scripts/build.py
export PATH="$PWD/bin:$PATH"
nexkit --version
```

Trong Codex, thêm marketplace của thư mục gói bằng `codex plugin marketplace add
/absolute/path/to/nexkit`, rồi `codex plugin add nexkit@personal`. Trong Claude
Code, dùng `claude --plugin-dir /absolute/path/to/nexkit/plugins/nexkit`.
Bạn cũng có thể dùng installer
skills của NexKit. [Hướng dẫn host](docs/compatibility.md) phân biệt cài được,
tương tác đã chạy và engine CI. Trong Codex dùng `$nexkit-init`; trong Claude
Code plugin dùng `/nexkit:nexkit-init`, rồi mô tả mục tiêu và ràng buộc của project.
Agent khảo sát repo, chuẩn bị cấu hình để bạn xem và chạy kiểm chứng setup.
[Các trường cấu hình và quyền](docs/configuration.md).

## Yêu cầu đầu tiên

Dùng skill `nexkit-request`. Nó gửi yêu cầu cho workflow intake tạo GitHub issue,
rồi làm rõ spec trong issue đó. Người có quyền review issue và tự đăng đúng
comment `/nexkit approve <hash>` do `nexkit approval <issue>` cung cấp.

Sau approval, theo dõi issue, PR và Actions. Dùng `nexkit status <issue>`;
`nexkit cancel <issue>` hoặc `nexkit resume <issue>` khi cần. Delivery không cần
terminal của bạn và không yêu cầu duyệt plan, task, test hay PR.

Khi muốn phát hành, dùng `nexkit release --commit <sha> --version <version>
--notes-file <file>`. Người có quyền chọn đúng candidate và đăng
`/nexkit release <hash>` trên issue candidate. Merge không tự tạo release.

[Cách CLI, skills và Actions phối hợp](docs/architecture.md) ·
[Kiểm thử và phục hồi](docs/operations.md) ·
[Dependency và nguồn tài liệu](docs/sources.md)
