## Mục đích

<!-- Thay đổi gì và vì sao. 1–3 câu. -->

**Gap / rủi ro:** <!-- F2-G01, SEC-03 … hoặc "không có" -->

## Thay đổi chính

-

## Cách kiểm tra

<!-- Lệnh đã chạy, kịch bản thử tay, ảnh chụp màn hình nếu đổi UI -->

## Ảnh hưởng production

- [ ] Merge PR này **sẽ deploy** (backend / frontend / mobile) — người merge theo dõi tới khi smoke test xanh
- [ ] Có migration DB — tương thích ngược theo [release-deploy § 4](https://github.com/LockR-Tech/docs/blob/main/04-engineering/release-deploy.md)
- [ ] Có biến môi trường / secret mới — đã cấu hình trên VM hoặc GitHub **trước** khi merge
- [ ] Đổi hợp đồng API / MQTT dùng chung — client cũ (app đã phát hành, kiosk, Pi) vẫn chạy
- [ ] Dùng `[skip ci]` — lý do:

## Tài liệu

- [ ] PR docs: <!-- link https://github.com/LockR-Tech/docs/pull/… -->
- [ ] Không ảnh hưởng tài liệu — lý do:

## Tự kiểm trước khi xin review

- [ ] Tiêu đề PR theo [Conventional Commits](https://github.com/LockR-Tech/docs/blob/main/04-engineering/commit-convention.md)
- [ ] Đã rebase trên `main` mới nhất
- [ ] Không có secret, `.env` thật, file build trong diff
- [ ] Endpoint mới / đổi: đã kiểm **ai gọi được** (vai trò + chủ sở hữu) ở phía service, không chỉ gateway
