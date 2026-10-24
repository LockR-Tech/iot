"""Mã mở ô qua Bluetooth (F3-G11) — giải và kiểm mã backend ký (iot-service `BleTokenSigner`).

Mã 91 byte, big-endian (docs/01-overview/ble-open-contract.md):

    0       phiên bản = 1
    1–4     lockerId (u32)
    5–6     boxNumber (u16)
    7–14    userId (u64)
    15–18   hết hạn, giây Unix (u32)
    19–26   nonce 8 byte
    27–90   chữ ký Ed25519 trên byte 0–26

Pi chỉ giữ khoá công khai: lộ thẻ nhớ cũng không tự làm ra mã mở được. Mã chỉ dùng một lần
(nhớ nonce tới khi hết hạn) và chỉ mở được đúng tủ Pi đang phục vụ.
"""
import base64
import json
import os
import struct
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from utils.logger import get_logger

logger = get_logger("BleOpenToken")

VERSION = 1
PAYLOAD_LENGTH = 27
SIGNATURE_LENGTH = 64
TOKEN_LENGTH = PAYLOAD_LENGTH + SIGNATURE_LENGTH
_PAYLOAD = struct.Struct(">BIHQI8s")

# Đồng hồ Pi có thể lệch vài giây so với máy chủ.
CLOCK_SKEW_S = 30
# Backend cấp mã sống 120 s; mã hết hạn xa hơn mức này là bất thường ⇒ từ chối.
MAX_LIFETIME_S = 600


class TokenError(Exception):
    """`code` gửi lại cho app (BAD_FORMAT, BAD_SIGNATURE, EXPIRED, WRONG_LOCKER, REPLAYED, NO_KEY…)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OpenToken:
    locker_id: int
    box_number: int
    user_id: int
    expires_at: int
    nonce: str  # hex — app đối chiếu kết quả theo nonce


def decode(data: bytes) -> OpenToken:
    """Giải phần dữ liệu (chưa kiểm chữ ký)."""
    if len(data) != TOKEN_LENGTH:
        raise TokenError("BAD_FORMAT", f"Mã phải dài {TOKEN_LENGTH} byte, nhận {len(data)}")
    version, locker_id, box_number, user_id, expires_at, nonce = _PAYLOAD.unpack(data[:PAYLOAD_LENGTH])
    if version != VERSION:
        raise TokenError("BAD_VERSION", f"Phiên bản mã {version} chưa hỗ trợ")
    return OpenToken(locker_id, box_number, user_id, expires_at, nonce.hex())


class ReplayGuard:
    """Nhớ nonce đã dùng tới khi mã hết hạn (trong RAM — mã chỉ sống 2 phút)."""

    def __init__(self):
        self._seen: Dict[str, int] = {}
        self._lock = threading.Lock()

    def claim(self, token: OpenToken, now: float) -> bool:
        with self._lock:
            for nonce, exp in list(self._seen.items()):
                if exp + CLOCK_SKEW_S < now:
                    del self._seen[nonce]
            if token.nonce in self._seen:
                return False
            self._seen[token.nonce] = token.expires_at
            return True


def verify(data: bytes, public_key: Optional[Ed25519PublicKey], expected_locker_id: Optional[int],
           replay: ReplayGuard, now: Optional[float] = None) -> OpenToken:
    """Kiểm đủ mọi điều kiện; sai bất kỳ điều gì ⇒ `TokenError`. Đúng ⇒ đánh dấu nonce đã dùng."""
    now = time.time() if now is None else now
    token = decode(bytes(data))
    if public_key is None:
        raise TokenError("NO_KEY", "Tủ chưa tải được khoá kiểm mã từ máy chủ")
    try:
        public_key.verify(bytes(data[PAYLOAD_LENGTH:]), bytes(data[:PAYLOAD_LENGTH]))
    except InvalidSignature:
        raise TokenError("BAD_SIGNATURE", "Chữ ký mã không hợp lệ")
    if expected_locker_id is None:
        raise TokenError("NOT_CONFIGURED", "Pi chưa được gán vào tủ nào")
    if token.locker_id != int(expected_locker_id):
        raise TokenError("WRONG_LOCKER", f"Mã dành cho tủ {token.locker_id}, không phải tủ này")
    if token.expires_at + CLOCK_SKEW_S < now:
        raise TokenError("EXPIRED", "Mã đã hết hạn — xin mã mới")
    if token.expires_at - now > MAX_LIFETIME_S:
        raise TokenError("BAD_EXPIRY", "Hạn của mã không hợp lệ (đồng hồ tủ lệch?)")
    if not replay.claim(token, now):
        raise TokenError("REPLAYED", "Mã này đã dùng rồi")
    return token


class PublicKeyStore:
    """Khoá công khai kiểm mã: lấy từ `.env` (BLE_PUBLIC_KEY) nếu có, không thì tải
    `{backend}/api/iot/ble/public-key` qua HTTPS và lưu ra file để vẫn mở được khi mất mạng."""

    def __init__(self, path: str, base_url: str, env_key: str = "",
                 fetch: Optional[Callable[[str], dict]] = None):
        self.path = path
        self.base_url = base_url.rstrip("/")
        self._env_key = env_key.strip()
        self._fetch = fetch or _http_get_json
        self._key: Optional[Ed25519PublicKey] = None
        self._raw: Optional[str] = None
        self._lock = threading.Lock()
        if self._env_key:
            self._set(self._env_key)
        else:
            self._load_file()

    @property
    def key(self) -> Optional[Ed25519PublicKey]:
        return self._key

    def refresh(self) -> bool:
        """Tải khoá mới nhất; giữ khoá cũ nếu lỗi. True khi có khoá dùng được."""
        if self._env_key:
            return self._key is not None
        url = f"{self.base_url}/api/iot/ble/public-key"
        try:
            body = self._fetch(url)
            data = body.get("data", body)
            raw = str(data["publicKey"])
            with self._lock:
                changed = raw != self._raw
                self._set(raw)
                if changed:
                    self._save(raw)
                    logger.info("BLE open-token public key updated from backend")
        except Exception as e:
            logger.warning(f"Không tải được khoá kiểm mã BLE từ {url}: {e}")
        return self._key is not None

    def _set(self, raw_b64: str):
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(raw_b64))
        self._key, self._raw = key, raw_b64

    def _load_file(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self._set(json.load(f)["publicKey"])
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Bỏ qua file khoá BLE hỏng {self.path}: {e}")

    def _save(self, raw_b64: str):
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"algorithm": "Ed25519", "publicKey": raw_b64}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.error(f"Không lưu được khoá BLE vào {self.path}: {e}")


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        return json.loads(resp.read().decode("utf-8"))
