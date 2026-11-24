"""Tests for identities (saved logins) and proxies: storage, how an identity replaces a server's login, the SOCKS5 and
HTTP-CONNECT clients against throw-away proxy servers, SSH through a proxy to an in-process SSH server, and the two
manager tabs. Nothing leaves this PC.  Run from the RC folder:  python -m unittest discover -s tests -v"""

import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

os.environ["RC_DATA_DIR"] = tempfile.mkdtemp(prefix="rc_ident_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_manager as core  # noqa: E402
import rc_identity as ident  # noqa: E402
from test_rc_features import SshServer  # noqa: E402

WIN = sys.platform == "win32"


def pipe(a, b):
    try:
        while True:
            d = a.recv(4096)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for x in (a, b):
            try:
                x.close()
            except OSError:
                pass


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise OSError("closed")
        buf += c
    return buf


class FakeProxy:
    """kind 'socks5' or 'http'. user/password None = no login needed. refuse = SOCKS reply code to answer with."""

    def __init__(self, kind, user=None, password=None, refuse=0, http_status=200):
        self.kind, self.user, self.password, self.refuse, self.http_status = kind, user, password, refuse, http_status
        self.requests = []        # (atyp or 'http', host, port)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(10)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()

    def close(self):
        self.sock.close()

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, c):
        try:
            (self.socks if self.kind == "socks5" else self.http)(c)
        except OSError:
            c.close()

    def socks(self, c):
        ver, n = recv_exact(c, 2)
        methods = recv_exact(c, n)
        want = 2 if self.user is not None else 0
        if want not in methods:
            c.sendall(b"\x05\xff")
            return c.close()
        c.sendall(bytes([5, want]))
        if want == 2:
            recv_exact(c, 1)
            u = recv_exact(c, recv_exact(c, 1)[0]).decode()
            p = recv_exact(c, recv_exact(c, 1)[0]).decode()
            ok = (u, p) == (self.user, self.password)
            c.sendall(bytes([1, 0 if ok else 1]))
            if not ok:
                return c.close()
        _v, _cmd, _r, atyp = recv_exact(c, 4)
        if atyp == 1:
            host = socket.inet_ntoa(recv_exact(c, 4))
        elif atyp == 3:
            host = recv_exact(c, recv_exact(c, 1)[0]).decode()
        else:
            host = "?"
        port = struct.unpack(">H", recv_exact(c, 2))[0]
        self.requests.append((atyp, host, port))
        if self.refuse:
            c.sendall(bytes([5, self.refuse, 0, 1]) + b"\0" * 6)
            return c.close()
        try:
            out = socket.create_connection(("127.0.0.1" if host in ("localhost", "intranet.lan") else host, port), 5)
        except OSError:
            c.sendall(bytes([5, 5, 0, 1]) + b"\0" * 6)
            return c.close()
        c.sendall(bytes([5, 0, 0, 1]) + b"\0" * 6)
        threading.Thread(target=pipe, args=(c, out), daemon=True).start()
        threading.Thread(target=pipe, args=(out, c), daemon=True).start()

    def http(self, c):
        head = b""
        while b"\r\n\r\n" not in head:
            head += c.recv(1)
        lines = head.decode().split("\r\n")
        verb, target, _ = lines[0].split(" ", 2)
        host, _, port = target.rpartition(":")
        self.requests.append(("http", host, int(port)))
        if self.user is not None:
            import base64
            auth = next((l.split(": ", 1)[1] for l in lines if l.lower().startswith("proxy-authorization")), "")
            if auth != "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode():
                c.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                return c.close()
        if self.http_status != 200:
            c.sendall(f"HTTP/1.1 {self.http_status} Nope\r\n\r\n".encode())
            return c.close()
        try:
            out = socket.create_connection(("127.0.0.1", int(port)), 5)
        except OSError:
            c.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return c.close()
        c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        threading.Thread(target=pipe, args=(c, out), daemon=True).start()
        threading.Thread(target=pipe, args=(out, c), daemon=True).start()


