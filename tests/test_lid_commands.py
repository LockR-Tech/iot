"""Hợp đồng MQTT cho lệnh điều khiển nắp/thanh trượt từ admin web (ADMIN + KTV tủ).

    cabinet/{id}/command/lid        {commandId, axis, action: open|close|home|stop}
    cabinet/{id}/command/lid-jog    {commandId, axis, revolutions, direction: open|close}
    cabinet/{id}/command/lid-tune   {commandId, axis, changes:{rps,...}}
    cabinet/{id}/command/panel      {commandId}
    → cabinet/{id}/command/{...}/result {commandId, status, ...}  (khớp commandId)

    uv run python -m unittest tests.test_lid_commands -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.settings import settings
from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import FakePins
from hardware.lid_controller import LidController
from infracstructure.cabinet_state import CabinetState
from services.locker_service import LockerService
from tests.test_mqtt_contract import DOORS, RELAYS, FakeMqtt


def make_lid(pins):
    return LidController(pins, pul_pin=18, dir_pin=27, home_pin=17, end_pin=10,
                         steps_per_sec=50_000, start_steps_per_sec=50_000, ramp_steps=0,
                         max_steps=400, pulse_us=0, steps_per_rev=100).start()


class LidCommandContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mqtt = FakeMqtt()
        self.state = CabinetState(state_path=os.path.join(self.tmp.name, "s.json"), fallback_locker_id="7")
        self.lid = make_lid(FakePins())
        self.service = LockerService(self.mqtt, None, cabinet_state=self.state, lids={1: self.lid})

    def tearDown(self):
        try:
            self.lid.stop()
        except Exception:
            pass
        self.tmp.cleanup()

    def send(self, topic, payload):
        self.service.handle_incoming_message(topic, json.dumps(payload))
        self.service._executor.submit(lambda: None).result(timeout=5)

    def result(self, action):
        return self.mqtt.on(f"cabinet/7/command/{action}/result")[-1]

    def test_lid_open_on_enabled_axis_is_accepted(self):
        self.send("cabinet/7/command/lid", {"commandId": "l-1", "axis": 1, "action": "open"})
        r = self.result("lid")
        self.assertEqual(r["commandId"], "l-1")
        self.assertEqual(r["status"], "ACCEPTED")
        self.assertEqual(r["axis"], 1)
        self.assertIn("lid", r)

    def test_lid_on_disabled_axis_fails(self):
        self.send("cabinet/7/command/lid", {"commandId": "l-2", "axis": 2, "action": "open"})
        r = self.result("lid")
        self.assertEqual(r["status"], "FAILED")
        self.assertEqual(r["errorCode"], "AXIS_DISABLED")

    def test_lid_bad_action_fails(self):
        self.send("cabinet/7/command/lid", {"commandId": "l-3", "axis": 1, "action": "fly"})
        self.assertEqual(self.result("lid")["errorCode"], "BAD_ACTION")

    def test_lid_jog_accepted(self):
        self.send("cabinet/7/command/lid-jog",
                  {"commandId": "j-1", "axis": 1, "revolutions": 1, "direction": "close"})
        r = self.result("lid-jog")
        self.assertEqual(r["status"], "ACCEPTED")
        self.assertEqual(r["direction"], "close")

    def test_lid_jog_rejects_too_many_revs(self):
        self.send("cabinet/7/command/lid-jog",
                  {"commandId": "j-2", "axis": 1, "revolutions": 9999, "direction": "open"})
        self.assertEqual(self.result("lid-jog")["errorCode"], "TOO_MANY_REVS")

    def test_lid_tune_applies_and_reports_config(self):
        self.send("cabinet/7/command/lid-tune",
                  {"commandId": "t-1", "axis": 1, "changes": {"rps": 1.5}})
        r = self.result("lid-tune")
        self.assertEqual(r["status"], "SUCCESS")
        self.assertEqual(r["config"]["rps"], 1.5)

    def test_panel_reports_lid_states(self):
        self.send("cabinet/7/command/panel", {"commandId": "p-1"})
        r = self.result("panel")
        self.assertEqual(r["status"], "SUCCESS")
        self.assertEqual([l["enabled"] for l in r["lids"]], [True, False])


class OpenRoutingTest(unittest.TestCase):
    """Tủ có nắp trượt vẫn mở ô bằng relay; chỉ lệnh drone (`lid: true`) mới trượt nắp."""

    def setUp(self):
        self.patches = [mock.patch.object(settings, "REQUIRE_DOOR_SENSOR", False),
                        mock.patch.object(settings, "LID_BYPASS_PHYSICAL_MOTION", True)]
        for p in self.patches:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.mqtt = FakeMqtt()
        self.state = CabinetState(state_path=os.path.join(self.tmp.name, "s.json"), fallback_locker_id="7")
        self.pins = FakePins()
        self.locker = GpioLockerManager(self.pins, RELAYS, DOORS, unlock_ms=5, settle_ms=5,
                                        close_wait_ms=1, poll_ms=10_000, debounce_ms=50).start()
        self.lid = make_lid(FakePins())
        self.service = LockerService(self.mqtt, None, serial_manager=self.locker,
                                     cabinet_state=self.state, lids={1: self.lid})

    def tearDown(self):
        self.service.shutdown()
        self.locker.close()
        self.tmp.cleanup()
        for p in self.patches:
            p.stop()

    def send(self, payload):
        self.service.handle_incoming_message("cabinet/7/command/open", json.dumps(payload))
        self.service._executor.submit(lambda: None).result(timeout=5)

    def relay_pulses(self):
        return [pin for pin, high in self.pins.history if high]

    def test_open_without_lid_flag_pulses_relay(self):
        # slot 1 trùng số với trục 1 — trước đây bị nhầm sang nắp trượt.
        for slot in (1, 4):
            with self.subTest(slot=slot):
                self.pins.history.clear()
                self.send({"commandId": f"o-{slot}", "boxId": 40 + slot, "slotIndex": slot, "action": "OPEN"})
                r = self.mqtt.on("cabinet/7/command/open/result")[-1]
                self.assertEqual(r["status"], "SUCCESS")
                self.assertEqual(r["errorMessage"], "Door opened")
                self.assertEqual(self.relay_pulses(), [RELAYS[slot]])

    def test_open_with_lid_flag_uses_lid_not_relay(self):
        self.send({"commandId": "d-1", "boxId": 40, "slotIndex": 2, "action": "OPEN", "lid": True})
        r = self.mqtt.on("cabinet/7/command/open/result")[-1]
        self.assertEqual(r["status"], "SUCCESS")
        self.assertIn("hardware bypass", r["errorMessage"])
        self.assertEqual(self.relay_pulses(), [])


if __name__ == "__main__":
    unittest.main()
