#!/usr/bin/env python3
"""Điều khiển Servo MG996R trên Raspberry Pi 5 (chân vật lý 12 = GPIO18, chân 6 = GND).

Sơ đồ đấu nối:
  • Mạch giảm áp 5V 3A:
      OUT+  ──▶ Dây ĐỎ của Servo MG996R (VCC 5V)
      OUT-  ──▶ Dây NÂU của Servo MG996R (GND)
      OUT-  ──▶ Chân 6 của Raspberry Pi 5 (GND chung — BẮT BUỘC)
  • Tín hiệu PWM:
      Chân 12 (GPIO18) của Raspberry Pi 5 ──▶ Dây CAM của Servo MG996R

Các lệnh chạy trên Pi qua SSH:
    python3 ~/servo.py loop        # xoay qua lại liên tục (0° -> 90° -> 180° -> 90°...), Ctrl+C để dừng
    python3 ~/servo.py sweep       # quét mượt liên tục từng góc từ 0° <-> 180°, Ctrl+C để dừng
    python3 ~/servo.py spin        # cấp xung quay liên tục 1 chiều (cho servo 360°), Ctrl+C để dừng
    python3 ~/servo.py stop        # dừng xoay và ngắt xung PWM ngay lập tức
    python3 ~/servo.py test        # xoay thử 1 vòng 0° -> 90° -> 180° -> 0° rồi dừng
    python3 ~/servo.py drop        # mở chốt (110°) -> chờ 2s thả hàng -> đóng lại (0°)
    python3 ~/servo.py open        # mở chốt (110°)
    python3 ~/servo.py close       # đóng chốt (0°)
    python3 ~/servo.py 90          # xoay tới góc bất kỳ (0..180)
"""
from __future__ import annotations

import argparse
import glob
import os
import signal
import subprocess
import sys
import time

# Chân BCM mặc định: GPIO18 = Chân vật lý số 12 trên header 40 chân của Raspberry Pi 5
DEFAULT_GPIO_PIN = 18
PWM_FREQ_HZ = 50               # MG996R dùng xung chuẩn 50Hz (chu kỳ 20ms = 20_000µs)
MIN_PULSE_US = 500.0           # 0.5ms  -> 0 độ (hoặc quay hết tốc độ chiều thuận với MG996R 360°)
MAX_PULSE_US = 2500.0          # 2.5ms  -> 180 độ (hoặc quay hết tốc độ chiều ngược với MG996R 360°)

# Góc đóng/mở mặc định cho cơ cấu thả hàng (có thể chỉnh theo cơ khí thực tế)
LOCK_ANGLE = 0.0               # Góc khóa chốt giữ hàng
DROP_ANGLE = 110.0             # Góc mở chốt thả hàng


def angle_to_pulse_us(angle: float) -> float:
    """Đổi góc (0..180 độ) sang độ rộng xung micro-giây (500..2500 µs)."""
    clamped = max(0.0, min(180.0, float(angle)))
    return MIN_PULSE_US + (clamped / 180.0) * (MAX_PULSE_US - MIN_PULSE_US)


def angle_to_duty_percent(angle: float) -> float:
    """Đổi góc (0..180 độ) sang % duty cycle ở tần số 50Hz (chu kỳ 20_000 µs)."""
    pulse_us = angle_to_pulse_us(angle)
    return (pulse_us / 20000.0) * 100.0


def _kill_other_servo_processes() -> None:
    """Giải phóng chân GPIO18 nếu có tiến trình servo.py cũ đang chạy ngầm."""
    ignore_pids = {os.getpid(), os.getppid()}
    try:
        out = subprocess.check_output(["pgrep", "-f", "python.*servo.py"], text=True)
        for line in out.splitlines():
            pid = int(line.strip())
            if pid not in ignore_pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(0.15)
    except Exception:
        pass


def _open_lgpio_chip(pin: int):
    import lgpio
    for chip_num in (4, 0, 1, 2, 3):
        h = None
        try:
            h = lgpio.gpiochip_open(chip_num)
            lgpio.gpio_claim_output(h, pin)
            return lgpio, h, chip_num
        except Exception:
            if h is not None:
                try:
                    lgpio.gpiochip_close(h)
                except Exception:
                    pass
    return None, None, None


