"""Unit tests for rc_manager.py (REGTeches RC).   Run from the RC folder:  python -m unittest discover -s tests -v

No network access beyond 127.0.0.1 and no real hosts.json: everything runs in a temp folder.
The DPAPI tests need Windows (they are skipped elsewhere).
"""

import base64
import hashlib
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import unittest

_TMP = tempfile.mkdtemp(prefix="rc_test_")
os.environ["RC_DATA_DIR"] = _TMP  # must be set before rc_manager is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as rc  # noqa: E402


class FakeKey:
    """Stands in for a paramiko key: fingerprint() only needs asbytes()."""
    def __init__(self, raw):
        self.raw = raw

    def asbytes(self):
        return self.raw


class TempData(unittest.TestCase):
    """Points HOSTS_FILE / SETTINGS_FILE at a fresh folder for each test."""
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc_case_")
        self._old = (rc.HOSTS_FILE, rc.SETTINGS_FILE, rc.SNIPPETS_FILE)
        rc.HOSTS_FILE = os.path.join(self.dir, "hosts.json")
        rc.SETTINGS_FILE = os.path.join(self.dir, "settings.json")
        rc.SNIPPETS_FILE = os.path.join(self.dir, "snippets.json")

    def tearDown(self):
        rc.HOSTS_FILE, rc.SETTINGS_FILE, rc.SNIPPETS_FILE = self._old

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


# ─── SSH key fingerprinting ──────────────────────────────────────────────────

class FingerprintTests(unittest.TestCase):
    def test_matches_openssh_format(self):
        raw = b"\x00\x00\x00\x0bssh-ed25519" + bytes(range(32))
        expect = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        self.assertEqual(rc.fingerprint(FakeKey(raw)), expect)

    def test_no_base64_padding_and_fixed_length(self):
        fp = rc.fingerprint(FakeKey(b"abc"))
        self.assertTrue(fp.startswith("SHA256:"))
        self.assertNotIn("=", fp)
        self.assertEqual(len(fp), len("SHA256:") + 43)  # a SHA-256 digest is always 43 base64 chars unpadded

    def test_different_keys_differ(self):
        self.assertNotEqual(rc.fingerprint(FakeKey(b"one")), rc.fingerprint(FakeKey(b"two")))


# ─── Credential handling ─────────────────────────────────────────────────────

@unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows only")
class CredentialTests(unittest.TestCase):
    def test_round_trip(self):
        blob = rc.protect_password("pässword ✔ 123")
        self.assertEqual(rc.unprotect_password(blob), "pässword ✔ 123")

    def test_blob_is_not_plaintext(self):
        blob = rc.protect_password("hunter2")
        self.assertNotIn("hunter2", blob)
        self.assertNotIn(b"hunter2", base64.b64decode(blob))

    def test_garbage_blob_gives_empty_string_not_crash(self):
        self.assertEqual(rc.unprotect_password("not base64 at all!!"), "")
        self.assertEqual(rc.unprotect_password(base64.b64encode(b"junk").decode()), "")

    def test_saved_login(self):
        h = {"user": "ron", "password_enc": rc.protect_password("secret")}
        self.assertEqual(rc.saved_login(h), ("ron", "secret"))
        self.assertIsNone(rc.saved_login({"user": "", "password_enc": h["password_enc"]}))  # no user
        self.assertIsNone(rc.saved_login({"user": "ron", "password_enc": ""}))
        self.assertIsNone(rc.saved_login({"user": "ron", "password_enc": "garbage"}))

    def test_password_never_reaches_the_log(self):
        path = os.path.join(_TMP, "pwlog.log")
        handler = logging.FileHandler(path, encoding="utf-8")
        rc.log.addHandler(handler)
        old_level = rc.log.level
        rc.log.setLevel(logging.DEBUG)
        try:
            rc.unprotect_password(rc.protect_password("TopSecret-9"))
            rc.unprotect_password("garbage")
        finally:
            rc.log.removeHandler(handler)
            rc.log.setLevel(old_level)
            handler.close()
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("TopSecret-9", f.read())

    def test_variant_with_other_user_drops_saved_password(self):
        h = {"user": "ron", "password_enc": "ENC"}
        v = rc.variant(h, {"user": "admin"})
        self.assertEqual(v["password_enc"], "")
        self.assertTrue(v["_nosave"])
        self.assertIs(v["_src"], h)

    def test_variant_with_own_password_keeps_it(self):
        v = rc.variant({"user": "ron", "password_enc": "ENC"}, {"password_enc": "OTHER"})
        self.assertEqual(v["password_enc"], "OTHER")
        self.assertTrue(v["_nosave"])


