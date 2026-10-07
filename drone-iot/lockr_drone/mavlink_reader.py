"""Đọc MAVLink từ autopilot trên luồng nền: tự mở lại cổng, tự yêu cầu lại tần suất message.

CHỈ ĐỌC. Thứ duy nhất gửi xuống autopilot là MAV_CMD_SET_MESSAGE_INTERVAL (xin tần suất
message). Không gửi heartbeat kiểu trạm mặt đất — để Pi tắt đi không kích hoạt GCS failsafe —
và không có lệnh arm, đổi mode, servo hay nhiệm vụ nào ở đây.
"""
import logging
import threading
import time

from pymavlink import mavutil

from .state import DroneState

log = logging.getLogger("mavlink")

# Message cần cho telemetry. HEARTBEAT autopilot tự gửi 1 Hz.
WANTED = (
    "GLOBAL_POSITION_INT", "GPS_RAW_INT", "SYS_STATUS", "VFR_HUD", "EXTENDED_SYS_STATE",
)
# Cổng mở mà không có byte nào trong ngần này giây ⇒ đóng và mở lại.
SILENCE_REOPEN_S = 10
# Đã xin tần suất mà message vẫn không về ⇒ xin lại sau ngần này giây.
REREQUEST_AFTER_S = 5
RECONNECT_DELAYS_S = (1, 2, 5, 10)


class MavlinkReader(threading.Thread):
    def __init__(self, state: DroneState, port: str, baud: int, rate_hz: float):
        super().__init__(name="mavlink-reader", daemon=True)
        self.state = state
        self.port = port
        self.baud = baud
        self.rate_hz = rate_hz
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            link = None
            try:
                # Thành phần "máy tính đi kèm" trên chính drone, không giả làm trạm mặt đất.
                link = mavutil.mavlink_connection(
                    self.port, baud=self.baud,
                    source_system=1, source_component=mavutil.mavlink.MAV_COMP_ID_ONBOARD_COMPUTER,
                )
                self.state.set_serial_open(True)
                log.info("Đã mở cổng %s", self.port)
                attempt = 0
                self._read(link)
            except Exception as error:  # cổng bị rút, không có quyền, thiết bị chưa lên…
                log.warning("Mất cổng MAVLink %s: %s", self.port, error)
            finally:
                self.state.set_serial_open(False)
                if link is not None:
                    try:
                        link.close()
                    except Exception:
                        pass
            delay = RECONNECT_DELAYS_S[min(attempt, len(RECONNECT_DELAYS_S) - 1)]
            attempt += 1
            self._stop_event.wait(delay)

    def _read(self, link) -> None:
        target = None           # (sysid, compid) của autopilot
        requested_at = None
        last_seen = {}          # message → time.monotonic() lần nhận gần nhất
        last_byte_at = time.monotonic()
        last_heartbeat_at = None

        while not self._stop_event.is_set():
            msg = link.recv_match(blocking=True, timeout=1)
            now = time.monotonic()
            if msg is None:
                if now - last_byte_at > SILENCE_REOPEN_S:
                    raise TimeoutError(f"không có dữ liệu trong {SILENCE_REOPEN_S} s")
                continue
            kind = msg.get_type()
            if kind == "BAD_DATA":
                continue
            last_byte_at = now

            if kind == "HEARTBEAT":
                # Bỏ heartbeat của trạm mặt đất, gimbal, thiết bị phụ.
                if (msg.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID
                        or msg.type == mavutil.mavlink.MAV_TYPE_GCS):
                    continue
                source = (msg.get_srcSystem(), msg.get_srcComponent())
                # Autopilot vừa xuất hiện, đổi id, hoặc im một lúc rồi có lại (khởi động lại):
                # tần suất đã xin trước đó mất, phải xin lại.
                returned = last_heartbeat_at is not None and now - last_heartbeat_at > REREQUEST_AFTER_S
                if source != target or returned:
                    target = source
                    log.info("Autopilot sysid=%d compid=%d — xin tần suất message", *target)
                    self._request_rates(link, target)
                    requested_at = now
                last_heartbeat_at = now
                self.state.apply(msg, mode=mavutil.mode_string_v10(msg))
            elif target is not None and msg.get_srcSystem() == target[0]:
                last_seen[kind] = now
                self.state.apply(msg)

            if target is not None and requested_at is not None and now - requested_at > REREQUEST_AFTER_S:
                missing = [name for name in WANTED if now - last_seen.get(name, 0) > REREQUEST_AFTER_S]
                if missing:
                    log.info("Chưa thấy %s — xin lại tần suất", ", ".join(missing))
                    self._request_rates(link, target, missing)
                requested_at = now

    def _request_rates(self, link, target, names=WANTED) -> None:
        interval_us = int(1_000_000 / self.rate_hz)
        for name in names:
            message_id = getattr(mavutil.mavlink, f"MAVLINK_MSG_ID_{name}")
            link.mav.command_long_send(
                target[0], target[1],
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                message_id, interval_us, 0, 0, 0, 0, 0,
            )
