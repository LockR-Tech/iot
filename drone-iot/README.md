# drone-iot — Pi trên drone gửi telemetry về Lock.R

Chạy trên Raspberry Pi gắn trên drone: đọc MAVLink từ autopilot (ArduPilot) qua USB, gửi telemetry lên broker MQTT cho order-service. Thuộc luồng drone (L1); **tách khỏi code tủ** ở phần còn lại của repo này — không dùng chung module, cấu hình hay dịch vụ nào.

Hợp đồng topic/payload/chữ ký: `../../docs/01-overview/drone-telemetry-contract.md`.

**Chỉ đọc.** Thứ duy nhất gửi xuống autopilot là yêu cầu tần suất message. Không arm, không đổi mode, không điều khiển servo.

**Giao hàng ArUco (`lockr/drone/delivery/events` ↔ `…/commands`) bắt buộc `DEVICE_KEY`.** Sự kiện gửi lên
(`MARKER_DETECTED`, `DROP_PHASE_COMPLETED`) được ký như telemetry; backend bỏ bản tin không ký, ký sai drone,
lệch giờ quá 120 giây (Pi phải đồng bộ NTP) hoặc trùng `messageId`. Ngược lại Pi chỉ làm theo lệnh backend ký
bằng khoá của chính drone này; thiếu `DEVICE_KEY` thì mọi lệnh giao hàng bị bỏ.

## Thành phần

| File | Việc |
|---|---|
| `main.py` | vòng lặp chính: mỗi giây dựng một bản tin và gửi (hoặc in với `--print`) |
| `lockr_drone/mavlink_reader.py` | luồng đọc MAVLink; tự mở lại cổng, tự xin lại tần suất message |
| `lockr_drone/state.py` | gom message, đổi đơn vị, ghi thời điểm từng nhóm số đo |
| `lockr_drone/telemetry.py` | dựng payload schemaVersion 1 |
| `lockr_drone/mqtt_publisher.py` | MQTT 5 + TLS, Last Will, ký bản tin |
| `lockr_drone/signing.py` | chữ ký HMAC |
| `lockr_drone/delivery.py` | nhiệm vụ giao hàng + quyết định khi nào xin mở nắp (ổn định, một lần, có hạn gửi lại) |
| `lockr_drone/camera.py` | camera + dò ArUco + HUD + `/api/status` (trường `arucoVision.delivery`) |
| `watch.py` | nghe lại từ broker và kiểm chữ ký (nghiệm thu) |
| `deploy/lockr-drone.service` | dịch vụ systemd |

## Cài trên Pi

```bash
mkdir -p ~/lockr-drone && cd ~/lockr-drone        # chép nội dung thư mục này vào đây
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ls -l /dev/serial/by-id/                            # tìm cổng autopilot (…-if00)
cp .env.example .env && chmod 600 .env && nano .env
```

User chạy bridge phải thuộc nhóm `dialout` (`id -nG`). Tháo cánh quạt trước khi thử trên bàn.

## Chạy thử

```bash
.venv/bin/python -m unittest discover tests   # không cần drone
.venv/bin/python main.py --print              # chỉ in telemetry, không cần MQTT
.venv/bin/python main.py                      # gửi lên broker
.venv/bin/python watch.py --seconds 15        # ở cửa sổ khác: nghe lại + kiểm chữ ký
```

## Tự chạy khi Pi bật

```bash
sudo cp deploy/lockr-drone.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lockr-drone
```

| Việc | Lệnh |
|---|---|
| Xem trạng thái | `systemctl status lockr-drone` |
| Xem log trực tiếp | `journalctl -u lockr-drone -f` |
| Khởi động lại | `sudo systemctl restart lockr-drone` |
| Dừng | `sudo systemctl stop lockr-drone` |
| Tắt tự chạy | `sudo systemctl disable --now lockr-drone` |

File dịch vụ ghi sẵn user `lockr` và đường dẫn `/home/lockr/lockr-drone`; Pi khác thì sửa cho khớp.

## Đọc log

- `Đã mở cổng …` / `Mất cổng MAVLink …` — dây USB tới autopilot.
- `Autopilot sysid=… — xin tần suất message` — đã nghe được heartbeat.
- `Đã kết nối broker …` / `Mất kết nối broker …` — mạng của Pi.
- Mỗi phút một dòng tổng kết: `mqtt=… mavlink=… · đã gửi N, bỏ M`.

## Nhận diện ArUco → mở nắp trượt tủ

Kiến trúc dùng lại kênh có sẵn: **Pi drone → backend → MQTT → Pi tủ** (Pi drone không gửi thẳng lệnh cho tủ).
Backend kiểm drone đã ký, đúng nhiệm vụ, đúng chặng (`EN_ROUTE`/`APPROACHING`/`ARRIVED`), đúng mã ô đích rồi
mới gửi lệnh mở **nắp trượt** — không phải lệnh mở cửa ô (relay cửa trước cho khách lấy hàng).

