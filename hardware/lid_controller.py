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
from typing import Callable, Optional

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

# Kiểu tăng tốc: "linear" = tăng đều từ start_rps lên rps trong ramp_steps bước, xung pulse_us;
# "chaydan" = đúng lệnh `chaydan` kỹ thuật tủ CAB-TU01 đã kiểm chứng (03/11/2026, chạy bằng pinctrl):
#   100 bước `dh; sleep 0.004; dl; sleep 0.004` → 100 bước `… sleep 0.001 …` → còn lại `dh; dl`.
# Mỗi lệnh pinctrl mất ≈ 1,4 ms (đo trên Pi 04/11) ⇒ chu kỳ ≈ 10,8 ms → 4,8 ms → 1/rps (≈ 2,8 ms ở 1,8 vòng/s),
# xung chiếm nửa chu kỳ. Nấc cuối dùng `rps` để chỉnh được.
PROFILES = ("linear", "chaydan")
CHAYDAN_STAGES = ((100, 0.0108), (100, 0.0048))   # (số bước, chu kỳ một bước giây)


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
        position_travel: bool = False,
        profile: str = "linear",
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
        if profile not in PROFILES:
            raise ValueError(f"profile phải là một trong {PROFILES}")
        self.profile = profile

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
        self._toward_end = True                  # chiều của lệnh đang chạy (jog không tự ghi trong motion)
        # Gọi khi mỗi lượt chạy kết thúc: on_finish(axis, last_result) — nhật ký điều khiển tủ.
        self.on_finish: Optional[Callable[[int, dict], None]] = None
        # Tủ chưa có công tắc hành trình: tin vị trí đếm bước. Ray dài đúng `max_steps` (gốc = đóng, cuối = mở);
        # Mở/Đóng hết chỉ chạy phần còn thiếu tới đầu ray rồi coi là tới — không đẩy nắp vào điểm chặn.
        self.position_travel = position_travel
        # Gọi mỗi khi vị trí đổi (lưu ra file để còn nhớ sau khi khởi động lại): on_position(axis, steps|None).
        self.on_position: Optional[Callable[[int, Optional[int]], None]] = None

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
            "profile": self.profile,
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
            "positionTravel": self.position_travel,
            "stepsPerSec": self.steps_per_sec,
            "effectivePulseUs": round(self._pulse_seconds(1.0 / self.steps_per_sec) * 1_000_000, 1),
            "pins": {"pul": self.pul_pin, "dir": self.dir_pin, "home": self.home_pin, "end": self.end_pin},
            "config": self.config(),
        }

    def tune(self, **changes) -> dict:
        """Đổi tốc độ/xung/giới hạn lúc chạy. Không đổi được khi động cơ đang quay."""
        if self.busy:
            raise RuntimeError("LID_BUSY")
        unknown = set(changes) - set(TUNING_LIMITS) - {"open_dir_high", "limit_active_low", "profile"}
        if unknown:
            raise ValueError(f"Không có thông số {sorted(unknown)}")
        changes = {k: v for k, v in changes.items() if v is not None}
        if "profile" in changes and changes["profile"] not in PROFILES:
            raise ValueError(f"profile phải là một trong {PROFILES} (đang là {changes['profile']})")
        for key, value in changes.items():
            if key == "profile":
                continue
            low, high = TUNING_LIMITS.get(key, (None, None))
            if low is not None and not low <= float(value) <= high:
                raise ValueError(f"{key} phải trong khoảng {low}–{high} (đang là {value})")
        merged = {**self.config(), **changes}
        if float(merged["start_rps"]) > float(merged["rps"]):
            merged["start_rps"] = merged["rps"]

        spr = int(merged["steps_per_rev"])
        position_revs = None if self.position is None else self.position / self.steps_per_rev
        self.steps_per_rev = spr
        self.steps_per_sec = max(1, round(float(merged["rps"]) * spr))
        self.start_steps_per_sec = max(1, min(round(float(merged["start_rps"]) * spr), self.steps_per_sec))
        self.ramp_steps = int(merged["ramp_steps"])
        self.pulse_us = int(merged["pulse_us"])
        self.max_steps = max(1, round(float(merged["max_revs"]) * spr))
        self.open_dir_high = bool(merged["open_dir_high"])
        self.limit_active_low = bool(merged["limit_active_low"])
        self.profile = str(merged.get("profile") or "linear")
        if self._group is not None:
            self._group.write(self.dir_pin, self.open_dir_high)
        if position_revs is not None:
            # Vị trí giữ theo vòng khi đổi số bước/vòng; đổi chiều dài ray thì kẹp trong ray mới.
            self.position = round(position_revs * spr)
            if self.position_travel:
                self.position = max(0, min(self.position, self.max_steps))
                if self.state not in (LidState.MOVING, LidState.UNKNOWN):
                    self.state = self._state_for_position()
        logger.info(f"Lid {self.axis} tuned: {self.config()}")
        return self.config()

    # ─── lệnh ───

    def open(self) -> dict:
        return self._travel(toward_end=True, motion="open")

    def close(self) -> dict:
        return self._travel(toward_end=False, motion="close")

    def remaining_steps(self, toward_end: bool) -> Optional[int]:
        """Số bước còn lại tới đầu ray theo vị trí đếm bước; None nếu không dùng / chưa biết vị trí."""
        if not self.position_travel or self.position is None:
            return None
        return max(0, self.max_steps - self.position) if toward_end else max(0, self.position)

    def _travel(self, toward_end: bool, motion: str) -> dict:
        remaining = self.remaining_steps(toward_end)
        if remaining is None:
            return self._move(toward_end=toward_end, motion=motion)
        result = self._move(toward_end=toward_end, motion=motion, step_budget=remaining, reach_on_budget=True)
        if result.get("result") == "OK":
            result["byPosition"] = True
        return result

    def set_position(self, revolutions: Optional[float]) -> dict:
        """Hiệu chỉnh: báo cho Pi bàn trượt đang cách gốc bao nhiêu vòng (None = không biết).
        0 = ở gốc (đóng), bằng giới hạn vòng = ở cuối ray (mở)."""
        if self.busy:
            raise RuntimeError("LID_BUSY")
        if revolutions is None:
            self.position, self.state = None, LidState.UNKNOWN
        else:
            revs = float(revolutions)
            max_revs = self.max_steps / self.steps_per_rev
            if not 0 <= revs <= max_revs:
                raise ValueError(f"Vị trí phải trong khoảng 0–{max_revs:g} vòng")
            self.position = round(revs * self.steps_per_rev)
            self.state = self._state_for_position()
        self.last_error = None
        logger.info(f"Lid {self.axis} position set: {self.position} steps ({self.state.value})")
        self._notify_position()
        return self.status()

    def _state_for_position(self) -> "LidState":
        if self.position is None:
            return LidState.UNKNOWN
        if self.position <= 0:
            return LidState.CLOSED
        if self.position >= self.max_steps:
            return LidState.OPEN
        return LidState.STOPPED

    def _notify_position(self):
        if self.on_position:
            try:
                self.on_position(self.axis, self.position)
            except Exception as e:
                logger.error(f"Lid {self.axis} on_position failed: {e}")

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

    def _move(self, toward_end: bool, motion: str, step_budget: Optional[int] = None,
              reach_on_budget: bool = False) -> dict:
        """`reach_on_budget`: chạy đủ `step_budget` bước nghĩa là đã tới đầu ray (vị trí đếm bước)."""
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
            self._toward_end = toward_end
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
                if reach_on_budget:
                    self._reached(target)
                    logger.info(f"Lid {self.axis} {target.value} after {steps} steps (theo vị trí đếm bước)")
                else:
                    self.state = LidState.STOPPED
                return self._finish({"result": "OK", "state": self.state.value, "steps": steps}, steps)
            if self.position_travel:
                # Không có công tắc: chạy đủ cả chiều dài ray ⇒ chắc chắn đã ở đầu ray — lấy làm mốc vị trí.
                if toward_end:
                    self.position = self.max_steps
                self._reached(target)
                logger.info(f"Lid {self.axis} {target.value}: chạy hết {steps} bước (cả ray), lấy làm mốc vị trí")
                return self._finish({"result": "OK", "state": self.state.value, "steps": steps,
                                     "byStepCount": True}, steps)
            self.state = LidState.FAULT
            self.last_error = "LIMIT_NOT_REACHED"
            logger.error(f"Lid {self.axis} FAULT: {steps} steps toward {target.value} without hitting the limit switch")
            return self._finish({"result": "FAIL", "state": self.state.value, "error": "LIMIT_NOT_REACHED",
                                 "steps": steps}, steps)
        finally:
            self._group.write(self.pul_pin, self.step_active_low)
            self.motion = None
            self._move_lock.release()

    def assume_reached(self, toward_end: bool) -> None:
        """Tủ chưa có công tắc hành trình: lượt chạy hết số vòng giới hạn được chấp nhận là tới đích
        (LID_REQUIRE_LIMIT_SWITCH tắt). Ghi nhận OPEN/CLOSED để lệnh lặp không đẩy nắp thêm một lượt."""
        if self.busy:
            return
        self.last_error = None
        if toward_end:
            self.position = self.max_steps   # hết ray: làm mốc cho lần chạy sau
        self._reached(LidState.OPEN if toward_end else LidState.CLOSED)
        self._notify_position()

    def _reached(self, target: LidState):
        self.state = target
        if target == LidState.CLOSED:
            self.position = 0
        elif self.position is not None:
            self.travel_steps = self.position

    def _finish(self, result: dict, steps: int) -> dict:
        if self.position_travel and self.position is not None:
            # Không có công tắc: chạy quá đầu ray thì bàn trượt chỉ đứng ở đầu ray (motor trượt bước).
            self.position = max(0, min(self.position, self.max_steps))
            if self.state == LidState.STOPPED:
                self.state = self._state_for_position()
        self._notify_position()
        self.last_steps = steps
        self.last_result = {**result, "action": self.motion,
                            "direction": "open" if self._toward_end else "close",
                            "revolutions": round(steps / self.steps_per_rev, 2)}
        if self.on_finish:
            try:
                self.on_finish(self.axis, dict(self.last_result))
            except Exception as e:
                logger.error(f"Lid {self.axis} on_finish failed: {e}")
        return result

    def _interval(self, step_index: int) -> float:
        """Chu kỳ một bước (giây): "chaydan" = các nấc của lệnh chaydan rồi chạy đều ở `rps`;
        "linear" = tăng tốc tuyến tính trong `ramp_steps` bước đầu."""
        if self.profile == "chaydan":
            done = 0
            for count, period in CHAYDAN_STAGES:
                if step_index < done + count:
                    return period
                done += count
            return 1.0 / self.steps_per_sec
        if self.ramp_steps and step_index < self.ramp_steps:
            rate = self.start_steps_per_sec + (self.steps_per_sec - self.start_steps_per_sec) * step_index / self.ramp_steps
        else:
            rate = self.steps_per_sec
        return 1.0 / rate

    def _pulse_seconds(self, interval: float) -> float:
        """Độ rộng xung thật: "chaydan" = nửa chu kỳ (như pinctrl dh … dl); "linear" = `pulse_us`,
        nhưng không quá nửa chu kỳ để còn thời gian nghỉ."""
        if self.profile == "chaydan":
            return interval / 2
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
