"""Lệnh nắp trượt của luồng drone: `cabinet/{id}/command/open|close` có `lid: true`.

    Backend gửi {commandId, boxId, lid: true, action, issuedAt, expiresAt?}; Pi trả
    `.../result` hai lần: ACCEPTED (đã nhận, nắp đang chạy) rồi SUCCESS/FAILED.

    uv run python -m unittest tests.test_drone_lid_commands -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.settings import settings
from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import FakePins
from hardware.lid_controller import LidController
from infracstructure.cabinet_state import CabinetState
from services.locker_service import LockerService
from tests.test_mqtt_contract import DOORS, RELAYS, FakeMqtt

PUL, DIR, HOME, END = 18, 27, 17, 10


def make_lid(pins):
    return LidController(pins, pul_pin=PUL, dir_pin=DIR, home_pin=HOME, end_pin=END,
                         steps_per_sec=50_000, start_steps_per_sec=50_000, ramp_steps=0,
                         max_steps=400, pulse_us=0, steps_per_rev=100).start()


class DroneLidCommandTest(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(settings, "REQUIRE_DOOR_SENSOR", False),
                        mock.patch.object(settings, "LID_BYPASS_PHYSICAL_MOTION", False),
                        mock.patch.object(settings, "LID_REQUIRE_LIMIT_SWITCH", False)]
        for p in self.patches:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.mqtt = FakeMqtt()
        self.state = CabinetState(state_path=os.path.join(self.tmp.name, "s.json"), fallback_locker_id="7")
        self.door_pins = FakePins()
        self.locker = GpioLockerManager(self.door_pins, RELAYS, DOORS, unlock_ms=5, settle_ms=5,
                                        close_wait_ms=1, poll_ms=10_000, debounce_ms=50).start()
        self.lid_pins = FakePins()
        self.lid = make_lid(self.lid_pins)
        self.service = LockerService(self.mqtt, None, serial_manager=self.locker,
                                     cabinet_state=self.state, lids={1: self.lid})

    def tearDown(self):
        self.service.shutdown()
        self.locker.close()
        self.tmp.cleanup()
        for p in self.patches:
            p.stop()

    # ─── helpers ───

    def send(self, action, command_id, **extra):
        payload = {"commandId": command_id, "boxId": 41, "slotIndex": 1, "action": action.upper(),
                   "lid": True, "source": "DRONE_DELIVERY"}
        payload.update(extra)
        self.service.handle_incoming_message(f"cabinet/7/command/{action}", json.dumps(payload))
        self.service._executor.submit(lambda: None).result(timeout=10)

    def results(self, action, command_id):
        return [r for r in self.mqtt.on(f"cabinet/7/command/{action}/result") if r["commandId"] == command_id]

    def motor_pulses(self):
        return sum(1 for pin, high in self.lid_pins.history if pin == PUL and high)

    def relay_pulses(self):
        return [pin for pin, high in self.door_pins.history if high]

    @staticmethod
    def in_seconds(seconds):
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    # ─── 1. đúng lệnh ⇒ nhận lệnh rồi trượt nắp thật ───

    def test_open_is_acknowledged_then_runs_the_real_lid_motor(self):
        self.send("open", "drone-1", expiresAt=self.in_seconds(60))

        first, last = self.results("open", "drone-1")
        self.assertEqual(first["status"], "ACCEPTED")
        self.assertEqual(first["hwState"], "MOVING")
        self.assertEqual(last["status"], "SUCCESS")
        self.assertEqual(last["hwState"], "OPEN")
        self.assertGreater(self.motor_pulses(), 0, "phải trượt nắp thật")
        self.assertEqual(self.relay_pulses(), [], "lệnh nắp drone không được kích relay cửa ô")

    def test_without_limit_switch_the_open_is_reported_as_unconfirmed(self):
        self.send("open", "drone-2")

        last = self.results("open", "drone-2")[-1]
        self.assertEqual(last["status"], "SUCCESS")
        self.assertEqual(last["lidConfirmation"], "STEP_COUNT")
        self.assertIn("chưa có công tắc hành trình", last["errorMessage"])

    def test_limit_switch_reached_is_reported_as_confirmed(self):
        # Công tắc cuối hành trình đã chạm (mức thấp, active-low) ⇒ xác nhận bằng công tắc.
        self.lid_pins.levels[END] = False
        self.send("open", "drone-3")

        last = self.results("open", "drone-3")[-1]
        self.assertEqual(last["status"], "SUCCESS")
        self.assertEqual(last["lidConfirmation"], "LIMIT_SWITCH")

    # ─── 4. mất ACK rồi backend gửi lại cùng commandId ⇒ không chạy motor lần hai ───

    def test_retry_with_the_same_command_id_does_not_run_the_motor_again(self):
        self.send("open", "drone-4")
        pulses = self.motor_pulses()
        self.lid.state = self.lid.state.__class__("UNKNOWN")   # kể cả khi trạng thái nắp không rõ

        self.send("open", "drone-4")

        self.assertEqual(self.motor_pulses(), pulses)
        replay = self.results("open", "drone-4")[-1]
        self.assertEqual(replay["status"], "SUCCESS")
        self.assertTrue(replay["duplicate"])

    # ─── 5. nắp đã mở ⇒ lệnh mới không kích motor ───

    def test_lid_already_open_does_not_trigger_the_motor(self):
        self.lid_pins.levels[END] = False
        self.send("open", "drone-5a")
        pulses = self.motor_pulses()

        self.send("open", "drone-5b")

        self.assertEqual(self.motor_pulses(), pulses)
        last = self.results("open", "drone-5b")[-1]
        self.assertEqual(last["status"], "SUCCESS")
        self.assertTrue(last["alreadyInPosition"])
        self.assertEqual(len(self.results("open", "drone-5b")), 1, "không báo ACCEPTED khi không chạy motor")

    # ─── 6. lệnh hết hạn / lỗi ⇒ trạng thái đúng ───

    def test_expired_open_command_is_dropped_without_moving_the_lid(self):
        self.send("open", "drone-6", expiresAt=self.in_seconds(-5))

        (only,) = self.results("open", "drone-6")
        self.assertEqual(only["status"], "FAILED")
        self.assertEqual(only["errorCode"], "COMMAND_EXPIRED")
        self.assertEqual(self.motor_pulses(), 0)

    def test_expiry_in_the_backend_format_is_understood(self):
        # Backend gửi Instant.toString() cắt về mili giây, ví dụ 2026-10-24T12:35:09.762Z.
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        self.send("open", "drone-6e", expiresAt=future)
        self.assertEqual(self.results("open", "drone-6e")[-1]["status"], "SUCCESS")

    def test_unreadable_expiry_is_treated_as_expired(self):
        self.send("open", "drone-6b", expiresAt="không-phải-thời-gian")

        self.assertEqual(self.results("open", "drone-6b")[-1]["errorCode"], "COMMAND_EXPIRED")
        self.assertEqual(self.motor_pulses(), 0)

    def test_required_limit_switch_turns_an_unconfirmed_open_into_a_failure(self):
        with mock.patch.object(settings, "LID_REQUIRE_LIMIT_SWITCH", True):
            self.send("open", "drone-6c")

        last = self.results("open", "drone-6c")[-1]
        self.assertEqual(last["status"], "FAILED")
        self.assertEqual(last["errorCode"], "LIMIT_NOT_REACHED")

    def test_required_switch_rejects_stale_open_state_without_sensor_confirmation(self):
        self.lid.state = self.lid.state.__class__("OPEN")
        with mock.patch.object(settings, "LID_REQUIRE_LIMIT_SWITCH", True):
            self.send("open", "drone-stale-open")

        (result,) = self.results("open", "drone-stale-open")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["errorCode"], "LIMIT_NOT_REACHED")
        self.assertEqual(self.motor_pulses(), 0)

    def test_bypass_mode_says_so_instead_of_claiming_a_real_open(self):
        with mock.patch.object(settings, "LID_BYPASS_PHYSICAL_MOTION", True):
            self.send("open", "drone-6d")

        last = self.results("open", "drone-6d")[-1]
        self.assertEqual(last["lidConfirmation"], "BYPASS")
        self.assertEqual(self.motor_pulses(), 0)

    def test_close_retry_is_idempotent_too_and_close_ignores_expiry(self):
        self.send("open", "drone-7-open")
        self.send("close", "drone-7-close", expiresAt=self.in_seconds(-5))   # đóng nắp không bị chặn vì hạn
        pulses = self.motor_pulses()
        closed = self.results("close", "drone-7-close")[-1]
        self.assertEqual(closed["status"], "SUCCESS")
        self.assertEqual(closed["hwState"], "CLOSED")

        self.send("close", "drone-7-close")

        self.assertEqual(self.motor_pulses(), pulses)
        self.assertTrue(self.results("close", "drone-7-close")[-1]["duplicate"])


if __name__ == "__main__":
    unittest.main()
