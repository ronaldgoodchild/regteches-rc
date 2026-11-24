"""Tests for the split view (SplitTab) and Broadcast input. They use the real App methods on a stand-in app and
never open a network connection. Run from the RC folder:  python -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import tkinter as tk
import unittest
from unittest import mock
from tkinter import ttk

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_split_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as core  # noqa: E402
import rc_terminal as term  # noqa: E402


class FakeTheme:
    def paint(self, w):
        pass


class FakeApp:
    """Just enough of App for the terminal tabs, with App's own broadcast/tab methods bound to it."""
    all_terminals = core.App.all_terminals
    cur_tab = core.App.cur_tab
    broadcast_peers = core.App.broadcast_peers
    broadcast_all = core.App.broadcast_all
    broadcast_changed = core.App.broadcast_changed
    refresh_tab_title = core.App.refresh_tab_title
    tab_title_base = core.App.tab_title_base
    active_terminal = core.App.active_terminal

    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.status = tk.StringVar()
        self.theme = FakeTheme()
        self.hosts = [{"id": str(i), "name": f"srv{i}", "address": f"10.0.0.{i}", "group": "lab"} for i in range(1, 6)]
        self.asked = 0

    def post(self, fn):
        fn()

    def credentials(self, h, allow_anonymous=False):
        return ("u", "p")

    def mark_connected(self, h):
        pass

    def confirm_broadcast(self):
        self.asked += 1
        return True


class Recorder:
    """Replaces a TerminalTab's send() so we can see what each session would have received."""
    def __init__(self, tab):
        self.got = []
        tab.send = self.got.append


class SplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1000x600")
        term.TerminalTab.AUTO_CONNECT = False

    @classmethod
    def tearDownClass(cls):
        term.TerminalTab.AUTO_CONNECT = True
        cls.root.destroy()

    def setUp(self):
        self.app = FakeApp(self.root)
        self.split = term.SplitTab(self.app)
        self.app.nb.add(self.split, text="  Split view  ")
        self.root.update()

    def tearDown(self):
        self.split.close()
        self.app.nb.destroy()

    def add(self, n):
        for h in self.app.hosts[:n]:
            self.assertIsNotNone(self.split.add_host(h))
        self.root.update()

    def cells(self):
        return sorted((int(p["frame"].grid_info()["row"]), int(p["frame"].grid_info()["column"])) for p in self.split.panes)

    def test_grid_layouts(self):
        self.add(1)
        self.assertEqual(self.cells(), [(0, 0)])
        self.split.add_host(self.app.hosts[1])       # now 2 panes: side by side
        self.assertEqual(self.cells(), [(0, 0), (0, 1)])
        self.split.add_host(self.app.hosts[2])
        self.split.add_host(self.app.hosts[3])       # 4 panes: 2 x 2
        self.assertEqual(self.cells(), [(0, 0), (0, 1), (1, 0), (1, 1)])
        self.split.layout_var.set("rows")
        self.split.relayout()
        self.assertEqual(self.cells(), [(0, 0), (1, 0), (2, 0), (3, 0)])
        self.split.layout_var.set("cols")
        self.split.relayout()
        self.assertEqual(self.cells(), [(0, 0), (0, 1), (0, 2), (0, 3)])

    def test_lonely_last_pane_fills_its_row(self):
        self.add(3)
        last = self.split.panes[-1]["frame"].grid_info()
        self.assertEqual((int(last["row"]), int(last["column"]), int(last["columnspan"])), (1, 0, 2))

    def test_zoom_and_unzoom(self):
        self.add(3)
        p = self.split.panes[1]
        self.split.zoom(p)
        self.root.update()
        self.assertEqual([q for q in self.split.panes if q["frame"].winfo_manager()], [p])
        self.split.zoom(p)
        self.root.update()
        self.assertEqual(len([q for q in self.split.panes if q["frame"].winfo_manager()]), 3)

    def test_remove_pane_closes_it_and_shows_the_hint_when_empty(self):
        self.add(2)
        gone = self.split.panes[0]
        self.split.remove_pane(gone)
        self.assertTrue(gone["tab"].closed)
        self.assertEqual(len(self.split.panes), 1)
        self.split.remove_pane(self.split.panes[0])
        self.root.update()
        self.assertTrue(self.split.hint.winfo_ismapped())

    def test_limit_of_nine(self):
        for i in range(term.SplitTab.MAX_PANES):
            self.assertIsNotNone(self.split.add_host(self.app.hosts[i % 5]))
        orig = term.messagebox.showinfo
        term.messagebox.showinfo = lambda *a, **k: None
        try:
            self.assertIsNone(self.split.add_host(self.app.hosts[0]))
        finally:
            term.messagebox.showinfo = orig
        self.assertEqual(len(self.split.panes), 9)

    def test_no_broadcast_by_default(self):
        self.add(3)
        recs = [Recorder(t) for t in self.split.terminals()]
        self.split.terminals()[0].send_user("ls\r")
        self.assertEqual([r.got for r in recs], [["ls\r"], [], []])

    def test_broadcast_all_panes(self):
        self.add(3)
        recs = [Recorder(t) for t in self.split.terminals()]
        self.split.bcast_var.set(True)
        self.split.toggle_broadcast()
        self.assertTrue(all(t.bcast_var.get() for t in self.split.terminals()))
        self.split.terminals()[1].send_user("uptime\r")
        self.assertEqual([r.got for r in recs], [["uptime\r"]] * 3)
        self.assertIn("3 terminal", self.app.status.get())

    def test_only_ticked_terminals_receive(self):
        self.add(3)
        recs = [Recorder(t) for t in self.split.terminals()]
        a, b, c = self.split.terminals()
        a.bcast_var.set(True)
        b.bcast_var.set(True)
        a.send_user("x")
        self.assertEqual([r.got for r in recs], [["x"], ["x"], []])
        c.send_user("y")                 # c is not ticked: stays private
        self.assertEqual([r.got for r in recs], [["x"], ["x"], ["y"]])

    def test_broadcast_spans_normal_tabs_and_split_panes(self):
        self.add(2)
        solo = term.TerminalTab(self.app, self.app.hosts[4], "u", "p")
        self.app.nb.add(solo, text="  SSH · srv5  ")
        recs = [Recorder(t) for t in self.split.terminals() + [solo]]
        solo.bcast_var.set(True)
        self.split.terminals()[0].bcast_var.set(True)
        solo.send_user("hi\r")
        self.assertEqual(recs[0].got, ["hi\r"])
        self.assertEqual(recs[1].got, [])
        self.assertEqual(recs[2].got, ["hi\r"])
        solo.close()

    def test_pasted_text_is_wrapped_per_receiving_terminal(self):
        self.add(2)
        a, b = self.split.terminals()
        ra, rb = Recorder(a), Recorder(b)
        a.bcast_var.set(True)
        b.bcast_var.set(True)
        b.screen.bracketed = True        # only the second session asked for bracketed paste
        self.root.clipboard_clear()
        self.root.clipboard_append("one\ntwo")
        with mock.patch.object(term.messagebox, "askyesno", return_value=True):
            a.paste()
        self.assertEqual(ra.got, ["one\rtwo"])
        self.assertEqual(rb.got, ["\x1b[200~one\rtwo\x1b[201~"])

    def test_terminal_replies_are_never_broadcast(self):
        self.add(2)
        a, b = self.split.terminals()
        ra, rb = Recorder(a), Recorder(b)
        a.bcast_var.set(True)
        b.bcast_var.set(True)
        a.send("\x1b[0n")                # e.g. an answer to a cursor-position query: plain send() only
        self.assertEqual((ra.got, rb.got), (["\x1b[0n"], []))

    def test_tab_title_gets_a_broadcast_mark(self):
        self.add(1)
        self.split.terminals()[0].bcast_var.set(True)
        self.app.broadcast_changed()
        self.assertIn("📡", self.app.nb.tab(self.split, "text"))
        self.split.terminals()[0].bcast_var.set(False)
        self.app.broadcast_changed()
        self.assertNotIn("📡", self.app.nb.tab(self.split, "text"))
        self.assertIn("Split view", self.app.nb.tab(self.split, "text"))

    def test_closing_the_tab_closes_every_session(self):
        self.add(3)
        tabs = self.split.terminals()
        self.split.close()
        self.assertTrue(all(t.closed for t in tabs))
        self.assertTrue(self.split.closed)


if __name__ == "__main__":
    unittest.main()
