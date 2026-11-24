"""Tests for the on-demand parts of the Stats tab: process detail + signals, services, system info.
No server is contacted (run_async is replaced). Run from the RC folder:  python -m unittest discover -s tests -v"""

import os
import re
import sys
import tempfile
import tkinter as tk
import unittest
from tkinter import ttk

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_stats2_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_monitor as mon  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_rc_monitor import output  # noqa: E402

DETAIL = """@@cmd
/usr/bin/python3 /opt/app.py --flag
@@cwd
/opt
@@exe
/usr/bin/python3.11
@@status
Name:\tpython3
State:\tS (sleeping)
PPid:\t1
Threads:\t4
VmPeak:\t  200000 kB
VmSize:\t  190000 kB
VmRSS:\t   51200 kB
VmData:\t   40000 kB
VmStk:\t     132 kB
voluntary_ctxt_switches:\t100
nonvoluntary_ctxt_switches:\t7
@@fds
12
@@io
rchar: 2048
wchar: 1024
read_bytes: 4096
write_bytes: 0
@@end
"""

SERVICES = """@@uid
0
@@has
/usr/bin/systemctl
@@units
ssh.service                loaded    active   running OpenBSD Secure Shell server
cron.service               loaded    active   running Regular background program processing daemon
● broken.service           loaded    failed   failed  A broken thing
ghost.service              not-found inactive dead    ghost.service
@@files
ssh.service        enabled         enabled
cron.service       enabled         enabled
broken.service     disabled        enabled
@@end
"""

INFO = """@@os
Linux 6.1.0 x86_64
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
mybox
ron
@@cpu
model name\t: Intel(R) Core(TM) i5
cpu MHz\t\t: 2400.000
4
@@time
1000100
EST
America/New_York
@@ip
2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 scope global eth0
@@ifaces
eth0|up|aa:bb:cc:dd:ee:ff|1000
wlan0|down|11:22:33:44:55:66|-1
lo|unknown|00:00:00:00:00:00|
@@blk
NAME="sda" TYPE="disk" SIZE="500G" MOUNTPOINT="" FSTYPE="" MODEL="Samsung SSD" ROTA="0"
NAME="sda1" TYPE="part" SIZE="499G" MOUNTPOINT="/" FSTYPE="ext4" MODEL="" ROTA="0"
@@end
"""


