"""Tests for the v1.1 features in rc_manager.py: Wake-on-LAN, alternate addresses, live status, favorites/recents
fields, trusted host keys, and jump hosts / address fallback over a real (in-process) SSH server.
Run from the RC folder:  python -m unittest discover -s tests -v"""

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="rc_feat_")
os.environ.setdefault("RC_DATA_DIR", _TMP)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paramiko  # noqa: E402
import rc_manager as rc  # noqa: E402

DEAD = "203.0.113.77"  # TEST-NET-3: never answers


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Listener:
    def __init__(self, banner=b"SSH-2.0-Test\r\n"):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.banner = banner
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            try:
                c.sendall(self.banner)
            finally:
                c.close()

    def close(self):
        self.sock.close()


# ─── Wake-on-LAN ─────────────────────────────────────────────────────────────

class WakeTests(unittest.TestCase):
    def test_mac_normalising(self):
        for raw in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "aabbccddeeff", " AA:BB:CC:DD:EE:FF "):
            self.assertEqual(rc.normalize_mac(raw), "AA:BB:CC:DD:EE:FF", raw)

    def test_bad_macs(self):
        for raw in ("", "aa:bb:cc", "zz:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff:00", None, 12345, "aa:bb:cc:dd:ee:fg"):
            self.assertIsNone(rc.normalize_mac(raw), raw)

    def test_magic_packet_layout(self):
        pkt = rc.wol_packet("01:23:45:67:89:ab")
        self.assertEqual(len(pkt), 102)
        self.assertEqual(pkt[:6], b"\xff" * 6)
        self.assertEqual(pkt[6:], bytes.fromhex("0123456789ab") * 16)
        with self.assertRaises(ValueError):
            rc.wol_packet("nope")

    def test_send_goes_to_broadcast_and_subnet(self):
        sent = []

        class FakeSock:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def setsockopt(self, *a): pass
            def sendto(self, data, addr): sent.append((data, addr))
        with mock.patch.object(rc.socket, "socket", lambda *a, **k: FakeSock()):
            dests = rc.send_wol("aa:bb:cc:dd:ee:ff", "192.168.1.20")
        self.assertEqual(sorted(a for _, a in sent), [("192.168.1.255", 7), ("192.168.1.255", 9),
                                                      ("255.255.255.255", 7), ("255.255.255.255", 9)])
        self.assertTrue(all(d == rc.wol_packet("aa:bb:cc:dd:ee:ff") for d, _ in sent))
        self.assertEqual(len(dests), 4)

    def test_dns_name_target_skips_subnet_broadcast(self):
        sent = []

        class FakeSock:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def setsockopt(self, *a): pass
            def sendto(self, data, addr): sent.append(addr)
        with mock.patch.object(rc.socket, "socket", lambda *a, **k: FakeSock()):
            rc.send_wol("aa:bb:cc:dd:ee:ff", "zimaos.lan")
        self.assertEqual({a[0] for a in sent}, {"255.255.255.255"})


# ─── New host fields ─────────────────────────────────────────────────────────

