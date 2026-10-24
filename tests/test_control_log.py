"""Nhật ký điều khiển tủ: ghi file trước, gửi `cabinet/{id}/event/control` khi có mạng, không mất khi
mất mạng hay khởi động lại; móc vào mọi lượt chạy nắp và mọi lần kích relay.

    uv run python -m unittest tests.test_control_log -v
"""
import os
import sys
import tempfile
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import FakePins
from services.control_log import ControlLog
from tests.test_lid_commands import make_lid
from tests.test_mqtt_contract import DOORS, RELAYS, FakeMqtt

TOPIC = "cabinet/7/event/control"


class ControlLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "outbox.jsonl")
        self.mqtt = FakeMqtt()
        self.mqtt.is_connected = False
        self.log = ControlLog(self.mqtt, self.path, locker_id=lambda: "7",
                              box_id_for_slot=lambda cab, slot: 38 + slot)

    def tearDown(self):
        self.log.stop()
        self.tmp.cleanup()

    def test_offline_events_are_kept_then_sent_in_order(self):
        self.log.record("BOX_OPEN", "OK", slot_index=3)
        self.log.record("LID_CLOSE", "STOPPED", axis=1, revolutions=12.5)
        self.assertEqual(self.log.flush(), 0)              # mất mạng: không gửi, không xoá
        self.assertEqual(len(self.log.pending()), 2)

        # Pi khởi động lại: đối tượng mới đọc lại đúng file
        self.log = ControlLog(self.mqtt, self.path, locker_id=lambda: "7")
        self.mqtt.is_connected = True
        self.assertEqual(self.log.flush(), 2)
        sent = self.mqtt.on(TOPIC)
        self.assertEqual([e["action"] for e in sent], ["BOX_OPEN", "LID_CLOSE"])
        self.assertEqual(sent[0]["slotIndex"], 3)
        self.assertEqual(sent[0]["boxId"], 41)
        self.assertEqual(sent[1]["revolutions"], 12.5)
        self.assertTrue(all(e["eventId"] and e["occurredAt"] for e in sent))
        self.assertNotIn("lockerId", sent[0])
        self.assertEqual(self.log.pending(), [])

    def test_unconfirmed_publish_keeps_event_for_retry(self):
        self.mqtt.is_connected = True
        self.mqtt.confirm = False
        self.log.record("BOX_OPEN", "OK", slot_index=0)
        self.assertEqual(self.log.flush(), 0)
        self.assertEqual(len(self.log.pending()), 1)
        self.mqtt.confirm = True
        self.assertEqual(self.log.flush(), 1)
        first, retry = self.mqtt.on(TOPIC)
        self.assertEqual(first["eventId"], retry["eventId"])   # gửi lại cùng eventId ⇒ backend bỏ trùng

    def test_unassigned_pi_records_nothing(self):
        log = ControlLog(self.mqtt, self.path, locker_id=lambda: None)
        self.assertIsNone(log.record("BOX_OPEN", "OK", slot_index=0))
        self.assertFalse(os.path.exists(self.path))

    def test_outbox_is_capped(self):
        log = ControlLog(self.mqtt, self.path, locker_id=lambda: "7", max_pending=3)
        for i in range(5):
            log.record("BOX_OPEN", "OK", slot_index=i)
        self.assertEqual([e["slotIndex"] for e in log.pending()], [2, 3, 4])

    def test_corrupt_line_is_skipped(self):
        self.log.record("BOX_OPEN", "OK", slot_index=1)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"eventId": "half-writ')          # mất điện giữa lúc ghi
        self.assertEqual(len(self.log.pending()), 1)

    def test_lid_run_is_logged_with_revolutions_and_direction(self):
        lid = make_lid(FakePins())
        lid.on_finish = self.log.lid_finished
        try:
            lid.jog(revolutions=2, toward_end=False)
        finally:
            lid.shutdown()
        (event,) = self.log.pending()
        self.assertEqual(event["action"], "LID_JOG")
        self.assertEqual(event["status"], "OK")
        self.assertEqual(event["axis"], 1)
        self.assertEqual(event["revolutions"], 2.0)
        self.assertIn("về phía đóng", event["message"])

    def test_lid_run_without_limit_switch_is_failed(self):
        lid = make_lid(FakePins())                          # max 400 bước, công tắc không bao giờ chạm
        lid.on_finish = self.log.lid_finished
        try:
            lid.close()
        finally:
            lid.shutdown()
        (event,) = self.log.pending()
        self.assertEqual(event["action"], "LID_CLOSE")
        self.assertEqual(event["status"], "FAILED")
        self.assertIn("không chạm công tắc", event["message"])

    def test_relay_pulse_is_logged(self):
        pins = FakePins()
        locker = GpioLockerManager(pins, RELAYS, DOORS, unlock_ms=5, settle_ms=5,
                                   close_wait_ms=1, poll_ms=10_000, debounce_ms=50).start()
        locker.on_pulse = self.log.box_pulsed
        try:
            locker.pulse_slot(4, 120)
        finally:
            locker.close()
        (event,) = self.log.pending()
        self.assertEqual(event["action"], "BOX_OPEN")
        self.assertEqual(event["slotIndex"], 4)
        self.assertEqual(event["boxId"], 42)
        self.assertIn("120 ms", event["message"])
        self.assertIn("bảng kỹ thuật", event["message"])


if __name__ == "__main__":
    unittest.main()
