"""Mở ô qua Bluetooth (F3-G11) — GATT server trên Pi cho app khách được admin cấp quyền thử.

App xin backend một mã mở ô (ký Ed25519, sống 2 phút), kết nối BLE tới Pi tên `LOCKR_<mã tủ>`, ghi
91 byte mã vào characteristic OPEN rồi chờ Pi báo kết quả (JSON) trên characteristic RESULT
(notify, đọc được). Pi kiểm chữ ký bằng khoá công khai tải từ backend nên lúc mở không cần mạng.

GATT server cũng phát quảng bá cùng tên `LOCKR_<mã tủ>` như `BleAdvertiserService`, nên khi chạy
được thì không cần bật quảng bá kiểu cũ (main.py). Hợp đồng: docs/01-overview/ble-open-contract.md.
"""
import asyncio
import json
import os
import platform
import threading
from typing import Callable, Optional

from services import ble_open_token
from services.ble_open_token import PublicKeyStore, ReplayGuard, TokenError
from utils.logger import get_logger

logger = get_logger("BleGatt")

SERVICE_UUID = "06776521-9674-432d-b886-5e951244ffa3"
OPEN_CHAR_UUID = "54af4ad7-dcde-47dc-ad44-fa2c7438dd36"
RESULT_CHAR_UUID = "8a6e33a5-1b26-4e80-8faa-026bab93ad4e"

START_TIMEOUT_S = 15
KEY_REFRESH_S = 6 * 3600
KEY_RETRY_S = 60


