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
                            kèm parcelRelease = LỆNH NHẢ HÀNG: servo mở chốt, giữ vài giây, đóng chốt
                            (release.py). Pi chưa bật servo: đơn demo kèm dropSimulationSeconds ⇒ giả lập
                            nhả hàng đúng số giây đó (cấu hình admin). Nhả xong:
    Pi  ──DROP_PHASE_COMPLETED─▶ backend ──cabinet/{id}/command/close {lid:true}──▶ tủ
    Pi  ◀─LID_CLOSING       (backend đã nhận báo cáo, tủ đang đóng nắp — thôi gửi lại báo cáo)
    Pi  ◀─LID_OPEN_FAILED / LID_CLOSE_FAILED / MARKER_REJECTED / SAFETY_ABORT / DELIVERY_READY
    Servo không nhả được (đã thử lại):
    Pi  ──PARCEL_RELEASE_FAILED─▶ backend ──close {lid:true}──▶ tủ (đóng nắp an toàn, kiện vẫn trên drone)
    Pi  ◀─LID_CLOSING → PARCEL_RELEASE_FAILED (nắp đã đóng; đơn chờ KTV drone xử lý, Pi không tự giao lại)

Quy tắc:
  • Camera trống / quay xung quanh không thấy mã nào ⇒ không gửi bản tin nào lên hệ thống.
  • Không có nhiệm vụ (chưa nhận DELIVERY_TARGET) ⇒ chỉ hỏi mục tiêu, không xin mở nắp, dù camera thấy marker.
  • Chỉ xét marker đúng ID và đúng từ điển của ô đích; marker khác trong cùng khung hình bị bỏ qua.
  • Phải thấy ổn định ít nhất `stable_frames` frame VÀ `stable_seconds` giây, mất dấu quá
    `lost_reset_s` thì đếm lại từ đầu.
  • Mỗi lần tiếp cận gửi MARKER_DETECTED MỘT lần; chỉ gửi lại khi backend không phản hồi sau
    `response_timeout_s`, tối đa `max_requests` lần (backend giữ nguyên phiên nếu đã có).
  • Nhận diện và mở nắp KHÔNG điều khiển drone đáp. Chỉ nhả hàng khi backend gửi lệnh nhả (tủ đã báo nắp mở);
    LID_OPENED không có lệnh nhả thì Pi không làm gì thêm.
  • Servo chạy ở luồng riêng (main.py nhận việc qua `claim_release`), không chặn luồng camera / MQTT.
  • Báo nhả hàng xong (hoặc lỗi) gửi lại mỗi `response_timeout_s` (tối đa `max_requests` lần) cho tới khi backend
    trả LID_CLOSING / DELIVERY_READY.
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
RELEASING = "RELEASING"            # nhận lệnh nhả hàng: servo đang mở chốt
DROPPING = "DROPPING"              # nhận lệnh nhả hàng, Pi chưa bật servo: giả lập trong số giây cấu hình
DROP_REPORTED = "DROP_REPORTED"    # đã báo nhả hàng xong, chờ backend xác nhận
LID_CLOSING = "LID_CLOSING"        # backend đã gửi lệnh đóng nắp, tủ đang đóng
COMPLETED = "COMPLETED"            # hàng đã vào ô, nắp đã đóng
FAILED = "FAILED"                  # lỗi; sau `cooldown_s` quay lại quét nếu còn nhiệm vụ
RELEASE_FAILED = "RELEASE_FAILED"  # servo không nhả được: kiện vẫn trên drone, KHÔNG tự quét lại

