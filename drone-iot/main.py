"""Bridge telemetry drone Lock.R: đọc MAVLink từ autopilot, gửi telemetry lên MQTT.

    python main.py             đọc MAVLink và gửi lên broker
    python main.py --print     chỉ in bản tin ra màn hình (không cần MQTT)
"""
import argparse
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from lockr_drone.config import Settings, load_env_file
from lockr_drone.mavlink_reader import MavlinkReader
from lockr_drone.state import DroneState
from lockr_drone.telemetry import build_payload, link_state

log = logging.getLogger("bridge")
SUMMARY_EVERY_S = 60


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", dest="print_only", action="store_true", help="chỉ in telemetry, không gửi MQTT")
    parser.add_argument("--count", type=int, default=0, help="dừng sau N bản tin (0 = chạy mãi)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_file(Path(__file__).with_name(".env"))
    settings = Settings.from_env()
    problems = settings.problems(mqtt=not args.print_only)
    if problems:
        log.error("Cấu hình thiếu: %s", "; ".join(problems))
        return 2

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    state = DroneState()
    reader = MavlinkReader(state, settings.mavlink_port, settings.mavlink_baud, settings.message_rate_hz)
    reader.start()

    publisher = None
    if not args.print_only:
        from lockr_drone.mqtt_publisher import MqttPublisher
        publisher = MqttPublisher(settings)
        publisher.start()

    sequence = sent = dropped = 0
    last_summary = time.monotonic()
    try:
        while not stop.wait(settings.publish_interval_s):
            sequence += 1
            snapshot = state.snapshot()
            payload = build_payload(
                snapshot, settings.drone_id, sequence,
                settings.heartbeat_timeout_s, settings.stale_after_s,
            )
            if publisher is None:
                print(json.dumps(payload, ensure_ascii=False), flush=True)
            elif publisher.publish_telemetry(payload):
                sent += 1
            else:
                dropped += 1
            # Một dòng mỗi phút để log không trôi mà vẫn thấy bridge còn sống.
            if publisher is not None and time.monotonic() - last_summary >= SUMMARY_EVERY_S:
                log.info("mqtt=%s mavlink=%s · đã gửi %d, bỏ %d (không có kết nối broker)",
                         "connected" if publisher.connected else "disconnected",
                         link_state(snapshot, settings.heartbeat_timeout_s), sent, dropped)
                sent = dropped = 0
                last_summary = time.monotonic()
            if args.count and sequence >= args.count:
                break
    finally:
        reader.stop()
        if publisher is not None:
            publisher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
