#!/usr/bin/env python3
"""Tủ giả qua Bluetooth (F3-G11) — thử mở ô bằng điện thoại thật khi chưa có tủ.

Chạy đúng `BleGattService` của Pi (kiểm chữ ký Ed25519 bằng khoá công khai tải từ backend, kiểm
tủ, hạn, chống dùng lại) trên laptop có Bluetooth: Windows, macOS hoặc Linux. Chỉ khác Pi ở chỗ
không có relay — "mở ô" chỉ in ra màn hình. App gửi mã thật xin từ production, nên chạy được ở
đây thì cắm sang tủ thật cũng chạy (tủ thật chỉ thêm tên quảng bá `LOCKR_<mã tủ>`).

Cài (một lần, Python ≥ 3.11, máy không cần uv):
    python -m venv .venv-ble
    .venv-ble\\Scripts\\pip install "bless==0.2.6" "bleak<1" "cryptography>=43"   # Windows
    .venv-ble/bin/pip install "bless==0.2.6" "bleak<1" "cryptography>=43"        # macOS/Linux

Chạy (tủ CAB-TU01 thật là lockerId 7, 8 ô):
    .venv-ble\\Scripts\\python simulate_ble_cabinet.py --locker-id 7 --locker-code CAB-TU01 --boxes 8
    .venv-ble\\Scripts\\python simulate_ble_cabinet.py              # nhận mã của tủ bất kỳ

Windows không cho đặt tên quảng bá: app nhận ra tủ giả theo service UUID, nên chỉ bật một
tủ giả mỗi lần. Hợp đồng: docs/01-overview/ble-open-contract.md.
"""
import argparse
import hashlib
import os
import platform
import sys
import tempfile
import time

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from services import ble_open_token
from services.ble_gatt_service import SERVICE_UUID, BleGattService
from services.ble_open_token import PublicKeyStore, TokenError

DEFAULT_BACKEND = "https://api.locker-drone.tech"


class _SimCabinetState:
    """Đủ cho `BleGattService`: tủ Pi đang phục vụ."""

    def __init__(self, locker_id):
        self.primary_locker_id = locker_id

    def get_cabinet_by_id(self, _locker_id):
        return {"slaveId": 1}


class _SimHardware:
    """Relay giả: in ô được mở, có thể ép lỗi từng ô để thử nhánh HARDWARE_ERROR."""

    def __init__(self, boxes: int, fail_boxes):
        self.boxes = boxes
        self.fail_boxes = set(fail_boxes)

    def ble_open_slot(self, slot: int) -> dict:
        box = slot + 1
        if box > self.boxes:
            return {"result": "FAIL", "error": "INVALID_SLOT"}
        if box in self.fail_boxes:
            return {"result": "FAIL", "error": "HARDWARE_ERROR"}
        print(f"\n  >>> [RELAY GIẢ] MỞ Ô {box}  ({time.strftime('%H:%M:%S')})\n", flush=True)
        return {"result": "OK"}


