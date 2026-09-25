# Cấu hình từ quyết định của project

`nexkit survey` thu thập tên file, instructions, workflows và manifest hiện có.
Skill setup dùng dữ liệu này cùng user input; core không có preset ngôn ngữ,
framework hay loại ứng dụng. Cấu hình của consumer ở `.nexkit/project.json`.

| Trường | Giá trị phải quyết định |
|---|---|
| `schema` | `1` |
| `repository`, `default_branch` | GitHub owner/name và nhánh tích hợp thực tế |
| `kit` | `repository`, full commit SHA `ref`, exact `version` |
| `engine` | `name: codex`, exact CLI `version`; hiện chỉ có engine CI này |
| `models` | Tên model người dùng truy cập được cho `implement` và `review`; không có mặc định |
| `limits` | `attempts` (1–20), `agent_calls` (2–40), `minutes` (1–1440), `command_seconds` (1–3600) |
| `environment` | `runner: ubuntu-24.04`, `setup`: danh sách commands dạng mảng argv |
| `application` | `present` hoặc `absent` lúc setup; repo mới vẫn phải khai báo commands dự kiến trước delivery |
| `checks` | Commands thực tế; mỗi mục có `name`, `kind`, `argv`, `timeout_seconds` |
| `decisions` | Các quyết định sản phẩm/kỹ thuật đã chốt, dạng danh sách chuỗi |
| `knowledge` | Đường dẫn tương đối tới nguồn kiến thức cần giữ |
| `merge_method` | `squash`, `merge` hoặc `rebase` theo policy project |
| `release` | `enabled` bắt buộc; khi bật cần `build` argv, `artifacts` paths, `tag_prefix` |

`kind` của check là `build`, `lint`, `test`, `e2e`. Test và E2E phải khai báo
`report` có `format: junit|tap|unittest`; JUnit thêm `path`. Báo cáo thiếu, không
có ca chạy, fail, skipped hoặc TODO không đạt. Reviewer đánh giá ý nghĩa assertions
và quan hệ với requirement. Build/lint được thêm khi project cần; chúng không
thay E2E. Commands là argv, không phải shell text; nếu project thực sự cần shell,
quyết định đó phải hiện rõ trong argv và chỉ chạy trong môi trường không có quyền cao.

Release build có thể dùng `{version}` và `{commit}` trong từng argv; artifact
path có thể dùng `{version}`. Nội dung version trong source phải được commit
trước khi chọn candidate. Không có publish-to-registry hoặc production deployment
mặc định; bản hiện tại phát hành artifact qua GitHub Releases.

Preview và áp dụng các quyết định đã được phép:

```sh
nexkit setup --config /path/to/accepted-project.json --host codex
nexkit setup --config /path/to/accepted-project.json --host codex --apply --online
nexkit doctor --online --checks
```

Setup ghi cấu hình và chạy commands; exit code 2 thể hiện capability chưa sẵn
sàng, không có check pass giả cho repo chưa có ứng dụng. `doctor` chưa chứng minh
model/API thực sự truy cập được chỉ qua tên secret: phải chạy live acceptance.

## GitHub settings

Setup cần administrator kiểm tra:

- Actions được phép tạo PR; workflows đã ở default branch.
- Active branch ruleset cho default branch yêu cầu hai checks `NexKit verification`
  và `NexKit review`, gắn với GitHub Actions App, strict up-to-date checks.
- Không có blanket bypass, mandatory human PR review, required deployment hoặc
  merge queue chưa được tích hợp. Classic protection và org rulesets cũng phải xét.
- State branch `nexkit/state` và delivery branches được phép ghi bằng token job.
- Required checks cũ được giữ về hành vi bằng commands đã khai báo hoặc có dispatch
  integration đã kiểm chứng; không chỉ xóa yêu cầu để làm pipeline xanh.
- `OPENAI_API_KEY` nằm trong Actions secrets, model đã được cấp quyền và giới hạn
  usage đã chấp nhận. Không copy login ChatGPT local vào public CI.

Runtime đọc active rules bằng API có Metadata:read. Full bypass/classic audit
chạy ở setup bằng tài khoản admin; CI không giữ credential quản trị. Ruleset
availability phụ thuộc loại repository/gói GitHub và phải được kiểm tra thực tế.

Không tự đổi settings bằng setup command hiện tại. Agent dùng GitHub tooling
để áp dụng đúng thay đổi quản trị đã được người dùng chấp nhận rồi chạy doctor.
Đây là công việc setup, không phải gate cho từng feature.
