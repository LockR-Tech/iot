"""Chạy: python -m unittest discover tests   (không cần pymavlink, không cần drone)."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lockr_drone.state import DroneState  # noqa: E402
from lockr_drone.telemetry import build_payload  # noqa: E402


def message(kind, **fields):
    return SimpleNamespace(get_type=lambda: kind, **fields)


def payload_of(state, now, **overrides):
    options = dict(heartbeat_timeout_s=5, stale_after_s=5,
                   observed_at=datetime(2026, 10, 4, 8, 0, 0, 123000, tzinfo=timezone.utc))
    options.update(overrides)
    return build_payload(state.snapshot(now=now), "S550-001", 7, **options)


class TelemetryPayloadTest(unittest.TestCase):

    def test_nothing_received_yet_is_all_null_and_stale(self):
        state = DroneState()
        payload = payload_of(state, now=100.0)

        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["droneId"], "S550-001")
        self.assertEqual(payload["sequence"], 7)
        self.assertEqual(payload["observedAt"], "2026-10-04T08:00:00.123Z")
        self.assertEqual(payload["link"], {"mavlink": "disconnected", "heartbeatAgeMs": None})
        self.assertEqual(payload["position"],
                         {"lat": None, "lng": None, "relativeAltM": None, "headingDeg": None,
                          "ageMs": None, "stale": True})
        self.assertIsNone(payload["flight"]["armed"])
        self.assertEqual(payload["warnings"], [])

    def test_units_are_converted(self):
        state = DroneState()
        state.set_serial_open(True)
        state.apply(message("HEARTBEAT", base_mode=128 | 1, system_status=4), mode="AUTO", now=100.0)
        state.apply(message("GLOBAL_POSITION_INT", lat=108412345, lon=1068098765,
                            relative_alt=25340, hdg=8750), now=100.0)
        state.apply(message("GPS_RAW_INT", fix_type=3, satellites_visible=14), now=100.0)
        state.apply(message("SYS_STATUS", battery_remaining=76, voltage_battery=15830,
                            current_battery=1250), now=100.0)
        state.apply(message("VFR_HUD", groundspeed=6.234, climb=-0.5), now=100.0)
        state.apply(message("EXTENDED_SYS_STATE", landed_state=2), now=100.0)

        payload = payload_of(state, now=100.5)

        self.assertEqual(payload["link"], {"mavlink": "connected", "heartbeatAgeMs": 500})
        self.assertEqual(payload["position"],
                         {"lat": 10.8412345, "lng": 106.8098765, "relativeAltM": 25.34,
                          "headingDeg": 87.5, "ageMs": 500, "stale": False})
        self.assertEqual(payload["gps"], {"fixType": 3, "satellites": 14, "ageMs": 500, "stale": False})
        self.assertEqual(payload["battery"],
                         {"percent": 76, "voltageV": 15.83, "currentA": 12.5, "ageMs": 500, "stale": False})
        self.assertEqual(payload["velocity"],
                         {"groundSpeedMs": 6.23, "climbMs": -0.5, "ageMs": 500, "stale": False})
        self.assertEqual(payload["flight"],
                         {"mode": "AUTO", "armed": True, "systemStatus": "ACTIVE", "ageMs": 500, "stale": False})
        self.assertEqual(payload["landed"]["landedState"], "IN_AIR")

    def test_unknown_sentinels_become_null(self):
        state = DroneState()
        state.apply(message("GLOBAL_POSITION_INT", lat=0, lon=0, relative_alt=0, hdg=65535), now=1.0)
        state.apply(message("GPS_RAW_INT", fix_type=0, satellites_visible=255), now=1.0)
        state.apply(message("SYS_STATUS", battery_remaining=-1, voltage_battery=65535,
                            current_battery=-1), now=1.0)
        state.apply(message("EXTENDED_SYS_STATE", landed_state=0), now=1.0)

        payload = payload_of(state, now=1.0)

        # Toạ độ (0, 0) là "chưa có vị trí", không phải một điểm ngoài khơi châu Phi.
        self.assertIsNone(payload["position"]["lat"])
        self.assertIsNone(payload["position"]["lng"])
        self.assertIsNone(payload["position"]["headingDeg"])
        self.assertIsNone(payload["gps"]["satellites"])
        self.assertEqual(payload["battery"]["percent"], None)
        self.assertEqual(payload["battery"]["voltageV"], None)
        self.assertEqual(payload["battery"]["currentA"], None)
        # MAV_LANDED_STATE_UNDEFINED: không đoán là đã đáp.
        self.assertIsNone(payload["landed"]["landedState"])

    def test_heartbeat_lost_is_not_the_same_as_port_closed(self):
        state = DroneState()
        state.set_serial_open(True)
        self.assertEqual(payload_of(state, now=1.0)["link"]["mavlink"], "heartbeat_lost")

        state.apply(message("HEARTBEAT", base_mode=0, system_status=3), mode="STABILIZE", now=1.0)
        self.assertEqual(payload_of(state, now=2.0)["link"]["mavlink"], "connected")
        self.assertEqual(payload_of(state, now=7.5)["link"]["mavlink"], "heartbeat_lost")

        state.set_serial_open(False)
        self.assertEqual(payload_of(state, now=7.5)["link"]["mavlink"], "disconnected")

    def test_old_group_keeps_last_value_but_is_marked_stale(self):
        state = DroneState()
        state.apply(message("VFR_HUD", groundspeed=4.0, climb=0.0), now=10.0)
        state.apply(message("GPS_RAW_INT", fix_type=3, satellites_visible=9), now=19.0)

        payload = payload_of(state, now=20.0)

        self.assertEqual(payload["velocity"]["groundSpeedMs"], 4.0)
        self.assertEqual(payload["velocity"]["ageMs"], 10000)
        self.assertTrue(payload["velocity"]["stale"])
        self.assertFalse(payload["gps"]["stale"])

    def test_only_warnings_and_worse_are_kept_and_capped(self):
        state = DroneState()
        state.apply(message("STATUSTEXT", severity=6, text="EKF3 IMU0 initialised"), now=1.0)
        for index in range(7):
            state.apply(message("STATUSTEXT", severity=4, text=f"PreArm: check {index}"), now=1.0)

        warnings = payload_of(state, now=1.0)["warnings"]

        self.assertEqual([w["text"] for w in warnings], [f"PreArm: check {i}" for i in range(2, 7)])
        self.assertEqual(warnings[0]["severity"], "WARNING")


if __name__ == "__main__":
    unittest.main()
