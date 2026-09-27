import ssl
import threading
import paho.mqtt.client as mqtt
from config.settings import settings
from utils.logger import get_logger

logger = get_logger("MQTT_Client")


class MQTTClientWrapper:
    """
    Kết nối MQTT của Pi. Hợp đồng topic: docs/01-overview/mqtt-contract.md (ADR-0008).

    - Mọi topic đã subscribe được nhớ lại và subscribe lại sau mỗi lần kết nối lại
      (clean session: broker quên subscription khi mất kết nối).
    - Kết nối bất đồng bộ, paho tự thử lại — mạng chưa lên lúc khởi động cũng không sao.
    - Đã đặt mật khẩu thì không bao giờ gửi nó qua kết nối không mã hoá.
    """

    def __init__(self, on_message_callback=None):
        self.callback = on_message_callback
        # Gọi (trên luồng riêng) sau mỗi lần kết nối thành công — ví dụ báo discovery lại cho backend.
        self.on_connected = None
        self._connected = False
        self._topics: dict[str, int] = {}
        self._topics_lock = threading.Lock()
        self.client = None
        self.re_init()

    def _base_topics(self) -> dict:
        """Topic cấp phát theo MAC — luôn nghe, kể cả khi chưa được gán vào tủ nào."""
        mac = settings.MAC_ADDRESS
        return {
            f"iot/{mac}/command/setup": 1,
            f"iot/{mac}/command/clear-setup": 1,
            f"iot/{mac}/discovery/start": 1,
        }

    def re_init(self):
        """Khởi tạo hoặc cập nhật cấu hình client (ví dụ khi host/user thay đổi)."""
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

        transport = "websockets" if settings.MQTT_TRANSPORT == "websockets" else "tcp"
        # Client id = MAC viết liền (giống username trên broker riêng): cố định để dễ lần trong
        # log broker; hai tiến trình cùng MAC sẽ đá nhau ra.
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=settings.MAC_ADDRESS.replace(":", ""), transport=transport)
        if transport == "websockets":
            self.client.ws_set_options(path=settings.MQTT_WS_PATH)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=max(settings.MQTT_RECONNECT_INTERVAL, 30))

        # ─── MQTT Authentication ───
        if settings.MQTT_USERNAME:
            self.client.username_pw_set(
                settings.MQTT_USERNAME,
                settings.MQTT_PASSWORD
            )
            logger.info(f"MQTT auth configured for user: {settings.MQTT_USERNAME}")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self._connected = True
            logger.warning(f"🌐 MQTT Connected to Broker: {settings.MQTT_BROKER}")
            topics = {**self._base_topics(), **self._snapshot_topics()}
            client.subscribe(list(topics.items()))
            for topic in topics:
                logger.info(f"Subscribed: {topic}")
            logger.info(f"⏳ Waiting for setup command (MAC: {settings.MAC_ADDRESS})...")
            if self.on_connected:
                threading.Thread(target=self._run_on_connected, daemon=True, name="MqttOnConnected").start()
        else:
            logger.error(f"Connection failed, reason code: {reason_code}")

    def _run_on_connected(self):
        try:
            self.on_connected()
        except Exception as e:
            logger.error(f"on_connected hook failed: {e}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected = False
        if reason_code != 0:
            logger.warning(f"Unexpected disconnection (rc={reason_code}). Will auto-reconnect...")
        else:
            logger.info("Disconnected from broker")

    def _on_message(self, client, userdata, msg):
        try:
            payload_str = msg.payload.decode()
            logger.info(f"📩 Received on [{msg.topic}]: {payload_str}")
            if self.callback:
                self.callback(msg.topic, payload_str)
        except Exception as e:
            logger.error(f"Error handling message on {msg.topic}: {e}", exc_info=True)

    def _snapshot_topics(self) -> dict:
        with self._topics_lock:
            return dict(self._topics)

    @property
    def subscriptions(self) -> set:
        """Các topic động đang giữ (không gồm topic cấp phát theo MAC)."""
        return set(self._snapshot_topics())

    def subscribe(self, topic: str, qos: int = 1):
        with self._topics_lock:
            self._topics[topic] = qos
        if self._connected:
            self.client.subscribe(topic, qos=qos)
        logger.info(f"Subscribed (dynamic): {topic}")

    def unsubscribe(self, topic: str):
        with self._topics_lock:
            self._topics.pop(topic, None)
        if self._connected:
            self.client.unsubscribe(topic)
        logger.info(f"Unsubscribed: {topic}")

    def set_subscriptions(self, topics: set, qos: int = 1):
        """Đưa tập topic động về đúng `topics`: subscribe cái mới, bỏ cái thừa."""
        current = self.subscriptions
        for topic in sorted(current - topics):
            self.unsubscribe(topic)
        for topic in sorted(topics - current):
            self.subscribe(topic, qos=qos)

    def publish(self, topic: str, payload: str, qos: int = 1):
        result = self.client.publish(topic, payload, qos=qos)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"📤 Published to [{topic}]: {payload}")
        else:
            logger.error(f"Publish failed to {topic}, rc={result.rc}")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self):
        """Kết nối tới broker (không chặn) và chạy vòng lặp nền; paho tự kết nối lại."""
        use_tls = settings.MQTT_USE_TLS
        if not use_tls and settings.MQTT_PASSWORD:
            logger.error("MQTT_USE_TLS=false nhưng có MQTT_PASSWORD — từ chối gửi mật khẩu không mã hoá. Bật TLS.")
            return
        port = settings.MQTT_PORT_SSL if use_tls else settings.MQTT_PORT
        if use_tls:
            # Chứng chỉ kiểm bằng kho CA của hệ điều hành (Let's Encrypt, HiveMQ…) hoặc MQTT_CA_CERTS.
            self.client.tls_set(ca_certs=settings.MQTT_CA_CERTS or None, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            self.client.tls_insecure_set(False)
        scheme = ("wss" if use_tls else "ws") if settings.MQTT_TRANSPORT == "websockets" else ("mqtts" if use_tls else "mqtt")
        path = settings.MQTT_WS_PATH if settings.MQTT_TRANSPORT == "websockets" else ""
        logger.info(f"Connecting to {scheme}://{settings.MQTT_BROKER}:{port}{path} ...")
        try:
            self.client.connect_async(settings.MQTT_BROKER, port, keepalive=settings.MQTT_KEEPALIVE)
            self.client.loop_start()
        except Exception as e:
            logger.error(f"MQTT connect setup failed: {e}")

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("MQTT client stopped")
        except Exception as e:
            logger.error(f"Error stopping MQTT client: {e}")
