"""Tests for rc_backup.py: the encryption, making a bundle on one 'PC' and restoring it on another, the merge rules and
the two dialogs. All data lives in throw-away folders.  Run from the RC folder:  python -m unittest discover -s tests -v"""

import base64
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_backup_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as core  # noqa: E402
import rc_backup as bk  # noqa: E402

WIN = sys.platform == "win32"
FILES = ("HOSTS_FILE", "SNIPPETS_FILE", "WORKSPACES_FILE", "IDENTITIES_FILE", "PROXIES_FILE", "KNOWN_HOSTS", "APP_DIR")


class Pc(unittest.TestCase):
    """Gives a test its own empty 'PC': every data file points into a new folder."""

    def setUp(self):
        self._old = {k: getattr(core, k) for k in FILES}
        core._LIST_CACHE.clear()
        self.new_pc()

    def tearDown(self):
        for k, v in self._old.items():
            setattr(core, k, v)
        core._LIST_CACHE.clear()

    def new_pc(self):
        d = tempfile.mkdtemp(prefix="rc_pc_")
        core.APP_DIR = d
        for name, fn in (("HOSTS_FILE", "hosts.json"), ("SNIPPETS_FILE", "snippets.json"), ("WORKSPACES_FILE", "workspaces.json"),
                         ("IDENTITIES_FILE", "identities.json"), ("PROXIES_FILE", "proxies.json"), ("KNOWN_HOSTS", "known_hosts")):
            setattr(core, name, os.path.join(d, fn))
        core._LIST_CACHE.clear()
        return d


def host(**kw):
    return core.sanitize_host(dict({"name": "nas", "address": "10.0.0.2", "user": "admin"}, **kw))


@unittest.skipUnless(WIN, "uses DPAPI")
class RoundTripTests(Pc):
    def test_make_encrypt_decrypt_restore_on_another_pc(self):
        h1 = host(id="h1", password_enc=core.protect_password("nas-pw"), jump_host="h2",
                  connections=[{"type": "web", "url": "http://x", "user": "u", "password_enc": core.protect_password("web-pw")}],
                  rustdesk_password_enc=core.protect_password("rd-pw"), _src={"junk": 1})
        h2 = host(id="h2", name="jump", address="10.0.0.1")
        core.save_snippets([{"id": "s1", "name": "Disk", "command": "df -h", "group": "Sys"}])
        core.save_workspaces([{"name": "Lab", "layout": "cols", "hosts": ["h1", "h2"]}])
        core.save_identities([{"id": "i1", "name": "Admin", "user": "root", "auth": "password", "secret_enc": core.protect_password("id-pw")}])
        core.save_proxies([{"id": "p1", "name": "Office", "kind": "socks5", "host": "10.9.9.9", "port": 1080, "user": "ron",
                            "password_enc": core.protect_password("px-pw")}])
        with open(core.KNOWN_HOSTS, "w") as f:
            f.write("10.0.0.2 ssh-ed25519 AAAAkey\n")
        bundle, lost = bk.make_bundle([h1, h2])
        self.assertEqual(lost, 0)
        blob = bk.encrypt(bundle, "correct horse")
        for secret in (b"nas-pw", b"web-pw", b"rd-pw", b"id-pw", b"px-pw", b"AAAAkey", b"10.0.0.2"):
            self.assertNotIn(secret, blob)                                   # nothing readable in the file
        # ---- another PC ----
        self.new_pc()
        hosts, report = bk.apply_bundle(bk.decrypt(blob, "correct horse"), [], replace=False)
        self.assertEqual(report["servers"], (2, 0, 0))
        got = {h["id"]: h for h in hosts}
        self.assertEqual(core.unprotect_password(got["h1"]["password_enc"]), "nas-pw")
        self.assertEqual(core.unprotect_password(got["h1"]["rustdesk_password_enc"]), "rd-pw")
        self.assertEqual(core.unprotect_password(got["h1"]["connections"][0]["password_enc"]), "web-pw")
        self.assertNotIn("password", got["h1"])
        self.assertEqual(got["h1"]["jump_host"], "h2")
        self.assertNotIn("_src", got["h1"])
        snips = {x["name"]: x for x in core.load_snippets()}                  # (a fresh PC also has the built-in examples)
        self.assertEqual(snips["Disk"]["group"], "Sys")
        self.assertEqual(core.load_workspaces(), [{"name": "Lab", "layout": "cols", "hosts": ["h1", "h2"]}])
        ident = core.load_identities()[0]
        self.assertEqual((ident["name"], ident["user"], core.unprotect_password(ident["secret_enc"])), ("Admin", "root", "id-pw"))
        px = core.load_proxies()[0]
        self.assertEqual((px["host"], px["port"], core.unprotect_password(px["password_enc"])), ("10.9.9.9", 1080, "px-pw"))
        with open(core.KNOWN_HOSTS) as f:
            self.assertIn("AAAAkey", f.read())
        text = bk.describe(report)
        self.assertIn("Servers: 2 added", text)

    def test_unopenable_passwords_are_counted_not_fatal(self):
        h = host(id="h1", password_enc="bm90IGEgcmVhbCBkcGFwaSBibG9i")
        bundle, lost = bk.make_bundle([h])
        self.assertEqual(lost, 1)
        self.assertEqual(bundle["hosts"][0]["password"], "")

    def test_empty_everything(self):
        bundle, lost = bk.make_bundle([])
        self.assertEqual(bk.decrypt(bk.encrypt(bundle, "pw"), "pw")["hosts"], [])


