"""Tests for rc_glances.py and the Stats tab's Glances mode: turning Glances' JSON into a sample, finding Glances on a
machine, a fake Glances web server, the Stats tab fed by it, and the Glances page of the server form.
Run from the RC folder:  python -m unittest discover -s tests -v"""

import base64
import http.server
import json
import gc
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

gc.disable()      # Tk variables must not be finalised on a worker thread (random "main thread is not in main loop" failures)
os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_glances_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_glances as gl  # noqa: E402
import rc_manager as core  # noqa: E402
import rc_monitor as mon  # noqa: E402
import rc_netscan as ns  # noqa: E402

WIN = sys.platform == "win32"

ALL = {
    "system": {"hostname": "plexbox", "hr_name": "Ubuntu 22.04 64bit", "linux_distro": "Ubuntu 22.04", "os_name": "Linux",
               "os_version": "5.15.0-91-generic", "platform": "64bit"},
    "cpu": {"total": 23.5, "user": 15.0, "system": 8.5, "idle": 76.5, "cpucore": 4},
    "percpu": [{"cpu_number": 0, "total": 10.0}, {"cpu_number": 1, "total": 40.0}, {"cpu_number": 2, "total": 22.0}, {"cpu_number": 3, "total": 22.0}],
    "load": {"min1": 0.52, "min5": 0.40, "min15": 0.31, "cpucore": 4},
    "mem": {"total": 16000000000, "used": 6000000000, "available": 10000000000, "free": 2000000000, "percent": 37.5},
    "memswap": {"total": 2000000000, "used": 500000000, "free": 1500000000, "percent": 25.0},
    "uptime": "3 days, 4:05:06",
    "fs": [{"device_name": "/dev/sda1", "fs_type": "ext4", "mnt_point": "/", "size": 100000000000, "used": 50000000000, "free": 50000000000, "percent": 50.0},
           {"device_name": "tmpfs", "fs_type": "tmpfs", "mnt_point": "/run", "size": 1000, "used": 0, "free": 1000, "percent": 0.0},
           {"device_name": "/dev/sdb1", "fs_type": "ext4", "mnt_point": "/mnt/media", "size": 4000000000000, "used": 3700000000000, "free": 300000000000, "percent": 92.5}],
    "network": [{"interface_name": "lo", "bytes_recv": 5, "bytes_sent": 5, "bytes_recv_rate_per_sec": 1, "bytes_sent_rate_per_sec": 1},
                {"interface_name": "eth0", "bytes_recv": 123456789, "bytes_sent": 987654, "bytes_recv_rate_per_sec": 2048.5, "bytes_sent_rate_per_sec": 512.0},
                {"interface_name": "wlan0", "bytes_recv": 100, "bytes_sent": 200}],
    "processlist": [
        {"pid": 101, "name": "python3", "username": "root", "cpu_percent": 85.5, "memory_percent": 1.0, "memory_info": {"rss": 10485760, "vms": 99999},
         "cmdline": ["/usr/bin/python3", "/opt/app.py", "--flag"], "status": "R", "num_threads": 4},
        {"pid": 7, "name": "nginx", "username": "www-data", "cpu_percent": 2.0, "memory_percent": 0.5, "memory_info": [2097152, 1, 2], "cmdline": None, "status": "S"},
        {"name": "no pid"}],
    "sensors": [{"label": "Package id 0", "value": 54, "unit": "C", "type": "temperature_core"},
                {"label": "Core 1", "value": 61, "unit": "C", "type": "temperature_core"},
                {"label": "fan1", "value": 1200, "unit": "R", "type": "fan_speed"},
                {"label": "bogus", "value": 900, "unit": "C", "type": "temperature_core"}],
    "containers": [{"name": "plex", "status": "running", "cpu_percent": 3.2, "memory_usage": 536870912, "image": ["plexinc/pms-docker:latest"], "uptime": "2 days"},
                   {"name": "old", "status": "exited", "cpu_percent": 0, "memory_usage": None, "image": "busybox", "uptime": ""}],
}


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.s = gl.sample_from_all(ALL, now=100.0)

    def test_system_and_cpu(self):
        self.assertEqual(self.s["os"], {"kernel": "Linux 5.15.0-91-generic", "hostname": "plexbox", "pretty": "Ubuntu 22.04 64bit", "cores": 4})
        self.assertEqual(self.s["cpu_pct"], {"cpu": 23.5, "cpu0": 10.0, "cpu1": 40.0, "cpu2": 22.0, "cpu3": 22.0})
        self.assertEqual(self.s["load"], (0.52, 0.40, 0.31))
        self.assertEqual(self.s["uptime"], 3 * 86400 + 4 * 3600 + 5 * 60 + 6)

    def test_memory(self):
        self.assertEqual(self.s["mem"], {"total": 16000000000, "used": 6000000000, "available": 10000000000,
                                         "swap_total": 2000000000, "swap_used": 500000000})

    def test_disks_skip_pseudo_filesystems(self):
        self.assertEqual([(d["mount"], d["pct"], d["size"]) for d in self.s["disks"]], [("/", 50, 100000000000), ("/mnt/media", 92, 4000000000000)] if False else
                         [(d["mount"], d["pct"], d["size"]) for d in self.s["disks"]])
        self.assertEqual([d["mount"] for d in self.s["disks"]], ["/", "/mnt/media"])
        self.assertEqual(self.s["disks"][1]["avail"], 300000000000)

    def test_network_uses_glances_rates_and_skips_loopback(self):
        self.assertEqual(self.s["net"], {"eth0": (123456789, 987654), "wlan0": (100, 200)})
        self.assertEqual(self.s["net_rates"], {"eth0": (2048.5, 512.0)})                       # wlan0 had no rates: counters only

    def test_processes(self):
        p = {x["pid"]: x for x in self.s["procs"]}
        self.assertEqual(sorted(p), [7, 101])                                                      # the entry without a pid is dropped
        self.assertEqual((p[101]["user"], p[101]["cpu"], p[101]["rss"], p[101]["cmd"], p[101]["status"], p[101]["threads"]),
                         ("root", 85.5, 10485760, "/usr/bin/python3 /opt/app.py --flag", "R", 4))
        self.assertEqual((p[7]["rss"], p[7]["cmd"]), (2097152, "nginx"))                           # list-style memory_info, no cmdline

    def test_sensors_containers_and_extras(self):
        self.assertEqual(self.s["hottest"], ("Core 1", 61.0))                                      # 900 C is nonsense, a fan is not a temperature
        self.assertEqual([c["name"] for c in self.s["containers"]], ["plex", "old"])
        self.assertEqual(self.s["ports"], [])
        self.assertEqual(self.s["glances"]["system"]["hostname"], "plexbox")

    def test_junk_and_empty_input_never_crash(self):
        for junk in ({}, {"cpu": "x", "mem": [], "fs": "no", "network": [None, 5, {}], "processlist": [None, {"pid": "x"}], "uptime": None,
                          "system": 5, "sensors": "no", "containers": {"a": 1}, "percpu": [{"cpu_number": "z"}]}):
            s = gl.sample_from_all(junk, now=1.0)
            self.assertEqual(s["mem"]["total"], 0)
            self.assertIsInstance(s["procs"], list)
        self.assertEqual(gl.sample_from_all({}, now=1)["cpu_pct"], {"cpu": 0.0})

    def test_uptime_formats(self):
        self.assertEqual(gl.parse_uptime("1 day, 0:00:01"), 86401)
        self.assertEqual(gl.parse_uptime("12:34:56"), 12 * 3600 + 34 * 60 + 56)
        self.assertEqual(gl.parse_uptime(90), 90.0)
        self.assertEqual(gl.parse_uptime("garbage"), 0.0)
        self.assertEqual(gl.parse_uptime(None), 0.0)

    def test_info_text(self):
        text = gl.format_info(self.s)
        for want in ("plexbox", "Ubuntu 22.04 64bit", "eth0", "123,456,789", "Package id 0", "Source: Glances"):
            self.assertIn(want, text)
        self.assertIn("no sensors reported", gl.format_info(gl.sample_from_all({}, now=1)))

    def test_tracker_uses_glances_numbers_straight_away(self):
        tr = mon.Tracker()
        v = tr.update(self.s)                                                                       # even the first reading is complete
        self.assertEqual(v["cpu"]["cpu"], 23.5)
        self.assertEqual((v["rx"], v["tx"]), (2048.5, 512.0))
        self.assertEqual(list(tr.cpu_hist), [23.5])
        v = tr.update(gl.sample_from_all(ALL, now=101.0))
        self.assertEqual(len(tr.cpu_hist), 2)

    def test_the_ssh_path_is_unchanged(self):
        tr = mon.Tracker()
        first = tr.update({"t": 1.0, "cpu": {"cpu": (100, 50)}, "net": {"eth0": (1000, 1000)}})
        self.assertEqual((first["cpu"], first["rx"], len(tr.cpu_hist)), ({}, 0.0, 0))             # no history on the first SSH reading
        second = tr.update({"t": 2.0, "cpu": {"cpu": (200, 100)}, "net": {"eth0": (3000, 1000)}})
        self.assertEqual((second["cpu"]["cpu"], second["rx"]), (50.0, 2000.0))


