"""Wi-Fi của Pi trên bảng /service (tab Cấu hình → Wi-Fi), qua NetworkManager (`nmcli`).

- Lưu mạng mới **không chuyển ngay**: Pi tự vào mạng đó khi mạng hiện tại không có (đem tủ sang chỗ
  mới) — cách an toàn nhất.
- "Chuyển ngay" chạy `scripts/wifi_switch.py` tách rời bằng `systemd-run` (vẫn chạy khi phiên SSH của
  người đang bấm bị cắt): thử mạng mới, không có IP / không tới được gateway trong thời hạn ⇒ tự quay
  về mạng cũ; kết quả ghi ra file để bảng hiện lại khi vào được Pi.

Mọi lệnh truyền tham số dạng danh sách (không qua shell); tên mạng/mật khẩu được kiểm tra trước.
"""
import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

from utils.logger import get_logger

logger = get_logger("Wifi")

IFACE = "wlan0"
SWITCH_SCRIPT = str(Path(__file__).parent.parent / "scripts" / "wifi_switch.py")
RESULT_FILE = str(Path(__file__).parent.parent / "config" / "wifi_switch_result.json")
_PRINTABLE = re.compile(r"^[^\x00-\x1f\x7f]+$")


def _split_terse(line: str) -> list:
    """Tách một dòng `nmcli -t` theo dấu `:` không bị escape (`\\:`)."""
    parts, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def check_ssid(ssid) -> str:
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or not _PRINTABLE.match(ssid):
        raise ValueError("Tên Wi-Fi (SSID) phải từ 1 tới 32 byte, không có ký tự điều khiển")
    return ssid


def check_password(password) -> str:
    if password in (None, ""):
        return ""
    if not isinstance(password, str) or not 8 <= len(password) <= 63 or not _PRINTABLE.match(password):
        raise ValueError("Mật khẩu Wi-Fi WPA phải từ 8 tới 63 ký tự")
    return password


def check_priority(priority) -> int:
    if not isinstance(priority, int) or isinstance(priority, bool) or not -999 <= priority <= 999:
        raise ValueError("Độ ưu tiên phải là số nguyên từ -999 tới 999 (cao hơn được chọn trước)")
    return priority


class WifiManager:
    def __init__(self, run: Optional[Callable] = None, iface: str = IFACE,
                 switch_script: str = SWITCH_SCRIPT, result_file: str = RESULT_FILE):
        self._run = run or subprocess.run
        self.iface = iface
        self.switch_script = switch_script
        self.result_file = result_file

    def _nmcli(self, *args, sudo: bool = False, timeout: int = 30) -> str:
        cmd = (["sudo", "-n"] if sudo else []) + ["nmcli", *args]
        try:
            done = self._run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise RuntimeError("Máy này không có NetworkManager (nmcli)")
        except subprocess.TimeoutExpired:
            raise RuntimeError("nmcli không trả lời kịp")
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout or f"nmcli lỗi {done.returncode}").strip())
        return done.stdout

    # ─── đọc ───

    def saved(self) -> list:
        out = []
        for line in self._nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,ACTIVE",
                                "connection", "show").splitlines():
            name, ctype, auto, prio, active = (_split_terse(line) + [""] * 5)[:5]
            if ctype != "802-11-wireless":
                continue
            try:
                ssid = self._nmcli("-g", "802-11-wireless.ssid", "connection", "show", name).strip() or name
            except RuntimeError:
                ssid = name
            out.append({"name": name, "ssid": ssid, "autoconnect": auto == "yes",
                        "priority": int(prio) if prio.lstrip("-").isdigit() else 0, "active": active == "yes"})
        return sorted(out, key=lambda n: (not n["active"], -n["priority"], n["name"]))

    def status(self) -> dict:
        ip = ""
        try:
            for line in self._nmcli("-t", "-f", "IP4.ADDRESS", "device", "show", self.iface).splitlines():
                ip = ip or _split_terse(line)[-1].split("/")[0]
        except RuntimeError:
            pass
        last = None
        try:
            last = json.loads(Path(self.result_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return {"saved": self.saved(), "ip": ip, "lastSwitch": last}

    def scan(self) -> list:
        seen, out = set(), []
        for line in self._nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                                "--rescan", "yes", timeout=40).splitlines():
            ssid, signal, security = (_split_terse(line) + [""] * 3)[:3]
            if ssid and ssid not in seen:
                seen.add(ssid)
                out.append({"ssid": ssid, "signal": int(signal) if signal.isdigit() else 0, "security": security})
        return sorted(out, key=lambda n: -n["signal"])

    # ─── ghi ───

    def add(self, ssid, password=None, priority: int = 10) -> dict:
        """Lưu (hoặc cập nhật) mạng; không chuyển sang ngay."""
        ssid, password, priority = check_ssid(ssid), check_password(password), check_priority(priority)
        exists = any(n["name"] == ssid for n in self.saved())
        settings = ["connection.autoconnect", "yes", "connection.autoconnect-priority", str(priority)]
        if password:
            settings += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        if exists:
            self._nmcli("connection", "modify", ssid, *settings, sudo=True)
        else:
            self._nmcli("connection", "add", "type", "wifi", "ifname", self.iface, "con-name", ssid,
                        "ssid", ssid, *settings, sudo=True)
        logger.warning(f"Wi-Fi '{ssid}' {'updated' if exists else 'saved'} (priority {priority})")
        return {"saved": True, "name": ssid, "updated": exists}

    def set_priority(self, name, priority) -> dict:
        self._known(name)
        self._nmcli("connection", "modify", name, "connection.autoconnect-priority",
                    str(check_priority(priority)), sudo=True)
        return {"name": name, "priority": priority}

    def delete(self, name) -> dict:
        net = self._known(name)
        if net["active"]:
            raise ValueError("Không xoá được mạng đang dùng — chuyển sang mạng khác trước")
        self._nmcli("connection", "delete", name, sudo=True)
        logger.warning(f"Wi-Fi '{name}' deleted")
        return {"deleted": name}

    def switch(self, name) -> dict:
        """Chuyển sang mạng `name`, tự quay về mạng đang dùng nếu không vào được. Chạy tách rời."""
        self._known(name)
        current = next((n["name"] for n in self.saved() if n["active"]), None)
        if current == name:
            raise ValueError("Pi đang dùng mạng này rồi")
        if current is None:
            raise ValueError("Không biết mạng đang dùng nên không quay về được — dùng 'Lưu Wi-Fi' rồi để Pi tự vào")
        try:
            Path(self.result_file).unlink()
        except OSError:
            pass
        try:
            done = self._run(["sudo", "-n", "systemd-run", "--collect", "--quiet", "--unit", "lockr-wifi-switch",
                              "python3", self.switch_script, name, current, self.result_file],
                             capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"Không chạy được lệnh chuyển mạng: {e}")
        if done.returncode != 0:
            raise RuntimeError((done.stderr or f"systemd-run lỗi {done.returncode}").strip())
        logger.warning(f"Wi-Fi switch {current} -> {name} started (auto-revert)")
        return {"switching": True, "from": current, "to": name}

    def _known(self, name) -> dict:
        net = next((n for n in self.saved() if n["name"] == name), None)
        if net is None:
            raise KeyError(f"Không có mạng đã lưu tên {name}")
        return net
