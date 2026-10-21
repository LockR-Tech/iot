"""Camera Streamer tích hợp cho Drone (MJPEG Stream).
Tự động chạy ngầm trong một luồng riêng khi khởi động bridge,
phát video tại http://0.0.0.0:8080/stream.mjpg cho Web Admin xem trực tiếp.
"""
import logging
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

log = logging.getLogger("camera")

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False


class CameraCapture:
    def __init__(self, device_index=0, width=640, height=480, fps=20):
        self.device_index = device_index
        self.width = width
        self.height = height
        self.fps = fps
        self.cap = None
        self.lock = threading.Lock()
        self.current_frame = None
        self.running = False

    def start(self):
        self.running = True
        if OPENCV_AVAILABLE:
            try:
                self.cap = cv2.VideoCapture(self.device_index)
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                self.cap.set(cv2.CAP_PROP_FPS, self.fps)
                log.info("Đã mở camera thiết bị index %d (%dx%d @ %d fps)", self.device_index, self.width, self.height, self.fps)
            except Exception as e:
                log.warning("Không mở được camera vật lý: %s. Sử dụng khung hình chờ.", e)
                self.cap = None
        else:
            log.info("Không có opencv-python, chạy chế độ camera chờ (standby frame).")

        t = threading.Thread(target=self._capture_loop, daemon=True, name="camera-capture")
        t.start()

    def _capture_loop(self):
        while self.running:
            frame_bytes = None
            if self.cap and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret:
                    cv2.putText(frame, time.strftime("%Y-%m-%d %H:%M:%S"), (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    ret_enc, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    if ret_enc:
                        frame_bytes = buffer.tobytes()

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
            cv2.putText(img, "LIVE STREAM STANDBY", (50, 300), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 0), 2)
            _, buf = cv2.imencode(".jpg", img)
            return buf.tobytes()
        return b""

    def get_frame(self):
        with self.lock:
            return self.current_frame

    def stop(self):
        self.running = False
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass


class StreamHandler(BaseHTTPRequestHandler):
    camera_capture = None

    def log_message(self, format, *args):
        # Tắt log HTTP request liên tục để tránh ngập log terminal
        pass

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
        else:
            self.send_response(404)
            self.end_headers()


class CameraStreamerServer:
    def __init__(self, port=8080):
        self.port = port
        self.capture = CameraCapture()
        self.httpd = None
        self.thread = None

    def start(self):
        self.capture.start()
        StreamHandler.camera_capture = self.capture
        try:
            self.httpd = HTTPServer(("", self.port), StreamHandler)
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="camera-http")
            self.thread.start()
            log.info("Camera live stream đang mở tại http://0.0.0.0:%d/stream.mjpg", self.port)
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

