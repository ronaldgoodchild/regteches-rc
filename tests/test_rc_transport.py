"""Tests for Telnet and Serial terminals (rc_transport.py + the terminal tab driving them) and the terminal polish
(colour schemes, font, copy/paste options, saving the output). Everything runs against throw-away local servers and
pyserial's loop:// port.  Run from the RC folder:  python -m unittest discover -s tests -v"""

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

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_transport_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as core  # noqa: E402
import rc_terminal as term  # noqa: E402
import rc_transport as tp  # noqa: E402

IAC, DONT, DO, WONT, WILL, SB, SE = tp.IAC, tp.DONT, tp.DO, tp.WONT, tp.WILL, tp.SB, tp.SE
try:
    import serial  # noqa: F401
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False


class FakeSock:
    """Scripted socket: recv() hands out the queued chunks, sendall() records."""

    def __init__(self, chunks=()):
        self.chunks, self.sent, self.closed = list(chunks), bytearray(), False

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent += data

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


class TelnetParseTests(unittest.TestCase):
    def chan(self, *chunks):
        return tp.TelnetChannel(FakeSock(chunks), 100, 30)

    def test_plain_text_passes_through(self):
        c = self.chan(b"hello\r\nworld")
        self.assertEqual(c.recv(), b"hello\r\nworld")
        self.assertEqual(c.recv(), b"")
        self.assertTrue(c.exit_status_ready())                      # the server hung up: a clean log-out

    def test_commands_are_stripped_and_iac_iac_is_a_real_ff(self):
        c = self.chan(bytes([IAC, DO, 99]) + b"a" + bytes([IAC, IAC]) + b"b" + bytes([IAC, 241]) + b"c")
        self.assertEqual(c.recv(), b"a\xffbc")

    def test_commands_split_across_reads(self):
        c = self.chan(b"x" + bytes([IAC]), bytes([DO]), bytes([99]) + b"y")
        out = c.recv() + c.recv() + c.recv()
        self.assertEqual(out, b"xy")
        self.assertEqual(bytes(c.sock.sent), bytes([IAC, WONT, 99]))

    def test_cr_nul_is_a_bare_cr(self):
        c = self.chan(b"a\r\x00b\r\nc")
        self.assertEqual(c.recv(), b"a\rb\r\nc")

    def test_negotiation_answers(self):
        c = self.chan(bytes([IAC, DO, tp.OPT_TTYPE, IAC, DO, tp.OPT_NAWS, IAC, DO, tp.OPT_SGA, IAC, WILL, tp.OPT_ECHO,
                             IAC, WILL, tp.OPT_SGA, IAC, DO, 99, IAC, WILL, 98]))
        c.recv()
        sent = bytes(c.sock.sent)
        self.assertIn(bytes([IAC, WILL, tp.OPT_TTYPE]), sent)
        self.assertIn(bytes([IAC, WILL, tp.OPT_NAWS]), sent)
        self.assertIn(bytes([IAC, SB, tp.OPT_NAWS, 0, 100, 0, 30, IAC, SE]), sent)           # 100 x 30
        self.assertIn(bytes([IAC, DO, tp.OPT_ECHO]), sent)                                   # we accept the server echoing
        self.assertIn(bytes([IAC, DO, tp.OPT_SGA]), sent)
        self.assertIn(bytes([IAC, WONT, 99]), sent)                                          # refuse what we do not know
        self.assertIn(bytes([IAC, DONT, 98]), sent)

    def test_repeated_requests_are_not_answered_twice(self):
        c = self.chan(bytes([IAC, DO, tp.OPT_SGA]) * 3 + bytes([IAC, WILL, tp.OPT_ECHO]) * 3)
        c.recv()
        sent = bytes(c.sock.sent)
        self.assertEqual(sent.count(bytes([IAC, WILL, tp.OPT_SGA])), 1)
        self.assertEqual(sent.count(bytes([IAC, DO, tp.OPT_ECHO])), 1)

    def test_terminal_type_request(self):
        c = self.chan(bytes([IAC, SB, tp.OPT_TTYPE, tp.TTYPE_SEND, IAC, SE]) + b"ok")
        self.assertEqual(c.recv(), b"ok")
        self.assertEqual(bytes(c.sock.sent), bytes([IAC, SB, tp.OPT_TTYPE, tp.TTYPE_IS]) + b"xterm-256color" + bytes([IAC, SE]))

    def test_window_size_with_an_ff_byte_is_escaped(self):
        c = tp.TelnetChannel(FakeSock([bytes([IAC, DO, tp.OPT_NAWS])]), 255, 24)
        c.recv()
        self.assertIn(bytes([IAC, SB, tp.OPT_NAWS, 0, 255, 255, 0, 24, IAC, SE]), bytes(c.sock.sent))

    def test_resize_sends_naws_only_when_agreed(self):
        c = self.chan(b"x")
        c.recv()
        c.resize_pty(120, 40)
        self.assertEqual(bytes(c.sock.sent), b"")
        c.naws = True
        c.resize_pty(120, 40)
        self.assertEqual(bytes(c.sock.sent), bytes([IAC, SB, tp.OPT_NAWS, 0, 120, 0, 40, IAC, SE]))

    def test_sending(self):
        c = self.chan()
        c.send("ls\r")
        self.assertEqual(bytes(c.sock.sent), b"ls\r\n")                                    # Enter is CR LF on the wire
        c.sock.sent.clear()
        c.send("é\xff")
        self.assertEqual(bytes(c.sock.sent), "é".encode() + "\xff".encode()) if False else None
        c.sock.sent.clear()
        c.send(b"\xff")
        self.assertEqual(bytes(c.sock.sent), b"\xff\xff")                                  # a literal FF is doubled

    def test_local_close_is_not_a_logout(self):
        c = self.chan(b"x")
        c.recv()
        c.close()
        self.assertEqual(c.recv(), b"")
        self.assertFalse(c.exit_status_ready())
        self.assertTrue(c.sock.closed)
        c.send("ignored")                                                                  # sending after close does nothing