class CryptoTests(unittest.TestCase):
    def test_round_trip(self):
        b = {"hosts": [{"name": "é ü ✓"}], "x": 1}
        self.assertEqual(bk.decrypt(bk.encrypt(b, "pässword"), "pässword"), b)

    def test_each_backup_is_different_and_the_password_matters(self):
        b = {"hosts": []}
        a, c = bk.encrypt(b, "pw"), bk.encrypt(b, "pw")
        self.assertNotEqual(a, c)                                            # fresh salt and nonce
        with self.assertRaisesRegex(bk.BackupError, "Wrong password"):
            bk.decrypt(a, "PW")
        with self.assertRaisesRegex(bk.BackupError, "Choose a password"):
            bk.encrypt(b, "")

    def test_tampering_is_detected(self):
        box = json.loads(bk.encrypt({"hosts": []}, "pw"))
        data = bytearray(base64.b64decode(box["data"]))
        data[0] ^= 1
        box["data"] = base64.b64encode(bytes(data)).decode()
        with self.assertRaisesRegex(bk.BackupError, "Wrong password, or the file is damaged"):
            bk.decrypt(json.dumps(box).encode(), "pw")
        box = json.loads(bk.encrypt({"hosts": []}, "pw"))
        box["kdf"]["salt"] = base64.b64encode(b"x" * 16).decode()             # a swapped salt gives a different key
        with self.assertRaises(bk.BackupError):
            bk.decrypt(json.dumps(box).encode(), "pw")

    def test_not_a_backup(self):
        for junk in (b"", b"hello", b"{}", b'{"format": "OTHER"}', b"[1,2]", b'{"format": "RC-BACKUP"}', "ü".encode("latin-1")):
            with self.assertRaisesRegex(bk.BackupError, "not an RC backup"):
                bk.decrypt(junk, "pw")

    def test_newer_version_and_absurd_parameters(self):
        box = json.loads(bk.encrypt({"hosts": []}, "pw"))
        newer = dict(box, version=99)
        with self.assertRaisesRegex(bk.BackupError, "newer RC"):
            bk.decrypt(json.dumps(newer).encode(), "pw")
        evil = json.loads(json.dumps(box))
        evil["kdf"]["n"] = 2 ** 30                                           # would eat gigabytes of memory
        with self.assertRaisesRegex(bk.BackupError, "not an RC backup"):
            bk.decrypt(json.dumps(evil).encode(), "pw")

    def test_decrypted_junk_that_is_not_a_bundle(self):
        with self.assertRaisesRegex(bk.BackupError, "could not be read"):
            bk.decrypt(bk.encrypt({"no": "hosts"}, "pw"), "pw")


