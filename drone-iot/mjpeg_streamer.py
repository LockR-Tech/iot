#!/usr/bin/env python3
"""
Camera Streamer cho Drone (MJPEG Stream)
Chạy trên Raspberry Pi: đọc camera USB (/dev/video0) hoặc Raspberry Pi Camera (CSI)
và phát luồng video trực tiếp qua HTTP tại http://<IP_Pi>:8080/stream.mjpg.
Frontend Admin của riêng drone này sẽ nhúng luồng này để hiển thị.
"""

import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

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
            except Exception as e:
                print(f"[WARN] Không mở được camera vật lý: {e}. Sử dụng khung hình giả lập.")
                self.cap = None
        else:
            print("[INFO] Không tìm thấy opencv-python, sử dụng chế độ tạo khung hình test.")

        thread = threading.Thread(target=self._capture_loop, daemon=True)
        thread.start()

    def _capture_loop(self):
        while self.running:
            frame_bytes = None
            if self.cap and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret:
                    # Đóng dấu thời gian lên khung hình
                    cv2.putText(
                        frame,
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 255, 0),
                        2,
                    )
                    ret_enc, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    if ret_enc:
                        frame_bytes = buffer.tobytes()

            if frame_bytes is None:
                # Tạo khung hình mẫu đơn giản nếu không có camera thật
                frame_bytes = self._generate_fallback_jpeg()

            with self.lock:
                self.current_frame = frame_bytes

            time.sleep(1.0 / self.fps)

    def _generate_fallback_jpeg(self):
        # Fallback JPEG tối giản để test web khi chưa cắm webcam
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


camera_capture = CameraCapture()


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/stream.mjpg" or self.path == "/":
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    frame = camera_capture.get_frame()
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
            frame = camera_capture.get_frame()
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


def run_server(port=8080):
    camera_capture.start()
    server_address = ("", port)
    httpd = HTTPServer(server_address, StreamHandler)
    print(f"[OK] Drone Camera Streamer đang chạy tại port {port} (/stream.mjpg)...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Đang dừng camera streamer...")
        camera_capture.running = False
        httpd.server_close()


if __name__ == "__main__":
    port = 8080
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    run_server(port)

