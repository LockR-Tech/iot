"""Nhật ký điều khiển tủ: ghi file trước, gửi `cabinet/{id}/event/control` khi có mạng, không mất khi
mất mạng hay khởi động lại; móc vào mọi lượt chạy nắp và mọi lần kích relay.

    uv run python -m unittest tests.test_control_log -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_ble_open_event_carries_nonce_user_and_box(self):
        """BOX_OPEN_BLE / BLE_REJECTED kèm nonce, userId, boxNumber (+ errorCode khi lỗi) — backend khớp
        nonce với mã cấp cho đơn rồi chuyển trạng thái đơn (F3-G11). Relay thật qua GPIO giả, cửa kẹt."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from services.ble_gatt_service import BleGattService
        from services.ble_open_token import PublicKeyStore
        from tests.test_ble_open import make_token, raw_public

        key = Ed25519PrivateKey.generate()
        pins = FakePins()
        locker = GpioLockerManager(pins, RELAYS, DOORS, unlock_ms=5, settle_ms=5,
                                   close_wait_ms=1, poll_ms=10_000, debounce_ms=50).start()
        locker.on_pulse = self.log.box_pulsed
        service = BleGattService(mock.MagicMock(primary_locker_id="7"), locker,
                                 PublicKeyStore("", "", env_key=raw_public(key)), name_fn=lambda: "x",
                                 control_log=self.log, require_door_sensor=lambda: True)
        try:
            # Ô 3 (slot 2): cảm biến kéo lên = cửa mở ⇒ OK.
            self.assertEqual("OK", service.handle_token(make_token(key, box=3, user=55, nonce=b"\xab" * 8))["result"])
            # Ô 4 (slot 3): cảm biến vẫn báo cửa đóng sau khi kích relay ⇒ JAMMED.
            pins.levels[DOORS[3]] = False
            reply = service.handle_token(make_token(key, box=4, user=55, nonce=b"\xcd" * 8))
            self.assertEqual({"nonce": "cd" * 8, "result": "FAIL", "box": 4, "error": "JAMMED"}, reply)
            service.handle_token(make_token(Ed25519PrivateKey.generate(), box=5, user=56, nonce=b"\xef" * 8))
        finally:
            locker.close()

        self.mqtt.is_connected = True
        self.assertEqual(self.log.flush(), 3)               # relay BLE không ghi thêm dòng BOX_OPEN
        ok, jam, rejected = self.mqtt.on(TOPIC)
        self.assertEqual(("BOX_OPEN_BLE", "OK", "ab" * 8, 55, 3, 2, 40),
                         (ok["action"], ok["status"], ok["nonce"], ok["userId"], ok["boxNumber"],
                          ok["slotIndex"], ok["boxId"]))
        self.assertNotIn("errorCode", ok)
        self.assertEqual(("BOX_OPEN_BLE", "FAILED", "JAMMED", "cd" * 8, 55, 4),
                         (jam["action"], jam["status"], jam["errorCode"], jam["nonce"], jam["userId"],
                          jam["boxNumber"]))
        self.assertEqual(("BLE_REJECTED", "FAILED", "BAD_SIGNATURE", "ef" * 8, 56, 5),
                         (rejected["action"], rejected["status"], rejected["errorCode"], rejected["nonce"],
                          rejected["userId"], rejected["boxNumber"]))
        self.assertNotIn("slotIndex", rejected)

    def test_events_without_ble_fields_keep_old_shape(self):
        self.log.record("BOX_OPEN", "OK", slot_index=3, message="m")
        (event,) = self.log.pending()
        self.assertEqual({"eventId", "lockerId", "action", "status", "slotIndex", "boxId", "message", "occurredAt"},
                         set(event))


if __name__ == "__main__":
    unittest.main()
