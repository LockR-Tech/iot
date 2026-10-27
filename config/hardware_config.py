"""Cấu hình phần cứng của tủ do admin sửa trên web (tab "Cấu hình phần cứng" của trang Điều khiển tủ).

Backend gửi `cabinet/{lockerId}/command/hw-config` ⇒ Pi kiểm tra (`validate`), lưu
`config/hardware_config.json` rồi khởi động lại bộ điều khiển. Lúc khởi động `apply_overlay` đổi file
này thành biến môi trường **trước khi** `config.settings` đọc `.env`, nên mọi chỗ đang dùng
`settings.GPIO_RELAY_PINS`, `settings.LID_PUL_PIN`… tự nhận giá trị mới — không cần sửa code hay `.env`.
File không có ⇒ `.env` như cũ. Hợp đồng: docs/01-overview/mqtt-contract.md § 2.1.

Không import `config.settings` ở đây: settings.py gọi `apply_overlay` lúc đang được import.
"""
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

DEFAULT_FILE = str(Path(__file__).parent / "hardware_config.json")
# Sơ đồ đấu dây mẫu của tủ TU01 (bảng tay của chủ tủ 2026-10-27) — hiện khi Pi chưa lưu sơ đồ nào.
DEFAULT_WIRING_FILE = str(Path(__file__).parent / "wiring_default.json")
# Một dòng đấu dây: nhóm, chân Pi (từ), tên dây, tới (relay/domino/driver), qua, ghi chú. Pi không dùng để chạy.
WIRING_FIELDS = ("group", "from", "wire", "to", "via", "note")
MAX_WIRING_ROWS = 300
MAX_WIRING_TEXT = 300

# Chân BCM có trên header 40 chân (0, 1 là EEPROM của HAT — không dùng).
VALID_GPIO = range(2, 28)
UNLOCK_MS_RANGE = (100, 10000)
MAX_BOXES = 16
LID_PIN_KEYS = ("pulPin", "dirPin", "homePin", "endPin")
LID_PIN_NAMES = {"pulPin": "PUL−", "dirPin": "DIR−", "homePin": "công tắc gốc", "endPin": "công tắc cuối"}

# Trục ⇒ tiền tố biến môi trường (trục 1 dùng LID_*, trục 2 dùng LID2_*).
_LID_PREFIX = {1: "LID", 2: "LID2"}
_LID_PIN_ENV = {"pulPin": "PUL_PIN", "dirPin": "DIR_PIN", "homePin": "HOME_PIN", "endPin": "END_PIN"}


def _csv(values) -> str:
    return ",".join(str(int(v)) for v in values)


def _bool(value) -> str:
    return "true" if value else "false"


def to_env(cfg: dict) -> dict:
    """Cấu hình (đã hợp lệ) ⇒ {tên biến môi trường: giá trị chuỗi} như trong `.env`."""
    env = {
        "GPIO_RELAY_PINS": _csv(cfg["relayPins"]),
        "GPIO_RELAY_ACTIVE_HIGH": _bool(cfg["relayActiveHigh"]),
        "UNLOCK_PULSE_MS": str(int(cfg["unlockMs"])),
        "GPIO_DOOR_PINS": _csv(cfg["doorPins"]),
        "GPIO_DOOR_CLOSED_LOW": _bool(cfg["doorClosedLow"]),
        "REQUIRE_DOOR_SENSOR": _bool(cfg["requireDoorSensor"]),
        "LID_BYPASS_PHYSICAL_MOTION": _bool(cfg["lidBypassPhysicalMotion"]),
    }
    for lid in cfg["lids"]:
        prefix = _LID_PREFIX[lid["axis"]]
        env[f"{prefix}_ENABLED"] = _bool(lid["enabled"])
        for key, suffix in _LID_PIN_ENV.items():
            env[f"{prefix}_{suffix}"] = str(int(lid[key]))
        env[f"{prefix}_OPEN_DIR_HIGH"] = _bool(lid["openDirHigh"])
        env[f"{prefix}_LIMIT_ACTIVE_LOW"] = _bool(lid["limitActiveLow"])
        env[f"LID{lid['axis']}_BOXES"] = _csv(lid["boxes"])
    return env


