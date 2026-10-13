"""
Điều khiển khoá + đọc cảm biến cửa bằng GPIO của Raspberry Pi — theo sơ đồ
nhà cung cấp tủ: `IN1…IN7` của module relay và dây tín hiệu khoá nối thẳng vào
Pi, không qua Arduino/RS485 (xem docs/03-hardware/cabinet-wiring-spec.md).

`GpioLockerManager` có cùng giao diện với `SerialManager` (`open_slot`,
`close_slot`, `test_slot`, `scan_slaves`, `on_door_event`, `close`…) và trả
cùng dạng kết quả, nên `LockerService`, `SetupHandler`, `DiscoveryService`
không phải sửa. Chọn bằng `HARDWARE_BACKEND=gpio` trong `.env`.

Hành vi giữ nguyên firmware Arduino (`locker_controller.ino`):
- mở ngăn: relay ON `unlock_ms` → OFF → chờ `settle_ms` cho lò xo bật cửa →
  đọc cảm biến; mỗi lúc chỉ một khoá có điện (nguồn 12 V chỉ phải gánh 1 khoá);
- cảm biến: kéo lên, **mức LOW = cửa đóng**, lọc nhiễu `debounce_ms`, báo
  `DOOR_OPENED` / `DOOR_CLOSED` khi trạng thái đổi.
"""
import threading
import time
from typing import Callable, List, Optional

from hardware.gpio_pins import PinGroup, PinIO
from utils.logger import get_logger

logger = get_logger("GpioLocker")


