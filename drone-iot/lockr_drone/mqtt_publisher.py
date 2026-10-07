"""Gửi telemetry lên broker MQTT (MQTT 5, TLS), tự kết nối lại.

Topic:
    lockr/drones/{droneId}/telemetry   QoS 0, KHÔNG retained — bản tin cũ không có giá trị
    lockr/drones/{droneId}/status      QoS 1, retained — online/offline, kèm thời điểm

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
from .signing import SIGNATURE_PROPERTY, sign

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

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("Broker từ chối kết nối: %s", reason_code)
            return
        self.connected = True
        log.info("Đã kết nối broker %s:%d", self.settings.mqtt_host, self.settings.mqtt_port)
        online = status_payload(self.settings.drone_id, "online")
        client.publish(self._status_topic, online, qos=1, retain=True,
                       properties=self._signed(self._status_topic, online))

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