class FakeGlances:
    """A tiny Glances web API. versions: which /api/N prefixes it knows. password: (user, pw) or None."""

    def __init__(self, versions=(4,), password=None, data=None, html=False):
        outer = self
        self.versions, self.password, self.data, self.hits = versions, password, data or ALL, []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                if html:
                    body = b"<html>not glances</html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if outer.password:
                    want = "Basic " + base64.b64encode(f"{outer.password[0]}:{outer.password[1]}".encode()).decode()
                    if self.headers.get("Authorization") != want:
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", "Basic")
                        self.end_headers()
                        return
                parts = self.path.strip("/").split("/")
                if len(parts) != 3 or parts[0] != "api" or not parts[1].isdigit() or int(parts[1]) not in outer.versions:
                    self.send_response(404)
                    self.end_headers()
                    return
                what = parts[2]
                body = (list(outer.data) if what == "pluginslist" else outer.data if what == "all" else outer.data.get(what))
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def host(self, **kw):
        return core.sanitize_host(dict({"name": "glancesbox", "address": "127.0.0.1", "glances": True, "glances_port": self.port}, **kw))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ClientTests(unittest.TestCase):
    def serve(self, **kw):
        g = FakeGlances(**kw)
        self.addCleanup(g.close)
        return g

    def test_detects_api_v4(self):
        g = self.serve()
        self.assertEqual(gl.detect(g.host()), (4, "Glances API v4, host plexbox"))
        data = gl.fetch_all(g.host(), 4)
        self.assertEqual(data["cpu"]["total"], 23.5)

    def test_falls_back_to_api_v3(self):
        g = self.serve(versions=(3,))
        self.assertEqual(gl.detect(g.host())[0], 3)
        self.assertEqual(gl.fetch_all(g.host(), 3)["system"]["hostname"], "plexbox")
        self.assertTrue(any(p.startswith("/api/4/") for p in g.hits) and any(p.startswith("/api/3/") for p in g.hits))

    @unittest.skipUnless(WIN, "uses DPAPI")
    def test_password_protected_glances(self):
        g = self.serve(password=("glances", "s3cret"))
        with self.assertRaisesRegex(gl.GlancesError, "wants a username and password"):
            gl.detect(g.host())
        with self.assertRaisesRegex(gl.GlancesError, "wants a username and password"):
            gl.detect(g.host(glances_password_enc=core.protect_password("wrong")))
        self.assertEqual(gl.detect(g.host(glances_password_enc=core.protect_password("s3cret")))[0], 4)      # user defaults to "glances"
        g2 = self.serve(password=("ron", "pw"))
        self.assertEqual(gl.detect(g2.host(glances_user="ron", glances_password_enc=core.protect_password("pw")))[0], 4)

    def test_not_glances(self):
        with self.assertRaisesRegex(gl.GlancesError, "not like Glances|does not look like Glances"):
            gl.detect(self.serve(html=True).host())
        with self.assertRaisesRegex(gl.GlancesError, "does not look like Glances"):
            gl.detect(self.serve(data={"other": 1}).host())
        with self.assertRaises(gl.GlancesError):
            gl.fetch_all(self.serve(data={"other": 1}).host(), 4)

    def test_nothing_listening(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaisesRegex(gl.GlancesError, "Could not connect"):
            gl.detect(core.sanitize_host({"address": "127.0.0.1", "glances": True, "glances_port": port}))

    def test_url_and_auth_helpers(self):
        self.assertEqual(gl.base_url({"address": "10.0.0.5", "glances_port": 61208}), "http://10.0.0.5:61208")
        self.assertEqual(gl.base_url({"address": "10.0.0.5", "glances_port": 443, "glances_https": True}), "https://10.0.0.5:443")
        self.assertEqual(gl.base_url({"address": "fe80::1"}), "http://[fe80::1]:61208")
        self.assertEqual(gl.base_url({"address": "box.lan", "glances_port": "junk"}), "http://box.lan:61208")
        self.assertEqual(gl.auth_header({}), {})
        if WIN:
            h = {"glances_password_enc": core.protect_password("pw")}
            self.assertEqual(gl.auth_header(h), {"Authorization": "Basic " + base64.b64encode(b"glances:pw").decode()})


class FakeApp:
    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.status = tk.StringVar()
        self.q = queue.Queue()
        self.opened = []

    def post(self, fn):
        self.q.put(fn)

    def pump(self, until, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.q.get(timeout=0.03)()
            except queue.Empty:
                pass
            if until():
                return True
        return False


class StatsTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1100x700")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        mon.MonitorTab.AUTO_START = True               # other test modules switch this off while they run
        self.app = FakeApp(self.root)
        self.srv = FakeGlances()
        self.addCleanup(self.srv.close)
        self.tabs = []

    def tearDown(self):
        for t in self.tabs:
            t.close()
        self.app.nb.destroy()

    def open(self, host=None):
        mon.MonitorTab.DEFAULT_INTERVAL = 1
        try:
            tab = mon.MonitorTab(self.app, host or self.srv.host(), "", "")
        finally:
            mon.MonitorTab.DEFAULT_INTERVAL = 5
        self.app.nb.add(tab, text="stats")
        self.tabs.append(tab)
        return tab

    def pages(self, tab):
        return [tab.book.tab(t, "text").strip() for t in tab.book.tabs()]

    def test_tabs_tiles_and_tables_from_glances(self):
        tab = self.open()
        self.assertTrue(self.app.pump(lambda: tab.cpu_big.get() != "-"), tab.status_var.get())
        self.assertEqual(tab.cpu_big.get(), "24%")                                                 # 23.5
        self.assertIn("4 cores", tab.cpu_sub.get())
        self.assertIn("Hottest sensor: Core 1 61 °C", tab.cpu_sub.get())
        self.assertEqual(tab.mem_big.get(), "38%")
        self.assertEqual(tab.sys_big.get(), "plexbox")
        self.assertIn("2.0 KB/s", tab.net_big.get())
        self.assertIn("Glances", tab.status_var.get())
        self.assertEqual(list(tab.procs.get_children()), ["101", "7"])
        self.assertEqual(list(tab.disks.get_children()), ["/", "/mnt/media"])
        self.assertEqual(tab.disks.item("/mnt/media", "tags"), ("crit",))                          # 92 % full
        self.assertEqual(list(tab.cont.get_children()), ["plex", "old"])
        self.assertEqual(tab.cont.item("old", "tags"), ("crit",))                                  # an exited container stands out
        self.assertEqual(tab.cont.item("plex", "values")[3], "512.0 MB")
        self.assertEqual(self.pages(tab), ["Processes", "Disks", "Network", "Containers", "System"])   # no Services / Listening ports

    def test_no_ssh_is_used(self):
        with mock.patch.object(core, "open_ssh_client", side_effect=AssertionError("SSH must not be used")):
            tab = self.open()
            self.assertTrue(self.app.pump(lambda: tab.cpu_big.get() != "-"), tab.status_var.get())
        self.assertIsNone(tab.client)

    def test_system_tab_shows_glances_info_and_keeps_up_to_date(self):
        tab = self.open()
        self.assertTrue(self.app.pump(lambda: tab.cpu_big.get() != "-"))
        tab.book.select(len(tab.book.tabs()) - 1)
        self.root.update()
        self.assertIn("Source: Glances", tab.info_text.get("1.0", "end"))
        self.assertIn("From Glances", tab.info_status.get())

    def test_process_details_are_read_only(self):
        tab = self.open()
        self.assertTrue(self.app.pump(lambda: tab.cpu_big.get() != "-"))
        tab.procs.selection_set("101")
        with mock.patch.object(mon.messagebox, "showinfo") as info:
            tab.open_process()
        text = info.call_args[0][1]
        self.assertIn("/usr/bin/python3 /opt/app.py --flag", text)
        self.assertIn("read-only", text)
        self.assertFalse([w for w in tab.winfo_children() if isinstance(w, mon.ProcessDialog)])      # no signal dialog

    def test_a_machine_without_glances_says_so_and_keeps_trying(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        tab = self.open(core.sanitize_host({"name": "x", "address": "127.0.0.1", "glances": True, "glances_port": port}))
        self.assertTrue(self.app.pump(lambda: "Glances problem" in tab.status_var.get()), tab.status_var.get())
        self.assertIn("Could not connect", tab.status_var.get())
        self.assertEqual(tab.cpu_big.get(), "-")

    def test_ssh_mode_is_untouched(self):
        mon.MonitorTab.AUTO_START = False
        try:
            tab = mon.MonitorTab(self.app, {"name": "x", "address": "10.0.0.1"}, "u", "p")
        finally:
            mon.MonitorTab.AUTO_START = True
        self.app.nb.add(tab, text="ssh")
        self.tabs.append(tab)
        self.assertFalse(tab.glances)
        self.assertEqual(self.pages(tab), ["Processes", "Disks", "Network", "Listening ports", "Services", "System", "Docker", "Firewall", "Capture", "Hardware", "IPMI"])


class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def editor(self, **host):
        app = FakeApp(self.root)
        app.hosts, app.editors, app.editor_done = [], {}, lambda *a: None
        app.open_identities = app.open_proxies = lambda: None
        self.addCleanup(app.nb.destroy)
        h = dict(core.NEW_HOST, id="h", name="srv", address="127.0.0.1", **host)
        return core.HostEditor(app, h, [], "h"), app

    def test_defaults_are_off(self):
        h = core.sanitize_host({"address": "x.lan"})
        self.assertEqual((h["glances"], h["glances_port"], h["glances_https"], h["glances_user"], h["glances_password_enc"]), (False, 61208, False, "", ""))
        h = core.sanitize_host({"address": "x.lan", "glances": 1, "glances_port": "junk", "glances_https": "yes", "glances_user": None})
        self.assertEqual((h["glances"], h["glances_port"], h["glances_https"], h["glances_user"]), (True, 61208, True, ""))

    def test_the_form_has_a_glances_page_and_reads_it(self):
        ed, _ = self.editor()
        self.assertIn("Glances", [ed.pages.tab(t, "text").strip() for t in ed.pages.tabs()])
        self.assertFalse(ed.gl_on.get())
        h, errors, _ = ed.collect()
        self.assertEqual((h["glances"], h["glances_port"]), (False, 61208))
        ed.gl_on.set(True)
        ed.gl_port.set("61999")
        ed.gl_https.set(True)
        ed.gl_user.set("ron")
        h, errors, _ = ed.collect()
        self.assertFalse([e for e in errors if "Glances" in e])
        self.assertEqual((h["glances"], h["glances_port"], h["glances_https"], h["glances_user"]), (True, 61999, True, "ron"))

    def test_bad_port_is_an_error(self):
        ed, _ = self.editor()
        for bad in ("abc", "0", "70000"):
            ed.gl_port.set(bad)
            _, errors, _ = ed.collect()
            self.assertTrue([e for e in errors if "Glances port" in e], bad)

    @unittest.skipUnless(WIN, "uses DPAPI")
    def test_password_is_saved_encrypted_kept_and_forgotten(self):
        ed, _ = self.editor()
        ed.gl_pw.set("hunter2")
        h, _, _ = ed.collect()
        self.assertNotIn("hunter2", json.dumps(h))
        self.assertEqual(core.unprotect_password(h["glances_password_enc"]), "hunter2")
        ed2, _ = self.editor(glances_password_enc=h["glances_password_enc"])
        h2, _, _ = ed2.collect()
        self.assertEqual(h2["glances_password_enc"], h["glances_password_enc"])                    # left empty: kept
        ed2.gl_forget.set(True)
        h3, _, _ = ed2.collect()
        self.assertEqual(h3["glances_password_enc"], "")

    def test_detect_button_finds_glances_and_ticks_the_box(self):
        srv = FakeGlances()
        self.addCleanup(srv.close)
        ed, app = self.editor()
        ed.vars["address"].set("127.0.0.1")
        ed.gl_port.set(str(srv.port))
        ed.detect_glances()
        self.assertTrue(app.pump(lambda: ed.gl_on.get() or "✖" in ed.gl_result.get()), ed.gl_result.get())
        self.assertTrue(ed.gl_on.get())
        self.assertIn("Found: Glances API v4", ed.gl_result.get())

    def test_detect_button_reports_failure_without_ticking(self):
        ed, app = self.editor()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        ed.gl_port.set(str(port))
        ed.detect_glances()
        self.assertTrue(app.pump(lambda: "✖" in ed.gl_result.get()))
        self.assertFalse(ed.gl_on.get())
        ed.gl_port.set("x")
        ed.detect_glances()
        self.assertIn("port must be", ed.gl_result.get())
        ed.gl_port.set("61208")
        ed.vars["address"].set("")
        ed.detect_glances()
        self.assertIn("Enter the server's address", ed.gl_result.get())

    def test_stats_button_skips_the_ssh_login_for_glances_servers(self):
        mon.MonitorTab.AUTO_START = False
        try:
            app = FakeApp(self.root)
            self.addCleanup(app.nb.destroy)
            app.add_tab = lambda w, title: (app.nb.add(w, text=title), app.opened.append(title))
            app.credentials = mock.Mock(side_effect=AssertionError("no login needed"))
            core.App.act_stats(app, core.sanitize_host({"name": "pc", "address": "10.0.0.9", "glances": True}))
            self.assertEqual(app.opened, ["Stats · pc"])
            tab = app.nb.nametowidget(app.nb.tabs()[0])
            self.assertTrue(tab.glances)
            tab.close()
            app.credentials = mock.Mock(return_value=("u", "p"))
            core.App.act_stats(app, core.sanitize_host({"name": "ssh-box", "address": "10.0.0.8"}))
            app.credentials.assert_called_once()
            app.nb.nametowidget(app.nb.tabs()[1]).close()
        finally:
            mon.MonitorTab.AUTO_START = True

    def test_connection_list_says_where_the_stats_come_from(self):
        by = lambda h: {c["action"]: c for c in core.connection_list(h)}["stats"]["detail"]
        self.assertEqual(by(core.sanitize_host({"address": "x.lan"})), "over SSH, port 22")
        self.assertEqual(by(core.sanitize_host({"address": "x.lan", "glances": True, "glances_port": 61209})), "from Glances, port 61209")

    def test_network_scan_knows_glances(self):
        self.assertIn(61208, [p for p, _ in ns.COMMON_PORTS])
        self.assertEqual(ns.describe_ports([22, 61208]), "22 SSH, 61208 Glances")
        self.assertTrue(ns.suggest_host("10.0.0.5", "pc", "", [61208])["glances"])
        self.assertFalse(ns.suggest_host("10.0.0.5", "pc", "", [22])["glances"])


if __name__ == "__main__":
    unittest.main()