class GpioLockerManager:
    def __init__(
        self,
        pins: PinIO,
        relay_pins: List[int],
        door_pins: List[int],
        relay_active_high: bool = True,
        door_closed_low: bool = True,
        slave_id: int = 1,
        unlock_ms: int = 1000,
        settle_ms: int = 2000,
        close_wait_ms: int = 1000,
        poll_ms: int = 100,
        debounce_ms: int = 200,
        on_door_event: Optional[Callable] = None,
    ):
        if len(relay_pins) != len(door_pins):
            raise ValueError(f"Số chân relay ({len(relay_pins)}) khác số chân cảm biến ({len(door_pins)})")
        overlap = set(relay_pins) & set(door_pins)
        if overlap:
            raise ValueError(f"Chân vừa là relay vừa là cảm biến: {sorted(overlap)}")

        self._pins = pins
        self.relay_pins = list(relay_pins)
        self.door_pins = list(door_pins)
        self.relay_active_high = relay_active_high
        self.door_closed_low = door_closed_low
        self.slave_id = slave_id
        self.unlock_ms = unlock_ms
        self.settle_ms = settle_ms
        self.close_wait_ms = close_wait_ms
        self.poll_ms = poll_ms
        self.debounce_ms = debounce_ms

        self._on_door_event = on_door_event
        self._on_reconnect: Optional[Callable] = None
        self._group: Optional[PinGroup] = None
        self._op_lock = threading.Lock()        # một khoá có điện tại một thời điểm
        self._stop = threading.Event()
        self._poller: Optional[threading.Thread] = None

        n = len(self.door_pins)
        self._door_closed = [False] * n         # trạng thái đã báo
        self._raw_closed = [False] * n          # lần đọc gần nhất
        self._raw_changed_at = [0.0] * n

    # ─── cùng giao diện SerialManager ───

    @property
    def num_slots(self) -> int:
        return len(self.relay_pins)

    @property
    def on_door_event(self) -> Optional[Callable]:
        return self._on_door_event

    @on_door_event.setter
    def on_door_event(self, callback: Callable):
        self._on_door_event = callback

    @property
    def on_reconnect(self) -> Optional[Callable]:
        return self._on_reconnect

    @on_reconnect.setter
    def on_reconnect(self, callback: Callable):
        # GPIO không mất kết nối như cổng serial — giữ callback cho đủ giao diện.
        self._on_reconnect = callback

    def start(self) -> "GpioLockerManager":
        relay_off = not self.relay_active_high
        self._group = self._pins.claim(
            outputs={pin: relay_off for pin in self.relay_pins},
            inputs=self.door_pins,
            pull_up=True,
        )
        now = time.monotonic()
        for i in range(self.num_slots):
            closed = self._read_door_closed(i)
            self._door_closed[i] = self._raw_closed[i] = closed
            self._raw_changed_at[i] = now
        logger.info(
            f"GPIO locker ready: {self.num_slots} slots, relay {self.relay_pins} "
            f"(active {'HIGH' if self.relay_active_high else 'LOW'}), door {self.door_pins}"
        )
        for i in range(self.num_slots):
            logger.info(f"  Slot {i}: relay GPIO{self.relay_pins[i]}, door GPIO{self.door_pins[i]} "
                        f"= {'CLOSED' if self._door_closed[i] else 'OPEN'}")
        self._stop.clear()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True, name="GpioDoorPoller")
        self._poller.start()
        return self

    def is_connected(self) -> bool:
        return self._group is not None

    def scan_slaves(self, range_start: int = 1, range_end: int = 1) -> list:
        if self._group is None or not (range_start <= self.slave_id <= range_end):
            return []
        return [{"slaveId": self.slave_id, "availableSlots": self.num_slots}]

    def open_slot(self, slot_index: int, slave_id: int = 1, timeout: int = 5) -> dict:
        return self._pulse(slot_index, slave_id, "OPEN")

    def test_slot(self, slot_index: int, slave_id: int = 1, timeout: int = 10) -> dict:
        # Firmware: lệnh T và O cùng một xung relay, chỉ khác mục đích gọi.
        return self._pulse(slot_index, slave_id, "TEST")

    def pulse_slot(self, slot_index: int, unlock_ms: int) -> dict:
        """Mở ngăn với thời gian kích riêng — bảng điều khiển kỹ thuật."""
        return self._pulse(slot_index, self.slave_id, "SERVICE", unlock_ms=unlock_ms)

    def close_slot(self, slot_index: int, slave_id: int = 1, timeout: int = 5) -> dict:
        error = self._validate(slot_index, slave_id)
        if error:
            return error
        start = time.monotonic()
        with self._op_lock:
            self._set_relay(slot_index, False)
            time.sleep(self.close_wait_ms / 1000)
        return self._result(slot_index, start)

    def door_closed(self, slot_index: int) -> bool:
        return self._door_closed[slot_index]

    def door_states(self) -> List[bool]:
        """True = cửa đóng, theo thứ tự slot."""
        return list(self._door_closed)

    def relay_states(self) -> List[bool]:
        """True = relay đang kích, theo thứ tự slot."""
        if self._group is None:
            return [False] * self.num_slots
        return [self._group.read(pin) == self.relay_active_high for pin in self.relay_pins]

    def close(self):
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=2)
        if self._group:
            try:
                for i in range(self.num_slots):
                    self._set_relay(i, False)
            finally:
                self._group.release()
                self._group = None
        logger.info("GPIO locker closed")

    # ─── nội bộ ───

    def _validate(self, slot_index: int, slave_id: int) -> Optional[dict]:
        if self._group is None:
            return {"slave": slave_id, "slot": slot_index, "result": "FAIL", "error": "GPIO_NOT_STARTED"}
        if slave_id != self.slave_id:
            return {"slave": slave_id, "slot": slot_index, "result": "FAIL", "error": "UNKNOWN_SLAVE"}
        if not isinstance(slot_index, int) or not 0 <= slot_index < self.num_slots:
            return {"slave": slave_id, "slot": slot_index, "result": "FAIL", "error": "INVALID_SLOT"}
        return None

    def _pulse(self, slot_index: int, slave_id: int, label: str, unlock_ms: Optional[int] = None) -> dict:
        error = self._validate(slot_index, slave_id)
        if error:
            return error
        unlock_ms = self.unlock_ms if unlock_ms is None else unlock_ms
        start = time.monotonic()
        with self._op_lock:
            logger.info(f"{label} slot {slot_index}: relay GPIO{self.relay_pins[slot_index]} ON {unlock_ms} ms")
            try:
                self._set_relay(slot_index, True)
                time.sleep(unlock_ms / 1000)
            finally:
                # Luôn ngắt cuộn khoá, kể cả khi có lỗi giữa chừng.
                self._set_relay(slot_index, False)
            time.sleep(self.settle_ms / 1000)
        return self._result(slot_index, start)

    def _result(self, slot_index: int, start: float) -> dict:
        return {
            "slave": self.slave_id,
            "slot": slot_index,
            "result": "OK",
            "gpio": self.relay_pins[slot_index],
            "ms": int((time.monotonic() - start) * 1000),
            "door": self._read_door_closed(slot_index),
        }

    def _set_relay(self, slot_index: int, on: bool):
        self._group.write(self.relay_pins[slot_index], on == self.relay_active_high)

    def _read_door_closed(self, slot_index: int) -> bool:
        level_high = self._group.read(self.door_pins[slot_index])
        return (not level_high) if self.door_closed_low else level_high

    def _poll_loop(self):
        while not self._stop.wait(self.poll_ms / 1000):
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f"Door poll error: {e}")

    def poll_once(self, now: Optional[float] = None):
        """Đọc mọi cảm biến một lượt, báo sự kiện khi trạng thái ổn định ≥ debounce_ms."""
        now = time.monotonic() if now is None else now
        for i in range(self.num_slots):
            closed = self._read_door_closed(i)
            if closed != self._raw_closed[i]:
                self._raw_closed[i] = closed
                self._raw_changed_at[i] = now
                continue
            stable_ms = (now - self._raw_changed_at[i]) * 1000
            if closed != self._door_closed[i] and stable_ms >= self.debounce_ms:
                self._door_closed[i] = closed
                event = "DOOR_CLOSED" if closed else "DOOR_OPENED"
                logger.info(f"{event}: slot {i}")
                if self._on_door_event:
                    try:
                        self._on_door_event(i, event, slave_id=self.slave_id)
                    except Exception as e:
                        logger.error(f"Door event handler failed for slot {i}: {e}")
