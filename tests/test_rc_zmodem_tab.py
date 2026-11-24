"""Tests for Zmodem inside a terminal tab: the reader loop spots a transfer in the output, the tab takes over a fake SSH channel
for the transfer (both directions), asks where to save / what to send, and hands the connection back. Also the Zmodem menu
items and "Clear screen (keep scrollback)".  Run from the RC folder:  python -m unittest discover -s tests -v"""

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

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_zmtab_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_manager as core  # noqa: E402
import rc_terminal as term  # noqa: E402
import rc_zmodem as zm  # noqa: E402
from test_rc_zmodem import RecordingUi, make_files, same  # noqa: E402


class FakeChannel:
    """The terminal's end of an SSH channel: recv / sendall / settimeout, like paramiko's Channel."""

    def __init__(self, inbox, outbox):
        self.inbox, self.outbox, self.timeout, self.closed = inbox, outbox, None, False

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        try:
            return self.inbox.get(timeout=self.timeout if self.timeout is not None else 30)
        except queue.Empty:
            raise socket.timeout()

    def sendall(self, data):
        self.outbox.put(bytes(data))

    def send(self, data):
        self.outbox.put(bytes(data))

    def exit_status_ready(self):
        return False

    def close(self):
        self.closed = True


class FakeApp:
    all_terminals = core.App.all_terminals
    cur_tab = core.App.cur_tab
    broadcast_peers = core.App.broadcast_peers
    broadcast_changed = core.App.broadcast_changed
    refresh_tab_title = core.App.refresh_tab_title
    tab_title_base = core.App.tab_title_base

    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.status = tk.StringVar()
        self.q = queue.Queue()

    def post(self, fn):
        self.q.put(fn)

    def pump(self, until, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.q.get(timeout=0.03)()
            except queue.Empty:
                pass
            if until():
                return True
        return until()

    def confirm_broadcast(self):
        return True


class Base(unittest.TestCase):
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
        self._old = core.APP_DIR
        core.APP_DIR = tempfile.mkdtemp(prefix="rc_zmtab_")
        self.app = FakeApp(self.root)
        self.tab = term.TerminalTab(self.app, {"name": "box", "address": "10.0.0.5"}, "u", "p")
        self.app.nb.add(self.tab, text="t")
        self.root.update()
        self.tab.apply_size()
        self.sent = []
        self.tab.send = self.sent.append
        self.server_to_tab, self.tab_to_server = queue.Queue(), queue.Queue()
        self.chan = FakeChannel(self.server_to_tab, self.tab_to_server)

    def tearDown(self):
        self.tab.close()
        self.app.nb.destroy()
        core.APP_DIR = self._old

    def server_read(self, n, timeout):
        try:
            return self.tab_to_server.get(timeout=timeout)
        except queue.Empty:
            return None

    def server_write(self, data):
        self.server_to_tab.put(bytes(data))

    def run_tab_transfer(self, first, server_job):
        """Start run_zmodem on a thread (as the reader thread would) with the 'server' running in another; pump the UI until done."""
        errors = {}
        tab_thread = threading.Thread(target=lambda: self.tab.run_zmodem(self.chan, first), daemon=True)
        srv_thread = threading.Thread(target=lambda: self._guard(errors, server_job), daemon=True)
        tab_thread.start()
        srv_thread.start()
        self.assertTrue(self.app.pump(lambda: not tab_thread.is_alive() and not srv_thread.is_alive(), 30))
        self.app.pump(lambda: False, 0.2)
        return errors

    @staticmethod
    def _guard(errors, fn):
        try:
            fn()
        except Exception as e:
            errors["server"] = e

    def screen(self):
        self.tab.render()
        return self.tab.t.get("1.0", "end")


class DownloadTests(Base):
    def test_the_server_runs_sz_and_the_file_lands_in_the_chosen_folder(self):
        src = make_files([("report.bin", 90000)])[0]
        dest = tempfile.mkdtemp(prefix="rc_zmdest_")
        first = zm.hex_header(zm.ZRQINIT, bytes(4))

        def server():                                                         # what `sz report.bin` does after its first header
            self.server_write(first)
            zm.Sender(self.server_read, self.server_write).send([src], first=None)

        with mock.patch("tkinter.filedialog.askdirectory", return_value=dest) as ask:
            # the server's ZRQINIT was consumed as `first`; the Sender then waits for the tab's ZRINIT
            errors = self.run_tab_transfer(first, lambda: zm.Sender(self.server_read, self.server_write).send([src]))
        self.assertEqual(errors, {})
        ask.assert_called_once()
        self.assertTrue(same(src, os.path.join(dest, "report.bin")))
        self.assertIn("Zmodem: received report.bin", self.screen())
        self.assertEqual(self.tab.prefs["zmodem_dir"], dest)                  # remembered
        self.assertFalse(self.tab.zm_cancel_btn.winfo_manager())              # the Cancel button is gone again

    def test_the_remembered_folder_is_not_asked_again_and_existing_files_are_kept(self):
        dest = tempfile.mkdtemp(prefix="rc_zmdest_")
        self.tab.set_pref(zmodem_dir=dest)
        with open(os.path.join(dest, "same.txt"), "wb") as f:
            f.write(b"precious")
        src = make_files([("same.txt", 500)])[0]
        first = zm.hex_header(zm.ZRQINIT, bytes(4))
        with mock.patch("tkinter.filedialog.askdirectory", side_effect=AssertionError("should not ask")):
            errors = self.run_tab_transfer(first, lambda: zm.Sender(self.server_read, self.server_write).send([src]))
        self.assertEqual(errors, {})
        with open(os.path.join(dest, "same.txt"), "rb") as f:
            self.assertEqual(f.read(), b"precious")                           # never overwritten
        self.assertTrue(same(src, os.path.join(dest, "same (copy).txt")))

    def test_no_folder_chosen_cancels_politely(self):
        first = zm.hex_header(zm.ZRQINIT, bytes(4))
        src = make_files([("x.bin", 100)])[0]
        result = {}

        def server():
            self.server_write(first)
            try:
                zm.Sender(self.server_read, self.server_write).send([src])
            except zm.ZError as e:
                result["error"] = e
        with mock.patch("tkinter.filedialog.askdirectory", return_value=""):
            errors = self.run_tab_transfer(first, server)
        self.assertIn("cancelled", self.screen())
        self.assertIsInstance(result.get("error"), zm.ZError)                 # the server saw the abort

    def test_a_cancel_click_mid_transfer_stops_it(self):
        dest = tempfile.mkdtemp(prefix="rc_zmdest_")
        self.tab.set_pref(zmodem_dir=dest)
        src = make_files([("big.bin", 190000)])[0]
        first = zm.hex_header(zm.ZRQINIT, bytes(4))
        seen = []
        orig = term._ZmodemUi.progress

        def progress(ui, name, done, total, sending):
            seen.append(done)
            if len(seen) == 15:
                self.tab.zmodem_cancel()                                      # the user clicks the button
            return orig(ui, name, done, total, sending)
        with mock.patch.object(term._ZmodemUi, "progress", progress):
            result = {}

            def server():
                try:
                    zm.Sender(self.server_read, self.server_write).send([src])
                except zm.ZError as e:
                    result["error"] = e
            self.run_tab_transfer(first, server)
        self.assertIn("Zmodem stopped", self.screen())
        self.assertLess(max(seen), 190000)
        self.assertIn("error", result)


class UploadTests(Base):
    def test_the_server_runs_rz_and_the_chosen_files_are_sent(self):
        files = make_files([("up1.bin", 70000), ("up2.txt", 12)])
        received = {}
        dest = tempfile.mkdtemp(prefix="rc_zmsrv_")
        first = zm.hex_header(zm.ZRINIT, bytes([0, 0, 0, 0x23]))

        def server():                                                         # `rz` on the server: it sent its ZRINIT (= first) and receives
            received["saved"] = zm.Receiver(self.server_read, self.server_write, RecordingUi(dest)).receive()
        with mock.patch("tkinter.filedialog.askopenfilenames", return_value=tuple(files)) as ask:
            errors = self.run_tab_transfer(first, server)
        self.assertEqual(errors, {})
        ask.assert_called_once()
        self.assertEqual([n for n, _, _ in received["saved"]], ["up1.bin", "up2.txt"])
        for p in files:
            self.assertTrue(same(p, os.path.join(dest, os.path.basename(p))))
        self.assertIn("Zmodem: sent 2 file(s)", self.screen())

    def test_choosing_no_files_cancels_the_upload(self):
        first = zm.hex_header(zm.ZRINIT, bytes([0, 0, 0, 0x23]))
        out = {}

        def server():
            try:
                zm.Receiver(self.server_read, self.server_write).receive()
            except zm.ZError as e:
                out["error"] = e
        with mock.patch("tkinter.filedialog.askopenfilenames", return_value=()):
            self.run_tab_transfer(first, server)
        self.assertIn("upload cancelled", self.screen())

    def test_the_upload_menu_item_types_rz_in_a_connected_ssh_terminal(self):
        self.tab.chan = self.chan
        self.tab.zmodem_upload()
        self.assertEqual(self.sent, ["rz -be\r"])
        self.tab.chan = None
        with mock.patch.object(term.messagebox, "showinfo") as info:
            self.tab.zmodem_upload()
        info.assert_called_once()
        self.assertEqual(len(self.sent), 1)


class ReaderLoopTests(Base):
    """The connect loop: output passes through, a Zmodem start is handed to run_zmodem, ordinary stars are left alone."""

    def run_loop(self, chunks, kind="ssh", zmodem=True):
        self.tab.prefs["zmodem"] = zmodem
        self.tab.kind = kind
        chan = FakeChannel(queue.Queue(), queue.Queue())
        for c in chunks:
            chan.inbox.put(c)
        chan.inbox.put(b"")
        client = mock.Mock()
        client.get_transport.return_value = None
        calls = []
        with mock.patch.object(core, "open_ssh_client", return_value=client), \
                mock.patch.object(self.tab, "run_zmodem", side_effect=lambda c, first: calls.append(first)):
            client.invoke_shell.return_value = chan
            t = threading.Thread(target=lambda: self.tab.connect(self.tab.generation, "u", "p", 80, 24), daemon=True)
            t.start()
            self.app.pump(lambda: not t.is_alive(), 10)
        self.app.pump(lambda: False, 0.2)
        return calls

    def test_plain_output_and_a_zmodem_start(self):
        calls = self.run_loop([b"hello ** world\r\n", b"rz waiting to receive.**\x18B0100000023be50\r\x8a\x11"])
        self.assertEqual(calls, [b"**\x18B0100000023be50\r\x8a\x11"])
        screen = self.screen()
        self.assertIn("hello ** world", screen)
        self.assertIn("rz waiting to receive.", screen)

    def test_a_signature_split_across_reads_is_still_found(self):
        calls = self.run_loop([b"x**\x18B", b"00000000000000\r\x8a"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith(b"**\x18B00"))

    def test_turned_off_or_not_ssh_means_no_interception(self):
        self.assertEqual(self.run_loop([b"**\x18B0100000023be50\r\x8a\x11"], zmodem=False), [])
        self.assertEqual(self.run_loop([b"**\x18B0100000023be50\r\x8a\x11"], kind="telnet"), [])


class MenuTests(Base):
    def test_zmodem_option_is_saved(self):
        self.assertTrue(self.tab.prefs["zmodem"])
        self.tab.opt_vars["zmodem"].set(False)
        self.tab.set_option("zmodem")
        self.assertFalse(term.load_prefs()["zmodem"])

    def test_pick_dir_remembers_and_validates(self):
        dest = tempfile.mkdtemp(prefix="rc_zmdir_")
        with mock.patch("tkinter.filedialog.askdirectory", return_value=dest):
            self.assertEqual(self.tab.zmodem_pick_dir(), dest)
        self.assertEqual(term.load_prefs()["zmodem_dir"], dest)
        with mock.patch("tkinter.filedialog.askdirectory", side_effect=AssertionError("asked again")):
            self.assertEqual(self.tab.zmodem_pick_dir(ask=False), dest)
        os.rmdir(dest)                                                         # the remembered folder disappeared: ask again
        new = tempfile.mkdtemp(prefix="rc_zmdir2_")
        with mock.patch("tkinter.filedialog.askdirectory", return_value=new):
            self.assertEqual(self.tab.zmodem_pick_dir(ask=False), new)
        with mock.patch("tkinter.filedialog.askdirectory", return_value=""):
            self.assertEqual(self.tab.zmodem_pick_dir(), "")                    # cancelled: nothing changes
        self.assertEqual(term.load_prefs()["zmodem_dir"], new)

    def test_clear_keeping_scrollback(self):
        self.tab.feed("\r\n".join(f"line{i}" for i in range(5)) + "\r\n$ ")
        self.tab.clear_keep_history()
        self.assertEqual(self.sent[-1], "\x0c")                                 # the shell redraws its prompt
        text = self.screen()
        for i in range(5):
            self.assertIn(f"line{i}", text)                                     # still in the scrollback
        visible = self.tab.screen.display
        self.assertTrue(all(not row.strip() for row in visible))                # the screen itself is blank
        self.assertGreater(self.tab.sb, 0)


if __name__ == "__main__":
    unittest.main()
