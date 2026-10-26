"""Bridge telemetry drone Lock.R: đọc MAVLink từ autopilot, phát camera stream tích hợp AI ArUco và gửi telemetry lên MQTT.

    python main.py             đọc MAVLink, mở camera stream và gửi telemetry lên broker
    python main.py --print     chỉ in telemetry, không gửi MQTT
"""
import argparse
import json
import logging
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lockr_drone.config import Settings, load_env_file
from lockr_drone.mavlink_reader import MavlinkReader
from lockr_drone.state import DroneState
from lockr_drone.telemetry import build_payload, link_state
from lockr_drone.camera import CameraStreamerServer
from lockr_drone.delivery import DeliveryTracker

log = logging.getLogger("bridge")
SUMMARY_EVERY_S = 60


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", dest="print_only", action="store_true", help="chỉ in telemetry, không gửi MQTT")
    parser.add_argument("--count", type=int, default=0, help="dừng sau N bản tin (0 = chạy mãi)")
    parser.add_argument("--no-camera", dest="no_camera", action="store_true", help="tắt camera stream")
    parser.add_argument("--camera-port", type=int, default=8080, help="cổng phát video camera (mặc định 8080)")
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

    publisher = None
    if not args.print_only:
        from lockr_drone.mqtt_publisher import MqttPublisher
        publisher = MqttPublisher(settings)

    # Chỉ xin mở nắp khi có nhiệm vụ và camera thấy ĐÚNG mã ô đích ổn định (xem lockr_drone/delivery.py).
    tracker = DeliveryTracker(
        settings.drone_id,
        dictionary=settings.aruco_dictionary,
        stable_frames=settings.aruco_stable_frames,
        stable_seconds=settings.aruco_stable_seconds,
        lost_reset_s=settings.aruco_lost_reset_s,
        response_timeout_s=settings.delivery_response_timeout_s,
        lid_timeout_s=settings.delivery_lid_timeout_s,
        max_requests=settings.delivery_max_requests,
        target_poll_s=settings.target_poll_s,
    )

    def publish_delivery(payload: dict) -> None:
        if publisher is None:
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            return
        if publisher.publish_delivery_event(payload):
            log.info("Đã gửi %s (mission=%s order=%s locker=%s aruco=%s)", payload.get("eventType"),
                     payload.get("missionId"), payload.get("orderId"), payload.get("lockerId"),
                     payload.get("arucoMarkerId"))
        else:
            log.warning("Không gửi được %s (MQTT chưa kết nối) — sẽ thử lại theo hẹn giờ",
                        payload.get("eventType"))

    # Khởi động camera streamer ngầm tích hợp ArUco Vision (phát tại /stream.mjpg và /api/status)
    camera_server = None
    if not args.no_camera:
        camera_server = CameraStreamerServer(
            port=args.camera_port,
            state=state,
            settings=settings,
            on_marker_detected=publish_delivery,
            tracker=tracker,
        )
        camera_server.start()

    if publisher is not None:
        publisher.delivery_command_listener = tracker.handle_command
        publisher.on_connected = tracker.request_target_now
        publisher.start()

    reader = MavlinkReader(state, settings.mavlink_port, settings.mavlink_baud, settings.message_rate_hz)
    reader.start()

    sequence = sent = dropped = 0
    last_summary = time.monotonic()
    try:
        while not stop.wait(settings.publish_interval_s):
            tracker.tick()
            if publisher is not None and publisher.connected:
                request = tracker.due_target_request()
                if request:
                    publish_delivery(request)
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
                publisher.publish_discovery()
                sent = dropped = 0
                last_summary = time.monotonic()
            if args.count and sequence >= args.count:
                break
    finally:
        reader.stop()
        if camera_server is not None:
            camera_server.stop()
        if publisher is not None:
            publisher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
