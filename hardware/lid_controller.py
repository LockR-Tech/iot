"""
Nắp trượt: động cơ bước Nema 17 qua driver TB6600 + 2 công tắc hành trình,
Pi phát xung trực tiếp bằng GPIO (sơ đồ nhà cung cấp tủ). Tủ có thể có nhiều
trục (mỗi trục một driver, một cặp công tắc) — mỗi trục là một `LidController`.

Nối dây (xem docs/03-hardware/controller-wiring-guide.md):
- `PUL+`, `DIR+` của TB6600 → **3,3 V** của Pi (không phải 5 V — GPIO Pi chỉ
  lên 3,3 V, nối 5 V thì opto trong TB6600 không tắt hẳn);
  `PUL-`, `DIR-` → GPIO. Kiểu nối này đảo mức: GPIO LOW = opto dẫn
  ⇒ `step_active_low=True`.
- Công tắc hành trình: một chân → GPIO (kéo lên), chân kia → GND. Công tắc
  thường mở (NO) ⇒ bấm = LOW ⇒ `limit_active_low=True`.

Opto của TB6600 nối 3,3 V cần xung dài (thử trên tủ thật: 20 µs động cơ chỉ
rung, 1000 µs chạy đủ vòng). Xung không bao giờ dài quá nửa chu kỳ một bước.

Chưa có lệnh MQTT nào cho nắp (gap F1.06) — điều khiển qua API cục bộ
`/hardware/lid/*` và bảng điều khiển kỹ thuật `/service` của `config_api.py`.
"""
import threading
import time
from enum import Enum
from typing import Optional

from hardware.gpio_pins import PinGroup, PinIO
from utils.logger import get_logger

logger = get_logger("Lid")

# Giới hạn khi chỉnh tốc độ lúc chạy (bảng điều khiển kỹ thuật).
TUNING_LIMITS = {
    "rps": (0.05, 5.0),             # vòng/giây khi chạy đều
    "start_rps": (0.05, 5.0),       # vòng/giây lúc bắt đầu tăng tốc
    "ramp_steps": (0, 20000),       # số bước tăng tốc
    "pulse_us": (2, 5000),          # độ rộng xung PUL
    "max_revs": (0.5, 500.0),       # chặn chạy mãi khi hỏng công tắc
    "steps_per_rev": (200, 25600),  # theo công tắc DIP của TB6600
}


