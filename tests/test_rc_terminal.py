"""Tests for rc_terminal.py: the emulated screen (scrollback, alternate screen), rendering, preferences, reconnect rules.
Run from the RC folder:  python -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import tkinter as tk
import unittest
from unittest import mock
from tkinter import ttk

_TMP = tempfile.mkdtemp(prefix="rc_term_test_")
os.environ.setdefault("RC_DATA_DIR", _TMP)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pyte  # noqa: E402
import rc_manager as core  # noqa: E402
import rc_terminal as term  # noqa: E402


def make_screen(cols=20, rows=5):
    sent = []
    scr = term.RcScreen(cols, rows, on_input=sent.append)
    return scr, pyte.Stream(scr, strict=False), sent


class ScreenTests(unittest.TestCase):
    def test_plain_text_and_colour(self):
        scr, st, _ = make_screen()
        st.feed("hi \x1b[31mred\x1b[0m ok")
        self.assertTrue(scr.display[0].startswith("hi red ok"))
        self.assertEqual(scr.buffer[0][3].fg, "red")

    def test_lines_that_scroll_off_are_kept(self):
        scr, st, _ = make_screen(rows=3)
        st.feed("one\r\ntwo\r\nthree\r\nfour\r\nfive")
        kept = ["".join(line[x].data for x in range(5)).rstrip() for line in scr.scrolled]
        self.assertEqual(kept, ["one", "two"])
        self.assertTrue(scr.display[0].startswith("three"))

    def test_alternate_screen_restores_the_shell(self):
        scr, st, _ = make_screen(rows=4)
        st.feed("$ ls\r\nfile1\r\n$ ")
        before = list(scr.display)
        st.feed("\x1b[?1049h")            # vim / less / htop start
        self.assertTrue(scr.alt)
        st.feed("\x1b[HFULL SCREEN APP")
        self.assertIn("FULL SCREEN APP", scr.display[0])   # the program's own picture is on screen
        st.feed("\r\n" + "x\r\n" * 10)                     # scrolling inside must NOT reach scrollback
        self.assertEqual(scr.scrolled, [])
        st.feed("\x1b[?1049l")            # program exits
        self.assertFalse(scr.alt)
        self.assertEqual(scr.display, before)
        self.assertEqual((scr.cursor.y, scr.cursor.x), (2, 2))

    def test_application_cursor_and_bracketed_paste_flags(self):
        scr, st, _ = make_screen()
        st.feed("\x1b[?1h\x1b[?2004h")
        self.assertTrue(scr.app_cursor and scr.bracketed)
        st.feed("\x1b[?1l\x1b[?2004l")
        self.assertFalse(scr.app_cursor or scr.bracketed)

    def test_cursor_position_query_is_answered(self):
        scr, st, sent = make_screen()
        st.feed("abc\x1b[6n")
        self.assertEqual("".join(sent), "\x1b[1;4R")

    def test_bell_callback(self):
        rang = []
        scr = term.RcScreen(10, 3, on_bell=lambda: rang.append(1))
        pyte.Stream(scr, strict=False).feed("\x07")
        self.assertEqual(rang, [1])

    def test_partial_escape_sequence_across_chunks(self):
        scr, st, _ = make_screen()
        st.feed("a\x1b[3")
        st.feed("2mb")
        self.assertEqual(scr.buffer[0][1].fg, "green")


class RenderHelperTests(unittest.TestCase):
    def line(self, text, **style):
        scr, st, _ = make_screen(cols=12)
        st.feed(text)
        return scr.buffer[0]

    def test_runs_trim_trailing_blanks_and_group_styles(self):
        runs = term.line_runs(self.line("ab\x1b[1;31mcd\x1b[0m"), 12)
        self.assertEqual([r[0] for r in runs], ["ab", "cd"])
        self.assertTrue(runs[1][1][2])  # bold

    def test_min_len_keeps_room_for_the_cursor(self):
        self.assertEqual("".join(t for t, _ in term.line_runs(None, 12, 5)), " " * 5)
        self.assertEqual(term.line_runs(None, 12, 0), [])

    def test_coloured_blanks_are_kept(self):
        runs = term.line_runs(self.line("\x1b[7m      "), 12)  # reverse-video bar, like vim's status line
        self.assertEqual(sum(len(t) for t, _ in runs), 6)

    def test_colours(self):
        self.assertEqual(term.style_colors(("red", "default", False, False, False, False)), (term.PALETTE[1], None))
        self.assertEqual(term.style_colors(("red", "default", True, False, False, False))[0], term.PALETTE[9])  # bold = bright
        self.assertEqual(term.style_colors(("ff8800", "default", False, False, False, False))[0], "#ff8800")
        self.assertEqual(term.style_colors(("default", "blue", False, False, False, True)), (term.PALETTE[4], term.DEFAULT_FG))
        self.assertEqual(term.COLOR_INDEX["brightcyan"], 14)

    def test_aixterm_names_are_all_known(self):
        from pyte import graphics as g
        for name in list(g.FG_ANSI.values()) + list(g.FG_AIXTERM.values()) + list(g.BG_ANSI.values()):
            self.assertTrue(name == "default" or name in term.COLOR_INDEX, name)


class PrefsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc_prefs_")
        self._old = core.APP_DIR
        core.APP_DIR = self.dir

    def tearDown(self):
        core.APP_DIR = self._old

    @staticmethod
    def core_two(prefs):
        """The size settings only: later versions added colour scheme, font, paste and Zmodem options."""
        return {k: prefs[k] for k in ("font_size", "scrollback")}

    def test_defaults_and_clamping(self):
        self.assertEqual(self.core_two(term.load_prefs()), {"font_size": 11, "scrollback": 10000})
        self.assertEqual(term.clean_prefs({"font_size": 500, "scrollback": 3})["font_size"], term.FONT_MAX)
        self.assertEqual(self.core_two(term.clean_prefs({"font_size": "x", "scrollback": None})), {"font_size": 11, "scrollback": 10000})
        self.assertEqual(term.clean_prefs("junk")["font_size"], 11)

    def test_round_trip_and_corrupt_file(self):
        term.save_prefs({"font_size": 14, "scrollback": 50000})
        self.assertEqual(self.core_two(term.load_prefs()), {"font_size": 14, "scrollback": 50000})
        with open(term.prefs_path(), "w") as f:
            f.write("{nope")
        self.assertEqual(term.load_prefs()["font_size"], 11)


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
        cls.root.geometry("900x500")
        cls.app = FakeApp(cls.root)
        term.TerminalTab.AUTO_CONNECT = False

    @classmethod
    def tearDownClass(cls):
        term.TerminalTab.AUTO_CONNECT = True
        cls.root.destroy()

    def setUp(self):
        self._old = core.APP_DIR
        core.APP_DIR = tempfile.mkdtemp(prefix="rc_tab_")
        self.tab = term.TerminalTab(self.app, {"address": "x.lan", "name": "x"}, "u", "p")
        self.app.nb.add(self.tab, text="t")
        self.root.update()
        self.tab.apply_size()

    def tearDown(self):
        self.tab.close()
        core.APP_DIR = self._old

    def text(self):
        self.tab.render()
        return self.tab.t.get("1.0", "end-1c")

    def test_output_and_scrollback_land_in_the_widget(self):
        self.tab.feed("\r\n".join(f"line{i}" for i in range(self.tab.rows + 20)))
        body = self.text()
        self.assertIn("line0", body)                       # scrolled off the screen but still reachable
        self.assertIn(f"line{self.tab.rows + 19}", body)
        self.assertEqual(int(self.tab.t.index("end-1c").split(".")[0]), self.tab.sb + self.tab.rows)

    def test_full_screen_program_leaves_no_residue(self):
        self.tab.feed("$ ls\r\nfile\r\n$ ")
        before = self.text()
        self.tab.feed("\x1b[?1049h\x1b[2J\x1b[HVIM TEXT\x1b[?1049l")
        after = self.text()
        self.assertNotIn("VIM TEXT", after)
        self.assertEqual(after, before)

    def test_scrollback_is_capped(self):
        self.tab.set_scrollback(500)
        self.tab.feed("\r\n".join(str(i) for i in range(1500)))
        self.text()
        self.assertLessEqual(self.tab.sb, 500)
        self.assertEqual(int(self.tab.t.index("end-1c").split(".")[0]), self.tab.sb + self.tab.rows)

    def test_font_change_is_clamped_and_saved(self):
        for _ in range(60):
            self.tab.change_font(+1)
        self.assertEqual(self.tab.font.cget("size"), term.FONT_MAX)
        self.tab.change_font(0)
        self.assertEqual(self.tab.font.cget("size"), term.FONT_DEFAULT)
        self.assertEqual(term.load_prefs()["font_size"], term.FONT_DEFAULT)

    def test_resize_keeps_widget_and_screen_in_step(self):
        self.root.geometry("600x300")
        self.root.update()
        self.tab.apply_size()
        self.tab.feed("hello")
        self.text()
        self.assertEqual(self.tab.screen.lines, self.tab.rows)
        self.assertEqual(int(self.tab.t.index("end-1c").split(".")[0]), self.tab.sb + self.tab.rows)

    def test_keys_follow_application_cursor_mode(self):
        sent = []
        self.tab.send = sent.append

        class E:
            state, char, keysym = 0, "", "Up"
        self.tab.on_key(E)
        self.tab.feed("\x1b[?1h")
        self.tab.on_key(E)
        self.assertEqual(sent, ["\x1b[A", "\x1bOA"])

    def test_alt_and_function_keys(self):
        sent = []
        self.tab.send = sent.append

        class F5:
            state, char, keysym = 0, "", "F5"

        class AltF:
            state, char, keysym = 0x20000, "f", "f"

        class AltGrAt:
            state, char, keysym = 0x20004, "@", "at"
        for ev in (F5, AltF, AltGrAt):
            self.tab.on_key(ev)
        self.assertEqual(sent, ["\x1b[15~", "\x1bf", "@"])

    def test_paste_is_bracketed_when_requested(self):
        sent = []
        self.tab.send = sent.append
        self.tab.clipboard_clear()
        self.tab.clipboard_append("a\nb")
        with mock.patch.object(term.messagebox, "askyesno", return_value=True) as ask:  # never open a real dialog in tests
            self.tab.paste()
            self.tab.feed("\x1b[?2004h")
            self.tab.paste()
        self.assertEqual(ask.call_count, 2)  # several lines: RC asks before pasting
        self.assertEqual(sent, ["a\rb", "\x1b[200~a\rb\x1b[201~"])

    def test_clear(self):
        self.tab.feed("\r\n".join(str(i) for i in range(100)))
        self.text()
        self.tab.send = lambda s: None
        self.tab.clear()
        self.assertEqual(self.tab.sb, 0)
        self.assertEqual(self.text().strip(), "")


class ReconnectRuleTests(TabTests):
    """Uses the same Tk fixture."""

    def test_clean_exit_does_not_reconnect(self):
        gen = self.tab.generation
        self.tab.session_ended(gen, clean=True)
        self.assertIsNone(self.tab.reconnect_job)
        self.assertIn("ended", self.tab.status_var.get())

    def test_drop_after_connecting_schedules_retry_with_backoff(self):
        self.tab.ever_connected = True
        gen = self.tab.generation
        self.tab.session_ended(gen, clean=False)
        self.assertIsNotNone(self.tab.reconnect_job)
        self.assertEqual(self.tab.attempt, 1)
        self.assertIn("1/", self.tab.status_var.get())
        self.tab.after_cancel(self.tab.reconnect_job)
        self.tab.reconnect_job = None

    def test_first_connection_failure_is_not_retried(self):
        self.tab.connect_failed(self.tab.generation, "timed out", fatal=False)
        self.assertIsNone(self.tab.reconnect_job)
        self.assertIn("Not connected", self.tab.status_var.get())

    def test_wrong_password_after_reconnect_is_not_retried(self):
        self.tab.ever_connected = True
        self.tab.connect_failed(self.tab.generation, "Authentication failed", fatal=True)
        self.assertIsNone(self.tab.reconnect_job)

    def test_gives_up_after_the_last_attempt(self):
        self.tab.ever_connected = True
        self.tab.attempt = len(term.RECONNECT_DELAYS)
        self.tab.schedule_reconnect()
        self.assertIsNone(self.tab.reconnect_job)
        self.assertIn("Disconnected", self.tab.status_var.get())

    def test_stale_generation_is_ignored(self):
        old = self.tab.generation
        self.tab.generation += 1
        self.tab.session_ended(old, clean=False)
        self.assertIsNone(self.tab.reconnect_job)


if __name__ == "__main__":
    unittest.main()
