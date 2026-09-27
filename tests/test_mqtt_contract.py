"""Contract test phía Pi cho hợp đồng MQTT backend ↔ tủ (ADR-0008).

Payload mẫu dưới đây phải giống hệt payload iot-service sinh ra — bên backend có
test tương ứng (`LockerMqttServiceTest`, `GatewayProvisioningServiceTest`). Đổi một
bên thì đổi cả bên kia và docs/01-overview/mqtt-contract.md.

    uv run python -m unittest tests.test_mqtt_contract -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.settings import settings
from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import FakePins
from infracstructure.cabinet_state import CabinetState
from infracstructure.mqtt_client import MQTTClientWrapper
from services.discovery_service import DiscoveryService
from services.heartbeat_service import HeartbeatService
from services.locker_service import LockerService

MAC = "2C:CF:67:DB:C5:C3"
RELAYS = [17, 27, 22, 23, 24, 25, 16]
DOORS = [5, 6, 12, 13, 19, 26, 20]

# ─── Payload do iot-service gửi (mqtt-contract.md § 2.1, § 3) ───
OPEN_CMD = {"commandId": "c-1", "boxId": 12, "box_id": 12, "slotIndex": 3, "action": "OPEN", "timeout": 15}
SYNC_CMD = {"boxId": 12, "state": "OCCUPIED", "orderId": 99}
SETUP_CMD = {
    "action": "SETUP_LOCKERS", "commandId": "s-1", "macAddress": MAC,
    "lockerId": 5, "cabinetId": "5", "cabinetCode": "CAB-TU01", "slaveId": 1,
    "totalRows": 3, "totalColumns": 1, "testDoors": True, "testTimeout": 10,
    "lockerLayout": [
        {"boxId": 40, "slotIndex": 0, "row": 1, "column": 0, "label": "1"},
        {"boxId": 41, "slotIndex": 1, "row": 2, "column": 0, "label": "2"},
        {"boxId": 42, "slotIndex": 2, "row": 3, "column": 0, "label": "3"},
    ],
}
RESULT_KEYS = {"commandId", "boxId", "slotIndex", "status", "hwState", "errorCode", "errorMessage", "timestamp"}


class FakeMqtt:
    """Thay MQTTClientWrapper: ghi lại publish và tập topic đang subscribe."""

    def __init__(self):
        self.published = []
        self.subscriptions = set()

    def publish(self, topic, payload, qos=1):
        self.published.append((topic, json.loads(payload)))

    def set_subscriptions(self, topics, qos=1):
        self.subscriptions = set(topics)

    def on(self, topic):
        return [p for t, p in self.published if t == topic]

    def topics(self):
        return [t for t, _ in self.published]


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class PiContractTest(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(settings, "MAC_ADDRESS", MAC),
            mock.patch.object(settings, "REQUIRE_DOOR_SENSOR", True),
            mock.patch.object(settings, "HARDWARE_BACKEND", "gpio"),
        ]
        for p in self.patches:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "cabinet_state.json")

        self.pins = FakePins()
        for pin in DOORS:
            self.pins.levels[pin] = False                   # LOW = cửa đóng
        self.locker = GpioLockerManager(self.pins, RELAYS, DOORS, unlock_ms=5, settle_ms=5,
                                        close_wait_ms=1, poll_ms=10_000, debounce_ms=50).start()

        self.mqtt = FakeMqtt()
        self.state = CabinetState(state_path=self.state_path, fallback_locker_id="1")
        self.heartbeat = HeartbeatService(self.mqtt, self.state, interval=3600, hardware=self.locker)
        self.discovery = DiscoveryService(self.mqtt, self.locker, cabinet_state=self.state)
        self.service = LockerService(self.mqtt, None, serial_manager=self.locker, cabinet_state=self.state,
                                     heartbeat_service=self.heartbeat, discovery_service=self.discovery)
        self.locker.on_door_event = self.service.handle_door_event
        self.service.refresh_subscriptions()

    def tearDown(self):
        self.heartbeat.stop()
        self.service.shutdown()
        self.locker.close()
        self.tmp.cleanup()
        for p in self.patches:
            p.stop()

    # ─── helpers ───

    def send(self, topic, payload):
        self.service.handle_incoming_message(topic, json.dumps(payload))
        self.service._executor.submit(lambda: None).result(timeout=5)   # chờ lệnh phần cứng chạy xong

    def relay_pulses(self):
        return [pin for pin, high in self.pins.history if high]

    def run_setup(self, payload):
        self.send(f"iot/{MAC}/command/setup", payload)
        self.assertTrue(wait_until(lambda: self.mqtt.on(f"iot/{MAC}/setup/result")), "không có setup/result")
        self.assertTrue(wait_until(lambda: not self.service.setup_handler.is_running))
        return self.mqtt.on(f"iot/{MAC}/setup/result")[-1]

    # ─── lệnh mở ───

    def test_subscribes_command_topics_of_fallback_locker(self):
        self.assertEqual(self.mqtt.subscriptions, {"cabinet/1/command/+"})

    def test_open_from_backend_payload_pulses_slot_and_replies(self):
        self.pins.levels[DOORS[3]] = True                   # lò xo bật cửa ra
        self.send("cabinet/1/command/open", OPEN_CMD)

        self.assertEqual(self.relay_pulses(), [RELAYS[3]])
        (result,) = self.mqtt.on("cabinet/1/command/open/result")
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual(result["commandId"], "c-1")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual((result["boxId"], result["slotIndex"], result["hwState"]), (12, 3, "OPEN"))
        self.assertIsNone(result["errorCode"])

    def test_door_stays_closed_is_jammed_unless_sensor_not_required(self):
        self.send("cabinet/1/command/open", OPEN_CMD)
        result = self.mqtt.on("cabinet/1/command/open/result")[-1]
        self.assertEqual((result["status"], result["errorCode"], result["hwState"]), ("FAILED", "JAMMED", "CLOSED"))

        with mock.patch.object(settings, "REQUIRE_DOOR_SENSOR", False):
            self.send("cabinet/1/command/open", {**OPEN_CMD, "commandId": "c-2"})
        result = self.mqtt.on("cabinet/1/command/open/result")[-1]
        self.assertEqual((result["commandId"], result["status"]), ("c-2", "SUCCESS"))

    def test_open_without_slot_index_uses_learned_box(self):
        self.send("cabinet/1/command/open", OPEN_CMD)       # lệnh trước cho biết box 12 = slot 3
        self.pins.history.clear()
        self.pins.levels[DOORS[3]] = True
        self.send("cabinet/1/command/open", {"commandId": "c-3", "boxId": 12, "action": "OPEN"})
        self.assertEqual(self.relay_pulses(), [RELAYS[3]])
        self.assertEqual(self.mqtt.on("cabinet/1/command/open/result")[-1]["status"], "SUCCESS")

    def test_sync_needs_no_reply(self):
        self.send("cabinet/1/command/sync", SYNC_CMD)
        self.assertEqual(self.mqtt.published, [])
        self.assertEqual(self.relay_pulses(), [])

    def test_open_unknown_box_fails_fast_without_touching_relays(self):
        self.send("cabinet/1/command/open", {"commandId": "c-9", "boxId": 999, "action": "OPEN"})
        result = self.mqtt.on("cabinet/1/command/open/result")[-1]
        self.assertEqual((result["status"], result["errorCode"]), ("FAILED", "UNKNOWN_SLOT"))
        self.assertEqual(self.relay_pulses(), [])

    def test_slot_beyond_hardware_is_reported(self):
        self.send("cabinet/1/command/open", {**OPEN_CMD, "slotIndex": 7})
        result = self.mqtt.on("cabinet/1/command/open/result")[-1]
        self.assertEqual((result["status"], result["errorCode"]), ("FAILED", "INVALID_SLOT"))

    def test_command_for_another_locker_is_ignored(self):
        self.send("cabinet/2/command/open", OPEN_CMD)
        self.assertEqual(self.mqtt.published, [])
        self.assertEqual(self.relay_pulses(), [])

    # ─── sự kiện cửa ───

    def test_door_sensor_change_publishes_status_with_box_id(self):
        self.send("cabinet/1/command/open", OPEN_CMD)       # Pi nhớ box 12 = slot 3
        self.pins.levels[DOORS[3]] = True                   # cửa ngăn 3 mở
        self.locker.poll_once(now=100.0)
        self.locker.poll_once(now=100.2)                    # ổn định > debounce

        (status,) = self.mqtt.on("cabinet/1/locker/3/status")
        self.assertEqual(status["slotIndex"], 3)
        self.assertEqual(status["boxId"], 12)
        self.assertEqual((status["hwState"], status["doorOpen"]), ("OPEN", True))
        self.assertFalse([t for t in self.mqtt.topics() if "/command/" in t and not t.endswith("/result")],
                         "Pi không được tự gửi lệnh vào topic lệnh")

    # ─── cấp phát (admin gán Pi vào tủ) ───

    def test_setup_provisions_locker_and_moves_subscriptions(self):
        for pin in DOORS:
            self.pins.levels[pin] = True                    # mọi cửa bật ra khi thử
        result = self.run_setup(SETUP_CMD)

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual((result["cabinetId"], result["lockerId"]), ("5", 5))
        self.assertEqual([l["boxId"] for l in result["lockers"]], [40, 41, 42])
        self.assertEqual(result["summary"]["totalOk"], 3)
        self.assertEqual(len(self.mqtt.on(f"iot/{MAC}/setup/progress")), 3)
        self.assertEqual(self.relay_pulses(), RELAYS[:3])

        self.assertEqual([c["id"] for c in self.state.all_cabinets], ["5"])
        self.assertTrue(wait_until(lambda: self.mqtt.subscriptions == {"cabinet/5/command/+"}))
        self.assertEqual(self.state.slot_for_box("5", 41), 1)

        reloaded = CabinetState(state_path=self.state_path, fallback_locker_id="1")
        self.assertEqual([c["id"] for c in reloaded.all_cabinets], ["5"])
        self.assertEqual(reloaded.box_id_for_slot("5", 2), 42)

    def test_setup_without_door_test_does_not_touch_relays(self):
        result = self.run_setup({**SETUP_CMD, "testDoors": False})
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.relay_pulses(), [])
        self.assertEqual(self.mqtt.on(f"iot/{MAC}/setup/progress"), [])

        self.pins.levels[DOORS[2]] = True
        self.send("cabinet/5/command/open", {"commandId": "c-5", "boxId": 42, "action": "OPEN"})
        self.assertEqual(self.relay_pulses(), [RELAYS[2]])

    def test_setup_rejects_layout_beyond_hardware(self):
        bad = {**SETUP_CMD, "lockerLayout": [{"boxId": 50, "slotIndex": 7, "row": 1, "column": 0}]}
        self.send(f"iot/{MAC}/command/setup", bad)
        (result,) = self.mqtt.on(f"iot/{MAC}/setup/result")
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("slotIndex 7", result["errorMessage"])
        self.assertEqual([c["id"] for c in self.state.all_cabinets], ["1"])

    def test_setup_for_another_mac_is_ignored(self):
        self.send(f"iot/{MAC}/command/setup", {**SETUP_CMD, "macAddress": "AA:BB:CC:DD:EE:FF"})
        time.sleep(0.05)
        self.assertEqual(self.mqtt.on(f"iot/{MAC}/setup/result"), [])

    def test_clear_setup_falls_back_to_env_locker(self):
        self.run_setup({**SETUP_CMD, "testDoors": False})
        self.send(f"iot/{MAC}/command/clear-setup", {"action": "CLEAR_SETUP", "commandId": "x-1"})
        self.assertTrue(wait_until(lambda: self.mqtt.subscriptions == {"cabinet/1/command/+"}))
        self.assertEqual([c["id"] for c in self.state.all_cabinets], ["1"])

    # ─── báo cáo định kỳ ───

    def test_heartbeat_payload(self):
        self.send("cabinet/1/command/open", OPEN_CMD)
        self.pins.levels[DOORS[3]] = True
        self.locker.poll_once(now=1.0)
        self.locker.poll_once(now=2.0)
        payload = json.loads(self.heartbeat.build_payload(self.state.all_cabinets[0]).to_json())

        self.assertEqual((payload["cabinetId"], payload["status"], payload["macAddress"]), ("1", "online", MAC))
        self.assertIn("firmwareVersion", payload)
        self.assertEqual(len(payload["lockers"]), 7)
        self.assertEqual(payload["lockers"][3], {"slotIndex": 3, "hwState": "OPEN", "boxId": 12})
        self.assertEqual(payload["lockers"][0], {"slotIndex": 0, "hwState": "CLOSED"})

    def test_discovery_payload(self):
        self.discovery.discover_and_report()
        (payload,) = self.mqtt.on(f"iot/{MAC}/discovery/result")
        self.assertEqual(payload["macAddress"], MAC)
        self.assertEqual(payload["hardware"], "gpio")
        self.assertEqual(payload["lockerId"], 1)
        self.assertEqual(payload["slaves"], [{"slaveId": 1, "availableSlots": 7}])


class MqttWrapperTest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(settings, "MAC_ADDRESS", MAC)
        self.patch.start()
        self.wrapper = MQTTClientWrapper()
        self.client = mock.MagicMock()
        self.wrapper.client = self.client

    def tearDown(self):
        self.patch.stop()

    def test_resubscribes_everything_after_reconnect(self):
        self.wrapper.set_subscriptions({"cabinet/1/command/+"})
        self.client.subscribe.assert_not_called()            # chưa kết nối: chỉ ghi nhớ

        self.wrapper._on_connect(self.client, None, None, 0, None)
        (topics,), _ = self.client.subscribe.call_args
        self.assertEqual(set(dict(topics)), {
            f"iot/{MAC}/command/setup", f"iot/{MAC}/command/clear-setup",
            f"iot/{MAC}/discovery/start", "cabinet/1/command/+",
        })

        self.wrapper.set_subscriptions({"cabinet/5/command/+"})
        self.client.unsubscribe.assert_called_with("cabinet/1/command/+")
        self.client.subscribe.assert_called_with("cabinet/5/command/+", qos=1)

    def test_refuses_to_send_password_without_tls(self):
        with mock.patch.object(settings, "MQTT_USE_TLS", False), \
                mock.patch.object(settings, "MQTT_PASSWORD", "secret"):
            self.wrapper.start()
        self.client.connect_async.assert_not_called()


if __name__ == "__main__":
    unittest.main()
