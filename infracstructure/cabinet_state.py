import json
import threading
from pathlib import Path
from typing import Callable, Optional
from utils.logger import get_logger

logger = get_logger("CabinetState")

# Mặc định lưu cùng thư mục config
_DEFAULT_STATE_PATH = str(Path(__file__).parent.parent / "config" / "cabinet_state.json")


class CabinetState:
    """
    Quản lý trạng thái hệ thống: lưu thông tin Location và danh sách các Cabinet.
    Hỗ trợ scale nhiều cabinet trên 1 Gateway RPi (kết nối qua RS485).

    Hợp đồng MQTT (ADR-0008): `id` của cabinet là `lockerId` của backend dạng chuỗi,
    và cũng là đoạn `{lockerId}` trong topic `cabinet/{lockerId}/…`. Pi chưa được gán
    trên admin thì dùng tủ dự phòng lấy từ `LOCKER_ID` trong `.env` (không lưu xuống đĩa).
    """

    def __init__(self, state_path: str = _DEFAULT_STATE_PATH, db_manager=None,
                 fallback_locker_id: str = "", fallback_slave_id: int = 1):
        self._path = Path(state_path)
        self._db = db_manager
        self._lock = threading.RLock()
        self._listeners: list[Callable[[], None]] = []

        self._location: Optional[dict] = None
        self._cabinets: dict[str, dict] = {} # Key là cabinet_id
        self._lockers: dict[str, dict[int, dict]] = {} # cabinet_id -> slot_index -> locker_data
        # boxId học được từ lệnh backend (open/sync) khi chưa có sơ đồ setup: cabinet_id -> slot -> boxId
        self._learned_boxes: dict[str, dict[int, int]] = {}

        fallback_locker_id = str(fallback_locker_id or "").strip()
        self._fallback: Optional[dict] = None
        if fallback_locker_id:
            self._fallback = {
                "id": fallback_locker_id,
                "name": f"locker-{fallback_locker_id}",
                "totalRows": 0,
                "totalColumns": 0,
                "slaveId": fallback_slave_id,
                "heartbeatInterval": 60,
                "isSynced": False,
                "source": "env",
            }

        self.load()

    # ─── Properties ───

    @property
    def location(self) -> Optional[dict]:
        return self._location

    @property
    def all_cabinets(self) -> list:
        """Các tủ Pi đang phục vụ: tủ đã được gán qua lệnh setup, nếu chưa có thì tủ dự phòng từ `.env`."""
        with self._lock:
            if self._cabinets:
                return list(self._cabinets.values())
            return [self._fallback] if self._fallback else []

    @property
    def provisioned_cabinets(self) -> list:
        """Chỉ các tủ đã được gán qua lệnh setup."""
        with self._lock:
            return list(self._cabinets.values())

    @property
    def is_configured(self) -> bool:
        """Có ít nhất 1 tủ để phục vụ (đã gán, hoặc dự phòng từ `.env`)."""
        return len(self.all_cabinets) > 0

    @property
    def primary_locker_id(self) -> Optional[str]:
        cabs = self.all_cabinets
        return cabs[0]["id"] if cabs else None

    @property
    def heartbeat_interval(self) -> int:
        """Lấy interval từ bất kỳ cabinet nào (thường giống nhau trên cùng 1 gateway)."""
        cabs = self.all_cabinets
        if not cabs:
            return 60
        return cabs[0].get("heartbeatInterval", 60)

    def get_cabinet_by_id(self, cabinet_id: str) -> Optional[dict]:
        cabinet_id = str(cabinet_id)
        for cab in self.all_cabinets:
            if cab["id"] == cabinet_id:
                return cab
        return None

    def get_cabinet_by_name(self, name: str) -> Optional[dict]:
        for cab in self.all_cabinets:
            if cab.get("name") == name:
                return cab
        return None

    def get_cabinet_by_slave(self, slave_id: int) -> Optional[dict]:
        for cab in self.all_cabinets:
            if cab.get("slaveId", 1) == slave_id:
                return cab
        return None

    # ─── Thay đổi cấu hình ───

    def add_listener(self, callback: Callable[[], None]):
        """Gọi `callback()` mỗi khi danh sách tủ đổi (setup, clear) — để subscribe lại topic."""
        self._listeners.append(callback)

    def _notify(self):
        for callback in list(self._listeners):
            try:
                callback()
            except Exception as e:
                logger.error(f"Cabinet state listener failed: {e}")

    # ─── Persistence ───

    def save_location(self, location_id: str, name: str, address: str):
        """Lưu thông tin Location."""
        self._location = {
            "id": location_id,
            "name": name,
            "address": address
        }

        if self._db:
            self._db.save_location(self._location)
        self._save_to_json()
        logger.info(f"Location saved: {name} ({location_id})")

    def save_cabinet(self, cabinet_id: str, name: str,
                     total_rows: int = 0, total_columns: int = 0,
                     slave_id: int = 0,
                     heartbeat_interval: int = 60,
                     is_synced: bool = False):
        """Lưu hoặc cập nhật thông tin một Cabinet."""
        if not self._location:
            logger.error("Cannot save cabinet without location")
            return

        cabinet_id = str(cabinet_id)
        cabinet_data = {
            "id": cabinet_id,
            "locationId": self._location["id"],
            "name": name,
            "totalRows": total_rows,
            "totalColumns": total_columns,
            "slaveId": slave_id,
            "heartbeatInterval": heartbeat_interval,
            "isSynced": is_synced
        }

        with self._lock:
            self._cabinets[cabinet_id] = cabinet_data

        if self._db:
            self._db.save_cabinet(cabinet_data)
        self._save_to_json()

        logger.info(
            f"Cabinet saved: {name} (id={cabinet_id}, slave={slave_id}, layout={total_rows}x{total_columns})"
        )
        self._notify()

    def remove_cabinet(self, cabinet_id: str):
        """Bỏ một tủ đã gán (Pi được gán sang tủ khác trên cùng slave)."""
        cabinet_id = str(cabinet_id)
        with self._lock:
            removed = self._cabinets.pop(cabinet_id, None)
            self._lockers.pop(cabinet_id, None)
            self._learned_boxes.pop(cabinet_id, None)
        if removed is None:
            return
        if self._db:
            self._db.delete_cabinet(cabinet_id)
        self._save_to_json()
        logger.info(f"Cabinet removed: {removed.get('name')} (id={cabinet_id})")
        self._notify()

    def load(self):
        """Load state từ file JSON hoặc Database."""
        data = None
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.debug("System state loaded from JSON")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load state from JSON: {e}")

        if not data and self._db:
            loc = self._db.get_location()
            cabs = self._db.get_cabinets()
            if loc:
                data = {"location": loc, "cabinets": cabs}
                logger.info("System state recovered from Database")

        if not data:
            return

        self._location = data.get("location")
        cabinets_list = [_camel_cabinet(c) for c in data.get("cabinets", [])]
        self._cabinets = {c["id"]: c for c in cabinets_list}

        # Sơ đồ ô: ưu tiên bản trong JSON, thiếu thì lấy từ Database
        saved_lockers = data.get("lockers") or {}
        for cab_id in self._cabinets:
            rows = saved_lockers.get(cab_id)
            if rows is None and self._db:
                rows = self._db.get_lockers(cab_id)
            if rows:
                self._lockers[cab_id] = {int(l["slot_index"]): self._locker_row(cab_id, l) for l in rows}

        cab_count = len(self._cabinets)
        loc_name = self._location.get("name") if self._location else "None"
        logger.info(f"System state initialized: Location={loc_name}, Cabinets={cab_count}")

    @staticmethod
    def _locker_row(cabinet_id: str, row: dict) -> dict:
        box_id = row.get("box_id", row.get("id"))
        return {
            "id": str(box_id) if box_id is not None else None,
            "box_id": _to_int(box_id),
            "cabinet_id": cabinet_id,
            "slot_index": int(row["slot_index"]),
            "locker_label": row.get("locker_label"),
        }

    def save_lockers(self, cabinet_id: str, lockers: list):
        """Lưu sơ đồ ô của một tủ: [{boxId, slotIndex, label, …}] theo lệnh setup."""
        cabinet_id = str(cabinet_id)
        rows = []
        for l in lockers:
            if l.get("slotIndex") is None:
                continue
            box_id = l.get("boxId", l.get("id"))
            rows.append({
                "id": str(box_id) if box_id is not None else f"{cabinet_id}-{l['slotIndex']}",
                "slotIndex": int(l["slotIndex"]),
                "label": l.get("label") or str(int(l["slotIndex"]) + 1),
            })

        if self._db:
            self._db.save_lockers(cabinet_id, rows)

        with self._lock:
            self._lockers[cabinet_id] = {r["slotIndex"]: self._locker_row(cabinet_id, {
                "id": r["id"], "slot_index": r["slotIndex"], "locker_label": r["label"],
            }) for r in rows}
            self._learned_boxes.pop(cabinet_id, None)
        self._save_to_json()

    def get_locker_by_slot(self, cabinet_id: str, slot_index: int) -> Optional[dict]:
        """Lấy thông tin locker từ memory cache."""
        cabinet_lockers = self._lockers.get(str(cabinet_id), {})
        return cabinet_lockers.get(slot_index)

    def layout_slots(self, cabinet_id: str) -> list:
        """Các slotIndex có trong sơ đồ setup (rỗng nếu chưa setup)."""
        return sorted(self._lockers.get(str(cabinet_id), {}).keys())

    # ─── Tra boxId ↔ slotIndex ───

    def remember_box(self, cabinet_id: str, slot_index: int, box_id):
        """Ghi nhớ boxId của một ô từ lệnh backend, để báo trạng thái cửa kèm boxId."""
        box_id = _to_int(box_id)
        if box_id is None or slot_index is None:
            return
        with self._lock:
            self._learned_boxes.setdefault(str(cabinet_id), {})[int(slot_index)] = box_id

    def box_id_for_slot(self, cabinet_id: str, slot_index: int) -> Optional[int]:
        locker = self.get_locker_by_slot(cabinet_id, slot_index)
        if locker and locker.get("box_id") is not None:
            return locker["box_id"]
        return self._learned_boxes.get(str(cabinet_id), {}).get(slot_index)

    def slot_for_box(self, cabinet_id: str, box_id) -> Optional[int]:
        box_id = _to_int(box_id)
        if box_id is None:
            return None
        for slot, locker in self._lockers.get(str(cabinet_id), {}).items():
            if locker.get("box_id") == box_id:
                return slot
        for slot, learned in self._learned_boxes.get(str(cabinet_id), {}).items():
            if learned == box_id:
                return slot
        return None

    def _save_to_json(self):
        """Ghi toàn bộ state ra JSON."""
        try:
            with self._lock:
                data = {
                    "location": self._location,
                    "cabinets": list(self._cabinets.values()),
                    "lockers": {
                        cab_id: [
                            {"id": l["id"], "slot_index": slot, "locker_label": l.get("locker_label")}
                            for slot, l in sorted(slots.items())
                        ]
                        for cab_id, slots in self._lockers.items()
                    },
                }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Failed to save state to JSON: {e}")

    def clear(self):
        """Xoá sạch state (quay về tủ dự phòng từ `.env` nếu có)."""
        with self._lock:
            self._location = None
            self._cabinets = {}
            self._lockers = {}
            self._learned_boxes = {}

        if self._path.exists():
            try:
                self._path.unlink()
            except IOError as e:
                logger.error(f"Failed to clear JSON state: {e}")

        if self._db:
            self._db.clear_all_state()

        logger.info("All system state cleared")
        self._notify()


_DB_TO_JSON_KEYS = {
    "location_id": "locationId", "total_rows": "totalRows", "total_columns": "totalColumns",
    "slave_id": "slaveId", "heartbeat_interval": "heartbeatInterval", "is_synced": "isSynced",
}


def _camel_cabinet(row: dict) -> dict:
    """Bản ghi từ Database dùng snake_case, từ JSON dùng camelCase — đưa về một dạng."""
    cab = {_DB_TO_JSON_KEYS.get(k, k): v for k, v in row.items() if k != "updated_at"}
    cab["id"] = str(cab["id"])
    return cab


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
