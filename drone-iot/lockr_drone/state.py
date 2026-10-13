"""Trạng thái drone gom từ các message MAVLink, kèm thời điểm từng nhóm được làm mới.

Không phụ thuộc pymavlink: nhận message qua `get_type()` và thuộc tính, nên test được
bằng đối tượng giả. Đơn vị MAVLink được đổi sang đơn vị thường dùng ngay tại đây; giá trị
autopilot báo "không biết" (65535, -1, 255…) thành None.
"""
import threading
import time
from dataclasses import dataclass, field

# MAV_LANDED_STATE
LANDED_STATES = {1: "ON_GROUND", 2: "IN_AIR", 3: "TAKEOFF", 4: "LANDING"}
# MAV_STATE
SYSTEM_STATES = {
    0: "UNINIT", 1: "BOOT", 2: "CALIBRATING", 3: "STANDBY", 4: "ACTIVE",
    5: "CRITICAL", 6: "EMERGENCY", 7: "POWEROFF", 8: "FLIGHT_TERMINATION",
}
SEVERITIES = ["EMERGENCY", "ALERT", "CRITICAL", "ERROR", "WARNING", "NOTICE", "INFO", "DEBUG"]
ARMED_FLAG = 128  # MAV_MODE_FLAG_SAFETY_ARMED
MAX_WARNINGS = 5
# Chỉ giữ STATUSTEXT từ WARNING trở lên (số nhỏ = nặng hơn).
WARNING_SEVERITY = 4


@dataclass
class Group:
    """Một nhóm số đo đến từ cùng một message, cùng thời điểm."""
    values: dict = field(default_factory=dict)
    updated_at: float | None = None  # time.monotonic()

    def set(self, now: float, **values) -> None:
        self.values = values
        self.updated_at = now

    def age_ms(self, now: float) -> int | None:
        return None if self.updated_at is None else int((now - self.updated_at) * 1000)


class DroneState:
    def __init__(self):
        self._lock = threading.Lock()
        self.serial_open = False
        self.last_heartbeat_at: float | None = None
        self.autopilot_sysid: int | None = None
        self.position = Group()
        self.gps = Group()
        self.battery = Group()
        self.velocity = Group()
        self.flight = Group()
        self.landed = Group()
        self.warnings: list[dict] = []

    def set_serial_open(self, is_open: bool) -> None:
        with self._lock:
            self.serial_open = is_open

    def apply(self, msg, mode: str | None = None, now: float | None = None) -> None:
        """Gộp một message MAVLink. `mode` là tên flight mode đã giải mã (chỉ cho HEARTBEAT)."""
        now = time.monotonic() if now is None else now
        kind = msg.get_type()
        with self._lock:
            if kind == "HEARTBEAT":
                self.last_heartbeat_at = now
                self.flight.set(
                    now,
                    mode=mode,
                    armed=bool(msg.base_mode & ARMED_FLAG),
                    systemStatus=SYSTEM_STATES.get(msg.system_status),
                )
            elif kind == "GLOBAL_POSITION_INT":
                # Chưa có ước lượng vị trí (GPS chưa khoá) thì autopilot gửi đúng (0, 0).
                located = not (msg.lat == 0 and msg.lon == 0)
                self.position.set(
                    now,
                    lat=msg.lat / 1e7 if located else None,
                    lng=msg.lon / 1e7 if located else None,
                    relativeAltM=round(msg.relative_alt / 1000, 2),
                    headingDeg=None if msg.hdg == 65535 else msg.hdg / 100,
                )
            elif kind == "GPS_RAW_INT":
                self.gps.set(
                    now,
                    fixType=msg.fix_type,
                    satellites=None if msg.satellites_visible == 255 else msg.satellites_visible,
                )
            elif kind == "SYS_STATUS":
                self.battery.set(
                    now,
                    percent=None if msg.battery_remaining < 0 else msg.battery_remaining,
                    voltageV=None if msg.voltage_battery == 65535 else round(msg.voltage_battery / 1000, 2),
                    currentA=None if msg.current_battery < 0 else round(msg.current_battery / 100, 2),
                )
            elif kind == "VFR_HUD":
                self.velocity.set(
                    now,
                    groundSpeedMs=round(msg.groundspeed, 2),
                    climbMs=round(msg.climb, 2),
                )
            elif kind == "EXTENDED_SYS_STATE":
                self.landed.set(now, landedState=LANDED_STATES.get(msg.landed_state))
            elif kind == "STATUSTEXT" and msg.severity <= WARNING_SEVERITY:
                self.warnings.append({
                    "severity": SEVERITIES[msg.severity],
                    "text": str(msg.text).rstrip("\x00"),
                    "at": int(time.time() * 1000),
                })
                del self.warnings[:-MAX_WARNINGS]

    def snapshot(self, now: float | None = None) -> dict:
        """Bản chụp nhất quán để dựng payload: mỗi nhóm là (values, age_ms)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            return {
                "serial_open": self.serial_open,
                "heartbeat_age_ms": None if self.last_heartbeat_at is None
                else int((now - self.last_heartbeat_at) * 1000),
                "groups": {
                    name: (dict(group.values), group.age_ms(now))
                    for name, group in (
                        ("position", self.position), ("gps", self.gps), ("battery", self.battery),
                        ("velocity", self.velocity), ("flight", self.flight), ("landed", self.landed),
                    )
                },
                "warnings": list(self.warnings),
            }