def validate(cfg) -> list:
    """Danh sách lỗi (tiếng Việt, hiện cho admin); rỗng = hợp lệ. Cùng luật với backend."""
    if not isinstance(cfg, dict):
        return ["Cấu hình phải là một đối tượng JSON"]
    errors = []

    def pin_list(name, label):
        value = cfg.get(name)
        if not isinstance(value, list) or not value or len(value) > MAX_BOXES \
                or not all(isinstance(p, int) and not isinstance(p, bool) for p in value):
            errors.append(f"{label}: cần 1–{MAX_BOXES} số GPIO")
            return []
        return value

    relays = pin_list("relayPins", "Chân relay")
    doors = pin_list("doorPins", "Chân cảm biến cửa")
    if relays and doors and len(relays) != len(doors):
        errors.append(f"Số chân relay ({len(relays)}) phải bằng số chân cảm biến cửa ({len(doors)}) — mỗi ô một cặp")

    for key in ("relayActiveHigh", "doorClosedLow", "requireDoorSensor", "lidBypassPhysicalMotion"):
        if not isinstance(cfg.get(key), bool):
            errors.append(f"{key} phải là true hoặc false")
    unlock = cfg.get("unlockMs")
    if not isinstance(unlock, int) or isinstance(unlock, bool) or not UNLOCK_MS_RANGE[0] <= unlock <= UNLOCK_MS_RANGE[1]:
        errors.append(f"Thời gian kích relay phải từ {UNLOCK_MS_RANGE[0]} tới {UNLOCK_MS_RANGE[1]} ms")

    # Mỗi chân chỉ được một việc: (chân, vai trò) để báo lỗi trùng dễ hiểu.
    used = [(p, f"relay ô {i + 1}") for i, p in enumerate(relays)]
    used += [(p, f"cảm biến cửa ô {i + 1}") for i, p in enumerate(doors)]

    lids = cfg.get("lids")
    if not isinstance(lids, list) or len(lids) > 2:
        errors.append("lids phải là danh sách tối đa 2 trục")
        lids = []
    seen_axes, box_owner = set(), {}
    for lid in lids:
        if not isinstance(lid, dict) or lid.get("axis") not in _LID_PREFIX or lid.get("axis") in seen_axes:
            errors.append("Mỗi trục nắp phải có axis là 1 hoặc 2, không trùng")
            continue
        axis = lid["axis"]
        seen_axes.add(axis)
        for key in ("enabled", "openDirHigh", "limitActiveLow"):
            if not isinstance(lid.get(key), bool):
                errors.append(f"Trục {axis}: {key} phải là true hoặc false")
        for key in LID_PIN_KEYS:
            pin = lid.get(key)
            if not isinstance(pin, int) or isinstance(pin, bool):
                errors.append(f"Trục {axis}: thiếu chân {LID_PIN_NAMES[key]}")
            elif lid.get("enabled") is True:
                used.append((pin, f"{LID_PIN_NAMES[key]} trục {axis}"))
        boxes = lid.get("boxes")
        if not isinstance(boxes, list) or not all(isinstance(b, int) and not isinstance(b, bool) for b in boxes):
            errors.append(f"Trục {axis}: boxes phải là danh sách số ô")
            continue
        for box in boxes:
            if relays and not 1 <= box <= len(relays):
                errors.append(f"Trục {axis}: ô {box} không có trong tủ (1–{len(relays)})")
            elif box in box_owner:
                errors.append(f"Ô {box} không thể nằm dưới cả trục {box_owner[box]} và trục {axis}")
            else:
                box_owner[box] = axis

    owner = {}
    for pin, role in used:
        if pin not in VALID_GPIO:
            errors.append(f"{role}: GPIO{pin} không dùng được (chỉ GPIO2–GPIO27)")
        elif pin in owner:
            errors.append(f"GPIO{pin} bị dùng hai lần: {owner[pin]} và {role}")
        else:
            owner[pin] = role
    return errors


