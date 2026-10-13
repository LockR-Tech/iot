"""Test bảng điều khiển kỹ thuật (/service) và các lệnh mới của nắp trượt — chân giả, không cần Pi.

    uv run python -m unittest tests.test_service_panel -v
"""
import json
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException

from hardware.factory import create_gpio_hardware, save_lid_tuning
from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import FakePins
from hardware.lid_controller import LidController, LidState
from infracstructure.service_panel import ServicePanel, require_local_same_origin
from tests.test_gpio_hardware import FakeTravel

RELAYS = [5, 6, 13, 19, 26, 22, 23]       # bản đồ chân tủ TU01
DOORS = [4, 12, 16, 20, 21, 24, 25]


def make_lid(pins, **kw):
    params = dict(pul_pin=18, dir_pin=27, home_pin=17, end_pin=10, steps_per_sec=50_000,
                  start_steps_per_sec=50_000, ramp_steps=0, max_steps=2000, pulse_us=0, steps_per_rev=100)
    params.update(kw)
    return LidController(pins, **params)


def make_settings(**kw):
    s = dict(
        MAC_ADDRESS="2C:CF:67:00:00:00", FIRMWARE_VERSION="test", UNLOCK_PULSE_MS=1000, DOOR_SETTLE_MS=10,
        BACKEND_API_URL="", GPIO_CHIP="AUTO", GPIO_SLAVE_ID=1, GPIO_RELAY_PINS=RELAYS, GPIO_DOOR_PINS=DOORS,
        GPIO_RELAY_ACTIVE_HIGH=True, GPIO_DOOR_CLOSED_LOW=True,
        LID_ENABLED=True, LID_PUL_PIN=18, LID_DIR_PIN=27, LID_HOME_PIN=17, LID_END_PIN=10,
        LID_STEP_ACTIVE_LOW=True, LID_LIMIT_ACTIVE_LOW=True, LID_OPEN_DIR_HIGH=True,
        LID_STEPS_PER_SEC=800, LID_START_STEPS_PER_SEC=200, LID_RAMP_STEPS=200, LID_MAX_STEPS=20000,
        LID_STEPS_PER_REV=1600, LID_PULSE_US=1000, LID_TUNING_FILE=os.path.join(tempfile.gettempdir(), "none.json"),
        LID2_ENABLED=False, LID2_PUL_PIN=9, LID2_DIR_PIN=11, LID2_HOME_PIN=7, LID2_END_PIN=8,
        LID2_OPEN_DIR_HIGH=True, LID2_LIMIT_ACTIVE_LOW=True,
    )
    s.update(kw)
    return types.SimpleNamespace(**s)


