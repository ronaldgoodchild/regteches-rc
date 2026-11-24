"""Tests for rc_prefs.py and the program features that use it: tab management (close others / left / right, lock, drag to reorder,
Ctrl+1-9, Ctrl+Tab, Ctrl+Shift+D), the exit confirmation, remembering and reopening the open tabs, and applying Preferences.
They drive the real App in a throw-away data folder.  Run from the RC folder:  python -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

os.environ["RC_DATA_DIR"] = tempfile.mkdtemp(prefix="rc_prefs_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as core  # noqa: E402
import rc_monitor  # noqa: E402
import rc_prefs as pf  # noqa: E402
import rc_terminal as term  # noqa: E402


class DataDir(unittest.TestCase):
    def setUp(self):
        self._old = core.APP_DIR
        core.APP_DIR = tempfile.mkdtemp(prefix="rc_prefsdata_")

    def tearDown(self):
        core.APP_DIR = self._old


class PrefsFileTests(DataDir):
    def test_defaults_and_round_trip(self):
        self.assertEqual(pf.load(), pf.DEFAULTS)
        p = dict(pf.DEFAULTS, confirm_exit=False, reopen_tabs=True, check_interval=300, stats_interval=10, ui_scale=125)
        pf.save(p)
        self.assertEqual(pf.load(), p)

    def test_bad_values_fall_back_one_by_one(self):
        with open(pf.prefs_path(), "w") as f:
            json.dump({"confirm_exit": "yes", "reopen_tabs": True, "check_interval": 7, "stats_interval": 5, "ui_scale": 999, "extra": 1}, f)
        got = pf.load()
        self.assertEqual((got["confirm_exit"], got["reopen_tabs"], got["check_interval"], got["stats_interval"], got["ui_scale"]),
                         (True, True, 60, 5, 100))
        self.assertNotIn("extra", got)

    def test_damaged_or_odd_files(self):
        with open(pf.prefs_path(), "w") as f:
            f.write("{nope")
        self.assertEqual(pf.load(), pf.DEFAULTS)
        with open(pf.prefs_path(), "w") as f:
            f.write("[1,2]")
        self.assertEqual(pf.load(), pf.DEFAULTS)


class SessionFileTests(DataDir):
    def test_clean(self):
        items = [{"action": "ssh", "host": "h1", "over": {"a": 1}, "active": 1}, {"action": "bogus", "host": "h"}, {"action": "files"},
                 {"action": "stats", "host": ""}, "junk", {"action": "telnet", "host": "h2", "over": "nope"}]
        self.assertEqual(pf.clean_session(items), [{"action": "ssh", "host": "h1", "over": {"a": 1}, "active": True},
                                                  {"action": "telnet", "host": "h2", "over": {}, "active": False}])
        self.assertEqual(pf.clean_session(None), [])
        self.assertEqual(len(pf.clean_session([{"action": "ssh", "host": str(i)} for i in range(50)])), pf.MAX_SESSION_TABS)

    def test_save_load_and_empty_removes_the_file(self):
        pf.save_session([{"action": "files", "host": "h1", "over": {}, "active": True}])
        self.assertEqual(pf.load_session(), [{"action": "files", "host": "h1", "over": {}, "active": True}])
        pf.save_session([])
        self.assertFalse(os.path.exists(pf.session_path()))
        self.assertEqual(pf.load_session(), [])
        with open(pf.session_path(), "w") as f:
            f.write("{nope")
        self.assertEqual(pf.load_session(), [])


class Tab(ttk.Frame):
    """A stand-in session tab."""

    def __init__(self, nb, name):
        super().__init__(nb)
        self.name, self.closed = name, False

    def close(self):
        self.closed = True


class AppBase(DataDir):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1300x760")
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        term.TerminalTab.AUTO_CONNECT = False
        rc_monitor.MonitorTab.AUTO_START = False

    @classmethod
    def tearDownClass(cls):
        term.TerminalTab.AUTO_CONNECT = True
        rc_monitor.MonitorTab.AUTO_START = True
        cls.root.destroy()

    def setUp(self):
        super().setUp()
        self._files = {k: getattr(core, k) for k in ("HOSTS_FILE", "SNIPPETS_FILE", "WORKSPACES_FILE", "IDENTITIES_FILE", "PROXIES_FILE", "KNOWN_HOSTS", "SETTINGS_FILE")}
        for name, fn in (("HOSTS_FILE", "hosts.json"), ("SNIPPETS_FILE", "snippets.json"), ("WORKSPACES_FILE", "workspaces.json"),
                         ("IDENTITIES_FILE", "identities.json"), ("PROXIES_FILE", "proxies.json"), ("KNOWN_HOSTS", "known_hosts"),
                         ("SETTINGS_FILE", "settings.json")):
            setattr(core, name, os.path.join(core.APP_DIR, fn))
        core._LIST_CACHE.clear()
        self.app = core.App(self.root)
        self.addCleanup(self.app.monitor.stop)
        self.msgs = []
        self.answer = True
        for name in ("showinfo", "showwarning", "showerror"):
            p = mock.patch.object(core.messagebox, name, side_effect=lambda *a, **k: self.msgs.append(a[1]))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(core.messagebox, "askyesno", side_effect=lambda *a, **k: (self.msgs.append(a[1]), self.answer)[1])
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        for t in list(self.app.nb.tabs()):
            w = self.app.nb.nametowidget(t)
            if w is not self.app.home and hasattr(w, "close"):
                try:
                    w.close()
                except Exception:
                    pass
        for child in list(self.root.winfo_children()):             # the next test builds a fresh App in the same window
            try:
                child.destroy()
            except tk.TclError:
                pass
        for k, v in self._files.items():
            setattr(core, k, v)
        core._LIST_CACHE.clear()
        super().tearDown()

    def add(self, name):
        t = Tab(self.app.nb, name)
        self.app.nb.add(t, text=f"  {name}  ")
        return t

    def names(self):
        return [getattr(self.app.nb.nametowidget(t), "name", "SERVERS") for t in self.app.nb.tabs()]


class TabManagementTests(AppBase):
    def setUp(self):
        super().setUp()
        self.tabs = {n: self.add(n) for n in "ABCDE"}

    def test_close_others_right_and_left(self):
        self.app.nb.select(self.tabs["C"])
        self.assertEqual(self.app.close_tabs("right"), 2)
        self.assertEqual(self.names(), ["SERVERS", "A", "B", "C"])
        self.assertTrue(self.tabs["D"].closed and self.tabs["E"].closed)
        self.assertEqual(self.app.close_tabs("left"), 2)
        self.assertEqual(self.names(), ["SERVERS", "C"])
        self.tabs2 = {n: self.add(n) for n in "XY"}
        self.app.nb.select(self.tabs["C"])
        self.assertEqual(self.app.close_tabs("others"), 2)
        self.assertEqual(self.names(), ["SERVERS", "C"])                          # the Servers tab is never closed

    def test_locked_tabs_survive_every_way_of_closing(self):
        self.app.toggle_lock(self.tabs["B"])
        self.app.nb.select(self.tabs["A"])
        self.assertEqual(self.app.close_tabs("others"), 3)
        self.assertEqual(self.names(), ["SERVERS", "A", "B"])
        self.assertIn("kept 1 locked", self.app.status.get())
        self.app.nb.select(self.tabs["B"])
        self.app.close_tab()
        self.assertIn("B", self.names())
        self.assertIn("locked", self.app.status.get())
        self.app.disconnect_all()
        self.assertEqual(self.names(), ["SERVERS", "B"])                          # Disconnect all keeps the locked one
        self.assertTrue(self.tabs["A"].closed and not self.tabs["B"].closed)
        self.app.toggle_lock(self.tabs["B"])                                      # unlock
        self.app.nb.select(self.tabs["B"])
        self.app.close_tab()
        self.assertEqual(self.names(), ["SERVERS"])

    def test_lock_mark_in_the_title_and_the_servers_tab_cannot_be_locked(self):
        self.assertTrue(self.app.toggle_lock(self.tabs["A"]))
        self.assertIn("🔒", self.app.nb.tab(self.tabs["A"], "text"))
        self.assertFalse(self.app.toggle_lock(self.tabs["A"]))
        self.assertNotIn("🔒", self.app.nb.tab(self.tabs["A"], "text"))
        self.assertFalse(self.app.toggle_lock(self.app.home))
        self.assertNotIn("🔒", self.app.nb.tab(self.app.home, "text"))

    def test_rename_keeps_the_marks(self):
        self.app.nb.select(self.tabs["A"])
        self.app.toggle_lock(self.tabs["A"])
        with mock.patch.object(core.simpledialog, "askstring", return_value="Renamed"):
            self.app.rename_tab()
        text = self.app.nb.tab(self.tabs["A"], "text")
        self.assertIn("Renamed", text)
        self.assertIn("🔒", text)
        self.assertEqual(text.count("🔒"), 1)

    def test_context_menu_offers_the_new_items(self):
        self.app.nb.select(self.tabs["B"])
        self.app.fill_tab_popup()
        m = self.app.tab_popup
        labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"]
        self.assertIn("🔒 Lock tab (can't be closed by accident)", labels)
        self.assertIn("Close several tabs", labels)
        self.app.toggle_lock(self.tabs["B"])
        self.app.fill_tab_popup()
        self.assertIn("🔓 Unlock tab", [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"])

    def test_dragging_a_tab_reorders_it_but_not_the_servers_tab(self):
        self.root.update()
        nb = self.app.nb

        for _ in range(8):
            self.root.update()
            time.sleep(0.1)

        def at(index):                                                            # ttk notebooks have no bbox: find the tab by probing the tab strip
            for y in (6, 12, 18, 24, 30):
                for x in range(0, nb.winfo_width(), 3):
                    try:
                        if nb.index(f"@{x},{y}") == index:
                            return mock.Mock(x=x + 2, y=y)
                    except tk.TclError:
                        continue
            raise AssertionError(f"tab {index} not found in the tab strip")
        self.app.tab_press(at(1))                                                 # grab A
        self.assertIs(self.app.drag_tab, self.tabs["A"])
        self.app.tab_drag(at(3))                                                  # drop it over C
        self.assertEqual(self.names().index("A"), 3)
        self.app.tab_press(at(0))                                                 # the Servers tab does not move
        self.app.tab_drag(at(2))
        self.assertEqual(self.names()[0], "SERVERS")
        self.app.drag_tab = self.tabs["B"]
        self.app.tab_drag(at(0))                                                  # nothing can go before Servers
        self.assertEqual(self.names()[0], "SERVERS")

    def test_keyboard_shortcuts(self):
        nb = self.app.nb
        order = list(nb.tabs())
        self.assertTrue(self.app.handle_shortcut("3", True, False))
        self.assertEqual(nb.select(), order[2])
        self.assertTrue(self.app.handle_shortcut("9", True, False))
        self.assertEqual(nb.select(), order[-1])                                  # Ctrl+9 = the last tab
        self.assertTrue(self.app.handle_shortcut("Tab", True, False))
        self.assertEqual(nb.select(), order[0])                                   # wraps around
        self.assertTrue(self.app.handle_shortcut("ISO_Left_Tab", True, True))
        self.assertEqual(nb.select(), order[-1])
        self.assertTrue(self.app.handle_shortcut("Tab", True, True))
        self.assertEqual(nb.select(), order[-2])
        self.assertFalse(self.app.handle_shortcut("3", False, False))             # without Ctrl: not ours
        self.assertFalse(self.app.handle_shortcut("x", True, False))
        with mock.patch.object(self.app, "duplicate_session") as dup:
            self.assertTrue(self.app.handle_shortcut("D", True, True))
            dup.assert_called_once()
            self.assertFalse(self.app.handle_shortcut("d", True, False))
        self.assertTrue(self.app.handle_shortcut("7", True, False))               # more than there are tabs: the last one
        self.assertEqual(nb.select(), order[min(6, len(order) - 1)])

    def test_shortcuts_work_while_typing_in_a_terminal_and_are_not_sent_to_the_server(self):
        t = term.TerminalTab(self.app, {"name": "box", "address": "10.0.0.5"}, "u", "p")
        self.app.nb.add(t, text="  SSH  ")
        self.app.nb.select(t)
        sent = []
        t.send = sent.append
        ev = mock.Mock(state=0x4, keysym="2", char="2")
        self.assertEqual(t.on_key(ev), "break")
        self.assertEqual(self.app.nb.select(), self.app.nb.tabs()[1])             # went to tab 2
        self.assertEqual(sent, [])
        self.app.nb.select(t)
        ev = mock.Mock(state=0x4, keysym="c", char="\x03")                        # Ctrl+C still reaches the server
        t.on_key(ev)
        self.assertEqual(sent, ["\x03"])


class SessionTests(AppBase):
    def setUp(self):
        super().setUp()
        self.h1 = core.sanitize_host({"id": "h1", "name": "web", "address": "10.0.0.1"})
        self.h2 = core.sanitize_host({"id": "h2", "name": "pc", "address": "10.0.0.2", "glances": True})
        self.app.hosts[:] = [self.h1, self.h2]

    def open_tabs(self):
        ssh = term.TerminalTab(self.app, self.h1, "u", "p")
        self.app.nb.add(ssh, text="  SSH · web  ")
        with mock.patch("rc_files.FilesTab.connect", lambda self: None):
            import rc_files
            files = rc_files.FilesTab(self.app, self.h1, "u", "p")
        self.app.nb.add(files, text="  Files · web  ")
        stats = rc_monitor.MonitorTab(self.app, self.h2, "", "")
        self.app.nb.add(stats, text="  Stats · pc  ")
        tel = term.TerminalTab(self.app, dict(self.h1, _kind="telnet", telnet_port=2323), "", "")
        self.app.nb.add(tel, text="  Telnet · web  ")
        self.add_other = Tab(self.app.nb, "other")
        self.app.nb.add(self.add_other, text="  other  ")
        self.app.nb.select(stats)
        return ssh, files, stats, tel

    def test_session_state_lists_the_reopenable_tabs(self):
        ssh, files, stats, tel = self.open_tabs()
        state = self.app.session_state()
        self.assertEqual([(i["action"], i["host"]) for i in state], [("ssh", "h1"), ("files", "h1"), ("stats", "h2"), ("telnet", "h1")])
        self.assertEqual(state[3]["over"], {"telnet_port": 2323})
        self.assertEqual([i["active"] for i in state], [False, False, True, False])         # the Stats tab was in front
        self.assertEqual(pf.clean_session(state), state)

    def test_restore_reopens_through_do_and_reselects_the_active_tab(self):
        calls = []

        def fake_do(action, over=None):
            calls.append((action, self.app.current_id(), over))
            self.app.add_tab(Tab(self.app.nb, action), action)
        with mock.patch.object(self.app, "do", side_effect=fake_do):
            pf.save_session([{"action": "ssh", "host": "h1", "over": {}, "active": False},
                              {"action": "stats", "host": "h2", "over": {}, "active": True},
                              {"action": "files", "host": "gone", "over": {}, "active": False},
                              {"action": "telnet", "host": "h1", "over": {"telnet_port": 23}, "active": False}])
            self.assertEqual(self.app.restore_session(), 3)
        self.assertEqual(calls, [("ssh", "h1", None), ("stats", "h2", None), ("telnet", "h1", {"telnet_port": 23})])     # the deleted server is skipped
        self.assertEqual(self.app.nb.nametowidget(self.app.nb.select()).name, "stats")
        self.assertIn("Reopened 3", self.app.status.get())

    def test_restore_survives_a_tab_that_fails_to_open(self):
        def fake_do(action, over=None):
            if action == "ssh":
                raise RuntimeError("boom")
            self.app.add_tab(Tab(self.app.nb, action), action)
        with mock.patch.object(self.app, "do", side_effect=fake_do):
            pf.save_session([{"action": "ssh", "host": "h1", "over": {}}, {"action": "files", "host": "h1", "over": {}}])
            self.assertEqual(self.app.restore_session(), 1)

    def test_nothing_saved_nothing_restored(self):
        self.assertEqual(self.app.restore_session(), 0)


class ExitTests(AppBase):
    def setUp(self):
        super().setUp()
        self.destroyed = []
        p = mock.patch.object(self.app.root, "destroy", side_effect=lambda: self.destroyed.append(1))
        p.start()
        self.addCleanup(p.stop)

    def test_no_sessions_no_question(self):
        self.app.on_exit()
        self.assertEqual(self.destroyed, [1])
        self.assertFalse(self.msgs)

    def test_open_sessions_ask_first(self):
        t = term.TerminalTab(self.app, {"name": "box", "address": "10.0.0.5"}, "u", "p")
        self.app.nb.add(t, text="  SSH  ")
        self.answer = False
        self.app.on_exit()
        self.assertEqual(self.destroyed, [])                                       # stayed open
        self.assertIn("1 session tab(s) are open", self.msgs[-1])
        self.answer = True
        self.app.on_exit()
        self.assertEqual(self.destroyed, [1])

    def test_the_question_mentions_running_transfers(self):
        with mock.patch("rc_files.FilesTab.connect", lambda self: None):
            import rc_files
            f = rc_files.FilesTab(self.app, core.sanitize_host({"id": "h1", "name": "web", "address": "10.0.0.1"}), "u", "p")
        self.app.nb.add(f, text="  Files  ")
        f.jobs.append({"state": "running"})
        self.answer = False
        self.app.on_exit()
        self.assertIn("still transferring", self.msgs[-1])
        f.jobs.clear()

    def test_can_be_switched_off(self):
        self.app.prefs["confirm_exit"] = False
        t = term.TerminalTab(self.app, {"name": "box", "address": "10.0.0.5"}, "u", "p")
        self.app.nb.add(t, text="  SSH  ")
        self.app.on_exit()
        self.assertEqual(self.destroyed, [1])
        self.assertFalse(self.msgs)

    def test_the_session_is_saved_only_when_reopening_is_on(self):
        t = term.TerminalTab(self.app, core.sanitize_host({"id": "h1", "name": "web", "address": "10.0.0.1"}), "u", "p")
        self.app.nb.add(t, text="  SSH  ")
        self.app.hosts.append(t.host)
        self.app.on_exit()
        self.assertFalse(os.path.exists(pf.session_path()))
        self.app.prefs["reopen_tabs"] = True
        self.app.on_exit()
        self.assertEqual([(i["action"], i["host"]) for i in pf.load_session()], [("ssh", "h1")])
        self.app.prefs["reopen_tabs"] = False                                      # switched off again: the old list is cleared
        self.app.nb.forget(t)
        self.app.on_exit()
        self.assertFalse(os.path.exists(pf.session_path()))


class ApplyTests(AppBase):
    def test_apply_sets_the_monitor_stats_interval_and_scaling(self):
        self.app.prefs.update(check_interval=300, stats_interval=10, ui_scale=150)
        self.app.apply_prefs()
        self.assertEqual(self.app.monitor.interval, 300)
        self.assertEqual(rc_monitor.MonitorTab.DEFAULT_INTERVAL, 10)
        self.assertAlmostEqual(float(self.root.tk.call("tk", "scaling")), self.app.base_scaling * 1.5, places=2)
        self.app.prefs.update(check_interval=60, stats_interval=5, ui_scale=100)
        self.app.apply_prefs()
        self.assertAlmostEqual(float(self.root.tk.call("tk", "scaling")), self.app.base_scaling, places=2)
        self.assertEqual(rc_monitor.MonitorTab.DEFAULT_INTERVAL, 5)

    def test_preferences_tab_saves_and_applies(self):
        self.app.open_prefs()
        tab = self.app.prefs_tab
        self.assertIs(self.app.open_singleton("prefs_tab", lambda: None, "x"), tab)          # one tab only
        tab.confirm.set(False)
        tab.set(confirm_exit=False)
        tab.check.set("Every 5 minutes")
        tab.check.event_generate("<<ComboboxSelected>>")
        tab.stats.set("30 s")
        tab.stats.event_generate("<<ComboboxSelected>>")
        tab.scale.set("125%")
        tab.scale.event_generate("<<ComboboxSelected>>")
        saved = pf.load()
        self.assertEqual((saved["confirm_exit"], saved["check_interval"], saved["stats_interval"], saved["ui_scale"]), (False, 300, 30, 125))
        self.assertEqual(self.app.monitor.interval, 300)
        self.app.prefs.update(ui_scale=100)
        self.app.apply_prefs()

    def test_preferences_tab_shows_the_current_values(self):
        self.app.prefs.update(check_interval=0, stats_interval=60, ui_scale=80, reopen_tabs=True)
        self.app.open_prefs()
        tab = self.app.prefs_tab
        self.assertEqual((tab.check.get(), tab.stats.get(), tab.scale.get(), tab.reopen.get()), ("Off (only when I click ⟳)", "60 s", "80%", True))
        self.app.prefs.update(ui_scale=100)
        self.app.apply_prefs()


class MonitorTests(unittest.TestCase):
    def test_interval_zero_checks_once_then_only_on_request(self):
        rounds = []
        m = core.StatusMonitor(lambda: [], lambda i, r: None, interval=0)
        m.round = lambda: rounds.append(time.time())
        m.start()
        time.sleep(0.4)
        self.assertEqual(len(rounds), 1)                                           # the first check at start-up
        time.sleep(0.4)
        self.assertEqual(len(rounds), 1)                                           # and then silence
        m.refresh()
        time.sleep(0.4)
        self.assertEqual(len(rounds), 2)
        m.stop()

    def test_the_status_check_knows_about_proxies(self):
        seen = []
        m = core.StatusMonitor(lambda: [{"id": "a", "address": "10.0.0.1", "proxy": "px", "ssh_port": 22, "rdp_port": 3389}], lambda i, r: None)
        with mock.patch.object(core, "check_host_status", side_effect=lambda h: (seen.append(h), {"state": "up", "addr": h["address"], "ms": 1, "port": 22,
                                                                                                "service": True, "via_alt": False})[1]):
            m.round()
        self.assertEqual(seen[0]["proxy"], "px")                                    # a proxied server must be tested through its proxy


if __name__ == "__main__":
    unittest.main()