STATE_MESSAGES = {
    NO_TARGET: "Chưa có nhiệm vụ giao hàng — không gửi lệnh mở nắp",
    SCANNING: "Đang tìm mã ArUco của ô đích",
    STABILIZING: "Đang xác nhận mã ArUco ổn định",
    REQUEST_SENT: "Đã gửi yêu cầu mở nắp, chờ hệ thống",
    VALIDATED: "Hệ thống đã gửi lệnh mở nắp tới tủ",
    LID_OPENING: "Tủ đã nhận lệnh, nắp trượt đang mở",
    LID_OPEN: "Tủ báo nắp đã mở",
    RELEASING: "Servo đang mở chốt nhả hàng",
    DROPPING: "Đang giả lập nhả hàng",
    DROP_REPORTED: "Đã báo nhả hàng xong, chờ hệ thống",
    LID_CLOSING: "Tủ đang đóng nắp",
    COMPLETED: "Đã giao xong — nắp đã đóng",
    FAILED: "Lỗi",
    RELEASE_FAILED: "Servo không nhả được hàng — kiện vẫn trên drone",
}

RELEASE_TIMEOUT_S = 60.0           # servo chưa xong sau chừng này giây ⇒ coi như lỗi


LID_TEST_MODE = "LID_TEST"         # chế độ thử nắp (admin bật): mục tiêu không gắn đơn
REARM_AFTER_TEST_S = 2.0           # thử xong: mã phải rời khung hình chừng này giây mới thử lại


