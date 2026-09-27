"""Dựng phần cứng GPIO (khoá + nắp trượt) từ `config.settings`."""
from typing import Optional, Tuple

from hardware.gpio_locker import GpioLockerManager
from hardware.gpio_pins import GpiodPins, PinIO
from hardware.lid_controller import LidController


def create_gpio_hardware(settings, pins: Optional[PinIO] = None) -> Tuple[GpioLockerManager, Optional[LidController]]:
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

    lid = None
    if settings.LID_ENABLED:
        lid = LidController(
            pins,
            pul_pin=settings.LID_PUL_PIN,
            dir_pin=settings.LID_DIR_PIN,
            home_pin=settings.LID_HOME_PIN,
            end_pin=settings.LID_END_PIN,
            step_active_low=settings.LID_STEP_ACTIVE_LOW,
            limit_active_low=settings.LID_LIMIT_ACTIVE_LOW,
            open_dir_high=settings.LID_OPEN_DIR_HIGH,
            steps_per_sec=settings.LID_STEPS_PER_SEC,
            max_steps=settings.LID_MAX_STEPS,
        ).start()
    return locker, lid
