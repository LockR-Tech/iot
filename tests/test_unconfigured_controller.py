"""Pi chưa gán tủ (F3-G12): vẫn báo discovery định kỳ, và bộ giả lập khai báo đúng số ô.

    uv run python -m unittest tests.test_unconfigured_controller -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infracstructure.serial_manager import MAX_SLOTS, SerialManager
from services.discovery_service import UnconfiguredAnnouncer


class UnconfiguredAnnouncerTest(unittest.TestCase):
    def test_announces_every_interval_while_unconfigured(self):
        announcer = UnconfiguredAnnouncer(interval_s=3)
        fired = [announcer.tick(configured=False, connected=True) for _ in range(7)]
        self.assertEqual(fired, [False, False, True, False, False, True, False])

    def test_never_announces_once_configured(self):
        announcer = UnconfiguredAnnouncer(interval_s=2)
        self.assertFalse(any(announcer.tick(configured=True, connected=True) for _ in range(10)))

    def test_waits_for_broker_then_announces(self):
        announcer = UnconfiguredAnnouncer(interval_s=2)
        self.assertFalse(announcer.tick(configured=False, connected=False))
        self.assertFalse(announcer.tick(configured=False, connected=False))
        self.assertTrue(announcer.tick(configured=False, connected=True))

    def test_interval_fits_backend_online_window(self):
        import main
        self.assertLess(main.UNCONFIGURED_DISCOVERY_INTERVAL_S, 150)


class SimulatedSlotCountTest(unittest.TestCase):
    def test_default_matches_firmware(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIM_SLOTS", None)
            serial = SerialManager(port="AUTO", baud_rate=9600, simulation=True)
        self.assertEqual(serial.num_slots, MAX_SLOTS)
        self.assertEqual(serial.scan_slaves(), [{"slaveId": 1, "availableSlots": MAX_SLOTS}])

    def test_sim_slots_overrides_count(self):
        with mock.patch.dict(os.environ, {"SIM_SLOTS": "8"}):
            serial = SerialManager(port="AUTO", baud_rate=9600, simulation=True)
        self.assertEqual(serial.num_slots, 8)
        self.assertEqual(serial.scan_slaves(), [{"slaveId": 1, "availableSlots": 8}])

    def test_invalid_sim_slots_falls_back(self):
        for bad in ("abc", "0", "-3"):
            with mock.patch.dict(os.environ, {"SIM_SLOTS": bad}):
                serial = SerialManager(port="AUTO", baud_rate=9600, simulation=True)
            self.assertEqual(serial.num_slots, MAX_SLOTS, bad)


if __name__ == "__main__":
    unittest.main()
