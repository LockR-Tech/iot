"""Mục tiêu giao hàng của drone và quyết định khi nào xin mở nắp tủ.

Luồng (Pi → backend → MQTT → tủ, tái dùng kênh có sẵn):

    Pi  ──TARGET_REQUEST──▶ backend           (chỉ khi camera đang thấy một mã ArUco; có nhịp giãn)
    Pi  ◀─DELIVERY_TARGET── backend           missionId, orderId, lockerId, compartmentId,
                                              arucoMarkerId, arucoDictionary, mode
    camera thấy ĐÚNG mã ô đích ổn định N frame + T giây
    Pi  ──MARKER_DETECTED─▶ backend ──cabinet/{id}/command/open {lid:true}──▶ tủ
    Pi  ◀─MARKER_VALIDATED  (backend đã kiểm và gửi lệnh mở cho tủ)
    Pi  ◀─LID_OPENING       (tủ đã NHẬN lệnh, nắp đang chạy)
    Pi  ◀─LID_OPENED        (tủ báo nắp đã mở; lidConfirmation = LIMIT_SWITCH | STEP_COUNT)
                            demo trong nhà: kèm dropSimulationSeconds = LỆNH NHẢ HÀNG — Pi giả lập
                            nhả hàng đúng số giây đó (cấu hình admin) rồi:
    Pi  ──DROP_PHASE_COMPLETED─▶ backend ──cabinet/{id}/command/close {lid:true}──▶ tủ
    Pi  ◀─LID_CLOSING       (backend đã nhận báo cáo, tủ đang đóng nắp — thôi gửi lại báo cáo)
    Pi  ◀─LID_OPEN_FAILED / LID_CLOSE_FAILED / MARKER_REJECTED / SAFETY_ABORT / DELIVERY_READY

Quy tắc:
  • Camera trống / quay xung quanh không thấy mã nào ⇒ không gửi bản tin nào lên hệ thống.
  • Không có nhiệm vụ (chưa nhận DELIVERY_TARGET) ⇒ chỉ hỏi mục tiêu, không xin mở nắp, dù camera thấy marker.
  • Chỉ xét marker đúng ID và đúng từ điển của ô đích; marker khác trong cùng khung hình bị bỏ qua.
  • Phải thấy ổn định ít nhất `stable_frames` frame VÀ `stable_seconds` giây, mất dấu quá
    `lost_reset_s` thì đếm lại từ đầu.
  • Mỗi lần tiếp cận gửi MARKER_DETECTED MỘT lần; chỉ gửi lại khi backend không phản hồi sau
    `response_timeout_s`, tối đa `max_requests` lần (backend giữ nguyên phiên nếu đã có).
  • Nhận diện và mở nắp KHÔNG điều khiển drone đáp. Nhả hàng hiện là giả lập (chờ đủ số giây backend gửi);
    LID_OPENED không kèm dropSimulationSeconds (giao ngoài trời) thì Pi không tự báo nhả hàng.
  • Báo nhả hàng xong gửi lại mỗi `response_timeout_s` (tối đa `max_requests` lần) cho tới khi backend trả
    LID_CLOSING / DELIVERY_READY.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("delivery")

NO_TARGET = "NO_TARGET"            # chưa có nhiệm vụ: không gửi lệnh
SCANNING = "SCANNING"              # có nhiệm vụ, đang tìm mã của ô đích
STABILIZING = "STABILIZING"        # đang thấy đúng mã, chờ đủ số frame / thời gian
REQUEST_SENT = "REQUEST_SENT"      # đã gửi MARKER_DETECTED, chờ backend
VALIDATED = "VALIDATED"            # backend đã kiểm, đã gửi lệnh mở nắp cho tủ
LID_OPENING = "LID_OPENING"        # tủ đã nhận lệnh, nắp đang chạy
LID_OPEN = "LID_OPEN"              # tủ báo nắp đã mở (không có lệnh nhả hàng giả lập)
DROPPING = "DROPPING"              # nhận lệnh nhả hàng: đang giả lập nhả hàng trong số giây cấu hình
DROP_REPORTED = "DROP_REPORTED"    # đã báo nhả hàng xong, chờ backend xác nhận
LID_CLOSING = "LID_CLOSING"        # backend đã gửi lệnh đóng nắp, tủ đang đóng
COMPLETED = "COMPLETED"            # hàng đã vào ô, nắp đã đóng
FAILED = "FAILED"                  # lỗi; sau `cooldown_s` quay lại quét nếu còn nhiệm vụ

STATE_MESSAGES = {
    NO_TARGET: "Chưa có nhiệm vụ giao hàng — không gửi lệnh mở nắp",
    SCANNING: "Đang tìm mã ArUco của ô đích",
    STABILIZING: "Đang xác nhận mã ArUco ổn định",
    REQUEST_SENT: "Đã gửi yêu cầu mở nắp, chờ hệ thống",
    VALIDATED: "Hệ thống đã gửi lệnh mở nắp tới tủ",
    LID_OPENING: "Tủ đã nhận lệnh, nắp trượt đang mở",
    LID_OPEN: "Tủ báo nắp đã mở",
    DROPPING: "Đang giả lập nhả hàng",
    DROP_REPORTED: "Đã báo nhả hàng xong, chờ hệ thống",
    LID_CLOSING: "Tủ đang đóng nắp",
    COMPLETED: "Đã giao xong — nắp đã đóng",
    FAILED: "Lỗi",
}


@dataclass(frozen=True)
class Target:
    mission_id: Optional[int]
    order_id: int
    locker_id: int
    box_id: Optional[int]
    marker_id: int
    dictionary: str
    mode: str

    @staticmethod
    def from_command(cmd: dict) -> Optional["Target"]:
        try:
            return Target(
                mission_id=_int(cmd.get("missionId")),
                order_id=int(cmd["orderId"]),
                locker_id=int(cmd["lockerId"]),
                box_id=_int(cmd.get("compartmentId")),
                marker_id=int(cmd["arucoMarkerId"]),
                dictionary=str(cmd.get("arucoDictionary") or "DICT_4X4_50"),
                mode=str(cmd.get("mode") or "REAL_OUTDOOR"),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _int(value) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


class DeliveryTracker:
    def __init__(self, drone_id: str, dictionary: str = "DICT_4X4_50", stable_frames: int = 5,
                 stable_seconds: float = 0.6, lost_reset_s: float = 1.0, response_timeout_s: float = 15.0,
                 lid_timeout_s: float = 180.0, max_requests: int = 3, cooldown_s: float = 5.0,
                 exhausted_cooldown_s: float = 120.0,
                 target_poll_s: float = 10.0, target_refresh_s: float = 30.0,
                 marker_recent_s: float = 2.0,
                 clock: Callable[[], float] = time.monotonic):
        self.drone_id = drone_id
        self.dictionary = dictionary          # từ điển camera đang dò
        self.stable_frames = max(1, int(stable_frames))
        self.stable_seconds = max(0.0, float(stable_seconds))
        self.lost_reset_s = lost_reset_s
        self.response_timeout_s = response_timeout_s
        self.lid_timeout_s = lid_timeout_s
        self.max_requests = max(1, int(max_requests))
        self.cooldown_s = cooldown_s
        self.exhausted_cooldown_s = exhausted_cooldown_s
        self._exhausted = False
        self.target_poll_s = target_poll_s
        self.target_refresh_s = target_refresh_s
        self._clock = clock
        self._lock = threading.RLock()

        self.target: Optional[Target] = None
        self.state = NO_TARGET
        self.message = STATE_MESSAGES[NO_TARGET]
        self._first_seen = None
        self._last_seen = None
        self._frames = 0
        self._entered_at = clock()
        self._requests = 0
        self._session_id = None
        self._correlation_id = None
        self._last_target_request = None
        self._marker_seen_at = None
        self.marker_recent_s = marker_recent_s
        self.lid_confirmation = None
        self.drop_simulation_s = None
        self._drop_reports = 0
        self._drop_report_due = False
        self.last_pose = None

    # ─── nhiệm vụ ───

    def due_target_request(self) -> Optional[dict]:
        """Bản tin TARGET_REQUEST nếu đến lúc hỏi backend. CHỈ hỏi khi camera vừa thấy một mã ArUco
        (trong `marker_recent_s` giây) — camera trống / quay xung quanh thì không gửi gì. Đang thấy mã:
        chưa có nhiệm vụ thì hỏi mỗi `target_poll_s`, đã có thì mỗi `target_refresh_s` (biết nhiệm vụ huỷ/xong)."""
        with self._lock:
            now = self._clock()
            if self._marker_seen_at is None or now - self._marker_seen_at > self.marker_recent_s:
                return None
            every = self.target_poll_s if self.target is None else self.target_refresh_s
            if self._last_target_request is not None and now - self._last_target_request < every:
                return None
            self._last_target_request = now
            return {
                "messageId": str(uuid.uuid4()),
                "eventType": "TARGET_REQUEST",
                "droneId": self.drone_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    def request_target_now(self) -> None:
        """Kết nối (lại) broker: lần kế tiếp camera thấy mã ArUco thì hỏi mục tiêu ngay (không chờ nhịp)."""
        with self._lock:
            self._last_target_request = None

    # ─── camera ───

    def observe(self, detections, pose=None) -> Optional[dict]:
        """Một frame camera. `detections`: các mã ArUco thấy trong frame — [id] hoặc [(id, (x, y, z))].
        Trả bản tin MARKER_DETECTED cần gửi (đúng một lần mỗi lần tiếp cận), hoặc None."""
        with self._lock:
            now = self._clock()
            if detections:
                self._marker_seen_at = now   # có mã ArUco trong khung hình ⇒ được phép hỏi mục tiêu
            self._expire_waits(now)
            if self.target is None or self.state not in (SCANNING, STABILIZING):
                return None
            if self.dictionary.upper() != self.target.dictionary.upper():
                self._set(FAILED, f"Camera dò {self.dictionary} nhưng ô đích dùng {self.target.dictionary}")
                return None
            # Nhiều marker trong khung hình: chỉ marker đúng ID của ô đích được xét.
            found, match = False, None
            for item in detections or ():
                marker_id, item_pose = (item if isinstance(item, tuple) else (item, None))
                if int(marker_id) == self.target.marker_id:
                    found, match = True, (item_pose if item_pose is not None else pose)
                    break
            if not found:
                if self.state == STABILIZING and self._last_seen is not None \
                        and now - self._last_seen > self.lost_reset_s:
                    self._reset_stability()
                    self._set(SCANNING)
                return None
            self.last_pose = match
            if self.state == SCANNING:
                self._first_seen, self._frames = now, 0
                self._set(STABILIZING)
            self._frames += 1
            self._last_seen = now
            if self._frames < self.stable_frames or now - self._first_seen < self.stable_seconds:
                return None
            return self._marker_detected(now)

    def _marker_detected(self, now) -> dict:
        target = self.target
        self._requests += 1
        if self._session_id is None:
            self._session_id = f"session-{uuid.uuid4().hex[:12]}"
            self._correlation_id = str(uuid.uuid4())
        x, y, z = (self.last_pose or (None, None, None))
        payload = {
            "messageId": str(uuid.uuid4()),
            "correlationId": self._correlation_id,
            "sessionId": self._session_id,
            "eventType": "MARKER_DETECTED",
            "droneId": self.drone_id,
            "orderId": target.order_id,
            "lockerId": target.locker_id,
            "arucoDictionary": target.dictionary,
            "arucoMarkerId": target.marker_id,
            "mode": target.mode,
            "stableFrames": self._frames,
            "stableSeconds": round(now - self._first_seen, 2),
            "attempt": self._requests,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if target.mission_id is not None:
            payload["missionId"] = target.mission_id
        if target.box_id is not None:
            payload["compartmentId"] = target.box_id
        if z is not None:
            payload.update({"distanceM": round(z, 3), "offsetX": round(x, 3), "offsetY": round(y, 3)})
        self._set(REQUEST_SENT, f"Đã gửi yêu cầu mở nắp (lần {self._requests}/{self.max_requests})")
        log.info("MARKER_DETECTED mission=%s order=%s locker=%s aruco=%s session=%s attempt=%d",
                 target.mission_id, target.order_id, target.locker_id, target.marker_id,
                 self._session_id, self._requests)
        return payload

    # ─── lệnh từ backend ───

    def handle_command(self, cmd: dict) -> None:
        event = str(cmd.get("eventType") or "")
        with self._lock:
            if event == "DELIVERY_TARGET":
                return self._on_target(cmd)
            if event == "NO_TARGET":
                if self.state not in (COMPLETED,):
                    self._clear_target(cmd.get("errorMessage") or STATE_MESSAGES[NO_TARGET])
                return None
            if self.target is None:
                return None
            order_id = _int(cmd.get("orderId"))
            if order_id is not None and order_id != self.target.order_id:
                log.warning("Bỏ lệnh %s cho đơn #%s — nhiệm vụ hiện tại là đơn #%s",
                            event, order_id, self.target.order_id)
                return None
            if event == "MARKER_VALIDATED":
                if self.state in (REQUEST_SENT, SCANNING, STABILIZING):
                    self._set(VALIDATED)
            elif event == "LID_OPENING":
                self._set(LID_OPENING)
            elif event == "LID_OPENED":
                if self.state in (DROPPING, DROP_REPORTED, LID_CLOSING, COMPLETED):
                    return None   # lệnh lặp (QoS 1): không giả lập nhả hàng lần nữa
                self.lid_confirmation = cmd.get("lidConfirmation") or "LIMIT_SWITCH"
                self._session_id = cmd.get("sessionId") or self._session_id
                self._correlation_id = cmd.get("correlationId") or self._correlation_id
                self._start_drop(cmd.get("dropSimulationSeconds"), cmd.get("errorMessage"))
            elif event == "LID_CLOSING":
                if self.state != COMPLETED:
                    self._drop_report_due = False
                    self._set(LID_CLOSING, cmd.get("errorMessage") or STATE_MESSAGES[LID_CLOSING])
            elif event == "DELIVERY_READY":
                self._drop_report_due = False
                self._set(COMPLETED, cmd.get("errorMessage") or STATE_MESSAGES[COMPLETED])
            elif event in ("LID_OPEN_FAILED", "LID_CLOSE_FAILED", "SAFETY_ABORT"):
                self._drop_report_due = False
                self._set(FAILED, f"{cmd.get('errorCode') or event}: {cmd.get('errorMessage') or ''}".strip())
            elif event == "MARKER_REJECTED":
                self._set(FAILED, f"Hệ thống từ chối mã: {cmd.get('errorMessage') or ''}".strip())
        return None

    def _on_target(self, cmd: dict) -> None:
        target = Target.from_command(cmd)
        if target is None:
            log.warning("DELIVERY_TARGET thiếu trường bắt buộc: %s", cmd)
            return
        if target == self.target:
            return
        log.info("Nhận nhiệm vụ: mission=%s order=%s locker=%s box=%s aruco=%s (%s) mode=%s",
                 target.mission_id, target.order_id, target.locker_id, target.box_id,
                 target.marker_id, target.dictionary, target.mode)
        self.target = target
        self._requests = 0
        self._exhausted = False
        self._session_id = cmd.get("sessionId") or None
        self._correlation_id = None
        self.lid_confirmation = self.drop_simulation_s = None
        self._drop_report_due = False
        self._reset_stability()
        session_state = str(cmd.get("sessionState") or "")
        if session_state == "LID_OPENED":
            # Pi khởi động lại khi nắp đang mở chờ thả: backend nhắc lại lệnh nhả hàng (nếu là demo).
            self._start_drop(cmd.get("dropSimulationSeconds"), None)
            return
        resume = {"LID_OPENING": LID_OPENING, "OPEN_LID_REQUESTED": VALIDATED,
                  "CLOSE_LID_REQUESTED": LID_CLOSING}.get(session_state)
        self._set(resume or SCANNING, f"Nhiệm vụ đơn #{target.order_id}: tìm mã ArUco #{target.marker_id}"
                  if resume is None else None)

    def _start_drop(self, seconds, lid_message: Optional[str]) -> None:
        """Nắp đã mở. Có `seconds` (lệnh nhả hàng của demo) ⇒ giả lập nhả hàng chừng ấy giây rồi báo
        DROP_PHASE_COMPLETED; không có ⇒ chỉ hiển thị nắp đã mở (giao ngoài trời, drone tự xử lý)."""
        try:
            seconds = float(seconds) if seconds is not None else None
        except (TypeError, ValueError):
            seconds = None
        self.drop_simulation_s = seconds if seconds and seconds > 0 else None
        self._drop_reports = 0
        self._drop_report_due = False
        if self.drop_simulation_s is None:
            self._set(LID_OPEN, lid_message or STATE_MESSAGES[LID_OPEN])
            return
        log.info("Nhận lệnh nhả hàng: giả lập %.0f giây (session=%s)", self.drop_simulation_s, self._session_id)
        self._set(DROPPING, f"Nắp đã mở — đang giả lập nhả hàng {self.drop_simulation_s:g} giây")

    def due_drop_report(self) -> Optional[dict]:
        """Bản tin DROP_PHASE_COMPLETED khi đã giả lập nhả hàng đủ số giây (và gửi lại nếu backend chưa
        xác nhận sau `response_timeout_s`, tối đa `max_requests` lần). None nếu chưa tới lúc."""
        with self._lock:
            now = self._clock()
            waited = now - self._entered_at
            if self.target is None:
                return None
            if self.state == DROPPING and waited >= (self.drop_simulation_s or 0):
                self._drop_report_due = True
            elif self.state == DROP_REPORTED and waited >= self.response_timeout_s:
                if self._drop_reports >= self.max_requests:
                    self._set(FAILED, f"Hệ thống không xác nhận báo nhả hàng sau {self._drop_reports} lần gửi")
                    return None
                self._drop_report_due = True
            if not self._drop_report_due:
                return None
            self._drop_report_due = False
            self._drop_reports += 1
            target = self.target
            payload = {
                "messageId": str(uuid.uuid4()),
                "eventType": "DROP_PHASE_COMPLETED",
                "droneId": self.drone_id,
                "orderId": target.order_id,
                "lockerId": target.locker_id,
                "mode": target.mode,
                "dropSimulationSeconds": self.drop_simulation_s,
                "attempt": self._drop_reports,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if self._session_id:
                payload["sessionId"] = self._session_id
            if self._correlation_id:
                payload["correlationId"] = self._correlation_id
            if target.mission_id is not None:
                payload["missionId"] = target.mission_id
            if target.box_id is not None:
                payload["compartmentId"] = target.box_id
            log.info("DROP_PHASE_COMPLETED order=%s session=%s attempt=%d", target.order_id, self._session_id,
                     self._drop_reports)
            # _set đổi _entered_at kể cả khi gửi lại (cùng trạng thái) để hẹn giờ gửi lại tính từ lần này.
            self._entered_at = now
            self._set(DROP_REPORTED, f"Đã báo nhả hàng xong (lần {self._drop_reports}), chờ tủ đóng nắp")
            return payload

    def drop_report_failed(self) -> None:
        """Không gửi được báo cáo (MQTT mất kết nối): thử lại ở vòng kế tiếp, không tính lượt."""
        with self._lock:
            if self.state == DROP_REPORTED:
                self._drop_reports = max(0, self._drop_reports - 1)
                self._drop_report_due = True

    def _clear_target(self, reason: str) -> None:
        if self.target is not None:
            log.info("Bỏ nhiệm vụ đơn #%s: %s", self.target.order_id, reason)
        self.target = None
        self._requests = 0
        self._session_id = self._correlation_id = None
        self._reset_stability()
        self._set(NO_TARGET, reason)

    # ─── hẹn giờ ───

    def _expire_waits(self, now) -> None:
        waited = now - self._entered_at
        if self.state == REQUEST_SENT and waited >= self.response_timeout_s:
            if self._requests >= self.max_requests:
                # Hết lượt: nghỉ lâu (exhausted_cooldown_s) rồi mới thử lại — không gửi dồn dập khi mất kết nối.
                self._exhausted = True
                self._set(FAILED, f"Không nhận phản hồi sau {self._requests} lần gửi yêu cầu mở nắp")
            else:
                self._reset_stability()
                self._set(SCANNING, "Chưa có phản hồi — quét lại để gửi lần nữa")
        elif self.state in (VALIDATED, LID_OPENING) and waited >= self.lid_timeout_s:
            self._set(FAILED, "Quá lâu chưa thấy tủ báo nắp đã mở")
        elif self.state == FAILED and self.target is not None and waited >= (
                self.exhausted_cooldown_s if self._exhausted else self.cooldown_s):
            # COMPLETED thì không quét lại: chờ backend báo NO_TARGET / nhiệm vụ mới.
            self._exhausted = False
            self._requests = 0
            self._session_id = self._correlation_id = None
            self._reset_stability()
            self._set(SCANNING, "Thử lại: tìm mã ArUco của ô đích")

    def tick(self) -> None:
        """Gọi định kỳ khi không có frame (camera tắt) để các hẹn giờ vẫn chạy."""
        with self._lock:
            self._expire_waits(self._clock())

    def _reset_stability(self) -> None:
        self._first_seen = self._last_seen = None
        self._frames = 0

    def _set(self, state: str, message: Optional[str] = None) -> None:
        if state != self.state:
            log.info("Trạng thái giao hàng: %s → %s", self.state, state)
            self._entered_at = self._clock()
        self.state = state
        self.message = message or STATE_MESSAGES.get(state, state)

    # ─── hiển thị ───

    def snapshot(self) -> dict:
        with self._lock:
            target = self.target
            return {
                "state": self.state,
                "message": self.message,
                "targetMarkerId": target.marker_id if target else None,
                "targetLockerId": target.locker_id if target else None,
                "targetOrderId": target.order_id if target else None,
                "targetMissionId": target.mission_id if target else None,
                "stableFrames": self._frames,
                "requiredFrames": self.stable_frames,
                "requests": self._requests,
                "lidConfirmation": self.lid_confirmation,
                "dropSimulationSeconds": self.drop_simulation_s,
                "secondsInState": round(self._clock() - self._entered_at, 1),
            }