def normalize(cfg: dict) -> dict:
    """Bổ sung trục còn thiếu (tắt) để file và báo cáo luôn có đủ trục 1 và 2."""
    lids = {lid["axis"]: lid for lid in cfg.get("lids", [])}
    for axis, (pul, dirp, home, end) in {1: (18, 21, 4, 10), 2: (9, 11, 7, 8)}.items():
        lids.setdefault(axis, {"axis": axis, "enabled": False, "pulPin": pul, "dirPin": dirp, "homePin": home,
                               "endPin": end, "openDirHigh": True, "limitActiveLow": True, "boxes": []})
    return {**cfg, "lids": [lids[1], lids[2]]}


def load(path: str = DEFAULT_FILE) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(cfg: dict, path: str = DEFAULT_FILE):
    """Ghi nguyên tử; bản trước giữ ở `<file>.bak` để quay lại tay nếu cần."""
    if os.path.exists(path):
        os.replace(path, f"{path}.bak")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def apply_overlay(path: Optional[str] = None) -> Optional[dict]:
    """Gọi trong settings.py sau `load_dotenv()`: file hợp lệ ⇒ ghi đè biến môi trường. Trả cấu hình đã dùng.

    File hỏng hoặc không hợp lệ thì bỏ qua cả file (chạy theo `.env`) — không bao giờ dùng nửa vời.
    """
    path = path or os.getenv("HARDWARE_CONFIG_FILE") or DEFAULT_FILE
    cfg = load(path)
    if cfg is None:
        return None
    cfg = normalize(cfg) if isinstance(cfg.get("lids"), list) else cfg
    if validate(cfg):
        return None
    os.environ.update(to_env(cfg))
    return cfg


def sanitize_wiring(rows) -> list:
    """Chỉ giữ các cột đã biết (chuỗi, cắt độ dài), bỏ dòng trống. Quá MAX_WIRING_ROWS ⇒ ValueError."""
    if not isinstance(rows, list):
        return []
    if len(rows) > MAX_WIRING_ROWS:
        raise ValueError(f"Sơ đồ đấu dây tối đa {MAX_WIRING_ROWS} dòng")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean = {f: str(row.get(f) or "").strip()[:MAX_WIRING_TEXT] for f in WIRING_FIELDS}
        if any(clean.values()):
            out.append(clean)
    return out


def load_default_wiring(path: str = DEFAULT_WIRING_FILE) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            return sanitize_wiring(json.load(f))
    except (OSError, ValueError):
        return []


def functional(cfg: dict) -> dict:
    """Phần Pi dùng để chạy (bỏ sơ đồ đấu dây), ô dưới nắp xếp tăng dần — để so hai cấu hình."""
    out = {k: v for k, v in cfg.items() if k != "wiring"}
    out["lids"] = [{**lid, "boxes": sorted(lid.get("boxes", []))}
                   for lid in sorted(cfg.get("lids", []), key=lambda l: l.get("axis", 0))]
    return out


def sync_lid_tuning(cfg: dict, path: str):
    """Chiều mở và mức công tắc trong lid_tuning.json ghi đè `.env` ⇒ đổi theo cấu hình mới."""
    try:
        with open(path, encoding="utf-8") as f:
            tuning = json.load(f)
    except (FileNotFoundError, ValueError):
        return
    changed = False
    for lid in cfg["lids"]:
        saved = tuning.get(str(lid["axis"]))
        if isinstance(saved, dict):
            saved["open_dir_high"] = lid["openDirHigh"]
            saved["limit_active_low"] = lid["limitActiveLow"]
            changed = True
    if changed:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tuning, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def update_boot_relay_pins(pins: list, script: str, backend: str) -> Optional[str]:
    """Chạy scripts/boot_relay_pins.py bằng sudo (dòng `gpio=` + ExecStopPost). Trả cảnh báo nếu không làm được."""
    if backend != "gpio":
        return None
    csv = ",".join(str(int(p)) for p in pins)
    try:
        done = subprocess.run(["sudo", "-n", "python3", script, csv], capture_output=True, text=True, timeout=20)
        report = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
        if done.returncode == 0 and report.get("ok"):
            return None
        reason = report.get("error") or done.stderr.strip() or f"mã {done.returncode}"
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        reason = str(e)
    return (f"Chưa cập nhật được dòng giữ relay ở mức thấp lúc khởi động ({reason}). "
            f"Chạy tay trên Pi: sudo python3 scripts/boot_relay_pins.py {csv}")


