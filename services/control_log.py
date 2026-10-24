"""Nhật ký điều khiển tủ — kích relay mở ô và chạy nắp — cho trang admin "Điều khiển tủ".

Ghi ra file trên thẻ nhớ TRƯỚC (fsync), rồi luồng nền mới gửi `cabinet/{lockerId}/event/control`
(QoS 1, chờ broker xác nhận). Mất mạng thì dòng nằm lại trong file, có mạng lại gửi tiếp theo đúng
thứ tự — kể cả sau khi Pi khởi động lại. Backend chống trùng theo `eventId` nên gửi lại là an toàn.
File giữ tối đa `max_pending` dòng (bỏ dòng cũ nhất) để không đầy thẻ khi mất mạng rất lâu.
Hợp đồng: docs/01-overview/mqtt-contract.md § 2.2.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from utils.logger import get_logger

logger = get_logger("ControlLog")

MAX_PENDING = 5000
FLUSH_INTERVAL = 5.0

_LID_STATUS = {"OK": "OK", "STOPPED": "STOPPED", "FAIL": "FAILED"}
_LID_ERRORS = {
    "LIMIT_NOT_REACHED": "chạy hết giới hạn vòng mà không chạm công tắc hành trình",
    "LID_BUSY": "trục đang chạy lệnh khác",
    "LID_NOT_STARTED": "trục chưa khởi động",
}
_PULSE_SOURCE = {"OPEN": "lệnh từ hệ thống", "SERVICE": "bảng kỹ thuật trên Pi", "TEST": "kiểm tra khi gán tủ"}


class ControlLog:
    def __init__(self, mqtt_client, path: str, locker_id: Callable[[], Optional[str]],
                 box_id_for_slot: Optional[Callable[[str, int], Optional[int]]] = None,
                 interval: float = FLUSH_INTERVAL, max_pending: int = MAX_PENDING):
        self.mqtt = mqtt_client
        self.path = path
        self._locker_id = locker_id
        self._box_id_for_slot = box_id_for_slot
        self._interval = interval
        self._max_pending = max_pending
        self._file_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ─── vòng gửi nền ───

    def start(self) -> "ControlLog":
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ControlLog")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        """Gửi ngay (vừa có mạng lại / vừa ghi dòng mới)."""
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            try:
                self.flush()
            except Exception as e:
                logger.error(f"Control log flush failed: {e}")

    # ─── ghi ───

    def record(self, action: str, status: str, *, axis: Optional[int] = None,
               slot_index: Optional[int] = None, revolutions: Optional[float] = None,
               message: Optional[str] = None) -> Optional[dict]:
        locker_id = self._locker_id()
        if not locker_id:
            logger.debug(f"Control log {action} skipped: Pi chưa gán vào tủ nào")
            return None
        box_id = None
        if slot_index is not None and self._box_id_for_slot:
            try:
                box_id = self._box_id_for_slot(str(locker_id), slot_index)
            except Exception:
                box_id = None
        event = {
            "eventId": str(uuid.uuid4()),
            "lockerId": str(locker_id),
            "action": action,
            "status": status,
            "axis": axis,
            "slotIndex": slot_index,
            "boxId": box_id,
            "revolutions": revolutions,
            "message": message,
            "occurredAt": datetime.now(timezone.utc).isoformat(),
        }
        event = {k: v for k, v in event.items() if v is not None}
        try:
            with self._file_lock:
                folder = os.path.dirname(self.path)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                self._trim_locked()
        except OSError as e:
            logger.error(f"Không ghi được nhật ký điều khiển vào {self.path}: {e}")
        self._wake.set()
        return event

    def lid_finished(self, axis: int, result: dict):
        """Gắn vào `LidController.on_finish`: mỗi lượt chạy nắp xong (mọi nguồn lệnh)."""
        action = f"LID_{str(result.get('action') or 'move').upper()}"
        status = _LID_STATUS.get(str(result.get("result")), "FAILED")
        revs = result.get("revolutions")
        parts = []
        if result.get("direction"):
            parts.append("về phía mở" if result["direction"] == "open" else "về phía đóng")
        if status == "OK":
            parts.append(f"xong, trạng thái {result.get('state')}")
        elif status == "STOPPED":
            parts.append("dừng giữa chừng")
        else:
            error = result.get("error")
            parts.append(_LID_ERRORS.get(error, f"lỗi {error}"))
        self.record(action, status, axis=axis, revolutions=revs, message=", ".join(parts))

    def box_pulsed(self, slot_index: int, label: str, unlock_ms: int, result: dict):
        """Gắn vào `GpioLockerManager.on_pulse`: mỗi lần kích relay mở ô."""
        if label == "BLE":
            return  # BleGattService ghi dòng BOX_OPEN_BLE riêng, có tài khoản mở
        ok = result.get("result") == "OK"
        action = "BOX_TEST" if label == "TEST" else "BOX_OPEN"
        message = f"Kích relay ô {slot_index + 1} trong {unlock_ms} ms ({_PULSE_SOURCE.get(label, label)})"
        if not ok:
            message += f" — lỗi {result.get('error')}"
        self.record(action, "OK" if ok else "FAILED", slot_index=slot_index, message=message)

    # ─── gửi ───

    def pending(self) -> list:
        with self._file_lock:
            return self._read_locked()

    def flush(self) -> int:
        """Gửi các dòng chờ theo thứ tự; dừng ở dòng đầu tiên gửi không được. Trả số dòng đã gửi."""
        if not getattr(self.mqtt, "is_connected", True):
            return 0
        with self._send_lock:
            sent = set()
            for event in self.pending():
                payload = {k: v for k, v in event.items() if k != "lockerId"}
                topic = f"cabinet/{event['lockerId']}/event/control"
                if not self.mqtt.publish_and_wait(topic, json.dumps(payload, ensure_ascii=False), qos=1, timeout=5.0):
                    break
                sent.add(event["eventId"])
            if sent:
                with self._file_lock:
                    remaining = [e for e in self._read_locked() if e.get("eventId") not in sent]
                    self._write_locked(remaining)
            return len(sent)

    # ─── file ───

    def _read_locked(self) -> list:
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue                       # dòng hỏng do mất điện giữa lúc ghi
            if isinstance(event, dict) and event.get("eventId") and event.get("lockerId"):
                events.append(event)
        return events

    def _write_locked(self, events: list):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _trim_locked(self):
        events = self._read_locked()
        if len(events) > self._max_pending:
            dropped = len(events) - self._max_pending
            logger.warning(f"Nhật ký điều khiển chờ gửi quá {self._max_pending} dòng — bỏ {dropped} dòng cũ nhất")
            self._write_locked(events[dropped:])
