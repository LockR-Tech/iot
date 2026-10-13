"""
Truy cập chân GPIO của Raspberry Pi qua libgpiod (gói `gpiod` 2.x).

Dùng character device `/dev/gpiochipN` nên chạy được trên Pi 5 (chip RP1) lẫn
Pi 4; thư viện `RPi.GPIO` cũ không chạy trên Pi 5. Số chân là số **BCM**
(GPIO17…), không phải số chân vật lý trên header.

Mỗi thành phần (khoá, nắp trượt) xin một nhóm chân riêng bằng `claim()`;
một chân chỉ thuộc một nhóm — kernel từ chối nếu tiến trình khác (ví dụ
`main.py` đang chạy) đã giữ chân đó.
"""
import glob
import threading
from typing import Dict, Iterable, Optional, Protocol

from utils.logger import get_logger

logger = get_logger("GPIO")

# BCM → số chân vật lý trên header 40 chân.
HEADER_PIN = {
    0: 27, 1: 28, 2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23, 12: 32,
    13: 33, 14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38, 21: 40, 22: 15, 23: 16,
    24: 18, 25: 22, 26: 37, 27: 13,
}
# Chân nguồn của header (không phải GPIO).
POWER_PINS = {1: "3V3", 17: "3V3", 2: "5V", 4: "5V",
              6: "GND", 9: "GND", 14: "GND", 20: "GND", 25: "GND", 30: "GND", 34: "GND", 39: "GND"}

# Nhãn chip điều khiển header 40 chân trên từng đời Pi.
_HEADER_CHIP_LABELS = ("pinctrl-rp1", "pinctrl-bcm2711", "pinctrl-bcm2835")


class PinGroup(Protocol):
    def write(self, pin: int, high: bool) -> None: ...
    def read(self, pin: int) -> bool: ...
    def release(self) -> None: ...


class PinIO(Protocol):
    def claim(self, outputs: Dict[int, bool], inputs: Iterable[int], pull_up: bool = True) -> PinGroup:
        """outputs: {chân: mức ban đầu (True = HIGH)} · inputs: các chân đọc."""
        ...


def find_header_chip() -> str:
    """Tìm /dev/gpiochipN điều khiển header 40 chân (Pi 5 đánh số khác Pi 4)."""
    import gpiod

    for path in sorted(glob.glob("/dev/gpiochip*")):
        try:
            with gpiod.Chip(path) as chip:
                label = chip.get_info().label
        except OSError:
            continue
        if label in _HEADER_CHIP_LABELS:
            return path
    raise RuntimeError(
        f"Không tìm thấy gpiochip của header 40 chân (cần nhãn {', '.join(_HEADER_CHIP_LABELS)}). "
        "Máy này có phải Raspberry Pi không? Đặt GPIO_CHIP=/dev/gpiochipN để chỉ định."
    )


class _GpiodGroup:
    def __init__(self, request):
        self._request = request
        self._lock = threading.Lock()

    def write(self, pin: int, high: bool) -> None:
        from gpiod.line import Value
        with self._lock:
            self._request.set_value(pin, Value.ACTIVE if high else Value.INACTIVE)

    def read(self, pin: int) -> bool:
        from gpiod.line import Value
        with self._lock:
            return self._request.get_value(pin) == Value.ACTIVE

    def release(self) -> None:
        with self._lock:
            self._request.release()


class GpiodPins:
    """Chân GPIO thật qua libgpiod."""

    def __init__(self, chip_path: Optional[str] = None, consumer: str = "lockr"):
        self.chip_path = chip_path if chip_path and chip_path.upper() != "AUTO" else find_header_chip()
        self.consumer = consumer
        logger.info(f"GPIO chip: {self.chip_path}")

    def claim(self, outputs: Dict[int, bool], inputs: Iterable[int], pull_up: bool = True) -> PinGroup:
        import gpiod
        from gpiod.line import Bias, Direction, Value

        config = {}
        for pin, initial_high in outputs.items():
            config[pin] = gpiod.LineSettings(
                direction=Direction.OUTPUT,
                output_value=Value.ACTIVE if initial_high else Value.INACTIVE,
            )
        input_pins = tuple(inputs)
        if input_pins:
            config[input_pins] = gpiod.LineSettings(
                direction=Direction.INPUT,
                bias=Bias.PULL_UP if pull_up else Bias.PULL_DOWN,
            )
        request = gpiod.request_lines(self.chip_path, consumer=self.consumer, config=config)
        return _GpiodGroup(request)


class _FakeGroup:
    def __init__(self, owner: "FakePins", pins: Iterable[int]):
        self._owner = owner
        self._pins = set(pins)

    def write(self, pin: int, high: bool) -> None:
        self._owner.levels[pin] = high
        self._owner.history.append((pin, high))

    def read(self, pin: int) -> bool:
        return self._owner.levels[pin]

    def release(self) -> None:
        for pin in self._pins:
            self._owner.claimed.discard(pin)


class FakePins:
    """Chân giả cho test và máy không phải Pi. Test đặt mức đầu vào qua `levels`."""

    def __init__(self):
        self.levels: Dict[int, bool] = {}
        self.history = []          # [(chân, mức)] theo thứ tự ghi
        self.claimed = set()

    def claim(self, outputs: Dict[int, bool], inputs: Iterable[int], pull_up: bool = True) -> PinGroup:
        input_pins = list(inputs)
        pins = list(outputs) + input_pins
        busy = self.claimed.intersection(pins)
        if busy:
            raise OSError(f"Chân đã bị giữ: {sorted(busy)}")
        self.claimed.update(pins)
        for pin, initial_high in outputs.items():
            self.levels[pin] = initial_high
        for pin in input_pins:
            self.levels.setdefault(pin, pull_up)   # để hở ⇒ theo điện trở kéo
        return _FakeGroup(self, pins)
