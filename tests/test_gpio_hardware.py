"""Test bộ điều khiển GPIO (khoá + nắp trượt) trên chân giả — không cần Pi.

    uv run python -m unittest tests.test_gpio_hardware -v
"""
import os
import sys
import threading
import time
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import FakePins
from hardware.lid_controller import LidController, LidState

RELAYS = [17, 27, 22, 23, 24, 25, 16]
DOORS = [5, 6, 12, 13, 19, 26, 20]


def make_locker(pins, **kw):
    params = dict(relay_pins=RELAYS, door_pins=DOORS, unlock_ms=20, settle_ms=10,
                  close_wait_ms=5, poll_ms=10_000, debounce_ms=200)
    params.update(kw)
    return GpioLockerManager(pins, **params)


class GpioLockerTest(unittest.TestCase):
    def setUp(self):
        self.pins = FakePins()
        for pin in DOORS:
            self.pins.levels[pin] = False          # LOW = cửa đóng
        self.locker = make_locker(self.pins).start()

    def tearDown(self):
        self.locker.close()

    def test_relays_start_off(self):
        for pin in RELAYS:
            self.assertFalse(self.pins.levels[pin], f"relay GPIO{pin} phải tắt lúc khởi động")

    def test_relays_start_off_when_active_low(self):
        pins = FakePins()
        locker = make_locker(pins, relay_active_high=False).start()
        try:
            self.assertTrue(all(pins.levels[p] for p in RELAYS))   # active LOW ⇒ tắt = HIGH
        finally:
            locker.close()

    def test_open_slot_pulses_only_that_relay_then_reads_door(self):
        self.pins.history.clear()
        self.pins.levels[DOORS[2]] = True           # cửa bật ra
        result = self.locker.open_slot(2, slave_id=1)
        self.assertEqual(result["result"], "OK")
        self.assertEqual(result["slot"], 2)
        self.assertEqual(result["gpio"], RELAYS[2])
        self.assertFalse(result["door"])            # door=False ⇒ đã mở (như firmware)
        self.assertEqual(self.pins.history, [(RELAYS[2], True), (RELAYS[2], False)])

    def test_open_reports_door_still_closed(self):
        result = self.locker.open_slot(0, slave_id=1)
        self.assertTrue(result["door"])             # LockerService coi là JAMMED

    def test_relay_released_even_if_sleep_fails(self):
        original_sleep = time.sleep

        def boom(_):
            raise RuntimeError("interrupted")

        time.sleep = boom
        try:
            with self.assertRaises(RuntimeError):
                self.locker.open_slot(1, slave_id=1)
        finally:
            time.sleep = original_sleep
        self.assertFalse(self.pins.levels[RELAYS[1]], "cuộn khoá phải được ngắt")

    def test_invalid_slot_and_slave(self):
        self.assertEqual(self.locker.open_slot(7, slave_id=1)["error"], "INVALID_SLOT")
        self.assertEqual(self.locker.open_slot(-1, slave_id=1)["error"], "INVALID_SLOT")
        self.assertEqual(self.locker.open_slot(0, slave_id=2)["error"], "UNKNOWN_SLAVE")
        self.assertEqual(self.pins.history, [], "không được kích relay nào")

    def test_close_slot_turns_relay_off(self):
        result = self.locker.close_slot(3, slave_id=1)
        self.assertEqual(result["result"], "OK")
        self.assertFalse(self.pins.levels[RELAYS[3]])

    def test_scan_slaves(self):
        self.assertEqual(self.locker.scan_slaves(1, 1), [{"slaveId": 1, "availableSlots": 7}])
        self.assertEqual(self.locker.scan_slaves(2, 5), [])

    def test_only_one_lock_energised_at_a_time(self):
        energised, peak = set(), [0]
        group_write = self.locker._group.write

        def tracking_write(pin, high):
            group_write(pin, high)
            if pin in RELAYS:
                (energised.add if high else energised.discard)(pin)
                peak[0] = max(peak[0], len(energised))

        self.locker._group.write = tracking_write
        threads = [threading.Thread(target=self.locker.open_slot, args=(i,), kwargs={"slave_id": 1})
                   for i in range(7)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(peak[0], 1)

    def test_door_events_are_debounced(self):
        events = []
        self.locker.on_door_event = lambda slot, event, slave_id=1: events.append((slot, event))
        t0 = 1000.0
        self.pins.levels[DOORS[4]] = True           # cửa 4 mở
        self.locker.poll_once(now=t0)               # ghi nhận thay đổi thô
        self.locker.poll_once(now=t0 + 0.1)         # mới 100 ms — chưa báo
        self.assertEqual(events, [])
        self.locker.poll_once(now=t0 + 0.25)        # ổn định 250 ms ≥ 200 ms
        self.assertEqual(events, [(4, "DOOR_OPENED")])
        self.locker.poll_once(now=t0 + 0.5)
        self.assertEqual(len(events), 1, "không báo lặp")

    def test_bounce_shorter_than_debounce_is_ignored(self):
        events = []
        self.locker.on_door_event = lambda slot, event, slave_id=1: events.append((slot, event))
        t0 = 2000.0
        self.pins.levels[DOORS[0]] = True
        self.locker.poll_once(now=t0)
        self.pins.levels[DOORS[0]] = False          # dội về trong 50 ms
        self.locker.poll_once(now=t0 + 0.05)
        self.locker.poll_once(now=t0 + 0.5)
        self.assertEqual(events, [])

    def test_rejects_pin_overlap(self):
        with self.assertRaises(ValueError):
            GpioLockerManager(FakePins(), relay_pins=[17, 5], door_pins=[5, 6])


class FakeTravel:
    """Mô phỏng nắp: đếm xung PUL theo chiều DIR, bật công tắc khi tới đầu/cuối."""

    def __init__(self, pins, lid, travel_steps, start_pos=0):
        self.pins, self.lid, self.travel, self.pos = pins, lid, travel_steps, start_pos
        self.steps_seen = 0
        self._sync()

    def _sync(self):
        # công tắc thường mở về GND: bấm = LOW
        self.pins.levels[self.lid.home_pin] = not (self.pos <= 0)
        self.pins.levels[self.lid.end_pin] = not (self.pos >= self.travel)

    def attach(self, group):
        write = group.write
        active = not self.lid.step_active_low

        def on_write(pin, high):
            write(pin, high)
            if pin == self.lid.pul_pin and high == active:
                toward_end = self.pins.levels[self.lid.dir_pin] == self.lid.open_dir_high
                self.pos += 1 if toward_end else -1
                self.steps_seen += 1
                self._sync()

        group.write = on_write


def make_lid(pins, **kw):
    params = dict(pul_pin=18, dir_pin=21, home_pin=4, end_pin=10, steps_per_sec=50_000,
                  start_steps_per_sec=50_000, ramp_steps=0, max_steps=500, pulse_us=0)
    params.update(kw)
    return LidController(pins, **params)


class LidControllerTest(unittest.TestCase):
    def setUp(self):
        self.pins = FakePins()
        self.lid = make_lid(self.pins)
        self.travel = FakeTravel(self.pins, self.lid, travel_steps=120, start_pos=60)
        self.lid.start()
        self.travel.attach(self.lid._group)

    def test_starts_unknown_between_limits(self):
        self.assertEqual(self.lid.state, LidState.UNKNOWN)
        self.assertTrue(self.pins.levels[18], "PUL nghỉ ở HIGH khi nối kiểu 3,3 V")

    def test_home_then_open_then_close(self):
        result = self.lid.home()
        self.assertEqual(result, {"result": "OK", "state": "CLOSED", "steps": 60})
        self.assertTrue(self.lid.at_home())
        result = self.lid.open()
        self.assertEqual(result["state"], "OPEN")
        self.assertEqual(result["steps"], 120)
        result = self.lid.close()
        self.assertEqual(result["state"], "CLOSED")
        self.assertEqual(self.travel.pos, 0)

    def test_already_at_limit_moves_nothing(self):
        self.lid.home()
        seen = self.travel.steps_seen
        self.assertEqual(self.lid.close()["steps"], 0)
        self.assertEqual(self.travel.steps_seen, seen)

    def test_fault_when_limit_never_reached(self):
        pins = FakePins()
        lid = make_lid(pins, max_steps=50).start()   # công tắc không bao giờ bấm
        result = lid.open()
        self.assertEqual(result["result"], "FAIL")
        self.assertEqual(result["error"], "LIMIT_NOT_REACHED")
        self.assertEqual(lid.state, LidState.FAULT)
        self.assertEqual(result["steps"], 50)
        self.assertTrue(pins.levels[18], "PUL phải về mức nghỉ sau khi dừng")

    def test_stop_interrupts_move(self):
        pins = FakePins()
        lid = make_lid(pins, steps_per_sec=2000, start_steps_per_sec=2000, max_steps=100_000).start()
        results = []
        t = threading.Thread(target=lambda: results.append(lid.open()))
        t.start()
        time.sleep(0.05)
        self.assertEqual(lid.open()["error"], "LID_BUSY")
        lid.stop()
        t.join(timeout=2)
        self.assertEqual(results[0]["result"], "STOPPED")
        self.assertEqual(lid.state, LidState.STOPPED)

    def test_ramp_starts_slow(self):
        lid = make_lid(FakePins(), steps_per_sec=800, start_steps_per_sec=200, ramp_steps=100)
        self.assertAlmostEqual(lid._interval(0), 1 / 200)
        self.assertAlmostEqual(lid._interval(100), 1 / 800)
        self.assertGreater(lid._interval(10), lid._interval(90))

    def test_rejects_duplicate_pins(self):
        with self.assertRaises(ValueError):
            make_lid(FakePins(), home_pin=18)


if __name__ == "__main__":
    unittest.main()
