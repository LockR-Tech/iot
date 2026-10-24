"""Gửi telemetry lên broker MQTT (MQTT 5, TLS), tự kết nối lại.

Topic:
    lockr/drones/{droneId}/telemetry   QoS 0, KHÔNG retained — bản tin cũ không có giá trị
    lockr/drones/{droneId}/status      QoS 1, retained — online/offline, kèm thời điểm
    lockr/drone/delivery/commands      QoS 1, nhận lệnh điều phối giao hàng (MARKER_VALIDATED, LID_OPENED, v.v.)
    lockr/drone/delivery/events        QoS 1, gửi sự kiện nhận diện (MARKER_DETECTED)

Pi mất mạng hoặc tắt đột ngột thì broker tự phát `offline` (Last Will). Telemetry không
gửi bù: mất kết nối lúc nào thì bản tin lúc đó bị bỏ, có mạng lại là gửi số đo hiện tại.
"""
import json
import logging
import ssl
import time

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from .config import Settings
from .signing import SIGNATURE_PROPERTY, sign, verify

log = logging.getLogger("mqtt")


def telemetry_topic(drone_id: str) -> str:
    return f"lockr/drones/{drone_id}/telemetry"


def status_topic(drone_id: str) -> str:
    return f"lockr/drones/{drone_id}/status"


def status_payload(drone_id: str, state: str, at_ms: int | None = None) -> str:
    # `at` để người nhận bản retained biết trạng thái này có từ lúc nào.
    return json.dumps({
        "schemaVersion": 1,
        "droneId": drone_id,
        "state": state,
        "at": int(time.time() * 1000) if at_ms is None else at_ms,
    })


