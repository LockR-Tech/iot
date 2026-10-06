"""
Bảng điều khiển kỹ thuật của tủ — trang `/service` trên API cục bộ cổng 8000.

Cho kỹ thuật viên: mở từng ô với thời gian kích tuỳ chọn, xem chân Pi nào nối
vào đâu (theo cấu hình đang chạy), chạy/chỉnh tốc độ các trục nắp trượt.
Chạy trong chính tiến trình `main.py` nên không phải dừng dịch vụ như
`debug_gpio.py`.

Chỉ nhận lệnh từ chính Pi (127.0.0.1): từ laptop đi qua SSH tunnel
`ssh -N -L 8800:127.0.0.1:8000 lockr@<pi>` rồi mở http://localhost:8800/service.
Kiosk không có đường dẫn tới trang này. Lệnh gửi từ trang khác nguồn (ví dụ
kiosk ở cổng 3002) bị chặn bằng kiểm tra `Origin`.
"""
import json
import socket
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from hardware.gpio_pins import HEADER_PIN, POWER_PINS
from utils.logger import get_logger

logger = get_logger("ServicePanel")

PANEL_HTML = Path(__file__).parent.parent / "service_panel" / "index.html"
LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_BACKEND = "https://api.locker-drone.tech"
OPEN_MS_RANGE = (100, 10000)         # cuộn khoá 12 V không nên giữ điện lâu hơn
LAYOUT_CACHE_S = 30

# Dây nguồn/GND không do phần mềm điều khiển — ghi theo hướng dẫn nối dây tủ TU01.
POWER_NOTES = {
    1: "PUL+ trục 1, VCC công tắc gốc trục 1",
    17: "DIR+ trục 1, VCC công tắc cuối trục 1, cầu sang cọc 8 domino 2 (trục 2)",
    2: "DC+ module relay",
    6: "DC− module relay",
    25: "GND chung dây vàng sọc xanh (domino 1)",
    9: "GND công tắc gốc trục 1",
    14: "GND công tắc cuối trục 1",
    20: "GND công tắc gốc trục 2",
    34: "GND công tắc cuối trục 2",
    30: "GND ô 7 (nếu dùng cặp dây vàng ở domino 2)",
}


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


