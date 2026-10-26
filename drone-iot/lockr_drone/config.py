"""Cấu hình bridge, đọc từ biến môi trường (file .env cạnh main.py)."""
import os
import re
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Nạp KEY=VALUE từ file .env; biến đã có sẵn trong môi trường được ưu tiên."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # Mã drone trên admin Lock.R (drone_units.code) — nằm trong topic MQTT.
    drone_id: str
    # Cổng MAVLink: ưu tiên đường dẫn /dev/serial/by-id/… (không đổi khi rút/cắm lại).
    mavlink_port: str
    mavlink_baud: int
    # Tần suất yêu cầu autopilot gửi từng loại message (Hz).
    message_rate_hz: float
    # Chu kỳ in/gửi một bản tin telemetry (giây).
    publish_interval_s: float
    # Quá ngần này giây không có heartbeat ⇒ coi như mất liên lạc với autopilot.
    heartbeat_timeout_s: float
    # Một nhóm số đo không được làm mới quá ngần này giây ⇒ đánh dấu stale.
    stale_after_s: float
    # ─── MQTT ───
    mqtt_host: str = ""
    mqtt_port: int = 8883
    mqtt_tls: bool = True
    mqtt_transport: str = "tcp"      # tcp | websockets (broker riêng sau Nginx)
    mqtt_ws_path: str = "/mqtt"
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_ca_certs: str = ""
    # Khoá ký bản tin của riêng drone này (bí mật) — xem signing.py.
    device_key: str = ""
    # ─── ArUco → mở nắp tủ (xem delivery.py) ───
    aruco_dictionary: str = "DICT_4X4_50"     # phải trùng từ điển của ô đích (backend gửi kèm mục tiêu)
    aruco_marker_size_m: float = 0.08         # cạnh marker in trên nắp ô, để ước lượng khoảng cách
    aruco_stable_frames: int = 5              # số frame liên tiếp phải thấy đúng mã
    aruco_stable_seconds: float = 0.6         # và thấy liên tục ít nhất chừng này giây
    aruco_lost_reset_s: float = 1.0           # mất dấu quá chừng này giây thì đếm lại
    delivery_response_timeout_s: float = 15.0  # chờ backend trả lời MARKER_DETECTED
    delivery_max_requests: int = 3            # số lần gửi MARKER_DETECTED tối đa mỗi lần tiếp cận
    delivery_lid_timeout_s: float = 180.0     # chờ tủ báo nắp đã mở sau khi backend đã gửi lệnh
    target_poll_s: float = 10.0               # chưa có nhiệm vụ: hỏi backend mỗi chừng này giây

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            drone_id=os.getenv("DRONE_ID", "").strip(),
            mavlink_port=os.getenv("MAVLINK_PORT", "").strip(),
            mavlink_baud=int(os.getenv("MAVLINK_BAUD", 115200)),
            message_rate_hz=_float("MESSAGE_RATE_HZ", 2),
            publish_interval_s=_float("PUBLISH_INTERVAL_S", 1),
            heartbeat_timeout_s=_float("HEARTBEAT_TIMEOUT_S", 5),
            stale_after_s=_float("STALE_AFTER_S", 5),
            mqtt_host=os.getenv("MQTT_HOST", "").strip(),
            mqtt_port=int(os.getenv("MQTT_PORT", 8883)),
            mqtt_tls=os.getenv("MQTT_TLS", "true").strip().lower() in ("1", "true", "yes", "on"),
            mqtt_transport=os.getenv("MQTT_TRANSPORT", "tcp").strip().lower(),
            mqtt_ws_path=os.getenv("MQTT_WS_PATH", "/mqtt"),
            mqtt_username=os.getenv("MQTT_USERNAME", "").strip(),
            mqtt_password=os.getenv("MQTT_PASSWORD", ""),
            mqtt_ca_certs=os.getenv("MQTT_CA_CERTS", "").strip(),
            device_key=os.getenv("DEVICE_KEY", "").strip(),
            aruco_dictionary=os.getenv("ARUCO_DICTIONARY", "DICT_4X4_50").strip().upper(),
            aruco_marker_size_m=_float("ARUCO_MARKER_SIZE_M", 0.08),
            aruco_stable_frames=int(os.getenv("ARUCO_STABLE_FRAMES", 5)),
            aruco_stable_seconds=_float("ARUCO_STABLE_SECONDS", 0.6),
            aruco_lost_reset_s=_float("ARUCO_LOST_RESET_S", 1.0),
            delivery_response_timeout_s=_float("DELIVERY_RESPONSE_TIMEOUT_S", 15),
            delivery_max_requests=int(os.getenv("DELIVERY_MAX_REQUESTS", 3)),
            delivery_lid_timeout_s=_float("DELIVERY_LID_TIMEOUT_S", 180),
            target_poll_s=_float("TARGET_POLL_S", 10),
        )

    def problems(self, mqtt: bool = True) -> list[str]:
        problems = []
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", self.drone_id):
            problems.append("DRONE_ID phải là 1–50 ký tự chữ, số, '-' hoặc '_' (nó nằm trong topic MQTT)")
        if not self.mavlink_port:
            problems.append("MAVLINK_PORT chưa đặt (xem: ls -l /dev/serial/by-id/)")
        if mqtt:
            if not self.mqtt_host:
                problems.append("MQTT_HOST chưa đặt")
            elif self.mqtt_host in ("localhost", "127.0.0.1"):
                problems.append("MQTT_HOST không được là localhost — broker không chạy trên Pi")
            if not self.device_key and not self.mqtt_username:
                problems.append("cần DEVICE_KEY (ký bản tin) hoặc tài khoản broker riêng")
            if self.mqtt_password and not self.mqtt_tls:
                problems.append("có MQTT_PASSWORD thì phải bật MQTT_TLS")
        return problems