@dataclass(frozen=True)
class Target:
    mission_id: Optional[int]
    order_id: Optional[int]        # None khi là mục tiêu của chế độ thử nắp
    locker_id: int
    box_id: Optional[int]
    marker_id: int
    dictionary: str
    mode: str

    @staticmethod
    def from_command(cmd: dict) -> Optional["Target"]:
        try:
            mode = str(cmd.get("mode") or "REAL_OUTDOOR")
            return Target(
                mission_id=_int(cmd.get("missionId")),
                order_id=_int(cmd.get("orderId")) if mode == LID_TEST_MODE else int(cmd["orderId"]),
                locker_id=int(cmd["lockerId"]),
                box_id=_int(cmd.get("compartmentId")),
                marker_id=int(cmd["arucoMarkerId"]),
                dictionary=str(cmd.get("arucoDictionary") or "DICT_4X4_50"),
                mode=mode,
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
                 marker_recent_s: float = 2.0, servo_enabled: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self.drone_id = drone_id
        self.servo_enabled = servo_enabled    # có servo nhả hàng thật (SERVO_RELEASE_ENABLED)
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
        self._seen_markers = {}            # {mã ArUco: lần cuối thấy} — gửi kèm TARGET_REQUEST
        self._rearm_needed = False         # vừa thử nắp xong: chờ mã rời khung hình
        self.marker_recent_s = marker_recent_s
        self.lid_confirmation = None
        self.drop_simulation_s = None
        self._drop_reports = 0
        self._drop_report_due = False
        self._reset_release()
        self.last_pose = None

    # ─── nhiệm vụ ───

    def due_target_request(self) -> Optional[dict]:
        """Bản tin TARGET_REQUEST nếu đến lúc hỏi backend. CHỈ hỏi khi camera vừa thấy một mã ArUco
        (trong `marker_recent_s` giây) — camera trống / quay xung quanh thì không gửi gì. Đang thấy mã:
        chưa có nhiệm vụ thì hỏi mỗi `target_poll_s`, đã có thì mỗi `target_refresh_s` (biết nhiệm vụ huỷ/xong)."""
        with self._lock:
            now = self._clock()
            if self._rearm_needed:
                # Vừa thử nắp xong: không hỏi lại cho tới khi mã rời khung hình đủ lâu (tránh mở/đóng liên tục).
                if self._marker_seen_at is None or now - self._marker_seen_at >= REARM_AFTER_TEST_S:
                    self._rearm_needed = False
                return None
            if self._marker_seen_at is None or now - self._marker_seen_at > self.marker_recent_s:
                return None
            every = self.target_poll_s if self.target is None else self.target_refresh_s
            if self._last_target_request is not None and now - self._last_target_request < every:
                return None
            self._last_target_request = now
            visible = sorted(m for m, at in self._seen_markers.items() if now - at <= self.marker_recent_s)
            return {
                "messageId": str(uuid.uuid4()),
                "eventType": "TARGET_REQUEST",
                "droneId": self.drone_id,
                "visibleMarkers": visible,      # chế độ thử nắp: hệ thống chọn ô theo mã đang thấy
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
                for item in detections:
                    marker_id = item[0] if isinstance(item, tuple) else item
                    self._seen_markers[int(marker_id)] = now
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
                if self.state in (RELEASING, DROPPING, DROP_REPORTED, LID_CLOSING, COMPLETED, RELEASE_FAILED):
                    return None   # lệnh lặp (QoS 1): không nhả hàng lần nữa
                self.lid_confirmation = cmd.get("lidConfirmation") or "LIMIT_SWITCH"
                self._session_id = cmd.get("sessionId") or self._session_id
                self._correlation_id = cmd.get("correlationId") or self._correlation_id
                self._start_drop(cmd.get("dropSimulationSeconds"), cmd.get("errorMessage"),
                                 bool(cmd.get("parcelRelease")))
            elif self.state == RELEASE_FAILED:
                # Servo đã báo lỗi: chỉ cập nhật lời nhắn, giữ trạng thái (không tự quét lại, không giao lại).
                if event in ("LID_CLOSING", "PARCEL_RELEASE_FAILED"):
                    self._drop_report_due = False
                    self._report_acked = True
                self.message = cmd.get("errorMessage") or self.message
            elif event == "LID_CLOSING":
                if self.state != COMPLETED:
                    self._drop_report_due = False
                    self._set(LID_CLOSING, cmd.get("errorMessage") or STATE_MESSAGES[LID_CLOSING])
            elif event == "DELIVERY_READY":
                self._drop_report_due = False
                self._set(COMPLETED, cmd.get("errorMessage") or STATE_MESSAGES[COMPLETED])
            elif event == "LID_TEST_DONE":
                # Thử nắp xong: bỏ mục tiêu, chờ mã rời khung hình rồi mới thử lần sau.
                self._rearm_needed = True
                self._clear_target(cmd.get("errorMessage") or "Đã thử nắp xong")
            elif event in ("LID_OPEN_FAILED", "LID_CLOSE_FAILED", "SAFETY_ABORT", "MARKER_REJECTED"):
                self._drop_report_due = False
                text = (f"Hệ thống từ chối mã: {cmd.get('errorMessage') or ''}" if event == "MARKER_REJECTED"
                        else f"{cmd.get('errorCode') or event}: {cmd.get('errorMessage') or ''}").strip()
                if self.target.mode == LID_TEST_MODE:
                    # Thử nắp lỗi: không tự quét lại sau cooldown (tránh lặp lệnh nắp) — chờ mã rời khung hình.
                    self._rearm_needed = True
                    self._clear_target(text)
                else:
                    self._set(FAILED, text)
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
        self._reset_release()
        self._reset_stability()
        session_state = str(cmd.get("sessionState") or "")
        if session_state == "LID_OPENED":
            # Pi khởi động lại khi nắp đang mở chờ thả: backend nhắc lại lệnh nhả hàng.
            self._start_drop(cmd.get("dropSimulationSeconds"), None, bool(cmd.get("parcelRelease")))
            return
        resume = {"LID_OPENING": LID_OPENING, "OPEN_LID_REQUESTED": VALIDATED,
                  "CLOSE_LID_REQUESTED": LID_CLOSING}.get(session_state)
        self._set(resume or SCANNING, f"Nhiệm vụ đơn #{target.order_id}: tìm mã ArUco #{target.marker_id}"
                  if resume is None else None)

    def _start_drop(self, seconds, lid_message: Optional[str], release_requested: bool = False) -> None:
        """Nắp đã mở. Có lệnh nhả hàng (`parcelRelease`, hoặc `seconds` của backend cũ cho đơn demo):
        - bật servo ⇒ RELEASING, main.py quay servo rồi gọi `release_finished`;
        - chưa bật servo, có `seconds` ⇒ giả lập nhả hàng chừng ấy giây rồi báo DROP_PHASE_COMPLETED;
        - chưa bật servo, không có `seconds` (giao thật) ⇒ báo PARCEL_RELEASE_FAILED để backend đóng nắp.
        Không có lệnh nhả hàng ⇒ chỉ hiển thị nắp đã mở."""
        try:
            seconds = float(seconds) if seconds is not None else None
        except (TypeError, ValueError):
            seconds = None
        self.drop_simulation_s = seconds if seconds and seconds > 0 else None
        self._drop_reports = 0
        self._drop_report_due = False
        self._reset_release()
        if self.servo_enabled and (release_requested or self.drop_simulation_s is not None):
            log.info("Nhận lệnh nhả hàng: servo mở chốt (session=%s)", self._session_id)
            self._release_due = True
            self._set(RELEASING, "Nắp đã mở — servo đang mở chốt nhả hàng")
            return
        if self.drop_simulation_s is not None:
            log.info("Nhận lệnh nhả hàng: giả lập %.0f giây (session=%s)", self.drop_simulation_s, self._session_id)
            self.release_method = "SIMULATION"
            self._set(DROPPING, f"Nắp đã mở — đang giả lập nhả hàng {self.drop_simulation_s:g} giây")
            return
        if release_requested:
            self._release_failed("SERVO_DISABLED", "Pi drone chưa bật servo nhả hàng (SERVO_RELEASE_ENABLED)", 0)
            return
        self._set(LID_OPEN, lid_message or STATE_MESSAGES[LID_OPEN])

    def claim_release(self) -> bool:
        """True đúng một lần khi có lệnh nhả hàng bằng servo: main.py nhận việc và quay servo ở luồng riêng."""
        with self._lock:
            if self.state == RELEASING and self._release_due:
                self._release_due = False
                return True
            return False

    def release_finished(self, ok: bool, attempts: int, error: Optional[str] = None,
                         warning: Optional[str] = None, details: Optional[dict] = None) -> None:
        """Kết quả quay servo. Nhả xong ⇒ báo DROP_PHASE_COMPLETED; lỗi ⇒ báo PARCEL_RELEASE_FAILED.
        `details`: số liệu thật của servo (góc, thời gian giữ, thời gian chạy, chân GPIO…) gửi kèm báo cáo."""
        with self._lock:
            if self.state != RELEASING:
                log.warning("Bỏ kết quả servo (ok=%s): trạng thái giao hàng đã là %s", ok, self.state)
                return
            self._release_attempts = attempts
            self._release_details = dict(details or {})
            if not ok:
                self._release_failed("SERVO_ERROR", error or "servo không phản hồi", attempts)
                return
            self.release_method = "SERVO"
            self._release_warning = warning
            self._drop_report_due = True
            self.message = "Servo đã nhả hàng — đang báo hệ thống" + (f" ({warning})" if warning else "")

    def _release_failed(self, code: str, error: str, attempts: int) -> None:
        log.error("Không nhả được hàng: %s — %s (session=%s)", code, error, self._session_id)
        self._release_error_code = code
        self._release_error = error
        self._release_attempts = attempts
        self._drop_reports = 0
        self._report_acked = False
        self._drop_report_due = True
        self._set(RELEASE_FAILED, f"Servo không nhả được hàng: {error}")

    def _reset_release(self) -> None:
        self._release_due = False
        self._release_details = {}
        self.release_method = None
        self._release_attempts = 0
        self._release_warning = None
        self._release_error_code = None
        self._release_error = None
        self._report_acked = False

    def due_drop_report(self) -> Optional[dict]:
        """Bản tin báo kết quả nhả hàng: DROP_PHASE_COMPLETED khi servo đã nhả xong (hoặc đã giả lập đủ số giây),
        PARCEL_RELEASE_FAILED khi servo lỗi. Gửi lại nếu backend chưa xác nhận sau `response_timeout_s`, tối đa
        `max_requests` lần. None nếu chưa tới lúc."""
        with self._lock:
            now = self._clock()
            waited = now - self._entered_at
            if self.target is None:
                return None
            failed = self.state == RELEASE_FAILED
            if self.state == DROPPING and waited >= (self.drop_simulation_s or 0):
                self._drop_report_due = True
            elif self.state == DROP_REPORTED and waited >= self.response_timeout_s:
                if self._drop_reports >= self.max_requests:
                    self._set(FAILED, f"Hệ thống không xác nhận báo nhả hàng sau {self._drop_reports} lần gửi")
                    return None
                self._drop_report_due = True
            elif failed and not self._report_acked and self._drop_reports and waited >= self.response_timeout_s:
                if self._drop_reports >= self.max_requests:
                    # Giữ RELEASE_FAILED (không quét lại): kiện còn trên drone, KTV phải xử lý.
                    self._report_acked = True
                    self.message = (f"Servo không nhả được hàng; hệ thống chưa xác nhận báo lỗi sau "
                                    f"{self._drop_reports} lần gửi — kiện vẫn trên drone")
                    return None
                self._drop_report_due = True
            if not self._drop_report_due:
                return None
            self._drop_report_due = False
            self._drop_reports += 1
            target = self.target
            payload = {
                "messageId": str(uuid.uuid4()),
                "eventType": "PARCEL_RELEASE_FAILED" if failed else "DROP_PHASE_COMPLETED",
                "droneId": self.drone_id,
                "orderId": target.order_id,
                "lockerId": target.locker_id,
                "mode": target.mode,
                "attempt": self._drop_reports,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if failed:
                payload.update(self._release_details)
                payload.update({"errorCode": self._release_error_code, "errorMessage": self._release_error,
                                "releaseAttempts": self._release_attempts})
            else:
                payload["releaseMethod"] = self.release_method or "SIMULATION"
                if self.release_method == "SERVO":
                    payload.update(self._release_details)
                    payload["releaseAttempts"] = self._release_attempts
                    if self._release_warning:
                        payload["warning"] = self._release_warning
                else:
                    payload["dropSimulationSeconds"] = self.drop_simulation_s
            if self._session_id:
                payload["sessionId"] = self._session_id
            if self._correlation_id:
                payload["correlationId"] = self._correlation_id
            if target.mission_id is not None:
                payload["missionId"] = target.mission_id
            if target.box_id is not None:
                payload["compartmentId"] = target.box_id
            log.info("%s order=%s session=%s attempt=%d", payload["eventType"], target.order_id, self._session_id,
                     self._drop_reports)
            # _set đổi _entered_at kể cả khi gửi lại (cùng trạng thái) để hẹn giờ gửi lại tính từ lần này.
            self._entered_at = now
            if failed:
                self.message = f"Servo không nhả được hàng — đã báo hệ thống (lần {self._drop_reports}): {self._release_error}"
            else:
                self._set(DROP_REPORTED, f"Đã báo nhả hàng xong (lần {self._drop_reports}), chờ tủ đóng nắp")
            return payload

    def drop_report_failed(self) -> None:
        """Không gửi được báo cáo (MQTT mất kết nối): thử lại ở vòng kế tiếp, không tính lượt."""
        with self._lock:
            if self.state in (DROP_REPORTED, RELEASE_FAILED):
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
        elif self.state == RELEASING and not self._drop_report_due and waited >= RELEASE_TIMEOUT_S:
            self._release_failed("SERVO_TIMEOUT", f"servo chưa xong sau {RELEASE_TIMEOUT_S:.0f} giây",
                                 self._release_attempts)
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
                "servoReleaseEnabled": self.servo_enabled,
                "releaseMethod": self.release_method,
                "releaseError": self._release_error,
                "secondsInState": round(self._clock() - self._entered_at, 1),
            }