class SimBleGattService(BleGattService):
    """`BleGattService` chạy được ngoài Pi; không có `--locker-id` thì nhận mã của tủ bất kỳ."""

    def __init__(self, *args, any_locker: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.any_locker = any_locker

    @staticmethod
    def hardware_available() -> bool:
        return True

    def handle_token(self, data: bytes) -> dict:
        try:
            token = ble_open_token.decode(bytes(data))
            print(f"  <<< Nhận mã: tủ #{token.locker_id}, ô {token.box_number}, tài khoản #{token.user_id}, "
                  f"còn {token.expires_at - int(time.time())} s, nonce {token.nonce}", flush=True)
            if self.any_locker:
                self.cabinet_state.primary_locker_id = token.locker_id
        except TokenError as e:
            print(f"  <<< Nhận {len(data)} byte không phải mã mở ô: {e.code}", flush=True)
        reply = super().handle_token(data)
        print(f"  --> Trả app: {reply}", flush=True)
        return reply


def _patch_bless_windows():
    """bless trên Windows luôn mở adapter qua `pysetupdi` (không có trên PyPI) chỉ để đổi tên
    quảng bá — tủ giả không đổi tên nên thay bằng adapter rỗng."""
    if platform.system() != "Windows":
        return
    try:
        import pysetupdi  # noqa: F401
        return
    except ImportError:
        pass
    import types
    sys.modules["pysetupdi"] = types.SimpleNamespace(devices=lambda _guid: [])
    try:
        import bless.backends.winrt.server as winrt_server
    except ImportError:
        return

    class _NoRenameAdapter:
        def set_local_name(self, _name):
            pass

    winrt_server.BLEAdapter = _NoRenameAdapter


def _fingerprint(store: PublicKeyStore) -> str:
    if store.key is None:
        return "-"
    return hashlib.sha256(store.key.public_bytes(Encoding.Raw, PublicFormat.Raw)).hexdigest()[:16]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # console Windows mặc định cp1252
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Tủ giả mở ô qua Bluetooth (F3-G11)")
    parser.add_argument("--locker-id", type=int, default=None,
                        help="lockerId tủ giả đóng vai; bỏ trống = nhận mã của tủ bất kỳ")
    parser.add_argument("--locker-code", default=os.getenv("LOCKER_CODE", "CAB-TU01"),
                        help="mã tủ, để đặt tên LOCKR_<mã tủ> (Linux/macOS)")
    parser.add_argument("--boxes", type=int, default=24, help="số ô có relay (ô lớn hơn ⇒ INVALID_SLOT)")
    parser.add_argument("--fail-box", type=int, action="append", default=[],
                        help="ô giả lỗi phần cứng (lặp lại được)")
    parser.add_argument("--backend", default=os.getenv("BACKEND_API_URL") or DEFAULT_BACKEND)
    parser.add_argument("--public-key", default=os.getenv("BLE_PUBLIC_KEY", ""),
                        help="khoá công khai base64, bỏ qua tải từ backend")
    args = parser.parse_args()

    key_file = os.path.join(tempfile.gettempdir(), "lockr_ble_public_key.sim.json")
    store = PublicKeyStore(key_file, args.backend, env_key=args.public_key)
    if not store.refresh():
        print(f"[!] Không tải được khoá công khai từ {args.backend}/api/iot/ble/public-key — "
              "mọi mã sẽ bị từ chối NO_KEY", file=sys.stderr)

    name = f"LOCKR_{args.locker_code}"
    service = SimBleGattService(
        _SimCabinetState(args.locker_id), _SimHardware(args.boxes, args.fail_box), store,
        name_fn=lambda: name, any_locker=args.locker_id is None)

    print("=" * 66)
    print("  LOCK.R — TỦ GIẢ MỞ Ô QUA BLUETOOTH")
    print("=" * 66)
    print(f"  Hệ điều hành : {platform.system()} {platform.release()}")
    print(f"  Tên          : {name}" + ("  (Windows quảng bá tên máy, app tìm theo UUID)"
                                         if platform.system() == "Windows" else ""))
    print(f"  Service UUID : {SERVICE_UUID}")
    print(f"  Tủ           : {'#' + str(args.locker_id) if args.locker_id else 'bất kỳ (nhận theo mã)'}")
    print(f"  Khoá         : {'có' if store.key else 'CHƯA CÓ'} (sha256 {_fingerprint(store)}) từ {args.backend}")
    print("=" * 66)

    _patch_bless_windows()
    if not service.start():
        print("[!] Không bật được GATT server: máy không có Bluetooth, chưa cài bless, hoặc adapter "
              "không hỗ trợ vai peripheral.", file=sys.stderr)
        return 1
    print("\n[*] Đang chờ điện thoại (Ctrl+C để dừng). App: Hồ sơ → Thử mở ô qua Bluetooth → chọn ô → Mở.\n",
          flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
    print("[*] Đã dừng.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
