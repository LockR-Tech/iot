"""Wi-Fi trên bảng /service: đọc/lưu/xoá mạng qua nmcli, chuyển mạng có đường lui.

    uv run python -m unittest tests.test_wifi -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infracstructure.wifi import WifiManager, _split_terse
from scripts import wifi_switch

SAVED = ("Mang_chinh:802-11-wireless:yes:80:yes\n"
         "Nha\\:Tro:802-11-wireless:yes:5:no\n"
         "Wired:802-3-ethernet:yes:0:no\n")


class FakeRun:
    def __init__(self, saved=SAVED):
        self.saved, self.calls = saved, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = ""
        if cmd[-2:] == ["connection", "show"]:
            out = self.saved
        elif cmd[:2] == ["nmcli", "-g"]:
            out = cmd[-1] + "\n"
        elif "IP4.ADDRESS" in cmd:
            out = "IP4.ADDRESS[1]:192.168.1.50/24\n"
        elif "list" in cmd:
            out = "Mang_chinh:78:WPA2\nCafe:30:\nMang_chinh:60:WPA2\n:20:WPA2\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def writes(self):
        return [c for c in self.calls if c[:2] == ["sudo", "-n"]]


class WifiManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = FakeRun()
        self.wifi = WifiManager(run=self.run, result_file=os.path.join(self.tmp.name, "result.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_terse_split_handles_escaped_colon(self):
        self.assertEqual(_split_terse("Nha\\:Tro:802-11-wireless:yes"), ["Nha:Tro", "802-11-wireless", "yes"])

    def test_status_lists_only_wifi_active_first(self):
        Path(self.wifi.result_file).write_text(json.dumps({"ok": False, "reverted": True}), encoding="utf-8")
        st = self.wifi.status()
        self.assertEqual([n["name"] for n in st["saved"]], ["Mang_chinh", "Nha:Tro"])
        self.assertTrue(st["saved"][0]["active"])
        self.assertEqual(st["saved"][1]["priority"], 5)
        self.assertEqual(st["ip"], "192.168.1.50")
        self.assertTrue(st["lastSwitch"]["reverted"])

    def test_scan_dedupes_and_skips_hidden(self):
        self.assertEqual([n["ssid"] for n in self.wifi.scan()], ["Mang_chinh", "Cafe"])

    def test_add_new_network_uses_wpa_psk_without_switching(self):
        self.wifi.add("Phong lab", "matkhau123", 20)
        cmd = self.run.writes()[-1]
        self.assertEqual(cmd[:6], ["sudo", "-n", "nmcli", "connection", "add", "type"])
        self.assertIn("matkhau123", cmd)
        self.assertEqual(cmd[cmd.index("connection.autoconnect-priority") + 1], "20")
        self.assertFalse(any("up" in c for c in self.run.writes()))

    def test_add_existing_network_modifies_it(self):
        self.wifi.add("Mang_chinh", None, 90)
        cmd = self.run.writes()[-1]
        self.assertEqual(cmd[3:6], ["connection", "modify", "Mang_chinh"])
        self.assertNotIn("wifi-sec.psk", cmd)

    def test_bad_input_is_rejected_before_nmcli(self):
        for args in (("", None, 10), ("x" * 33, None, 10), ("ok", "short", 10), ("ok", None, 5000),
                     ("bad\nname", None, 10)):
            with self.assertRaises(ValueError, msg=args):
                self.wifi.add(*args)
        self.assertEqual(self.run.writes(), [])

    def test_cannot_delete_active_or_unknown(self):
        with self.assertRaises(ValueError):
            self.wifi.delete("Mang_chinh")
        with self.assertRaises(KeyError):
            self.wifi.delete("Khong_co")
        self.wifi.delete("Nha:Tro")
        self.assertEqual(self.run.writes()[-1][3:], ["connection", "delete", "Nha:Tro"])

    def test_switch_runs_detached_with_previous_network(self):
        r = self.wifi.switch("Nha:Tro")
        self.assertEqual(r, {"switching": True, "from": "Mang_chinh", "to": "Nha:Tro"})
        cmd = self.run.writes()[-1]
        self.assertEqual(cmd[2], "systemd-run")
        self.assertEqual(cmd[-3:], ["Nha:Tro", "Mang_chinh", self.wifi.result_file])
        with self.assertRaises(ValueError):
            self.wifi.switch("Mang_chinh")

    def test_switch_refused_when_no_active_network(self):
        wifi = WifiManager(run=FakeRun("A:802-11-wireless:yes:0:no\n"))
        with self.assertRaises(ValueError):
            wifi.switch("A")

    def test_nmcli_failure_becomes_runtime_error(self):
        wifi = WifiManager(run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 10, "", "Error: not authorized"))
        with self.assertRaisesRegex(RuntimeError, "not authorized"):
            wifi.saved()


class SwitchScriptTest(unittest.TestCase):
    def _run(self, up_rc, online, back_rc=0):
        calls = []

        def run(cmd, timeout=60):
            calls.append(cmd)
            rc = up_rc if len(calls) == 1 else back_rc
            return subprocess.CompletedProcess(cmd, rc, "", "" if rc == 0 else "Error: secrets required")
        with mock.patch.object(wifi_switch, "run", run), mock.patch.object(wifi_switch, "online", lambda: online):
            return wifi_switch.switch("Moi", "Cu", wait=5, sleep=lambda s: None), calls

    def test_success_stays_on_new_network(self):
        result, calls = self._run(0, True)
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)

    def test_failed_connect_reverts(self):
        result, calls = self._run(4, True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["reverted"])
        self.assertEqual(calls[-1][-1], "Cu")
        self.assertIn("secrets", result["error"])

    def test_connected_but_no_gateway_reverts(self):
        result, _ = self._run(0, False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["reverted"])

    def test_revert_failure_is_reported(self):
        result, _ = self._run(4, False, back_rc=4)
        self.assertFalse(result["reverted"])
        self.assertIn("quay về Cu", result["error"])


if __name__ == "__main__":
    unittest.main()