class ServicePanel:
    def __init__(self, hardware, lids: Dict[int, object], cabinet_state, settings,
                 save_tuning: Optional[Callable[[], None]] = None,
                 fetch_json: Optional[Callable[[str], dict]] = None):
        self.hardware = hardware
        self.lids = lids or {}
        self.cabinet_state = cabinet_state
        self.settings = settings
        self._save_tuning = save_tuning
        self._fetch_json = fetch_json or _http_get_json
        self._layout_cache = (0.0, None)
        self._layout_lock = threading.Lock()
        self.actions = deque(maxlen=60)

    # ─── đọc ───

    def overview(self) -> dict:
        cabinets = self.cabinet_state.all_cabinets if self.cabinet_state else []
        return {
            "system": {
                "macAddress": self.settings.MAC_ADDRESS,
                "firmware": self.settings.FIRMWARE_VERSION,
                "ipAddress": local_ip(),
                "backend": type(self.hardware).__name__ if self.hardware is not None else None,
                "connected": bool(self.hardware and self.hardware.is_connected()),
                "cabinet": cabinets[0] if cabinets else None,
            },
            "relay": {
                "unlockMs": getattr(self.hardware, "unlock_ms", self.settings.UNLOCK_PULSE_MS),
                "settleMs": getattr(self.hardware, "settle_ms", self.settings.DOOR_SETTLE_MS),
                "activeHigh": getattr(self.hardware, "relay_active_high", None),
                "doorClosedLow": getattr(self.hardware, "door_closed_low", None),
                "openMsRange": list(OPEN_MS_RANGE),
            },
            "boxes": self._boxes(),
            "lids": [self._lid_view(axis) for axis in (1, 2)],
            "header": self._header(),
            "actions": list(self.actions)[::-1],
        }

    def _boxes(self) -> list:
        relay_pins = getattr(self.hardware, "relay_pins", None)
        door_pins = getattr(self.hardware, "door_pins", None)
        count = len(relay_pins) if relay_pins else getattr(self.hardware, "num_slots", 0) or 0
        doors = self.hardware.door_states() if hasattr(self.hardware, "door_states") else [None] * count
        relays = self.hardware.relay_states() if hasattr(self.hardware, "relay_states") else [None] * count
        boxes = []
        for slot in range(count):
            relay = relay_pins[slot] if relay_pins else None
            door = door_pins[slot] if door_pins else None
            boxes.append({
                "box": slot + 1,
                "slot": slot,
                "relayChannel": f"IN{slot + 1}",
                "relayGpio": relay,
                "relayPin": HEADER_PIN.get(relay),
                "relayOn": relays[slot],
                "doorGpio": door,
                "doorPin": HEADER_PIN.get(door),
                "doorClosed": doors[slot],
            })
        return boxes

    def _axis_pins(self, axis: int) -> dict:
        s = self.settings
        if axis == 1:
            return {"pul": s.LID_PUL_PIN, "dir": s.LID_DIR_PIN, "home": s.LID_HOME_PIN, "end": s.LID_END_PIN}
        return {"pul": s.LID2_PUL_PIN, "dir": s.LID2_DIR_PIN, "home": s.LID2_HOME_PIN, "end": s.LID2_END_PIN}

    def _lid_view(self, axis: int) -> dict:
        lid = self.lids.get(axis)
        pins = self._axis_pins(axis)
        view = {
            "axis": axis,
            "enabled": lid is not None,
            "envFlag": "LID_ENABLED" if axis == 1 else "LID2_ENABLED",
            "pins": {k: {"gpio": v, "pin": HEADER_PIN.get(v)} for k, v in pins.items()},
        }
        if lid is not None:
            view["status"] = lid.status()
        return view

    def _header(self) -> list:
        roles = {}       # chân vật lý → (loại, nhãn, đang dùng?)
        for b in self._boxes():
            if b["relayPin"]:
                roles[b["relayPin"]] = ("relay", f"Relay {b['relayChannel']} · ô {b['box']}", True)
            if b["doorPin"]:
                roles[b["doorPin"]] = ("door", f"Cảm biến cửa ô {b['box']}", True)
        names = {"pul": "PUL−", "dir": "DIR−", "home": "công tắc gốc", "end": "công tắc cuối"}
        for axis in (1, 2):
            active = axis in self.lids
            for key, gpio in self._axis_pins(axis).items():
                pin = HEADER_PIN.get(gpio)
                if pin and pin not in roles:
                    label = f"Trục {axis} {names[key]}" + ("" if active else " (chưa bật)")
                    roles[pin] = (f"lid{axis}", label, active)
        header = []
        for pin in range(1, 41):
            gpio = next((g for g, p in HEADER_PIN.items() if p == pin), None)
            if pin in roles:
                kind, label, active = roles[pin]
            elif pin in POWER_PINS:
                kind = POWER_PINS[pin].lower()
                label, active = POWER_NOTES.get(pin, ""), pin in POWER_NOTES
            else:
                kind, label, active = "free", "", False
            header.append({"pin": pin, "gpio": gpio, "kind": kind, "label": label, "used": active})
        return header

    def layout(self) -> dict:
        """Sơ đồ ô theo vị trí thật, lấy từ backend (để lưới trên trang giống tủ)."""
        cabinets = self.cabinet_state.all_cabinets if self.cabinet_state else []
        if not cabinets:
            return {"cells": None, "error": "Pi chưa được gán vào tủ nào"}
        with self._layout_lock:
            fetched_at, cached = self._layout_cache
            if cached is not None and time.monotonic() - fetched_at < LAYOUT_CACHE_S:
                return cached
            base = (self.settings.BACKEND_API_URL or DEFAULT_BACKEND).rstrip("/")
            url = f"{base}/api/lockers/{cabinets[0]['id']}/layout"
            try:
                body = self._fetch_json(url)
                data = body.get("data", body)
                cells = [{k: c.get(k) for k in ("boxNumber", "rowIndex", "colIndex", "size", "cellType", "status")}
                         for c in data.get("cells", [])]
                result = {"cells": cells, "error": None}
                self._layout_cache = (time.monotonic(), result)
                return result
            except Exception as e:
                logger.warning(f"Không lấy được sơ đồ ô từ {url}: {e}")
                return {"cells": None, "error": "Không lấy được sơ đồ ô từ máy chủ"}

    # ─── lệnh ───

    def _log(self, what: str, result: dict) -> dict:
        self.actions.append({"at": datetime.now().strftime("%H:%M:%S"), "what": what,
                             "result": result.get("result"), "detail": result})
        logger.warning(f"Service panel: {what} → {result.get('result')}")
        return result

    def open_box(self, box: int, ms: Optional[int] = None) -> dict:
        count = len(self._boxes())
        if not 1 <= box <= count:
            raise ValueError(f"Ô phải từ 1 tới {count}")
        if ms is not None and not OPEN_MS_RANGE[0] <= ms <= OPEN_MS_RANGE[1]:
            raise ValueError(f"Thời gian kích phải từ {OPEN_MS_RANGE[0]} tới {OPEN_MS_RANGE[1]} ms")
        slot = box - 1
        if ms is not None and hasattr(self.hardware, "pulse_slot"):
            result = self.hardware.pulse_slot(slot, ms)
        else:
            result = self.hardware.open_slot(slot, slave_id=getattr(self.hardware, "slave_id", 1))
        return self._log(f"Mở ô {box}" + (f" ({ms} ms)" if ms else ""), result)

    def _lid(self, axis: int):
        lid = self.lids.get(axis)
        if lid is None:
            raise KeyError(f"Trục {axis} chưa bật")
        return lid

    def lid_command(self, axis: int, action: str) -> dict:
        lid = self._lid(axis)
        if action == "stop":
            return self._log(f"Dừng trục {axis}", lid.stop())
        names = {"open": "mở hết", "close": "đóng hết", "home": "về gốc"}
        if action not in names:
            raise ValueError("Lệnh phải là open, close, home hoặc stop")
        return self._log(f"Trục {axis} {names[action]}", lid.run_async(action))

    def jog(self, axis: int, revolutions: float, direction: str) -> dict:
        lid = self._lid(axis)
        if direction not in ("open", "close"):
            raise ValueError("Chiều phải là open hoặc close")
        if revolutions <= 0:
            raise ValueError("Số vòng phải lớn hơn 0")
        max_revs = lid.config()["max_revs"]
        if revolutions > max_revs:
            raise ValueError(f"Tối đa {max_revs} vòng mỗi lần (giới hạn an toàn)")
        label = "về phía mở" if direction == "open" else "về phía đóng"
        return self._log(f"Trục {axis} chạy {revolutions:g} vòng {label}",
                         lid.run_async("jog", revolutions=revolutions, toward_end=direction == "open"))

    def tune(self, axis: int, changes: dict) -> dict:
        lid = self._lid(axis)
        config = lid.tune(**changes)
        if self._save_tuning:
            self._save_tuning()
        self._log(f"Chỉnh trục {axis}: {config['rps']} vòng/s, xung {config['pulse_us']} µs", {"result": "OK"})
        return config

    def stop_all(self) -> dict:
        for lid in self.lids.values():
            lid.stop()
        return self._log("Dừng tất cả trục", {"result": "OK"})


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ─── HTTP ───

