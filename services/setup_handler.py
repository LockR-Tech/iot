import time
import threading
from datetime import datetime, timezone
from dataclasses import asdict
from config.settings import settings
from domain.enums import LockerHwState
from domain.models import (
    SetupProgressPayload,
    SetupResultPayload,
    HwDetail,
    SetupProgress,
    LockerResultDetail,
    SetupResultSummary,
)
from utils.logger import get_logger

logger = get_logger("SetupHandler")


class SetupHandler:
    """
    Xử lý lệnh SETUP_LOCKERS từ BE — admin gán Pi vào một tủ (ADR-0008).

    Khi nhận lệnh setup:
    1. Kiểm sơ đồ ô với số ô phần cứng
    2. (testDoors) Lần lượt thử từng ô, publish tiến độ tới iot/{mac}/setup/progress
    3. Publish kết quả tới iot/{mac}/setup/result
    4. Không FAILED ⇒ lưu tủ (id = lockerId) và sơ đồ slotIndex ↔ boxId

    Hợp đồng payload: docs/01-overview/mqtt-contract.md § 3.
    """

    def __init__(self, mqtt_client, serial_manager, cabinet_state=None,
                 on_setup_complete=None):
        self.mqtt = mqtt_client
        self.serial = serial_manager
        self.cabinet_state = cabinet_state
        self._on_setup_complete = on_setup_complete
        self._is_running = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _hardware_slots(self) -> int:
        from infracstructure.serial_manager import MAX_SLOTS
        slots = getattr(self.serial, "num_slots", None)
        return slots if isinstance(slots, int) and slots > 0 else MAX_SLOTS

    @staticmethod
    def _locker_id(cab_data: dict):
        """lockerId của backend; bản setup cũ chỉ có cabinetId."""
        raw = cab_data.get("lockerId", cab_data.get("cabinetId"))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def handle(self, payload: dict, prefix: str):
        """
        Handle SETUP_LOCKERS or BULK_SETUP_LOCKERS command.
        prefix = "iot/{macAddress}" (lấy từ topic)
        """
        if self._is_running:
            logger.warning("Setup already in progress, ignoring new command")
            return

        action = payload.get("action", "")

        # ─── Validate Payload ───
        if action == "BULK_SETUP_LOCKERS":
            cabinets = payload.get("cabinets", [])
            if not cabinets:
                logger.error("Bulk setup rejected: no cabinets in payload")
                return
        else:
            layout = payload.get("lockerLayout", [])
            error = self._validate_layout(layout)
            if error:
                logger.error(f"Setup REJECTED: {error}")
                self._report_failure(payload, prefix, error)
                return

        thread = threading.Thread(
            target=self._run_setup,
            args=(payload, prefix),
            daemon=True,
            name="SetupThread"
        )
        thread.start()

    def _validate_layout(self, layout: list):
        hw_slots = self._hardware_slots()
        if len(layout) > hw_slots:
            return f"Sơ đồ có {len(layout)} ô, phần cứng chỉ có {hw_slots} ô"
        for slot in layout:
            try:
                slot_index = int(slot.get("slotIndex"))
            except (TypeError, ValueError):
                return f"slotIndex không hợp lệ: {slot.get('slotIndex')!r}"
            if not 0 <= slot_index < hw_slots:
                return f"slotIndex {slot_index} ngoài khoảng 0–{hw_slots - 1} của phần cứng"
        return None

    def _report_failure(self, payload: dict, prefix: str, error_msg: str):
        locker_id = self._locker_id(payload)
        reject_payload = SetupResultPayload(
            commandId=payload.get("commandId", ""),
            cabinetId=str(locker_id if locker_id is not None else payload.get("cabinetId", "")),
            status="FAILED",
            summary=asdict(SetupResultSummary(total=0, totalOk=0, totalFail=0, duration=0)),
            lockers=[],
            timestamp=datetime.now(timezone.utc).isoformat(),
            lockerId=locker_id,
            errorMessage=error_msg,
        )
        self.mqtt.publish(f"{prefix}/setup/result", reject_payload.to_json(), qos=1)

    def _test_slot(self, slot_index: int, slave_id: int, timeout: int) -> dict:
        """Mở thử một ô, trả {testResult, hwState, errorCode, errorMessage, doorClosed, ms}."""
        result = self.serial.test_slot(slot_index, slave_id=slave_id, timeout=timeout)
        relay_ok = result.get("result") == "OK"
        has_door = "door" in result
        door_closed = bool(result.get("door", False))
        hw_state = (LockerHwState.CLOSED.value if door_closed else LockerHwState.OPEN.value) \
            if has_door else LockerHwState.UNKNOWN.value

        if not relay_ok:
            test_result, error_code = "FAIL", result.get("error", "HW_ERROR")
            error_msg = f"Hardware test failed: {error_code}"
        elif settings.REQUIRE_DOOR_SENSOR and door_closed:
            test_result, error_code = "FAIL", "JAMMED"
            error_msg = "Relay chạy nhưng cảm biến vẫn thấy cửa đóng"
        else:
            test_result, error_code, error_msg = "OK", None, None
        return {
            "testResult": test_result, "hwState": hw_state, "errorCode": error_code,
            "errorMessage": error_msg, "doorClosed": door_closed, "ms": result.get("ms"),
        }

    def _run_setup(self, payload: dict, prefix: str):
        """Logic chính setup – chạy trên thread riêng."""
        import logging
        from utils.logger import set_global_log_level
        self._is_running = True
        set_global_log_level(logging.INFO)

        action = payload.get("action", "")
        command_id = payload.get("commandId", "no-id")

        try:
            test_timeout = payload.get("testTimeout", 10)
            test_doors = payload.get("testDoors", True) is not False

            # Danh sách các tủ cần setup
            cabinets_to_process = []
            if action == "BULK_SETUP_LOCKERS":
                cabinets_to_process = payload.get("cabinets", [])
            else:
                # Wrap single setup into list
                cabinets_to_process = [{
                    "lockerId": payload.get("lockerId"),
                    "cabinetId": payload.get("cabinetId"),
                    "cabinetCode": payload.get("cabinetCode", "Unknown"),
                    "slaveId": payload.get("slaveId", 1),
                    "lockerLayout": payload.get("lockerLayout", []),
                    "totalRows": payload.get("totalRows", 0),
                    "totalColumns": payload.get("totalColumns", 0)
                }]

            logger.info(f"=== {action} START: count={len(cabinets_to_process)}, testDoors={test_doors} ===")

            # 1. Lưu Location (chung cho cả gateway)
            if self.cabinet_state:
                # [NEW] Xoá sạch state cũ trước khi đồng bộ mới nếu là lệnh BULK
                if action == "BULK_SETUP_LOCKERS":
                    logger.info("Bulk setup detected. Pruning old cabinet state.")
                    self.cabinet_state.clear()

                loc_id = payload.get("locationId") or "unknown-loc"
                loc_name = payload.get("locationName") or "Unknown Location"
                loc_addr = payload.get("address") or ""
                self.cabinet_state.save_location(loc_id, loc_name, loc_addr)

            for cab_data in cabinets_to_process:
                locker_id = self._locker_id(cab_data)
                cabinet_id = str(locker_id if locker_id is not None else cab_data["cabinetId"])
                # Ưu tiên cabinetCode từ payload gởi xuống
                cabinet_name = cab_data.get("cabinetCode") or cab_data.get("name") or f"Cab-{cabinet_id[:4]}"
                slave_id = cab_data.get("slaveId", 1)
                layout = [s for s in cab_data.get("lockerLayout", []) if s.get("slotIndex") is not None]

                logger.info(f"Processing Cabinet: {cabinet_name} (lockerId={cabinet_id}, Slave={slave_id}, Slots={len(layout)})")

                details = []
                ok_count = 0
                fail_count = 0
                start_time = time.time()

                for i, slot in enumerate(layout):
                    slot_index = int(slot["slotIndex"])
                    box_id = slot.get("boxId")

                    if test_doors:
                        logger.info(f"[{cabinet_name}] Testing slot {slot_index} ({i+1}/{len(layout)})...")
                        outcome = self._test_slot(slot_index, slave_id, test_timeout)
                    else:
                        outcome = {"testResult": "OK", "hwState": LockerHwState.UNKNOWN.value, "errorCode": None,
                                   "errorMessage": None, "doorClosed": None, "ms": None}

                    if outcome["testResult"] == "OK": ok_count += 1
                    else: fail_count += 1

                    details.append(LockerResultDetail(
                        slotIndex=slot_index, row=slot.get("row", 0), column=slot.get("column", 0),
                        testResult=outcome["testResult"], hwState=outcome["hwState"],
                        responseTimeMs=outcome["ms"], errorCode=outcome["errorCode"],
                        errorMessage=outcome["errorMessage"], boxId=box_id,
                    ))

                    if test_doors:
                        progress_payload = SetupProgressPayload(
                            commandId=command_id, cabinetId=cabinet_id, slotIndex=slot_index,
                            row=slot.get("row", 0), column=slot.get("column", 0),
                            testResult=outcome["testResult"],
                            hwDetail=asdict(HwDetail(
                                servoResponse=outcome["errorCode"] not in ("HW_ERROR", "TIMEOUT", "SERIAL_ERROR"),
                                doorSensor=bool(outcome["doorClosed"]), lockSensor=False,
                                responseTimeMs=outcome["ms"],
                            )),
                            progress=asdict(SetupProgress(
                                tested=i + 1, total=len(layout), okCount=ok_count, failCount=fail_count,
                            )),
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            lockerId=locker_id, boxId=box_id,
                        )
                        self.mqtt.publish(f"{prefix}/setup/progress", progress_payload.to_json(), qos=1)
                        time.sleep(0.1)

                # --- Hoàn tất 1 tủ ---
                duration = int(time.time() - start_time)
                if not layout:
                    status = "FAILED"
                else:
                    status = "COMPLETED" if fail_count == 0 else "FAILED" if ok_count == 0 else "PARTIAL"

                # Lưu state tủ và mapping lockers TRƯỚC khi báo kết quả: backend thấy
                # COMPLETED là có thể gửi lệnh mở ngay, Pi phải tra được boxId rồi.
                saved = self.cabinet_state is not None and status != "FAILED"
                if saved:
                    # Một slave chỉ phục vụ một tủ: gán sang tủ mới thì bỏ tủ cũ.
                    for old in self.cabinet_state.provisioned_cabinets:
                        if old["id"] != cabinet_id and old.get("slaveId", 1) == slave_id:
                            self.cabinet_state.remove_cabinet(old["id"])

                    self.cabinet_state.save_cabinet(
                        cabinet_id=cabinet_id,
                        name=cabinet_name,
                        total_rows=cab_data.get("totalRows", 0),
                        total_columns=cab_data.get("totalColumns", 0),
                        slave_id=slave_id,
                        is_synced=True
                    )
                    # Sau save_cabinet: bảng lockers trong Database có khoá ngoại tới cabinets.
                    self.cabinet_state.save_lockers(cabinet_id, layout)

                final_payload = SetupResultPayload(
                    commandId=command_id, cabinetId=cabinet_id, status=status,
                    summary=asdict(SetupResultSummary(total=len(layout), totalOk=ok_count, totalFail=fail_count, duration=duration)),
                    lockers=[asdict(d) for d in details],
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    lockerId=locker_id,
                    errorMessage="Sơ đồ ô rỗng" if not layout else None,
                )
                self.mqtt.publish(f"{prefix}/setup/result", final_payload.to_json(), qos=1)

                if saved and self._on_setup_complete:
                    self._on_setup_complete(cabinet_id)

        except Exception as e:
            logger.exception(f"Unexpected error during setup: {e}")
        finally:
            self._is_running = False
            logger.info(f"=== {action} FINISHED ===")
