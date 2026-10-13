"""Dựng bản tin telemetry (schemaVersion 1) từ bản chụp trạng thái."""
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# Field của từng nhóm — nhóm chưa nhận được message nào vẫn xuất đủ field với giá trị null.
GROUP_FIELDS = {
    "position": ("lat", "lng", "relativeAltM", "headingDeg"),
    "gps": ("fixType", "satellites"),
    "battery": ("percent", "voltageV", "currentA"),
    "velocity": ("groundSpeedMs", "climbMs"),
    "flight": ("mode", "armed", "systemStatus"),
    "landed": ("landedState",),
}


def link_state(snapshot: dict, heartbeat_timeout_s: float) -> str:
    """`disconnected`: không mở được cổng · `heartbeat_lost`: cổng mở nhưng autopilot im
    (hoặc chưa từng lên tiếng) · `connected`."""
    if not snapshot["serial_open"]:
        return "disconnected"
    age = snapshot["heartbeat_age_ms"]
    if age is None or age > heartbeat_timeout_s * 1000:
        return "heartbeat_lost"
    return "connected"


def build_payload(
    snapshot: dict,
    drone_id: str,
    sequence: int,
    heartbeat_timeout_s: float,
    stale_after_s: float,
    observed_at: datetime | None = None,
) -> dict:
    observed_at = observed_at or datetime.now(timezone.utc)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "droneId": drone_id,
        "sequence": sequence,
        # Thời điểm Pi chụp số đo (UTC). `ageMs` của từng nhóm tính lùi từ thời điểm này.
        "observedAt": observed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "link": {
            "mavlink": link_state(snapshot, heartbeat_timeout_s),
            "heartbeatAgeMs": snapshot["heartbeat_age_ms"],
        },
    }
    for name, fields in GROUP_FIELDS.items():
        values, age_ms = snapshot["groups"][name]
        group = {key: values.get(key) for key in fields}
        group["ageMs"] = age_ms
        # Giữ giá trị cuối cùng nhưng nói rõ nó đã cũ; chưa từng nhận thì cũng là stale.
        group["stale"] = age_ms is None or age_ms > stale_after_s * 1000
        payload[name] = group
    payload["warnings"] = snapshot["warnings"]
    return payload
