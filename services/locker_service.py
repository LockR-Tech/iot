import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional
from config.settings import settings
from domain.enums import CommandAction, LockerHwState
from services.setup_handler import SetupHandler
from utils.logger import get_logger

logger = get_logger("LockerService")


class LockerService:
    """
    Điều phối lệnh MQTT ↔ phần cứng theo hợp đồng ADR-0008
    (docs/01-overview/mqtt-contract.md):

        cabinet/{lockerId}/command/open|close|sync   ← backend
        cabinet/{lockerId}/command/open/result        → backend (khớp commandId)
        cabinet/{lockerId}/locker/{slotIndex}/status  → backend (sự kiện cửa)
        iot/{mac}/command/setup|clear-setup           ← backend (admin gán tủ)
        iot/{mac}/discovery/start                     ← backend
    """

    def __init__(self, mqtt_client, hardware_controller,
                 serial_manager=None, cabinet_state=None,
                 heartbeat_service=None, db_manager=None,
                 discovery_service=None):
        self.mqtt = mqtt_client
        self.hw = hardware_controller
        self.serial = serial_manager
        self.cabinet_state = cabinet_state
        self.heartbeat = heartbeat_service
        self.db = db_manager
        self.discovery = discovery_service

        # Lệnh phần cứng chạy tuần tự trên một luồng riêng: một lần mở mất ~3 s,
        # chạy thẳng trong callback MQTT sẽ chặn cả vòng lặp mạng của paho.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="LockerCmd")

        # Setup handler (nếu có serial manager)
        self.setup_handler = None
        if self.serial:
            self.setup_handler = SetupHandler(
                mqtt_client,
                serial_manager,
                cabinet_state=cabinet_state,
                on_setup_complete=self._on_setup_complete,
            )

        if self.cabinet_state:
            self.cabinet_state.add_listener(self._on_cabinets_changed)

        self._start_time = time.time()

    # ═══════════════════════════════════════════════════════════
    #  SUBSCRIPTIONS
    # ═══════════════════════════════════════════════════════════

    def command_topics(self) -> set:
        """Topic lệnh vận hành của mọi tủ đang phục vụ."""
        if not self.cabinet_state:
            return set()
        return {f"cabinet/{cab['id']}/command/+" for cab in self.cabinet_state.all_cabinets}

    def refresh_subscriptions(self):
        topics = self.command_topics()
        self.mqtt.set_subscriptions(topics)
        if not topics:
            logger.warning("Chưa phục vụ tủ nào — chờ admin gán Pi vào tủ, hoặc đặt LOCKER_ID trong .env")

    def _on_cabinets_changed(self):
        self.refresh_subscriptions()
        if self.heartbeat:
            if self.cabinet_state.is_configured:
                self.heartbeat.start()
            else:
                self.heartbeat.stop()

    # ═══════════════════════════════════════════════════════════
    #  MESSAGE ROUTING
    # ═══════════════════════════════════════════════════════════

    def handle_incoming_message(self, topic: str, payload_str: str):
        """
        Xử lý messages từ MQTT.
        Topics:
            iot/{macAddress}/command/setup
            iot/{macAddress}/command/clear-setup
            iot/{macAddress}/discovery/start
            cabinet/{lockerId}/command/open
            cabinet/{lockerId}/command/close
            cabinet/{lockerId}/command/sync
        """
        try:
            # Log MQTT message to database
            if self.db:
                self.db.log_mqtt(topic, payload_str, direction="IN")

            # Route: Discovery start command
            if topic.endswith("/discovery/start"):
                logger.info(f"Received discovery trigger on {topic}")
                if self.discovery:
                    # Parse maxCabinets from payload if available
                    max_cabinets = None
                    try:
                        data = json.loads(payload_str)
                        max_cabinets = data.get("maxCabinets")
                    except Exception:
                        pass

                    threading.Thread(target=self.discovery.discover_and_report,
                                     args=(max_cabinets,), daemon=True).start()
                else:
                    logger.error("Discovery service not available in LockerService")
                return

            data = json.loads(payload_str)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON payload on {topic}")
            return
        if not isinstance(data, dict):
            logger.error(f"Payload on {topic} is not a JSON object")
            return

        # ─── Route: Setup command ───
        if topic.endswith("/command/setup"):
            self._handle_setup_command(topic, data)
            return

        # ─── Route: Clear Setup command ───
        if topic.endswith("/command/clear-setup"):
            self._handle_clear_setup_command(topic, data)
            return

        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != "cabinet" or parts[2] != "command":
            logger.debug(f"Unhandled topic: {topic}")
            return

        cab = self.cabinet_state.get_cabinet_by_id(parts[1]) if self.cabinet_state else None
        if not cab:
            logger.error(f"Command for locker {parts[1]} ignored – Pi is not serving that locker")
            return

        action = parts[3]
        if action == "open":
            self._executor.submit(self._safe, self._handle_open_command, cab, data)
        elif action == "close":
            self._executor.submit(self._safe, self._handle_close_command, cab, data)
        elif action == "sync":
            self._handle_sync_command(cab, data)
        else:
            logger.debug(f"Unhandled command: {topic}")

    @staticmethod
    def _safe(fn, *args):
        try:
            fn(*args)
        except Exception as e:
            logger.error(f"Command handler {fn.__name__} failed: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════
    #  SETUP COMMAND (BE → RPi)
    # ═══════════════════════════════════════════════════════════

    def _handle_setup_command(self, topic: str, data: dict):
        """
        Nhận lệnh setup từ BE.
        """
        action = data.get("action", "")
        if action not in [CommandAction.SETUP_LOCKERS.value, CommandAction.BULK_SETUP_LOCKERS.value]:
            logger.debug(f"Ignoring action: {action}")
            return

        # Check macAddress
        payload_mac = data.get("macAddress", "").lower()
        my_mac = settings.MAC_ADDRESS.lower()

        if payload_mac != my_mac:
            logger.warning(f"Ignoring setup command – MAC mismatch (payload={payload_mac}, mine={my_mac})")
            return

        # Extract prefix: iot/{macAddress}
        # Topic format: iot/{macAddress}/command/setup
        parts = topic.split("/")
        prefix = f"{parts[0]}/{parts[1]}"

        logger.warning(f"📋 Setup command received for gateway {prefix} (Action: {action})")

        # Chạy setup handler
        if self.setup_handler:
            self.setup_handler.handle(data, prefix)
        else:
            logger.error("Setup handler not available")

    # ═══════════════════════════════════════════════════════════
    #  CLEAR SETUP COMMAND
    # ═══════════════════════════════════════════════════════════

    def _handle_clear_setup_command(self, topic: str, data: dict):
        """Xoá toàn bộ cấu hình (quay về LOCKER_ID trong .env nếu có)."""
        action = data.get("action", "")
        if action != CommandAction.CLEAR_SETUP.value: return

        def do_clear():
            if self.cabinet_state:
                self.cabinet_state.clear()   # listener tự subscribe lại + bật/tắt heartbeat
                logger.info("System state cleared")
                if self.discovery:
                    self.discovery.discover_and_report()

        threading.Thread(target=do_clear, daemon=True).start()

    # ═══════════════════════════════════════════════════════════
    #  OPEN / CLOSE / SYNC (BE → RPi)
    # ═══════════════════════════════════════════════════════════

    def _resolve_slot(self, cab: dict, data: dict):
        """(slotIndex, boxId) của lệnh: slotIndex nếu backend gửi, không thì tra boxId trong sơ đồ."""
        box_id = data.get("boxId", data.get("box_id"))
        slot_index = data.get("slotIndex")
        if slot_index is not None:
            try:
                slot_index = int(slot_index)
            except (TypeError, ValueError):
                slot_index = None
        if slot_index is None and box_id is not None:
            slot_index = self.cabinet_state.slot_for_box(cab["id"], box_id)
        if slot_index is not None and box_id is not None:
            self.cabinet_state.remember_box(cab["id"], slot_index, box_id)
        elif slot_index is not None:
            box_id = self.cabinet_state.box_id_for_slot(cab["id"], slot_index)
        return slot_index, box_id

    def _publish_result(self, cab: dict, action: str, command_id, slot_index, box_id,
                        status: str, hw_state: str, error_code: Optional[str], message: str):
        payload = {
            "commandId": command_id,
            "boxId": box_id,
            "slotIndex": slot_index,
            "status": status,
            "hwState": hw_state,
            "errorCode": error_code,
            "errorMessage": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.mqtt.publish(f"cabinet/{cab['id']}/command/{action}/result", json.dumps(payload), qos=1)

    def _handle_open_command(self, cab: dict, data: dict):
        """Topic: cabinet/{lockerId}/command/open"""
        cabinet_id = cab["id"]
        slave_id = cab.get("slaveId", 1)
        command_id = data.get("commandId")
        slot_index, box_id = self._resolve_slot(cab, data)

        if slot_index is None:
            logger.error(f"OPEN {command_id}: không biết ô nào (boxId={box_id}, không có slotIndex)")
            self._publish_result(cab, "open", command_id, None, box_id, "FAILED",
                                 LockerHwState.UNKNOWN.value, "UNKNOWN_SLOT",
                                 "Lệnh không có slotIndex và boxId không có trong sơ đồ ô của Pi")
            return
        if not self.serial:
            self._publish_result(cab, "open", command_id, slot_index, box_id, "FAILED",
                                 LockerHwState.UNKNOWN.value, "HW_ERROR", "Hardware not available")
            return

        logger.warning(f"🔓 OPEN REQUEST: locker={cabinet_id}, slot={slot_index}, box={box_id}, cmdId={command_id}")
        result = self.serial.open_slot(slot_index, slave_id=slave_id)
        relay_ok = result.get("result") == "OK"
        door_closed = result.get("door", True)   # door: True = cửa đóng
        hw_state = LockerHwState.CLOSED.value if door_closed else LockerHwState.OPEN.value

        if relay_ok and (not door_closed or not settings.REQUIRE_DOOR_SENSOR):
            status, error_code, message = "SUCCESS", None, "Door opened"
        elif relay_ok:
            status, error_code = "FAILED", "JAMMED"
            message = "Relay chạy nhưng cảm biến vẫn thấy cửa đóng (kẹt cửa hoặc cảm biến chưa nối)"
        else:
            status = "FAILED"
            error_code = result.get("error", "HW_ERROR")
            message = f"Hardware error: {error_code}"

        self._publish_result(cab, "open", command_id, slot_index, box_id, status, hw_state, error_code, message)
        if self.heartbeat:
            self.heartbeat.update_locker_state(cabinet_id, slot_index, hw_state)

    def _handle_close_command(self, cab: dict, data: dict):
        """Topic: cabinet/{lockerId}/command/close — dành sẵn, backend hiện chưa gửi."""
        cabinet_id = cab["id"]
        slave_id = cab.get("slaveId", 1)
        command_id = data.get("commandId")
        slot_index, box_id = self._resolve_slot(cab, data)

        if slot_index is None:
            self._publish_result(cab, "close", command_id, None, box_id, "FAILED",
                                 LockerHwState.UNKNOWN.value, "UNKNOWN_SLOT",
                                 "Lệnh không có slotIndex và boxId không có trong sơ đồ ô của Pi")
            return
        if not self.serial:
            logger.error("Serial not available")
            return

        logger.warning(f"🔒 CLOSE REQUEST: locker={cabinet_id}, slot={slot_index}, cmdId={command_id}")
        result = self.serial.close_slot(slot_index, slave_id=slave_id)
        ok = result.get("result") == "OK"
        door_closed = result.get("door", True)
        hw_state = LockerHwState.CLOSED.value if door_closed else LockerHwState.OPEN.value

        if ok and door_closed:
            status, error_code, message = "SUCCESS", None, "Door is closed and locked"
        else:
            status = "FAILED"
            message = "Door failed to lock or is still open"
            error_code = result.get("error", "HW_ERROR") if not ok else "DOOR_OPEN"

        self._publish_result(cab, "close", command_id, slot_index, box_id, status, hw_state, error_code, message)
        if self.heartbeat:
            self.heartbeat.update_locker_state(cabinet_id, slot_index, hw_state)

    def _handle_sync_command(self, cab: dict, data: dict):
        """Topic: cabinet/{lockerId}/command/sync — backend báo trạng thái đặt chỗ của ô, không cần phản hồi."""
        slot_index, box_id = self._resolve_slot(cab, data)
        logger.info(f"SYNC: locker={cab['id']} slot={slot_index} box={box_id} state={data.get('state')}")

    # ═══════════════════════════════════════════════════════════
    #  DOOR EVENT (cảm biến → RPi → BE)
    # ═══════════════════════════════════════════════════════════

    def handle_door_event(self, slot_index: int, event_type: str, slave_id: int = 1):
        """
        Callback khi cảm biến cửa đổi trạng thái (GPIO hoặc Arduino).
        Topic: cabinet/{lockerId}/locker/{slotIndex}/status
        """
        cab = self.cabinet_state.get_cabinet_by_slave(slave_id) if self.cabinet_state else None
        if not cab:
            logger.warning(f"Event ignored – no cabinet mapped to Slave {slave_id}")
            return

        cabinet_id = cab["id"]
        door_open = event_type == "DOOR_OPENED"
        hw_state = LockerHwState.OPEN.value if door_open else LockerHwState.CLOSED.value
        box_id = self.cabinet_state.box_id_for_slot(cabinet_id, slot_index)

        logger.warning(f"🚪 {event_type}: locker={cabinet_id}, slot={slot_index}, box={box_id}")

        status_payload = {
            "slotIndex": slot_index,
            "hwState": hw_state,
            "doorOpen": door_open,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if box_id is not None:
            status_payload["boxId"] = box_id
        self.mqtt.publish(f"cabinet/{cabinet_id}/locker/{slot_index}/status", json.dumps(status_payload), qos=1)

        if self.heartbeat:
            self.heartbeat.update_locker_state(cabinet_id, slot_index, hw_state)

    def _on_setup_complete(self, cabinet_id: str):
        """Setup xong -> subscribe lệnh của tủ, báo heartbeat + discovery để backend thấy ngay."""
        logger.info(f"Setup complete for locker {cabinet_id}")
        self.refresh_subscriptions()
        if self.heartbeat:
            self.heartbeat.start()
            self.heartbeat.publish_now()
        if self.discovery:
            threading.Thread(target=self.discovery.discover_and_report, daemon=True).start()

    def shutdown(self):
        self._executor.shutdown(wait=True, cancel_futures=True)

    # ═══════════════════════════════════════════════════════════
    #  UTILITY METHODS
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _get_ip_address() -> str:
        """Lấy IP address hiện tại."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "0.0.0.0"
