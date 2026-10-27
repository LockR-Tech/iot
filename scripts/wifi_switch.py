#!/usr/bin/env python3
"""Chuyển Wi-Fi có đường lui — chạy tách rời (systemd-run, quyền root) từ bảng /service.

    python3 scripts/wifi_switch.py <mạng mới> <mạng đang dùng> <file kết quả> [giây chờ]

Bật mạng mới; trong thời hạn phải có IPv4 trên wlan0 và ping được gateway. Không được ⇒ bật lại
mạng cũ. Ghi kết quả JSON ra file để bảng hiện lại khi người dùng vào được Pi.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

IFACE = "wlan0"


def run(cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def online(iface: str = IFACE) -> bool:
    """Có IPv4 và ping được gateway mặc định qua wlan0."""
    route = run(["ip", "-4", "route", "show", "default", "dev", iface], timeout=10).stdout.split()
    if "via" not in route:
        return False
    gateway = route[route.index("via") + 1]
    return run(["ping", "-c", "2", "-W", "3", "-I", iface, gateway], timeout=15).returncode == 0


def switch(target: str, previous: str, wait: int = 45, sleep=time.sleep) -> dict:
    up = run(["nmcli", "--wait", str(wait), "connection", "up", target], timeout=wait + 15)
    ok = up.returncode == 0
    if ok:
        for _ in range(5):                         # DHCP/gateway đôi khi chậm vài giây sau khi "up"
            if online():
                break
            sleep(3)
        else:
            ok = False
    result = {"target": target, "previous": previous, "ok": ok, "reverted": False,
              "error": None if ok else (up.stderr or up.stdout or "không có mạng sau khi kết nối").strip()[:300]}
    if not ok:
        back = run(["nmcli", "--wait", "30", "connection", "up", previous], timeout=45)
        result["reverted"] = back.returncode == 0
        if not result["reverted"]:
            result["error"] += f" | quay về {previous} cũng lỗi: {(back.stderr or '').strip()[:200]}"
    result["at"] = datetime.now(timezone.utc).isoformat()
    return result


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: wifi_switch.py <target> <previous> <result_file> [wait]")
        return 2
    target, previous, result_file = sys.argv[1:4]
    wait = int(sys.argv[4]) if len(sys.argv) > 4 else 45
    result = switch(target, previous, wait)
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