class OpenBoxRequest(BaseModel):
    ms: Optional[int] = None


class JogRequest(BaseModel):
    revolutions: float
    direction: str


class TuningRequest(BaseModel):
    rps: Optional[float] = None
    start_rps: Optional[float] = None
    ramp_steps: Optional[int] = None
    pulse_us: Optional[int] = None
    max_revs: Optional[float] = None
    steps_per_rev: Optional[int] = None
    open_dir_high: Optional[bool] = None
    limit_active_low: Optional[bool] = None


def require_local_same_origin(request: Request):
    host = request.client.host if request.client else ""
    if host not in LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="Chỉ mở được từ chính Pi hoặc qua SSH tunnel.")
    origin = request.headers.get("origin")
    if origin and origin != "null" and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Lệnh từ trang khác bị chặn.")


def _call(fn, *args):
    try:
        return fn(*args)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail="Trục đang chạy, dừng trước rồi chỉnh." if str(e) == "LID_BUSY" else str(e))


def create_service_router(panel: ServicePanel) -> APIRouter:
    # Các route dùng `def` thường: FastAPI chạy trong threadpool nên lệnh dừng vẫn
    # vào được trong lúc một ô đang kích hoặc một trục đang chạy.
    router = APIRouter(prefix="/service", dependencies=[Depends(require_local_same_origin)])

    @router.get("", include_in_schema=False)
    @router.get("/", include_in_schema=False)
    def page():
        return FileResponse(PANEL_HTML, media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store"})

    @router.get("/api/overview")
    def overview():
        return panel.overview()

    @router.get("/api/layout")
    def layout():
        return panel.layout()

    @router.post("/api/boxes/{box}/open")
    def open_box(box: int, req: Optional[OpenBoxRequest] = None):
        return _call(panel.open_box, box, req.ms if req else None)

    @router.post("/api/lids/{axis}/jog")
    def jog(axis: int, req: JogRequest):
        return _call(panel.jog, axis, req.revolutions, req.direction)

    @router.put("/api/lids/{axis}/tuning")
    def tune(axis: int, req: TuningRequest):
        return _call(panel.tune, axis, req.model_dump(exclude_none=True))

    @router.post("/api/lids/{axis}/{action}")
    def lid_command(axis: int, action: str):
        return _call(panel.lid_command, axis, action)

    @router.post("/api/stop-all")
    def stop_all():
        return panel.stop_all()

    return router