@unittest.skipUnless(WIN, "uses DPAPI")
class MergeTests(Pc):
    def bundle(self, *hosts, **extra):
        return dict({"hosts": [bk.portable_host(h)[0] for h in hosts], "snippets": [], "workspaces": [], "identities": [],
                     "proxies": [], "known_hosts": ""}, **extra)

    def test_new_only_leaves_matches_alone(self):
        mine = host(id="h1", user="mine")
        incoming = self.bundle(host(id="h1", user="theirs"), host(id="h9", name="other", address="10.0.0.9"))
        hosts, rep = bk.apply_bundle(incoming, [mine], replace=False)
        self.assertEqual(rep["servers"], (1, 0, 1))
        self.assertEqual({h["id"]: h["user"] for h in hosts}, {"h1": "mine", "h9": "admin"})

    def test_replace_overwrites_matches(self):
        hosts, rep = bk.apply_bundle(self.bundle(host(id="h1", user="theirs")), [host(id="h1", user="mine")], replace=True)
        self.assertEqual((rep["servers"], hosts[0]["user"]), ((0, 1, 0), "theirs"))

    def test_same_name_and_address_with_a_different_id_is_the_same_server(self):
        mine = host(id="local-id")
        incoming = self.bundle(host(id="their-id", user="theirs"), host(id="j", name="jump", address="10.0.0.1"),
                               workspaces=[{"name": "W", "layout": "grid", "hosts": ["their-id", "j"]}])
        incoming["hosts"][0]["jump_host"] = "their-id"
        hosts, rep = bk.apply_bundle(incoming, [mine], replace=True)
        by = {h["name"]: h for h in hosts}
        self.assertEqual(by["nas"]["id"], "local-id")                        # we keep our own id
        self.assertEqual(by["nas"]["user"], "theirs")
        self.assertEqual(by["nas"]["jump_host"], "local-id")                 # ... and references follow it
        self.assertEqual(core.load_workspaces()[0]["hosts"], ["local-id", "j"])

    def test_workspace_follows_a_skipped_match_too(self):
        incoming = self.bundle(host(id="their-id"), workspaces=[{"name": "W", "layout": "grid", "hosts": ["their-id"]}])
        bk.apply_bundle(incoming, [host(id="local-id")], replace=False)
        self.assertEqual(core.load_workspaces()[0]["hosts"], ["local-id"])

    def test_snippets_identities_proxies_merge_by_id(self):
        core.save_snippets([{"id": "s1", "name": "Mine", "command": "a"}])
        core.save_identities([{"id": "i1", "name": "Old", "user": "old"}])
        core.save_proxies([{"id": "p1", "name": "P", "kind": "http", "host": "h", "port": 3128}])
        incoming = self.bundle(
            snippets=[{"id": "s1", "name": "Theirs", "command": "b"}, {"id": "s2", "name": "New", "command": "c"}, {"name": "", "command": "x"}],
            identities=[{"id": "i1", "name": "New", "user": "new", "secret": "pw"}, {"id": "i2", "name": "Two", "user": "u2"}],
            proxies=[{"id": "p2", "name": "P2", "kind": "socks5", "host": "h2", "port": 1080}])
        _, rep = bk.apply_bundle(incoming, [], replace=False)
        self.assertEqual((rep["snippets"], rep["identities"], rep["proxies"]), ((1, 0, 1), (1, 0, 1), (1, 0, 0)))
        self.assertEqual([s["name"] for s in core.load_snippets()], ["Mine", "New"])
        self.assertEqual(core.load_identities()[0]["name"], "Old")           # not replaced
        _, rep = bk.apply_bundle(incoming, [], replace=True)
        self.assertEqual(rep["identities"], (0, 2, 0))
        got = {i["id"]: i for i in core.load_identities()}
        self.assertEqual((got["i1"]["name"], core.unprotect_password(got["i1"]["secret_enc"])), ("New", "pw"))

    def test_known_hosts_are_added_once(self):
        with open(core.KNOWN_HOSTS, "w") as f:
            f.write("a ssh-rsa KEYA")                                        # no trailing newline
        incoming = self.bundle(known_hosts="a ssh-rsa KEYA\nb ssh-rsa KEYB\n\n")
        _, rep = bk.apply_bundle(incoming, [], replace=False)
        self.assertEqual(rep["host_keys"], 1)
        _, rep = bk.apply_bundle(incoming, [], replace=False)
        self.assertEqual(rep["host_keys"], 0)
        with open(core.KNOWN_HOSTS) as f:
            self.assertEqual(f.read().splitlines(), ["a ssh-rsa KEYA", "b ssh-rsa KEYB"])

    def test_junk_entries_are_ignored(self):
        incoming = self.bundle(workspaces=["x", {"name": "", "hosts": []}], identities=["x", {"name": ""}], proxies=[5])
        incoming["hosts"].append("not a dict")
        hosts, rep = bk.apply_bundle(incoming, [], replace=True)
        self.assertEqual((hosts, rep["workspaces"], rep["identities"], rep["proxies"]), ([], (0, 0, 0), (0, 0, 0), (0, 0, 0)))


class FakeApp:
    def __init__(self, hosts):
        self.hosts = hosts
        self.root = None
        self.status = mock.Mock()
        self.committed = 0

    def commit(self):
        core.save_hosts(self.hosts)
        self.committed += 1


