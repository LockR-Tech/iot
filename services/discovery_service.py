import json
import os
from datetime import datetime, timezone
from config.settings import settings
from utils.logger import get_logger

logger = get_logger("DiscoveryService")

class DiscoveryService:
    """
    Pi tự báo mình cho backend: MAC, phần cứng, số ô, tủ đang phục vụ.
    Backend dùng bản báo này để admin gán Pi vào tủ (ADR-0008).
    """

    def __init__(self, mqtt_client, serial_manager, cabinet_state=None):
        self.mqtt = mqtt_client
        self.serial = serial_manager
        self.cabinet_state = cabinet_state

    @staticmethod
    def _hardware_kind() -> str:
        if os.getenv("SIMULATION", "false").lower() == "true":
            return "simulation"
        return settings.HARDWARE_BACKEND

    def build_payload(self, slaves: list) -> dict:
        locker_id = self.cabinet_state.primary_locker_id if self.cabinet_state else None
        return {
            "macAddress": settings.MAC_ADDRESS,
            "firmwareVersion": settings.FIRMWARE_VERSION,
            "hardware": self._hardware_kind(),
            "lockerId": int(locker_id) if locker_id and str(locker_id).isdigit() else None,
            "slaves": slaves, # List of {"slaveId": N, "availableSlots": M}
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def discover_and_report(self, max_cabinets: int = None):
        """
        Thực hiện quét slaveId và gửi kết quả về backend.
        Topic: iot/{macAddress}/discovery/result
        """
        mac = settings.MAC_ADDRESS
        logger.info(f"Starting discovery for RPi: {mac} (max_cabinets={max_cabinets})")

        # 1. Quét phần cứng (Theo dải config từ settings hoặc dynamic từ BE)
        range_end = max_cabinets if max_cabinets is not None else settings.MAX_CABINETS
        slaves = self.serial.scan_slaves(range_start=1, range_end=range_end)

        # 2. Publish MQTT
        topic = f"iot/{mac}/discovery/result"
        self.mqtt.publish(topic, json.dumps(self.build_payload(slaves)), qos=1)
        logger.info(f"Discovery results reported to {topic}")
        return slaves
