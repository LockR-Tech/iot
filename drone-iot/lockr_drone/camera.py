"""Camera Streamer tích hợp cho Drone (MJPEG Stream).
Hỗ trợ cả Raspberry Pi CSI Camera (Picamera2 trên Pi 5) và USB Camera (OpenCV V4L2).
Tự động phát video tại http://0.0.0.0:8080/stream.mjpg cho Web Admin xem trực tiếp.
"""
import io
import json
import logging
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

log = logging.getLogger("camera")

try:
    from picamera2 import Picamera2
    PICAMERA2_AVAILABLE = True
except Exception:
    PICAMERA2_AVAILABLE = False

try:
    import cv2
    OPENCV_AVAILABLE = True
except Exception:
    OPENCV_AVAILABLE = False


class CameraCapture:
    def __init__(self, device_index=0, width=640, height=480, fps=20):
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

    def start(self):
        self.running = True

        # 1. Thử Picamera2 trước (chuẩn cho CSI camera trên Pi 5 như IMX219)
        if PICAMERA2_AVAILABLE:
            try:
                self.picam2 = Picamera2()
                video_config = self.picam2.create_video_configuration(
                    main={"size": (self.width, self.height), "format": "RGB888"}
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
                    # capture_file dưới dạng JPEG vào memory buffer
                    mem = io.BytesIO()
                    self.picam2.capture_file(mem, format="jpeg")
                    frame_bytes = mem.getvalue()
                except Exception as ex:
                    log.debug("Lỗi capture Picamera2: %s", ex)

            # Capture từ OpenCV USB
            elif self.mode == "opencv" and self.cap and self.cap.isOpened():
                try:
                    ret, frame = self.cap.read()
                    if ret:
                        ret_enc, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                        if ret_enc:
                            frame_bytes = buffer.tobytes()
                except Exception as ex:
                    log.debug("Lỗi capture OpenCV: %s", ex)

            # Fallback nếu chưa có camera
            if frame_bytes is None:
                frame_bytes = self._generate_fallback_jpeg()

            with self.lock:
                self.current_frame = frame_bytes

            time.sleep(1.0 / self.fps)

    def _generate_fallback_jpeg(self):
        if OPENCV_AVAILABLE:
            import numpy as np
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

        flight_mode = "MANUAL"
        armed = False
        landed_state = "ON_GROUND"
        gps_fix = 0
        gps_sats = 0
        battery_pct = 100
        voltage = 16.4
        current = 0.8
        mavlink_status = "disconnected"

        if cls.state:
            snapshot = cls.state.snapshot()
            heartbeat_age = snapshot.get("heartbeat_age_ms")
            if heartbeat_age is not None and heartbeat_age < 5000:
                mavlink_status = "connected"
            elif snapshot.get("serial_open"):
                mavlink_status = "connecting"

            groups = snapshot.get("groups", {})
            flight_grp = groups.get("flight", ({}, None))[0]
            battery_grp = groups.get("battery", ({}, None))[0]
            gps_grp = groups.get("gps", ({}, None))[0]
            landed_grp = groups.get("landed", ({}, None))[0]

            flight_mode = flight_grp.get("mode") or "MANUAL"
            armed = bool(flight_grp.get("armed"))
            landed_state = landed_grp.get("landedState") or "ON_GROUND"
            gps_fix = gps_grp.get("fixType") or 0
            gps_sats = gps_grp.get("satellites") or 0
            if battery_grp.get("percent") is not None:
                battery_pct = battery_grp.get("percent")
            if battery_grp.get("voltageV") is not None:
                voltage = battery_grp.get("voltageV")
            if battery_grp.get("currentA") is not None:
                current = battery_grp.get("currentA")

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
    def __init__(self, port=8080, state=None, settings=None):
        self.port = port
        self.state = state
        self.settings = settings
        self.capture = CameraCapture()
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

    def stop(self):
        self.capture.stop()
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