# ─── Address / host validation ───────────────────────────────────────────────

class ValidationTests(unittest.TestCase):
    def host(self, **kw):
        h = dict(rc.NEW_HOST, id="1", name="x", address="192.168.1.1")
        h.update(kw)
        return h

    def test_valid_addresses(self):
        for a in ("192.168.1.1", "example.com", "rgplex", "ZimaOS.lan", "::1", "[fe80::1]", "a-b.example.org."):
            self.assertTrue(rc.valid_address(a), a)

    def test_invalid_addresses(self):
        for a in ("", "192.168.1.300", "1.2.3", "-bad.com", "bad-.com", "a..b", "x" * 64 + ".com", "has space"):
            self.assertFalse(rc.valid_address(a), a)

    def test_host_errors(self):
        for addr, word in (("user@host", "@"), ("host:22", "port"), ("999.1.1.1", "valid"), ("a b", "spaces")):
            errors, _ = rc.validate_host(self.host(address=addr))
            self.assertTrue(errors and word in errors[0], (addr, errors))

    def test_clean_host_has_no_errors(self):
        self.assertEqual(rc.validate_host(self.host()), ([], []))

    def test_public_key_rejected(self):
        p = os.path.join(_TMP, "id.pub")
        with open(p, "w") as f:
            f.write("ssh-ed25519 AAAA")
        errors, _ = rc.validate_host(self.host(key_file=p))
        self.assertTrue(any(".pub" in e for e in errors))

    def test_missing_key_file(self):
        errors, _ = rc.validate_host(self.host(key_file=os.path.join(_TMP, "nope")))
        self.assertTrue(any("doesn't exist" in e for e in errors))

    def test_duplicate_warning(self):
        other = self.host(id="2")
        _, warns = rc.validate_host(self.host(), [other])
        self.assertTrue(warns)

    def test_bad_web_url(self):
        errors, _ = rc.validate_host(self.host(web_url="ftp://x"))
        self.assertTrue(errors)


# ─── Config loading, repair and versioning ───────────────────────────────────

class ConfigTests(TempData):
    def test_port_helper(self):
        self.assertEqual(rc._port("2222", 22), 2222)
        for bad in (0, -1, 70000, "abc", None, ""):
            self.assertEqual(rc._port(bad, 22), 22, bad)

    def test_sanitize_repairs_values(self):
        problems = []
        h = rc.sanitize_host({"address": "a", "ssh_port": "99999", "file_mode": "SCP",
                              "default_action": "fly", "user": None, "tunnels": [{"kind": "bogus"}]}, problems)
        self.assertEqual((h["ssh_port"], h["file_mode"], h["default_action"], h["user"], h["tunnels"]),
                         (22, "SFTP", "ssh", "", []))
        self.assertEqual(len(problems), 3)
        self.assertTrue(h["id"])

    def test_missing_file_is_empty(self):
        self.assertEqual(rc.load_hosts(), [])

    def test_save_load_round_trip_and_version(self):
        h = rc.sanitize_host({"address": "10.0.0.5", "name": "box"})
        rc.save_hosts([h])
        with open(rc.HOSTS_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], rc.HOSTS_VERSION)
        loaded = rc.load_hosts()
        self.assertEqual([x["address"] for x in loaded], ["10.0.0.5"])
        self.assertFalse(os.path.exists(rc.HOSTS_FILE + ".tmp"))

    def test_old_list_format_still_loads(self):
        self.write(rc.HOSTS_FILE, json.dumps([{"address": "old.lan", "name": "old"}]))
        self.assertEqual(rc.load_hosts()[0]["address"], "old.lan")

    def test_entries_without_address_are_skipped(self):
        self.write(rc.HOSTS_FILE, json.dumps({"hosts": [{"name": "nope"}, "junk", {"address": "ok.lan"}]}))
        self.assertEqual([h["address"] for h in rc.load_hosts()], ["ok.lan"])

    def test_corrupt_file_is_set_aside_not_lost(self):
        self.write(rc.HOSTS_FILE, "{ this is not json")
        self.assertEqual(rc.load_hosts(), [])
        self.assertFalse(os.path.exists(rc.HOSTS_FILE))
        self.assertTrue(any(n.startswith("hosts.json.corrupt-") for n in os.listdir(self.dir)))

    def test_newer_version_warns_but_loads(self):
        self.write(rc.HOSTS_FILE, json.dumps({"version": rc.HOSTS_VERSION + 5, "hosts": [{"address": "f.lan"}]}))
        with self.assertLogs("rc", level="WARNING") as cm:
            hosts = rc.load_hosts()
        self.assertEqual(len(hosts), 1)
        self.assertTrue(any("newer" in m for m in cm.output))

    def test_settings_check(self):
        self.assertEqual(rc.check_settings_file(), [])  # no file is fine
        self.write(rc.SETTINGS_FILE, "{ broken")
        self.assertTrue(rc.check_settings_file())
        self.write(rc.SETTINGS_FILE, "[1, 2]")
        self.assertTrue(rc.check_settings_file())
        self.write(rc.SETTINGS_FILE, json.dumps({"theme": "Royal Dark"}))
        self.assertEqual(rc.check_settings_file(), [])

    def test_snippets_fall_back_to_defaults(self):
        self.assertEqual(rc.load_snippets()[0]["id"], "df")
        self.write(rc.SNIPPETS_FILE, "oops")
        self.assertEqual(rc.load_snippets()[0]["id"], "df")


