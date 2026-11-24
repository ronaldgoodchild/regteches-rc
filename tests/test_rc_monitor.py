"""Tests for rc_monitor.py (live server stats): parsing of the script output, the CPU / network maths, and the tab.
No server is contacted. Run from the RC folder:  python -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import tkinter as tk
import unittest
from tkinter import ttk

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_mon_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as core  # noqa: E402
import rc_monitor as mon  # noqa: E402


def output(cpu="cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 500 0 250 4000 250 0 0 0 0 0\ncpu1 500 0 250 4000 250 0 0 0 0 0",
           rx=1000, tx=2000, ps_extra=""):
    return f"""@@os
Linux 6.1.0
mybox
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
2
@@up
93784.12 180000.00
0.52 0.40 0.31 1/300 1234
@@stat
{cpu}
@@mem
MemTotal:       16000000 kB
MemFree:         2000000 kB
MemAvailable:    6000000 kB
Buffers:          100000 kB
Cached:          3000000 kB
SwapTotal:       2000000 kB
SwapFree:        1500000 kB
@@disk
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sda1        100000000  50000000  50000000      50% /
tmpfs              8000000         0   8000000       0% /dev/shm
/dev/sdb1         20000000  19000000   1000000      95% /mnt/big disk
/dev/loop3            1000      1000         0     100% /snap/core/1
@@net
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:  500  5 0 0 0 0 0 0  500 5 0 0 0 0 0 0
  eth0: {rx} 10 0 0 0 0 0 0 {tx} 12 0 0 0 0 0 0
@@ps
  PID USER     %CPU %MEM   RSS     ELAPSED COMMAND
  101 root     85.5  1.0 10240  1-02:03:04 /usr/bin/python3 /opt/app.py --flag
    7 www-data  2.0  0.5  2048       05:10 nginx: worker process
{ps_extra}@@ports
Netid State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process
tcp   LISTEN 0      128          0.0.0.0:22        0.0.0.0:*     users:(("sshd",pid=1,fd=3))
tcp   LISTEN 0      128             [::]:22           [::]:*     users:(("sshd",pid=1,fd=4))
udp   UNCONN 0      0            0.0.0.0:68        0.0.0.0:*
@@end
"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.s = mon.parse_sample(output(), now=100.0)

    def test_os_and_uptime(self):
        self.assertEqual(self.s["os"], {"kernel": "Linux 6.1.0", "hostname": "mybox",
                                        "pretty": "Debian GNU/Linux 12 (bookworm)", "cores": 2})
        self.assertAlmostEqual(self.s["uptime"], 93784.12)
        self.assertEqual(self.s["load"], (0.52, 0.40, 0.31))
        self.assertEqual(mon.human_duration(93784), "1d 2h 3m")

    def test_memory_uses_available_not_free(self):
        m = self.s["mem"]
        self.assertEqual(m["total"], 16000000 * 1024)
        self.assertEqual(m["used"], 10000000 * 1024)          # total - MemAvailable
        self.assertEqual(m["swap_used"], 500000 * 1024)

    def test_memory_without_memavailable_falls_back(self):
        m = mon.parse_mem(["MemTotal: 1000 kB", "MemFree: 100 kB", "Buffers: 50 kB", "Cached: 150 kB"])
        self.assertEqual(m["used"], 700 * 1024)
        self.assertEqual(m["swap_total"], 0)

    def test_disks_skip_pseudo_filesystems_and_keep_spaces_in_mounts(self):
        mounts = [d["mount"] for d in self.s["disks"]]
        self.assertEqual(mounts, ["/", "/mnt/big disk"])
        self.assertEqual(self.s["disks"][1]["pct"], 95)

    def test_network_skips_loopback(self):
        self.assertEqual(self.s["net"], {"eth0": (1000, 2000)})

    def test_processes(self):
        p = self.s["procs"]
        self.assertEqual([x["pid"] for x in p], [101, 7])
        self.assertEqual(p[0]["cmd"], "/usr/bin/python3 /opt/app.py --flag")
        self.assertEqual(p[0]["rss"], 10240 * 1024)
        self.assertEqual(mon.etime_seconds("1-02:03:04"), 93784)
        self.assertEqual(mon.etime_seconds("05:10"), 310)
        self.assertEqual(mon.etime_seconds("junk"), 0)

    def test_ports_from_ss(self):
        ports = self.s["ports"]
        self.assertEqual([(p["proto"], p["addr"], p["port"], p["proc"]) for p in ports],
                         [("tcp", "0.0.0.0", 22, "sshd"), ("tcp", "::", 22, "sshd"), ("udp", "0.0.0.0", 68, "")])

    def test_ports_from_netstat(self):
        lines = ["Active Internet connections (only servers)",
                 "Proto Recv-Q Send-Q Local Address           Foreign Address         State",
                 "tcp        0      0 0.0.0.0:80              0.0.0.0:*               LISTEN",
                 "tcp6       0      0 :::443                  :::*                    LISTEN",
                 "udp        0      0 0.0.0.0:5353            0.0.0.0:*"]
        self.assertEqual([(p["proto"], p["port"]) for p in mon.parse_ports(lines)], [("tcp", 80), ("tcp6", 443), ("udp", 5353)])

    def test_not_a_linux_server(self):
        with self.assertRaises(mon.Unsupported):
            mon.parse_sample("'sh' is not recognized as an internal or external command,\r\noperable program or batch file.")
        with self.assertRaises(mon.Unsupported):
            mon.parse_sample("@@os\nFreeBSD\n@@stat\n@@mem\n@@end\n")

    def test_garbage_lines_do_not_crash(self):
        s = mon.parse_sample(output(ps_extra="garbage line\n  x y\n"))
        self.assertEqual(len(s["procs"]), 2)


