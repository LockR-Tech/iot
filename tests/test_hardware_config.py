"""Cấu hình phần cứng sửa trên admin web: kiểm tra hợp lệ, ghi đè `.env` lúc khởi động, lệnh MQTT
`cabinet/{id}/command/hw-config`, báo cấu hình đang chạy trong `panel`, script dòng an toàn lúc khởi động.

    uv run python -m unittest tests.test_hardware_config -v
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import hardware_config
from config.settings import settings
from infracstructure.cabinet_state import CabinetState
from services.locker_service import RESTART_EXIT_CODE, LockerService
from tests.test_mqtt_contract import FakeMqtt

# CAB-TU01 như đang chạy (bản đồ chân của người làm tủ, trục 1 nằm trên ô 3).
TU01 = {
    "relayPins": [5, 6, 13, 19, 26, 22, 23],
    "relayActiveHigh": True,
    "unlockMs": 1000,
    "doorPins": [4, 12, 16, 20, 21, 24, 25],
    "doorClosedLow": True,
    "requireDoorSensor": False,
    "lidBypassPhysicalMotion": False,
    "lids": [
        {"axis": 1, "enabled": True, "pulPin": 18, "dirPin": 27, "homePin": 17, "endPin": 10,
         "openDirHigh": True, "limitActiveLow": True, "boxes": [3]},
        {"axis": 2, "enabled": False, "pulPin": 9, "dirPin": 11, "homePin": 7, "endPin": 8,
         "openDirHigh": True, "limitActiveLow": True, "boxes": []},
    ],
}


def cfg(**changes):
    out = copy.deepcopy(TU01)
    out.update(changes)
    return out


def rewired():
    """Bản đồ khác hẳn nhưng hợp lệ: relay về chân mặc định của code, nắp trục 1 dời chân cho khỏi trùng."""
    out = cfg(relayPins=[17, 27, 22, 23, 6, 5, 16], doorPins=[4, 12, 13, 20, 21, 24, 25])
    out["lids"][0].update({"pulPin": 18, "dirPin": 19, "homePin": 26, "endPin": 10})
    return out


class ValidateTest(unittest.TestCase):
    def test_tu01_is_valid(self):
        self.assertEqual(hardware_config.validate(TU01), [])

    def test_pin_used_twice_is_rejected(self):
        errors = hardware_config.validate(cfg(doorPins=[4, 12, 13, 20, 21, 24, 25]))  # 13 = relay ô 3
        self.assertTrue(any("GPIO13 bị dùng hai lần" in e for e in errors), errors)

    def test_enabled_lid_pin_conflicts_but_disabled_lid_is_ignored(self):
        bad = cfg()
        bad["lids"][0]["dirPin"] = 21                                  # cửa ô 5
        self.assertTrue(any("GPIO21" in e for e in hardware_config.validate(bad)))
        off = cfg()
        off["lids"][1]["pulPin"] = 5                                   # trục 2 tắt: không tính
        self.assertEqual(hardware_config.validate(off), [])

    def test_bad_values(self):
        self.assertTrue(hardware_config.validate(cfg(relayPins=[5, 6, 30, 19, 26, 22, 23])))
        self.assertTrue(hardware_config.validate(cfg(unlockMs=50)))
        self.assertTrue(hardware_config.validate(cfg(doorPins=[4, 12])))
        self.assertTrue(hardware_config.validate(cfg(relayActiveHigh="yes")))
        self.assertTrue(hardware_config.validate("not a dict"))
        overlap = cfg()
        overlap["lids"][1]["boxes"] = [3]
        self.assertTrue(any("ô 3" in e.lower() for e in hardware_config.validate(overlap)))
        outside = cfg()
        outside["lids"][0]["boxes"] = [9]
        self.assertTrue(hardware_config.validate(outside))


class OverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "hardware_config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_file_overrides_env(self):
        hardware_config.save(rewired(), self.path)
        with mock.patch.dict(os.environ, {"GPIO_RELAY_PINS": "5,6", "LID1_BOXES": ""}):
            self.assertIsNotNone(hardware_config.apply_overlay(self.path))
            self.assertEqual(os.environ["GPIO_RELAY_PINS"], "17,27,22,23,6,5,16")
            self.assertEqual(os.environ["LID_DIR_PIN"], "19")
            self.assertEqual(os.environ["LID1_BOXES"], "3")
            self.assertEqual(os.environ["LID2_ENABLED"], "false")
            self.assertEqual(os.environ["REQUIRE_DOOR_SENSOR"], "false")

    def test_invalid_or_broken_file_is_ignored_entirely(self):
        hardware_config.save(cfg(unlockMs=1), self.path)
        with mock.patch.dict(os.environ, {"GPIO_RELAY_PINS": "5,6"}):
            self.assertIsNone(hardware_config.apply_overlay(self.path))
            self.assertEqual(os.environ["GPIO_RELAY_PINS"], "5,6")
        Path(self.path).write_text("{half", encoding="utf-8")
        self.assertIsNone(hardware_config.apply_overlay(self.path))
        self.assertIsNone(hardware_config.apply_overlay(os.path.join(self.tmp.name, "missing.json")))

    def test_save_keeps_previous_as_backup(self):
        hardware_config.save(TU01, self.path)
        hardware_config.save(cfg(unlockMs=2000), self.path)
        self.assertEqual(json.loads(Path(self.path + ".bak").read_text(encoding="utf-8"))["unlockMs"], 1000)

    def test_effective_reports_settings(self):
        eff = hardware_config.effective(settings)
        self.assertEqual(eff["relayPins"], list(settings.GPIO_RELAY_PINS))
        self.assertEqual([l["axis"] for l in eff["lids"]], [1, 2])


class HwConfigCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "hardware_config.json")
        self.tuning_path = os.path.join(self.tmp.name, "lid_tuning.json")
        Path(self.tuning_path).write_text(json.dumps({"1": {"rps": 1.0, "open_dir_high": False,
                                                             "limit_active_low": False}}), encoding="utf-8")
        self.patches = [
            mock.patch.object(settings, "HARDWARE_CONFIG_FILE", self.cfg_path),
            mock.patch.object(settings, "LID_TUNING_FILE", self.tuning_path),
            mock.patch.object(settings, "HARDWARE_BACKEND", "gpio"),
            mock.patch.object(settings, "GPIO_RELAY_PINS", list(TU01["relayPins"])),
        ]
        for p in self.patches:
            p.start()
        self.mqtt = FakeMqtt()
        self.exits = []
        self.state = CabinetState(state_path=os.path.join(self.tmp.name, "s.json"), fallback_locker_id="7")
        self.service = LockerService(self.mqtt, None, cabinet_state=self.state,
                                     exit_process=lambda code: self.exits.append(code))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def send(self, payload, action="hw-config"):
        self.service.handle_incoming_message(f"cabinet/7/command/{action}", json.dumps(payload))
        self.service._executor.submit(lambda: None).result(timeout=5)

    def result(self, action="hw-config"):
        return self.mqtt.on(f"cabinet/7/command/{action}/result")[-1]

    @mock.patch("services.locker_service.subprocess.run")
    def test_valid_config_is_saved_then_controller_restarts(self, run):
        self.send({"commandId": "h-1", "config": TU01})
        r = self.result()
        self.assertEqual(r["status"], "ACCEPTED")
        self.assertEqual(r["warnings"], [])
        self.assertEqual(json.loads(Path(self.cfg_path).read_text(encoding="utf-8"))["relayPins"], TU01["relayPins"])
        self.assertEqual(self.exits, [RESTART_EXIT_CODE])
        # chiều/mức công tắc trong lid_tuning.json đổi theo cấu hình
        self.assertTrue(json.loads(Path(self.tuning_path).read_text(encoding="utf-8"))["1"]["open_dir_high"])
        run.assert_not_called()                                         # chân relay không đổi
        self.assertIn("cabinet/7/command/hw-config/result", [t for t, *_ in self.mqtt.waited])

    @mock.patch("services.locker_service.subprocess.run")
    def test_changed_relay_pins_update_boot_line(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{"ok": true}\n', stderr="")
        self.send({"commandId": "h-2", "config": rewired()})
        self.assertEqual(self.result()["status"], "ACCEPTED")
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["sudo", "-n", "python3"])
        self.assertEqual(args[-1], "17,27,22,23,6,5,16")

    @mock.patch("services.locker_service.subprocess.run")
    def test_boot_line_failure_is_reported_as_warning(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="sudo: a password is required")
        self.send({"commandId": "h-3", "config": cfg(relayPins=[5, 6, 13, 19, 26, 22, 9])})
        r = self.result()
        self.assertEqual(r["status"], "ACCEPTED")
        self.assertTrue(r["warnings"] and "sudo" in r["warnings"][0])

    def test_invalid_config_changes_nothing(self):
        self.send({"commandId": "h-4", "config": cfg(doorPins=[5, 12, 16, 20, 21, 24, 25])})
        r = self.result()
        self.assertEqual(r["status"], "FAILED")
        self.assertEqual(r["errorCode"], "INVALID_CONFIG")
        self.assertFalse(os.path.exists(self.cfg_path))
        self.assertEqual(self.exits, [])

    def test_panel_reports_running_hardware(self):
        self.send({"commandId": "p-1"}, action="panel")
        hw = self.result("panel")["hardware"]
        self.assertEqual(hw["relayPins"], TU01["relayPins"])
        self.assertEqual(len(hw["lids"]), 2)


class BootRelayScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location(
            "boot_relay_pins", Path(__file__).parent.parent / "scripts" / "boot_relay_pins.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.config_txt = Path(self.tmp.name, "config.txt")
        self.unit = Path(self.tmp.name, "lockr-controller.service")
        self.config_txt.write_text("[all]\ndtparam=audio=on\ngpio=5,6,13,19,26,22,23=op,dl\n", encoding="utf-8")
        self.unit.write_text("[Service]\nExecStart=/x\nExecStopPost=/usr/bin/pinctrl set 5,6,13,19,26,22,23 op dl\n",
                             encoding="utf-8")
        self.mod.CONFIG_TXT = (str(self.config_txt),)
        self.mod.UNIT = str(self.unit)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *argv):
        with mock.patch.object(sys, "argv", ["boot_relay_pins.py", *argv]), \
                mock.patch.object(self.mod.subprocess, "run") as run, \
                mock.patch("builtins.print") as out:
            code = self.mod.main()
        return code, json.loads(out.call_args[0][0]), run

    def test_rewrites_both_lines_with_backup(self):
        code, report, run = self.run_script("17,27,22")
        self.assertEqual(code, 0)
        self.assertIn("gpio=17,27,22=op,dl", self.config_txt.read_text(encoding="utf-8"))
        self.assertIn("pinctrl set 17,27,22 op dl", self.unit.read_text(encoding="utf-8"))
        self.assertTrue(report["configTxt"]["changed"] and report["unit"]["changed"])
        run.assert_called_once()                                        # daemon-reload
        self.assertEqual(len(list(Path(self.tmp.name).glob("*.bak-*"))), 2)

    def test_rejects_bad_pins(self):
        code, report, _ = self.run_script("5,5,99")
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertIn("gpio=5,6,13", self.config_txt.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