# ─── Connection list ─────────────────────────────────────────────────────────

class ConnectionListTests(unittest.TestCase):
    def test_builtin_and_extra_connections(self):
        h = rc.sanitize_host({"address": "box.lan", "connections": [
            {"type": "ftp", "port": 2121}, {"type": "web", "url": "https://box.lan:8443"}, {"type": "bogus"}]})
        conns = rc.connection_list(h)
        self.assertEqual([c["action"] for c in conns[:14]],
                         ["ssh", "files", "stats", "rdp", "vnc", "rustdesk", "web", "ps", "ssh_ext", "mail", "smb", "ping", "wake", "verify"])
        extras = [c for c in conns if c.get("extra")]
        self.assertEqual(len(extras), 2)  # the bogus type was dropped by sanitize_host
        self.assertEqual(extras[0]["over"], {"file_mode": "FTP", "file_port": 2121})

    def test_web_url_default(self):
        self.assertEqual(rc.web_url_for({"address": "a.lan", "web_url": ""}), "http://a.lan")
        self.assertEqual(rc.web_url_for({"address": "a.lan", "web_url": "a.lan:8080"}), "http://a.lan:8080")


# ─── Connection testing (probe_host) ─────────────────────────────────────────

class Listener:
    """A throwaway TCP server on 127.0.0.1 that greets each client with `banner`."""
    def __init__(self, banner=b""):
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
                if self.banner:
                    c.sendall(self.banner)
            finally:
                c.close()

    def close(self):
        self.sock.close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ProbeTests(unittest.TestCase):
    def test_tcp_reads_banner(self):
        srv = Listener(b"SSH-2.0-Test\r\n")
        self.addCleanup(srv.close)
        self.assertEqual(rc._tcp("127.0.0.1", srv.port), "SSH-2.0-Test")

    def test_tcp_refused_raises(self):
        with self.assertRaises(OSError):
            rc._tcp("127.0.0.1", free_port(), timeout=1)

    def test_probe_reports_ssh_and_refused_ports(self):
        srv = Listener(b"SSH-2.0-OpenSSH_9.6\r\n")
        self.addCleanup(srv.close)
        h = rc.sanitize_host({"address": "127.0.0.1", "ssh_port": srv.port, "rdp_port": free_port(),
                              "web_url": f"http://127.0.0.1:{free_port()}/"})
        results = rc.probe_host(h)
        text = rc.format_probe(results)
        self.assertEqual(results[0][0], "ok")           # name resolved
        self.assertIn("ms)", results[0][1])             # DNS timing shown
        self.assertIn("SSH", text)
        self.assertIn("OpenSSH_9.6", text)              # banner read
        self.assertIn("refused", text)                  # closed RDP port
        self.assertTrue(any(s == "ok" and "SSH" in t for s, t in results))

    def test_probe_flags_non_ssh_service(self):
        srv = Listener(b"HELLO\r\n")
        self.addCleanup(srv.close)
        h = rc.sanitize_host({"address": "127.0.0.1", "ssh_port": srv.port, "rdp_port": free_port(),
                              "web_url": f"http://127.0.0.1:{free_port()}/"})
        self.assertIn("doesn't look like SSH", rc.format_probe(rc.probe_host(h)))

    def test_probe_unresolvable_name(self):
        real = socket.getaddrinfo

        def fail(*a, **k):
            raise socket.gaierror("nope")
        socket.getaddrinfo = fail
        try:
            results = rc.probe_host(rc.sanitize_host({"address": "no-such-host.invalid"}))
        finally:
            socket.getaddrinfo = real
        self.assertEqual(results[0][0], "fail")
        self.assertIn("doesn't resolve", results[0][1])

    def test_format_probe_marks(self):
        self.assertEqual(rc.format_probe([("ok", "a"), ("warn", "b"), ("fail", "c")]), "✔ a\n⚠ b\n✖ c")