class LidJogAndTuneTest(unittest.TestCase):
    def setUp(self):
        self.pins = FakePins()
        self.lid = make_lid(self.pins)
        self.travel = FakeTravel(self.pins, self.lid, travel_steps=1000, start_pos=300)
        self.lid.start()
        self.travel.attach(self.lid._group)

    def test_jog_runs_exact_revolutions(self):
        result = self.lid.jog(2.5, toward_end=True)
        self.assertEqual(result, {"result": "OK", "state": "STOPPED", "steps": 250})
        self.assertEqual(self.travel.pos, 550)
        self.assertEqual(self.lid.last_result["revolutions"], 2.5)

    def test_jog_stops_early_at_limit(self):
        result = self.lid.jog(5, toward_end=False)          # chỉ còn 3 vòng tới gốc
        self.assertEqual(result["state"], "CLOSED")
        self.assertEqual(result["steps"], 300)
        self.assertEqual(self.lid.position, 0)

    def test_position_and_travel_after_home(self):
        self.assertIsNone(self.lid.position)
        self.lid.home()
        self.assertEqual(self.lid.position, 0)
        self.lid.jog(1.5, toward_end=True)
        self.assertEqual(self.lid.status()["positionRevs"], 1.5)
        self.lid.open()
        self.assertEqual(self.lid.status()["travelRevs"], 10.0)

    def test_jog_rejects_zero_and_over_max(self):
        self.assertEqual(self.lid.jog(0, True)["error"], "INVALID_REVOLUTIONS")
        self.assertEqual(self.lid.jog(25, True)["error"], "OVER_MAX_REVS")   # max 2000 bước = 20 vòng

    def test_tune_converts_revolutions_to_steps(self):
        cfg = self.lid.tune(rps=1.5, start_rps=0.5, pulse_us=300, max_revs=12, steps_per_rev=1600)
        self.assertEqual(self.lid.steps_per_sec, 2400)
        self.assertEqual(self.lid.start_steps_per_sec, 800)
        self.assertEqual(self.lid.max_steps, 19200)
        self.assertEqual(cfg["rps"], 1.5)

    def test_tune_rejects_out_of_range_and_unknown(self):
        with self.assertRaises(ValueError):
            self.lid.tune(rps=9)
        with self.assertRaises(ValueError):
            self.lid.tune(speed=1)

    def test_tune_flips_direction_pin(self):
        self.lid.tune(open_dir_high=False)
        self.assertFalse(self.pins.levels[27])

    def test_pulse_never_longer_than_half_interval(self):
        lid = make_lid(FakePins(), pulse_us=1000)
        self.assertAlmostEqual(lid._pulse_seconds(1 / 400), 0.001)        # 2,5 ms chu kỳ ⇒ đủ 1000 µs
        self.assertAlmostEqual(lid._pulse_seconds(1 / 2400), 1 / 4800)    # chu kỳ ngắn ⇒ nửa chu kỳ

    def test_run_async_and_busy(self):
        lid = make_lid(FakePins(), steps_per_sec=2000, start_steps_per_sec=2000, max_steps=100_000).start()
        self.assertEqual(lid.run_async("jog", revolutions=500, toward_end=True)["result"], "STARTED")
        time.sleep(0.05)
        self.assertTrue(lid.busy)
        self.assertEqual(lid.run_async("open")["error"], "LID_BUSY")
        with self.assertRaises(RuntimeError):
            lid.tune(rps=1)
        lid.stop()
        for _ in range(100):
            if not lid.busy:
                break
            time.sleep(0.02)
        self.assertEqual(lid.state, LidState.STOPPED)
        self.assertEqual(lid.last_result["result"], "STOPPED")


