"""Dựng phần cứng GPIO (khoá + các trục nắp trượt) từ `config.settings`."""
import json
import os
from typing import Dict, Optional, Tuple

from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import GpiodPins, PinIO
from hardware.lid_controller import LidController
from utils.logger import get_logger

logger = get_logger("Hardware")


def lid_axis_configs(settings) -> Dict[int, dict]:
    """Chân của từng trục đang bật: {số trục: tham số riêng}."""
    axes = {}
    if settings.LID_ENABLED:
        axes[1] = dict(pul_pin=settings.LID_PUL_PIN, dir_pin=settings.LID_DIR_PIN,
                       home_pin=settings.LID_HOME_PIN, end_pin=settings.LID_END_PIN,
                       open_dir_high=settings.LID_OPEN_DIR_HIGH,
                       limit_active_low=settings.LID_LIMIT_ACTIVE_LOW)
    if settings.LID2_ENABLED:
        axes[2] = dict(pul_pin=settings.LID2_PUL_PIN, dir_pin=settings.LID2_DIR_PIN,
                       home_pin=settings.LID2_HOME_PIN, end_pin=settings.LID2_END_PIN,
                       open_dir_high=settings.LID2_OPEN_DIR_HIGH,
                       limit_active_low=settings.LID2_LIMIT_ACTIVE_LOW)
    return axes


def load_lid_tuning(path: str) -> Dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.error(f"Bỏ qua {path}: {e}")
        return {}


def save_lid_tuning(path: str, lids: Dict[int, LidController]):
    data = {str(axis): lid.config() for axis, lid in lids.items()}
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_lid_positions(path: str) -> Dict[str, dict]:
    """{trục: {"positionRevs": số vòng cách gốc}} đã lưu; file hỏng/thiếu ⇒ {} (vị trí chưa biết)."""
    return load_lid_tuning(path)


class LidPositionStore:
    """Lưu vị trí bàn trượt (đơn vị vòng — đổi số bước/vòng vẫn đúng) mỗi khi một trục chạy xong."""

    def __init__(self, path: str, lids: Dict[int, LidController]):
        self.path = path
        self.lids = lids

    def save(self, _axis: int = None, _position=None):
        data = {}
        for axis, lid in self.lids.items():
            revs = None if lid.position is None else round(lid.position / lid.steps_per_rev, 3)
            data[str(axis)] = {"positionRevs": revs}
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.error(f"Không lưu được vị trí nắp vào {self.path}: {e}")


def restore_lid_positions(lids: Dict[int, LidController], path: str) -> LidPositionStore:
    """Nạp vị trí đã lưu cho trục chưa biết vị trí (không chạm công tắc lúc khởi động) và gắn bộ lưu."""
    saved = load_lid_positions(path)
    store = LidPositionStore(path, lids)
    for axis, lid in lids.items():
        if not lid.position_travel:
            continue
        revs = (saved.get(str(axis)) or {}).get("positionRevs")
        if lid.position is None and isinstance(revs, (int, float)):
            try:
                lid.set_position(min(float(revs), lid.max_steps / lid.steps_per_rev))
            except (ValueError, RuntimeError) as e:
                logger.error(f"Bỏ qua vị trí đã lưu của trục {axis}: {e}")
        lid.on_position = store.save
        logger.info(f"Lid {axis}: vị trí {'chưa biết' if lid.position is None else f'{lid.position / lid.steps_per_rev:g} vòng cách gốc'}"
                    f" ({lid.state.value})")
    return store


def create_gpio_hardware(settings, pins: Optional[PinIO] = None) -> Tuple[GpioLockerManager, Dict[int, LidController]]:
    pins = pins or GpiodPins(settings.GPIO_CHIP)

    locker = GpioLockerManager(
        pins,
        relay_pins=settings.GPIO_RELAY_PINS,
        door_pins=settings.GPIO_DOOR_PINS,
        relay_active_high=settings.GPIO_RELAY_ACTIVE_HIGH,
        door_closed_low=settings.GPIO_DOOR_CLOSED_LOW,
        slave_id=settings.GPIO_SLAVE_ID,
        unlock_ms=settings.UNLOCK_PULSE_MS,
        settle_ms=settings.DOOR_SETTLE_MS,
    ).start()

    tuning = load_lid_tuning(settings.LID_TUNING_FILE)
    lids: Dict[int, LidController] = {}
    for axis, axis_pins in lid_axis_configs(settings).items():
        lid = LidController(
            pins,
            axis=axis,
            step_active_low=settings.LID_STEP_ACTIVE_LOW,
            steps_per_sec=settings.LID_STEPS_PER_SEC,
            start_steps_per_sec=settings.LID_START_STEPS_PER_SEC,
            ramp_steps=settings.LID_RAMP_STEPS,
            max_steps=settings.LID_MAX_STEPS,
            pulse_us=settings.LID_PULSE_US,
            steps_per_rev=settings.LID_STEPS_PER_REV,
            position_travel=not getattr(settings, "LID_REQUIRE_LIMIT_SWITCH", False),
            **axis_pins,
        )
        saved = tuning.get(str(axis))
        if saved:
            try:
                lid.tune(**saved)
            except (ValueError, RuntimeError) as e:
                logger.error(f"Bỏ qua thông số đã lưu của trục {axis}: {e}")
        lids[axis] = lid.start()
    restore_lid_positions(lids, getattr(settings, "LID_POSITION_FILE", "lid_position.json"))
    return locker, lids