class MathTests(unittest.TestCase):
    def test_cpu_percent(self):
        a = mon.parse_cpu(["cpu  1000 0 500 8000 500 0 0 0", "cpu0 0 0 0 0 0 0 0 0"])
        b = mon.parse_cpu(["cpu  1500 0 750 8500 500 0 0 0", "cpu0 0 0 0 0 0 0 0 0"])
        pct = mon.cpu_percent(a, b)
        self.assertAlmostEqual(pct["cpu"], 60.0)      # busy +750 of +1250 jiffies
        self.assertEqual(pct["cpu0"], 0.0)            # no time passed: no division by zero
        self.assertEqual(mon.cpu_percent({}, b), {})

    def test_network_rates_and_counter_reset(self):
        r = mon.net_rates({"eth0": (1000, 2000)}, {"eth0": (3000, 2500), "wlan0": (5, 5)}, 2.0)
        self.assertEqual(r, {"eth0": (1000.0, 250.0)})            # new interface has no earlier sample
        self.assertEqual(mon.net_rates({"eth0": (9000, 9000)}, {"eth0": (10, 10)}, 1.0), {"eth0": (0.0, 0.0)})
        self.assertEqual(mon.net_rates({"eth0": (1, 1)}, {"eth0": (2, 2)}, 0), {})

    def test_tracker_builds_history(self):
        tr = mon.Tracker()
        v1 = tr.update(mon.parse_sample(output(), now=100.0))
        self.assertEqual(v1["cpu"], {})                           # first sample: nothing to compare with
        v2 = tr.update(mon.parse_sample(output(cpu="cpu 1500 0 750 8500 500 0 0 0\ncpu0 0 0 0 0 0 0 0 0",
                                              rx=3000, tx=2500), now=102.0))
        self.assertAlmostEqual(v2["cpu"]["cpu"], 60.0)
        self.assertEqual((v2["rx"], v2["tx"]), (1000.0, 250.0))
        self.assertEqual(list(tr.cpu_hist), [v2["cpu"]["cpu"]])

    def test_retry_delay_backs_off_and_is_capped(self):
        self.assertEqual(mon.retry_delay(5, 0), 5.0)                      # all is well: the normal interval
        self.assertEqual([mon.retry_delay(5, n) for n in (1, 2, 3, 4)], [10.0, 20.0, 40.0, 60.0])
        self.assertEqual(mon.retry_delay(5, 99), 60.0)                    # never more than a minute
        self.assertEqual(mon.retry_delay(60, 1), 60.0)
        self.assertEqual(mon.retry_delay(2, 1), 4.0)
        self.assertEqual(mon.retry_delay(0, 1), 2.0)                      # a zero interval cannot make it spin

    def test_human_formatting(self):
        self.assertEqual(mon.human_bytes(512), "512 B")
        self.assertEqual(mon.human_bytes(1536), "1.5 KB")
        self.assertEqual(mon.human_bytes(5 * 1024 ** 3), "5.0 GB")
        self.assertEqual(mon.human_rate(2048), "2.0 KB/s")

    def test_script_is_read_only_and_quote_safe(self):
        self.assertNotIn("'", mon.SCRIPT)                          # it is wrapped in sh -c '...'
        for bad in ("rm ", "kill", " > ", "chmod", "mv ", "sudo"):
            self.assertNotIn(bad, mon.SCRIPT)
        self.assertTrue(mon.COMMAND.startswith("sh -c '") and mon.COMMAND.endswith("'"))


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

    @classmethod
    def tearDownClass(cls):
        mon.MonitorTab.AUTO_START = True
        cls.root.destroy()

    def setUp(self):
        self.app = FakeApp(self.root)
        self.tab = mon.MonitorTab(self.app, {"name": "box", "address": "10.0.0.5"}, "u", "p")
        self.app.nb.add(self.tab, text="Stats")
        self.root.update()

    def tearDown(self):
        self.tab.close()
        self.app.nb.destroy()

    def feed(self):
        self.tab.apply(mon.parse_sample(output(), now=100.0))
        self.tab.apply(mon.parse_sample(output(cpu="cpu 1500 0 750 8500 500 0 0 0\ncpu0 0 0 0 0 0 0 0 0", rx=3000, tx=2500), now=102.0))
        self.root.update()

    def test_tiles_show_values(self):
        self.feed()
        self.assertEqual(self.tab.cpu_big.get(), "60%")
        self.assertIn("2 cores", self.tab.cpu_sub.get())
        self.assertEqual(self.tab.mem_big.get(), "62%")
        self.assertIn("swap", self.tab.mem_sub.get())
        self.assertIn("1000 B/s", self.tab.net_big.get())
        self.assertEqual(self.tab.sys_big.get(), "mybox")
        self.assertIn("Up 1d 2h", self.tab.sys_sub.get())

    def test_tables(self):
        self.feed()
        self.assertEqual(list(self.tab.procs.get_children()), ["101", "7"])        # busiest first
        self.assertEqual(self.tab.procs.item("101", "tags"), ("crit",))
        self.assertEqual(list(self.tab.disks.get_children()), ["/", "/mnt/big disk"])
        self.assertEqual(self.tab.disks.item("/mnt/big disk", "tags"), ("crit",))   # 95% full
        self.assertEqual(self.tab.nets.item("eth0", "values")[1], "1000 B/s")
        self.assertEqual(len(self.tab.ports.get_children()), 3)

    def test_sort_and_filter(self):
        self.feed()
        self.tab.sort_procs("user")
        self.assertEqual(list(self.tab.procs.get_children()), ["101", "7"])         # root before www-data
        self.tab.sort_procs("user")
        self.assertEqual(list(self.tab.procs.get_children()), ["7", "101"])
        self.tab.proc_filter.set("nginx")
        self.assertEqual(list(self.tab.procs.get_children()), ["7"])

    def test_refresh_keeps_selection(self):
        self.feed()
        self.tab.procs.selection_set("7")
        self.feed()
        self.assertEqual(self.tab.procs.selection(), ("7",))

    def test_processes_that_vanish_are_removed(self):
        self.feed()
        self.tab.apply(mon.parse_sample(output().replace("    7 www-data  2.0  0.5  2048       05:10 nginx: worker process\n", ""), now=104.0))
        self.assertEqual(list(self.tab.procs.get_children()), ["101"])

    def test_interval_and_pause_copies_for_the_worker(self):
        self.tab.interval.set(30)
        self.assertEqual(self.tab.interval_s, 30)
        self.tab.paused.set(True)
        self.tab.sync_settings()
        self.assertTrue(self.tab.paused_flag)

    def test_unsupported_server_message(self):
        self.tab.fail("not a Linux machine", fatal=True)
        self.assertEqual(self.tab.cpu_big.get(), "n/a")

    def test_close_stops_the_worker(self):
        self.tab.close()
        self.assertTrue(self.tab.closed and self.tab.stop_evt.is_set())