class LidState(str, Enum):
    UNKNOWN = "UNKNOWN"      # chưa về gốc lần nào
    CLOSED = "CLOSED"        # đang chạm công tắc gốc
    OPEN = "OPEN"            # đang chạm công tắc cuối
    MOVING = "MOVING"
    STOPPED = "STOPPED"      # dừng giữa đường (bấm dừng hoặc chạy đủ số vòng)
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
        steps_per_rev: int = 1600,
        axis: int = 1,
    ):
        pins_used = [pul_pin, dir_pin, home_pin, end_pin]
        if len(set(pins_used)) != len(pins_used):
            raise ValueError(f"Chân nắp trượt bị trùng: {pins_used}")
        if steps_per_sec <= 0 or start_steps_per_sec <= 0 or max_steps <= 0 or steps_per_rev <= 0:
            raise ValueError("steps_per_sec, start_steps_per_sec, max_steps, steps_per_rev phải > 0")

        self._pins = pins
        self.axis = axis
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
        self.steps_per_rev = steps_per_rev

        self._group: Optional[PinGroup] = None
        self._move_lock = threading.Lock()
        self._abort = threading.Event()
        self.state = LidState.UNKNOWN
        self.last_error: Optional[str] = None
        self.last_steps = 0
        self.last_result: Optional[dict] = None
        self.position: Optional[int] = None      # số bước tính từ công tắc gốc; None = chưa biết
        self.travel_steps: Optional[int] = None  # gốc → cuối, đo được khi chạy hết một lượt
        self.motion: Optional[str] = None        # lệnh đang chạy: open/close/home/jog
        self.progress = 0                        # số bước đã chạy của lệnh hiện tại
        self.target_steps: Optional[int] = None  # số bước dự định (lệnh jog)

    def start(self) -> "LidController":
        idle = self.step_active_low          # không có xung: opto tắt
        self._group = self._pins.claim(
            outputs={self.pul_pin: idle, self.dir_pin: self.open_dir_high},
            inputs=[self.home_pin, self.end_pin],
            pull_up=True,
        )
        if self.at_home():
            self.state, self.position = LidState.CLOSED, 0
        elif self.at_end():
            self.state = LidState.OPEN
        logger.info(
            f"Lid {self.axis} ready: PUL GPIO{self.pul_pin}, DIR GPIO{self.dir_pin}, "
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

    @property
    def busy(self) -> bool:
        return self._move_lock.locked()

    def config(self) -> dict:
        """Thông số chỉnh được, theo đơn vị người dùng (vòng)."""
        return {
            "rps": round(self.steps_per_sec / self.steps_per_rev, 3),
            "start_rps": round(self.start_steps_per_sec / self.steps_per_rev, 3),
            "ramp_steps": self.ramp_steps,
            "pulse_us": self.pulse_us,
            "max_revs": round(self.max_steps / self.steps_per_rev, 2),
            "steps_per_rev": self.steps_per_rev,
            "open_dir_high": self.open_dir_high,
            "limit_active_low": self.limit_active_low,
        }

    def status(self) -> dict:
        started = self._group is not None
        revs = (lambda steps: None if steps is None else round(steps / self.steps_per_rev, 2))
        return {
            "axis": self.axis,
            "state": self.state.value,
            "busy": self.busy,
            "homeLimit": self.at_home() if started else None,
            "endLimit": self.at_end() if started else None,
            "lastSteps": self.last_steps,
            "lastError": self.last_error,
            "lastResult": self.last_result,
            "motion": self.motion,
            "progressSteps": self.progress,
            "targetSteps": self.target_steps,
            "positionSteps": self.position,
            "positionRevs": revs(self.position),
            "travelRevs": revs(self.travel_steps),
            "stepsPerSec": self.steps_per_sec,
            "effectivePulseUs": round(self._pulse_seconds(1.0 / self.steps_per_sec) * 1_000_000, 1),
            "pins": {"pul": self.pul_pin, "dir": self.dir_pin, "home": self.home_pin, "end": self.end_pin},
            "config": self.config(),
        }

    def tune(self, **changes) -> dict:
        """Đổi tốc độ/xung/giới hạn lúc chạy. Không đổi được khi động cơ đang quay."""
        if self.busy:
            raise RuntimeError("LID_BUSY")
        unknown = set(changes) - set(TUNING_LIMITS) - {"open_dir_high", "limit_active_low"}
        if unknown:
            raise ValueError(f"Không có thông số {sorted(unknown)}")
        changes = {k: v for k, v in changes.items() if v is not None}
        for key, value in changes.items():
            low, high = TUNING_LIMITS.get(key, (None, None))
            if low is not None and not low <= float(value) <= high:
                raise ValueError(f"{key} phải trong khoảng {low}–{high} (đang là {value})")
        merged = {**self.config(), **changes}
        if float(merged["start_rps"]) > float(merged["rps"]):
            merged["start_rps"] = merged["rps"]

        spr = int(merged["steps_per_rev"])
        self.steps_per_rev = spr
        self.steps_per_sec = max(1, round(float(merged["rps"]) * spr))
        self.start_steps_per_sec = max(1, min(round(float(merged["start_rps"]) * spr), self.steps_per_sec))
        self.ramp_steps = int(merged["ramp_steps"])
        self.pulse_us = int(merged["pulse_us"])
        self.max_steps = max(1, round(float(merged["max_revs"]) * spr))
        self.open_dir_high = bool(merged["open_dir_high"])
        self.limit_active_low = bool(merged["limit_active_low"])
        if self._group is not None:
            self._group.write(self.dir_pin, self.open_dir_high)
        logger.info(f"Lid {self.axis} tuned: {self.config()}")
        return self.config()

    # ─── lệnh ───

    def open(self) -> dict:
        return self._move(toward_end=True, motion="open")

    def close(self) -> dict:
        return self._move(toward_end=False, motion="close")

    def home(self) -> dict:
        """Về gốc = đóng nắp tới công tắc gốc; gọi sau khi bật máy."""
        return self._move(toward_end=False, motion="home")

    def jog(self, revolutions: float, toward_end: bool) -> dict:
        """Chạy đúng `revolutions` vòng; vẫn dừng khi chạm công tắc ở phía đang chạy."""
        steps = round(abs(float(revolutions)) * self.steps_per_rev)
        if steps < 1:
            return {"result": "FAIL", "error": "INVALID_REVOLUTIONS", "state": self.state.value}
        if steps > self.max_steps:
            return {"result": "FAIL", "error": "OVER_MAX_REVS", "state": self.state.value,
                    "maxRevs": self.config()["max_revs"]}
        return self._move(toward_end=toward_end, motion="jog", step_budget=steps)

    def run_async(self, action: str, **kwargs) -> dict:
        """Chạy lệnh trong luồng nền để API trả lời ngay; theo dõi qua `status()`."""
        commands = {"open": self.open, "close": self.close, "home": self.home, "jog": self.jog}
        if action not in commands:
            raise ValueError(f"action phải là một trong {sorted(commands)}")
        if self._group is None:
            return {"result": "FAIL", "error": "LID_NOT_STARTED"}
        if self.busy:
            return {"result": "FAIL", "error": "LID_BUSY", "state": self.state.value}
        threading.Thread(target=commands[action], kwargs=kwargs, daemon=True,
                         name=f"Lid{self.axis}-{action}").start()
        return {"result": "STARTED", "action": action}

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

    def _move(self, toward_end: bool, motion: str, step_budget: Optional[int] = None) -> dict:
        if self._group is None:
            return {"result": "FAIL", "error": "LID_NOT_STARTED"}
        if not self._move_lock.acquire(blocking=False):
            return {"result": "FAIL", "error": "LID_BUSY", "state": self.state.value}
        target = LidState.OPEN if toward_end else LidState.CLOSED
        limit_reached = self.at_end if toward_end else self.at_home
        limit_steps = self.max_steps if step_budget is None else step_budget
        steps = 0
        try:
            self._abort.clear()
            self.last_error = None
            self.motion, self.progress, self.target_steps = motion, 0, step_budget
            if limit_reached():
                self._reached(target)
                return self._finish({"result": "OK", "state": self.state.value, "steps": 0}, 0)

            self.state = LidState.MOVING
            dir_high = self.open_dir_high if toward_end else not self.open_dir_high
            self._group.write(self.dir_pin, dir_high)
            time.sleep(0.001)                    # TB6600 cần DIR ổn định trước xung đầu

            while steps < limit_steps:
                if limit_reached():
                    self._reached(target)
                    logger.info(f"Lid {self.axis} {target.value} after {steps} steps")
                    return self._finish({"result": "OK", "state": self.state.value, "steps": steps}, steps)
                if self._abort.is_set():
                    self.state = LidState.STOPPED
                    logger.warning(f"Lid {self.axis} stopped after {steps} steps")
                    return self._finish({"result": "STOPPED", "state": self.state.value, "steps": steps}, steps)
                self._step(self._interval(steps))
                steps += 1
                self.progress = steps
                if self.position is not None:
                    self.position += 1 if toward_end else -1

            if step_budget is not None:
                self.state = LidState.STOPPED
                return self._finish({"result": "OK", "state": self.state.value, "steps": steps}, steps)
            self.state = LidState.FAULT
            self.last_error = "LIMIT_NOT_REACHED"
            logger.error(f"Lid {self.axis} FAULT: {steps} steps toward {target.value} without hitting the limit switch")
            return self._finish({"result": "FAIL", "error": self.last_error, "state": self.state.value,
                                 "steps": steps}, steps)
        finally:
            self._group.write(self.pul_pin, self.step_active_low)
            self.motion = None
            self._move_lock.release()

    def _reached(self, target: LidState):
        self.state = target
        if target == LidState.CLOSED:
            self.position = 0
        elif self.position is not None:
            self.travel_steps = self.position

    def _finish(self, result: dict, steps: int) -> dict:
        self.last_steps = steps
        self.last_result = {**result, "action": self.motion,
                            "revolutions": round(steps / self.steps_per_rev, 2)}
        return result

    def _interval(self, step_index: int) -> float:
        """Chu kỳ một bước (giây), tăng tốc tuyến tính trong `ramp_steps` bước đầu."""
        if self.ramp_steps and step_index < self.ramp_steps:
            rate = self.start_steps_per_sec + (self.steps_per_sec - self.start_steps_per_sec) * step_index / self.ramp_steps
        else:
            rate = self.steps_per_sec
        return 1.0 / rate

    def _pulse_seconds(self, interval: float) -> float:
        """Độ rộng xung thật: `pulse_us`, nhưng không quá nửa chu kỳ để còn thời gian nghỉ."""
        return min(self.pulse_us / 1_000_000, interval / 2)

    def _step(self, interval: float):
        active = not self.step_active_low
        t0 = time.perf_counter()
        self._group.write(self.pul_pin, active)
        pulse_end = t0 + self._pulse_seconds(interval)
        while time.perf_counter() < pulse_end:
            pass
        self._group.write(self.pul_pin, not active)
        # time.sleep trễ thêm cỡ 0,1 ms — ngủ phần lớn rồi chờ bận phần cuối để tốc độ đúng.
        deadline = t0 + interval
        remaining = deadline - time.perf_counter()
        if remaining > 0.002:
            time.sleep(remaining - 0.001)
        while time.perf_counter() < deadline:
            pass
