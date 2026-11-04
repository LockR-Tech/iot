"""Nắp trượt chưa có công tắc hành trình: Pi đếm bước để biết bàn trượt ở đâu (tủ CAB-TU01, ray 36 vòng).

    uv run python -m unittest tests.test_lid_position -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hardware.factory import restore_lid_positions
from hardware.gpio_pins import FakePins
from hardware.lid_controller import LidController

PUL, DIR, HOME, END = 18, 27, 17, 10
SPR, MAX_REVS = 100, 36          # 100 bước/vòng cho test nhanh; ray 36 vòng như tủ thật


def make_lid(pins, position_travel=True):
    return LidController(pins, pul_pin=PUL, dir_pin=DIR, home_pin=HOME, end_pin=END,
                         steps_per_sec=50_000, start_steps_per_sec=50_000, ramp_steps=0,
                         max_steps=MAX_REVS * SPR, pulse_us=0, steps_per_rev=SPR, step_active_low=False,
                         position_travel=position_travel).start()


class LidPositionTest(unittest.TestCase):
    def setUp(self):
        self.pins = FakePins()
        self.lid = make_lid(self.pins)

    def tearDown(self):
        self.lid.shutdown()

    def pulses(self):
        return sum(1 for pin, high in self.pins.history if pin == PUL and high)

    def test_open_and_close_run_only_the_remaining_travel(self):
        self.lid.set_position(2)                       # bàn trượt cách gốc 2 vòng
        result = self.lid.open()
        self.assertEqual(result["result"], "OK")
        self.assertEqual(result["steps"], 34 * SPR, "phải chạy đúng 34 vòng còn lại tới cuối ray")
        self.assertEqual(self.lid.status()["state"], "OPEN")
        self.assertEqual(self.lid.status()["positionRevs"], 36)

        before = self.pulses()
        self.assertEqual(self.lid.open()["steps"], 0, "đã ở cuối ray mà vẫn đẩy thêm")
        self.assertEqual(self.pulses(), before)

        result = self.lid.close()
        self.assertEqual(result["steps"], 36 * SPR)
        self.assertEqual(self.lid.status()["state"], "CLOSED")
        self.assertEqual(self.lid.status()["positionRevs"], 0)

    def test_unknown_position_runs_the_whole_rail_once_and_takes_it_as_reference(self):
        result = self.lid.open()
        self.assertEqual(result["result"], "OK")
        self.assertEqual(result["steps"], 36 * SPR)
        self.assertEqual(self.lid.status()["state"], "OPEN", "không có công tắc thì chạy hết ray không phải lỗi")
        self.assertEqual(self.lid.status()["positionRevs"], 36)
        self.assertEqual(self.lid.close()["steps"], 36 * SPR)

    def test_jog_past_the_rail_end_is_clamped(self):
        self.lid.set_position(35)
        self.lid.jog(3, toward_end=True)
        self.assertEqual(self.lid.status()["positionRevs"], 36)
        self.assertEqual(self.lid.status()["state"], "OPEN")
        self.lid.jog(10, toward_end=False)
        self.assertEqual(self.lid.status()["positionRevs"], 26)
        self.assertEqual(self.lid.status()["state"], "STOPPED")

    def test_changing_steps_per_rev_keeps_the_position_in_revolutions(self):
        self.lid.set_position(10)
        self.lid.tune(steps_per_rev=200, max_revs=36)
        self.assertEqual(self.lid.position, 2000)
        self.assertEqual(self.lid.status()["positionRevs"], 10)

    def test_position_out_of_the_rail_is_refused(self):
        with self.assertRaises(ValueError):
            self.lid.set_position(40)

    def test_without_position_travel_behaviour_is_unchanged(self):
        lid = make_lid(FakePins(), position_travel=False)
        try:
            result = lid.open()
            self.assertEqual(result["error"], "LIMIT_NOT_REACHED")
            self.assertEqual(lid.status()["state"], "FAULT")
        finally:
            lid.shutdown()


class ChaydanProfileTest(unittest.TestCase):
    """Dạng xung giống lệnh `chaydan` kỹ thuật tủ đã kiểm chứng: 100 bước ≈10,8 ms → 100 bước ≈4,8 ms →
    chạy đều ở rps; xung = nửa chu kỳ."""

    def setUp(self):
        self.lid = LidController(FakePins(), pul_pin=PUL, dir_pin=DIR, home_pin=HOME, end_pin=END,
                                 steps_per_sec=358, steps_per_rev=200, max_steps=7200,
                                 step_active_low=False, profile="chaydan")

    def test_stage_periods_follow_the_chaydan_command(self):
        self.assertAlmostEqual(self.lid._interval(0), 0.0108)
        self.assertAlmostEqual(self.lid._interval(99), 0.0108)
        self.assertAlmostEqual(self.lid._interval(100), 0.0048)
        self.assertAlmostEqual(self.lid._interval(199), 0.0048)
        self.assertAlmostEqual(self.lid._interval(200), 1 / 358)
        self.assertAlmostEqual(self.lid._interval(5000), 1 / 358)

    def test_pulse_is_half_the_period_like_pinctrl_dh_dl(self):
        for step in (0, 150, 3000):
            interval = self.lid._interval(step)
            self.assertAlmostEqual(self.lid._pulse_seconds(interval), interval / 2)

    def test_profile_is_tunable_and_saved_in_config(self):
        lid = make_lid(FakePins())
        try:
            self.assertEqual(lid.config()["profile"], "linear")
            self.assertEqual(lid.tune(profile="chaydan", rps=1.79)["profile"], "chaydan")
            self.assertEqual(lid.tune(rps=1.5)["profile"], "chaydan", "lưu từ bảng /service làm mất profile")
            with self.assertRaises(ValueError):
                lid.tune(profile="nhanh")
        finally:
            lid.shutdown()


class LidPositionStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "lid_position.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_position_survives_a_restart(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"1": {"positionRevs": 2}, "2": {"positionRevs": 36}}, f)
        lids = {1: make_lid(FakePins()), 2: make_lid(FakePins())}
        try:
            restore_lid_positions(lids, self.path)
            self.assertEqual(lids[1].status()["positionRevs"], 2)
            self.assertEqual(lids[2].status()["state"], "OPEN")

            lids[1].open()                                # chạy xong ⇒ ghi file
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["1"]["positionRevs"], 36)
            self.assertEqual(saved["2"]["positionRevs"], 36)
        finally:
            for lid in lids.values():
                lid.shutdown()

        again = {1: make_lid(FakePins())}
        try:
            restore_lid_positions(again, self.path)
            self.assertEqual(again[1].status()["state"], "OPEN")
            self.assertEqual(again[1].open()["steps"], 0)
        finally:
            again[1].shutdown()

    def test_missing_or_broken_file_means_unknown_position(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{hỏng")
        lids = {1: make_lid(FakePins())}
        try:
            restore_lid_positions(lids, self.path)
            self.assertIsNone(lids[1].status()["positionRevs"])
        finally:
            lids[1].shutdown()


if __name__ == "__main__":
    unittest.main()
