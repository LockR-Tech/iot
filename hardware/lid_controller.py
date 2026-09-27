"""
Nắp trượt: động cơ bước Nema 17 qua driver TB6600 + 2 công tắc hành trình,
Pi phát xung trực tiếp bằng GPIO (sơ đồ nhà cung cấp tủ).

Nối dây (xem docs/03-hardware/controller-wiring-guide.md):
- `PUL+`, `DIR+` của TB6600 → **3,3 V** của Pi (không phải 5 V — GPIO Pi chỉ
  lên 3,3 V, nối 5 V thì opto trong TB6600 không tắt hẳn);
  `PUL-`, `DIR-` → GPIO. Kiểu nối này đảo mức: GPIO LOW = opto dẫn
  ⇒ `step_active_low=True`.
- Công tắc hành trình: một chân → GPIO (kéo lên), chân kia → GND. Công tắc
  thường mở (NO) ⇒ bấm = LOW ⇒ `limit_active_low=True`.

Chưa có lệnh MQTT nào cho nắp (gap F1.06) — điều khiển qua API cục bộ
`/hardware/lid/*` của `config_api.py`.
"""
import threading
import time
from enum import Enum
from typing import Optional

from hardware.gpio_pins import PinGroup, PinIO
from utils.logger import get_logger

logger = get_logger("Lid")


class LidState(str, Enum):
    UNKNOWN = "UNKNOWN"      # chưa về gốc lần nào
    CLOSED = "CLOSED"        # đang chạm công tắc gốc
    OPEN = "OPEN"            # đang chạm công tắc cuối
    MOVING = "MOVING"
    STOPPED = "STOPPED"      # bị dừng giữa đường
    FAULT = "FAULT"          # chạy hết số bước tối đa mà không chạm công tắc


