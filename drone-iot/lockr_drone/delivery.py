"""Mục tiêu giao hàng của drone và quyết định khi nào xin mở nắp tủ.

Luồng (Pi → backend → MQTT → tủ, tái dùng kênh có sẵn):

    Pi  ──TARGET_REQUEST──▶ backend           (khởi động, kết nối lại, định kỳ)
    Pi  ◀─DELIVERY_TARGET── backend           missionId, orderId, lockerId, compartmentId,
                                              arucoMarkerId, arucoDictionary, mode
    camera thấy ĐÚNG mã ô đích ổn định N frame + T giây
    Pi  ──MARKER_DETECTED─▶ backend ──cabinet/{id}/command/open {lid:true}──▶ tủ
    Pi  ◀─MARKER_VALIDATED  (backend đã kiểm và gửi lệnh mở cho tủ)
    Pi  ◀─LID_OPENING       (tủ đã NHẬN lệnh, nắp đang chạy)
    Pi  ◀─LID_OPENED        (tủ báo nắp đã mở; lidConfirmation = LIMIT_SWITCH | STEP_COUNT)
    Pi  ◀─LID_OPEN_FAILED / MARKER_REJECTED / SAFETY_ABORT / DELIVERY_READY

Quy tắc:
  • Không có nhiệm vụ (chưa nhận DELIVERY_TARGET) ⇒ không gửi gì, dù camera thấy marker.
  • Chỉ xét marker đúng ID và đúng từ điển của ô đích; marker khác trong cùng khung hình bị bỏ qua.
  • Phải thấy ổn định ít nhất `stable_frames` frame VÀ `stable_seconds` giây, mất dấu quá
    `lost_reset_s` thì đếm lại từ đầu.
  • Mỗi lần tiếp cận gửi MARKER_DETECTED MỘT lần; chỉ gửi lại khi backend không phản hồi sau
    `response_timeout_s`, tối đa `max_requests` lần (backend giữ nguyên phiên nếu đã có).
  • Nhận diện và mở nắp KHÔNG điều khiển drone đáp hay nhả hàng — module này chỉ báo trạng thái.
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
LID_OPEN = "LID_OPEN"              # tủ báo nắp đã mở
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
        self.lid_confirmation = None
        self.drop_simulation_s = None
        self.last_pose = None

    # ─── nhiệm vụ ───

    def due_target_request(self) -> Optional[dict]:
        """Bản tin TARGET_REQUEST nếu đến lúc hỏi backend (chưa có nhiệm vụ: mỗi `target_poll_s`,
        đang có: mỗi `target_refresh_s` để biết nhiệm vụ đã huỷ/xong)."""
        with self._lock:
            now = self._clock()
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
        """Kết nối (lại) broker: hỏi mục tiêu ngay ở vòng kế tiếp."""
        with self._lock:
            self._last_target_request = None

    # ─── camera ───

    def observe(self, detections, pose=None) -> Optional[dict]:
        """Một frame camera. `detections`: các mã ArUco thấy trong frame — [id] hoặc [(id, (x, y, z))].
        Trả bản tin MARKER_DETECTED cần gửi (đúng một lần mỗi lần tiếp cận), hoặc None."""
        with self._lock:
            now = self._clock()
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
                self.lid_confirmation = cmd.get("lidConfirmation") or "LIMIT_SWITCH"
                self.drop_simulation_s = cmd.get("dropSimulationSeconds")
                text = cmd.get("errorMessage") or STATE_MESSAGES[LID_OPEN]
                if self.drop_simulation_s:
                    text += f" — giả lập drone nhả hàng {self.drop_simulation_s} giây"
                self._set(LID_OPEN, text)
            elif event == "DELIVERY_READY":
                self._set(COMPLETED, cmd.get("errorMessage") or STATE_MESSAGES[COMPLETED])
            elif event in ("LID_OPEN_FAILED", "LID_CLOSE_FAILED", "SAFETY_ABORT"):
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
        self._session_id = self._correlation_id = None
        self.lid_confirmation = self.drop_simulation_s = None
        self._reset_stability()
        resume = {"LID_OPENING": LID_OPENING, "LID_OPENED": LID_OPEN, "OPEN_LID_REQUESTED": VALIDATED,
                  "CLOSE_LID_REQUESTED": LID_OPEN}.get(str(cmd.get("sessionState") or ""))
        self._set(resume or SCANNING, f"Nhiệm vụ đơn #{target.order_id}: tìm mã ArUco #{target.marker_id}"
                  if resume is None else None)

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
