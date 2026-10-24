import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lockr_drone.signing import SIGNATURE_PROPERTY, device_key, sign, verify  # noqa: E402


class VerifyTest(unittest.TestCase):
    MASTER = "test-master-secret"
    DRONE = "DRONE-A1"
    TOPIC = "lockr/drone/delivery/commands"
    PAYLOAD = '{"eventType":"LID_OPENED","droneId":"DRONE-A1"}'

    def test_accepts_only_the_signature_of_this_drones_key(self):
        key = device_key(self.MASTER, self.DRONE)
        good = sign(key, self.TOPIC, self.PAYLOAD)
        self.assertTrue(verify(key, self.TOPIC, self.PAYLOAD, good))
        self.assertTrue(verify(key, self.TOPIC, self.PAYLOAD, good.upper()))
        other = sign(device_key(self.MASTER, "DRONE-OTHER"), self.TOPIC, self.PAYLOAD)
        self.assertFalse(verify(key, self.TOPIC, self.PAYLOAD, other))
        self.assertFalse(verify(key, self.TOPIC, self.PAYLOAD + " ", good))
        self.assertFalse(verify(key, self.TOPIC, self.PAYLOAD, None))
        self.assertFalse(verify("", self.TOPIC, self.PAYLOAD, good))


class CommandSignatureTest(unittest.TestCase):
    """Pi chỉ làm theo lệnh giao hàng backend ký bằng khoá của chính drone đó."""

    def setUp(self):
        try:
            from lockr_drone.mqtt_publisher import MqttPublisher
        except ImportError as exc:  # paho chưa cài trên máy chạy test
            self.skipTest(f"paho-mqtt chưa cài: {exc}")
        self.key = device_key("test-master-secret", "DRONE-A1")
        self.publisher = object.__new__(MqttPublisher)
        self.publisher.settings = SimpleNamespace(device_key=self.key)

    def props(self, signature):
        return SimpleNamespace(UserProperty=[(SIGNATURE_PROPERTY, signature)] if signature else [])

    def test_signed_command_is_accepted(self):
        topic, body = "lockr/drone/delivery/commands", '{"eventType":"LID_OPENED"}'
        self.assertTrue(self.publisher._command_is_signed(topic, body, self.props(sign(self.key, topic, body))))

    def test_unsigned_forged_or_keyless_commands_are_rejected(self):
        topic, body = "lockr/drone/delivery/commands", '{"eventType":"LID_OPENED"}'
        self.assertFalse(self.publisher._command_is_signed(topic, body, self.props(None)))
        self.assertFalse(self.publisher._command_is_signed(topic, body, self.props("00" * 32)))
        self.publisher.settings = SimpleNamespace(device_key="")
        self.assertFalse(self.publisher._command_is_signed(topic, body, self.props(sign(self.key, topic, body))))


if __name__ == "__main__":
    unittest.main()