class HostFieldTests(unittest.TestCase):
    def test_defaults_exist(self):
        h = rc.sanitize_host({"address": "a.lan"})
        self.assertEqual((h["favorite"], h["last_connected"], h["mac"], h["alt_addresses"], h["jump_host"]),
                         (False, 0, "", [], ""))

    def test_values_are_cleaned(self):
        h = rc.sanitize_host({"address": "a.lan", "favorite": 1, "last_connected": "1700000000", "mac": "aa-bb-cc-dd-ee-ff",
                              "alt_addresses": "10.0.0.5, 172.26.0.1;bad host  a.lan", "jump_host": 5})
        self.assertIs(h["favorite"], True)
        self.assertEqual(h["last_connected"], 1700000000.0)
        self.assertEqual(h["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(h["alt_addresses"], ["10.0.0.5", "172.26.0.1", "bad", "host"])  # 'a.lan' is the main address: dropped
        self.assertEqual(h["jump_host"], "")

    def test_bad_mac_and_time_are_dropped(self):
        problems = []
        h = rc.sanitize_host({"address": "a.lan", "mac": "oops", "last_connected": "soon"}, problems)
        self.assertEqual((h["mac"], h["last_connected"]), ("", 0))
        self.assertTrue(any("MAC" in p for p in problems))

    def test_validation_messages(self):
        h = rc.sanitize_host({"address": "a.lan", "id": "1"})
        h.update(alt_addresses=["999.1.1.1"], mac="12:34", jump_host="1")
        errors, _ = rc.validate_host(h)
        text = " ".join(errors)
        self.assertIn("999.1.1.1", text)
        self.assertIn("MAC", text)
        self.assertIn("own jump", text)

    def test_connection_list_has_wake(self):
        h = rc.sanitize_host({"address": "a.lan", "mac": "aa:bb:cc:dd:ee:ff"})
        wake = [c for c in rc.connection_list(h) if c["action"] == "wake"]
        self.assertEqual(wake[0]["detail"], "AA:BB:CC:DD:EE:FF")


# ─── Address fallback and live status ────────────────────────────────────────

class AddressTests(unittest.TestCase):
    def setUp(self):
        rc._ADDR_CACHE.clear()

    def test_probe_states(self):
        srv = Listener()
        self.addCleanup(srv.close)
        self.assertEqual(rc.tcp_probe("127.0.0.1", srv.port), "open")
        # a closed local port can take ~2 s to be refused on Windows servers (CI), so allow plenty of time
        self.assertEqual(rc.tcp_probe("127.0.0.1", free_port(), 8), "refused")
        self.assertIsNone(rc.tcp_probe(DEAD, 22, 0.3))

    def test_no_alternates_returns_main_without_probing(self):
        with mock.patch.object(rc, "tcp_probe", side_effect=AssertionError("should not probe")):
            self.assertEqual(rc.resolve_address({"address": "a.lan"}), "a.lan")

    def test_falls_back_in_order(self):
        srv = Listener()
        self.addCleanup(srv.close)
        h = {"address": DEAD, "alt_addresses": ["203.0.113.78", "127.0.0.1"], "ssh_port": srv.port}
        self.assertEqual(rc.resolve_address(h, timeout=0.3), "127.0.0.1")

    def test_main_address_wins_when_it_answers(self):
        srv = Listener()
        self.addCleanup(srv.close)
        h = {"address": "127.0.0.1", "alt_addresses": [DEAD], "ssh_port": srv.port}
        self.assertEqual(rc.resolve_address(h, timeout=0.3), "127.0.0.1")

    def test_all_dead_uses_main_and_result_is_cached(self):
        h = {"address": DEAD, "alt_addresses": ["203.0.113.78"], "ssh_port": 22}
        self.assertEqual(rc.resolve_address(h, timeout=0.2), DEAD)
        with mock.patch.object(rc, "tcp_probe", side_effect=AssertionError("cache not used")):
            self.assertEqual(rc.resolve_address(h, timeout=0.2), DEAD)

    def test_status_up_down_refused_and_alt(self):
        srv = Listener()
        self.addCleanup(srv.close)
        up = rc.check_host_status({"address": "127.0.0.1", "alt_addresses": [], "ssh_port": srv.port, "rdp_port": 1})
        self.assertEqual((up["state"], up["service"], up["via_alt"]), ("up", True, False))
        refused = rc.check_host_status({"address": "127.0.0.1", "ssh_port": free_port(), "rdp_port": free_port()})
        self.assertEqual((refused["state"], refused["service"]), ("up", False))   # a refusal proves the machine is up
        alt = rc.check_host_status({"address": DEAD, "alt_addresses": ["127.0.0.1"], "ssh_port": srv.port,
                                    "rdp_port": srv.port}, timeout=0.3)
        self.assertEqual((alt["state"], alt["addr"], alt["via_alt"]), ("up", "127.0.0.1", True))
        with mock.patch.object(rc, "_ping_ms", return_value=None):
            down = rc.check_host_status({"address": DEAD, "ssh_port": 22, "rdp_port": 3389}, timeout=0.3)
        self.assertEqual(down["state"], "down")

    def test_ping_fallback_marks_up(self):
        with mock.patch.object(rc, "_ping_ms", return_value=7):
            r = rc.check_host_status({"address": DEAD, "ssh_port": 22, "rdp_port": 3389}, timeout=0.2)
        self.assertEqual((r["state"], r["ms"], r["service"]), ("up", 7, False))

    def test_monitor_round_updates_and_logs_changes(self):
        srv = Listener()
        self.addCleanup(srv.close)
        hosts = [{"id": "aaaaaaaa1", "address": "127.0.0.1", "ssh_port": srv.port, "rdp_port": 1, "alt_addresses": []},
                 {"id": "bbbbbbbb2", "address": DEAD, "ssh_port": 22, "rdp_port": 3389, "alt_addresses": []}]
        seen = {}
        mon = rc.StatusMonitor(lambda: hosts, lambda i, r: seen.update({i: r["state"]}))
        orig = rc.check_host_status
        with mock.patch.object(rc, "_ping_ms", return_value=None), \
                mock.patch.object(rc, "check_host_status", lambda h, timeout=0.3: orig(h, 0.3)):
            mon.round()
            self.assertEqual(seen, {"aaaaaaaa1": "up", "bbbbbbbb2": "down"})
            self.assertEqual(mon.summary([{"id": "aaaaaaaa1"}, {"id": "bbbbbbbb2"}, {"id": "cccccccc3"}]), (1, 1, 1))
            hosts[1]["address"] = "127.0.0.1"
            hosts[1]["ssh_port"] = srv.port
            with self.assertLogs("rc", level="INFO") as cm:
                mon.round()
        self.assertTrue(any("is now UP" in m for m in cm.output))

    def test_monitor_stop_wakes_the_thread(self):
        mon = rc.StatusMonitor(lambda: [], lambda i, r: None, interval=3600)
        mon.start()
        time.sleep(0.2)
        mon.stop()
        mon.thread.join(2)
        self.assertFalse(mon.thread.is_alive())

    def test_ago(self):
        now = 1_000_000
        self.assertEqual(rc.ago(0, now), "never")
        self.assertEqual(rc.ago(now - 10, now), "just now")
        self.assertEqual(rc.ago(now - 300, now), "5 min ago")
        self.assertEqual(rc.ago(now - 7200, now), "2 h ago")
        self.assertEqual(rc.ago(now - 172800, now), "2 d ago")


# ─── Trusted host keys ───────────────────────────────────────────────────────

class KnownHostsTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="rc_kh_"), "known_hosts")
        keys = paramiko.HostKeys()
        self.k1, self.k2 = paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)
        keys.add("nas.lan", "ssh-rsa", self.k1)
        keys.add("plex.lan", "ssh-rsa", self.k2)
        keys.save(self.path)

    def test_list_matches_fingerprints(self):
        rows = rc.list_known_hosts(self.path)
        self.assertEqual([r["host"] for r in rows], ["nas.lan", "plex.lan"])
        self.assertEqual(rows[0]["fp"], rc.fingerprint(self.k1))
        self.assertTrue(rows[0]["fp"].startswith("SHA256:"))

    def test_remove_one_host(self):
        self.assertEqual(rc.remove_known_host("nas.lan", path=self.path), 1)
        self.assertEqual([r["host"] for r in rc.list_known_hosts(self.path)], ["plex.lan"])

    def test_remove_by_key_type_and_unknown(self):
        self.assertEqual(rc.remove_known_host("plex.lan", "ssh-ed25519", path=self.path), 0)
        self.assertEqual(rc.remove_known_host("plex.lan", "ssh-rsa", path=self.path), 1)
        self.assertEqual(rc.remove_known_host("ghost.lan", path=self.path), 0)

    def test_missing_file(self):
        self.assertEqual(rc.list_known_hosts(os.path.join(self.path, "nope")), [])


