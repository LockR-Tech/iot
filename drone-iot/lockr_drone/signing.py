"""Chữ ký bản tin MQTT của drone.

Broker hiện là broker công khai: ai cũng ghi được vào topic, nên backend chỉ tin bản tin có
chữ ký đúng. Mỗi drone giữ một khoá riêng (DEVICE_KEY) do backend suy ra từ khoá gốc:

    deviceKey = hex(HMAC-SHA256(khoá gốc, "lockr-drone:" + droneId))
    sig       = hex(HMAC-SHA256(deviceKey, topic + "\n" + payload))

`sig` đi kèm bản tin dưới dạng MQTT 5 user property. Đổi công thức ⇒ sửa
`DroneTelemetrySigner.java` ở backend/order-service và test hai phía.
"""
import hashlib
import hmac

KEY_CONTEXT = "lockr-drone:"
SIGNATURE_PROPERTY = "sig"


def _hmac_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def device_key(master_secret: str, drone_id: str) -> str:
    return _hmac_hex(master_secret, KEY_CONTEXT + drone_id)


def sign(key: str, topic: str, payload: str) -> str:
    return _hmac_hex(key, topic + "\n" + payload)