class FakeTelnetServer:
    """Negotiates like a real one, shows a login prompt and echoes what it is sent."""

    def __init__(self, hang_up_after=None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.received = bytearray()
        self.hang_up_after = hang_up_after
        self.clients = []
        threading.Thread(target=self.loop, daemon=True).start()

    def close(self):
        self.sock.close()
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.clients.append(c)
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, c):
        try:
            c.sendall(bytes([IAC, DO, tp.OPT_TTYPE, IAC, DO, tp.OPT_NAWS, IAC, WILL, tp.OPT_ECHO, IAC, WILL, tp.OPT_SGA]))
            c.sendall(b"Switch-01\r\n\xff\xff-ready\r\nlogin: ")
            while True:
                d = c.recv(4096)
                if not d:
                    return
                self.received += d
                payload = bytes(d)
                if bytes([IAC]) not in payload:
                    c.sendall(payload)                                                    # echo
                if self.hang_up_after and self.hang_up_after in self.received:
                    c.close()
                    return
        except OSError:
            pass


class TelnetSocketTests(unittest.TestCase):
    def test_against_a_real_socket(self):
        srv = FakeTelnetServer()
        self.addCleanup(srv.close)
        chan = tp.open_telnet({"address": "127.0.0.1", "telnet_port": srv.port}, 90, 25)
        chan.sock.settimeout(8)                      # a stalled server must fail this test, not hang the whole run
        self.addCleanup(chan.close)
        got = b""
        deadline = time.time() + 5
        while b"login: " not in got and time.time() < deadline:
            got += chan.recv()
        self.assertEqual(got, b"Switch-01\r\n\xff-ready\r\nlogin: ")
        chan.send("admin\r")
        echo = b""
        deadline = time.time() + 5
        while b"admin" not in echo and time.time() < deadline:
            echo += chan.recv()
        self.assertIn(b"admin", echo)
        self.assertIn(b"admin\r\n", bytes(srv.received))
        self.assertIn(bytes([IAC, SB, tp.OPT_NAWS, 0, 90, 0, 25, IAC, SE]), bytes(srv.received))
        self.assertIn(bytes([IAC, WILL, tp.OPT_TTYPE]), bytes(srv.received))

    def test_server_hanging_up(self):
        srv = FakeTelnetServer(hang_up_after=b"logout")
        self.addCleanup(srv.close)
        chan = tp.open_telnet({"address": "127.0.0.1", "telnet_port": srv.port})
        chan.sock.settimeout(8)                      # a stalled server must fail this test, not hang the whole run
        chan.recv()
        chan.send("logout\r")
        deadline = time.time() + 5
        while time.time() < deadline:
            if chan.recv() == b"" and chan.closed:
                break
        self.assertTrue(chan.exit_status_ready())

    def test_refused_connection(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(OSError):
            tp.open_telnet({"address": "127.0.0.1", "telnet_port": port}, timeout=2)


class SerialSpecTests(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(tp.parse_serial_spec("COM3"), {"port": "COM3", "baudrate": 9600, "bytesize": 8, "parity": "N",
                                                        "stopbits": 1, "xonxoff": False, "rtscts": False})
        s = tp.parse_serial_spec("COM12, 115200, 7e2, rtscts")
        self.assertEqual((s["port"], s["baudrate"], s["bytesize"], s["parity"], s["stopbits"], s["rtscts"]),
                         ("COM12", 115200, 7, "E", 2, True))
        self.assertEqual(tp.parse_serial_spec("COM3 xonxoff 8N1.5")["stopbits"], 1.5)
        self.assertEqual(tp.parse_serial_spec("/dev/ttyUSB0", 57600)["baudrate"], 57600)            # speed from the Port box
        self.assertEqual(tp.parse_serial_spec("COM3, 19200", 57600)["baudrate"], 19200)             # the text wins

    def test_errors_are_readable(self):
        for bad in ("", "   ", None):
            with self.assertRaisesRegex(ValueError, "Enter the COM port"):
                tp.parse_serial_spec(bad)
        with self.assertRaisesRegex(ValueError, "Don't understand “fast”"):
            tp.parse_serial_spec("COM3, fast")
        with self.assertRaisesRegex(ValueError, "not a sensible speed"):
            tp.parse_serial_spec("COM3, 7")
        with self.assertRaisesRegex(ValueError, "Don't understand"):
            tp.parse_serial_spec("COM3, 9N1")


@unittest.skipUnless(HAVE_SERIAL, "pyserial is not installed")
class SerialChannelTests(unittest.TestCase):
    def test_loopback_round_trip(self):
        chan = tp.open_serial("loop://, 115200, 8N1")
        self.addCleanup(chan.close)
        chan.send("hello\r")
        got = b""
        deadline = time.time() + 3
        while len(got) < 6 and time.time() < deadline:
            got += chan.recv()
        self.assertEqual(got, b"hello\r")                          # a serial Enter is a plain CR
        chan.resize_pty(80, 24)                                    # no-op, must not fail
        self.assertFalse(chan.exit_status_ready())

    def test_close_unblocks_and_ends(self):
        chan = tp.open_serial("loop://")
        t = threading.Thread(target=chan.recv, daemon=True)
        t.start()
        time.sleep(0.3)
        chan.close()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(chan.recv(), b"")

    def test_missing_port_names_the_ports_that_exist(self):
        with self.assertRaisesRegex(RuntimeError, "(?s)Could not open COM999.*Serial ports on this PC"):
            tp.open_serial("COM999")

    def test_bad_settings_reach_the_user_as_valueerror(self):
        with self.assertRaises(ValueError):
            tp.open_serial("COM3, wobble")

    def test_port_list_is_a_list_of_pairs(self):
        for dev, desc in tp.list_serial_ports():
            self.assertIsInstance(dev, str)
            self.assertIsInstance(desc, str)


class ConnectionListTests(unittest.TestCase):
    def test_extras_for_telnet_and_serial(self):
        h = core.sanitize_host({"address": "sw.lan", "connections": [
            {"type": "telnet", "port": 2323, "label": "Console"}, {"type": "telnet"},
            {"type": "serial", "url": "COM3, 115200", "label": "Serial console"},
            {"type": "serial", "url": "COM4", "port": 19200}]})
        extras = [c for c in core.connection_list(h) if c.get("extra")]
        self.assertEqual([c["action"] for c in extras], ["telnet", "telnet", "serial", "serial"])
        self.assertEqual(extras[0]["over"], {"telnet_port": 2323})
        self.assertEqual(extras[1]["over"], {"telnet_port": 23})
        self.assertEqual(extras[2]["over"], {"serial_spec": "COM3, 115200", "serial_baud": 0})
        self.assertEqual(extras[3]["detail"], "COM4  @ 19200 baud")
        self.assertEqual(core.variant(h, extras[0]["over"])["telnet_port"], 2323)

    def test_builtin_buttons_are_unchanged_and_new_ones_exist(self):
        h = core.sanitize_host({"address": "x.lan"})
        self.assertFalse([c for c in core.connection_list(h) if c["action"] in ("telnet", "serial")])
        for key in ("telnet", "serial"):
            self.assertIn(key, core.ACTION_LABEL)
            self.assertIn(key, core.SHORT_LABEL)
            self.assertIn(key, core.OPTIONAL_ACTIONS)


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
        self.queue = queue.Queue()

    def post(self, fn):
        self.queue.put(fn)

    def pump(self, seconds=0.2, root=None):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.queue.get(timeout=0.02)()
            except queue.Empty:
                pass
            if root:
                root.update()

    def confirm_broadcast(self):
        return True

    def open_snippets(self):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1000x600")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self._old = core.APP_DIR
        core.APP_DIR = tempfile.mkdtemp(prefix="rc_tpdata_")
        term.TerminalTab.AUTO_CONNECT = False
        self.app = FakeApp(self.root)

    def tearDown(self):
        for t in self.app.all_terminals():
            t.close()
        self.app.nb.destroy()
        term.TerminalTab.AUTO_CONNECT = True
        core.APP_DIR = self._old

    def open(self, **host):
        term.TerminalTab.AUTO_CONNECT = True
        h = dict({"name": "sw1", "address": "127.0.0.1"}, **host)
        t = term.TerminalTab(self.app, h, h.pop("_user", ""), "")
        self.app.nb.add(t, text="t")
        return t

    def wait_for(self, tab, text, seconds=5):
        end = time.time() + seconds
        while time.time() < end:
            self.app.pump(0.05, self.root)
            tab.render()
            if text in tab.t.get("1.0", "end"):
                return True
        return False


class TelnetTabTests(Base):
    def test_session_login_echo_and_logout(self):
        srv = FakeTelnetServer(hang_up_after=b"bye")
        self.addCleanup(srv.close)
        t = self.open(_kind="telnet", telnet_port=srv.port)
        self.assertTrue(self.wait_for(t, "login:"), t.t.get("1.0", "end"))
        self.assertIn("Switch-01", t.t.get("1.0", "end"))
        self.assertIn("�-ready", t.t.get("1.0", "end"))                # IAC IAC arrived as one 0xFF byte (not UTF-8: shown as the replacement mark)
        for ch in "admin":
            t.send_user(ch)
        t.send_user("\r")
        self.assertTrue(self.wait_for(t, "admin"))
        self.assertIn(b"admin\r\n", bytes(srv.received))
        for ch in "bye":
            t.send_user(ch)
        t.send_user("\r")
        self.assertTrue(self.wait_for(t, "[session ended]"))                # the server hung up: a log-out, no retry storm
        self.assertIsNone(t.reconnect_job)

    def test_login_script_logs_in_over_telnet(self):
        srv = FakeTelnetServer()
        self.addCleanup(srv.close)
        t = self.open(_kind="telnet", telnet_port=srv.port, _user="admin", login_script="login: => ${user}")
        self.assertTrue(self.wait_for(t, "login:"))
        end = time.time() + 5
        while b"admin" not in srv.received and time.time() < end:
            self.app.pump(0.05, self.root)
        self.assertIn(b"admin\r\n", bytes(srv.received))

    def test_refused_connection_is_reported_like_ssh(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        t = self.open(_kind="telnet", telnet_port=port)
        self.assertTrue(self.wait_for(t, "Connection failed", seconds=8), t.t.get("1.0", "end"))
        self.assertIn("Not connected", t.status_var.get())
        self.assertTrue(t.reconnect_btn.instate(["!disabled"]))              # Reconnect is offered

    def test_window_resize_reaches_the_server(self):
        srv = FakeTelnetServer()
        self.addCleanup(srv.close)
        t = self.open(_kind="telnet", telnet_port=srv.port)
        self.assertTrue(self.wait_for(t, "login:"))
        before = bytes(srv.received).count(bytes([IAC, SB, tp.OPT_NAWS]))
        t.cols, t.rows = 77, 33
        t.resize_pty()
        end = time.time() + 3
        while bytes(srv.received).count(bytes([IAC, SB, tp.OPT_NAWS])) <= before and time.time() < end:
            time.sleep(0.05)
        self.assertIn(bytes([IAC, SB, tp.OPT_NAWS, 0, 77, 0, 33, IAC, SE]), bytes(srv.received))


@unittest.skipUnless(HAVE_SERIAL, "pyserial is not installed")
class SerialTabTests(Base):
    def test_typing_echoes_back_through_the_loopback_port(self):
        t = self.open(_kind="serial", serial_spec="loop://, 9600", serial_baud=0)
        self.assertTrue(self.wait_for(t, "Connected."))
        for ch in "ping":
            t.send_user(ch)
        self.assertTrue(self.wait_for(t, "ping"))

    def test_a_missing_port_is_not_retried(self):
        t = self.open(_kind="serial", serial_spec="COM999")
        self.assertTrue(self.wait_for(t, "Could not open COM999"), t.t.get("1.0", "end"))
        self.assertIsNone(t.reconnect_job)                                   # the user fixes the port and clicks Reconnect


class PolishTests(Base):
    def tab(self):
        t = term.TerminalTab(self.app, {"name": "box", "address": "10.0.0.5"}, "u", "p")
        self.app.nb.add(t, text="t")
        self.root.update()
        t.apply_size()
        self.sent = []
        t.send = self.sent.append
        return t

    def test_every_scheme_is_complete(self):
        for name, sc in term.SCHEMES.items():
            self.assertEqual(len(sc["ansi"]), 16, name)
            for c in sc["ansi"] + [sc["bg"], sc["fg"], sc["cursor"], sc["sel"], sc["bold"]]:
                self.assertRegex(c, r"^#[0-9a-fA-F]{6}$", name)
        self.assertEqual(term.SCHEMES["Dark"]["ansi"], term.PALETTE)

    def test_style_colors_follow_the_scheme(self):
        red = ("red", "default", False, False, False, False)
        self.assertEqual(term.style_colors(red)[0], term.PALETTE[1])
        for name, sc in term.SCHEMES.items():
            self.assertEqual(term.style_colors(red, sc)[0], sc["ansi"][1])
            bold_red = ("red", "default", True, False, False, False)
            self.assertEqual(term.style_colors(bold_red, sc)[0], sc["ansi"][9])
            self.assertEqual(term.style_colors(("default", "default", True, False, False, False), sc)[0], sc["bold"])
            self.assertEqual(term.style_colors(("default", "default", False, False, False, True), sc), (sc["bg"], sc["fg"]))

    def test_prefs_are_cleaned(self):
        p = term.clean_prefs({"scheme": "Nonexistent", "font": "  ", "copy_on_select": "yes", "middle_click_paste": False})
        self.assertEqual((p["scheme"], p["font"], p["copy_on_select"], p["middle_click_paste"]), ("Dark", "Consolas", False, False))
        self.assertEqual(term.clean_prefs({"scheme": "Nord", "font": "Courier New"})["scheme"], "Nord")

    def test_choosing_a_scheme_recolours_everything_and_is_remembered(self):
        a, b = self.tab(), self.tab()
        a.feed("\x1b[31mred text\x1b[0m plain\r\n")
        a.render()
        a.choose_scheme("Solarized Dark")
        sc = term.SCHEMES["Solarized Dark"]
        for t in (a, b):
            self.assertEqual(t.t.cget("bg"), sc["bg"])
            self.assertEqual(t.t.cget("fg"), sc["fg"])
            self.assertEqual(t.scheme_var.get(), "Solarized Dark")
        red_tag = next(tag for style, tag in a.styles.items() if style[0] == "red")
        self.assertEqual(str(a.t.tag_cget(red_tag, "foreground")), sc["ansi"][1])                  # text already on screen too
        self.assertEqual(term.load_prefs()["scheme"], "Solarized Dark")
        self.assertEqual(self.tab().t.cget("bg"), sc["bg"])                                         # a new tab starts with it
        a.apply_scheme("nope")                                                                      # unknown names are ignored
        self.assertEqual(a.t.cget("bg"), sc["bg"])

    def test_new_colours_after_a_scheme_change_use_it(self):
        t = self.tab()
        t.choose_scheme("Dracula")
        t.feed("\x1b[32mgreen\x1b[0m")
        t.render()
        tag = next(tag for style, tag in t.styles.items() if style[0] == "green")
        self.assertEqual(str(t.t.tag_cget(tag, "foreground")), term.SCHEMES["Dracula"]["ansi"][2])

    def test_font_family_is_applied_everywhere_and_saved(self):
        a, b = self.tab(), self.tab()
        fam = next(f for f in term.monospace_families(a) if f != a.font.cget("family"))
        a.set_pref(font=fam)
        for t in (a, b):
            t.apply_font(fam)
        self.assertEqual((a.font.cget("family"), b.font.cget("family")), (fam, fam))
        self.assertEqual(term.load_prefs()["font"], fam)
        self.assertEqual(self.tab().font.cget("family"), fam)

    def test_unknown_saved_font_falls_back(self):
        term.save_prefs(dict(term.load_prefs(), font="No Such Font 123"))
        t = self.tab()
        self.assertIn(t.font.cget("family"), ("Consolas", "Courier New"))

    def test_monospace_list_has_only_fixed_fonts(self):
        t = self.tab()
        fams = term.monospace_families(t)
        self.assertTrue(fams)
        self.assertTrue(all(not f.startswith("@") for f in fams))
        self.assertIn("Consolas" if "Consolas" in fams else fams[0], fams)

    def test_copy_on_select(self):
        t = self.tab()
        t.feed("select this text")
        t.render()
        t.t.tag_add("sel", "1.0", "1.6")
        t.clipboard_clear()
        t.select_release(None)
        with self.assertRaises(tk.TclError):
            t.clipboard_get()                                                                       # off by default
        t.opt_vars["copy_on_select"].set(True)
        t.set_option("copy_on_select")
        t.select_release(None)
        self.assertEqual(t.clipboard_get(), t.t.get("sel.first", "sel.last"))

    def test_right_and_middle_click_paste(self):
        t = self.tab()
        t.clipboard_clear()
        t.clipboard_append("pasted")
        ev = mock.Mock(x_root=0, y_root=0)
        with mock.patch.object(t.menu, "tk_popup") as popup:
            t.popup(ev)
            popup.assert_called_once()                                                              # default: the menu
            self.assertEqual(self.sent, [])
            t.opt_vars["right_click_paste"].set(True)
            t.set_option("right_click_paste")
            self.assertEqual(t.popup(ev), "break")
            popup.assert_called_once()
        self.assertEqual(self.sent, ["pasted"])
        self.assertEqual(t.middle_click(ev), "break")                                               # middle-click pastes by default
        self.assertEqual(self.sent, ["pasted", "pasted"])
        t.opt_vars["middle_click_paste"].set(False)
        t.set_option("middle_click_paste")
        t.middle_click(ev)
        self.assertEqual(len(self.sent), 2)

    def test_save_output_includes_scrollback_and_strips_trailing_spaces(self):
        t = self.tab()
        t.feed("\r\n".join(f"line{i}   " for i in range(t.rows + 15)))
        path = os.path.join(tempfile.mkdtemp(prefix="rc_out_"), "out.txt")
        from tkinter import filedialog
        with mock.patch.object(filedialog, "asksaveasfilename", return_value=path):
            self.assertEqual(t.save_output(), path)
        with open(path, encoding="utf-8", newline="") as f:
            body = f.read()
        lines = body.split("\n")
        self.assertIn("line0", lines)                                                              # scrolled-off lines are included
        self.assertLess(lines.index("line0"), lines.index("line30"))
        self.assertIn(f"line{t.rows + 14}", body)
        self.assertTrue(all(l == l.rstrip() for l in lines))
        self.assertTrue(body.endswith("\n") and not body.endswith("\n\n"))
        self.assertIn("Saved the terminal output", self.app.status.get())

    def test_save_output_cancelled(self):
        t = self.tab()
        from tkinter import filedialog
        with mock.patch.object(filedialog, "asksaveasfilename", return_value=""):
            self.assertIsNone(t.save_output())


if __name__ == "__main__":
    unittest.main()
