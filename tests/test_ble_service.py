import unittest
from unittest.mock import MagicMock, patch
from services.ble_service import BleAdvertiserService
from config.settings import settings


class TestBleAdvertiserService(unittest.TestCase):
    def setUp(self):
        self.original_ble_enabled = settings.BLE_ENABLED
        self.original_locker_code = settings.LOCKER_CODE
        self.original_locker_id = settings.LOCKER_ID
        self.original_dev_name = settings.BLE_DEVICE_NAME

    def tearDown(self):
        settings.BLE_ENABLED = self.original_ble_enabled
        settings.LOCKER_CODE = self.original_locker_code
        settings.LOCKER_ID = self.original_locker_id
        settings.BLE_DEVICE_NAME = self.original_dev_name

    def test_resolve_device_name_custom(self):
        service = BleAdvertiserService(device_name="LOCKR_CUSTOM")
        self.assertEqual(service.resolve_device_name(), "LOCKR_CUSTOM")

    def test_resolve_device_name_from_settings_code(self):
        settings.BLE_DEVICE_NAME = ""
        settings.LOCKER_CODE = "CAB-TU01"
        service = BleAdvertiserService()
        self.assertEqual(service.resolve_device_name(), "LOCKR_CAB-TU01")

    def test_resolve_device_name_from_cabinet_state(self):
        state = MagicMock()
        state.is_configured = True
        state.all_cabinets = [{"id": 7, "code": "CAB-TU01", "name": "Tủ thật TU01"}]
        service = BleAdvertiserService(cabinet_state=state)
        self.assertEqual(service.resolve_device_name(), "LOCKR_CAB-TU01")

    def test_start_and_stop_simulated(self):
        service = BleAdvertiserService(device_name="LOCKR_TEST")
        with patch("platform.system", return_value="Windows"):
            service.start()
            self.assertTrue(service._is_running)
            self.assertEqual(service._current_name, "LOCKR_TEST")
            service.stop()
            self.assertFalse(service._is_running)

    def test_bluetoothctl_fallback_puts_the_locker_name_in_the_advertisement(self):
        # Pi không có btmgmt ⇒ dùng bluetoothctl; tên phải nằm trong gói quảng bá, không chỉ là alias.
        settings.BLE_ENABLED = True
        service = BleAdvertiserService(device_name="LOCKR_CAB-TU01")
        proc = MagicMock()

        def run(cmd, **_kwargs):
            if "btmgmt" in cmd:
                raise FileNotFoundError("btmgmt")
            return MagicMock(returncode=0)

        with patch("platform.system", return_value="Linux"), \
                patch("os.path.exists", return_value=True), \
                patch("os.listdir", return_value=["hci0"]), \
                patch("subprocess.run", side_effect=run), \
                patch("subprocess.Popen", return_value=proc):
            service.start()

        written = "".join(call.args[0] for call in proc.stdin.write.call_args_list)
        self.assertIn("menu advertise\nname LOCKR_CAB-TU01\nback\n", written)
        self.assertLess(written.index("name LOCKR_CAB-TU01"), written.index("advertise on"))

    def test_ble_disabled(self):
        settings.BLE_ENABLED = False
        service = BleAdvertiserService(device_name="LOCKR_TEST")
        service.start()
        self.assertFalse(service._is_running)


if __name__ == "__main__":
    unittest.main()
