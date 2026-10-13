"""Nghe lại những gì drone gửi lên broker và kiểm chữ ký — để nghiệm thu, không phải để vận hành.

    python watch.py            nghe mãi (Ctrl+C để dừng)
    python watch.py --seconds 10

Dùng cấu hình trong .env (broker, DRONE_ID, DEVICE_KEY); kết nối bằng một client id khác bridge.
"""
import argparse
import json
import ssl
import time
from pathlib import Path

import paho.mqtt.client as mqtt

from lockr_drone.config import Settings, load_env_file
from lockr_drone.signing import SIGNATURE_PROPERTY, sign


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=0)
    args = parser.parse_args()

    load_env_file(Path(__file__).with_name(".env"))
    settings = Settings.from_env()
    topic_filter = f"lockr/drones/{settings.drone_id}/#"
    counts = {"ok": 0, "bad": 0}

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connected ({reason_code}) — subscribing {topic_filter}", flush=True)
        client.subscribe(topic_filter, qos=1)

    def on_message(client, userdata, msg):
        body = msg.payload.decode("utf-8", errors="replace")
        user_properties = dict(getattr(msg.properties, "UserProperty", None) or [])
        signature = user_properties.get(SIGNATURE_PROPERTY)
        valid = bool(settings.device_key) and signature == sign(settings.device_key, msg.topic, body)
        counts["ok" if valid else "bad"] += 1
        data = json.loads(body)
        kind = msg.topic.rsplit("/", 1)[-1]
        if kind == "status":
            summary = f"state={data.get('state')} at={data.get('at')}"
        else:
            summary = (f"seq={data.get('sequence')} link={data['link']['mavlink']} mode={data['flight']['mode']} "
                       f"armed={data['flight']['armed']} landed={data['landed']['landedState']} "
                       f"sats={data['gps']['satellites']} lat={data['position']['lat']}")
        print(f"[{'sig OK ' if valid else 'SIG BAD'}] {kind}{' (retained)' if msg.retain else ''}: {summary}",
              flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"lockr-drone-watch-{int(time.time())}", protocol=mqtt.MQTTv5)
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    if settings.mqtt_tls:
        client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30)
    client.loop_start()
    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    client.loop_stop()
    print(f"signed correctly: {counts['ok']} · bad or unsigned: {counts['bad']}")


if __name__ == "__main__":
    main()
