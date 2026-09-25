# Chạy nghiệm thu live khi đủ điều kiện

Giữ nguyên tiêu chí A–H. Không tự tạo repo, đổi settings, đăng approval thay
người hoặc public release để làm bảng nghiệm thu xanh.

## Setup được phép

Chủ project chỉ định hai repo: một mới, một có nội dung/hành vi thật, và phạm vi
private test release. Chốt model implement/review, CLI version, budget. Với từng
repo dùng `nexkit survey` và skill `nexkit-init`; tạo cấu hình từ user input và
repo thực, không lấy fixtures làm catalog setup. Pin kit bằng full SHA đã kiểm tra.

Áp dụng rules/settings đã được phép theo [configuration](configuration.md).
Chủ tài khoản đặt OPENAI_API_KEY qua GitHub Secrets hoặc `gh secret set`, không
đưa secret vào chat/source/logs. Chạy setup --apply --online, commit setup lên
default branch, chạy doctor --online --checks. Ghi đúng capability chưa sẵn sàng;
repo chưa có app không được có test pass giả. Secret metadata không chứng minh
model auth.

## Prompt → Issue → approval → merge

Dùng skill nexkit-request hoặc:

```sh
nexkit request --title 'Mục tiêu cụ thể' --body-file request.md --key nghiem-thu-1
nexkit intake-status --key nghiem-thu-1
```

Đóng host sau dispatch thành công. Xác minh intake tạo đúng một Issue, Actions
clarification tự đọc repo/skills. Người có quyền trả lời câu hỏi trên Issue;
lượt sau phải hiểu câu hỏi cũ và câu trả lời mới. Người thật đọc spec và tự đăng
`/nexkit approve HASH`. Không bấm approve plan, PR hoặc tests giữa delivery.

Quan sát CLI/model/skill/tool logs, implementation, real tests/E2E, reviewer
session riêng và merge. Kiểm tra ứng dụng qua CLI/HTTP/browser phù hợp. Ghi
Issue/PR/run URLs, spec/config/base/head/kit identity, thời gian, lượt sửa và
human interventions; token/chi phí chỉ ghi khi đo được. Chưa được có release.
Lặp lại trên consumer thứ hai với yêu cầu/cấu hình khác, không đổi core.

## Repair, blocked và durability

Dùng defect có ý nghĩa trong spec đã duyệt. Quan sát check/reviewer phát hiện,
feedback được chuyển cho agent, candidate sửa có checks/review mới rồi mới
merge. Không thay agent bằng mock để gọi đây là live repair.

Trong phạm vi private đã cho phép, thử:

- Chưa duyệt, sai người, spec/body/title sửa rồi revert; outsider comments.
- Base/head/config đổi, stale/missing review, failed/zero/skipped tests, sai schema.
- Sửa controls để tự approve, hết attempts/calls/time, thiếu quyền GitHub.
- Cùng key nhiều lần, dispatch đồng thời, mất kết nối sau side effect, cancel
  trước publish/merge, resume giữ budget, reinstall/update/uninstall có user edits.

Không trường hợp thiếu điều kiện nào được merge. Mocks trong tests phục vụ
failure injection riêng; vẫn cần thao tác GitHub thật để hoàn tất nhóm G.

## Human release decision

Merge nhiều thay đổi và xác minh chưa có tag/release. Commit source version
changes trước khi chọn candidate, chuẩn bị notes rồi chạy:

```sh
nexkit release --commit FULL_SHA --version VERSION_DA_CHON --notes-file notes.md
nexkit intake-status --operation release --key KEY_TRA_VE
nexkit approval SO_ISSUE_CANDIDATE
```

Người thật chọn thời điểm/candidate và tự đăng `/nexkit release HASH`. Chỉ trong
phạm vi test release đã cho phép, quan sát verify/build/tag/publish exact source,
version, notes; đối chiếu manifest và server asset hashes. Merge tiếp ở default
branch không được đổi source candidate.

Thử wrong approver, drift, retry và ngắt sau partial upload. Status phải ghi lý do;
resume dùng artifact của run gốc (lưu 7 ngày), không đổi tag/upload trùng hoặc
rebuild bytes khác. Artifact hết hạn/hash khác phải blocked và giữ draft.
GitHub release không phải production deployment.

Điền kết quả/link thực vào [acceptance](acceptance.md). B chỉ đạt khi AI CLI chạy
trên runner; D/H chỉ đạt với human approval thật. Thiếu prerequisites thì giữ
mục chưa xác minh, không thay bằng demo dễ hơn.