# ─── Jump hosts and alternate addresses over real SSH (in-process servers) ───

class _Server(paramiko.ServerInterface):
    def __init__(self):
        self.destinations = []

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == "pw" else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.destinations.append(destination)
        return paramiko.OPEN_SUCCEEDED


def _pipe(a, b):
    try:
        while True:
            data = a.recv(4096)
            if not data:
                break
            b.sendall(data)
    except Exception:
        pass
    finally:
        for x in (a, b):
            try:
                x.close()
            except Exception:
                pass


class SshServer:
    """A tiny SSH server that accepts user 'tester' / password 'pw' and forwards direct-tcpip channels."""

    def __init__(self):
        self.key = paramiko.RSAKey.generate(1024)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.servers = []
        self.transports = []
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.key)
            srv = _Server()
            self.servers.append(srv)
            self.transports.append(t)
            try:
                t.start_server(server=srv)
            except Exception:
                continue
            threading.Thread(target=self.channels, args=(t, srv), daemon=True).start()

    def channels(self, t, srv):
        while t.is_active():
            chan = t.accept(1)
            if chan is None:
                continue
            if srv.destinations:
                host, port = srv.destinations[-1]
                try:
                    out = socket.create_connection((host, port), 5)
                except OSError:
                    chan.close()
                    continue
                threading.Thread(target=_pipe, args=(chan, out), daemon=True).start()
                threading.Thread(target=_pipe, args=(out, chan), daemon=True).start()

    def close(self):
        self.sock.close()
        for t in self.transports:
            t.close()


