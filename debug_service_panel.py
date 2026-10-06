"""
Chạy thử bảng điều khiển kỹ thuật (/service) trên máy không phải Pi: chân GPIO
giả, nắp trượt mô phỏng (công tắc bật khi chạy tới đầu/cuối hành trình).

    uv run python debug_service_panel.py              # ô xếp theo thứ tự
    uv run python debug_service_panel.py --locker 7   # lấy sơ đồ ô của tủ id 7 trên máy chủ

Mở http://127.0.0.1:8000/service. Không chạm phần cứng thật.
"""
import argparse

import uvicorn
from fastapi import FastAPI

from config.settings import settings
from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import FakePins
from hardware.lid_controller import LidController
from infracstructure.service_panel import ServicePanel, create_service_router

TRAVEL_REVS = 6     # hành trình mô phỏng gốc → cuối


class _Cabinet:
    def __init__(self, locker_id):
        self.all_cabinets = [{"id": locker_id, "name": f"Tủ {locker_id}"}] if locker_id else []


def _simulate_travel(pins: FakePins, lid: LidController, start_revs: float):
    travel = TRAVEL_REVS * lid.steps_per_rev
    pos = [round(start_revs * lid.steps_per_rev)]

    def sync():
        pressed_low = lid.limit_active_low
        pins.levels[lid.home_pin] = (pos[0] > 0) == pressed_low
        pins.levels[lid.end_pin] = (pos[0] < travel) == pressed_low

    group, write = lid._group, lid._group.write
    active = not lid.step_active_low

    def on_write(pin, high):
        write(pin, high)
        if pin == lid.pul_pin and high == active:
            pos[0] += 1 if pins.levels[lid.dir_pin] == lid.open_dir_high else -1
            sync()

    group.write = on_write
    sync()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--locker", type=int, default=None, help="id tủ để lấy sơ đồ ô từ máy chủ")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    pins = FakePins()
    for pin in settings.GPIO_DOOR_PINS:
        pins.levels[pin] = False                     # LOW = cửa đóng
    locker = GpioLockerManager(pins, relay_pins=settings.GPIO_RELAY_PINS, door_pins=settings.GPIO_DOOR_PINS,
                               unlock_ms=settings.UNLOCK_PULSE_MS, settle_ms=300).start()
    lids = {}
    for axis, pul, dir_, home, end, start in ((1, settings.LID_PUL_PIN, settings.LID_DIR_PIN, settings.LID_HOME_PIN, settings.LID_END_PIN, 2.5),
                                             (2, settings.LID2_PUL_PIN, settings.LID2_DIR_PIN, settings.LID2_HOME_PIN, settings.LID2_END_PIN, 0)):
        if axis == 2 and not settings.LID2_ENABLED:
            continue
        lid = LidController(pins, axis=axis, pul_pin=pul, dir_pin=dir_, home_pin=home, end_pin=end,
                            steps_per_sec=settings.LID_STEPS_PER_SEC, pulse_us=settings.LID_PULSE_US,
                            steps_per_rev=settings.LID_STEPS_PER_REV, max_steps=settings.LID_MAX_STEPS)
        if start == 0:
            pins.levels[home] = not lid.limit_active_low   # bắt đầu ở gốc
        lid.start()
        _simulate_travel(pins, lid, start)
        lids[axis] = lid

    app = FastAPI(title="Bảng điều khiển tủ (giả lập)")
    app.include_router(create_service_router(ServicePanel(locker, lids, _Cabinet(args.locker), settings)))
    print(f"Mở http://127.0.0.1:{args.port}/service")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
