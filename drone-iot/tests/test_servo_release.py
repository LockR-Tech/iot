"""Nhả hàng bằng servo: chu trình servo và luồng lệnh nhả hàng của Pi drone (không cần servo thật).

    python -m unittest tests.test_servo_release -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lockr_drone import delivery
from lockr_drone.delivery import DeliveryTracker
from lockr_drone.release import ServoReleaser

TARGET = {
    "eventType": "DELIVERY_TARGET", "missionId": 301, "orderId": 21, "lockerId": 7, "compartmentId": 9001,
    "arucoMarkerId": 23, "arucoDictionary": "DICT_4X4_50", "mode": "REAL_OUTDOOR",
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeServo:
    """Ghi lại các góc đã quay; `fail` = danh sách góc ném lỗi (mỗi phần tử dùng một lần)."""

    def __init__(self, fail=()):
        self.moves = []
        self.fail = list(fail)

    def __call__(self, angle):
        if angle in self.fail:
            self.fail.remove(angle)
            raise RuntimeError("Không tìm thấy thư viện GPIO phù hợp trên Pi 5")
        self.moves.append(angle)


class ServoReleaserTest(unittest.TestCase):
    def releaser(self, servo):
        self.waits = []
        return ServoReleaser(servo, open_angle=110, lock_angle=0, hold_s=2, attempts=2, sleep=self.waits.append)

    def test_release_opens_holds_then_locks_the_latch(self):
        servo = FakeServo()
        result = self.releaser(servo).release()
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(servo.moves, [110, 0])
        self.assertEqual(self.waits, [2])

    def test_open_failure_is_retried_once(self):
        servo = FakeServo(fail=[110])
        result = self.releaser(servo).release()
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(servo.moves, [110, 0])

    def test_open_failing_on_every_attempt_reports_the_error(self):
        servo = FakeServo(fail=[110, 110])
        result = self.releaser(servo).release()
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertIn("GPIO", result.error)
        self.assertEqual(servo.moves, [])

    def test_details_carry_the_real_servo_numbers_for_the_backend_log(self):
        clock = Clock()

        def move(angle):
            clock.now += 0.8      # mỗi lần quay cấp xung 0.8 giây

        releaser = ServoReleaser(move, open_angle=180, lock_angle=0, hold_s=5, attempts=2,
                                 sleep=lambda s: setattr(clock, "now", clock.now + s), clock=clock)
        result = releaser.release()
        details = releaser.details(result, gpio_pin=18, gpio_backend="lgpio")
        self.assertEqual(result.duration_s, 6.6)
        self.assertEqual(details["servoOpenAngle"], 180)
        self.assertEqual(details["servoLockAngle"], 0)
        self.assertEqual(details["servoHoldSeconds"], 5)
        self.assertEqual(details["releaseAttempts"], 1)
        self.assertEqual(details["releaseDurationSeconds"], 6.6)
        self.assertEqual((details["servoGpioPin"], details["gpioBackend"]), (18, "lgpio"))
        self.assertIn("releasedAt", details)

    def test_latch_that_opened_but_did_not_relock_still_counts_as_released(self):
        servo = FakeServo(fail=[0])
        result = self.releaser(servo).release()
        self.assertTrue(result.ok, "kiện đã rơi khi chốt mở — không được báo lỗi nhả hàng")
        self.assertEqual(result.attempts, 1)
        self.assertIn("không đóng lại", result.warning)
        self.assertEqual(servo.moves, [110])


class ServoDeliveryFlowTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tracker = self.make(servo_enabled=True)

    def make(self, servo_enabled):
        return DeliveryTracker("DRONE-A1", stable_frames=5, stable_seconds=0.6, response_timeout_s=15,
                               max_requests=3, cooldown_s=5, servo_enabled=servo_enabled, clock=self.clock)

    def open_lid(self, tracker=None, **extra):
        tracker = tracker or self.tracker
        tracker.handle_command(dict(TARGET, mode=extra.pop("mode", "REAL_OUTDOOR")))
        for _ in range(10):
            tracker.observe([23])
            self.clock.now += 0.2
        tracker.handle_command({"eventType": "MARKER_VALIDATED", "orderId": 21})
        tracker.handle_command({"eventType": "LID_OPENING", "orderId": 21})
        command = {"eventType": "LID_OPENED", "orderId": 21, "sessionId": "SESSION-21", "correlationId": "corr-21",
                   "lidConfirmation": "STEP_COUNT"}
        command.update(extra)
        tracker.handle_command(command)

    # cửa mở + lệnh nhả ⇒ servo nhả đúng một lần ⇒ báo DROP_PHASE_COMPLETED ⇒ tủ đóng nắp
    def test_lid_opened_with_release_command_runs_the_servo_once_then_reports(self):
        self.open_lid(parcelRelease=True)
        self.assertEqual(self.tracker.state, delivery.RELEASING)
        self.assertIsNone(self.tracker.due_drop_report(), "báo nhả hàng trước khi servo quay")

        self.assertTrue(self.tracker.claim_release())
        self.assertFalse(self.tracker.claim_release(), "servo bị giao việc hai lần")

        self.tracker.release_finished(True, 1, details={
            "servoOpenAngle": 180.0, "servoHoldSeconds": 5.0, "releaseDurationSeconds": 6.6,
            "releaseAttempts": 1, "servoGpioPin": 18, "gpioBackend": "lgpio"})
        report = self.tracker.due_drop_report()
        self.assertEqual(report["eventType"], "DROP_PHASE_COMPLETED")
        self.assertEqual(report["releaseMethod"], "SERVO")
        self.assertEqual(report["releaseAttempts"], 1)
        self.assertEqual((report["servoOpenAngle"], report["servoHoldSeconds"], report["releaseDurationSeconds"]),
                         (180.0, 5.0, 6.6))
        self.assertEqual((report["servoGpioPin"], report["gpioBackend"]), (18, "lgpio"))
        self.assertNotIn("dropSimulationSeconds", report)
        self.assertEqual((report["sessionId"], report["orderId"], report["lockerId"]), ("SESSION-21", 21, 7))
        self.assertEqual(self.tracker.state, delivery.DROP_REPORTED)

        self.tracker.handle_command({"eventType": "LID_CLOSING", "orderId": 21})
        self.tracker.handle_command({"eventType": "DELIVERY_READY", "orderId": 21})
        self.assertEqual(self.tracker.state, delivery.COMPLETED)

    def test_duplicate_lid_opened_does_not_release_again(self):
        self.open_lid(parcelRelease=True)
        self.assertTrue(self.tracker.claim_release())
        self.tracker.handle_command({"eventType": "LID_OPENED", "orderId": 21, "parcelRelease": True})
        self.assertFalse(self.tracker.claim_release())
        self.tracker.release_finished(True, 1)
        self.assertIsNotNone(self.tracker.due_drop_report())
        self.tracker.handle_command({"eventType": "LID_OPENED", "orderId": 21, "parcelRelease": True})
        self.assertEqual(self.tracker.state, delivery.DROP_REPORTED)
        self.assertFalse(self.tracker.claim_release())

    # bật servo thì đơn demo cũng nhả bằng servo, không giả lập nữa
    def test_demo_order_uses_the_servo_when_it_is_enabled(self):
        self.open_lid(mode="INDOOR_ARUCO_DEMO", parcelRelease=True, dropSimulationSeconds=7)
        self.assertEqual(self.tracker.state, delivery.RELEASING)
        self.assertTrue(self.tracker.claim_release())

    def test_old_backend_demo_command_without_parcel_release_still_uses_the_servo(self):
        self.open_lid(mode="INDOOR_ARUCO_DEMO", dropSimulationSeconds=7)
        self.assertEqual(self.tracker.state, delivery.RELEASING)

    def test_lid_opened_without_release_command_does_not_touch_the_servo(self):
        self.open_lid()
        self.assertEqual(self.tracker.state, delivery.LID_OPEN)
        self.assertFalse(self.tracker.claim_release())
        self.clock.now += 60
        self.assertIsNone(self.tracker.due_drop_report())

    # servo lỗi (đã thử lại) ⇒ báo PARCEL_RELEASE_FAILED, gửi lại tới khi backend xác nhận, không tự giao lại
    def test_servo_failure_is_reported_until_acknowledged_and_never_rescans(self):
        self.open_lid(parcelRelease=True)
        self.assertTrue(self.tracker.claim_release())
        self.tracker.release_finished(False, 2, error="RuntimeError: Không tìm thấy thư viện GPIO",
                                      details={"servoOpenAngle": 180.0, "servoGpioPin": 18, "gpioBackend": None,
                                               "releaseAttempts": 2, "releaseDurationSeconds": 0.1})
        self.assertEqual(self.tracker.state, delivery.RELEASE_FAILED)

        report = self.tracker.due_drop_report()
        self.assertEqual(report["eventType"], "PARCEL_RELEASE_FAILED")
        self.assertEqual(report["errorCode"], "SERVO_ERROR")
        self.assertIn("GPIO", report["errorMessage"])
        self.assertEqual(report["releaseAttempts"], 2)
        self.assertEqual((report["servoOpenAngle"], report["servoGpioPin"]), (180.0, 18))
        self.assertEqual(report["sessionId"], "SESSION-21")
        self.assertIsNone(self.tracker.due_drop_report())

        self.clock.now += 15
        resent = self.tracker.due_drop_report()
        self.assertEqual(resent["eventType"], "PARCEL_RELEASE_FAILED")
        self.assertEqual(resent["attempt"], 2)

        self.tracker.handle_command({"eventType": "LID_CLOSING", "orderId": 21,
                                     "errorMessage": "Đã nhận báo servo lỗi, tủ đang đóng nắp"})
        self.clock.now += 60
        self.assertIsNone(self.tracker.due_drop_report(), "đã được xác nhận mà vẫn gửi lại")
        self.tracker.handle_command({"eventType": "PARCEL_RELEASE_FAILED", "orderId": 21,
                                     "errorMessage": "Servo trên drone không nhả được kiện hàng"})
        self.tracker.handle_command({"eventType": "LID_CLOSE_FAILED", "orderId": 21, "errorCode": "X"})
        self.assertEqual(self.tracker.state, delivery.RELEASE_FAILED)

        self.clock.now += 300
        self.tracker.tick()
        self.assertEqual(self.tracker.state, delivery.RELEASE_FAILED, "servo lỗi mà tự quét lại để giao lần nữa")
        self.assertEqual([p for p in (self.tracker.observe([23]) for _ in range(20)) if p], [])

    def test_failure_report_stops_after_max_attempts_but_stays_failed(self):
        self.open_lid(parcelRelease=True)
        self.tracker.claim_release()
        self.tracker.release_finished(False, 2, error="servo")
        sent = [self.tracker.due_drop_report()]
        for _ in range(5):
            self.clock.now += 15
            sent.append(self.tracker.due_drop_report())
        self.assertEqual(len([r for r in sent if r]), 3)
        self.assertEqual(self.tracker.state, delivery.RELEASE_FAILED)

    def test_servo_that_hangs_is_reported_as_failed(self):
        self.open_lid(parcelRelease=True)
        self.tracker.claim_release()
        self.clock.now += delivery.RELEASE_TIMEOUT_S + 1
        self.tracker.tick()
        self.assertEqual(self.tracker.state, delivery.RELEASE_FAILED)
        self.assertEqual(self.tracker.due_drop_report()["errorCode"], "SERVO_TIMEOUT")
        self.tracker.release_finished(True, 1)   # servo xong quá muộn: bỏ qua
        self.assertEqual(self.tracker.state, delivery.RELEASE_FAILED)

    # Pi chưa bật servo
    def test_real_delivery_without_servo_reports_failure_so_the_lid_closes(self):
        tracker = self.make(servo_enabled=False)
        self.open_lid(tracker, parcelRelease=True)
        self.assertEqual(tracker.state, delivery.RELEASE_FAILED)
        self.assertFalse(tracker.claim_release())
        report = tracker.due_drop_report()
        self.assertEqual(report["eventType"], "PARCEL_RELEASE_FAILED")
        self.assertEqual(report["errorCode"], "SERVO_DISABLED")

    def test_demo_without_servo_keeps_the_simulated_drop(self):
        tracker = self.make(servo_enabled=False)
        self.open_lid(tracker, mode="INDOOR_ARUCO_DEMO", parcelRelease=True, dropSimulationSeconds=3)
        self.assertEqual(tracker.state, delivery.DROPPING)
        self.clock.now += 3
        report = tracker.due_drop_report()
        self.assertEqual(report["eventType"], "DROP_PHASE_COMPLETED")
        self.assertEqual(report["releaseMethod"], "SIMULATION")
        self.assertEqual(report["dropSimulationSeconds"], 3)

    def test_restart_while_lid_is_open_releases_from_the_target(self):
        self.tracker.handle_command(dict(TARGET, sessionState="LID_OPENED", sessionId="SESSION-21",
                                         parcelRelease=True))
        self.assertEqual(self.tracker.state, delivery.RELEASING)
        self.assertTrue(self.tracker.claim_release())
        self.tracker.release_finished(True, 1)
        self.assertEqual(self.tracker.due_drop_report()["sessionId"], "SESSION-21")


if __name__ == "__main__":
    unittest.main()