@unittest.skipUnless(WIN, "uses DPAPI")
class DialogTests(Pc):
    def setUp(self):
        super().setUp()
        self.msgs = []
        self.patches = [
            mock.patch.object(core.messagebox, "showinfo", side_effect=lambda *a, **k: self.msgs.append(("info", a[1]))),
            mock.patch.object(core.messagebox, "showerror", side_effect=lambda *a, **k: self.msgs.append(("error", a[1]))),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def run_backup(self, passwords, path):
        from tkinter import filedialog, messagebox, simpledialog
        with mock.patch.object(simpledialog, "askstring", side_effect=list(passwords)), \
                mock.patch.object(filedialog, "asksaveasfilename", return_value=path), \
                mock.patch.object(messagebox, "askyesno", return_value=True), \
                mock.patch.object(messagebox, "showinfo", side_effect=lambda *a, **k: self.msgs.append(("info", a[1]))), \
                mock.patch.object(messagebox, "showerror", side_effect=lambda *a, **k: self.msgs.append(("error", a[1]))):
            return bk.backup_dialog(self.app)

    def run_restore(self, path, password, answer):
        from tkinter import messagebox, simpledialog
        with mock.patch.object(simpledialog, "askstring", return_value=password), \
                mock.patch.object(messagebox, "askyesnocancel", return_value=answer), \
                mock.patch.object(messagebox, "showinfo", side_effect=lambda *a, **k: self.msgs.append(("info", a[1]))), \
                mock.patch.object(messagebox, "showerror", side_effect=lambda *a, **k: self.msgs.append(("error", a[1]))):
            return bk.restore_dialog(self.app, path)

    def test_backup_then_restore_on_a_fresh_pc(self):
        self.app = FakeApp([host(id="h1", password_enc=core.protect_password("pw1"), key_file="C:/k")])
        path = os.path.join(tempfile.mkdtemp(prefix="rc_bkfile_"), "b.rcbackup")
        self.assertEqual(self.run_backup(["longpassword", "longpassword"], path), path)
        self.assertTrue(os.path.getsize(path) > 100)
        info = [m for k, m in self.msgs if k == "info"][-1]
        self.assertIn("1 server(s) use an SSH key file", info)
        self.new_pc()
        self.app = FakeApp([])
        rep = self.run_restore(path, "longpassword", True)
        self.assertEqual(rep["servers"], (1, 0, 0))
        self.assertEqual(self.app.committed, 1)
        self.assertEqual(core.unprotect_password(self.app.hosts[0]["password_enc"]), "pw1")
        self.assertIn("Servers: 1 added", [m for k, m in self.msgs if k == "info"][-1])

    def test_mismatched_or_empty_password_saves_nothing(self):
        self.app = FakeApp([host()])
        path = os.path.join(tempfile.mkdtemp(prefix="rc_bkfile_"), "b.rcbackup")
        self.assertIsNone(self.run_backup(["longpassword", "different!"], path))
        self.assertFalse(os.path.exists(path))
        self.assertTrue(any(k == "error" for k, _ in self.msgs))
        self.assertIsNone(self.run_backup([None], path))
        self.assertFalse(os.path.exists(path))

    def test_restore_wrong_password_changes_nothing(self):
        self.app = FakeApp([host(id="h1")])
        path = os.path.join(tempfile.mkdtemp(prefix="rc_bkfile_"), "b.rcbackup")
        self.run_backup(["longpassword", "longpassword"], path)
        self.new_pc()
        self.app = FakeApp([])
        self.assertIsNone(self.run_restore(path, "WRONG", True))
        self.assertEqual((self.app.hosts, self.app.committed), ([], 0))
        self.assertIn("Wrong password", [m for k, m in self.msgs if k == "error"][-1])

    def test_restore_cancel_and_safety_copy(self):
        self.app = FakeApp([host(id="h1", user="theirs")])
        path = os.path.join(tempfile.mkdtemp(prefix="rc_bkfile_"), "b.rcbackup")
        self.run_backup(["longpassword", "longpassword"], path)
        self.app = FakeApp([host(id="h1", user="mine")])
        core.save_hosts(self.app.hosts)
        self.assertIsNone(self.run_restore(path, "longpassword", None))              # Cancel
        self.assertEqual(self.app.hosts[0]["user"], "mine")
        rep = self.run_restore(path, "longpassword", False)                           # No = add only
        self.assertEqual((rep["servers"], self.app.hosts[0]["user"]), ((0, 0, 1), "mine"))
        rep = self.run_restore(path, "longpassword", True)                            # Yes = replace
        self.assertEqual((rep["servers"], self.app.hosts[0]["user"]), ((0, 1, 0), "theirs"))
        copies = [f for f in os.listdir(core.APP_DIR) if f.startswith("hosts.json.before-restore-")]
        self.assertTrue(copies)

    def test_restore_a_non_backup_file(self):
        path = os.path.join(tempfile.mkdtemp(prefix="rc_bkfile_"), "x.rcbackup")
        with open(path, "w") as f:
            f.write("hello")
        self.app = FakeApp([])
        self.assertIsNone(self.run_restore(path, "pw", True))
        self.assertIn("not an RC backup", [m for k, m in self.msgs if k == "error"][-1])


if __name__ == "__main__":
    unittest.main()