class ParseTests(unittest.TestCase):
    def test_process_detail(self):
        d = mon.parse_proc_detail(DETAIL)
        self.assertEqual(d["cmdline"], "/usr/bin/python3 /opt/app.py --flag")
        self.assertEqual((d["cwd"], d["exe"], d["fds"]), ("/opt", "/usr/bin/python3.11", 12))
        self.assertEqual(d["io"]["read_bytes"], 4096)
        text = mon.format_proc_detail(42, d)
        self.assertIn("PID 42", text)
        self.assertIn("threads: 4", text)
        self.assertIn("in RAM 50.0 MB", text)
        self.assertIn("100 voluntary, 7 involuntary", text)

    def test_vanished_process(self):
        with self.assertRaises(LookupError):
            mon.parse_proc_detail("@@cmd\n\n@@cwd\n\n@@exe\n\n@@status\n@@fds\n0\n@@io\n@@end\n")

    def test_detail_command_only_takes_a_number(self):
        self.assertIn("/proc/42;", mon.detail_command("42"))
        with self.assertRaises(ValueError):
            mon.detail_command("42; rm -rf /")

    def test_kill_command(self):
        self.assertEqual(mon.kill_command("TERM", 42), "kill -TERM 42 2>&1")
        self.assertEqual(mon.kill_command("KILL", "7", sudo=True), "sudo -n kill -KILL 7 2>&1")
        for bad in (("STOP; id", 1), ("9", 1)):
            with self.assertRaises(ValueError):
                mon.kill_command(*bad)
        with self.assertRaises(ValueError):
            mon.kill_command("TERM", "1 2")

    def test_services(self):
        root, svcs = mon.parse_services(SERVICES)
        self.assertTrue(root)
        self.assertEqual([s["unit"] for s in svcs], ["broken.service", "cron.service", "ssh.service"])   # not-found one dropped
        broken = svcs[0]
        self.assertEqual((broken["active"], broken["startup"], broken["desc"]), ("failed", "disabled", "A broken thing"))
        self.assertEqual(svcs[2]["startup"], "enabled")

    def test_no_systemd(self):
        with self.assertRaises(mon.Unsupported):
            mon.parse_services("@@uid\n1000\n@@has\n@@units\n@@files\n@@end\n")

    def test_service_and_journal_commands_reject_odd_names(self):
        self.assertEqual(mon.service_command("restart", "ssh.service", sudo=True), "sudo -n systemctl restart ssh.service 2>&1")
        self.assertEqual(mon.service_command("stop", "getty@tty1.service"), "systemctl stop getty@tty1.service 2>&1")
        for bad in ("ssh; reboot", "a b", "$(id)", "", "x|y", "a`b`"):
            with self.assertRaises(ValueError):
                mon.service_command("start", bad)
            with self.assertRaises(ValueError):
                mon.journal_command(bad)
        with self.assertRaises(ValueError):
            mon.service_command("delete", "ssh.service")
        self.assertIn("-u cron.service -n 50", mon.journal_command("cron.service", 50))

    def test_scripts_are_quote_safe_and_read_only(self):
        for cmd in (mon.SERVICES_COMMAND, mon.INFO_COMMAND, mon.detail_command(1)):
            self.assertTrue(cmd.startswith("sh -c '") and cmd.endswith("'"))
            self.assertNotIn("'", cmd[7:-1])
            for bad in (r"rm\s", r"kill", "systemctl (start|stop|restart)", r"sudo", r"mv", r"[^2]>[^&/]"):
                self.assertIsNone(re.search(bad, cmd), bad)

    def test_system_info(self):
        i = mon.parse_info(INFO, 1000090.0, 1000092.0)       # server clock 1000100 vs PC midpoint 1000091
        self.assertEqual((i["hostname"], i["user"], i["pretty"]), ("mybox", "ron", "Debian GNU/Linux 12 (bookworm)"))
        self.assertEqual(i["cpu"], {"model": "Intel(R) Core(TM) i5", "mhz": "2400.000", "cores": "4"})
        self.assertAlmostEqual(i["skew"], 9.0)
        self.assertEqual([n["name"] for n in i["ifaces"]], ["eth0", "wlan0"])         # loopback left out
        self.assertEqual((i["ifaces"][0]["speed"], i["ifaces"][0]["addrs"]), ("1000", ["192.168.1.5/24"]))
        self.assertEqual(i["ifaces"][1]["speed"], "")                                  # -1 means unknown
        self.assertEqual(i["blk"][0]["MODEL"], "Samsung SSD")
        text = mon.format_info(i)
        self.assertIn("9.0 s ahead of this PC", text)
        self.assertNotIn("check NTP", text)
        self.assertIn("1000 Mbps", text)
        self.assertIn("disk SSD", text)

    def test_big_clock_skew_is_flagged(self):
        i = mon.parse_info(INFO, 1000000.0, 1000000.0)
        self.assertIn("check NTP", mon.format_info(i))
        self.assertIsNone(mon.parse_info(INFO)["skew"])
        self.assertIn("unknown", mon.format_info(mon.parse_info(INFO)))


class FakeApp:
    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.status = tk.StringVar()

    def post(self, fn):
        fn()


class TabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1100x700")
        mon.MonitorTab.AUTO_START = False
        cls.mb = mon.messagebox

    @classmethod
    def tearDownClass(cls):
        mon.MonitorTab.AUTO_START = True
        cls.root.destroy()

    def setUp(self):
        self.app = FakeApp(self.root)
        self.tab = mon.MonitorTab(self.app, {"name": "box", "address": "10.0.0.5"}, "u", "p")
        self.app.nb.add(self.tab, text="Stats")
        self.root.update()
        self.sent = []
        self.replies = {}

        def fake_run(command, done, timeout=30):
            self.sent.append(command)
            for key, reply in self.replies.items():
                if key in command:
                    return done(*reply)
            done(0, "", "", None)
        self.tab.run_async = fake_run
        self.asked = []
        self._old = (mon.messagebox.askyesno, mon.messagebox.showwarning, mon.messagebox.showinfo)
        mon.messagebox.askyesno = lambda *a, **k: (self.asked.append(a[1]), self.answer)[1]
        mon.messagebox.showwarning = lambda *a, **k: self.asked.append("WARN " + a[1])
        mon.messagebox.showinfo = lambda *a, **k: self.asked.append("INFO " + a[1])
        self.answer = True

    def tearDown(self):
        mon.messagebox.askyesno, mon.messagebox.showwarning, mon.messagebox.showinfo = self._old
        self.tab.close()
        self.app.nb.destroy()

    def select_tab(self, label):
        for i, t in enumerate(self.tab.book.tabs()):
            if self.tab.book.tab(t, "text").strip() == label:
                self.tab.book.select(i)
                self.root.update()
                return
        self.fail("no such tab " + label)

    def test_services_load_when_tab_is_opened_and_filter(self):
        self.replies["systemctl list-units"] = (0, SERVICES, "", None)
        self.assertEqual(self.sent, [])
        self.select_tab("Services")
        self.assertEqual(list(self.tab.svc_tree.get_children()), ["broken.service", "cron.service", "ssh.service"])
        self.assertEqual(self.tab.svc_tree.item("broken.service", "tags"), ("crit",))
        self.assertIn("1 failed", self.tab.svc_info.get())
        self.tab.svc_state.set("Failed")
        self.tab.show_services()
        self.assertEqual(list(self.tab.svc_tree.get_children()), ["broken.service"])
        self.tab.svc_state.set("All")
        self.tab.svc_filter.set("cron")
        self.assertEqual(list(self.tab.svc_tree.get_children()), ["cron.service"])

    def test_service_action_asks_first_and_uses_sudo_when_not_root(self):
        self.replies["systemctl list-units"] = (0, SERVICES.replace("@@uid\n0", "@@uid\n1000"), "", None)
        self.select_tab("Services")
        self.tab.svc_tree.selection_set("cron.service")
        self.answer = False
        self.tab.service_action("restart")
        self.assertFalse([c for c in self.sent if "restart" in c])                 # declined: nothing was run
        self.answer = True
        self.tab.service_action("restart")
        self.assertIn("sudo -n systemctl restart cron.service 2>&1", self.sent)

    def test_failed_action_explains_sudo(self):
        self.replies["systemctl list-units"] = (0, SERVICES.replace("@@uid\n0", "@@uid\n1000"), "", None)
        self.select_tab("Services")
        self.tab.svc_tree.selection_set("cron.service")
        self.replies["systemctl stop"] = (1, "sudo: a password is required", "", None)
        self.tab.service_action("stop")
        self.assertTrue(any(m.startswith("WARN") and "passwordless sudo" in m for m in self.asked))

    def test_critical_service_gets_extra_warning(self):
        self.replies["systemctl list-units"] = (0, SERVICES, "", None)
        self.select_tab("Services")
        self.tab.svc_tree.selection_set("ssh.service")
        self.tab.service_action("stop")
        self.assertTrue(any("cut off" in m for m in self.asked))

    def test_no_selection_just_explains(self):
        self.tab.service_action("start")
        self.assertTrue(any(m.startswith("INFO") for m in self.asked))

    def test_system_tab_fills_text(self):
        self.replies["lsblk"] = (0, INFO, "", None)
        self.select_tab("System")
        body = self.tab.info_text.get("1.0", "end")
        self.assertIn("mybox", body)
        self.assertIn("Samsung SSD", body)
        self.assertIn("Read at", self.tab.info_status.get())

    def test_system_tab_reports_errors(self):
        self.replies["lsblk"] = (None, "", "", RuntimeError("Not connected right now"))
        self.select_tab("System")
        self.assertIn("Not connected", self.tab.info_status.get())

    def test_process_dialog_details_and_signal(self):
        self.replies["/proc/"] = (0, DETAIL, "", None)
        self.tab.apply(mon.parse_sample(output(), now=100.0))
        self.tab.procs.selection_set("101")
        self.tab.open_process()
        dlg = next(w for w in self.tab.winfo_children() if isinstance(w, mon.ProcessDialog))
        self.root.update()
        self.assertIn("/usr/bin/python3 /opt/app.py", dlg.text.get("1.0", "end"))
        dlg.sig.set([l for n, l in mon.SIGNALS if n == "KILL"][0])
        self.answer = False
        dlg.send()
        self.assertFalse([c for c in self.sent if c.startswith("kill") or c.startswith("sudo")])
        self.answer = True
        dlg.send()
        self.assertIn("kill -KILL 101 2>&1", self.sent)
        self.assertTrue(any("immediately" in m for m in self.asked))              # KILL carries the extra warning
        dlg.destroy()

    def test_process_signal_retries_with_sudo_once(self):
        self.tab.is_root = False
        self.replies["/proc/"] = (0, DETAIL, "", None)
        self.replies["kill -TERM"] = (1, "kill: (101): Operation not permitted", "", None)
        self.tab.apply(mon.parse_sample(output(), now=100.0))
        self.tab.procs.selection_set("101")
        dlg = mon.ProcessDialog(self.tab, 101)
        dlg.send()
        self.assertIn("sudo -n kill -TERM 101 2>&1", self.sent)
        self.assertEqual(len([c for c in self.sent if "kill -TERM" in c and "sudo" not in c]), 1)
        dlg.destroy()

    def test_open_process_without_selection(self):
        self.tab.open_process()
        self.assertTrue(any(m.startswith("INFO") for m in self.asked))


if __name__ == "__main__":
    unittest.main()
