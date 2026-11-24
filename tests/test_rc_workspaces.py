"""Tests for saved workspaces (a named split view): storage, saving, opening, managing.
They drive the real App on a throw-away data folder; no network is used.  Run:  python -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import tkinter as tk
import unittest
from tkinter import ttk

os.environ["RC_DATA_DIR"] = tempfile.mkdtemp(prefix="rc_ws_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as core  # noqa: E402
import rc_terminal as term  # noqa: E402


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._old = core.WORKSPACES_FILE
        self.dir = tempfile.mkdtemp(prefix="rc_wsfile_")
        core.WORKSPACES_FILE = os.path.join(self.dir, "workspaces.json")

    def tearDown(self):
        core.WORKSPACES_FILE = self._old

    def test_missing_file_is_empty(self):
        self.assertEqual(core.load_workspaces(), [])

    def test_round_trip(self):
        core.save_workspaces([{"name": "Plex stack", "layout": "cols", "hosts": ["a", "b"]}])
        self.assertEqual(core.load_workspaces(), [{"name": "Plex stack", "layout": "cols", "hosts": ["a", "b"]}])

    def test_bad_entries_are_cleaned(self):
        with open(core.WORKSPACES_FILE, "w") as f:
            json.dump([{"name": " ok ", "layout": "weird", "hosts": ["a", "", None, 5]}, {"name": "", "hosts": []},
                       {"name": "nohosts"}, "junk", {"name": "x", "hosts": "no"}], f)
        self.assertEqual(core.load_workspaces(), [{"name": "ok", "layout": "grid", "hosts": ["a", "5"]}])

    def test_more_than_nine_hosts_are_cut(self):
        core.save_workspaces([{"name": "big", "layout": "grid", "hosts": [str(i) for i in range(20)]}])
        self.assertEqual(len(core.load_workspaces()[0]["hosts"]), 9)

    def test_damaged_file_is_moved_aside_not_overwritten(self):
        with open(core.WORKSPACES_FILE, "w") as f:
            f.write("{nope")
        self.assertEqual(core.load_workspaces(), [])
        names = os.listdir(self.dir)
        self.assertTrue(any(n.startswith("workspaces.json.corrupt-") for n in names))
        self.assertNotIn("workspaces.json", names)


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1200x700")
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        term.TerminalTab.AUTO_CONNECT = False
        cls.app = core.App(cls.root)
        cls.app.credentials = lambda h, allow_anonymous=False: ("u", "p")
        cls.mb = (core.messagebox.askyesno, core.messagebox.showinfo, core.messagebox.showerror)
        cls.ask = core.simpledialog.askstring
        for n in "abc":
            cls.app.hosts.append(dict(core.NEW_HOST, id=n * 8, name="srv-" + n, address="10.1.1." + str(ord(n) - 96)))
        cls.app.refill()

    @classmethod
    def tearDownClass(cls):
        term.TerminalTab.AUTO_CONNECT = True
        core.messagebox.askyesno, core.messagebox.showinfo, core.messagebox.showerror = cls.mb
        core.simpledialog.askstring = cls.ask
        cls.root.destroy()

    def setUp(self):
        self._old = core.WORKSPACES_FILE
        core.WORKSPACES_FILE = os.path.join(tempfile.mkdtemp(prefix="rc_wsapp_"), "workspaces.json")
        self.infos = []
        core.messagebox.showinfo = lambda *a, **k: self.infos.append(a[1])
        core.messagebox.askyesno = lambda *a, **k: self.yes
        self.yes = True
        self.name = "Lab"
        core.simpledialog.askstring = lambda *a, **k: self.name

    def tearDown(self):
        core.WORKSPACES_FILE = self._old
        for t in list(self.app.nb.tabs()):
            w = self.app.nb.nametowidget(t)
            if w is not self.app.home:
                self.app.nb.select(w)
                self.app.close_tab()

    def split_with(self, ids, layout="grid"):
        sp = self.app.new_split_view()
        sp.layout_var.set(layout)
        for i in ids:
            sp.add_host(next(h for h in self.app.hosts if h["id"] == i))
        self.root.update()
        return sp

    def test_save_needs_a_split_view(self):
        self.app.save_workspace()
        self.assertTrue(self.infos)
        self.assertEqual(core.load_workspaces(), [])

    def test_save_and_open_round_trip(self):
        sp = self.split_with(["aaaaaaaa", "bbbbbbbb", "cccccccc"], "cols")
        self.app.save_workspace()
        saved = core.load_workspaces()
        self.assertEqual(saved, [{"name": "Lab", "layout": "cols", "hosts": ["aaaaaaaa", "bbbbbbbb", "cccccccc"]}])
        self.assertIn("Lab", self.app.nb.tab(sp, "text"))
        self.app.open_workspace("Lab")
        self.root.update()
        opened = self.app.cur_tab()
        self.assertIsNot(opened, sp)
        self.assertEqual([p["host"]["name"] for p in opened.panes], ["srv-a", "srv-b", "srv-c"])
        self.assertEqual(opened.layout_var.get(), "cols")
        self.assertEqual(sorted(p["frame"].grid_info()["column"] for p in opened.panes), [0, 1, 2])

    def test_saving_same_name_asks_before_replacing(self):
        self.split_with(["aaaaaaaa"])
        self.app.save_workspace()
        self.yes = False
        self.app.save_workspace()
        self.assertEqual(len(core.load_workspaces()), 1)
        self.yes = True
        self.app.save_workspace()
        self.assertEqual(len(core.load_workspaces()), 1)                # replaced, not duplicated

    def test_cancelled_name_saves_nothing(self):
        self.split_with(["aaaaaaaa"])
        self.name = "   "
        self.app.save_workspace()
        self.assertEqual(core.load_workspaces(), [])

    def test_deleted_servers_are_skipped(self):
        core.save_workspaces([{"name": "Old", "layout": "grid", "hosts": ["aaaaaaaa", "gone0000", "bbbbbbbb"]}])
        self.app.open_workspace("Old")
        self.assertEqual([p["host"]["name"] for p in self.app.cur_tab().panes], ["srv-a", "srv-b"])
        self.assertIn("1 server(s) no longer exist", self.app.status.get())

    def test_workspace_with_only_deleted_servers_opens_nothing(self):
        core.save_workspaces([{"name": "Dead", "layout": "grid", "hosts": ["gone0000"]}])
        before = len(self.app.nb.tabs())
        self.app.open_workspace("Dead")
        self.assertEqual(len(self.app.nb.tabs()), before)
        self.assertTrue(self.infos)

    def test_open_menu_lists_saved_workspaces(self):
        self.app.fill_workspace_menu()
        self.assertEqual(self.app.ws_menu.entrycget(0, "state"), "disabled")
        core.save_workspaces([{"name": "One", "layout": "grid", "hosts": ["aaaaaaaa"]},
                              {"name": "Two", "layout": "rows", "hosts": ["aaaaaaaa", "bbbbbbbb"]}])
        self.app.fill_workspace_menu()
        labels = [self.app.ws_menu.entrycget(i, "label") for i in range(self.app.ws_menu.index("end") + 1)]
        self.assertEqual(labels, ["One  (1)", "Two  (2)"])

    def test_missing_workspace(self):
        self.app.open_workspace("nope")
        self.assertTrue(self.infos)


if __name__ == "__main__":
    unittest.main()
