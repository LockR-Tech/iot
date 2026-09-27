import time
import threading
from datetime import datetime, timezone
from config.settings import settings
from infracstructure.serial_manager import MAX_SLOTS
from domain.enums import LockerHwState
from domain.models import HeartbeatPayload
from utils.logger import get_logger

logger = get_logger("HeartbeatService")

# Heartbeat interval mặc định (giây)
DEFAULT_HEARTBEAT_INTERVAL = 60


class HeartbeatService:
    """
    Publish heartbeat định kỳ lên MQTT để BE biết RPi còn online.

    Topic: cabinet/{lockerId}/heartbeat (QoS 0) — docs/01-overview/mqtt-contract.md § 2.2
    Payload: {
        cabinetId, status: "online", macAddress, firmwareVersion, uptime, timestamp,
        lockers: [{ slotIndex, boxId?, hwState }]
    }
    """

    def __init__(self, mqtt_client, cabinet_state,
                 interval: int = DEFAULT_HEARTBEAT_INTERVAL, hardware=None):
        self.mqtt = mqtt_client
        self.cabinet_state = cabinet_state
        self.interval = interval
        # GpioLockerManager có door_states() đọc thẳng cảm biến; Arduino thì dựa vào sự kiện.
        self.hardware = hardware

        self._thread: threading.Thread | None = None
        self._running = False
        self._start_time = time.time()

        # hwState của từng slot theo sự kiện gần nhất: cabinet_id -> slot -> hwState
        self._locker_states: dict[str, dict[int, str]] = {}

    # ─── Locker state tracking ───

    def update_locker_state(self, cabinet_id: str, slot_index: int, hw_state: str):
        """Cập nhật hwState cho slot của một cabinet."""
        self._locker_states.setdefault(str(cabinet_id), {})[slot_index] = hw_state

    def _slot_count(self, cab: dict) -> int:
        layout = self.cabinet_state.layout_slots(cab["id"])
        if layout:
            return max(layout) + 1
        hw_slots = getattr(self.hardware, "num_slots", None)
        if isinstance(hw_slots, int) and hw_slots > 0:
            return hw_slots
        return cab.get("totalRows", 0) * cab.get("totalColumns", 0) or MAX_SLOTS

    def _get_lockers_status(self, cabinet_id: str) -> list:
        """Lấy danh sách trạng thái lockers cho một cabinet."""
        cab = self.cabinet_state.get_cabinet_by_id(cabinet_id)
        if not cab:
            return []

        doors = None
        if self.hardware is not None and hasattr(self.hardware, "door_states"):
            try:
                doors = self.hardware.door_states()
            except Exception as e:
                logger.debug(f"Cannot read door sensors: {e}")
        tracked = self._locker_states.get(str(cabinet_id), {})

        lockers = []
        for slot in range(self._slot_count(cab)):
            if doors is not None and slot < len(doors):
                hw_state = LockerHwState.CLOSED.value if doors[slot] else LockerHwState.OPEN.value
            else:
                hw_state = tracked.get(slot, LockerHwState.UNKNOWN.value)
            entry = {"slotIndex": slot, "hwState": hw_state}
            box_id = self.cabinet_state.box_id_for_slot(cabinet_id, slot)
            if box_id is not None:
                entry["boxId"] = box_id
            lockers.append(entry)
        return lockers

    # ─── Heartbeat loop ───

    def start(self):
        """Bắt đầu gửi heartbeat định kỳ (background thread)."""
        if not self.cabinet_state.is_configured:
            logger.info("Heartbeat not started – no cabinets configured yet")
            return

        if self._running:
            logger.debug("Heartbeat already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="HeartbeatThread"
        )
        self._thread.start()
        logger.info(f"Heartbeat service started (interval: {self.interval}s)")

    def stop(self):
        """Dừng heartbeat."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
            logger.info("Heartbeat service stopped")

    def _heartbeat_loop(self):
        """Loop gửi heartbeat mỗi interval giây."""
        while self._running:
            try:
                self._publish_all_heartbeats()
            except Exception as e:
                logger.error(f"Heartbeat publish error: {e}")

            # Sleep theo interval nhưng check _running mỗi giây để shutdown nhanh
            for _ in range(self.interval):
                if not self._running:
                    return
                time.sleep(1)

    def publish_now(self):
        """Gửi heartbeat ngay (ví dụ vừa được gán vào tủ mới)."""
        try:
            self._publish_all_heartbeats()
        except Exception as e:
            logger.error(f"Heartbeat publish error: {e}")

    def _publish_all_heartbeats(self):
        """Build và publish heartbeat cho TẤT CẢ các cabinet đang phục vụ."""
        if not getattr(self.mqtt, "is_connected", True):
            return   # chưa kết nối: bỏ lượt này, kết nối xong main.py gửi ngay một nhịp
        for cab in self.cabinet_state.all_cabinets:
            self._publish_single_heartbeat(cab)

    def build_payload(self, cabinet: dict) -> HeartbeatPayload:
        return HeartbeatPayload(
            cabinetId=cabinet["id"],
            timestamp=datetime.now(timezone.utc).isoformat(),
            status="online",
            lockers=self._get_lockers_status(cabinet["id"]),
            macAddress=settings.MAC_ADDRESS,
            firmwareVersion=settings.FIRMWARE_VERSION,
            uptime=int(time.time() - self._start_time),
        )

    def _publish_single_heartbeat(self, cabinet: dict):
        """Build và publish heartbeat cho 1 cabinet."""
        topic = f"cabinet/{cabinet['id']}/heartbeat"
        self.mqtt.publish(topic, self.build_payload(cabinet).to_json(), qos=0)
        logger.debug(f"💓 Heartbeat → {topic}")
