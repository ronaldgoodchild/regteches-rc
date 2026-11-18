"""Tests for rc_backup.py (export / import of everything).  Run from the RC folder:  python -m unittest discover -s tests -v

Everything happens in temp folders; the Windows DPAPI calls are replaced with a reversible fake so the tests run anywhere.
"""

import base64
import json
import os
import sys
import tempfile
import unittest
import zipfile

_TMP = tempfile.mkdtemp(prefix="rc_test_")
os.environ["RC_DATA_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as rc  # noqa: E402
import rc_backup as bk  # noqa: E402


def fake_protect(pw):
    return "PROT:" + base64.b64encode(pw.encode()).decode()


def fake_unprotect(blob):
    return base64.b64decode(blob[5:]).decode() if blob.startswith("PROT:") else ""


class PackCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc_bk_")
        self.ssh = os.path.join(self.dir, "ssh")
        os.makedirs(self.ssh)
        self._saved = (rc.APP_DIR, rc.HOSTS_FILE, rc.SETTINGS_FILE, rc.SNIPPETS_FILE, rc.KNOWN_HOSTS,
                       rc.protect_password, rc.unprotect_password)
        rc.APP_DIR = self.dir
        rc.HOSTS_FILE = os.path.join(self.dir, "hosts.json")
        rc.SETTINGS_FILE = os.path.join(self.dir, "settings.json")
        rc.SNIPPETS_FILE = os.path.join(self.dir, "snippets.json")
        rc.KNOWN_HOSTS = os.path.join(self.dir, "known_hosts")
        rc.protect_password, rc.unprotect_password = fake_protect, fake_unprotect
        self.key = os.path.join(self.ssh, "rc_homelab_OLDPC")
        with open(self.key, "wb") as f:
            f.write(b"-----BEGIN PRIVATE KEY-----\nSECRETKEYDATA\n-----END PRIVATE KEY-----\n")
        h = rc.sanitize_host({"id": "a1", "name": "Plex", "address": "192.168.8.173", "user": "rg",
                              "password_enc": fake_protect("hunter2"), "key_file": self.key, "favorite": True,
                              "rustdesk_password_enc": fake_protect("rd-pass"),
                              "connections": [{"type": "web", "port": 8096, "password_enc": fake_protect("webpw")}],
                              "tunnels": [{"kind": "local", "listen_port": 8080, "dest_host": "x", "dest_port": 80}]})
        h2 = rc.sanitize_host({"id": "b2", "name": "NAS", "address": "192.168.8.121", "group": "Storage"})
        self.hosts = [h, h2]
        with open(rc.SNIPPETS_FILE, "w") as f:
            json.dump([{"id": "mine1", "name": "Disk", "command": "df -h", "confirm": False}], f)
        with open(rc.SETTINGS_FILE, "w") as f:
            json.dump({"dark": True, "theme": "Royal Dark"}, f)
        with open(rc.KNOWN_HOSTS, "w") as f:
            f.write("192.168.8.173 ssh-ed25519 AAAAkey1\n")
        self.pack = os.path.join(self.dir, "x" + bk.PACK_EXT)

    def tearDown(self):
        (rc.APP_DIR, rc.HOSTS_FILE, rc.SETTINGS_FILE, rc.SNIPPETS_FILE, rc.KNOWN_HOSTS,
         rc.protect_password, rc.unprotect_password) = self._saved

    def fresh_pc(self):
        """Point RC at an empty data folder, like RC on another machine."""
        d = tempfile.mkdtemp(prefix="rc_pc2_")
        rc.APP_DIR = d
        rc.HOSTS_FILE = os.path.join(d, "hosts.json")
        rc.SETTINGS_FILE = os.path.join(d, "settings.json")
        rc.SNIPPETS_FILE = os.path.join(d, "snippets.json")
        rc.KNOWN_HOSTS = os.path.join(d, "known_hosts")
        return d, os.path.join(d, "ssh2")


class ExportTests(PackCase):
    def test_no_passphrase_means_no_secrets_anywhere_in_the_file(self):
        s = bk.export_pack(self.pack, self.hosts, "")
        self.assertEqual((s["passwords"], s["keys"]), (0, 0))
        raw = b"".join(zipfile.ZipFile(self.pack).read(n) for n in zipfile.ZipFile(self.pack).namelist())
        for secret in (b"hunter2", b"rd-pass", b"webpw", b"SECRETKEYDATA", b"PROT:"):
            self.assertNotIn(secret, raw)
        self.assertNotIn("secrets.enc", zipfile.ZipFile(self.pack).namelist())

    def test_passphrase_pack_hides_secrets_but_holds_them(self):
        s = bk.export_pack(self.pack, self.hosts, "correct horse")
        self.assertEqual((s["passwords"], s["keys"]), (3, 1))
        z = zipfile.ZipFile(self.pack)
        raw = b"".join(z.read(n) for n in z.namelist())
        for secret in (b"hunter2", b"rd-pass", b"webpw", b"SECRETKEYDATA"):
            self.assertNotIn(secret, raw)  # encrypted, never plain
        self.assertIn("secrets.enc", z.namelist())

    def test_export_does_not_change_live_hosts(self):
        before = json.dumps(self.hosts, sort_keys=True)
        bk.export_pack(self.pack, self.hosts, "pass12")
        self.assertEqual(before, json.dumps(self.hosts, sort_keys=True))

    def test_summary_counts(self):
        s = bk.export_pack(self.pack, self.hosts, "")
        self.assertEqual((s["servers"], s["tunnels"], s["snippets"], s["trusted_keys"]), (2, 1, 1, 1))


class ImportTests(PackCase):
    def roundtrip(self, mode="merge", phrase="pw-12345", existing=None):
        bk.export_pack(self.pack, self.hosts, phrase)
        d, ssh2 = self.fresh_pc()
        pack = bk.open_pack(self.pack)
        new, rep = bk.import_pack(pack, existing or [], mode, phrase, ssh_dir=ssh2)
        return new, rep, d, ssh2

    def test_everything_comes_back_on_a_new_pc(self):
        new, rep, d, ssh2 = self.roundtrip()
        self.assertEqual((rep["added"], rep["passwords"], rep["keys"]), (2, 3, 1))
        plex = next(h for h in new if h["name"] == "Plex")
        self.assertEqual(fake_unprotect(plex["password_enc"]), "hunter2")
        self.assertEqual(fake_unprotect(plex["rustdesk_password_enc"]), "rd-pass")
        self.assertEqual(fake_unprotect(plex["connections"][0]["password_enc"]), "webpw")
        self.assertTrue(plex["favorite"])
        self.assertEqual(plex["tunnels"][0]["dest_port"], 80)
        self.assertEqual(os.path.dirname(plex["key_file"]), ssh2)  # key re-homed to this PC's .ssh
        with open(plex["key_file"], "rb") as f:
            self.assertIn(b"SECRETKEYDATA", f.read())
        self.assertEqual(json.load(open(rc.SETTINGS_FILE))["theme"], "Royal Dark")
        self.assertIn("Disk", [x["name"] for x in rc.load_snippets()])
        self.assertIn("AAAAkey1", open(rc.KNOWN_HOSTS).read())

    def test_wrong_passphrase_is_refused_and_changes_nothing(self):
        bk.export_pack(self.pack, self.hosts, "right-pass")
        d, ssh2 = self.fresh_pc()
        with self.assertRaises(bk.PackError):
            bk.import_pack(bk.open_pack(self.pack), [], "merge", "wrong-pass", ssh_dir=ssh2)
        self.assertFalse(os.path.exists(rc.SETTINGS_FILE))
        self.assertFalse(os.path.exists(ssh2))

    def test_import_without_passphrase_gives_servers_but_no_secrets(self):
        bk.export_pack(self.pack, self.hosts, "right-pass")
        d, ssh2 = self.fresh_pc()
        os.remove(self.key)  # the other PC does not have that file
        new, rep = bk.import_pack(bk.open_pack(self.pack), [], "merge", "", ssh_dir=ssh2)
        plex = next(h for h in new if h["name"] == "Plex")
        self.assertEqual((plex["password_enc"], rep["passwords"], rep["keys"]), ("", 0, 0))
        self.assertEqual(plex["key_file"], "")  # that path does not exist here

    def test_merge_updates_matches_keeps_local_only_and_never_drops_a_local_password(self):
        local = [rc.sanitize_host({"id": "zz", "name": "Plex", "address": "192.168.8.173", "notes": "old",
                                   "password_enc": fake_protect("localpw")}),
                 rc.sanitize_host({"id": "q9", "name": "LocalOnly", "address": "10.9.9.9"})]
        bk.export_pack(self.pack, self.hosts, "")
        d, ssh2 = self.fresh_pc()
        new, rep = bk.import_pack(bk.open_pack(self.pack), local, "merge", "", ssh_dir=ssh2)
        names = sorted(h["name"] for h in new)
        self.assertEqual(names, ["LocalOnly", "NAS", "Plex"])
        plex = next(h for h in new if h["name"] == "Plex")
        self.assertEqual(plex["id"], "zz")                       # kept the local identity
        self.assertEqual(fake_unprotect(plex["password_enc"]), "localpw")
        self.assertEqual((rep["added"], rep["updated"]), (1, 1))

    def test_replace_makes_it_match_the_pack_and_backs_up_first(self):
        local = [rc.sanitize_host({"name": "LocalOnly", "address": "10.9.9.9"})]
        bk.export_pack(self.pack, self.hosts, "")
        d, ssh2 = self.fresh_pc()
        open(rc.SETTINGS_FILE, "w").write('{"theme": "mine"}')
        new, rep = bk.import_pack(bk.open_pack(self.pack), local, "replace", "", ssh_dir=ssh2)
        self.assertEqual(sorted(h["name"] for h in new), ["NAS", "Plex"])
        self.assertTrue(os.path.isfile(os.path.join(rep["backup"], "settings.json")))
        self.assertIn("mine", open(os.path.join(rep["backup"], "settings.json")).read())

    def test_importing_twice_does_not_duplicate(self):
        bk.export_pack(self.pack, self.hosts, "")
        d, ssh2 = self.fresh_pc()
        new, _ = bk.import_pack(bk.open_pack(self.pack), [], "merge", "", ssh_dir=ssh2)
        again, rep = bk.import_pack(bk.open_pack(self.pack), new, "merge", "", ssh_dir=ssh2)
        self.assertEqual(len(again), 2)
        self.assertEqual(rep["added"], 0)
        names = [x["name"] for x in rc.load_snippets()]
        self.assertEqual(len(names), len(set(names)))  # built-in + imported, no repeats
        self.assertEqual(open(rc.KNOWN_HOSTS).read().count("AAAAkey1"), 1)

    def test_existing_key_file_is_never_overwritten(self):
        bk.export_pack(self.pack, self.hosts, "pw-12345")
        d, ssh2 = self.fresh_pc()
        os.makedirs(ssh2)
        mine = os.path.join(ssh2, "rc_homelab_OLDPC")
        open(mine, "wb").write(b"MY OWN KEY")
        new, rep = bk.import_pack(bk.open_pack(self.pack), [], "merge", "pw-12345", ssh_dir=ssh2)
        self.assertEqual(open(mine, "rb").read(), b"MY OWN KEY")
        plex = next(h for h in new if h["name"] == "Plex")
        self.assertNotEqual(plex["key_file"], mine)
        self.assertIn(b"SECRETKEYDATA", open(plex["key_file"], "rb").read())

    def test_only_chosen_parts_are_imported(self):
        bk.export_pack(self.pack, self.hosts, "")
        d, ssh2 = self.fresh_pc()
        new, rep = bk.import_pack(bk.open_pack(self.pack), [], "merge", "", parts=("servers",), ssh_dir=ssh2)
        self.assertEqual(len(new), 2)
        self.assertFalse(os.path.exists(rc.SETTINGS_FILE))
        self.assertFalse(os.path.exists(rc.KNOWN_HOSTS))


class OpenPackTests(PackCase):
    def test_not_a_zip(self):
        p = os.path.join(self.dir, "bad.rcpack")
        open(p, "w").write("hello")
        with self.assertRaises(bk.PackError):
            bk.open_pack(p)

    def test_zip_without_manifest(self):
        p = os.path.join(self.dir, "other.rcpack")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("readme.txt", "x")
        with self.assertRaises(bk.PackError):
            bk.open_pack(p)

    def test_newer_format_is_refused(self):
        p = os.path.join(self.dir, "new.rcpack")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("manifest.json", json.dumps({"app": "REGTeches RC", "format": 99}))
            z.writestr("hosts.json", '{"hosts": []}')
        with self.assertRaises(bk.PackError):
            bk.open_pack(p)

    def test_path_traversal_names_are_never_extracted(self):
        bk.export_pack(self.pack, self.hosts, "")
        evil = os.path.join(self.dir, "evil.rcpack")
        with zipfile.ZipFile(self.pack) as src, zipfile.ZipFile(evil, "w") as z:
            for n in src.namelist():
                z.writestr(n, src.read(n))
            z.writestr("../../escape.txt", "boom")
        pack = bk.open_pack(evil)
        self.assertNotIn("../../escape.txt", pack.parts)

    def test_describe_mentions_source_and_servers(self):
        bk.export_pack(self.pack, self.hosts, "pw-12345")
        text = bk.open_pack(self.pack).describe()
        self.assertIn("2 server(s)", text)
        self.assertIn("passwords and keys", text)


if __name__ == "__main__":
    unittest.main()
