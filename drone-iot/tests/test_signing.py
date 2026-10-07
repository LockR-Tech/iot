import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lockr_drone.signing import device_key, sign  # noqa: E402


class SigningTest(unittest.TestCase):
    """Vector cố định — `DroneTelemetrySignerTest` ở backend dùng đúng các giá trị này."""

    MASTER = "test-master-secret"
    DRONE = "DRONE-S550-01"
    TOPIC = "lockr/drones/DRONE-S550-01/telemetry"
    PAYLOAD = '{"schemaVersion": 1, "droneId": "DRONE-S550-01", "sequence": 1}'

    def test_device_key_and_signature_match_the_backend_vector(self):
        key = device_key(self.MASTER, self.DRONE)
        self.assertEqual(key, "22e3e9b5237ea3f838e29f419f2da40d1850e21d7e4592fc606ea2ad981c1631")
        self.assertEqual(sign(key, self.TOPIC, self.PAYLOAD), "cb7a596f42ae3b100cfc020a4201ac032fd8bc304e6d94b81a95e6121926819b")

    def test_signature_depends_on_topic_and_payload(self):
        key = device_key(self.MASTER, self.DRONE)
        base = sign(key, self.TOPIC, self.PAYLOAD)
        self.assertNotEqual(base, sign(key, "lockr/drones/DRONE-02/telemetry", self.PAYLOAD))
        self.assertNotEqual(base, sign(key, self.TOPIC, self.PAYLOAD + " "))
        self.assertNotEqual(key, device_key(self.MASTER, "DRONE-02"))


if __name__ == "__main__":
    unittest.main()
