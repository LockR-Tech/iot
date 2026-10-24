"""Camera Streamer tích hợp AI thị giác ArUco cho Drone (MJPEG Stream).
Hỗ trợ cả Raspberry Pi CSI Camera (Picamera2 trên Pi 5) và USB Camera (OpenCV V4L2).
Tự động phát video tại http://0.0.0.0:8080/stream.mjpg cho Web Admin xem trực tiếp kèm HUD nhận diện.
Tự động quét mã ArUco ô tủ (DICT_4X4_50, 8x8cm) và gửi sự kiện MQTT để kích hoạt mở nắp tủ.
"""
import io
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

log = logging.getLogger("camera")

try:
    from picamera2 import Picamera2
    PICAMERA2_AVAILABLE = True
except Exception:
    PICAMERA2_AVAILABLE = False

try:
    import cv2
    import numpy as np
    OPENCV_AVAILABLE = True
except Exception:
    OPENCV_AVAILABLE = False


class CameraCapture:
    def __init__(self, device_index=0, width=640, height=480, fps=20, on_marker_detected=None):
        self.device_index = device_index
        self.width = width
        self.height = height
        self.fps = fps
        self.picam2 = None
        self.cap = None
        self.lock = threading.Lock()
        self.current_frame = None
        self.running = False
        self.mode = "none"
        self.on_marker_detected = on_marker_detected

        # Trạng thái ArUco AI Vision & HUD
        self.state = "SCANNING"  # SCANNING, DETECTED, VALIDATED, LID_OPEN, COMPLETED, REJECTED
        self.status_message = "Ready to scan ArUco marker (DICT_4X4_50)..."
        self.last_detected_id = None
        self.detected_id_count = 0
        self.detected_at = None
        self.last_marker_seen_at = None
        self.rejected_until = 0.0
        self.lid_opened_time = None
        self.cur_x = None
        self.cur_y = None
        self.cur_z = None

        # ArUco Detector
        self.detector = None
        self.camera_matrix = None
        self.dist_coeffs = None
        self.object_points = None
        self._init_aruco()

    def _init_aruco(self):
        if not OPENCV_AVAILABLE:
            return
        try:
            dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
            parameters = cv2.aruco.DetectorParameters()
            self.detector = cv2.aruco.ArucoDetector(dictionary, parameters)

            marker_size_m = 0.08  # 8x8 cm chuẩn của ô tủ
            half = marker_size_m / 2.0
            self.object_points = np.array([
                [-half,  half, 0.0],
                [ half,  half, 0.0],
                [ half, -half, 0.0],
                [-half, -half, 0.0]
            ], dtype=np.float32)

            calib_file = "/home/lockr/aruco_landing/calibration/camera_calibration.npz"
            if os.path.exists(calib_file):
                calib = np.load(calib_file)
                self.camera_matrix = calib["camera_matrix"]
                self.dist_coeffs = calib["dist_coeffs"]
                log.info("Đã tải camera calibration từ %s", calib_file)
            else:
                fx = self.width * 0.8
                fy = self.width * 0.8
                cx = self.width / 2.0
                cy = self.height / 2.0
                self.camera_matrix = np.array([
                    [fx, 0, cx],
                    [0, fy, cy],
                    [0, 0, 1]
                ], dtype=np.float32)
                self.dist_coeffs = np.zeros((4, 1), dtype=np.float32)
            log.info("Khởi tạo bộ nhận diện ArUco DICT_4X4_50 thành công")
        except Exception as e:
            log.warning("Không thể khởi tạo ArUco detector: %s", e)
            self.detector = None

    def start(self):
        self.running = True

        # 1. Thử Picamera2 trước (chuẩn cho CSI camera trên Pi 5 như IMX219)
        if PICAMERA2_AVAILABLE:
            try:
                self.picam2 = Picamera2()
                video_config = self.picam2.create_video_configuration(
                    main={"size": (self.width, self.height), "format": "RGB888"},
                    controls={"FrameRate": self.fps}
                )
                self.picam2.configure(video_config)
                self.picam2.start()
                self.mode = "picamera2"
                log.info("Đã mở camera CSI qua Picamera2 (%dx%d @ %d fps)", self.width, self.height, self.fps)
            except Exception as e:
                log.warning("Không mở được Picamera2 CSI: %s. Thử OpenCV...", e)
                self.picam2 = None

        # 2. Nếu không có Picamera2, thử OpenCV V4L2 (USB Camera)
        if self.mode == "none" and OPENCV_AVAILABLE:
            try:
                self.cap = cv2.VideoCapture(self.device_index)
                if self.cap.isOpened():
                    self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                    self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                    self.cap.set(cv2.CAP_PROP_FPS, self.fps)
                    self.mode = "opencv"
                    log.info("Đã mở camera USB qua OpenCV index %d", self.device_index)
                else:
                    self.cap = None
            except Exception as e:
                log.warning("Không mở được camera USB OpenCV: %s", e)
                self.cap = None

        if self.mode == "none":
            log.info("Chạy chế độ camera mẫu/chờ (standby test pattern).")

        t = threading.Thread(target=self._capture_loop, daemon=True, name="camera-capture")
        t.start()

    def _capture_loop(self):
        while self.running:
            frame_bytes = None

            # Capture từ Picamera2
            if self.mode == "picamera2" and self.picam2:
                try:
                    frame_rgb = self.picam2.capture_array()
                    if OPENCV_AVAILABLE:
                        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                        frame_bytes = self._process_frame_and_annotate(frame_bgr)
                    else:
                        mem = io.BytesIO()
                        self.picam2.capture_file(mem, format="jpeg")
                        frame_bytes = mem.getvalue()
                except Exception as ex:
                    log.debug("Lỗi capture Picamera2: %s", ex)

            # Capture từ OpenCV USB
            elif self.mode == "opencv" and self.cap and self.cap.isOpened():
                try:
                    ret, frame_bgr = self.cap.read()
                    if ret:
                        frame_bytes = self._process_frame_and_annotate(frame_bgr)
                except Exception as ex:
                    log.debug("Lỗi capture OpenCV: %s", ex)

            # Fallback nếu chưa có camera
            if frame_bytes is None:
                frame_bytes = self._generate_fallback_jpeg()

            with self.lock:
                self.current_frame = frame_bytes

            time.sleep(1.0 / self.fps)

    def _process_frame_and_annotate(self, frame_bgr):
        now = time.time()
        marker_found = False
        found_id = None
        x, y, z = None, None, None
        corners = None
        ids = None

        if self.detector is not None:
            try:
                gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
                corners, ids, _ = self.detector.detectMarkers(gray)

                if ids is not None and len(ids) > 0:
                    for i in range(len(ids)):
                        c = corners[i]
                        mid = int(ids[i][0])
                        ok, rvec, tvec = cv2.solvePnP(
                            self.object_points,
                            c[0],
                            self.camera_matrix,
                            self.dist_coeffs,
                            flags=cv2.SOLVEPNP_IPPE_SQUARE
                        )
                        if ok:
                            cam_x = float(tvec[0][0])
                            cam_y = float(tvec[1][0])
                            cam_z = float(tvec[2][0])
                            x = -cam_y
                            y = cam_x
                            z = cam_z
                            marker_found = True
                            found_id = mid
                            break
            except Exception as e:
                log.debug("Lỗi xử lý ArUco: %s", e)

        # Xử lý State Machine & Debounce
        with self.lock:
            if marker_found:
                self.last_marker_seen_at = now
                self.cur_x, self.cur_y, self.cur_z = x, y, z
                if self.state == "REJECTED" and now < self.rejected_until:
                    pass
                elif self.state in ("SCANNING", "REJECTED"):
                    if self.state == "REJECTED":
                        self.state = "SCANNING"
                    if found_id == self.last_detected_id:
                        self.detected_id_count += 1
                    else:
                        self.last_detected_id = found_id
                        self.detected_id_count = 1

                    if self.detected_id_count >= 5:  # DEBOUNCE_FRAMES
                        self.state = "DETECTED"
                        self.detected_at = now
                        self.status_message = f"Target #{found_id} detected! Verifying with locker..."
                        if self.on_marker_detected:
                            # Gọi callback gửi MQTT ra ngoài
                            threading.Thread(
                                target=self.on_marker_detected,
                                args=(found_id, x, y, z),
                                daemon=True
                            ).start()
                elif self.state == "DETECTED":
                    if found_id != self.last_detected_id:
                        self.last_detected_id = found_id
                        self.detected_id_count = 1
                        self.state = "SCANNING"
                    elif self.detected_at is None or (now - self.detected_at) >= 4.0:
                        self.detected_at = now
                        if self.on_marker_detected:
                            threading.Thread(
                                target=self.on_marker_detected,
                                args=(found_id, x, y, z),
                                daemon=True
                            ).start()
            else:
                self.cur_x, self.cur_y, self.cur_z = None, None, None
                if self.state == "SCANNING":
                    self.detected_id_count = 0
                elif self.state == "DETECTED" and self.last_marker_seen_at and (now - self.last_marker_seen_at) >= 2.0:
                    self.state = "SCANNING"
                    self.status_message = "Ready to scan ArUco marker (DICT_4X4_50)..."
                    self.detected_id_count = 0
                    self.last_detected_id = None
                    self.detected_at = None

            # Đếm ngược giai đoạn mở nắp
            if self.state == "LID_OPEN" and self.lid_opened_time:
                elapsed = now - self.lid_opened_time
                remaining = max(0, 7.0 - elapsed)
                if remaining > 0:
                    self.status_message = f"Locker lid is OPEN! Cargo drop: {remaining:.1f}s..."
                else:
                    self.status_message = "Cargo drop completed! Locker lid closing..."

        # Vẽ HUD trực tiếp lên khung hình xem live
        draw_frame = frame_bgr.copy()
        if ids is not None and len(ids) > 0:
            cv2.aruco.drawDetectedMarkers(draw_frame, corners, ids)

        self._draw_hud(draw_frame, found_id)

        ret_enc, buffer = cv2.imencode(".jpg", draw_frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
        return buffer.tobytes() if ret_enc else None

    @staticmethod
    def _draw_pill(frame, x1, y1, x2, y2, bg_color=(20, 20, 20), alpha=0.65):
        h, w = frame.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 > x1 and y2 > y1:
            sub = frame[y1:y2, x1:x2]
            color_block = np.full(sub.shape, bg_color, dtype=np.uint8)
            cv2.addWeighted(color_block, alpha, sub, 1.0 - alpha, 0, sub)

    def _draw_hud(self, frame, found_id):
        h, w, _ = frame.shape

        # 1. Subtle Drone Targeting Reticle (Center crosshair)
        cx, cy = w // 2, h // 2
        reticle_color = (130, 130, 130)
        cv2.line(frame, (cx - 24, cy), (cx - 7, cy), reticle_color, 1, cv2.LINE_AA)
        cv2.line(frame, (cx + 7, cy), (cx + 24, cy), reticle_color, 1, cv2.LINE_AA)
        cv2.line(frame, (cx, cy - 24), (cx, cy - 7), reticle_color, 1, cv2.LINE_AA)
        cv2.line(frame, (cx, cy + 7), (cx, cy + 24), reticle_color, 1, cv2.LINE_AA)

        # 2. Top-Left: Minimal Cam Label
        self._draw_pill(frame, 12, 10, 108, 34, bg_color=(20, 20, 20), alpha=0.7)
        cv2.circle(frame, (23, 22), 4, (0, 255, 0), -1, cv2.LINE_AA)
        cv2.putText(frame, "AI VISION", (34, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA)

        # 3. Top-Right: Clean Status Pill
        state_configs = {
            "SCANNING":  ("SCANNING",        (0, 215, 255)),   # Amber
            "DETECTED":  ("TARGET DETECTED", (0, 255, 255)),   # Yellow
            "VALIDATED": ("TARGET VERIFIED", (0, 255, 0)),     # Green
            "LID_OPEN":  ("LID OPEN",        (255, 180, 50)),  # Light Blue
            "COMPLETED": ("COMPLETED",       (0, 255, 0)),     # Green
            "REJECTED":  ("REJECTED",        (0, 0, 255)),     # Red
        }
        label, state_color = state_configs.get(self.state, (self.state, (200, 200, 200)))

        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
        pill_pad_x, pill_pad_y = 10, 6
        rx2 = w - 12
        rx1 = rx2 - tw - pill_pad_x * 2
        ry1 = 10
        ry2 = ry1 + th + pill_pad_y * 2

        self._draw_pill(frame, rx1, ry1, rx2, ry2, bg_color=(20, 20, 20), alpha=0.7)
        cv2.rectangle(frame, (rx1, ry1), (rx2, ry2), state_color, 1, cv2.LINE_AA)
        cv2.putText(frame, label, (rx1 + pill_pad_x, ry1 + th + pill_pad_y - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, state_color, 1, cv2.LINE_AA)

        # 4. Target Pose Info (Top Center) - Only when marker detected
        if self.cur_z is not None:
            mid = found_id if found_id is not None else self.last_detected_id
            pose_text = f"MARKER #{mid} | DIST: {self.cur_z:.2f}m | X: {self.cur_x:+.2f}m Y: {self.cur_y:+.2f}m"
            (pw, ph), _ = cv2.getTextSize(pose_text, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
            px1 = (w - pw) // 2 - 10
            py1 = 10
            px2 = (w + pw) // 2 + 10
            py2 = py1 + ph + 12

            self._draw_pill(frame, px1, py1, px2, py2, bg_color=(15, 25, 20), alpha=0.75)
            cv2.rectangle(frame, (px1, py1), (px2, py2), (0, 255, 180), 1, cv2.LINE_AA)
            cv2.putText(frame, pose_text, (px1 + 10, py1 + ph + 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 180), 1, cv2.LINE_AA)

        # 5. Bottom Center Toast - Only for active transitions
        bottom_msg = None
        if self.state == "LID_OPEN" and self.lid_opened_time:
            elapsed = time.time() - self.lid_opened_time
            remaining = max(0, 7.0 - elapsed)
            if remaining > 0:
                bottom_msg = f"Lid Open - Cargo Drop: {remaining:.1f}s remaining..."
            else:
                bottom_msg = "Cargo Dropped. Closing Locker Lid..."
        elif self.state == "VALIDATED":
            mid = found_id if found_id is not None else self.last_detected_id
            bottom_msg = f"Marker #{mid} Confirmed. Opening Locker Lid..."
        elif self.state == "COMPLETED":
            bottom_msg = "Delivery Completed Successfully."
        elif self.state == "REJECTED":
            bottom_msg = "Target Rejected. Safety Abort."

        if bottom_msg:
            (bw, bh), _ = cv2.getTextSize(bottom_msg, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            bx1 = (w - bw) // 2 - 12
            by1 = h - 36
            bx2 = (w + bw) // 2 + 12
            by2 = h - 14

            self._draw_pill(frame, bx1, by1, bx2, by2, bg_color=(15, 15, 15), alpha=0.8)
            cv2.putText(frame, bottom_msg, (bx1 + 12, by2 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)

    def handle_delivery_command(self, data: dict):
        event_type = data.get("eventType")
        with self.lock:
            if event_type == "MARKER_VALIDATED":
                self.state = "VALIDATED"
                box_id = data.get("compartmentId")
                marker_id = data.get("arucoMarkerId", self.last_detected_id)
                self.status_message = f"Marker #{marker_id} Validated! Box #{box_id}. Opening lid..."
            elif event_type in ("WRONG_MARKER", "SAFETY_ABORT", "MARKER_REJECTED"):
                err_msg = data.get("errorMessage", "Sai mã ArUco hoặc không khớp đơn")
                self.state = "REJECTED"
                self.detected_id_count = 0
                self.rejected_until = time.time() + 3.0
                self.status_message = f"REJECTED: {err_msg}"
                threading.Timer(4.0, self._reset_scanning).start()
            elif event_type == "LID_OPENED":
                self.state = "LID_OPEN"
                self.lid_opened_time = time.time()
                self.status_message = "Locker lid is OPEN! Cargo drop in progress (7s)..."
            elif event_type == "DELIVERY_READY":
                self.state = "COMPLETED"
                self.status_message = "DELIVERY COMPLETED! Locker lid closed. Ready for pickup."
                threading.Timer(15.0, self._reset_scanning).start()

    def _reset_scanning(self):
        with self.lock:
            if self.state in ("REJECTED", "COMPLETED"):
                self.state = "SCANNING"
                self.status_message = "Ready to scan ArUco marker (DICT_4X4_50)..."
                self.last_detected_id = None
                self.detected_id_count = 0
                self.detected_at = None
                self.cur_x, self.cur_y, self.cur_z = None, None, None

    def _generate_fallback_jpeg(self):
        if OPENCV_AVAILABLE:
            img = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            cv2.putText(img, "LOCK.R DRONE CAMERA", (50, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            cv2.putText(img, time.strftime("%H:%M:%S"), (50, 220), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 2)
            cv2.putText(img, "STANDBY", (50, 300), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 0), 2)
            _, buf = cv2.imencode(".jpg", img)
            return buf.tobytes()
        return b""

    def get_frame(self):
        with self.lock:
            return self.current_frame

    def stop(self):
        self.running = False
        if self.picam2:
            try:
                self.picam2.stop()
            except Exception:
                pass
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass


class StreamHandler(BaseHTTPRequestHandler):
    camera_capture = None
    state = None
    settings = None
    port = 8080

    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    @classmethod
    def get_status_data(cls):
        import uuid
        import socket

        hostname = socket.gethostname()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "127.0.0.1"

        mac = ":".join(f"{(uuid.getnode() >> ele) & 0xff:02X}" for ele in range(40, -1, -8))
        drone_id = getattr(cls.settings, "drone_id", "DRONE-S550-07") if cls.settings else "DRONE-S550-07"

        flight_mode = None
        armed = None
        landed_state = None
        gps_fix = None
        gps_sats = None
        battery_pct = None
        voltage = None
        current = None
        mavlink_status = "disconnected"

        if cls.state:
            snapshot = cls.state.snapshot()
            heartbeat_age = snapshot.get("heartbeat_age_ms")
            if heartbeat_age is not None and heartbeat_age < 5000:
                mavlink_status = "connected"
            elif snapshot.get("serial_open"):
                mavlink_status = "connecting"

            # Chỉ đọc các thông số bay/pin khi Pixhawk thực sự có kết nối MAVLink
            if mavlink_status == "connected":
                groups = snapshot.get("groups", {})
                flight_grp = groups.get("flight", ({}, None))[0]
                battery_grp = groups.get("battery", ({}, None))[0]
                gps_grp = groups.get("gps", ({}, None))[0]
                landed_grp = groups.get("landed", ({}, None))[0]

                flight_mode = flight_grp.get("mode")
                if "armed" in flight_grp:
                    armed = bool(flight_grp.get("armed"))
                landed_state = landed_grp.get("landedState")
                gps_fix = gps_grp.get("fixType")
                gps_sats = gps_grp.get("satellites")
                if battery_grp.get("percent") is not None:
                    battery_pct = battery_grp.get("percent")
                if battery_grp.get("voltageV") is not None:
                    voltage = battery_grp.get("voltageV")
                if battery_grp.get("currentA") is not None:
                    current = battery_grp.get("currentA")

        vision_data = {}
        if cls.camera_capture:
            with cls.camera_capture.lock:
                vision_data = {
                    "visionState": cls.camera_capture.state,
                    "statusMessage": cls.camera_capture.status_message,
                    "lastMarkerId": cls.camera_capture.last_detected_id,
                    "distanceM": cls.camera_capture.cur_z,
                }

        return {
            "id": 1,
            "droneId": drone_id,
            "droneCode": drone_id,
            "macAddress": mac,
            "ipAddress": ip,
            "hostname": hostname,
            "online": True,
            "cameraOnline": True,
            "cameraStreamUrl": f"http://{ip}:{cls.port}/stream.mjpg",
            "firmwareVersion": "LockR-OS v1.2 (Bookworm)",
            "mavlinkStatus": mavlink_status,
            "flightMode": flight_mode,
            "armed": armed,
            "landedState": landed_state,
            "gpsFixType": gps_fix,
            "gpsSatellites": gps_sats,
            "batteryPercent": battery_pct,
            "voltageV": voltage,
            "currentA": current,
            "arucoVision": vision_data,
            "lastSeenAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def do_GET(self):
        if self.path in ("/stream.mjpg", "/"):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    frame = self.camera_capture.get_frame() if self.camera_capture else None
                    if frame:
                        self.wfile.write(b"--frame\r\n")
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(frame)))
                        self.end_headers()
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                    time.sleep(0.05)
            except (ConnectionResetError, BrokenPipeError):
                pass
        elif self.path == "/snapshot.jpg":
            frame = self.camera_capture.get_frame() if self.camera_capture else None
            if frame:
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
            else:
                self.send_response(503)
                self.end_headers()
        elif self.path in ("/status", "/api/status"):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = self.get_status_data()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


class CameraStreamerServer:
    def __init__(self, port=8080, state=None, settings=None, on_marker_detected=None):
        self.port = port
        self.state = state
        self.settings = settings
        self.capture = CameraCapture(on_marker_detected=on_marker_detected)
        self.httpd = None
        self.thread = None

    def start(self):
        self.capture.start()
        StreamHandler.camera_capture = self.capture
        StreamHandler.state = self.state
        StreamHandler.settings = self.settings
        StreamHandler.port = self.port
        try:
            self.httpd = ThreadingHTTPServer(("", self.port), StreamHandler)
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="camera-http")
            self.thread.start()
            log.info("Camera live stream đang mở tại http://0.0.0.0:%d/stream.mjpg (Threading)", self.port)
        except Exception as e:
            log.warning("Không thể khởi động camera server port %d: %s", self.port, e)

    def handle_delivery_command(self, data: dict):
        if self.capture:
            self.capture.handle_delivery_command(data)

    def stop(self):
        self.capture.stop()
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