class FakeApp:
    def __init__(self, hosts):
        self.hosts = hosts
        self.root = None

    def post(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def credentials(self, h, allow_anonymous=False):  # only reached if a jump host has no saved login
        return ("tester", "pw")


@unittest.skipUnless(sys.platform == "win32", "uses DPAPI for the jump server's saved login")
class SshPathTests(unittest.TestCase):
    def setUp(self):
        self._kh = rc.KNOWN_HOSTS
        rc.KNOWN_HOSTS = os.path.join(tempfile.mkdtemp(prefix="rc_ssh_"), "known_hosts")
        rc._ADDR_CACHE.clear()
        self.target, self.jump = SshServer(), SshServer()
        self.addCleanup(self.target.close)
        self.addCleanup(self.jump.close)
        patcher = mock.patch.object(rc.messagebox, "askyesno", return_value=True)  # "trust this server?" -> yes
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        rc.KNOWN_HOSTS = self._kh

    def host(self, **kw):
        h = rc.sanitize_host({"id": "target-id", "name": "target", "address": "127.0.0.1", "ssh_port": self.target.port,
                              "user": "tester"})
        h.update(kw)
        return h

    def test_direct_login_works(self):
        c = rc.open_ssh_client(FakeApp([]), self.host(), "tester", "pw")
        self.assertTrue(c.get_transport().is_active())
        c.close()

    def test_wrong_password_fails(self):
        with self.assertRaises(paramiko.AuthenticationException):
            rc.open_ssh_client(FakeApp([]), self.host(), "tester", "nope")

    def test_jump_host_carries_the_connection(self):
        jump = rc.sanitize_host({"id": "jump-id", "name": "jumpbox", "address": "127.0.0.1", "ssh_port": self.jump.port,
                                 "user": "tester", "password_enc": rc.protect_password("pw")})
        target = self.host(jump_host="jump-id", address="127.0.0.1")
        c = rc.open_ssh_client(FakeApp([jump, target]), target, "tester", "pw")
        try:
            self.assertTrue(c.get_transport().is_active())
            # the jump server was asked to open a channel to the target's port; the target saw a login
            self.assertIn(("127.0.0.1", self.target.port), [d for s in self.jump.servers for d in s.destinations])
        finally:
            c.close()
        time.sleep(0.2)
        self.assertTrue(all(not t.is_active() for t in self.jump.transports), "closing the target must close the jump link")

    def test_jump_loop_is_not_followed(self):
        a = self.host(id="a", jump_host="b")
        b = rc.sanitize_host({"id": "b", "name": "b", "address": "127.0.0.1", "ssh_port": self.jump.port, "user": "tester",
                              "password_enc": rc.protect_password("pw"), "jump_host": "a"})
        c = rc.open_ssh_client(FakeApp([a, b]), a, "tester", "pw")  # b -> a is cut, so the chain ends instead of looping
        c.close()

    def test_alternate_address_used_and_key_saved_under_main_name(self):
        h = self.host(address=DEAD, alt_addresses=["127.0.0.1"])
        c = rc.open_ssh_client(FakeApp([]), h, "tester", "pw")
        try:
            self.assertTrue(c.get_transport().is_active())
        finally:
            c.close()
        hosts = " ".join(r["host"] for r in rc.list_known_hosts())
        self.assertIn(DEAD, hosts)
        self.assertNotIn("127.0.0.1", hosts)


if __name__ == "__main__":
    unittest.main()