def store(cfg, settings, wiring: Optional[list] = None) -> dict:
    """Đường lưu chung của lệnh hw-config (admin web) và nút Lưu trên bảng /service.

    Kiểm tra ⇒ ghi file (giữ sơ đồ đấu dây cũ nếu `wiring` là None) ⇒ đồng bộ lid_tuning.json ⇒ đổi
    chân relay thì cập nhật dòng an toàn lúc khởi động. Trả {"errors", "warnings", "config"}; có lỗi
    thì không ghi gì. Việc khởi động lại để Pi chạy theo bản mới là của nơi gọi.
    """
    if isinstance(cfg, dict) and isinstance(cfg.get("lids"), list):
        cfg = normalize(cfg)
    errors = validate(cfg)
    if errors:
        return {"errors": errors, "warnings": [], "config": cfg}
    path = settings.HARDWARE_CONFIG_FILE
    if wiring is None:
        wiring = (load(path) or {}).get("wiring")
    cfg = {**functional(cfg), **({"wiring": sanitize_wiring(wiring)} if wiring is not None else {})}
    save(cfg, path)
    sync_lid_tuning(cfg, settings.LID_TUNING_FILE)
    warnings = []
    if list(cfg["relayPins"]) != list(settings.GPIO_RELAY_PINS):
        warning = update_boot_relay_pins(cfg["relayPins"], settings.BOOT_RELAY_SCRIPT, settings.HARDWARE_BACKEND)
        if warning:
            warnings.append(warning)
    return {"errors": [], "warnings": warnings, "config": cfg}


def effective(settings, lids: Optional[dict] = None) -> dict:
    """Cấu hình đang chạy, cùng định dạng với lệnh hw-config. `lids` = {trục: LidController} để lấy
    chiều/mức công tắc thật (thông số đã chỉnh trên bảng /service ghi đè `.env`)."""
    lids = lids or {}
    out = []
    for axis, prefix in _LID_PREFIX.items():
        live = lids.get(axis) or lids.get(str(axis))
        live_cfg = live.config() if live else {}
        out.append({
            "axis": axis,
            "enabled": bool(getattr(settings, f"{prefix}_ENABLED")),
            "pulPin": getattr(settings, f"{prefix}_PUL_PIN"),
            "dirPin": getattr(settings, f"{prefix}_DIR_PIN"),
            "homePin": getattr(settings, f"{prefix}_HOME_PIN"),
            "endPin": getattr(settings, f"{prefix}_END_PIN"),
            "openDirHigh": bool(live_cfg.get("open_dir_high", getattr(settings, f"{prefix}_OPEN_DIR_HIGH"))),
            "limitActiveLow": bool(live_cfg.get("limit_active_low", getattr(settings, f"{prefix}_LIMIT_ACTIVE_LOW"))),
            "boxes": list(getattr(settings, f"LID{axis}_BOXES", []) or []),
        })
    return {
        "relayPins": list(settings.GPIO_RELAY_PINS),
        "relayActiveHigh": bool(settings.GPIO_RELAY_ACTIVE_HIGH),
        "unlockMs": int(settings.UNLOCK_PULSE_MS),
        "doorPins": list(settings.GPIO_DOOR_PINS),
        "doorClosedLow": bool(settings.GPIO_DOOR_CLOSED_LOW),
        "requireDoorSensor": bool(settings.REQUIRE_DOOR_SENSOR),
        "lidBypassPhysicalMotion": bool(settings.LID_BYPASS_PHYSICAL_MOTION),
        "lids": out,
    }
