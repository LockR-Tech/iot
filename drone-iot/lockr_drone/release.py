"""Nhả kiện hàng bằng servo MG996R giữ chốt dưới bụng drone (đấu nối: xem servo.py).

Chu trình một lần nhả: quay tới góc MỞ chốt → giữ `hold_s` giây cho kiện rơi vào ô → quay về góc KHOÁ.
Không có cảm biến xác nhận kiện đã rời móc: "nhả xong" nghĩa là servo đã quay mở rồi đóng lại không lỗi.

Lỗi trước khi chốt mở (không có thư viện GPIO, không mở được chân…) thì thử lại tới `attempts` lần rồi báo lỗi.
Chốt đã mở mà không đóng lại được thì kiện ĐÃ rơi: vẫn tính là nhả xong, kèm cảnh báo để KTV kiểm tra chốt.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("release")


@dataclass(frozen=True)
class ReleaseResult:
    ok: bool
    attempts: int
    error: Optional[str] = None       # lỗi khiến không nhả được (ok = False)
    warning: Optional[str] = None     # nhả được nhưng chốt không đóng lại (ok = True)
    duration_s: float = 0.0           # thời gian thật cả chu trình (kể cả các lần thử lỗi)
    opened_at: Optional[str] = None   # lúc chốt mở thật (ISO UTC) — kiện rơi từ thời điểm này


class ServoReleaser:
    def __init__(self, move: Callable[[float], None], open_angle: float = 180.0, lock_angle: float = 0.0,
                 hold_s: float = 5.0, attempts: int = 2, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        """`move(angle)`: quay servo tới góc rồi trả về, lỗi thì ném ngoại lệ (servo.set_servo_angle)."""
        self.move = move
        self.open_angle = float(open_angle)
        self.lock_angle = float(lock_angle)
        self.hold_s = max(0.0, float(hold_s))
        self.attempts = max(1, int(attempts))
        self._sleep = sleep
        self._clock = clock

    def release(self) -> ReleaseResult:
        started = self._clock()
        error = None
        for attempt in range(1, self.attempts + 1):
            try:
                self.move(self.open_angle)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.warning("Servo không mở được chốt (lần %d/%d): %s", attempt, self.attempts, error)
                continue
            opened_at = datetime.now(timezone.utc).isoformat()
            log.info("Servo mở chốt %.0f° — giữ %.1f giây cho kiện rơi", self.open_angle, self.hold_s)
            self._sleep(self.hold_s)
            try:
                self.move(self.lock_angle)
            except Exception as exc:
                warning = f"Chốt không đóng lại được: {type(exc).__name__}: {exc}"
                log.warning("Đã nhả hàng nhưng %s", warning)
                return ReleaseResult(True, attempt, warning=warning, duration_s=self._elapsed(started),
                                     opened_at=opened_at)
            log.info("Servo đóng chốt %.0f° — nhả hàng xong", self.lock_angle)
            return ReleaseResult(True, attempt, duration_s=self._elapsed(started), opened_at=opened_at)
        return ReleaseResult(False, self.attempts, error=error, duration_s=self._elapsed(started))

    def _elapsed(self, started: float) -> float:
        return round(self._clock() - started, 2)

    def details(self, result: ReleaseResult, gpio_pin: int, gpio_backend: Optional[str]) -> dict:
        """Số liệu thật của lần nhả gửi kèm báo cáo lên backend (nhật ký drone ↔ tủ hiển thị lại)."""
        details = {
            "servoOpenAngle": self.open_angle,
            "servoLockAngle": self.lock_angle,
            "servoHoldSeconds": self.hold_s,
            "releaseAttempts": result.attempts,
            "releaseDurationSeconds": result.duration_s,
            "servoGpioPin": gpio_pin,
            "gpioBackend": gpio_backend,
        }
        if result.opened_at:
            details["releasedAt"] = result.opened_at
        if result.warning:
            details["warning"] = result.warning
        return details


def gpio_backend_available() -> Optional[str]:
    """Tên thư viện GPIO servo.py dùng được trong Python đang chạy, hoặc None (venv thiếu lgpio/gpiozero/gpiod)."""
    for name in ("lgpio", "gpiozero", "gpiod"):
        try:
            __import__(name)
            return name
        except ImportError:
            continue
    return None
