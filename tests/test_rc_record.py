"""Tests for rc_record.py: writing a recording, reading one (including damaged ones), the playback arithmetic, the terminal's
Record button and the player tab. Run from the RC folder:  python -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_record_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as core  # noqa: E402
import rc_record as rec  # noqa: E402
import rc_terminal as term  # noqa: E402

CAST = "\n".join([
    json.dumps({"version": 2, "width": 80, "height": 24, "timestamp": 1700000000, "title": "demo"}),
    json.dumps([0.1, "o", "hello\r\n"]),
    json.dumps([0.5, "o", "\u001b[31mred\u001b[0m\r\n"]),
    json.dumps([30.0, "o", "after a long pause\r\n"]),
    json.dumps([30.2, "r", "100x30"]),
    json.dumps([30.5, "o", "end\r\n"]),
]) + "\n"


class DataDir(unittest.TestCase):
    def setUp(self):
        self._old = core.APP_DIR
        core.APP_DIR = tempfile.mkdtemp(prefix="rc_recdata_")

    def tearDown(self):
        core.APP_DIR = self._old


class FormatTests(DataDir):
    def test_recorder_writes_a_valid_asciicast(self):
        r = rec.Recorder.for_session("web 01/prod", 100, 30, "ssh web")
        r.output("hello\r\n")
        time.sleep(0.05)
        r.output("")                                                  # empty chunks are not events
        r.resize(120, 40)
        r.output("bye")
        path = r.close()
        self.assertTrue(path.startswith(rec.recordings_dir()))
        self.assertRegex(os.path.basename(path), r"^web_01_prod_\d{8}_\d{6}\.cast$")
        with open(path, encoding="utf-8") as f:
            header, events = rec.parse_cast(f.read())
        self.assertEqual((header["version"], header["width"], header["height"], header["title"]), (2, 100, 30, "ssh web"))
        self.assertEqual([(k, d) for _, k, d in events], [("o", "hello\r\n"), ("r", "120x40"), ("o", "bye")])
        self.assertTrue(events[0][0] < events[2][0])

    def test_close_is_idempotent_and_writing_after_close_is_ignored(self):
        r = rec.Recorder.for_session("x", 80, 24)
        r.output("a")
        r.close()
        r.output("ignored")
        r.close()
        with open(r.path, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 2)

    def test_two_recordings_in_one_second_get_different_files(self):
        with mock.patch.object(rec.time, "strftime", return_value="20260101_000000"):
            a = rec.Recorder.for_session("x", 80, 24)
            b = rec.Recorder.for_session("x", 80, 24)
        a.close()
        b.close()
        self.assertNotEqual(a.path, b.path)

    def test_unicode_and_control_characters_survive(self):
        r = rec.Recorder.for_session("u", 80, 24)
        text = "héllo ✓ \u001b[1m bold \x00 nul  "
        r.output(text)
        r.close()
        with open(r.path, encoding="utf-8") as f:
            _, events = rec.parse_cast(f.read())
        self.assertEqual(events[0][2], text)


class ParseTests(unittest.TestCase):
    def test_reads_events_in_time_order(self):
        header, events = rec.parse_cast(CAST)
        self.assertEqual((header["width"], header["height"]), (80, 24))
        self.assertEqual([e[1] for e in events], ["o", "o", "o", "r", "o"])
        shuffled = CAST.splitlines()
        shuffled[1], shuffled[3] = shuffled[3], shuffled[1]
        self.assertEqual(rec.parse_cast("\n".join(shuffled))[1][0][0], 0.1)

    def test_windows_line_endings(self):
        self.assertEqual(len(rec.parse_cast(CAST.replace("\n", "\r\n"))[1]), 5)

    def test_bad_lines_are_skipped(self):
        text = CAST + "not json\n[1]\n[\"x\", \"o\", \"d\"]\n[1, 2, 3]\n[-1, \"o\", \"neg\"]\n{\"a\":1}\n"
        self.assertEqual(len(rec.parse_cast(text)[1]), 5)

    def test_bad_headers(self):
        for text, msg in (("", "empty"), ("hello", "not a terminal recording"), ('{"version": 1, "width": 80, "height": 24}', "version 1"),
                          ("[1,2]", "Only asciicast version 2"), ('{"version": 2}', "how big"), ('{"version": 2, "width": "x", "height": 3}', "how big")):
            with self.assertRaisesRegex(rec.CastError, msg):
                rec.parse_cast(text)

    def test_absurd_screen_sizes_are_clamped(self):
        h, _ = rec.parse_cast(json.dumps({"version": 2, "width": 99999, "height": 1}) + "\n")
        self.assertEqual((h["width"], h["height"]), (rec.MAX_COLS, 3))


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.tl = rec.Timeline(rec.parse_cast(CAST)[1])

    def test_delays(self):
        self.assertAlmostEqual(self.tl.delay_after(0), 0.4)
        self.assertAlmostEqual(self.tl.delay_after(0, speed=2), 0.2)
        self.assertEqual(self.tl.delay_after(1), rec.MAX_GAP)                    # the 29.5 s pause is cut to 2 s
        self.tl.skip_pauses = False
        self.assertAlmostEqual(self.tl.delay_after(1), 29.5)
        self.assertEqual(self.tl.delay_after(4), 0.0)                             # nothing after the last event
        self.assertEqual(self.tl.delay_after(0, speed=0), self.tl.delay_after(0, speed=0.01))   # a zero speed cannot divide by zero

    def test_position_lookup(self):
        self.assertEqual(self.tl.index_at(0), 0)
        self.assertEqual(self.tl.index_at(0.1), 1)
        self.assertEqual(self.tl.index_at(0.4), 1)
        self.assertEqual(self.tl.index_at(0.5), 2)
        self.assertEqual(self.tl.index_at(999), 5)
        self.assertEqual(self.tl.duration, 30.5)
        self.assertEqual(rec.Timeline([]).duration, 0.0)

    def test_time_format(self):
        self.assertEqual((rec.fmt_time(0), rec.fmt_time(65), rec.fmt_time(3599.9), rec.fmt_time(-5)), ("0:00", "1:05", "59:59", "0:00"))


class FakeApp:
    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.status = tk.StringVar()
        self.root = None

    def post(self, fn):
        fn()

    def add_tab(self, w, title):
        self.nb.add(w, text=title)
        self.nb.select(w)


class Base(DataDir):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1000x650")
        term.TerminalTab.AUTO_CONNECT = False

    @classmethod
    def tearDownClass(cls):
        term.TerminalTab.AUTO_CONNECT = True
        cls.root.destroy()

    def setUp(self):
        super().setUp()
        self.app = FakeApp(self.root)

    def tearDown(self):
        for t in list(self.app.nb.tabs()):
            w = self.app.nb.nametowidget(t)
            if hasattr(w, "close"):
                w.close()
        self.app.nb.destroy()
        super().tearDown()


class TerminalRecordTests(Base):
    def tab(self):
        t = term.TerminalTab(self.app, {"name": "box", "address": "10.0.0.5"}, "u", "p")
        self.app.nb.add(t, text="t")
        self.root.update()
        t.apply_size()
        return t

    def test_record_button_saves_remote_output_only(self):
        t = self.tab()
        t.rec_var.set(True)
        t.toggle_record()
        self.assertIsNotNone(t.recorder)
        self.assertIn("Recording to", self.app.status.get())
        t.remote_data("from the server\r\n")
        t.write_local("[local note]\r\n")                                        # RC's own messages are not recorded
        t.send_user("x")                                                         # neither are keystrokes
        t.rec_var.set(False)
        t.toggle_record()
        self.assertIsNone(t.recorder)
        files = os.listdir(rec.recordings_dir())
        self.assertEqual(len(files), 1)
        with open(os.path.join(rec.recordings_dir(), files[0]), encoding="utf-8") as f:
            header, events = rec.parse_cast(f.read())
        self.assertEqual([d for _, k, d in events], ["from the server\r\n"])
        self.assertEqual((header["width"], header["height"]), (t.cols, t.rows))
        self.assertIn("Recording saved", self.app.status.get())

    def test_resizing_while_recording_is_recorded_and_closing_the_tab_finishes_the_file(self):
        t = self.tab()
        t.rec_var.set(True)
        t.toggle_record()
        t.remote_data("a")
        t.fixed_size = (91, 27)
        t.apply_size()
        path = t.recorder.path
        t.close()
        self.assertIsNone(t.recorder)
        with open(path, encoding="utf-8") as f:
            _, events = rec.parse_cast(f.read())
        self.assertIn(("r", "91x27"), [(k, d) for _, k, d in events])

    def test_a_failing_folder_is_reported(self):
        t = self.tab()
        t.rec_var.set(True)
        with mock.patch.object(rec.Recorder, "for_session", side_effect=OSError("disk full")), \
                mock.patch.object(term.messagebox, "showerror") as err:
            t.toggle_record()
        self.assertFalse(t.rec_var.get())
        self.assertIsNone(t.recorder)
        err.assert_called_once()


class PlayerTests(Base):
    def player(self, text=CAST):
        header, events = rec.parse_cast(text)
        p = rec.PlayerTab(self.app, "demo.cast", header, events)
        self.app.add_tab(p, "player")
        self.root.update()
        return p

    def screen_text(self, p):
        p.screen.render()
        return p.screen.t.get("1.0", "end")

    def run_until(self, cond, seconds=8):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_screen_is_the_recorded_size_and_has_no_toolbar(self):
        p = self.player()
        self.assertEqual((p.screen.cols, p.screen.rows), (80, 24))
        self.assertFalse(p.screen.winfo_children()[0].winfo_manager())          # the live-terminal toolbar is not shown
        self.assertNotIn("Connecting", self.screen_text(p))
        self.assertIsNone(p.screen.chan)

    def test_plays_to_the_end_skipping_the_long_pause(self):
        p = self.player()
        p.speed.set("8x")
        started = time.time()
        p.play()
        self.assertTrue(self.run_until(lambda: not p.playing))
        self.assertLess(time.time() - started, 5)                                # 30 s of recording, with the pause cut
        text = self.screen_text(p)
        for word in ("hello", "red", "after a long pause", "end"):
            self.assertIn(word, text)
        self.assertEqual((p.screen.cols, p.screen.rows), (100, 30))              # the recorded resize was followed
        self.assertEqual(p.play_btn.cget("text"), "▶ Replay")
        self.assertIn("0:30 / 0:30", p.pos_var.get())

    def test_pause_and_resume(self):
        p = self.player()
        p.play()
        self.assertEqual(p.play_btn.cget("text"), "⏸ Pause")
        self.run_until(lambda: p.i >= 1, 2)
        p.pause()
        stopped_at = p.i
        end = time.time() + 0.6
        while time.time() < end:
            self.root.update()
        self.assertEqual(p.i, stopped_at)
        self.assertFalse(p.playing)

    def test_seek_rebuilds_the_screen_at_that_moment(self):
        p = self.player()
        p.seek(0.6)
        text = self.screen_text(p)
        self.assertIn("hello", text)
        self.assertIn("red", text)
        self.assertNotIn("after a long pause", text)
        self.assertEqual(p.i, 2)
        p.seek(31)
        self.assertIn("end", self.screen_text(p))
        self.assertEqual((p.screen.cols, p.screen.rows), (100, 30))
        p.seek(0)
        self.assertNotIn("hello", self.screen_text(p))                           # back to a clean screen
        self.assertEqual((p.i, p.screen.cols), (0, 80))

    def test_restart_after_the_end(self):
        p = self.player()
        p.speed.set("8x")
        p.seek(100)
        p.finished()
        p.toggle()                                                                # at the end, Play means start again
        self.assertTrue(p.playing or p.i > 0)
        self.assertTrue(self.run_until(lambda: not p.playing))
        self.assertIn("end", self.screen_text(p))

    def test_speed_option_and_pause_skipping_switch(self):
        p = self.player()
        p.speed.set("4x")
        self.assertEqual(p.speed_value(), 4.0)
        p.speed.set("garbage")
        self.assertEqual(p.speed_value(), 1.0)
        p.skip.set(False)
        p.options_changed()
        self.assertFalse(p.timeline.skip_pauses)

    def test_closing_the_player_stops_the_timer(self):
        p = self.player()
        p.play()
        p.close()
        self.assertFalse(p.playing)
        self.assertIsNone(p.job)


class PlayFileTests(Base):
    def test_opening_files(self):
        path = os.path.join(core.APP_DIR, "t.cast")
        with open(path, "w", encoding="utf-8") as f:
            f.write(CAST)
        self.app.root = mock.Mock()
        tab = rec.play_file(self.app, path)
        self.assertIsInstance(tab, rec.PlayerTab)
        self.assertIn("t.cast", self.app.nb.tab(tab, "text"))
        for name, text, kind in (("empty.cast", "", "showerror"), ("v1.cast", '{"version":1,"width":80,"height":24}\n', "showerror"),
                                 ("noev.cast", json.dumps({"version": 2, "width": 80, "height": 24}) + "\n", "showinfo")):
            p = os.path.join(core.APP_DIR, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            with mock.patch.object(rec.messagebox, kind) as box:
                self.assertIsNone(rec.play_file(self.app, p), name)
            box.assert_called_once()
        with mock.patch.object(rec.messagebox, "showerror") as box:
            self.assertIsNone(rec.play_file(self.app, os.path.join(core.APP_DIR, "missing.cast")))
        box.assert_called_once()
        with mock.patch.object(rec.filedialog, "askopenfilename", return_value=""):
            self.assertIsNone(rec.play_file(self.app))


if __name__ == "__main__":
    unittest.main()
