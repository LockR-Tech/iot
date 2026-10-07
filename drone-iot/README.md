# drone-iot — Pi trên drone gửi telemetry về Lock.R

Chạy trên Raspberry Pi gắn trên drone: đọc MAVLink từ autopilot (ArduPilot) qua USB, gửi telemetry lên broker MQTT cho order-service. Thuộc luồng drone (L1); **tách khỏi code tủ** ở phần còn lại của repo này — không dùng chung module, cấu hình hay dịch vụ nào.

Hợp đồng topic/payload/chữ ký: `../../docs/01-overview/drone-telemetry-contract.md`.

**Chỉ đọc.** Thứ duy nhất gửi xuống autopilot là yêu cầu tần suất message. Không arm, không đổi mode, không điều khiển servo.

## Thành phần

| File | Việc |
|---|---|
| `main.py` | vòng lặp chính: mỗi giây dựng một bản tin và gửi (hoặc in với `--print`) |
| `lockr_drone/mavlink_reader.py` | luồng đọc MAVLink; tự mở lại cổng, tự xin lại tần suất message |
| `lockr_drone/state.py` | gom message, đổi đơn vị, ghi thời điểm từng nhóm số đo |
| `lockr_drone/telemetry.py` | dựng payload schemaVersion 1 |
| `lockr_drone/mqtt_publisher.py` | MQTT 5 + TLS, Last Will, ký bản tin |
| `lockr_drone/signing.py` | chữ ký HMAC |
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