class Echo:
    """A TCP server that greets with a banner, then echoes."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            c.sendall(b"SSH-2.0-echo\r\n")
            threading.Thread(target=lambda c=c: pipe(c, c), daemon=True).start()

    def close(self):
        self.sock.close()


def proxy_dict(fp, user="", pw=""):
    return {"id": "p1", "name": "px", "kind": fp.kind, "host": "127.0.0.1", "port": fp.port, "user": user,
            "password_enc": core.protect_password(pw) if pw and WIN else ""}


class Data(unittest.TestCase):
    """Each test gets its own identities.json / proxies.json / hosts."""

    def setUp(self):
        self._old = (core.IDENTITIES_FILE, core.PROXIES_FILE, core.HOSTS_FILE if hasattr(core, "HOSTS_FILE") else None)
        d = tempfile.mkdtemp(prefix="rc_identdata_")
        core.IDENTITIES_FILE = os.path.join(d, "identities.json")
        core.PROXIES_FILE = os.path.join(d, "proxies.json")
        core._LIST_CACHE.clear()
        self.dir = d

    def tearDown(self):
        core.IDENTITIES_FILE, core.PROXIES_FILE = self._old[0], self._old[1]
        core._LIST_CACHE.clear()


class StorageTests(Data):
    def test_round_trip_and_cleaning(self):
        self.assertEqual(core.load_identities(), [])
        core.save_identities([{"id": "i1", "name": "  Lab admin ", "user": "root", "auth": "weird", "secret_enc": "xx", "key_file": ""},
                              {"id": "i2", "name": "", "user": "x"}, {"id": "i1", "name": "dup"}, "junk"])
        got = core.load_identities()
        self.assertEqual([(i["id"], i["name"], i["auth"]) for i in got], [("i1", "Lab admin", "password")])

    def test_proxy_cleaning(self):
        core.save_proxies([{"id": "a", "name": "Office", "kind": "http", "host": "10.0.0.1", "port": "99999"},
                           {"id": "b", "name": "No host", "kind": "socks5"}, {"name": "ok", "host": "h", "kind": "bogus", "port": 3128}])
        got = core.load_proxies()
        self.assertEqual([(p["name"], p["kind"], p["port"]) for p in got], [("Office", "http", 8080), ("ok", "socks5", 3128)])
        self.assertTrue(got[1]["id"])                      # an id is made up when missing

    def test_damaged_file_is_moved_aside(self):
        with open(core.IDENTITIES_FILE, "w") as f:
            f.write("{nope")
        self.assertEqual(core.load_identities(), [])
        names = os.listdir(self.dir)
        self.assertTrue(any(n.startswith("identities.json.corrupt-") for n in names))
        self.assertNotIn("identities.json", names)

    def test_cache_follows_file_changes(self):
        core.save_identities([{"id": "i1", "name": "A"}])
        self.assertEqual(core.load_identities()[0]["name"], "A")
        core.save_identities([{"id": "i1", "name": "B"}])
        self.assertEqual(core.load_identities()[0]["name"], "B")
        time.sleep(0.02)
        with open(core.IDENTITIES_FILE, "w") as f:                  # edited by hand
            json.dump([{"id": "i1", "name": "C"}], f)
        self.assertEqual(core.load_identities()[0]["name"], "C")

    def test_returned_lists_are_copies(self):
        core.save_identities([{"id": "i1", "name": "A"}])
        core.load_identities()[0]["name"] = "changed"
        self.assertEqual(core.load_identities()[0]["name"], "A")


@unittest.skipUnless(WIN, "uses DPAPI")
class IdentityTests(Data):
    def setUp(self):
        super().setUp()
        core.save_identities([
            {"id": "pw", "name": "Pw", "user": "admin", "auth": "password", "secret_enc": core.protect_password("s3cret")},
            {"id": "key", "name": "Key", "user": "deploy", "auth": "key", "key_file": r"C:\keys\id", "secret_enc": core.protect_password("phrase")},
            {"id": "ag", "name": "Agent", "user": "ron", "auth": "agent", "secret_enc": "leftover", "key_file": "x"},
            {"id": "ask", "name": "Ask", "user": "bob", "auth": "password", "secret_enc": ""}])
        self.h = core.sanitize_host({"id": "h1", "name": "srv", "address": "10.0.0.5", "user": "own", "key_file": "own.key",
                                     "password_enc": core.protect_password("own-pw")})

    def eff(self, ident_id):
        return core.effective_host(dict(self.h, identity=ident_id))

    def test_no_identity_or_unknown_identity_changes_nothing(self):
        self.assertIs(core.effective_host(self.h), self.h)
        self.assertEqual(self.eff("gone")["user"], "own")

    def test_password_identity_replaces_everything(self):
        e = self.eff("pw")
        self.assertEqual((e["user"], e["key_file"]), ("admin", ""))
        self.assertEqual(core.saved_login(e), ("admin", "s3cret"))
        self.assertEqual(core.saved_login(dict(self.h, identity="pw")), ("admin", "s3cret"))    # saved_login resolves by itself too

    def test_key_identity(self):
        e = self.eff("key")
        self.assertEqual((e["user"], e["key_file"]), ("deploy", r"C:\keys\id"))
        self.assertEqual(core.unprotect_password(e["password_enc"]), "phrase")           # the passphrase

    def test_agent_identity_drops_secrets(self):
        e = self.eff("ag")
        self.assertEqual((e["user"], e["password_enc"], e["key_file"]), ("ron", "", ""))
        self.assertIsNone(core.saved_login(e))

    def test_identity_without_password_asks_and_does_not_save_on_the_server(self):
        e = self.eff("ask")
        self.assertEqual((e["user"], e["password_enc"], e["_nosave"]), ("bob", "", True))

    def test_stored_server_is_never_changed_and_resolving_twice_is_the_same(self):
        h = dict(self.h, identity="pw")
        before = json.dumps(h, sort_keys=True)
        e = core.effective_host(h)
        self.assertEqual(json.dumps(h, sort_keys=True), before)
        self.assertEqual(core.effective_host(e), e)

    def test_extra_connection_with_its_own_login_ignores_the_identity(self):
        h = dict(self.h, identity="pw")
        v = core.variant(h, {"user": "other", "password_enc": core.protect_password("x")})
        self.assertEqual(core.effective_host(v)["user"], "other")
        v2 = core.variant(h, {"ssh_port": 2222})
        self.assertEqual(core.effective_host(v2)["user"], "admin")                       # only a port: identity still applies

    def test_auto_login_for_tunnels_and_snippets(self):
        import rc_tools
        self.assertEqual(rc_tools.auto_login(dict(self.h, identity="pw")), ("admin", "s3cret"))
        self.assertEqual(rc_tools.auto_login(dict(self.h, identity="ask")), ("bob", ""))

    def test_credentials_use_the_identity(self):
        class A:
            root = None
        creds = core.App.credentials
        self.assertEqual(creds(A(), dict(self.h, identity="pw")), ("admin", "s3cret"))
        self.assertEqual(creds(A(), dict(self.h, identity="key")), ("deploy", "phrase"))

    def test_host_fields_survive_sanitising(self):
        h = core.sanitize_host({"address": "x.lan", "identity": "pw", "proxy": "p1"})
        self.assertEqual((h["identity"], h["proxy"]), ("pw", "p1"))
        h = core.sanitize_host({"address": "x.lan", "identity": None, "proxy": 5})
        self.assertEqual((h["identity"], h["proxy"]), ("", "5"))
        self.assertEqual(core.sanitize_host({"address": "x.lan"})["identity"], "")


class ProxyClientTests(unittest.TestCase):
    def setUp(self):
        self.echo = Echo()
        self.addCleanup(self.echo.close)

    def banner(self, sock):
        sock.settimeout(3)
        data = sock.recv(20)
        sock.close()
        return data

    def make(self, **kw):
        fp = FakeProxy(**kw)
        self.addCleanup(fp.close)
        return fp

    def test_socks5_by_ip(self):
        fp = self.make(kind="socks5")
        s = core.proxy_connect(proxy_dict(fp), "127.0.0.1", self.echo.port)
        self.assertEqual(self.banner(s), b"SSH-2.0-echo\r\n")
        self.assertEqual(fp.requests, [(1, "127.0.0.1", self.echo.port)])

    def test_socks5_sends_names_for_the_proxy_to_look_up(self):
        fp = self.make(kind="socks5")
        s = core.proxy_connect(proxy_dict(fp), "intranet.lan", self.echo.port)
        self.assertEqual(self.banner(s), b"SSH-2.0-echo\r\n")
        self.assertEqual(fp.requests, [(3, "intranet.lan", self.echo.port)])

    @unittest.skipUnless(WIN, "uses DPAPI")
    def test_socks5_login(self):
        fp = self.make(kind="socks5", user="ron", password="pw1")
        s = core.proxy_connect(proxy_dict(fp, "ron", "pw1"), "127.0.0.1", self.echo.port)
        self.assertEqual(self.banner(s), b"SSH-2.0-echo\r\n")
        with self.assertRaisesRegex(core.ProxyError, "rejected the username or password"):
            core.proxy_connect(proxy_dict(fp, "ron", "wrong"), "127.0.0.1", self.echo.port)

    def test_socks5_needs_a_login_we_did_not_give(self):
        fp = self.make(kind="socks5", user="ron", password="pw1")
        with self.assertRaisesRegex(core.ProxyError, "username and password"):
            core.proxy_connect(proxy_dict(fp), "127.0.0.1", self.echo.port)

    def test_socks5_refusal_is_explained(self):
        fp = self.make(kind="socks5", refuse=5)
        with self.assertRaisesRegex(core.ProxyError, "connection refused by the target"):
            core.proxy_connect(proxy_dict(fp), "127.0.0.1", self.echo.port)
        fp2 = self.make(kind="socks5", refuse=2)
        with self.assertRaisesRegex(core.ProxyError, "not allowed by the proxy"):
            core.proxy_connect(proxy_dict(fp2), "127.0.0.1", self.echo.port)

    def test_http_connect(self):
        fp = self.make(kind="http")
        s = core.proxy_connect(proxy_dict(fp), "127.0.0.1", self.echo.port)
        self.assertEqual(self.banner(s), b"SSH-2.0-echo\r\n")
        self.assertEqual(fp.requests, [("http", "127.0.0.1", self.echo.port)])

    @unittest.skipUnless(WIN, "uses DPAPI")
    def test_http_login(self):
        fp = self.make(kind="http", user="ron", password="pw1")
        self.banner(core.proxy_connect(proxy_dict(fp, "ron", "pw1"), "127.0.0.1", self.echo.port))
        with self.assertRaisesRegex(core.ProxyError, "username and password"):
            core.proxy_connect(proxy_dict(fp, "ron", "bad"), "127.0.0.1", self.echo.port)

    def test_http_refusal_and_wrong_kind(self):
        fp = self.make(kind="http", http_status=403)
        with self.assertRaisesRegex(core.ProxyError, "refused to connect.*403"):
            core.proxy_connect(proxy_dict(fp), "127.0.0.1", self.echo.port)
        # an HTTP proxy asked to speak SOCKS, and a SOCKS proxy asked to speak HTTP: both say so plainly
        http = self.make(kind="http")
        with self.assertRaises(core.ProxyError):
            core.proxy_connect(dict(proxy_dict(http), kind="socks5"), "127.0.0.1", self.echo.port, timeout=1)
        socks = self.make(kind="socks5")
        with self.assertRaises(core.ProxyError):
            core.proxy_connect(dict(proxy_dict(socks), kind="http"), "127.0.0.1", self.echo.port, timeout=1)

    def test_unreachable_proxy(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaisesRegex(core.ProxyError, "Cannot reach the proxy"):
            core.proxy_connect({"kind": "socks5", "host": "127.0.0.1", "port": port, "user": "", "password_enc": ""},
                               "127.0.0.1", 22, timeout=2)


class FakeApp:
    def __init__(self, hosts):
        self.hosts = hosts
        self.root = None

    def post(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def credentials(self, h, allow_anonymous=False):
        return ("tester", "pw")


@unittest.skipUnless(WIN, "uses DPAPI")
class SshThroughProxyTests(Data):
    def setUp(self):
        super().setUp()
        self._kh = core.KNOWN_HOSTS
        core.KNOWN_HOSTS = os.path.join(tempfile.mkdtemp(prefix="rc_ssh_"), "known_hosts")
        core._ADDR_CACHE.clear()
        self.ssh = SshServer()
        self.addCleanup(self.ssh.close)
        p = mock.patch.object(core.messagebox, "askyesno", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        core.KNOWN_HOSTS = self._kh
        super().tearDown()

    def host(self, **kw):
        return core.sanitize_host(dict({"id": "h1", "name": "srv", "address": "127.0.0.1", "ssh_port": self.ssh.port,
                                        "user": "tester"}, **kw))

    def test_login_goes_through_the_proxy(self):
        for kind in ("socks5", "http"):
            fp = FakeProxy(kind)
            self.addCleanup(fp.close)
            core.save_proxies([{"id": "px", "name": "P", "kind": kind, "host": "127.0.0.1", "port": fp.port}])
            for attempt in range(3):                       # the in-process SSH server can drop a banner when the PC is busy
                try:
                    c = core.open_ssh_client(FakeApp([]), self.host(proxy="px"), "tester", "pw")
                    break
                except Exception:
                    if attempt == 2:
                        raise
            try:
                self.assertTrue(c.get_transport().is_active())
            finally:
                c.close()
            self.assertEqual(fp.requests[-1][1:], ("127.0.0.1", self.ssh.port), kind)

    def test_without_a_proxy_nothing_goes_through_one(self):
        fp = FakeProxy("socks5")
        self.addCleanup(fp.close)
        core.save_proxies([{"id": "px", "name": "P", "kind": "socks5", "host": "127.0.0.1", "port": fp.port}])
        core.open_ssh_client(FakeApp([]), self.host(), "tester", "pw").close()
        core.open_ssh_client(FakeApp([]), self.host(proxy="deleted"), "tester", "pw").close()   # a missing proxy is ignored
        self.assertEqual(fp.requests, [])

    def test_a_broken_proxy_gives_a_clear_error(self):
        fp = FakeProxy("socks5", refuse=2)
        self.addCleanup(fp.close)
        core.save_proxies([{"id": "px", "name": "P", "kind": "socks5", "host": "127.0.0.1", "port": fp.port}])
        with self.assertRaisesRegex(core.ProxyError, "not allowed by the proxy"):
            core.open_ssh_client(FakeApp([]), self.host(proxy="px"), "tester", "pw")

    def test_identity_and_proxy_together(self):
        fp = FakeProxy("socks5")
        self.addCleanup(fp.close)
        core.save_proxies([{"id": "px", "name": "P", "kind": "socks5", "host": "127.0.0.1", "port": fp.port}])
        core.save_identities([{"id": "i1", "name": "Lab", "user": "tester", "auth": "password", "secret_enc": core.protect_password("pw")}])
        h = self.host(proxy="px", identity="i1", user="someone-else")
        user, pw = core.saved_login(h)
        c = core.open_ssh_client(FakeApp([]), h, user, pw)
        c.close()
        self.assertEqual(len(fp.requests), 1)

    def test_status_dot_is_tested_through_the_proxy(self):
        fp = FakeProxy("socks5")
        self.addCleanup(fp.close)
        core.save_proxies([{"id": "px", "name": "P", "kind": "socks5", "host": "127.0.0.1", "port": fp.port}])
        up = core.check_host_status(self.host(proxy="px"))
        self.assertEqual((up["state"], up["port"]), ("up", self.ssh.port))
        down = core.check_host_status(self.host(proxy="px", ssh_port=1, address="127.0.0.1"))
        self.assertEqual(down["state"], "down")
        fp.close()
        self.assertEqual(core.check_host_status(self.host(proxy="px"))["state"], "down")      # proxy itself gone


class FakeMainApp:
    """What the two tabs need from the main window."""

    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.status = tk.StringVar()
        self.hosts = [dict(core.NEW_HOST, id="h1", name="srv1", address="10.0.0.1"),
                      dict(core.NEW_HOST, id="h2", name="srv2", address="10.0.0.2")]
        self.posted = []

    def post(self, fn):
        self.posted.append(fn)


@unittest.skipUnless(WIN, "uses DPAPI")
class TabTests(Data):
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
        super().setUp()
        self.app = FakeMainApp(self.root)
        self._hf = core.save_hosts
        core.save_hosts = lambda hosts: None
        self.answers = {"yes": True}
        self._mb = (ident.messagebox.askyesno, ident.messagebox.showwarning, ident.messagebox.showinfo, ident.messagebox.showerror)
        self.shown = []
        ident.messagebox.askyesno = lambda *a, **k: self.answers["yes"]
        ident.messagebox.showwarning = lambda *a, **k: self.shown.append(("warn", a[1]))
        ident.messagebox.showinfo = lambda *a, **k: self.shown.append(("info", a[1]))
        ident.messagebox.showerror = lambda *a, **k: self.shown.append(("error", a[1]))

    def tearDown(self):
        core.save_hosts = self._hf
        (ident.messagebox.askyesno, ident.messagebox.showwarning, ident.messagebox.showinfo, ident.messagebox.showerror) = self._mb
        self.app.nb.destroy()
        super().tearDown()

    def test_identity_save_edit_and_keep_password(self):
        tab = ident.IdentitiesTab(self.app)
        tab.name.set("Lab")
        tab.user.set("root")
        tab.pw.set("hunter2")
        tab.save()
        got = core.load_identities()
        self.assertEqual((got[0]["name"], got[0]["user"]), ("Lab", "root"))
        self.assertEqual(core.unprotect_password(got[0]["secret_enc"]), "hunter2")
        with open(core.IDENTITIES_FILE) as f:
            self.assertNotIn("hunter2", f.read())                                      # never stored as text
        tab.listbox.selection_set(0)
        tab.pick()
        self.assertEqual(tab.pw.get(), "")                                            # a saved password is never shown again
        tab.user.set("admin")
        tab.save()
        got = core.load_identities()
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["user"], core.unprotect_password(got[0]["secret_enc"])), ("admin", "hunter2"))
        tab.listbox.selection_set(0)
        tab.pick()
        tab.forget_pw.set(True)
        tab.save()
        self.assertEqual(core.load_identities()[0]["secret_enc"], "")

    def test_identity_validation(self):
        tab = ident.IdentitiesTab(self.app)
        tab.user.set("root")
        tab.save()                                                                   # no name
        self.assertTrue(self.shown and self.shown[-1][0] == "warn")
        tab.name.set("K")
        tab.auth.set("key")
        tab.key.set(r"C:\does\not\exist")
        tab.save()
        self.assertEqual(core.load_identities(), [])
        pub = os.path.join(self.dir, "id.pub")
        with open(pub, "w") as f:
            f.write("ssh-ed25519 AAAA")
        tab.key.set(pub)
        tab.save()
        self.assertEqual(core.load_identities(), [])                                 # .pub is refused
        key = os.path.join(self.dir, "id")
        with open(key, "w") as f:
            f.write("-----BEGIN OPENSSH PRIVATE KEY-----")
        tab.key.set(key)
        tab.save()
        self.assertEqual(core.load_identities()[0]["key_file"], key)

    def test_duplicate_names_refused(self):
        tab = ident.IdentitiesTab(self.app)
        tab.name.set("Lab")
        tab.user.set("a")
        tab.save()
        tab.new()
        tab.name.set("lab")
        tab.user.set("b")
        tab.save()
        self.assertEqual(len(core.load_identities()), 1)

    def test_deleting_an_identity_releases_its_servers(self):
        tab = ident.IdentitiesTab(self.app)
        tab.name.set("Lab")
        tab.user.set("a")
        tab.save()
        iid = core.load_identities()[0]["id"]
        self.app.hosts[0]["identity"] = iid
        tab.listbox.selection_set(0)
        tab.pick()
        self.assertIn("srv1", tab.used.get())
        tab.delete()
        self.assertEqual(core.load_identities(), [])
        self.assertEqual(self.app.hosts[0]["identity"], "")

    def test_proxy_save_and_validation(self):
        tab = ident.ProxiesTab(self.app)
        tab.name.set("Office")
        tab.host.set("http://bad")
        tab.save()
        self.assertEqual(core.load_proxies(), [])
        tab.host.set("10.0.0.9")
        tab.port.set("99999")
        tab.save()
        self.assertEqual(core.load_proxies(), [])
        tab.port.set("1080")
        tab.user.set("ron")
        tab.pw.set("pw")
        tab.save()
        p = core.load_proxies()[0]
        self.assertEqual((p["name"], p["kind"], p["host"], p["port"], p["user"]), ("Office", "socks5", "10.0.0.9", 1080, "ron"))
        self.assertEqual(core.unprotect_password(p["password_enc"]), "pw")

    def test_proxy_type_switch_changes_the_default_port(self):
        tab = ident.ProxiesTab(self.app)
        tab.kind_box.set("HTTP (CONNECT)")
        tab.kind_chosen()
        self.assertEqual((tab.kind.get(), tab.port.get()), ("http", "8080"))
        tab.port.set("3128")
        tab.kind_box.set("SOCKS5")
        tab.kind_chosen()
        self.assertEqual(tab.port.get(), "3128")                                     # a port you typed is kept

    def test_proxy_test_button_reports_success_and_failure(self):
        echo = Echo()
        fp = FakeProxy("socks5")
        self.addCleanup(echo.close)
        self.addCleanup(fp.close)
        tab = ident.ProxiesTab(self.app)
        tab.name.set("Local")
        tab.host.set("127.0.0.1")
        tab.port.set(str(fp.port))
        orig = ident.simpledialog.askstring
        try:
            ident.simpledialog.askstring = lambda *a, **k: f"127.0.0.1:{echo.port}"
            tab.test()
            deadline = time.time() + 5
            while not self.app.posted and time.time() < deadline:
                time.sleep(0.05)
            self.app.posted.pop()()
            self.assertEqual(self.shown[-1][0], "info")
            self.assertIn("SSH-2.0-echo", self.shown[-1][1])
            fp.refuse = 2
            tab.test()
            deadline = time.time() + 5
            while not self.app.posted and time.time() < deadline:
                time.sleep(0.05)
            self.app.posted.pop()()
            self.assertEqual(self.shown[-1][0], "warn")
        finally:
            ident.simpledialog.askstring = orig

    def test_host_editor_offers_them(self):
        core.save_identities([{"id": "i1", "name": "Lab", "user": "root"}])
        core.save_proxies([{"id": "p1", "name": "Office", "kind": "socks5", "host": "10.0.0.9", "port": 1080}])
        app = self.app
        app.editors = {}
        app.editor_done = lambda *a: None
        app.open_identities = app.open_proxies = lambda: None
        host = dict(core.NEW_HOST, id="h1", name="srv1", address="10.0.0.1", identity="i1", proxy="p1")
        ed = core.HostEditor(app, host, [], "h1")
        self.assertIn("Lab  (root)", ed.identity_map)
        self.assertEqual(ed.vars["identity"].get(), "Lab  (root)")
        self.assertTrue(ed.vars["proxy"].get().startswith("Office"))
        h, errors, _warn = ed.collect()
        self.assertEqual((h["identity"], h["proxy"]), ("i1", "p1"))
        ed.vars["identity"].set(next(iter(ed.identity_map)))
        ed.vars["proxy"].set(next(iter(ed.proxy_map)))
        h, errors, _warn = ed.collect()
        self.assertEqual((h["identity"], h["proxy"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
