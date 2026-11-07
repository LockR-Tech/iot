"""Mở ô qua Bluetooth (F3-G11): giải/kiểm mã backend ký và xử lý lệnh mở trên Pi."""
import base64
import json
import os
import struct
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from services import ble_open_token
from services.ble_gatt_service import BleGattService
from services.ble_open_token import PublicKeyStore, ReplayGuard, TokenError

LOCKER = 7


def make_token(key, locker=LOCKER, box=3, user=1234, exp=None, nonce=b"\x01" * 8, version=1):
    exp = int(time.time()) + 120 if exp is None else exp
    payload = struct.pack(">BIHQI8s", version, locker, box, user, exp, nonce)
    return payload + key.sign(payload)


def raw_public(key) -> str:
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


class TestBleOpenToken(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key()

    def verify(self, data, locker=LOCKER, replay=None, public=None):
        return ble_open_token.verify(data, public or self.public, locker, replay or ReplayGuard())

    def assertRejected(self, code, *args, **kwargs):
        with self.assertRaises(TokenError) as ctx:
            self.verify(*args, **kwargs)
        self.assertEqual(code, ctx.exception.code)

    def test_valid_token_is_decoded(self):
        token = self.verify(make_token(self.key, box=5, user=99, nonce=bytes(range(8))))
        self.assertEqual((LOCKER, 5, 99), (token.locker_id, token.box_number, token.user_id))
        self.assertEqual("0001020304050607", token.nonce)

    def test_wrong_length_and_version(self):
        self.assertRejected("BAD_FORMAT", make_token(self.key)[:-1])
        self.assertRejected("BAD_VERSION", make_token(self.key, version=2))

    def test_signature_from_other_key_or_tampered_payload(self):
        self.assertRejected("BAD_SIGNATURE", make_token(Ed25519PrivateKey.generate()))
        data = bytearray(make_token(self.key, box=3))
        data[6] = 4  # đổi sang ô 4
        self.assertRejected("BAD_SIGNATURE", bytes(data))

    def test_token_for_other_locker(self):
        self.assertRejected("WRONG_LOCKER", make_token(self.key, locker=8))

    def test_unassigned_pi_and_missing_key(self):
        self.assertRejected("NOT_CONFIGURED", make_token(self.key), locker=None)
        with self.assertRaises(TokenError) as ctx:
            ble_open_token.verify(make_token(self.key), None, LOCKER, ReplayGuard())
        self.assertEqual("NO_KEY", ctx.exception.code)

    def test_expired_and_far_future(self):
        self.assertRejected("EXPIRED", make_token(self.key, exp=int(time.time()) - 60))
        self.verify(make_token(self.key, exp=int(time.time()) - 10))  # trong biên lệch đồng hồ
        self.assertRejected("BAD_EXPIRY", make_token(self.key, exp=int(time.time()) + 3600))

    def test_token_works_only_once(self):
        replay = ReplayGuard()
        data = make_token(self.key)
        self.verify(data, replay=replay)
        self.assertRejected("REPLAYED", data, replay=replay)
        self.verify(make_token(self.key, nonce=b"\x02" * 8), replay=replay)

    def test_token_signed_by_backend_java(self):
        # Sinh bởi iot-service BleTokenSigner (Java) — khớp định dạng giữa hai bên.
        public = PublicKeyStore("", "", env_key=JAVA_PUBLIC_KEY).key
        data = base64.urlsafe_b64decode(JAVA_TOKEN + "=" * (-len(JAVA_TOKEN) % 4))
        token = ble_open_token.verify(data, public, LOCKER, ReplayGuard(), now=JAVA_TOKEN_EXP - 60)
        self.assertEqual((LOCKER, 3, 1234), (token.locker_id, token.box_number, token.user_id))


class TestPublicKeyStore(unittest.TestCase):
    def test_fetches_saves_and_reloads_offline(self):
        key = Ed25519PrivateKey.generate()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ble_public_key.json")
            fetch = MagicMock(return_value={"data": {"publicKey": raw_public(key)}})
            store = PublicKeyStore(path, "https://api.example/", fetch=fetch)
            self.assertIsNone(store.key)
            self.assertTrue(store.refresh())
            fetch.assert_called_once_with("https://api.example/api/iot/ble/public-key")
            offline = PublicKeyStore(path, "https://api.example", fetch=MagicMock(side_effect=OSError("down")))
            self.assertIsNotNone(offline.key)
            self.assertTrue(offline.refresh())  # giữ khoá cũ khi mất mạng
            ble_open_token.verify(make_token(key), offline.key, LOCKER, ReplayGuard())

    def test_env_key_wins_and_is_not_fetched(self):
        key = Ed25519PrivateKey.generate()
        fetch = MagicMock()
        store = PublicKeyStore("/nonexistent/k.json", "https://x", env_key=raw_public(key), fetch=fetch)
        self.assertTrue(store.refresh())
        fetch.assert_not_called()


class TestBleGattHandle(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        store = PublicKeyStore("", "", env_key=raw_public(self.key))
        self.state = MagicMock()
        self.state.primary_locker_id = str(LOCKER)
        self.hardware = MagicMock()
        self.hardware.ble_open_slot.return_value = {"result": "OK", "slot": 2}
        self.log = MagicMock()
        self.service = BleGattService(self.state, self.hardware, store, name_fn=lambda: "LOCKR_CAB-TU01",
                                      control_log=self.log)

    def test_valid_token_pulses_box_relay_and_logs_user(self):
        reply = self.service.handle_token(make_token(self.key, box=3, user=1234, nonce=b"\xab" * 8))
        self.assertEqual({"nonce": "ab" * 8, "result": "OK", "box": 3}, reply)
        self.hardware.ble_open_slot.assert_called_once_with(2)
        action, status = self.log.record.call_args.args
        self.assertEqual(("BOX_OPEN_BLE", "OK"), (action, status))
        kwargs = self.log.record.call_args.kwargs
        self.assertIn("#1234", kwargs["message"])
        # Backend khớp nonce với mã đã cấp cho đơn ⇒ chuyển trạng thái đơn (F3-G11 mở theo đơn).
        self.assertEqual(("ab" * 8, 1234, 3, 2, None),
                         (kwargs["nonce"], kwargs["user_id"], kwargs["box_number"], kwargs["slot_index"],
                          kwargs["error_code"]))

    def test_hardware_failure_is_reported(self):
        self.hardware.ble_open_slot.return_value = {"result": "FAIL", "error": "INVALID_SLOT"}
        reply = self.service.handle_token(make_token(self.key, box=8))
        self.assertEqual("FAIL", reply["result"])
        self.assertEqual("INVALID_SLOT", reply["error"])
        self.assertEqual("FAILED", self.log.record.call_args.args[1])
        self.assertEqual("INVALID_SLOT", self.log.record.call_args.kwargs["error_code"])

    def test_rejected_token_does_not_touch_relay(self):
        reply = self.service.handle_token(make_token(Ed25519PrivateKey.generate()))
        self.assertEqual("BAD_SIGNATURE", reply["error"])
        self.assertEqual("01" * 8, reply["nonce"])
        self.hardware.ble_open_slot.assert_not_called()
        self.assertEqual("BLE_REJECTED", self.log.record.call_args.args[0])
        kwargs = self.log.record.call_args.kwargs
        self.assertEqual(("01" * 8, 1234, 3, "BAD_SIGNATURE", None),
                         (kwargs["nonce"], kwargs["user_id"], kwargs["box_number"], kwargs["error_code"],
                          kwargs["slot_index"]))

    def test_door_still_closed_is_jammed_when_sensor_required(self):
        self.hardware.ble_open_slot.return_value = {"result": "OK", "slot": 2, "door": True}
        reply = self.door_service(True).handle_token(make_token(self.key, box=3, nonce=b"\xcd" * 8))
        self.assertEqual({"nonce": "cd" * 8, "result": "FAIL", "box": 3, "error": "JAMMED"}, reply)
        action, status = self.log.record.call_args.args
        self.assertEqual(("BOX_OPEN_BLE", "FAILED"), (action, status))
        kwargs = self.log.record.call_args.kwargs
        self.assertEqual(("JAMMED", "cd" * 8, 1234, 3),
                         (kwargs["error_code"], kwargs["nonce"], kwargs["user_id"], kwargs["box_number"]))

    def test_door_check_is_off_without_sensor_requirement_or_reading(self):
        cases = [(False, {"result": "OK", "door": True}),     # REQUIRE_DOOR_SENSOR tắt
                 (True, {"result": "OK", "door": False}),     # cửa đã mở
                 (True, {"result": "OK"})]                    # phần cứng không báo cảm biến
        for i, (required, result) in enumerate(cases):
            with self.subTest(required=required, result=result):
                self.hardware.ble_open_slot.return_value = result
                reply = self.door_service(required).handle_token(make_token(self.key, nonce=bytes([i]) * 8))
                self.assertEqual("OK", reply["result"])
                self.assertNotIn("error", reply)
                self.assertEqual("OK", self.log.record.call_args.args[1])

    def test_default_service_has_no_door_check(self):
        self.hardware.ble_open_slot.return_value = {"result": "OK", "door": True}
        self.assertEqual("OK", self.service.handle_token(make_token(self.key))["result"])

    def door_service(self, required):
        return BleGattService(self.state, self.hardware, self.service.key_store, name_fn=lambda: "x",
                              control_log=self.log, require_door_sensor=lambda: required)

    def test_garbage_is_rejected_without_log(self):
        reply = self.service.handle_token(b"hello")
        self.assertEqual({"nonce": None, "result": "FAIL", "error": "BAD_FORMAT"}, reply)
        self.log.record.assert_not_called()

    def test_rs485_backend_uses_open_slot_with_cabinet_slave(self):
        hardware = MagicMock(spec=["open_slot"])
        hardware.open_slot.return_value = {"result": "OK"}
        self.state.get_cabinet_by_id.return_value = {"id": "7", "slaveId": 2}
        service = BleGattService(self.state, hardware, self.service.key_store, name_fn=lambda: "x")
        self.assertEqual("OK", service.handle_token(make_token(self.key, box=1))["result"])
        hardware.open_slot.assert_called_once_with(0, slave_id=2)

    def test_reply_fits_one_notification(self):
        reply = self.service.handle_token(make_token(self.key, box=3))
        self.assertLess(len(json.dumps(reply, separators=(",", ":"))), 180)


class TestSimulatedCabinet(unittest.TestCase):
    """simulate_ble_cabinet.py — tủ giả để thử bằng điện thoại thật: cùng luật kiểm mã như Pi."""

    def setUp(self):
        import simulate_ble_cabinet as sim
        self.sim = sim
        self.key = Ed25519PrivateKey.generate()
        self.store = PublicKeyStore("", "", env_key=raw_public(self.key))

    def service(self, locker_id, boxes=24, fail=(), jam=()):
        return self.sim.SimBleGattService(
            self.sim._SimCabinetState(locker_id), self.sim._SimHardware(boxes, fail, jam), self.store,
            name_fn=lambda: "LOCKR_SIM", any_locker=locker_id is None, require_door_sensor=lambda: True)

    def test_any_locker_accepts_signed_token_of_each_locker(self):
        service = self.service(None)
        self.assertEqual("OK", service.handle_token(make_token(self.key, locker=3, nonce=b"\x01" * 8))["result"])
        self.assertEqual("OK", service.handle_token(make_token(self.key, locker=9, nonce=b"\x02" * 8))["result"])

    def test_still_checks_signature_and_replay(self):
        service = self.service(None)
        self.assertEqual("BAD_SIGNATURE",
                         service.handle_token(make_token(Ed25519PrivateKey.generate()))["error"])
        token = make_token(self.key)
        self.assertEqual("OK", service.handle_token(token)["result"])
        self.assertEqual("REPLAYED", service.handle_token(token)["error"])

    def test_fixed_locker_rejects_other_locker(self):
        self.assertEqual("WRONG_LOCKER", self.service(LOCKER).handle_token(make_token(self.key, locker=8))["error"])

    def test_box_without_relay_and_forced_failure(self):
        service = self.service(None, boxes=4, fail=[2])
        self.assertEqual("INVALID_SLOT", service.handle_token(make_token(self.key, box=5, nonce=b"\x03" * 8))["error"])
        self.assertEqual("HARDWARE_ERROR", service.handle_token(make_token(self.key, box=2, nonce=b"\x04" * 8))["error"])

    def test_jammed_box(self):
        service = self.service(None, jam=[3])
        self.assertEqual("JAMMED", service.handle_token(make_token(self.key, box=3, nonce=b"\x05" * 8))["error"])
        self.assertEqual("OK", service.handle_token(make_token(self.key, box=4, nonce=b"\x06" * 8))["result"])


# Sinh 2026-10-24 bằng BleTokenSigner thật (tủ 7, ô 3, tài khoản 1234).
JAVA_PUBLIC_KEY = "sflPhGIWa5qerzw2X7ayRZ9YD43aautnIXEL9Qjlzbw="
JAVA_TOKEN = ("AQAAAAcAAwAAAAAAAATSat8CQLA3RBUIWi0ceu5RKezYJ747sHjMbOebCGlW_9PYxELVYY-onvu4C5etmS0U"
              "FBpxeDr5RmcWg8oGA0T4UaMicwzZTJFJR2deDA")
JAVA_TOKEN_EXP = 1793000000


if __name__ == "__main__":
    unittest.main()