| Bước | Topic | Payload chính |
|---|---|---|
| Pi hỏi nhiệm vụ (khởi động, kết nối lại, mỗi `TARGET_POLL_S`) | `lockr/drone/delivery/events` | `eventType=TARGET_REQUEST, droneId, messageId, timestamp` |
| Backend trả mục tiêu | `lockr/drone/delivery/commands` | `DELIVERY_TARGET: missionId, orderId, lockerId, compartmentId, arucoMarkerId, arucoDictionary, mode` hoặc `NO_TARGET` |
| Camera thấy đúng mã ổn định | `lockr/drone/delivery/events` | `MARKER_DETECTED: missionId, orderId, lockerId, compartmentId, arucoMarkerId, sessionId, stableFrames, stableSeconds, distanceM` |
| Backend → tủ | `cabinet/{lockerId}/command/open` (QoS 1, **không retained**) | `commandId, boxId, lid: true, action: OPEN, source: DRONE_DELIVERY, issuedAt, expiresAt` |
| Tủ trả lời (2 lần) | `cabinet/{lockerId}/command/open/result` | `ACCEPTED, hwState=MOVING` → `SUCCESS/FAILED, hwState, errorCode, lidConfirmation` |
| Backend → Pi | `lockr/drone/delivery/commands` | `MARKER_VALIDATED` → `LID_OPENING` → `LID_OPENED (lidConfirmation, dropSimulationSeconds)` hoặc `LID_OPEN_FAILED` / `MARKER_REJECTED` / `SAFETY_ABORT` → `DELIVERY_READY` |

Trạng thái trên HUD và `/api/status → arucoVision.delivery.state`:
`NO_TARGET` (không có nhiệm vụ — không gửi gì) → `SCANNING` → `STABILIZING` → `REQUEST_SENT` (đã gửi) →
`VALIDATED` (backend đã gửi lệnh cho tủ) → `LID_OPENING` (**tủ đã nhận lệnh**) → `LID_OPEN` (**tủ báo nắp đã mở**)
→ `COMPLETED`; lỗi thì `FAILED` rồi quét lại sau 5 giây (hết lượt gửi vì không có phản hồi: sau 120 giây).

Quy tắc an toàn:

- Mã cần tìm lấy từ nhiệm vụ (cấu hình ô đích ở locker-service), không có mã cứng. Không có nhiệm vụ ⇒ không gửi.
- Nhiều marker trong khung hình: chỉ marker đúng ID của ô đích được xét.
- Phải thấy ổn định `ARUCO_STABLE_FRAMES` frame **và** `ARUCO_STABLE_SECONDS` giây; một frame không bao giờ đủ.
- Mỗi lần tiếp cận gửi một `MARKER_DETECTED`; gửi lại chỉ khi backend im lặng quá `DELIVERY_RESPONSE_TIMEOUT_S`,
  tối đa `DELIVERY_MAX_REQUESTS` lần, cùng `sessionId`. Backend giữ nguyên phiên nên không mở nắp hai lần.
- Lệnh mở nắp của backend có `commandId` duy nhất; gửi lại dùng **đúng** `commandId` đó và giữ nguyên `expiresAt`.
  Pi tủ nhớ `commandId` đã chạy nên không chạy motor lần hai, bỏ lệnh quá hạn, và không chạy motor khi nắp đã mở.
- `lidConfirmation`: `LIMIT_SWITCH` = công tắc hành trình xác nhận mở hoàn toàn; `STEP_COUNT` = nắp đã chạy hết số
  bước nhưng **chưa có cảm biến xác nhận** (tủ hiện tại chưa nối công tắc). Pi tủ bật `LID_BYPASS_PHYSICAL_MOTION`
  (giả lập) thì backend **không** coi là đã mở.
- Nhận diện marker và mở nắp **không** điều khiển drone đáp hay nhả hàng. Ở chế độ demo trong nhà, việc nhả hàng
  được giả lập trong số giây admin đặt ở *Thời gian giả lập drone nhả hàng (giây)*; nắp vẫn mở/đóng thật.

Chạy thử không cần phần cứng:

```bash
.venv/bin/python -m unittest tests.test_delivery_tracker tests.test_camera_aruco_pipeline -v
```

`test_camera_aruco_pipeline` dựng ảnh ArUco bằng OpenCV và chạy đúng hàm xử lý frame của camera
(cần `opencv-contrib-python`, OpenCV 4 hoặc 5).

Kiểm trên phần cứng (chưa làm — cần làm khi lắp):

1. Pi drone và Pi tủ đồng bộ NTP (`timedatectl`): lệch giờ làm backend bỏ sự kiện (> 120 s) và tủ bỏ lệnh theo `expiresAt`.
2. In marker đúng ID/từ điển của ô đích (xem admin → ô tủ), đo lại `ARUCO_MARKER_SIZE_M`; kiểm khoảng cách trên HUD.
3. Có nhiệm vụ ở chặng `APPROACHING` (hoặc demo trong nhà có đúng một đơn DEMO): HUD phải hiện `SCANNING #<mã>`;
   đưa marker ô khác vào khung hình → không có gì gửi đi; đưa đúng marker → một dòng `Đã gửi MARKER_DETECTED` trong log.
4. Trên Pi tủ: `LID_BYPASS_PHYSICAL_MOTION` **không** bật; nắp phải trượt thật; log `DRONE LID OPEN ACCEPTED/SUCCESS`.
5. Rút mạng Pi tủ trước khi drone tới, cắm lại sau khi quá `expiresAt` → tủ báo `COMMAND_EXPIRED`, nắp không mở.
6. Nếu đã nối công tắc hành trình: kiểm `lidConfirmation=LIMIT_SWITCH`, rồi bật `LID_REQUIRE_LIMIT_SWITCH=true` trên Pi tủ.
