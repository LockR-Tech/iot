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
                                     "errorMessage": "Nắp đã chạy hết hành trình theo số bước"})
        self.assertEqual(self.tracker.state, delivery.LID_OPEN)
        self.assertEqual(self.tracker.snapshot()["lidConfirmation"], "STEP_COUNT")
        # Không có lệnh nhả hàng (giao ngoài trời): Pi không tự báo gì.
        self.assertEqual(self.frames([23], 20), [])
        self.clock.now += 60
        self.assertIsNone(self.tracker.due_drop_report())

        self.tracker.handle_command({"eventType": "DELIVERY_READY", "orderId": 21})
        self.assertEqual(self.tracker.state, delivery.COMPLETED)

    def open_lid_with_drop_command(self, seconds=7):
        self.tracker.handle_command(TARGET)
        self.frames([23], 10)
        self.tracker.handle_command({"eventType": "MARKER_VALIDATED", "orderId": 21})
        self.tracker.handle_command({"eventType": "LID_OPENING", "orderId": 21})
        self.tracker.handle_command({"eventType": "LID_OPENED", "orderId": 21, "sessionId": "SESSION-21",
                                     "correlationId": "corr-21", "lidConfirmation": "STEP_COUNT",
                                     "dropSimulationSeconds": seconds})

    # lệnh nhả hàng: giả lập đúng số giây cấu hình rồi báo DROP_PHASE_COMPLETED đúng một lần
    def test_drop_command_is_simulated_for_the_configured_seconds_then_reported(self):
        self.open_lid_with_drop_command(7)
        self.assertEqual(self.tracker.state, delivery.DROPPING)
        self.assertIn("7 giây", self.tracker.message)

        self.clock.now += 6.5
        self.assertIsNone(self.tracker.due_drop_report(), "báo nhả hàng trước khi hết thời gian giả lập")

        self.clock.now += 0.6
        report = self.tracker.due_drop_report()
        self.assertEqual(report["eventType"], "DROP_PHASE_COMPLETED")
        self.assertEqual(report["sessionId"], "SESSION-21")
        self.assertEqual(report["correlationId"], "corr-21")
        self.assertEqual((report["orderId"], report["lockerId"], report["missionId"]), (21, 7, 301))
        self.assertEqual(report["droneId"], "DRONE-A1")
        self.assertEqual(self.tracker.state, delivery.DROP_REPORTED)
        self.assertIsNone(self.tracker.due_drop_report())

        self.tracker.handle_command({"eventType": "LID_CLOSING", "orderId": 21})
        self.assertEqual(self.tracker.state, delivery.LID_CLOSING)
        self.clock.now += 60
        self.assertIsNone(self.tracker.due_drop_report(), "đã được xác nhận mà vẫn gửi lại")

        self.tracker.handle_command({"eventType": "DELIVERY_READY", "orderId": 21})
        self.assertEqual(self.tracker.state, delivery.COMPLETED)

    def test_duplicate_lid_opened_does_not_restart_the_drop(self):
        self.open_lid_with_drop_command(7)
        self.clock.now += 8
        self.assertIsNotNone(self.tracker.due_drop_report())
        self.tracker.handle_command({"eventType": "LID_OPENED", "orderId": 21, "dropSimulationSeconds": 7})
        self.assertEqual(self.tracker.state, delivery.DROP_REPORTED)

    def test_unconfirmed_drop_report_is_resent_a_limited_number_of_times(self):
        self.open_lid_with_drop_command(3)
        self.clock.now += 3
        sent = [self.tracker.due_drop_report()]
        for _ in range(5):
            self.clock.now += 15
            sent.append(self.tracker.due_drop_report())
        reports = [r for r in sent if r]
        self.assertEqual(len(reports), 3)
        self.assertEqual({r["sessionId"] for r in reports}, {"SESSION-21"})
        self.assertEqual(len({r["messageId"] for r in reports}), 3)
        self.assertEqual(self.tracker.state, delivery.FAILED)

    def test_report_that_could_not_be_sent_is_retried_without_using_up_attempts(self):
        self.open_lid_with_drop_command(2)
        self.clock.now += 2
        self.assertIsNotNone(self.tracker.due_drop_report())
        self.tracker.drop_report_failed()          # MQTT mất kết nối lúc gửi
        retry = self.tracker.due_drop_report()
        self.assertEqual(retry["attempt"], 1)

    def test_restart_while_lid_is_open_resumes_the_drop_from_the_target(self):
        self.tracker.handle_command(dict(TARGET, sessionState="LID_OPENED", sessionId="SESSION-21",
                                         dropSimulationSeconds=5))
        self.assertEqual(self.tracker.state, delivery.DROPPING)
        self.clock.now += 5
        self.assertEqual(self.tracker.due_drop_report()["sessionId"], "SESSION-21")

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

    def test_empty_camera_never_asks_for_a_target(self):
        for _ in range(30):                    # 5 phút camera trống / quay xung quanh
            self.tracker.observe([])
            self.assertIsNone(self.tracker.due_target_request())
            self.clock.now += 10

    def test_target_is_asked_only_while_a_marker_is_in_view_and_paced(self):
        self.tracker.observe([5])              # thấy một mã ArUco bất kỳ
        self.assertEqual(self.tracker.due_target_request()["eventType"], "TARGET_REQUEST")
        self.assertIsNone(self.tracker.due_target_request())
        self.clock.now += 11
        self.tracker.observe([5])
        self.assertIsNotNone(self.tracker.due_target_request())

        self.clock.now += 11                   # mã đã ra khỏi khung hình
        self.tracker.observe([])
        self.assertIsNone(self.tracker.due_target_request())

        self.tracker.request_target_now()      # vừa kết nối lại broker: chưa thấy mã thì vẫn im lặng
        self.assertIsNone(self.tracker.due_target_request())
        self.tracker.observe([23])
        self.assertIsNotNone(self.tracker.due_target_request())

    # ─── chế độ thử nắp (admin bật): mục tiêu không gắn đơn ───

    LID_TEST_TARGET = {"eventType": "DELIVERY_TARGET", "lockerId": 7, "compartmentId": 39,
                       "arucoMarkerId": 0, "arucoDictionary": "DICT_4X4_50", "mode": "LID_TEST"}

    def test_target_request_tells_the_system_which_markers_are_in_view(self):
        self.tracker.observe([(0, (0.0, 0.0, 0.4)), 1])
        request = self.tracker.due_target_request()
        self.assertEqual(request["visibleMarkers"], [0, 1])

    def test_lid_test_target_without_order_leads_to_a_lid_test_marker_event(self):
        self.tracker.observe([0])
        self.tracker.due_target_request()
        self.tracker.handle_command(self.LID_TEST_TARGET)
        self.assertEqual(self.tracker.state, delivery.SCANNING)

        sent = self.frames([0], 10)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["mode"], "LID_TEST")
        self.assertIsNone(sent[0]["orderId"])
        self.assertEqual((sent[0]["lockerId"], sent[0]["compartmentId"], sent[0]["arucoMarkerId"]), (7, 39, 0))

    def test_after_a_lid_test_the_marker_must_leave_the_view_before_testing_again(self):
        self.tracker.observe([0])
        self.tracker.handle_command(self.LID_TEST_TARGET)
        self.frames([0], 10)
        self.tracker.handle_command({"eventType": "LID_OPENED", "sessionId": "S"})
        self.tracker.handle_command({"eventType": "LID_TEST_DONE", "errorMessage": "Đã thử xong"})
        self.assertEqual(self.tracker.state, delivery.NO_TARGET)

        for _ in range(5):                       # camera vẫn chĩa vào mã: không thử lại
            self.tracker.observe([0])
            self.clock.now += 11
            self.tracker.observe([0])
            self.assertIsNone(self.tracker.due_target_request())

        self.clock.now += 3                      # mã rời khung hình > 2 giây
        self.tracker.observe([])
        self.assertIsNone(self.tracker.due_target_request())
        self.tracker.observe([0])                # đưa mã vào lại ⇒ được thử lần mới
        self.assertIsNotNone(self.tracker.due_target_request())

    def test_failed_lid_test_does_not_retry_by_itself(self):
        self.tracker.observe([0])
        self.tracker.handle_command(self.LID_TEST_TARGET)
        self.frames([0], 10)
        self.tracker.handle_command({"eventType": "LID_OPEN_FAILED", "errorCode": "LOCKER_NO_ACK"})
        self.assertEqual(self.tracker.state, delivery.NO_TARGET)
        self.clock.now += 30
        self.assertEqual(self.frames([0], 20), [])

    def test_marker_seen_then_target_arrives_then_lid_request_is_sent(self):
        self.tracker.observe([23])
        self.assertIsNotNone(self.tracker.due_target_request())
        self.tracker.handle_command(TARGET)
        sent = self.frames([23], 10)
        self.assertEqual([p["eventType"] for p in sent], ["MARKER_DETECTED"])


if __name__ == "__main__":
    unittest.main()