class BleGattService:
    def __init__(self, cabinet_state, hardware, key_store: PublicKeyStore,
                 name_fn: Callable[[], str], control_log=None):
        self.cabinet_state = cabinet_state
        self.hardware = hardware
        self.key_store = key_store
        self._name_fn = name_fn
        self.control_log = control_log
        self.replay = ReplayGuard()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._key_thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._stop = threading.Event()
        self._error: Optional[str] = None
        self._open_lock = threading.Lock()

    # ─── xử lý mã (không phụ thuộc BLE — test được) ───

    def handle_token(self, data: bytes) -> dict:
        """Kiểm mã rồi kích relay. Trả JSON gửi lại app: nonce, result OK/FAIL, box, error."""
        nonce = None
        try:
            nonce = ble_open_token.decode(bytes(data)).nonce
        except TokenError:
            pass
        try:
            token = ble_open_token.verify(data, self.key_store.key, self.cabinet_state.primary_locker_id,
                                          self.replay)
        except TokenError as e:
            logger.warning(f"BLE open rejected: {e.code} — {e.message}")
            if nonce is not None and e.code not in ("BAD_FORMAT", "BAD_VERSION"):
                self._log("BLE_REJECTED", "FAILED", None, f"Từ chối mã mở ô qua Bluetooth: {e.code}")
            return {"nonce": nonce, "result": "FAIL", "error": e.code}

        slot = token.box_number - 1
        with self._open_lock:
            try:
                result = self._open(slot)
            except Exception as e:
                logger.error(f"BLE open box {token.box_number} failed: {e}")
                result = {"result": "FAIL", "error": "HARDWARE_ERROR"}
        ok = result.get("result") == "OK"
        error = None if ok else str(result.get("error") or "OPEN_FAILED")
        message = f"Mở ô {token.box_number} qua Bluetooth (tài khoản #{token.user_id})"
        if not ok:
            message += f" — lỗi {error}"
        self._log("BOX_OPEN_BLE", "OK" if ok else "FAILED", slot, message)
        logger.warning(message + (" → OK" if ok else ""))
        reply = {"nonce": token.nonce, "result": "OK" if ok else "FAIL", "box": token.box_number}
        if error:
            reply["error"] = error
        return reply

    def _open(self, slot: int) -> dict:
        ble_open = getattr(self.hardware, "ble_open_slot", None)
        if ble_open:
            return ble_open(slot)
        cabinet = self.cabinet_state.get_cabinet_by_id(str(self.cabinet_state.primary_locker_id)) or {}
        return self.hardware.open_slot(slot, slave_id=int(cabinet.get("slaveId") or 1))

    def _log(self, action: str, status: str, slot: Optional[int], message: str):
        if self.control_log:
            try:
                self.control_log.record(action, status, slot_index=slot, message=message)
            except Exception as e:
                logger.error(f"Control log {action} failed: {e}")

    # ─── khoá công khai ───

    def _key_loop(self):
        while not self._stop.is_set():
            ok = self.key_store.refresh()
            self._stop.wait(KEY_REFRESH_S if ok else KEY_RETRY_S)

    # ─── GATT server ───

    @staticmethod
    def hardware_available() -> bool:
        return (platform.system() == "Linux" and os.path.exists("/sys/class/bluetooth")
                and len(os.listdir("/sys/class/bluetooth")) > 0)

    def start(self) -> bool:
        """Chạy GATT server ở luồng riêng. False khi máy không có Bluetooth/bless hoặc BlueZ từ chối —
        main.py khi đó dùng quảng bá kiểu cũ để app vẫn nhận ra tủ ở gần."""
        self._stop.clear()
        self._key_thread = threading.Thread(target=self._key_loop, daemon=True, name="BleKey")
        self._key_thread.start()
        if not self.hardware_available():
            logger.info("[BLE GATT] Không có Bluetooth (hci) — bỏ qua GATT server mở ô")
            return False
        try:
            import bless  # noqa: F401
        except ImportError:
            logger.warning("[BLE GATT] Thiếu thư viện bless (uv sync) — bỏ qua GATT server mở ô")
            return False
        self._started.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="BleGatt")
        self._thread.start()
        self._started.wait(START_TIMEOUT_S)
        if not self._started.is_set() or self._error:
            logger.warning(f"[BLE GATT] Không khởi động được: {self._error or 'quá thời gian chờ'}")
            self._stop.set()
            return False
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception as e:
            self._error = str(e) or type(e).__name__
            logger.error(f"[BLE GATT] Lỗi: {self._error}")
        finally:
            self._started.set()
            loop.close()

    async def _serve(self):
        from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties

        name = self._name_fn()
        server = BlessServer(name=name, loop=asyncio.get_running_loop())
        server.read_request_func = self._on_read
        server.write_request_func = self._on_write
        await server.add_new_service(SERVICE_UUID)
        await server.add_new_characteristic(
            SERVICE_UUID, OPEN_CHAR_UUID, GATTCharacteristicProperties.write, None,
            GATTAttributePermissions.writeable)
        await server.add_new_characteristic(
            SERVICE_UUID, RESULT_CHAR_UUID,
            GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
            bytearray(b"{}"), GATTAttributePermissions.readable)
        await server.start()
        self._server = server
        logger.info(f"[OK] BLE GATT server mở ô đang chạy: '{name}' (service {SERVICE_UUID})")
        self._started.set()
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
        try:
            await server.stop()
        except Exception as e:
            logger.debug(f"BLE GATT stop: {e}")
        self._server = None

    def _on_read(self, characteristic, **kwargs) -> bytearray:
        return characteristic.value or bytearray()

    def _on_write(self, characteristic, value, **kwargs):
        if str(characteristic.uuid).lower() != OPEN_CHAR_UUID:
            return
        data = bytes(value)
        # Kích relay mất vài giây — không chặn vòng sự kiện D-Bus.
        threading.Thread(target=self._process, args=(data,), daemon=True, name="BleOpen").start()

    def _process(self, data: bytes):
        reply = self.handle_token(data)
        payload = json.dumps({k: v for k, v in reply.items() if v is not None},
                             separators=(",", ":")).encode("utf-8")
        loop = self._loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._publish, payload)

    def _publish(self, payload: bytes):
        server = self._server
        if server is None:
            return
        characteristic = server.get_characteristic(RESULT_CHAR_UUID)
        if characteristic is None:
            return
        characteristic.value = bytearray(payload)
        server.update_value(SERVICE_UUID, RESULT_CHAR_UUID)

