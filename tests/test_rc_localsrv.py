"""Tests for rc_localsrv.py: the TFTP server (read, write, options, safety), the HTTP server and the tab. A small TFTP
client lives in this file. Everything runs on 127.0.0.1.  Run from the RC folder:  python -m unittest discover -s tests -v"""

import os
import queue
import socket
import struct
import sys
import tempfile
import time
import tkinter as tk
import unittest
import urllib.error
import urllib.request
from tkinter import ttk
from unittest import mock

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_localsrv_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_localsrv as ls  # noqa: E402

RRQ, WRQ, DATA, ACK, ERROR, OACK = ls.RRQ, ls.WRQ, ls.DATA, ls.ACK, ls.ERROR, ls.OACK


class TftpError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code, self.msg = code, msg


def request(op, name, mode="octet", opts=None):
    pkt = struct.pack("!H", op) + name.encode() + b"\0" + mode.encode() + b"\0"
    for k, v in (opts or {}).items():
        pkt += k.encode() + b"\0" + str(v).encode() + b"\0"
    return pkt


def tftp_get(port, name, blksize=None, timeout=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        opts = {"blksize": blksize, "tsize": 0} if blksize else None
        s.sendto(request(RRQ, name, opts=opts), ("127.0.0.1", port))
        data, blk, server, oack = b"", 1, None, None
        while True:
            pkt, addr = s.recvfrom(70000)
            server = server or addr
            op = struct.unpack("!H", pkt[:2])[0]
            if op == ERROR:
                raise TftpError(struct.unpack("!H", pkt[2:4])[0], pkt[4:-1].decode())
            if op == OACK:
                oack = dict(zip(*[iter(pkt[2:].split(b"\0")[:-1])] * 2))
                s.sendto(struct.pack("!HH", ACK, 0), server)
                continue
            n = struct.unpack("!H", pkt[2:4])[0]
            if n == blk & 0xFFFF:
                data += pkt[4:]
                s.sendto(struct.pack("!HH", ACK, n), server)
                blk += 1
                if len(pkt) - 4 < (blksize or 512):
                    return (data, oack) if blksize else data
    finally:
        s.close()


def tftp_put(port, name, payload, blksize=None, timeout=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        bs = blksize or 512
        s.sendto(request(WRQ, name, opts={"blksize": blksize} if blksize else None), ("127.0.0.1", port))
        pkt, server = s.recvfrom(70000)
        op = struct.unpack("!H", pkt[:2])[0]
        if op == ERROR:
            raise TftpError(struct.unpack("!H", pkt[2:4])[0], pkt[4:-1].decode())
        blocks = [payload[i:i + bs] for i in range(0, len(payload), bs)]
        if len(payload) % bs == 0:
            blocks.append(b"")
        for n, chunk in enumerate(blocks, 1):
            s.sendto(struct.pack("!HH", DATA, n & 0xFFFF) + chunk, server)
            pkt, _ = s.recvfrom(70000)
            if struct.unpack("!H", pkt[:2])[0] == ERROR:
                raise TftpError(struct.unpack("!H", pkt[2:4])[0], pkt[4:-1].decode())
    finally:
        s.close()


def free_port(udp=False):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class PortHelpTests(unittest.TestCase):
    def test_port_available_and_first_free(self):
        s = socket.socket()
        s.bind(("0.0.0.0", 0))
        s.listen(1)
        held = s.getsockname()[1]
        self.addCleanup(s.close)
        self.assertFalse(ls.port_available(held))
        free = free_port()
        self.assertTrue(ls.port_available(free))
        self.assertEqual(ls.first_free([held, free]), free)                      # skips a port that cannot be used
        self.assertEqual(ls.first_free([held]), held)                            # none usable: the first, so the real error shows

    def test_bind_errors_are_explained(self):
        reserved = OSError(10013, "forbidden")
        reserved.winerror = 10013
        text = ls.explain_bind_error(reserved, 8099)
        self.assertIn("reserved", text)
        self.assertIn("8888", text)
        self.assertIn("already using port 8000", ls.explain_bind_error(OSError(10048, "in use"), 8000))
        self.assertEqual(ls.explain_bind_error(OSError("odd"), 1), "odd")

    def test_the_lan_address_comes_first(self):
        class S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def connect(self, addr):
                pass

            def getsockname(self):
                return ("192.168.1.5", 1234)
        with mock.patch.object(ls.socket, "socket", return_value=S()), \
                mock.patch.object(ls.socket, "gethostbyname_ex", return_value=("h", [], ["172.18.0.1", "192.168.1.5", "127.0.0.1"])):
            self.assertEqual(ls.local_addresses(), ["192.168.1.5", "172.18.0.1"])


class SafePathTests(unittest.TestCase):
    def test_inside_and_outside(self):
        root = tempfile.mkdtemp(prefix="rc_root_")
        self.assertEqual(ls.safe_path(root, "a.txt"), os.path.join(root, "a.txt"))
        self.assertEqual(ls.safe_path(root, "/sub/a.txt"), os.path.join(root, "sub", "a.txt"))          # leading slash ignored
        self.assertEqual(ls.safe_path(root, "sub\\a.txt"), os.path.join(root, "sub", "a.txt"))          # Windows separators
        for bad in ("../x", "a/../../x", "..\\x", "..", "", "/", "a\x00b", "sub/../../etc"):
            self.assertIsNone(ls.safe_path(root, bad), bad)


class TftpTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rc_tftp_")
        self.log = []
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.stop()

    def start(self, **kw):
        self.srv = ls.TftpServer(self.root, port=0, bind="127.0.0.1", logger=self.log.append, **kw)
        self.srv.timeout = 0.5
        self.srv.start()
        return self.srv.port

    def put_file(self, name, data):
        path = os.path.join(self.root, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def test_read_sizes(self):
        port = self.start()
        for n in (0, 1, 511, 512, 513, 1024, 5000):
            data = os.urandom(n)
            self.put_file(f"f{n}.bin", data)
            self.assertEqual(tftp_get(port, f"f{n}.bin"), data, n)            # 512 and 1024 end with an empty block

    def test_subfolder_and_absolute_style_names(self):
        port = self.start()
        self.put_file("cfg/sw1.conf", b"hostname sw1\n")
        self.assertEqual(tftp_get(port, "cfg/sw1.conf"), b"hostname sw1\n")
        self.assertEqual(tftp_get(port, "/cfg/sw1.conf"), b"hostname sw1\n")

    def test_blksize_option(self):
        port = self.start()
        data = os.urandom(10000)
        self.put_file("big.bin", data)
        got, oack = tftp_get(port, "big.bin", blksize=1428)
        self.assertEqual(got, data)
        self.assertEqual(oack[b"blksize"], b"1428")
        self.assertEqual(oack[b"tsize"], b"10000")                             # the server tells the real size
        got, oack = tftp_get(port, "big.bin", blksize=99999)
        self.assertEqual(oack[b"blksize"], b"65464")                           # clamped to the protocol maximum
        self.assertEqual(got, data)

    def test_not_found_and_outside_the_folder(self):
        port = self.start()
        with self.assertRaises(TftpError) as cm:
            tftp_get(port, "missing.txt")
        self.assertEqual(cm.exception.code, 1)
        secret = os.path.join(os.path.dirname(self.root), "rc_secret.txt")
        with open(secret, "w") as f:
            f.write("secret")
        self.addCleanup(os.remove, secret)
        for name in ("../rc_secret.txt", "..\\rc_secret.txt", "sub/../../rc_secret.txt"):
            with self.assertRaises(TftpError) as cm:
                tftp_get(port, name)
            self.assertEqual(cm.exception.code, 2, name)                       # access violation
        self.assertTrue(any("outside the folder" in m for m in self.log))

    def test_a_folder_is_not_a_file(self):
        port = self.start()
        os.makedirs(os.path.join(self.root, "d"))
        with self.assertRaises(TftpError):
            tftp_get(port, "d")

    def test_uploads_are_refused_by_default(self):
        port = self.start()
        with self.assertRaises(TftpError) as cm:
            tftp_put(port, "new.txt", b"x")
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.root, "new.txt")))
        self.assertTrue(any("uploads are off" in m for m in self.log))

    def test_upload_when_allowed(self):
        port = self.start(allow_write=True)
        for n in (0, 5, 512, 1500):
            data = os.urandom(n)
            tftp_put(port, f"up{n}.bin", data)
            time.sleep(0.2)
            with open(os.path.join(self.root, f"up{n}.bin"), "rb") as f:
                self.assertEqual(f.read(), data, n)
        tftp_put(port, "deep/er/file.bin", b"abc", blksize=1024)
        time.sleep(0.2)
        with open(os.path.join(self.root, "deep", "er", "file.bin"), "rb") as f:
            self.assertEqual(f.read(), b"abc")
        self.assertFalse([f for f in os.listdir(self.root) if f.endswith(".part")])
        self.assertTrue(any("received up1500.bin (1500 bytes" in m for m in self.log))

    def test_existing_files_are_only_replaced_if_allowed(self):
        self.put_file("cfg.txt", b"old")
        port = self.start(allow_write=True)
        with self.assertRaises(TftpError) as cm:
            tftp_put(port, "cfg.txt", b"new")
        self.assertEqual(cm.exception.code, 6)
        self.srv.stop()
        port = self.start(allow_write=True, overwrite=True)
        tftp_put(port, "cfg.txt", b"new")
        time.sleep(0.2)
        with open(os.path.join(self.root, "cfg.txt"), "rb") as f:
            self.assertEqual(f.read(), b"new")

    def test_cannot_write_outside_the_folder(self):
        port = self.start(allow_write=True, overwrite=True)
        with self.assertRaises(TftpError) as cm:
            tftp_put(port, "../evil.txt", b"x")
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.root), "evil.txt")))

    def test_an_aborted_upload_leaves_nothing_behind(self):
        port = self.start(allow_write=True)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(request(WRQ, "half.bin"), ("127.0.0.1", port))
        _, server = s.recvfrom(1000)
        s.sendto(struct.pack("!HH", DATA, 1) + b"x" * 512, server)
        s.recvfrom(1000)
        s.close()                                                              # ... and the client vanishes
        self.srv.retries = 2
        time.sleep(2.5)
        self.assertFalse(os.path.exists(os.path.join(self.root, "half.bin")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "half.bin.part")))

    def test_garbage_does_not_hurt_the_server(self):
        port = self.start()
        self.put_file("ok.txt", b"fine")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for junk in (b"", b"x", b"\x00\x09abc", b"\x00\x01", b"\x00\x01noterminator", b"\xff" * 600, b"\x00\x01\xff\xfe\x00octet\x00"):
            s.sendto(junk, ("127.0.0.1", port))
        s.close()
        time.sleep(0.3)
        self.assertEqual(tftp_get(port, "ok.txt"), b"fine")

    def test_unknown_sender_in_the_middle_of_a_transfer_is_told_off(self):
        port = self.start()
        self.put_file("two.bin", os.urandom(1000))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(request(RRQ, "two.bin"), ("127.0.0.1", port))
        pkt, server = s.recvfrom(2000)
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        stranger.settimeout(2)
        stranger.sendto(struct.pack("!HH", ACK, 1), server)                    # a different source port
        reply, _ = stranger.recvfrom(2000)
        self.assertEqual(struct.unpack("!HH", reply[:4]), (ERROR, 5))
        s.close()
        stranger.close()

    def test_stop_frees_the_port_and_start_validates_the_folder(self):
        port = self.start()
        self.srv.stop()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", port))                                            # free again
        s.close()
        self.assertTrue(any("stopped" in m for m in self.log))
        with self.assertRaises(OSError):
            ls.TftpServer(os.path.join(self.root, "nope"), port=0).start()

    def test_request_parser(self):
        self.assertEqual(ls.parse_request(request(RRQ, "a.bin", "OCTET", {"blksize": 512})), (RRQ, "a.bin", "octet", {"blksize": "512"}))
        self.assertIsNone(ls.parse_request(b"\x00\x03\x00\x01data"))
        self.assertIsNone(ls.parse_request(b""))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rc_http_")
        with open(os.path.join(self.root, "hello.txt"), "w") as f:
            f.write("hello")
        os.makedirs(os.path.join(self.root, "sub"))
        self.log = []
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.stop()

    def start(self, **kw):
        self.srv = ls.HttpServer(self.root, port=0, bind="127.0.0.1", logger=self.log.append, **kw)
        self.srv.start()
        return f"http://127.0.0.1:{self.srv.port}"

    def get(self, url, method="GET"):
        req = urllib.request.Request(url, method=method, data=b"x" if method in ("POST", "PUT") else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_serves_files_and_logs_requests(self):
        base = self.start()
        self.assertEqual(self.get(base + "/hello.txt"), (200, b"hello"))
        self.assertEqual(self.get(base + "/nope.txt")[0], 404)
        self.assertTrue(any("GET /hello.txt" in m and "200" in m for m in self.log))

    def test_listing_on_and_off(self):
        base = self.start(listing=True)
        status, body = self.get(base + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"hello.txt", body)
        self.srv.stop()
        base = self.start(listing=False)
        self.assertEqual(self.get(base + "/")[0], 403)
        self.assertEqual(self.get(base + "/hello.txt"), (200, b"hello"))        # files still work

    def test_outside_the_folder_and_writes_are_refused(self):
        base = self.start()
        secret = os.path.join(os.path.dirname(self.root), "rc_http_secret.txt")
        with open(secret, "w") as f:
            f.write("secret")
        self.addCleanup(os.remove, secret)
        for url in ("/../rc_http_secret.txt", "/%2e%2e/rc_http_secret.txt", "/..%5crc_http_secret.txt", "/sub/../../rc_http_secret.txt"):
            status, body = self.get(base + url)
            self.assertNotEqual(body, b"secret", url)
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.get(base + "/hello.txt", method)[0], 405, method)

    def test_stop_frees_the_port(self):
        base = self.start()
        port = self.srv.port
        self.srv.stop()
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.close()
        with self.assertRaises(OSError):
            ls.HttpServer(os.path.join(self.root, "nope"), port=0).start()


