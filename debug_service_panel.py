"""
Chạy thử bảng điều khiển kỹ thuật (/service) trên máy không phải Pi: chân GPIO
giả, nắp trượt mô phỏng (công tắc bật khi chạy tới đầu/cuối hành trình).

    uv run python debug_service_panel.py              # ô xếp theo thứ tự
    uv run python debug_service_panel.py --locker 7   # lấy sơ đồ ô của tủ id 7 trên máy chủ

Mở http://127.0.0.1:8000/service. Không chạm phần cứng thật.
"""
import argparse
import json
import subprocess
import sys
import threading

import uvicorn
from fastapi import FastAPI

from config.settings import settings
from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import FakePins
from hardware.lid_controller import LidController
from infracstructure.service_panel import ServicePanel, create_service_router
from infracstructure.wifi import WifiManager

TRAVEL_REVS = 6     # hành trình mô phỏng gốc → cuối


class _Cabinet:
    def __init__(self, locker_id):
        self.all_cabinets = [{"id": locker_id, "name": f"Tủ {locker_id}"}] if locker_id else []


class _FakeNmcli:
    """NetworkManager giả cho WifiManager: vài mạng đã lưu + quét được; mạng tên có chữ "sai" thì
    chuyển sang sẽ thất bại và tự quay về (để xem đường lui hoạt động)."""

    def __init__(self):
        self.nets = {"Mang_chinh": [80, True], "Phong_lab": [5, False]}    # tên → [ưu tiên, đang dùng]
        self.air = [("Mang_chinh", 78, "WPA2"), ("Mang_khach", 64, "WPA2"), ("Phong_lab", 41, "WPA2"),
                    ("Wifi_sai_mat_khau", 35, "WPA2"), ("Cafe Free", 20, "")]

    def __call__(self, cmd, **kw):
        out = ""
        args = cmd[2:] if cmd[:2] == ["sudo", "-n"] else cmd
        if args[0] == "systemd-run":
            target, previous, result_file = args[-3:]
            threading.Timer(4, self._finish_switch, (target, previous, result_file)).start()
            for net in self.nets.values():
                net[1] = False                          # đang chuyển: tạm mất mạng
        elif args[-2:] == ["connection", "show"]:
            out = "".join(f"{n.replace(':', chr(92) + ':')}:802-11-wireless:yes:{p}:{'yes' if a else 'no'}\n"
                          for n, (p, a) in self.nets.items()) + "Wired:802-3-ethernet:yes:0:no\n"
        elif args[:2] == ["-g", "802-11-wireless.ssid"]:
            out = args[-1] + "\n"
        elif "IP4.ADDRESS" in args:
            out = "IP4.ADDRESS[1]:192.168.1.50/24\n" if any(a for _, a in self.nets.values()) else ""
        elif "wifi" in args and "list" in args:
            out = "".join(f"{s}:{sig}:{sec}\n" for s, sig, sec in self.air)
        elif args[1:3] == ["connection", "add"]:
            self.nets[args[args.index("con-name") + 1]] = [int(args[args.index("connection.autoconnect-priority") + 1]), False]
        elif args[1:3] == ["connection", "modify"]:
            if "connection.autoconnect-priority" in args:
                self.nets[args[3]][0] = int(args[args.index("connection.autoconnect-priority") + 1])
        elif args[1:3] == ["connection", "delete"]:
            self.nets.pop(args[3], None)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def _finish_switch(self, target, previous, result_file):
        ok = "sai" not in target
        self.nets.get(target if ok else previous, [0, False])[1] = True
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"target": target, "previous": previous, "ok": ok, "reverted": not ok,
                       "error": None if ok else "Secrets were required, but not provided (giả lập)",
                       "at": "giả lập"}, f, ensure_ascii=False)


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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # console Windows mặc định cp1252
    parser = argparse.ArgumentParser()
    parser.add_argument("--locker", type=int, default=None, help="id tủ để lấy sơ đồ ô từ máy chủ")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--wifi-result", default="wifi_switch_result.sim.json", help="file kết quả chuyển Wi-Fi giả lập")
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
    app.include_router(create_service_router(ServicePanel(
        locker, lids, _Cabinet(args.locker), settings,
        restart=lambda: print("(giả lập) Đã lưu cấu hình — trên Pi thật bộ điều khiển sẽ khởi động lại"),
        wifi=WifiManager(run=_FakeNmcli(), result_file=args.wifi_result))))
    print(f"Mở http://127.0.0.1:{args.port}/service")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
