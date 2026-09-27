# AGENTS.md — iot

Repo này thuộc hệ thống **Lock.R** (org [LockR-Tech](https://github.com/LockR-Tech)). File này chỉ là **con trỏ** — tiến độ, luật và sơ đồ nằm ở repo [**docs**](https://github.com/LockR-Tech/docs). Không ghi tiến độ vào đây.

## Trước khi làm bất cứ việc gì

1. Repo docs phải nằm cạnh repo này (`../docs`). Chưa có: `git clone https://github.com/LockR-Tech/docs.git ../docs`. Có rồi: `git -C ../docs pull --ff-only`.
2. Đọc `../docs/AGENTS.md` — giao thức bắt đầu/kết thúc phiên.
3. Đọc `../docs/STATUS.md` — tiến độ, rủi ro, việc đang làm, việc tiếp theo.
4. Đọc file luồng liên quan trong `../docs/02-flows/` (L1, L2, L3 đều đi qua tủ).

## Luật bắt buộc

- Không push thẳng `main` — nhánh + PR + review + squash merge.
- Nhánh `<type>/<gap-id>-<mo-ta>`; commit và tiêu đề PR theo Conventional Commits; footer `Refs: F2-G09`.
- Không commit secret, `.env` thật, file build. Không thêm trailer `Co-Authored-By` của công cụ AI.
- Việc chỉ xong khi tài liệu ở `../docs` đã cập nhật.

## Riêng repo này

- **Không có CI/CD.** Cập nhật tủ thật = SSH vào Raspberry Pi, `git pull`, khởi động lại dịch vụ. Cần phần cứng thật (Pi + Arduino RS485) để thử đường mở khoá vật lý.
- **Tủ vật lý:** sơ đồ đấu nối của nhà cung cấp + đối chiếu chân Arduino trong firmware ở `../docs/03-hardware/cabinet-wiring-spec.md`; chuẩn bị Pi, thứ tự nối dây, bring-up và kiosk trên Pi ở `../docs/03-hardware/controller-wiring-guide.md`. Hai cách điều khiển, chọn bằng `HARDWARE_BACKEND` trong `.env`: `gpio` — Pi nối thẳng relay, dây tín hiệu khoá, TB6600 + công tắc hành trình như sơ đồ nhà cung cấp (`hardware/gpio_locker.py`, `hardware/lid_controller.py`, kiểm tra bằng `debug_gpio.py`; ADR-0007 trong repo docs) · `rs485` (mặc định) — qua Arduino, firmware 7 ngăn, `SLAVE_ID = 1`.
- **Thành phần:** `main.py` (Pi controller: GPIO hoặc serial RS485, MQTT, heartbeat, FastAPI :8000) · `hardware/` (GPIO: khoá, cảm biến cửa, nắp trượt) · `arduino/locker_controller/` (sketch cho cách RS485, `SLAVE_ID` đặt riêng từng board) · `ui/` (kiosk React, gọi thẳng API production) · `simulate_demo_cabinet.py` (giả lập tủ khớp hợp đồng MQTT của backend).
- **Lệnh:** `uv sync` · `docker compose -f docker-compose.postgres.yml up -d` · `SIMULATION=true uv run python main.py` (không cần Arduino) · `uv run python simulate_demo_cabinet.py` · kiosk: `cd ui && npm install && npm run dev`.
- ⚠ Hợp đồng MQTT **lệch** giữa backend và `main.py`: backend gửi `{commandId, box_id, action}` tới `cabinet/{lockerId}/command/open`, còn Pi cần `slotIndex` và dùng **tên** tủ trong topic. Hiện chỉ bộ giả lập chạy end-to-end — gap F2-G09.
- ⚠ Broker mặc định là `broker.hivemq.com` công khai (SEC-04) — không dùng cho tủ thật.
- Kiosk chỉ mở cửa khi nhập mã, **không hoàn tất đơn** — gap F2-G01.
