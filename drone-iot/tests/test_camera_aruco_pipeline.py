"""Đường đi thật của một frame: OpenCV dò ArUco → DeliveryTracker → bản tin MARKER_DETECTED.

Ảnh dựng bằng cv2.aruco (không cần camera). Bỏ qua nếu máy không có OpenCV.

    python -m unittest tests.test_camera_aruco_pipeline -v
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - máy không có OpenCV
    cv2 = None

from lockr_drone.delivery import DeliveryTracker

TARGET = {"eventType": "DELIVERY_TARGET", "missionId": 301, "orderId": 21, "lockerId": 7, "compartmentId": 9001,
          "arucoMarkerId": 23, "arucoDictionary": "DICT_4X4_50", "mode": "INDOOR_ARUCO_DEMO"}


def frame_with(*markers):
    """Ảnh 640x480 nền trắng, mỗi marker (id, x, y) cạnh 120 px."""
    img = np.full((480, 640, 3), 255, dtype=np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for marker_id, x, y in markers:
        tile = cv2.aruco.generateImageMarker(dictionary, marker_id, 120)
        img[y:y + 120, x:x + 120] = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
    return img


@unittest.skipIf(cv2 is None, "cần opencv-contrib-python")
class CameraArucoPipelineTest(unittest.TestCase):
    def setUp(self):
        from lockr_drone.camera import CameraCapture
        self.now = [0.0]
        self.tracker = DeliveryTracker("DRONE-A1", stable_frames=5, stable_seconds=0.6,
                                       clock=lambda: self.now[0])
        self.sent = []
        self.done = threading.Event()

        def publish(payload):
            self.sent.append(payload)
            self.done.set()

        self.camera = CameraCapture(on_marker_detected=publish, tracker=self.tracker)

    def run_frames(self, img, count, dt=0.2):
        for _ in range(count):
            self.camera._process_frame_and_annotate(img)
            self.now[0] += dt

    def test_destination_marker_among_others_triggers_one_request(self):
        self.tracker.handle_command(TARGET)

        self.run_frames(frame_with((8, 40, 60), (23, 260, 180), (41, 480, 300)), 15)

        self.assertTrue(self.done.wait(2))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["arucoMarkerId"], 23)
        self.assertEqual(self.sent[0]["lockerId"], 7)
        self.assertEqual(self.camera.last_detected_id, 23, "HUD bám marker của ô đích")
        self.assertEqual(self.camera.state, "REQUEST_SENT")

    def test_other_lockers_markers_never_trigger(self):
        self.tracker.handle_command(TARGET)

        self.run_frames(frame_with((8, 40, 60), (41, 480, 300)), 30)

        self.assertFalse(self.done.wait(0.3))
        self.assertEqual(self.sent, [])

    def test_without_a_mission_nothing_is_sent(self):
        self.run_frames(frame_with((23, 260, 180)), 30)

        self.assertFalse(self.done.wait(0.3))
        self.assertEqual(self.camera.state, "NO_TARGET")


if __name__ == "__main__":
    unittest.main()
