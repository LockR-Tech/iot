"""
Kiểm tra phần cứng GPIO khi lắp tủ (HARDWARE_BACKEND=gpio) — thay cho Serial
Monitor của Arduino. Đọc bản đồ chân từ `.env` / `config/settings.py`.

Phải dừng dịch vụ trước vì chỉ một tiến trình được giữ chân GPIO:
    sudo systemctl stop lockr-controller

    uv run python debug_gpio.py pins           # in bản đồ chân
    uv run python debug_gpio.py doors          # theo dõi cảm biến cửa, Ctrl+C để thoát
    uv run python debug_gpio.py open 0         # kích khoá ngăn 0 (1 s) rồi đọc cảm biến
    uv run python debug_gpio.py lid status     # trạng thái công tắc hành trình
    uv run python debug_gpio.py lid home|open|close

Xong thì: sudo systemctl start lockr-controller
"""
import sys
import time

from config.settings import settings

HEADER = {  # BCM → chân vật lý trên header 40 chân
    2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23, 12: 32, 13: 33,
    14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38, 21: 40, 22: 15, 23: 16,
    24: 18, 25: 22, 26: 37, 27: 13,
}


def _pin(bcm: int) -> str:
    return f"GPIO{bcm} (chân {HEADER.get(bcm, '?')})"


def cmd_pins():
    print(f"Relay: kích {'HIGH' if settings.GPIO_RELAY_ACTIVE_HIGH else 'LOW'} · "
          f"cửa đóng khi {'LOW' if settings.GPIO_DOOR_CLOSED_LOW else 'HIGH'}")
    for i, (relay, door) in enumerate(zip(settings.GPIO_RELAY_PINS, settings.GPIO_DOOR_PINS)):
        print(f"  ngăn {i}: IN{i + 1} ← {_pin(relay):18}  cảm biến ← {_pin(door)}")
    print(f"Nắp trượt ({'bật' if settings.LID_ENABLED else 'tắt'}): PUL- {_pin(settings.LID_PUL_PIN)}, "
          f"DIR- {_pin(settings.LID_DIR_PIN)}, gốc {_pin(settings.LID_HOME_PIN)}, cuối {_pin(settings.LID_END_PIN)}")
    print("PUL+ / DIR+ của TB6600 → 3,3 V (chân 1 hoặc 17). Relay DC+ → 5 V (chân 2), DC- → GND (chân 6).")


def _locker():
    from hardware.gpio_locker import GpioLockerManager
    from hardware.gpio_pins import GpiodPins
    return GpioLockerManager(
        GpiodPins(settings.GPIO_CHIP),
        relay_pins=settings.GPIO_RELAY_PINS,
        door_pins=settings.GPIO_DOOR_PINS,
        relay_active_high=settings.GPIO_RELAY_ACTIVE_HIGH,
        door_closed_low=settings.GPIO_DOOR_CLOSED_LOW,
        unlock_ms=settings.UNLOCK_PULSE_MS,
        settle_ms=settings.DOOR_SETTLE_MS,
    )


def cmd_doors():
    locker = _locker()
    locker.on_door_event = lambda slot, event, slave_id=1: print(f"  → {event} ngăn {slot}")
    locker.start()
    print("Đóng/mở tay từng ngăn; Ctrl+C để thoát.")
    try:
        while True:
            states = " ".join(f"{i}:{'Đ' if c else 'M'}" for i, c in enumerate(locker.door_states()))
            print(f"\r{states}   (Đ = đóng, M = mở)", end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        locker.close()


def cmd_open(slot: int):
    locker = _locker().start()
    try:
        print(locker.open_slot(slot, slave_id=locker.slave_id))
    finally:
        locker.close()


def cmd_lid(action: str):
    from hardware.gpio_pins import GpiodPins
    from hardware.lid_controller import LidController
    lid = LidController(
        GpiodPins(settings.GPIO_CHIP),
        pul_pin=settings.LID_PUL_PIN, dir_pin=settings.LID_DIR_PIN,
        home_pin=settings.LID_HOME_PIN, end_pin=settings.LID_END_PIN,
        step_active_low=settings.LID_STEP_ACTIVE_LOW, limit_active_low=settings.LID_LIMIT_ACTIVE_LOW,
        open_dir_high=settings.LID_OPEN_DIR_HIGH, steps_per_sec=settings.LID_STEPS_PER_SEC,
        max_steps=settings.LID_MAX_STEPS,
    ).start()
    try:
        if action == "status":
            print(lid.status())
        elif action in ("home", "open", "close"):
            print(getattr(lid, action)())
        else:
            sys.exit("lid: home | open | close | status")
    except KeyboardInterrupt:
        lid.stop()
    finally:
        lid.shutdown()


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # console Windows mặc định cp1252
    if not argv or argv[0] == "pins":
        cmd_pins()
    elif argv[0] == "doors":
        cmd_doors()
    elif argv[0] == "open" and len(argv) == 2:
        cmd_open(int(argv[1]))
    elif argv[0] == "lid" and len(argv) == 2:
        cmd_lid(argv[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