def stop_servo(pin: int = DEFAULT_GPIO_PIN) -> None:
    """Dừng tiến trình xoay ngầm (nếu có) và ngắt hoàn toàn xung PWM trên chân GPIO."""
    _kill_other_servo_processes()
    lgpio, handle, _ = _open_lgpio_chip(pin)
    if handle is not None:
        try:
            lgpio.tx_pwm(handle, pin, PWM_FREQ_HZ, 0)
            lgpio.gpio_write(handle, pin, 0)
        finally:
            lgpio.gpio_free(handle, pin)
            lgpio.gpiochip_close(handle)
        print(f"[SERVO] Đã dừng và ngắt xung PWM trên GPIO{pin} (chân 12).")


def loop_servo(pin: int = DEFAULT_GPIO_PIN, step_s: float = 0.8, count: int = 0) -> None:
    """Xoay qua lại liên tục 0° -> 90° -> 180° -> 90° (bấm Ctrl+C để dừng)."""
    _kill_other_servo_processes()
    lgpio, handle, chip_num = _open_lgpio_chip(pin)
    if handle is None:
        raise RuntimeError("Không mở được gpiochip qua lgpio trên Pi 5.")

    running = True

    def _on_sig(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    angles = (0.0, 90.0, 180.0, 90.0)
    print(f"[SERVO] Đang xoay qua lại liên tục trên GPIO{pin} (chip={chip_num}). Bấm Ctrl+C để dừng...")
    cycle = 0
    try:
        while running:
            for angle in angles:
                if not running:
                    break
                duty = angle_to_duty_percent(angle)
                lgpio.tx_pwm(handle, pin, PWM_FREQ_HZ, duty)
                print(
                    f"[SERVO] Vòng {cycle + 1}: góc {angle:.0f}° "
                    f"(xung {angle_to_pulse_us(angle):.0f}µs, duty {duty:.2f}%)",
                    flush=True,
                )
                t_end = time.monotonic() + step_s
                while running and time.monotonic() < t_end:
                    time.sleep(0.05)
            cycle += 1
            if count > 0 and cycle >= count:
                break
    finally:
        lgpio.tx_pwm(handle, pin, PWM_FREQ_HZ, 0)
        lgpio.gpio_write(handle, pin, 0)
        lgpio.gpio_free(handle, pin)
        lgpio.gpiochip_close(handle)
        print(f"\n[SERVO] Đã dừng xoay và ngắt xung PWM trên GPIO{pin}.")


def sweep_servo(pin: int = DEFAULT_GPIO_PIN, step_deg: float = 5.0, delay_s: float = 0.03) -> None:
    """Quét mượt liên tục từng góc nhỏ từ 0° -> 180° -> 0° (bấm Ctrl+C để dừng)."""
    _kill_other_servo_processes()
    lgpio, handle, chip_num = _open_lgpio_chip(pin)
    if handle is None:
        raise RuntimeError("Không mở được gpiochip qua lgpio trên Pi 5.")

    running = True

    def _on_sig(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    print(f"[SERVO] Đang quét mượt liên tục 0° <-> 180° trên GPIO{pin} (chip={chip_num}). Bấm Ctrl+C để dừng...")
    angle = 0.0
    direction = 1.0
    try:
        while running:
            duty = angle_to_duty_percent(angle)
            lgpio.tx_pwm(handle, pin, PWM_FREQ_HZ, duty)
            time.sleep(delay_s)
            angle += direction * step_deg
            if angle >= 180.0:
                angle = 180.0
                direction = -1.0
            elif angle <= 0.0:
                angle = 0.0
                direction = 1.0
    finally:
        lgpio.tx_pwm(handle, pin, PWM_FREQ_HZ, 0)
        lgpio.gpio_write(handle, pin, 0)
        lgpio.gpio_free(handle, pin)
        lgpio.gpiochip_close(handle)
        print(f"\n[SERVO] Đã dừng quét mượt và ngắt xung PWM trên GPIO{pin}.")


def spin_servo(pin: int = DEFAULT_GPIO_PIN, angle: float = 0.0) -> None:
    """Cấp xung PWM liên tục không ngắt (dùng cho servo 360° quay tròn liên tục hoặc giữ lực cố định)."""
    _kill_other_servo_processes()
    lgpio, handle, chip_num = _open_lgpio_chip(pin)
    if handle is None:
        raise RuntimeError("Không mở được gpiochip qua lgpio trên Pi 5.")

    running = True

    def _on_sig(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    duty = angle_to_duty_percent(angle)
    lgpio.tx_pwm(handle, pin, PWM_FREQ_HZ, duty)
    print(
        f"[SERVO] Đang cấp xung liên tục trên GPIO{pin} (chip={chip_num}, góc/tốc độ={angle:.0f}°, "
        f"xung={angle_to_pulse_us(angle):.0f}µs, duty={duty:.2f}%). Bấm Ctrl+C để dừng...",
        flush=True,
    )
    try:
        while running:
            time.sleep(0.1)
    finally:
        lgpio.tx_pwm(handle, pin, PWM_FREQ_HZ, 0)
        lgpio.gpio_write(handle, pin, 0)
        lgpio.gpio_free(handle, pin)
        lgpio.gpiochip_close(handle)
        print(f"\n[SERVO] Đã dừng và ngắt xung PWM trên GPIO{pin}.")


def _move_with_lgpio(pin: int, angle: float, hold_s: float) -> bool:
    """Cách 1: Dùng lgpio (cài sẵn trên Raspberry Pi OS Bookworm / Trixie cho Pi 5)."""
    try:
        lgpio, handle, _ = _open_lgpio_chip(pin)
    except ImportError:
        return False
    if handle is None:
        return False

    try:
        duty = angle_to_duty_percent(angle)
        lgpio.tx_pwm(handle, pin, PWM_FREQ_HZ, duty)
        time.sleep(hold_s)
        # Ngắt xung PWM sau khi đã quay tới góc để MG996R không bị rung/nóng
        lgpio.tx_pwm(handle, pin, PWM_FREQ_HZ, 0)
        lgpio.gpio_write(handle, pin, 0)
        return True
    finally:
        lgpio.gpio_free(handle, pin)
        lgpio.gpiochip_close(handle)


def _move_with_gpiozero(pin: int, angle: float, hold_s: float) -> bool:
    """Cách 2: Dùng gpiozero.AngularServo (có sẵn trên Raspberry Pi OS)."""
    try:
        from gpiozero import AngularServo
    except ImportError:
        return False

    servo = AngularServo(
        pin,
        min_angle=0,
        max_angle=180,
        min_pulse_width=MIN_PULSE_US / 1_000_000.0,
        max_pulse_width=MAX_PULSE_US / 1_000_000.0,
        frame_width=1.0 / PWM_FREQ_HZ,
    )
    try:
        servo.angle = max(0.0, min(180.0, float(angle)))
        time.sleep(hold_s)
        servo.detach()  # Ngắt xung để chống rung servo
        return True
    finally:
        servo.close()


def _move_with_gpiod(pin: int, angle: float, hold_s: float) -> bool:
    """Cách 3: Phát xung PWM chuẩn 50Hz trực tiếp qua libgpiod trên chip RP1 của Pi 5."""
    try:
        import gpiod
        from gpiod.line import Direction, Value
    except ImportError:
        return False

    chip_path = None
    for path in sorted(glob.glob("/dev/gpiochip*")):
        try:
            with gpiod.Chip(path) as chip:
                if chip.get_info().label in ("pinctrl-rp1", "pinctrl-bcm2711", "pinctrl-bcm2835"):
                    chip_path = path
                    break
        except OSError:
            continue
    if not chip_path:
        return False

    pulse_s = angle_to_pulse_us(angle) / 1_000_000.0
    period_s = 1.0 / PWM_FREQ_HZ
    cycles = max(15, int(hold_s * PWM_FREQ_HZ))

    with gpiod.request_lines(
        chip_path,
        consumer="lockr-servo",
        config={pin: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)},
    ) as req:
        for _ in range(cycles):
            t0 = time.perf_counter()
            req.set_value(pin, Value.ACTIVE)
            while time.perf_counter() - t0 < pulse_s:
                pass
            req.set_value(pin, Value.INACTIVE)
            remaining = period_s - (time.perf_counter() - t0)
            if remaining > 0.001:
                time.sleep(remaining - 0.0005)
            while time.perf_counter() - t0 < period_s:
                pass
        req.set_value(pin, Value.INACTIVE)
    return True


def set_servo_angle(angle: float, pin: int = DEFAULT_GPIO_PIN, hold_s: float = 0.7) -> None:
    """Quay servo MG996R tới góc `angle` (0..180°), giữ xung `hold_s` giây rồi ngắt xung."""
    _kill_other_servo_processes()
    print(f"[SERVO] GPIO{pin} (chân 12) -> góc {angle:.0f}° "
          f"(xung {angle_to_pulse_us(angle):.0f}µs, duty {angle_to_duty_percent(angle):.2f}%)")

    for backend_name, fn in (
        ("lgpio", _move_with_lgpio),
        ("gpiozero", _move_with_gpiozero),
        ("gpiod", _move_with_gpiod),
    ):
        try:
            if fn(pin, angle, hold_s):
                print(f"[SERVO] Đã xoay xong tới {angle:.0f}° (backend={backend_name})")
                return
        except Exception as exc:
            print(f"[SERVO] Thử {backend_name} lỗi: {exc}", file=sys.stderr)

    raise RuntimeError(
        "Không tìm thấy thư viện GPIO phù hợp trên Pi 5. "
        "Hãy chạy bằng `/usr/bin/python3 servo.py` hoặc cài: sudo apt install -y python3-lgpio python3-gpiozero"
    )


def drop_payload(
    pin: int = DEFAULT_GPIO_PIN,
    lock_angle: float = LOCK_ANGLE,
    drop_angle: float = DROP_ANGLE,
    open_wait_s: float = 2.0,
) -> None:
    """Chu trình thả hàng: Mở chốt -> đợi hàng rơi -> đóng chốt lại."""
    print(f"[SERVO] Bắt đầu chu trình thả hàng: mở {drop_angle:.0f}° trong {open_wait_s}s rồi đóng {lock_angle:.0f}°")
    set_servo_angle(drop_angle, pin=pin, hold_s=0.8)
    time.sleep(open_wait_s)
    set_servo_angle(lock_angle, pin=pin, hold_s=0.8)
    print("[SERVO] Hoàn tất chu trình thả hàng!")


def main() -> int:
    parser = argparse.ArgumentParser(description="Điều khiển Servo MG996R trên Raspberry Pi 5 (GPIO18 - Chân 12)")
    parser.add_argument(
        "command",
        nargs="?",
        default="loop",
        help="Lệnh: 'loop'/'xoay' (xoay qua lại liên tục), 'sweep' (quét mượt liên tục), 'spin' (cấp xung liên tục), 'test', 'drop', 'open', 'close', 'stop', hoặc góc (0..180)",
    )
    parser.add_argument("--pin", type=int, default=DEFAULT_GPIO_PIN, help="Chân GPIO theo chuẩn BCM (mặc định: 18 = chân vật lý 12)")
    parser.add_argument("--hold", type=float, default=0.8, help="Thời gian giữ mỗi góc (giây, mặc định 0.8s)")
    parser.add_argument("--wait", type=float, default=2.0, help="Thời gian chờ hàng rơi khi chạy lệnh 'drop' (giây)")
    parser.add_argument("--count", type=int, default=0, help="Số vòng lặp khi chạy 'loop' (0 = xoay mãi tới khi Ctrl+C)")
    args = parser.parse_args()

    cmd = args.command.strip().lower()
    if cmd in ("loop", "xoay"):
        loop_servo(pin=args.pin, step_s=args.hold, count=args.count)
    elif cmd == "sweep":
        sweep_servo(pin=args.pin)
    elif cmd in ("spin", "360"):
        spin_servo(pin=args.pin, angle=0.0)
    elif cmd in ("spin-rev", "360-rev"):
        spin_servo(pin=args.pin, angle=180.0)
    elif cmd == "stop":
        stop_servo(pin=args.pin)
    elif cmd == "test":
        for angle in (0, 90, 180, 0):
            set_servo_angle(angle, pin=args.pin, hold_s=args.hold)
            time.sleep(0.5)
    elif cmd == "drop":
        drop_payload(pin=args.pin, open_wait_s=args.wait)
    elif cmd == "open":
        set_servo_angle(DROP_ANGLE, pin=args.pin, hold_s=args.hold)
    elif cmd == "close":
        set_servo_angle(LOCK_ANGLE, pin=args.pin, hold_s=args.hold)
    else:
        try:
            angle = float(cmd)
        except ValueError:
            print(
                f"Lệnh không hợp lệ: {args.command}. Dùng góc 0..180 hoặc: loop | xoay | sweep | spin | test | drop | open | close | stop",
                file=sys.stderr,
            )
            return 2
        set_servo_angle(angle, pin=args.pin, hold_s=args.hold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