class ServicePanelTest(unittest.TestCase):
    def setUp(self):
        self.pins = FakePins()
        for pin in DOORS:
            self.pins.levels[pin] = False
        self.locker = GpioLockerManager(self.pins, relay_pins=RELAYS, door_pins=DOORS,
                                        unlock_ms=20, settle_ms=5, poll_ms=10_000).start()
        self.lid = make_lid(self.pins).start()
        self.saved = 0
        self.fetched = []
        cabinet = types.SimpleNamespace(all_cabinets=[{"id": 7, "name": "CAB-TU01"}])

        def fetch(url):
            self.fetched.append(url)
            return {"success": True, "data": {"cells": [
                {"boxNumber": 1, "rowIndex": 2, "colIndex": 0, "cellType": "XL", "size": "XL", "status": "AVAILABLE"}]}}

        self.panel = ServicePanel(self.locker, {1: self.lid}, cabinet, make_settings(),
                                  save_tuning=self._save, fetch_json=fetch)

    def _save(self):
        self.saved += 1

    def tearDown(self):
        self.locker.close()

    def test_overview_maps_gpio_to_header_pins(self):
        ov = self.panel.overview()
        self.assertEqual(len(ov["boxes"]), 7)
        box2 = ov["boxes"][1]
        self.assertEqual((box2["relayChannel"], box2["relayGpio"], box2["relayPin"]), ("IN2", 6, 31))
        self.assertEqual((box2["doorGpio"], box2["doorPin"]), (12, 32))
        header = {p["pin"]: p for p in ov["header"]}
        self.assertEqual(len(header), 40)
        self.assertEqual(header[29]["label"], "Relay IN1 · ô 1")
        self.assertEqual(header[12]["kind"], "lid1")
        self.assertEqual(header[21]["label"], "Trục 2 PUL− (chưa bật)")   # GPIO9, trục 2 chưa bật
        self.assertFalse(header[21]["used"])
        self.assertEqual(header[1]["kind"], "3v3")
        self.assertEqual([l["enabled"] for l in ov["lids"]], [True, False])
        self.assertEqual(ov["lids"][1]["pins"]["home"], {"gpio": 7, "pin": 26})

    def test_open_box_with_custom_time(self):
        self.pins.history.clear()
        result = self.panel.open_box(3, ms=150)
        self.assertEqual(result["result"], "OK")
        self.assertEqual(self.pins.history, [(13, True), (13, False)])
        self.assertGreaterEqual(result["ms"], 150)
        self.assertEqual(self.panel.actions[-1]["what"], "Mở ô 3 (150 ms)")

    def test_open_box_validates(self):
        with self.assertRaises(ValueError):
            self.panel.open_box(8)
        with self.assertRaises(ValueError):
            self.panel.open_box(1, ms=60_000)

    def test_lid_commands_and_missing_axis(self):
        with self.assertRaises(KeyError):
            self.panel.lid_command(2, "open")
        with self.assertRaises(ValueError):
            self.panel.lid_command(1, "fly")
        with self.assertRaises(ValueError):
            self.panel.jog(1, 50, "open")                 # quá max_revs (20)
        self.assertEqual(self.panel.lid_command(1, "stop")["result"], "OK")

    def test_tune_saves(self):
        cfg = self.panel.tune(1, {"rps": 2})
        self.assertEqual(cfg["rps"], 2.0)
        self.assertEqual(self.saved, 1)

    def test_layout_is_cached(self):
        first = self.panel.layout()
        self.assertEqual(first["cells"][0]["boxNumber"], 1)
        self.panel.layout()
        self.assertEqual(len(self.fetched), 1)
        self.assertTrue(self.fetched[0].endswith("/api/lockers/7/layout"))

    def test_layout_error_does_not_raise(self):
        def boom(url):
            raise OSError("offline")
        panel = ServicePanel(self.locker, {}, types.SimpleNamespace(all_cabinets=[{"id": 7}]), make_settings(),
                             fetch_json=boom)
        self.assertIsNone(panel.layout()["cells"])


class GuardTest(unittest.TestCase):
    @staticmethod
    def req(host, origin=None, host_header="localhost:8800"):
        headers = {"host": host_header}
        if origin:
            headers["origin"] = origin
        return types.SimpleNamespace(client=types.SimpleNamespace(host=host), headers=headers)

    def test_local_same_origin_allowed(self):
        require_local_same_origin(self.req("127.0.0.1"))
        require_local_same_origin(self.req("127.0.0.1", origin="http://localhost:8800"))

    def test_remote_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            require_local_same_origin(self.req("192.0.2.10"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_cross_origin_rejected(self):
        with self.assertRaises(HTTPException):
            require_local_same_origin(self.req("127.0.0.1", origin="http://localhost:3002"))


class FactoryTest(unittest.TestCase):
    def test_two_axes_and_saved_tuning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lid_tuning.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"2": {"rps": 1.25, "pulse_us": 400}}, f)
            settings = make_settings(LID2_ENABLED=True, LID_TUNING_FILE=path)
            locker, lids = create_gpio_hardware(settings, pins=FakePins())
            try:
                self.assertEqual(sorted(lids), [1, 2])
                self.assertEqual(lids[1].pulse_us, 1000)
                self.assertEqual(lids[2].config()["rps"], 1.25)
                self.assertEqual(lids[2].pulse_us, 400)
                lids[1].tune(rps=0.75)
                save_lid_tuning(path, lids)
                with open(path, encoding="utf-8") as f:
                    saved = json.load(f)
                self.assertEqual(saved["1"]["rps"], 0.75)
                self.assertEqual(saved["2"]["pulse_us"], 400)
            finally:
                locker.close()
                for lid in lids.values():
                    lid.shutdown()

    def test_axis2_off_by_default(self):
        locker, lids = create_gpio_hardware(make_settings(), pins=FakePins())
        try:
            self.assertEqual(list(lids), [1])
        finally:
            locker.close()


if __name__ == "__main__":
    unittest.main()