class FakeApp:
    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.queue = queue.Queue()

    def post(self, fn):
        self.queue.put(fn)

    def pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.queue.get(timeout=0.02)()
            except queue.Empty:
                pass


class TabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.app = FakeApp(self.root)
        self.tab = ls.LocalServicesTab(self.app)
        self.app.nb.add(self.tab, text="x")
        self.folder = tempfile.mkdtemp(prefix="rc_tabroot_")
        with open(os.path.join(self.folder, "f.txt"), "w") as f:
            f.write("data")
        self.warned = []
        self.patches = [mock.patch.object(ls.messagebox, "showwarning", side_effect=lambda *a, **k: self.warned.append(a[1])),
                        mock.patch.object(ls.messagebox, "showerror", side_effect=lambda *a, **k: self.warned.append(a[1]))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tab.close()
        self.app.nb.destroy()

    def configure(self, panel, port):
        panel.folder.set(self.folder)
        panel.port.set(str(port))

    def test_start_serve_log_and_stop_tftp(self):
        port = free_port(udp=True)
        p = self.tab.tftp
        self.configure(p, port)
        self.assertTrue(p.start())
        self.assertIn("Running", p.status.get())
        self.assertEqual(p.btn.cget("text"), "■ Stop")
        got = tftp_get(port, "f.txt")
        self.assertEqual(got, b"data")
        self.app.pump()
        self.assertIn("sent f.txt", self.tab.out.get("1.0", "end"))
        p.toggle()
        self.assertEqual((p.status.get(), p.btn.cget("text"), p.server), ("Stopped", "▶ Start", None))

    def test_http_panel(self):
        port = free_port()
        p = self.tab.http
        self.configure(p, port)
        self.assertTrue(p.start())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/f.txt", timeout=5) as r:
            self.assertEqual(r.read(), b"data")
        self.app.pump()
        self.assertIn("GET /f.txt", self.tab.out.get("1.0", "end"))

    def test_validation(self):
        p = self.tab.tftp
        p.folder.set(os.path.join(self.folder, "nope"))
        self.assertFalse(p.start())
        self.configure(p, "abc")
        self.assertFalse(p.start())
        self.configure(p, 70000)
        self.assertFalse(p.start())
        self.configure(p, 69)
        p.auto.set("-5")
        self.assertFalse(p.start())
        self.assertEqual(len(self.warned), 4)
        self.assertIsNone(p.server)

    def test_port_in_use_is_explained(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        self.addCleanup(s.close)
        p = self.tab.http
        p.folder.set(self.folder)
        p.port.set(str(port))
        # bind on all interfaces collides with the listener on 127.0.0.1 only on some systems: use the same address
        with mock.patch.object(ls.HttpServer, "__init__", lambda self, root, port, listing=True, bind="0.0.0.0", logger=None:
                               (setattr(self, "root", root), setattr(self, "port", port), setattr(self, "listing", listing),
                                setattr(self, "bind", "127.0.0.1"), setattr(self, "logger", logger), setattr(self, "httpd", None),
                                setattr(self, "running", False))[0]):
            self.assertFalse(p.start())
        self.assertTrue(any("another program" in w.lower() for w in self.warned))
        self.assertIsNone(p.server)

    def test_a_reserved_port_gets_a_clear_message_instead_of_a_cryptic_one(self):
        p = self.tab.http
        self.configure(p, 8099)
        err = OSError(10013, "forbidden")
        err.winerror = 10013
        with mock.patch.object(ls.HttpServer, "start", side_effect=err):
            self.assertFalse(p.start())
        self.assertTrue(any("reserved" in w for w in self.warned))
        self.assertIsNone(p.server)

    def test_default_http_port_is_one_this_pc_can_use(self):
        self.assertTrue(ls.port_available(int(self.tab.http.port.get())))

    def test_timer_stops_the_server(self):
        port = free_port(udp=True)
        p = self.tab.tftp
        self.configure(p, port)
        p.auto.set("0.01")                                                     # 0.6 s
        self.assertTrue(p.start())
        end = time.time() + 3
        while p.server and time.time() < end:
            self.root.update()
            time.sleep(0.05)
        self.assertIsNone(p.server)
        self.app.pump()
        self.assertIn("stopped by its timer", self.tab.out.get("1.0", "end"))

    def test_closing_the_tab_stops_everything(self):
        p1, p2 = free_port(udp=True), free_port()
        self.configure(self.tab.tftp, p1)
        self.configure(self.tab.http, p2)
        self.tab.tftp.start()
        self.tab.http.start()
        servers = [self.tab.tftp.server, self.tab.http.server]
        self.tab.close()
        self.assertTrue(all(not s.running for s in servers))
        self.assertEqual((self.tab.tftp.server, self.tab.http.server), (None, None))

    def test_local_addresses_are_not_loopback(self):
        self.assertTrue(all(not a.startswith("127.") for a in ls.local_addresses()))


if __name__ == "__main__":
    unittest.main()