class MqttPublisher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.connected = False
        self._stopping = False
        self._telemetry_topic = telemetry_topic(settings.drone_id)
        self._status_topic = status_topic(settings.drone_id)
        self._delivery_commands_topic = "lockr/drone/delivery/commands"
        self._delivery_events_topic = "lockr/drone/delivery/events"
        self.delivery_command_listener = None

        transport = "websockets" if settings.mqtt_transport == "websockets" else "tcp"
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"lockr-drone-{settings.drone_id}",
            protocol=mqtt.MQTTv5,
            transport=transport,
        )
        if transport == "websockets":
            self.client.ws_set_options(path=settings.mqtt_ws_path)
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        if settings.mqtt_tls:
            self.client.tls_set(ca_certs=settings.mqtt_ca_certs or None, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        will = status_payload(settings.drone_id, "offline")
        self.client.will_set(
            self._status_topic, will, qos=1, retain=True,
            properties=self._signed(self._status_topic, will, PacketTypes.WILLMESSAGE),
        )

    def _signed(self, topic: str, payload: str, packet_type: int = PacketTypes.PUBLISH) -> Properties | None:
        if not self.settings.device_key:
            return None
        properties = Properties(packet_type)
        properties.UserProperty = (SIGNATURE_PROPERTY, sign(self.settings.device_key, topic, payload))
        return properties

    def _command_is_signed(self, topic: str, payload: str, properties) -> bool:
        """Lệnh từ backend có chữ ký đúng khoá của drone này. Chưa cấu hình DEVICE_KEY thì không kiểm được ⇒ từ chối."""
        if not self.settings.device_key:
            return False
        user_properties = dict(getattr(properties, "UserProperty", None) or [])
        return verify(self.settings.device_key, topic, payload, user_properties.get(SIGNATURE_PROPERTY))

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("Broker từ chối kết nối: %s", reason_code)
            return
        self.connected = True
        log.info("Đã kết nối broker %s:%d", self.settings.mqtt_host, self.settings.mqtt_port)
        online = status_payload(self.settings.drone_id, "online")
        client.publish(self._status_topic, online, qos=1, retain=True,
                       properties=self._signed(self._status_topic, online))
        self.publish_discovery()

        # Subscribe topic nhận lệnh điều phối giao hàng (mở nắp tủ, ArUco validation)
        try:
            client.subscribe(self._delivery_commands_topic, qos=1)
            log.info("Đã subscribe topic nhận lệnh giao hàng: %s", self._delivery_commands_topic)
        except Exception as e:
            log.warning("Không thể subscribe topic lệnh %s: %s", self._delivery_commands_topic, e)

    def _on_message(self, client, userdata, message):
        try:
            if message.topic == self._delivery_commands_topic:
                payload_str = message.payload.decode("utf-8")
                # Broker có thể là broker công khai: chỉ làm theo lệnh backend ký bằng khoá của drone này.
                if not self._command_is_signed(message.topic, payload_str, message.properties):
                    log.warning("Bỏ lệnh giao hàng không có chữ ký hợp lệ")
                    return
                data = json.loads(payload_str)
                drone_id = data.get("droneId")
                # Nếu backend gửi đích danh droneId, chỉ xử lý nếu đúng drone
                if drone_id and drone_id != self.settings.drone_id:
                    return
                log.info("Nhận lệnh giao hàng từ backend: %s", data.get("eventType"))
                if self.delivery_command_listener:
                    self.delivery_command_listener(data)
        except Exception as e:
            log.warning("Lỗi xử lý tin nhắn MQTT: %s", e)

    def publish_delivery_event(self, payload: dict) -> bool:
        """Gửi sự kiện phát hiện marker ArUco lên backend để kích hoạt mở nắp tủ."""
        if not self.connected:
            return False
        try:
            body = json.dumps(payload, ensure_ascii=False)
            # Backend chỉ nhận sự kiện có chữ ký của đúng drone (messageId + timestamp chống phát lại).
            info = self.client.publish(self._delivery_events_topic, body, qos=1,
                                       properties=self._signed(self._delivery_events_topic, body))
            return info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            log.warning("Lỗi gửi delivery event lên MQTT: %s", e)
            return False

    def publish_discovery(self) -> None:
        """Tự động phát bản tin discovery lên hệ thống giống Kiosk để backend nhận diện Pi tự động."""
        try:
            from datetime import datetime, timezone
            from .telemetry import get_pi_info
            pi = get_pi_info()
            mac = pi.get("macAddress")
            if not mac or not self.connected:
                return
            disc_topic = f"iot/{mac}/discovery/result"
            payload = json.dumps({
                "macAddress": mac,
                "firmwareVersion": pi.get("firmware", "LockR-Drone v1.0"),
                "hardware": "drone",
                "ipAddress": pi.get("ipAddress"),
                "cameraStreamUrl": pi.get("cameraStreamUrl"),
                "slaves": [],
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            self.client.publish(disc_topic, payload, qos=1)
            log.info("Đã gửi bản tin discovery cho Pi (%s) tới topic %s", mac, disc_topic)
        except Exception as e:
            log.warning("Không thể gửi discovery: %s", e)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        if self._stopping:
            log.info("Đã ngắt kết nối broker")
        else:
            log.warning("Mất kết nối broker (%s) — tự kết nối lại", reason_code)

    def start(self) -> None:
        """Không chặn: mạng chưa lên lúc khởi động thì paho tự thử lại ở luồng nền."""
        self.client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=30)
        self.client.loop_start()

    def publish_telemetry(self, payload: dict) -> bool:
        if not self.connected:
            return False
        body = json.dumps(payload, ensure_ascii=False)
        info = self.client.publish(self._telemetry_topic, body, qos=0, retain=False,
                                   properties=self._signed(self._telemetry_topic, body))
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def stop(self) -> None:
        """Tắt có chủ đích: tự báo offline (Last Will chỉ phát khi rớt đột ngột)."""
        self._stopping = True
        try:
            if self.connected:
                offline = status_payload(self.settings.drone_id, "offline")
                self.client.publish(self._status_topic, offline, qos=1, retain=True,
                                    properties=self._signed(self._status_topic, offline)).wait_for_publish(3)
            self.client.disconnect()
        finally:
            self.client.loop_stop()