class WorkerTests(unittest.TestCase):
    """The background thread end to end, with the SSH calls replaced (results come back through app.post)."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def run_tab(self, replies, seconds=3.0):
        import queue
        import time
        import rc_tools
        q = queue.Queue()

        class App(FakeApp):
            def post(self, fn):
                q.put(fn)

        app = App(self.root)
        calls = {"open": 0, "closed": 0}

        class Client:
            def close(self):
                calls["closed"] += 1

        def fake_open(app_, host, user, pw):
            calls["open"] += 1
            return Client()

        replies = list(replies)

        def fake_run(client, command, timeout=60):
            r = replies.pop(0) if len(replies) > 1 else replies[0]
            if isinstance(r, Exception):
                raise r
            return 0, r, ""

        old = (core.open_ssh_client, rc_tools.run_remote, mon.MonitorTab.AUTO_START)
        core.open_ssh_client, rc_tools.run_remote, mon.MonitorTab.AUTO_START = fake_open, fake_run, True
        try:
            mon.MonitorTab.DEFAULT_INTERVAL = 1
            tab = mon.MonitorTab(app, {"name": "box", "address": "10.0.0.5"}, "u", "p")
            app.nb.add(tab, text="Stats")
            end = time.time() + seconds
            while time.time() < end:
                try:
                    q.get(timeout=0.1)()
                except queue.Empty:
                    pass
                self.root.update()
            tab.close()
            tab.thread.join(5)
            return tab, calls
        finally:
            core.open_ssh_client, rc_tools.run_remote, mon.MonitorTab.AUTO_START = old
            mon.MonitorTab.DEFAULT_INTERVAL = 5

    def test_polls_and_reuses_one_connection(self):
        faster = output(cpu="cpu 1500 0 750 8500 500 0 0 0\ncpu0 0 0 0 0 0 0 0 0", rx=3000, tx=2500)
        tab, calls = self.run_tab([output(), faster])
        self.assertEqual(calls["open"], 1)
        self.assertNotEqual(tab.cpu_big.get(), "-")
        self.assertEqual(calls["closed"], 1)                      # closed when the tab closed
        self.assertFalse(tab.thread.is_alive())

    def test_reconnects_after_a_dropped_connection(self):
        tab, calls = self.run_tab([OSError("connection reset"), output()])
        self.assertGreaterEqual(calls["open"], 2)                 # the first client was dropped and a new one opened
        self.assertEqual(tab.sys_big.get(), "mybox")

    def test_windows_server_stops_politely(self):
        tab, calls = self.run_tab(["'sh' is not recognized as an internal or external command"], seconds=1.5)
        self.assertEqual(tab.cpu_big.get(), "n/a")
        self.assertIn("Windows", tab.status_var.get())
        self.assertFalse(tab.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
