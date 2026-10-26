"""Quyết định gửi yêu cầu mở nắp của Pi drone (không cần camera thật).

    python -m unittest tests.test_delivery_tracker -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lockr_drone import delivery
from lockr_drone.delivery import DeliveryTracker

TARGET = {
    "eventType": "DELIVERY_TARGET", "missionId": 301, "orderId": 21, "lockerId": 7, "compartmentId": 9001,
    "arucoMarkerId": 23, "arucoDictionary": "DICT_4X4_50", "mode": "INDOOR_ARUCO_DEMO",
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class DeliveryTrackerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tracker = DeliveryTracker("DRONE-A1", stable_frames=5, stable_seconds=0.6, lost_reset_s=1.0,
                                       response_timeout_s=15, max_requests=3, cooldown_s=5, clock=self.clock)

    def frames(self, detections, count, dt=0.2):
        """Cho `count` frame cách nhau `dt` giây; trả các bản tin cần gửi."""
        sent = []
        for _ in range(count):
            payload = self.tracker.observe(detections)
            if payload:
                sent.append(payload)
            self.clock.now += dt
        return sent

    # 1. đúng ID, đúng nhiệm vụ ⇒ một yêu cầu
    def test_right_marker_for_the_mission_sends_one_request_after_it_is_stable(self):
        self.tracker.handle_command(TARGET)

        sent = self.frames([(23, (0.01, -0.02, 0.8))], 10)

        self.assertEqual(len(sent), 1)
        req = sent[0]
        self.assertEqual(req["eventType"], "MARKER_DETECTED")
        self.assertEqual(req["arucoMarkerId"], 23)
        self.assertEqual((req["missionId"], req["orderId"], req["lockerId"], req["compartmentId"]), (301, 21, 7, 9001))
        self.assertEqual(req["mode"], "INDOOR_ARUCO_DEMO")
        self.assertGreaterEqual(req["stableFrames"], 5)
        self.assertGreaterEqual(req["stableSeconds"], 0.6)
        self.assertEqual(req["distanceM"], 0.8)
        self.assertEqual(self.tracker.state, delivery.REQUEST_SENT)

    def test_a_single_frame_is_never_enough(self):
        self.tracker.handle_command(TARGET)
        self.assertEqual(self.frames([23], 1), [])
        # Đủ số frame nhưng chưa đủ thời gian (camera nhanh) cũng chưa gửi.
        self.assertEqual(self.frames([23], 4, dt=0.01), [])

    def test_losing_the_marker_restarts_the_stability_count(self):
        self.tracker.handle_command(TARGET)
        self.frames([23], 4)
        self.clock.now += 1.5               # mất dấu quá lost_reset_s
        self.frames([], 1)
        self.assertEqual(self.tracker.state, delivery.SCANNING)
        self.assertEqual(self.frames([23], 4), [])
        self.assertEqual(len(self.frames([23], 2)), 1)

    # 2. sai ID hoặc không có nhiệm vụ ⇒ không gửi
    def test_no_mission_means_no_request_even_for_a_visible_marker(self):
        self.assertEqual(self.frames([23, 7, 1], 30), [])
        self.assertEqual(self.tracker.state, delivery.NO_TARGET)

    def test_wrong_marker_is_ignored(self):
        self.tracker.handle_command(TARGET)
        self.assertEqual(self.frames([(8, (0, 0, 0.5))], 30), [])

    def test_target_from_another_dictionary_is_refused(self):
        self.tracker.handle_command(dict(TARGET, arucoDictionary="DICT_5X5_100"))
        self.assertEqual(self.frames([23], 10), [])
        self.assertEqual(self.tracker.state, delivery.FAILED)

    # 3. marker xuất hiện liên tục ⇒ không spam
    def test_marker_in_view_for_a_long_time_is_not_spammed(self):
        self.tracker.handle_command(TARGET)

        sent = self.frames([23], 50, dt=0.1)   # 5 giây liên tục trong khung hình

        self.assertEqual(len(sent), 1)

    def test_no_response_resends_a_limited_number_of_times_with_the_same_session(self):
        self.tracker.handle_command(TARGET)
        sent = []
        for _ in range(6):
            sent += self.frames([23], 10)
            self.clock.now += 16               # quá response_timeout_s, backend im lặng
            self.tracker.tick()

        self.assertEqual(len(sent), 3, "tối đa max_requests lần")
        self.assertEqual({s["sessionId"] for s in sent}, {sent[0]["sessionId"]})
        self.assertEqual(len({s["messageId"] for s in sent}), 3)
        self.assertEqual(self.tracker.state, delivery.FAILED)
        # Hết lượt thì nghỉ lâu rồi mới thử lại (không gửi dồn dập khi mất kết nối).
        self.clock.now += 120
        self.tracker.tick()
        self.assertEqual(self.tracker.state, delivery.SCANNING)

    # 7. nhiều marker ⇒ chỉ đúng tủ đích
    def test_with_several_markers_only_the_destination_marker_counts(self):
        self.tracker.handle_command(TARGET)

        sent = self.frames([(5, (0, 0, 0.4)), (23, (0.1, 0.0, 0.9)), (41, (0, 0, 1.2))], 10)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["arucoMarkerId"], 23)
        self.assertEqual(sent[0]["lockerId"], 7)
        self.assertEqual(sent[0]["distanceM"], 0.9)

    # trạng thái: đã gửi → tủ đã nhận → nắp đã mở (có/không công tắc xác nhận)
    def test_states_follow_the_backend_and_locker_replies(self):
        self.tracker.handle_command(TARGET)
        self.frames([23], 10)

        self.tracker.handle_command({"eventType": "MARKER_VALIDATED", "orderId": 21})
        self.assertEqual(self.tracker.state, delivery.VALIDATED)
        self.tracker.handle_command({"eventType": "LID_OPENING", "orderId": 21})
        self.assertEqual(self.tracker.state, delivery.LID_OPENING)
        self.tracker.handle_command({"eventType": "LID_OPENED", "orderId": 21, "lidConfirmation": "STEP_COUNT",
                                     "dropSimulationSeconds": 7,
                                     "errorMessage": "Nắp đã chạy hết hành trình theo số bước"})
        self.assertEqual(self.tracker.state, delivery.LID_OPEN)
        self.assertEqual(self.tracker.snapshot()["lidConfirmation"], "STEP_COUNT")
        self.assertIn("giả lập drone nhả hàng 7 giây", self.tracker.message)
        # Nắp mở không làm Pi gửi thêm gì (không tự đáp, không tự nhả hàng).
        self.assertEqual(self.frames([23], 20), [])

        self.tracker.handle_command({"eventType": "DELIVERY_READY", "orderId": 21})
        self.assertEqual(self.tracker.state, delivery.COMPLETED)

    def test_failure_is_shown_then_scanning_resumes_after_cooldown(self):
        self.tracker.handle_command(TARGET)
        self.frames([23], 10)
        self.tracker.handle_command({"eventType": "LID_OPEN_FAILED", "orderId": 21,
                                     "errorCode": "COMMAND_EXPIRED", "errorMessage": "Lệnh mở nắp đã quá hạn"})
        self.assertEqual(self.tracker.state, delivery.FAILED)
        self.assertIn("COMMAND_EXPIRED", self.tracker.message)

        self.clock.now += 6
        self.tracker.tick()
        self.assertEqual(self.tracker.state, delivery.SCANNING)

    def test_commands_for_another_order_are_ignored(self):
        self.tracker.handle_command(TARGET)
        self.frames([23], 10)
        self.tracker.handle_command({"eventType": "LID_OPENED", "orderId": 99})
        self.assertEqual(self.tracker.state, delivery.REQUEST_SENT)

    def test_no_target_from_backend_stops_scanning(self):
        self.tracker.handle_command(TARGET)
        self.tracker.handle_command({"eventType": "NO_TARGET", "errorMessage": "Đơn đã huỷ"})
        self.assertEqual(self.tracker.state, delivery.NO_TARGET)
        self.assertEqual(self.frames([23], 20), [])

    def test_target_requests_are_paced(self):
        self.assertIsNotNone(self.tracker.due_target_request())
        self.assertIsNone(self.tracker.due_target_request())
        self.clock.now += 11
        self.assertEqual(self.tracker.due_target_request()["eventType"], "TARGET_REQUEST")
        self.tracker.request_target_now()      # vừa kết nối lại broker
        self.assertIsNotNone(self.tracker.due_target_request())


if __name__ == "__main__":
    unittest.main()