# ─── Threads, shared files, dependencies ─────────────────────────────────────

class FakeApp:
    """post() runs the function on a helper thread, like App.pump would on the UI thread."""
    def post(self, fn):
        threading.Thread(target=fn, daemon=True).start()


class DroppedApp:
    def post(self, fn):
        pass  # the UI never answers


class ThreadingTests(TempData):
    def test_ui_call_returns_result(self):
        self.assertEqual(rc.ui_call(FakeApp(), lambda: 42), 42)

    def test_ui_call_logs_exception_and_returns_none(self):
        def boom():
            raise RuntimeError("dialog failed")
        with self.assertLogs("rc", level="ERROR") as cm:
            self.assertIsNone(rc.ui_call(FakeApp(), boom))
        self.assertTrue(any("dialog failed" in m for m in cm.output))

    def test_ui_call_times_out_instead_of_hanging(self):
        with self.assertLogs("rc", level="ERROR"):
            self.assertIsNone(rc.ui_call(DroppedApp(), lambda: 1, timeout=0.2))

    def test_concurrent_saves_leave_valid_file_and_no_temp(self):
        hosts = [rc.sanitize_host({"address": f"h{i}.lan"}) for i in range(20)]
        errors = []

        def save():
            try:
                for _ in range(10):
                    rc.save_hosts(hosts)
            except Exception as e:  # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=save) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(rc.load_hosts()), 20)
        self.assertFalse(os.path.exists(rc.HOSTS_FILE + ".tmp"))

    def test_failed_save_cleans_temp_file(self):
        with self.assertRaises(TypeError):
            rc.save_hosts([{"address": object()}])  # not JSON serialisable
        self.assertFalse(os.path.exists(rc.HOSTS_FILE + ".tmp"))

    def test_locks_are_reentrant(self):
        with rc.HOSTS_LOCK, rc.HOSTS_LOCK, rc.KNOWN_HOSTS_LOCK, rc.KNOWN_HOSTS_LOCK:
            pass


class DependencyTests(unittest.TestCase):
    def test_current_install_is_ok(self):
        if rc.paramiko is None:
            self.skipTest("paramiko not installed")
        self.assertFalse([p for p in rc.check_dependencies() if p.startswith("paramiko")])

    def test_missing_paramiko_reported(self):
        real, rc.paramiko = rc.paramiko, None
        try:
            with self.assertLogs("rc", level="WARNING"):
                self.assertTrue(any("not installed" in p for p in rc.check_dependencies()))
        finally:
            rc.paramiko = real

    def test_old_paramiko_reported(self):
        class Old:
            __version__ = "2.1.0"
        real, rc.paramiko = rc.paramiko, Old
        try:
            with self.assertLogs("rc", level="WARNING"):
                self.assertTrue(any("older than" in p for p in rc.check_dependencies()))
        finally:
            rc.paramiko = real


# ─── Logging and timing ──────────────────────────────────────────────────────

class LoggingTests(unittest.TestCase):
    def test_timed_records_elapsed_and_logs(self):
        with self.assertLogs("rc", level="INFO") as cm:
            with rc.timed("Thing", "detail") as t:
                pass
        self.assertGreaterEqual(t.ms, 0)
        self.assertIn("Thing detail - ok in", cm.output[0])

    def test_timed_logs_failure_and_reraises(self):
        with self.assertLogs("rc", level="WARNING") as cm:
            with self.assertRaises(ValueError):
                with rc.timed("Boom", "x"):
                    raise ValueError("bad")
        self.assertIn("failed: bad", cm.output[0])

    def test_timed_flags_slow(self):
        with self.assertLogs("rc", level="WARNING") as cm:
            with rc.timed("Slow", "x", slow_ms=0):
                pass
        self.assertIn("SLOW", cm.output[0])

    def test_setup_logging_writes_file_once(self):
        path = os.path.join(_TMP, "setup.log")
        before = len(rc.log.handlers)
        try:
            self.assertEqual(rc.setup_logging(path), path)
            self.assertEqual(rc.setup_logging(path), path)  # second call adds no second handler
            self.assertEqual(len(rc.log.handlers), before + 1)
            rc.log.info("hello from test")
        finally:
            for h in rc.log.handlers[before:]:
                h.close()
                rc.log.removeHandler(h)
        with open(path, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("starting", body)
        self.assertIn("hello from test", body)


if __name__ == "__main__":
    unittest.main()