class LidController:
    def __init__(
        self,
        pins: PinIO,
        pul_pin: int,
        dir_pin: int,
        home_pin: int,
        end_pin: int,
        step_active_low: bool = True,
        limit_active_low: bool = True,
        open_dir_high: bool = True,
        steps_per_sec: int = 800,
        start_steps_per_sec: int = 200,
        ramp_steps: int = 200,
        max_steps: int = 20000,
        pulse_us: int = 20,
    ):
        pins_used = [pul_pin, dir_pin, home_pin, end_pin]
        if len(set(pins_used)) != len(pins_used):
            raise ValueError(f"Chân nắp trượt bị trùng: {pins_used}")
        if steps_per_sec <= 0 or start_steps_per_sec <= 0 or max_steps <= 0:
            raise ValueError("steps_per_sec, start_steps_per_sec, max_steps phải > 0")

        self._pins = pins
        self.pul_pin, self.dir_pin = pul_pin, dir_pin
        self.home_pin, self.end_pin = home_pin, end_pin
        self.step_active_low = step_active_low
        self.limit_active_low = limit_active_low
        self.open_dir_high = open_dir_high
        self.steps_per_sec = steps_per_sec
        self.start_steps_per_sec = min(start_steps_per_sec, steps_per_sec)
        self.ramp_steps = max(ramp_steps, 0)
        self.max_steps = max_steps
        self.pulse_us = pulse_us

        self._group: Optional[PinGroup] = None
        self._move_lock = threading.Lock()
        self._abort = threading.Event()
        self.state = LidState.UNKNOWN
        self.last_error: Optional[str] = None
        self.last_steps = 0

    def start(self) -> "LidController":
        idle = self.step_active_low          # không có xung: opto tắt
        self._group = self._pins.claim(
            outputs={self.pul_pin: idle, self.dir_pin: self.open_dir_high},
            inputs=[self.home_pin, self.end_pin],
            pull_up=True,
        )
        if self.at_home():
            self.state = LidState.CLOSED
        elif self.at_end():
            self.state = LidState.OPEN
        logger.info(
            f"Lid ready: PUL GPIO{self.pul_pin}, DIR GPIO{self.dir_pin}, "
            f"home GPIO{self.home_pin}, end GPIO{self.end_pin}, state {self.state.value}"
        )
        return self

    # ─── trạng thái ───

    def _limit_hit(self, pin: int) -> bool:
        level_high = self._group.read(pin)
        return (not level_high) if self.limit_active_low else level_high

    def at_home(self) -> bool:
        return self._limit_hit(self.home_pin)

    def at_end(self) -> bool:
        return self._limit_hit(self.end_pin)

    def status(self) -> dict:
        started = self._group is not None
        return {
            "state": self.state.value,
            "busy": self._move_lock.locked(),
            "homeLimit": self.at_home() if started else None,
            "endLimit": self.at_end() if started else None,
            "lastSteps": self.last_steps,
            "lastError": self.last_error,
        }

    # ─── lệnh ───

    def open(self) -> dict:
        return self._move(toward_end=True)

    def close(self) -> dict:
        return self._move(toward_end=False)

    def home(self) -> dict:
        """Về gốc = đóng nắp tới công tắc gốc; gọi sau khi bật máy."""
        return self._move(toward_end=False)

    def stop(self) -> dict:
        self._abort.set()
        return {"result": "OK", "state": self.state.value}

    def shutdown(self):
        self._abort.set()
        with self._move_lock:
            if self._group:
                self._group.write(self.pul_pin, self.step_active_low)
                self._group.release()
                self._group = None

    # ─── chạy động cơ ───

    def _move(self, toward_end: bool) -> dict:
        if self._group is None:
            return {"result": "FAIL", "error": "LID_NOT_STARTED"}
        if not self._move_lock.acquire(blocking=False):
            return {"result": "FAIL", "error": "LID_BUSY", "state": self.state.value}
        target = LidState.OPEN if toward_end else LidState.CLOSED
        limit_reached = self.at_end if toward_end else self.at_home
        try:
            self._abort.clear()
            self.last_error = None
            if limit_reached():
                self.state, self.last_steps = target, 0
                return {"result": "OK", "state": self.state.value, "steps": 0}

            self.state = LidState.MOVING
            dir_high = self.open_dir_high if toward_end else not self.open_dir_high
            self._group.write(self.dir_pin, dir_high)
            time.sleep(0.001)                    # TB6600 cần DIR ổn định trước xung đầu

            steps = 0
            while steps < self.max_steps:
                if limit_reached():
                    self.state, self.last_steps = target, steps
                    logger.info(f"Lid {target.value} after {steps} steps")
                    return {"result": "OK", "state": self.state.value, "steps": steps}
                if self._abort.is_set():
                    self.state, self.last_steps = LidState.STOPPED, steps
                    logger.warning(f"Lid stopped after {steps} steps")
                    return {"result": "STOPPED", "state": self.state.value, "steps": steps}
                self._step(self._interval(steps))
                steps += 1

            self.state, self.last_steps = LidState.FAULT, steps
            self.last_error = "LIMIT_NOT_REACHED"
            logger.error(f"Lid FAULT: {steps} steps toward {target.value} without hitting the limit switch")
            return {"result": "FAIL", "error": self.last_error, "state": self.state.value, "steps": steps}
        finally:
            self._group.write(self.pul_pin, self.step_active_low)
            self._move_lock.release()

    def _interval(self, step_index: int) -> float:
        """Chu kỳ một bước (giây), tăng tốc tuyến tính trong `ramp_steps` bước đầu."""
        if self.ramp_steps and step_index < self.ramp_steps:
            rate = self.start_steps_per_sec + (self.steps_per_sec - self.start_steps_per_sec) * step_index / self.ramp_steps
        else:
            rate = self.steps_per_sec
        return 1.0 / rate

    def _step(self, interval: float):
        active = not self.step_active_low
        t0 = time.perf_counter()
        self._group.write(self.pul_pin, active)
        pulse_end = t0 + self.pulse_us / 1_000_000
        while time.perf_counter() < pulse_end:
            pass
        self._group.write(self.pul_pin, not active)
        remaining = interval - (time.perf_counter() - t0)
        if remaining > 0:
            time.sleep(remaining)
