import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Optional
from config import hardware_config
from config.settings import settings
from domain.enums import CommandAction, LockerHwState
from services.setup_handler import SetupHandler
from utils.logger import get_logger

logger = get_logger("LockerService")

# Mã thoát khi tự khởi động lại theo lệnh restart: khác 0 để systemd chạy lại lockr-controller với cả
# Restart=always lẫn Restart=on-failure (75 = EX_TEMPFAIL — lỗi tạm thời, chạy lại là được).
RESTART_EXIT_CODE = 75
# Chờ broker xác nhận kết quả restart tối đa chừng này giây rồi mới thoát.
RESTART_RESULT_TIMEOUT = 5.0


def _exit_process(code: int):
    """Mặc định khi không truyền exit_process: thoát ngay cả tiến trình.
    Handler chạy trên luồng phụ nên sys.exit chỉ dừng luồng đó, phải dùng os._exit."""
    os._exit(code)


class LockerService:
    """
    Điều phối lệnh MQTT ↔ phần cứng theo hợp đồng ADR-0008
    (docs/01-overview/mqtt-contract.md):

        cabinet/{lockerId}/command/open|close|sync   ← backend
        cabinet/{lockerId}/command/open/result        → backend (khớp commandId)
        cabinet/{lockerId}/command/restart            ← backend (KTV khởi động lại bộ điều khiển)
        cabinet/{lockerId}/command/restart/result     → backend (ACCEPTED rồi tiến trình thoát)
        cabinet/{lockerId}/locker/{slotIndex}/status  → backend (sự kiện cửa)
        iot/{mac}/command/setup|clear-setup           ← backend (admin gán tủ)
        iot/{mac}/discovery/start                     ← backend
    """

    def __init__(self, mqtt_client, hardware_controller,
                 serial_manager=None, cabinet_state=None,
                 heartbeat_service=None, db_manager=None,
                 discovery_service=None,
                 exit_process: Optional[Callable[[int], None]] = None,
                 lids: Optional[dict] = None):
        self.mqtt = mqtt_client
        self.hw = hardware_controller
        self.serial = serial_manager
        self.cabinet_state = cabinet_state
        self.heartbeat = heartbeat_service
        self.db = db_manager
        self.discovery = discovery_service
        # Gọi với RESTART_EXIT_CODE sau khi báo kết quả lệnh restart. main.py truyền hàm dừng êm
        # các dịch vụ rồi thoát; test truyền hàm ghi lại để không giết tiến trình test.
        self._exit_process = exit_process or _exit_process
        self._restart_requested = False
        self.lids = lids or {}

        # Lệnh phần cứng chạy tuần tự trên một luồng riêng: một lần mở mất ~3 s,
        # chạy thẳng trong callback MQTT sẽ chặn cả vòng lặp mạng của paho.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="LockerCmd")

        # Lệnh nắp drone đã nhận: (lockerId, action, commandId) → {"at", "payload"}; payload None = đang chạy.
        self._drone_lid_commands: dict = {}
        self._drone_lid_lock = threading.Lock()

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
            cabinet/{lockerId}/command/restart
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
        elif action == "restart":
            # Cùng hàng đợi với lệnh phần cứng: lệnh mở đang chạy dở xong rồi mới thoát. Cũng không được
            # chạy trên luồng mạng của paho — luồng đó phải rảnh để nhận PUBACK của kết quả.
            self._executor.submit(self._safe, self._handle_restart_command, cab, data)
        elif action == "lid":
            self._executor.submit(self._safe, self._handle_lid_command, cab, data)
        elif action == "lid-jog":
            self._executor.submit(self._safe, self._handle_lid_jog_command, cab, data)
        elif action == "lid-tune":
            self._executor.submit(self._safe, self._handle_lid_tune_command, cab, data)
        elif action == "panel":
            self._executor.submit(self._safe, self._handle_panel_command, cab, data)
        elif action == "hw-config":
            # Như restart: chờ lệnh phần cứng đang chạy xong, và không chạy trên luồng mạng của paho.
            self._executor.submit(self._safe, self._handle_hw_config_command, cab, data)
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


    # ═══════════════════════════════════════════════════════════
    #  LID / PANEL CONTROL (admin web: ADMIN + KTV tủ, qua iot-service)
    #  Topic lệnh:  cabinet/{id}/command/{lid|lid-jog|lid-tune|panel}
    #  Topic kết quả: cabinet/{id}/command/{...}/result  (khớp commandId)
    #  Lệnh chạy nắp trả ACCEPTED ngay (không chờ động cơ xong); web poll `panel` để xem trạng thái.
    # ═══════════════════════════════════════════════════════════

    def _publish_cmd_result(self, cab: dict, action: str, command_id, status: str, extra: Optional[dict] = None):
        payload = {"commandId": command_id, "status": status,
                   "timestamp": datetime.now(timezone.utc).isoformat()}
        if extra:
            payload.update(extra)
        self.mqtt.publish(f"cabinet/{cab['id']}/command/{action}/result", json.dumps(payload), qos=1)

    def _lid_by_axis(self, data: dict):
        axis = data.get("axis", 1)
        try:
            axis = int(axis)
        except (TypeError, ValueError):
            axis = 1
        return axis, (self.lids.get(axis) or self.lids.get(str(axis)))

    def _save_lid_tuning(self):
        try:
            from hardware.factory import save_lid_tuning
            save_lid_tuning(settings.LID_TUNING_FILE, self.lids)
        except Exception as e:
            logger.warning(f"Could not save lid tuning: {e}")

    def _handle_lid_command(self, cab: dict, data: dict):
        command_id = data.get("commandId")
        axis, lid = self._lid_by_axis(data)
        action = str(data.get("action", "")).lower()
        if lid is None:
            return self._publish_cmd_result(cab, "lid", command_id, "FAILED",
                {"axis": axis, "errorCode": "AXIS_DISABLED", "errorMessage": f"Trục {axis} chưa bật trên Pi"})
        if action not in ("open", "close", "home", "stop"):
            return self._publish_cmd_result(cab, "lid", command_id, "FAILED",
                {"axis": axis, "errorCode": "BAD_ACTION", "errorMessage": "Lệnh nắp phải là open/close/home/stop"})
        try:
            lid.stop() if action == "stop" else lid.run_async(action)
            self._publish_cmd_result(cab, "lid", command_id, "ACCEPTED",
                {"axis": axis, "action": action, "lid": lid.status()})
        except RuntimeError as e:
            busy = str(e) == "LID_BUSY"
            self._publish_cmd_result(cab, "lid", command_id, "FAILED",
                {"axis": axis, "errorCode": "LID_BUSY" if busy else "LID_ERROR",
                 "errorMessage": "Trục đang chạy, dừng trước rồi thử lại" if busy else str(e),
                 "lid": lid.status()})

    def _handle_lid_jog_command(self, cab: dict, data: dict):
        command_id = data.get("commandId")
        axis, lid = self._lid_by_axis(data)
        if lid is None:
            return self._publish_cmd_result(cab, "lid-jog", command_id, "FAILED",
                {"axis": axis, "errorCode": "AXIS_DISABLED", "errorMessage": f"Trục {axis} chưa bật trên Pi"})
        direction = str(data.get("direction", "open")).lower()
        try:
            revolutions = float(data.get("revolutions", 0))
        except (TypeError, ValueError):
            revolutions = 0
        if direction not in ("open", "close") or revolutions <= 0:
            return self._publish_cmd_result(cab, "lid-jog", command_id, "FAILED",
                {"axis": axis, "errorCode": "BAD_PARAM", "errorMessage": "Chiều phải là open/close và số vòng > 0"})
        max_revs = lid.config().get("max_revs", 12.5)
        if revolutions > max_revs:
            return self._publish_cmd_result(cab, "lid-jog", command_id, "FAILED",
                {"axis": axis, "errorCode": "TOO_MANY_REVS", "errorMessage": f"Tối đa {max_revs} vòng mỗi lần (giới hạn an toàn)"})
        try:
            lid.run_async("jog", revolutions=revolutions, toward_end=(direction == "open"))
            self._publish_cmd_result(cab, "lid-jog", command_id, "ACCEPTED",
                {"axis": axis, "revolutions": revolutions, "direction": direction, "lid": lid.status()})
        except RuntimeError as e:
            busy = str(e) == "LID_BUSY"
            self._publish_cmd_result(cab, "lid-jog", command_id, "FAILED",
                {"axis": axis, "errorCode": "LID_BUSY" if busy else "LID_ERROR",
                 "errorMessage": "Trục đang chạy, dừng trước rồi thử lại" if busy else str(e),
                 "lid": lid.status()})

    def _handle_lid_tune_command(self, cab: dict, data: dict):
        command_id = data.get("commandId")
        axis, lid = self._lid_by_axis(data)
        if lid is None:
            return self._publish_cmd_result(cab, "lid-tune", command_id, "FAILED",
                {"axis": axis, "errorCode": "AXIS_DISABLED", "errorMessage": f"Trục {axis} chưa bật trên Pi"})
        allowed = {"rps", "start_rps", "ramp_steps", "pulse_us", "max_revs",
                   "steps_per_rev", "open_dir_high", "limit_active_low"}
        changes = {k: v for k, v in (data.get("changes") or {}).items() if k in allowed and v is not None}
        try:
            config = lid.tune(**changes)
            self._save_lid_tuning()
            self._publish_cmd_result(cab, "lid-tune", command_id, "SUCCESS", {"axis": axis, "config": config})
        except (ValueError, RuntimeError) as e:
            busy = str(e) == "LID_BUSY"
            self._publish_cmd_result(cab, "lid-tune", command_id, "FAILED",
                {"axis": axis, "errorCode": "LID_BUSY" if busy else "TUNE_REJECTED",
                 "errorMessage": "Trục đang chạy, dừng trước rồi chỉnh" if busy else str(e)})

    def _handle_panel_command(self, cab: dict, data: dict):
        command_id = data.get("commandId")
        lids = []
        for axis in (1, 2):
            lid = self.lids.get(axis) or self.lids.get(str(axis))
            lids.append({"axis": axis, "enabled": lid is not None,
                         "boxes": self._lid_boxes()[axis],
                         "status": lid.status() if lid else None})
        self._publish_cmd_result(cab, "panel", command_id, "SUCCESS", {
            "lids": lids, "hardware": hardware_config.effective(settings, self.lids)})

    def _handle_hw_config_command(self, cab: dict, data: dict):
        """Topic: cabinet/{lockerId}/command/hw-config — admin đổi bản đồ chân trên web.

        Kiểm tra ⇒ lưu config/hardware_config.json (ghi đè `.env` từ lần khởi động sau) ⇒ đồng bộ
        chiều/mức công tắc vào lid_tuning.json ⇒ đổi chân relay thì cập nhật dòng an toàn lúc khởi động
        ⇒ báo ACCEPTED rồi thoát để systemd chạy lại với cấu hình mới. Sai ⇒ FAILED, không đổi gì.
        """
        command_id = data.get("commandId")
        topic = f"cabinet/{cab['id']}/command/hw-config/result"
        try:
            # wiring=None: giữ sơ đồ đấu dây đã ghi trên bảng /service của Pi.
            outcome = hardware_config.store(data.get("config"), settings)
        except OSError as e:
            return self._publish_cmd_result(cab, "hw-config", command_id, "FAILED", {
                "errorCode": "SAVE_FAILED", "errorMessage": f"Không lưu được cấu hình trên Pi: {e}"})
        if outcome["errors"]:
            logger.error(f"HW-CONFIG {command_id} rejected: {outcome['errors']}")
            return self._publish_cmd_result(cab, "hw-config", command_id, "FAILED", {
                "errorCode": "INVALID_CONFIG", "errorMessage": "; ".join(outcome["errors"]),
                "errors": outcome["errors"]})
        cfg, warnings = hardware_config.functional(outcome["config"]), outcome["warnings"]
        for warning in warnings:
            logger.error(warning)
        logger.warning(f"🛠 HW-CONFIG {command_id} saved — restarting with relay {cfg['relayPins']}, "
                       f"door {cfg['doorPins']}")
        payload = {"commandId": command_id, "status": "ACCEPTED", "config": cfg, "warnings": warnings,
                   "timestamp": datetime.now(timezone.utc).isoformat()}
        if not self.mqtt.publish_and_wait(topic, json.dumps(payload), qos=1, timeout=RESTART_RESULT_TIMEOUT):
            logger.error(f"HW-CONFIG {command_id}: broker chưa xác nhận kết quả — vẫn khởi động lại")
        self._restart_process(command_id)

    def _lid_for(self, data: dict, slot_index: Optional[int]):
        """Nắp trượt chỉ dùng khi lệnh ghi rõ `lid: true` — luồng drone thả hàng qua nóc tủ.
        Mở/đóng ô của khách, KTV, admin luôn kích relay của ô, kể cả ô drone (khách lấy hàng
        bằng cửa trước). `lids` có khoá là số trục, không phải số ô. Trục lấy từ `lidAxis` nếu lệnh
        ghi rõ, không thì theo ô qua `LIDn_BOXES`; ô không thuộc trục nào ⇒ None (lệnh báo NO_LID)."""
        if not self.lids or data.get("lid") is not True:
            return None
        axis = data.get("lidAxis")
        if axis is None:
            boxes = self._lid_boxes()
            if not any(boxes.values()):
                return self.lids.get(1) or self.lids.get("1") or next(iter(self.lids.values()))
            box_number = None if slot_index is None else slot_index + 1
            axis = next((a for a, numbers in boxes.items() if box_number in numbers), None)
            if axis is None:
                return None
        return self.lids.get(axis) or self.lids.get(str(axis))


    @staticmethod
    def _lid_boxes() -> dict:
        """{trục: [số ô]} mà nắp của trục nằm trên — `LID1_BOXES`, `LID2_BOXES` trong .env của Pi."""
        return {1: list(getattr(settings, "LID1_BOXES", None) or []),
                2: list(getattr(settings, "LID2_BOXES", None) or [])}


    def _no_lid(self, cab: dict, action: str, command_id, slot_index: int, box_id):
        """Lệnh drone cần nắp mà ô không có nắp: báo lỗi, không kích relay cửa trước thay vào."""
        logger.error(f"{action.upper()} {command_id}: ô {slot_index + 1} không có nắp trượt")
        self._publish_result(cab, action, command_id, slot_index, box_id, "FAILED",
                             LockerHwState.UNKNOWN.value, "NO_LID",
                             f"Ô {slot_index + 1} không có nắp trượt (LID1_BOXES/LID2_BOXES trên Pi)")

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
                        status: str, hw_state: str, error_code: Optional[str], message: str,
                        extra: Optional[dict] = None):
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
        if extra:
            payload.update(extra)
        self.mqtt.publish(f"cabinet/{cab['id']}/command/{action}/result", json.dumps(payload), qos=1)
        return payload

    # ═══════════════════════════════════════════════════════════
    #  NẮP TRƯỢT CỦA LUỒNG DRONE (lệnh open/close có `lid: true`)
    #  Không đổi cơ cấu motor (LidController) — chỉ thêm các chốt chặn quanh lời gọi:
    #   • commandId đã chạy ⇒ không chạy motor lần hai, gửi lại đúng kết quả cũ (backend gửi lại khi mất ACK)
    #   • lệnh mở quá `expiresAt` ⇒ bỏ (tủ kết nối lại không mở do lệnh cũ)
    #   • nắp đã ở đúng vị trí ⇒ không kích motor
    #   • báo ACCEPTED ngay khi nhận, rồi kết quả cuối; ghi rõ có/không có công tắc hành trình xác nhận
    # ═══════════════════════════════════════════════════════════

    def _drone_lid_cached(self, key):
        """Kết quả đã lưu của lệnh nắp drone (None nếu lệnh mới). Dọn bản ghi quá hạn nhớ."""
        now = time.monotonic()
        with self._drone_lid_lock:
            for old in [k for k, v in self._drone_lid_commands.items()
                        if now - v["at"] > settings.DRONE_LID_COMMAND_MEMORY_S]:
                del self._drone_lid_commands[old]
            entry = self._drone_lid_commands.get(key)
            if entry is None:
                self._drone_lid_commands[key] = {"at": now, "payload": None}
            return entry

    def _drone_lid_done(self, key, payload):
        with self._drone_lid_lock:
            self._drone_lid_commands[key] = {"at": time.monotonic(), "payload": payload}

    @staticmethod
    def _expired(expires_at) -> bool:
        if not expires_at:
            return False
        try:
            deadline = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            return True   # hạn không đọc được ⇒ không liều mở
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > deadline

    def _run_drone_lid(self, cab: dict, action: str, data: dict, slot_index, box_id, lid):
        """Chạy lệnh nắp drone (action = "open" | "close") với các chốt chặn ở trên."""
        cabinet_id = cab["id"]
        command_id = data.get("commandId")
        key = (str(cabinet_id), action, command_id)
        target = LockerHwState.OPEN.value if action == "open" else LockerHwState.CLOSED.value
        verb = "mở" if action == "open" else "đóng"

        seen = self._drone_lid_cached(key) if command_id else None
        if seen is not None:
            logger.warning(f"DRONE LID {action.upper()} duplicate: locker={cabinet_id}, slot={slot_index}, "
                           f"cmdId={command_id} — không chạy motor lần hai")
            if seen["payload"] is None:
                self._publish_result(cab, action, command_id, slot_index, box_id, "ACCEPTED", "MOVING", None,
                                     f"Tủ đã nhận lệnh {verb} này, nắp đang chạy", {"duplicate": True})
            else:
                cached = dict(seen["payload"], duplicate=True,
                              timestamp=datetime.now(timezone.utc).isoformat())
                self.mqtt.publish(f"cabinet/{cabinet_id}/command/{action}/result", json.dumps(cached), qos=1)
            return

        def finish(status, hw_state, error_code, message, extra=None):
            payload = self._publish_result(cab, action, command_id, slot_index, box_id, status, hw_state,
                                           error_code, message, extra)
            if command_id:
                self._drone_lid_done(key, payload)
            if self.heartbeat and status == "SUCCESS":
                self.heartbeat.update_locker_state(cabinet_id, slot_index, hw_state)
            logger.warning(f"DRONE LID {action.upper()} {status}: locker={cabinet_id}, slot={slot_index}, "
                           f"box={box_id}, cmdId={command_id}, hwState={hw_state}, error={error_code}")

        status_now = lid.status() if hasattr(lid, "status") else {}
        current = status_now.get("state")
        if action == "open" and self._expired(data.get("expiresAt")):
            return finish("FAILED", current or LockerHwState.UNKNOWN.value, "COMMAND_EXPIRED",
                          f"Lệnh mở nắp đã quá hạn ({data.get('expiresAt')}) — không mở")

        def confirmation(at_limit: bool):
            return "LIMIT_SWITCH" if at_limit else "STEP_COUNT"

        limit_check = getattr(lid, "at_end" if action == "open" else "at_home", None)
        at_limit = bool(limit_check()) if callable(limit_check) else bool(limit_check)
        if not getattr(lid, "busy", False) and current == target:
            if settings.LID_REQUIRE_LIMIT_SWITCH and not at_limit:
                return finish("FAILED", LockerHwState.UNKNOWN.value, "LIMIT_NOT_REACHED",
                              f"Trạng thái lưu của nắp là {verb} nhưng công tắc hành trình chưa xác nhận")
            return finish("SUCCESS", target, None, f"Nắp trượt đã ở vị trí {verb} — không chạy motor lại",
                          {"alreadyInPosition": True, "lidConfirmation": confirmation(bool(at_limit))})

        self._publish_result(cab, action, command_id, slot_index, box_id, "ACCEPTED", "MOVING", None,
                             f"Tủ đã nhận lệnh {verb} nắp trượt, nắp đang chạy")
        logger.warning(f"🚀 SLIDING LID {action.upper()} REQUEST: locker={cabinet_id}, slot={slot_index}, "
                       f"box={box_id}, cmdId={command_id}")
        if getattr(settings, "LID_BYPASS_PHYSICAL_MOTION", False):
            logger.info("⚡ LID_BYPASS_PHYSICAL_MOTION: chỉ báo kết quả, không trượt nắp cơ học.")
            return finish("SUCCESS", target, None, f"(Giả lập — hardware bypass) báo nắp đã {verb}, nắp KHÔNG trượt",
                          {"lidConfirmation": "BYPASS"})

        result = lid.open() if action == "open" else lid.close()
        step_count_only = (result.get("error") == "LIMIT_NOT_REACHED"
                           and not settings.LID_REQUIRE_LIMIT_SWITCH)
        ok = result.get("result") == "OK" or step_count_only
        if not ok:
            error_code = result.get("error") or "LID_JAMMED"
            return finish("FAILED", (lid.status() or {}).get("state", LockerHwState.UNKNOWN.value), error_code,
                          f"{verb.capitalize()} nắp trượt ô tủ thất bại: {error_code}")
        reached_limit = bool(limit_check()) if callable(limit_check) else bool(limit_check)
        if not reached_limit and settings.LID_REQUIRE_LIMIT_SWITCH:
            return finish("FAILED", LockerHwState.UNKNOWN.value, "LIMIT_NOT_REACHED",
                          f"Nắp chạy hết số bước nhưng không chạm công tắc hành trình — chưa xác nhận đã {verb}")
        message = (f"Nắp trượt ô tủ đã {verb} hoàn toàn (công tắc hành trình)" if reached_limit else
                   f"Nắp đã chạy hết hành trình theo số bước — chưa có công tắc hành trình xác nhận đã {verb}")
        return finish("SUCCESS", target, None, message, {"lidConfirmation": confirmation(reached_limit)})

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

        lid = self._lid_for(data, slot_index)
        if data.get("lid") is True and lid is None:
            self._no_lid(cab, "open", command_id, slot_index, box_id)
            return
        if lid:
            self._run_drone_lid(cab, "open", data, slot_index, box_id, lid)
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

        lid = self._lid_for(data, slot_index)
        if data.get("lid") is True and lid is None:
            self._no_lid(cab, "close", command_id, slot_index, box_id)
            return
        if lid:
            self._run_drone_lid(cab, "close", data, slot_index, box_id, lid)
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
    #  RESTART (BE → RPi)
    # ═══════════════════════════════════════════════════════════

    def _handle_restart_command(self, cab: dict, data: dict):
        """Topic: cabinet/{lockerId}/command/restart — báo ACCEPTED rồi thoát để systemd chạy lại dịch vụ.

        Chỉ khởi động lại tiến trình lockr-controller, không reboot máy. Kết quả gửi QoS 1 và chờ broker
        xác nhận trước khi thoát; quá RESTART_RESULT_TIMEOUT vẫn thoát (lệnh đã tới Pi, backend sẽ báo TIMEOUT).
        """
        command_id = data.get("commandId")
        topic = f"cabinet/{cab['id']}/command/restart/result"
        action = data.get("action")
        if action is not None and action != CommandAction.RESTART.value:
            logger.error(f"RESTART {command_id}: action không hợp lệ ({action}) — không khởi động lại")
            self.mqtt.publish(topic, json.dumps({
                "commandId": command_id,
                "status": "FAILED",
                "errorMessage": f"Lệnh restart có action không hợp lệ: {action}",
            }), qos=1)
            return

        logger.warning(f"🔄 RESTART REQUEST: locker={cab['id']}, cmdId={command_id}")
        accepted = json.dumps({"commandId": command_id, "status": "ACCEPTED"})
        if not self.mqtt.publish_and_wait(topic, accepted, qos=1, timeout=RESTART_RESULT_TIMEOUT):
            logger.error(f"RESTART {command_id}: broker chưa xác nhận kết quả — vẫn khởi động lại")

        self._restart_process(command_id)

    def _restart_process(self, command_id):
        """Thoát với RESTART_EXIT_CODE để systemd chạy lại lockr-controller (lệnh restart, hw-config)."""
        if self._restart_requested:
            # QoS 1 có thể giao lại lệnh, hoặc KTV bấm lại khi tiến trình đang dừng: trả lời nhưng không thoát lần nữa
            logger.info(f"RESTART {command_id}: đang khởi động lại rồi, bỏ qua lệnh trùng")
            return
        self._restart_requested = True
        logger.warning(f"🔄 Thoát với mã {RESTART_EXIT_CODE} để systemd khởi động lại lockr-controller")
        self._exit_process(RESTART_EXIT_CODE)

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
