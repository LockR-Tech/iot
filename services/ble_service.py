"""Locker BLE Advertising Service for Raspberry Pi & Simulation.

Phát sóng BLE Beacon / Advertising để Mobile App phát hiện tủ ở cự ly gần (<2m).
Tích hợp vào main.py chạy tự động trên Raspberry Pi hoặc chế độ mô phỏng.
"""

import os
import platform
import subprocess
import threading
from typing import Optional
from config.settings import settings
from utils.logger import get_logger

logger = get_logger("BleService")


class BleAdvertiserService:
    """Quản lý phát sóng BLE Advertising định danh tủ (LOCKR_<MãTủ>)"""

    def __init__(self, cabinet_state=None, device_name: Optional[str] = None):
        self.cabinet_state = cabinet_state
        self._custom_name = device_name
        self._current_name: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._using_btmgmt = False
        self._is_running = False
        self._lock = threading.Lock()

        if self.cabinet_state:
            self.cabinet_state.add_listener(self._on_cabinet_changed)

    def resolve_device_name(self) -> str:
        """Xác định tên thiết bị phát sóng BLE."""
        if self._custom_name:
            return self._custom_name
        if settings.BLE_DEVICE_NAME:
            return settings.BLE_DEVICE_NAME

        # Lấy từ tủ đang phục vụ trong cabinet_state
        if self.cabinet_state and self.cabinet_state.is_configured:
            cabs = self.cabinet_state.all_cabinets
            if cabs:
                first_cab = cabs[0]
                code = first_cab.get("code") or first_cab.get("name") or str(first_cab.get("id"))
                return f"LOCKR_{code}"

        # Lấy từ settings
        if settings.LOCKER_CODE:
            return f"LOCKR_{settings.LOCKER_CODE}"
        if settings.LOCKER_ID:
            return f"LOCKR_{settings.LOCKER_ID}"

        return "LOCKR_CAB-TU01"

    def start(self):
        """Khởi động phát sóng BLE Advertising."""
        with self._lock:
            if not settings.BLE_ENABLED:
                logger.info("BLE advertising is disabled (BLE_ENABLED=false)")
                return

            dev_name = self.resolve_device_name()
            self._current_name = dev_name
            self._is_running = True

            sys_name = platform.system()
            has_bt_hardware = (
                sys_name == "Linux"
                and os.path.exists("/sys/class/bluetooth")
                and len(os.listdir("/sys/class/bluetooth")) > 0
            )

            if has_bt_hardware:
                logger.info(f"[*] Starting BLE Advertising on Linux/RPi for '{dev_name}'...")
                self._start_linux_advertising(dev_name)
            else:
                logger.info(
                    f"[*] [BLE SIMULATION] Virtual BLE beacon active: '{dev_name}' "
                    f"(System: {sys_name}, No hardware hci interface needed)"
                )

    def _start_linux_advertising(self, dev_name: str):
        """Cấu hình và phát sóng BLE trên Linux BlueZ (Raspberry Pi)."""
        # 1. Bỏ chặn rfkill nếu có
        try:
            subprocess.run(["sudo", "rfkill", "unblock", "bluetooth"], check=False, timeout=5)
        except Exception:
            pass

        # 2. Thử cách 1: btmgmt (chuẩn kernel Linux BlueZ, ổn định nhất)
        try:
            subprocess.run(["bluetoothctl", "power", "on"], check=False, timeout=5)
            subprocess.run(["sudo", "btmgmt", "-i", "hci0", "power", "off"], check=False, timeout=5)
            subprocess.run(["sudo", "btmgmt", "-i", "hci0", "le", "on"], check=False, timeout=5)
            subprocess.run(["sudo", "btmgmt", "-i", "hci0", "name", dev_name], check=False, timeout=5)
            subprocess.run(["sudo", "btmgmt", "-i", "hci0", "power", "on"], check=False, timeout=5)
            res = subprocess.run(["sudo", "btmgmt", "-i", "hci0", "advertising", "on"], check=False, timeout=5)
            if res.returncode == 0:
                self._using_btmgmt = True
                logger.info(f"[OK] BLE Advertising broadcasting via btmgmt: '{dev_name}'")
                return
        except Exception as ex:
            logger.debug(f"btmgmt not available or failed: {ex}")

        # 3. Thử cách 2: Persistent bluetoothctl subprocess
        # BlueZ unregisters advertisement if the D-Bus client quits,
        # so we keep the bluetoothctl process open.
        try:
            self._proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            cmd = f"power on\nsystem-alias {dev_name}\nadvertise on\n"
            if self._proc.stdin:
                self._proc.stdin.write(cmd)
                self._proc.stdin.flush()
            logger.info(f"[OK] BLE Advertising broadcasting via persistent bluetoothctl: '{dev_name}'")
        except Exception as ex:
            logger.warning(f"[!] Could not start BLE advertising via bluetoothctl: {ex}")

    def stop(self):
        """Dừng phát sóng BLE Advertising."""
        with self._lock:
            if not self._is_running:
                return

            self._is_running = False
            logger.info(f"Stopping BLE Advertising for '{self._current_name}'...")

            if self._proc:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.write("advertise off\nquit\n")
                        self._proc.stdin.flush()
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
                self._proc = None

            if self._using_btmgmt:
                try:
                    subprocess.run(["sudo", "btmgmt", "-i", "hci0", "advertising", "off"], check=False, timeout=3)
                except Exception:
                    pass
                self._using_btmgmt = False

            logger.info("BLE Advertising stopped.")

    def _on_cabinet_changed(self):
        """Tự động đổi tên phát sóng BLE nếu tủ được gán lại."""
        if not self._is_running:
            return
        new_name = self.resolve_device_name()
        if new_name != self._current_name:
            logger.info(f"Cabinet identity changed: '{self._current_name}' -> '{new_name}'. Restarting BLE...")
            self.stop()
            self.start()
