"""Tests for the VNC and RustDesk connections (command lines, viewer discovery, saved fields)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_test_remote_"))
import rc_manager as rc  # noqa: E402


class HostFieldTests(unittest.TestCase):
    def test_defaults(self):
        h = rc.sanitize_host({"address": "pc.lan"})
        self.assertEqual((h["vnc_port"], h["rustdesk_id"], h["rustdesk_password_enc"]), (5900, "", ""))

    def test_bad_vnc_port_repaired(self):
        problems = []
        h = rc.sanitize_host({"address": "pc.lan", "vnc_port": "abc"}, problems)
        self.assertEqual(h["vnc_port"], 5900)
        self.assertTrue(any("vnc_port" in p for p in problems))

    def test_rustdesk_id_must_be_text(self):
        self.assertEqual(rc.sanitize_host({"address": "pc.lan", "rustdesk_id": 123456789})["rustdesk_id"], "123456789")


class CommandTests(unittest.TestCase):
    def test_rustdesk_target_prefers_id_then_address(self):
        self.assertEqual(rc.rustdesk_target({"address": "10.1.1.5", "rustdesk_id": "123 456 789"}), "123456789")
        self.assertEqual(rc.rustdesk_target({"address": "10.1.1.5", "rustdesk_id": ""}), "10.1.1.5")

    def test_rustdesk_command(self):
        self.assertEqual(rc.rustdesk_command("r.exe", "123"), ["r.exe", "--connect", "123"])
        self.assertEqual(rc.rustdesk_command("r.exe", "123", "pw"), ["r.exe", "--connect", "123", "--password", "pw"])

    def test_vnc_command_has_no_password(self):
        self.assertEqual(rc.vnc_command("v.exe", "pc.lan", 5901), ["v.exe", "pc.lan::5901"])


class ConnectionListTests(unittest.TestCase):
    def test_builtin_entries(self):
        h = rc.sanitize_host({"address": "pc.lan", "vnc_port": 5901})
        by = {c["action"]: c for c in rc.connection_list(h)}
        self.assertEqual(by["vnc"]["detail"], "port 5901")
        self.assertIn("direct IP", by["rustdesk"]["detail"])

    def test_extra_connections(self):
        h = rc.sanitize_host({"address": "pc.lan", "connections": [
            {"type": "vnc", "port": 5902}, {"type": "rustdesk", "url": "987654321"}]})
        extras = [c for c in rc.connection_list(h) if c.get("extra")]
        self.assertEqual([c["action"] for c in extras], ["vnc", "rustdesk"])
        self.assertEqual(extras[0]["over"], {"vnc_port": 5902})
        self.assertEqual(extras[1]["over"], {"rustdesk_id": "987654321"})
        self.assertEqual(rc.variant(h, extras[1]["over"])["rustdesk_id"], "987654321")

    def test_buttons_exist(self):
        for key in ("vnc", "rustdesk"):
            self.assertIn(key, rc.ACTION_LABEL)
            self.assertIn(key, rc.SHORT_LABEL)


class DiscoveryTests(unittest.TestCase):
    def test_setting_overrides_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "viewer.exe")
            open(exe, "w").close()
            sf = os.path.join(d, "settings.json")
            with open(sf, "w") as f:
                json.dump({"vnc_viewer": exe, "rustdesk_path": exe}, f)
            with mock.patch.object(rc, "SETTINGS_FILE", sf):
                self.assertEqual(rc.find_vnc_viewer(), exe)
                self.assertEqual(rc.find_rustdesk(), exe)
            with mock.patch.object(rc, "SETTINGS_FILE", os.path.join(d, "none.json")), \
                    mock.patch.dict(os.environ, {"ProgramFiles": d, "ProgramFiles(x86)": d, "LOCALAPPDATA": d}), \
                    mock.patch.object(rc.shutil, "which", return_value=None):
                self.assertIsNone(rc.find_vnc_viewer())
                self.assertIsNone(rc.find_rustdesk())

    def test_unreadable_settings(self):
        with tempfile.TemporaryDirectory() as d:
            sf = os.path.join(d, "settings.json")
            with open(sf, "w") as f:
                f.write("{not json")
            with mock.patch.object(rc, "SETTINGS_FILE", sf):
                self.assertIsNone(rc.read_setting("vnc_viewer"))


class ActionTests(unittest.TestCase):
    """Run act_vnc / act_rustdesk on a stand-in App with the process launch and dialogs replaced."""

    def setUp(self):
        self.app = mock.Mock()
        self.h = rc.sanitize_host({"address": "pc.lan", "rustdesk_id": "123456789"})

    def test_rustdesk_launches_with_saved_password(self):
        self.h["rustdesk_password_enc"] = rc.protect_password("s3cret")
        with mock.patch.object(rc, "find_rustdesk", return_value="r.exe"), \
                mock.patch.object(rc.subprocess, "Popen") as popen:
            rc.App.act_rustdesk(self.app, self.h)
        popen.assert_called_once_with(["r.exe", "--connect", "123456789", "--password", "s3cret"])

    def test_rustdesk_missing_shows_help(self):
        with mock.patch.object(rc, "find_rustdesk", return_value=None), \
                mock.patch.object(rc.messagebox, "showinfo") as info, mock.patch.object(rc.subprocess, "Popen") as popen:
            rc.App.act_rustdesk(self.app, self.h)
        info.assert_called_once()
        popen.assert_not_called()

    def test_vnc_launch_and_missing(self):
        with mock.patch.object(rc, "find_vnc_viewer", return_value="v.exe"), \
                mock.patch.object(rc.subprocess, "Popen") as popen:
            rc.App.act_vnc(self.app, self.h)
        popen.assert_called_once_with(["v.exe", "pc.lan::5900"])
        with mock.patch.object(rc, "find_vnc_viewer", return_value=None), \
                mock.patch.object(rc.messagebox, "showinfo") as info:
            rc.App.act_vnc(self.app, self.h)
        info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
