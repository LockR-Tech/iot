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
        self.status_message = "Sẵn sàng quét mã ArUco ô tủ (DICT_4X4_50)..."
        self.last_detected_id = None
        self.detected_id_count = 0
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
                self.cur_x, self.cur_y, self.cur_z = x, y, z
                if self.state in ("SCANNING", "REJECTED"):
                    if found_id == self.last_detected_id:
                        self.detected_id_count += 1
                    else:
                        self.last_detected_id = found_id
                        self.detected_id_count = 1

                    if self.detected_id_count >= 5:  # DEBOUNCE_FRAMES
                        self.state = "DETECTED"
                        self.status_message = f"Phát hiện ArUco #{found_id}! Đang xác thực với tủ..."
                        if self.on_marker_detected:
                            # Gọi callback gửi MQTT ra ngoài
                            threading.Thread(
                                target=self.on_marker_detected,
                                args=(found_id, x, y, z),
                                daemon=True
                            ).start()
            else:
                if self.state == "SCANNING":
                    self.detected_id_count = 0
                    self.cur_x, self.cur_y, self.cur_z = None, None, None

            # Đếm ngược giai đoạn mở nắp
            if self.state == "LID_OPEN" and self.lid_opened_time:
                elapsed = now - self.lid_opened_time
                remaining = max(0, 7.0 - elapsed)
                if remaining > 0:
                    self.status_message = f"Nắp tủ đang MỞ! Mô phỏng thả hàng: còn {remaining:.1f}s..."
                else:
                    self.status_message = "Thả hàng hoàn tất! Tủ đang đóng nắp an toàn..."

        # Vẽ HUD trực tiếp lên khung hình xem live
        draw_frame = frame_bgr.copy()
        if ids is not None and len(ids) > 0:
            cv2.aruco.drawDetectedMarkers(draw_frame, corners, ids)

        self._draw_hud(draw_frame, found_id)

        ret_enc, buffer = cv2.imencode(".jpg", draw_frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
        return buffer.tobytes() if ret_enc else None

    def _draw_hud(self, frame, found_id):
        h, w, _ = frame.shape

        # Thanh tiêu đề trên
        cv2.rectangle(frame, (0, 0), (w, 50), (25, 25, 25), -1)
        cv2.putText(frame, "LOCK.R DRONE | AI ARUCO VISION (8080)", (15, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 215, 255), 2)
        cv2.putText(frame, "CSI IMX219 | DICT_4X4_50 (8x8cm) | Auto Lid Opening", (15, 42),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1)

        # Màu Badge trạng thái
        state_color = (0, 255, 255)  # Vàng (SCANNING / DETECTED)
        if self.state == "VALIDATED":
            state_color = (0, 255, 0)    # Xanh lá
        elif self.state == "LID_OPEN":
            state_color = (255, 180, 0)  # Xanh dương
        elif self.state == "COMPLETED":
            state_color = (0, 255, 0)    # Xanh sáng
        elif self.state == "REJECTED":
            state_color = (0, 0, 255)    # Đỏ

        badge_text = f"STATE: {self.state}"
        cv2.putText(frame, badge_text, (w - 240, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, state_color, 2)

        # Khung tọa độ Pose nếu đang thấy marker
        if self.cur_z is not None:
            pose_box = (
                f"Marker #{found_id if found_id is not None else self.last_detected_id}  |  "
                f"Dist: {self.cur_z:.2f}m  |  "
                f"X: {self.cur_x:+.2f}m  Y: {self.cur_y:+.2f}m"
            )
            cv2.rectangle(frame, (15, 60), (w - 15), 35, (20, 20, 20), -1) if hasattr(cv2, "rectangle") else None
            cv2.rectangle(frame, (15, 60), (w - 15, 90), (20, 20, 20), -1)
            cv2.putText(frame, pose_box, (25, 82),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 180), 2)

        # Thanh trạng thái dưới
        cv2.rectangle(frame, (0, h - 45), (w, h), (20, 20, 20), -1)
        cv2.putText(frame, self.status_message, (15, h - 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

    def handle_delivery_command(self, data: dict):
        event_type = data.get("eventType")
        with self.lock:
            if event_type == "MARKER_VALIDATED":
                self.state = "VALIDATED"
                box_id = data.get("compartmentId")
                marker_id = data.get("arucoMarkerId", self.last_detected_id)
                self.status_message = f"Mã #{marker_id} HỢP LỆ! Ô tủ: #{box_id}. Đang mở nắp..."
            elif event_type in ("WRONG_MARKER", "SAFETY_ABORT", "MARKER_REJECTED"):
                err_msg = data.get("errorMessage", "Sai mã ArUco hoặc không khớp đơn")
                self.state = "REJECTED"
                self.status_message = f"TỪ CHỐI: {err_msg}"
                threading.Timer(5.0, self._reset_scanning).start()
            elif event_type == "LID_OPENED":
                self.state = "LID_OPEN"
                self.lid_opened_time = time.time()
                self.status_message = "Nắp ô tủ đã MỞ! Đang tiến hành thả hàng (7s)..."
            elif event_type == "DELIVERY_READY":
                self.state = "COMPLETED"
                self.status_message = "GIAO HÀNG THÀNH CÔNG! Nắp tủ đã đóng kín. Sẵn sàng lấy hàng."
                threading.Timer(15.0, self._reset_scanning).start()

    def _reset_scanning(self):
        with self.lock:
            self.state = "SCANNING"
            self.status_message = "Sẵn sàng quét mã ArUco ô tủ (DICT_4X4_50)..."
            self.last_detected_id = None
            self.detected_id_count = 0
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
