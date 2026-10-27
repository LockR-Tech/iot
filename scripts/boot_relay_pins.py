#!/usr/bin/env python3
"""Cập nhật hai chỗ giữ relay ở mức thấp ngoài code khi admin đổi chân relay (lệnh hw-config).

    sudo -n python3 scripts/boot_relay_pins.py 5,6,13,19,26,22,23

1. `/boot/firmware/config.txt`: dòng `gpio=<chân>=op,dl` — firmware ép relay về thấp ngay lúc bật nguồn,
   trước khi lockr-controller chạy (GPIO0–8 mặc định kéo lên ⇒ relay có thể nhả khoá lúc khởi động).
   Có hiệu lực từ lần khởi động máy kế tiếp.
2. `/etc/systemd/system/lockr-controller.service`: `ExecStopPost=… pinctrl set <chân> op dl` — ngắt
   relay khi dịch vụ dừng. Đổi xong chạy `systemctl daemon-reload`.

Sao lưu file trước khi sửa (`<file>.bak-<thời gian>`). In một dòng JSON kết quả. Cần quyền root.
"""
import json
import re
import shutil
import subprocess
import sys
import time

CONFIG_TXT = ("/boot/firmware/config.txt", "/boot/config.txt")
UNIT = "/etc/systemd/system/lockr-controller.service"
GPIO_LINE = re.compile(r"^gpio=[0-9,]+=op,dl\s*$", re.M)
STOP_POST = re.compile(r"^(ExecStopPost=.*pinctrl set )[0-9,]+( op dl.*)$", re.M)


def parse_pins(raw: str) -> str:
    pins = [int(p) for p in raw.split(",") if p.strip()]
    if not pins or any(not 2 <= p <= 27 for p in pins) or len(set(pins)) != len(pins):
        raise ValueError(f"danh sách chân relay không hợp lệ: {raw}")
    return ",".join(str(p) for p in pins)


def rewrite(path: str, new_text: str, old_text: str) -> bool:
    if new_text == old_text:
        return False
    shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: boot_relay_pins.py 5,6,13"}))
        return 2
    try:
        pins = parse_pins(sys.argv[1])
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    result = {"ok": True, "pins": pins, "configTxt": None, "unit": None}

    for path in CONFIG_TXT:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            continue
        line = f"gpio={pins}=op,dl"
        new = GPIO_LINE.sub(line, text) if GPIO_LINE.search(text) else text.rstrip("\n") + f"\n{line}\n"
        result["configTxt"] = {"path": path, "changed": rewrite(path, new, text)}
        break

    try:
        with open(UNIT, encoding="utf-8") as f:
            text = f.read()
        new = STOP_POST.sub(lambda m: f"{m.group(1)}{pins}{m.group(2)}", text)
        changed = rewrite(UNIT, new, text)
        if changed:
            subprocess.run(["systemctl", "daemon-reload"], check=False)
        result["unit"] = {"path": UNIT, "changed": changed, "hasStopPost": bool(STOP_POST.search(text))}
    except FileNotFoundError:
        pass

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
