"""Tests for rc_import.py (MobaXterm and Xshell) and how the import list turns the items into servers.
Run from the RC folder:  python -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import tkinter as tk
import unittest
from tkinter import ttk

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_import_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_import as imp  # noqa: E402
import rc_manager as core  # noqa: E402
import rc_tools  # noqa: E402

MOBA = """[Bookmarks]
SubRep=
ImgNum=42
[Bookmarks_1]
SubRep=Servers\\Linux
ImgNum=41
Web server=#109#0%192.168.1.10%22%root%%-1%-1%%%%%0%0%0%%%-1%0%0%0%%1080%%0%0%1#MobaFont%10%0%0%-1%15%236,236,236%30,30,30%180,180,192%0%-1%0%%xterm%-1%-1%_Std_Colors_0_%80%24%0%1%-1%<none>%%0%1%-1#0# #-1
NAS files=#109#7%nas.lan%22%<default>%%-1%-1%%%%%0%0%0#MobaFont%10#0# #-1
Old router=#98#1%10.0.0.1%2323%admin%%-1#0# #-1
[Bookmarks_2]
SubRep=Windows
ImgNum=91
Desktop PC=#91#4%192.168.1.20%3389%Administrator%0%-1%0%%%%%%#0# #-1
Remote screen=#128#5%192.168.1.30%%%-1%-1%#0# #-1
FTP box=#130#6%ftp.example.com%21%anonymous%%-1#0# #-1
Console=#109#8%COM3%115200%%%-1#0# #-1
Mystery=#99#99%1.2.3.4%1%u%%-1#0# #-1
No host=#109#0%%22%u%%-1#0# #-1
[Bookmarks_3]
SubRep=
ImgNum=41
Top level=#109#0%top.lan%%ron%%-1#0# #-1
not a session=hello
"""

XSH = """[CONNECTION]
Host=10.1.1.5
Port=2222
Protocol=SSH
[CONNECTION:AUTHENTICATION]
UserName=deploy
Password=encrypted-garbage
[TERMINAL]
Type=xterm
"""


class MobaTests(unittest.TestCase):
    def setUp(self):
        self.items = {i["name"]: i for i in imp.parse_mobaxterm(MOBA)}

    def test_what_is_found(self):
        self.assertEqual(sorted(self.items), sorted(["Web server", "NAS files", "Old router", "Desktop PC", "Remote screen",
                                                     "FTP box", "Console", "Top level"]))

    def test_ssh_with_a_folder(self):
        i = self.items["Web server"]
        self.assertEqual((i["address"], i["user"], i["port"], i["group"], i["kind"], i["source"]),
                         ("192.168.1.10", "root", 22, "Servers / Linux", "ssh", "MobaXterm · SSH"))

    def test_sftp_and_default_user(self):
        i = self.items["NAS files"]
        self.assertEqual((i["user"], i["kind"], i["mode"]), ("", "ssh", "SFTP"))                    # '<default>' is no user

    def test_other_protocols_and_ports(self):
        self.assertEqual((self.items["Old router"]["kind"], self.items["Old router"]["port"]), ("telnet", 2323))
        self.assertEqual((self.items["Desktop PC"]["kind"], self.items["Desktop PC"]["port"], self.items["Desktop PC"]["group"]), ("rdp", 3389, "Windows"))
        self.assertEqual((self.items["Remote screen"]["kind"], self.items["Remote screen"]["port"]), ("vnc", 5900))   # empty port: the default
        self.assertEqual((self.items["FTP box"]["kind"], self.items["FTP box"]["port"], self.items["FTP box"]["user"]), ("ftp", 21, "anonymous"))
        self.assertEqual(self.items["Top level"]["group"], "")
        self.assertEqual(self.items["Top level"]["port"], 22)

    def test_serial_sessions(self):
        i = self.items["Console"]
        self.assertEqual((i["kind"], i["address"], i["serial"]), ("serial", "localhost", "COM3, 115200"))

    def test_junk_and_empty_input(self):
        self.assertEqual(imp.parse_mobaxterm(""), [])
        self.assertEqual(imp.parse_mobaxterm(None), [])
        self.assertEqual(imp.parse_mobaxterm("garbage\n[x\n=#1#0%h%22%u"), [])
        self.assertEqual(len(imp.parse_mobaxterm(MOBA.replace("\n", "\r\n"))), 8)                   # Windows line endings


class XshellTests(unittest.TestCase):
    def test_one_session(self):
        i = imp.parse_xshell_session(XSH, "db01", "Prod")
        self.assertEqual((i["name"], i["address"], i["user"], i["port"], i["group"], i["kind"], i["source"]),
                         ("db01", "10.1.1.5", "deploy", 2222, "Prod", "ssh", "Xshell"))
        self.assertNotIn("Password", str(i))                                                          # secrets are never read

    def test_other_protocols(self):
        t = imp.parse_xshell_session(XSH.replace("SSH", "TELNET").replace("2222", ""), "sw")
        self.assertEqual((t["kind"], t["port"]), ("telnet", 23))
        s = imp.parse_xshell_session(XSH.replace("Protocol=SSH", "Protocol=SFTP"), "files")
        self.assertEqual((s["kind"], s["mode"], s["source"]), ("ssh", "SFTP", "Xshell · Sftp"))
        self.assertEqual(imp.parse_xshell_session(XSH.replace("Protocol=SSH", "Protocol=RLOGIN"), "x"), None)
        self.assertEqual(imp.parse_xshell_session(XSH.replace("Host=10.1.1.5", "Host="), "x"), None)
        self.assertEqual(imp.parse_xshell_session("", "x"), None)

    def test_folder_scan_with_utf16_and_subfolders(self):
        root = tempfile.mkdtemp(prefix="rc_xsh_")
        os.makedirs(os.path.join(root, "Prod", "DB"))
        with open(os.path.join(root, "top.xsh"), "wb") as f:
            f.write(b"\xff\xfe" + XSH.replace("10.1.1.5", "top.lan").encode("utf-16-le"))           # Xshell writes UTF-16
        with open(os.path.join(root, "Prod", "web.xsh"), "w", encoding="utf-8") as f:
            f.write(XSH)
        with open(os.path.join(root, "Prod", "DB", "pg.xsh"), "w", encoding="latin-1") as f:
            f.write(XSH.replace("10.1.1.5", "pg.lan"))
        with open(os.path.join(root, "Prod", "notes.txt"), "w") as f:
            f.write("not a session")
        with open(os.path.join(root, "bad.xsh"), "w") as f:
            f.write("[CONNECTION]\nProtocol=SSH\n")                                                  # no host
        got = {i["name"]: (i["address"], i["group"]) for i in imp.scan_xshell_folder(root)}
        self.assertEqual(got, {"top": ("top.lan", ""), "web": ("10.1.1.5", "Prod"), "pg": ("pg.lan", "Prod / DB")})
        self.assertEqual(imp.scan_xshell_folder(os.path.join(root, "nope")), [])

    def test_default_folder_does_not_crash(self):
        self.assertIsInstance(imp.default_xshell_folder(), str)

    def test_read_text_encodings(self):
        d = tempfile.mkdtemp(prefix="rc_enc_")
        for name, data in (("a", "héllo".encode("utf-8")), ("b", "héllo".encode("latin-1")), ("c", b"\xff\xfe" + "héllo".encode("utf-16-le")),
                           ("d", b"\xef\xbb\xbf" + "héllo".encode("utf-8"))):
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
            self.assertEqual(imp.read_text(os.path.join(d, name)), "héllo", name)


class FakeApp:
    def __init__(self, root, hosts=()):
        self.nb = ttk.Notebook(root)
        self.nb.pack()
        self.hosts = list(hosts)
        self.status = tk.StringVar()
        self.closed = 0

    def refill(self):
        pass

    def close_widget(self, w):
        self.closed += 1


class ImportListTests(unittest.TestCase):
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
        self._save = core.save_hosts
        core.save_hosts = lambda hosts: None

    def tearDown(self):
        core.save_hosts = self._save

    def run_import(self, items, existing=()):
        app = FakeApp(self.root, existing)
        tab = rc_tools.ImportTab(app, items)
        tab.do_import()
        return app, tab

    def test_each_kind_becomes_a_sensible_server(self):
        items = imp.parse_mobaxterm(MOBA) + [imp.parse_xshell_session(XSH, "db01", "Prod")]
        app, _ = self.run_import(items)
        by = {h["name"]: h for h in app.hosts}
        self.assertEqual(len(by), 9)
        h = by["Web server"]
        self.assertEqual((h["address"], h["user"], h["group"], h["ssh_port"], h["default_action"]), ("192.168.1.10", "root", "Servers / Linux", 22, "ssh"))
        self.assertIn("Imported from MobaXterm · SSH", h["notes"])
        self.assertEqual((by["db01"]["ssh_port"], by["db01"]["user"], by["db01"]["group"]), (2222, "deploy", "Prod"))
        self.assertEqual((by["Desktop PC"]["rdp_port"], by["Desktop PC"]["default_action"]), (3389, "rdp"))
        self.assertEqual((by["Remote screen"]["vnc_port"], by["Remote screen"]["default_action"]), (5900, "vnc"))
        self.assertEqual((by["FTP box"]["file_mode"], by["FTP box"]["file_port"], by["FTP box"]["default_action"]), ("FTP", 21, "files"))
        self.assertEqual((by["NAS files"]["file_mode"], by["NAS files"]["default_action"]), ("SFTP", "ssh"))
        r = by["Old router"]
        self.assertEqual(r["default_action"], "telnet")
        self.assertEqual(r["connections"], [{"type": "telnet", "label": "Telnet", "port": 2323, "url": "", "user": ""}])
        c = by["Console"]
        self.assertEqual((c["default_action"], c["connections"][0]["type"], c["connections"][0]["url"]), ("serial", "serial", "COM3, 115200"))
        self.assertEqual(by["Top level"]["group"], "MobaXterm")                                      # no folder: grouped by program
        # all of them are valid and survive a reload
        for h in app.hosts:
            self.assertEqual(core.sanitize_host(h)["connections"], h["connections"])

    def test_servers_already_in_rc_are_not_selected_or_duplicated(self):
        mine = core.sanitize_host({"id": "m", "name": "mine", "address": "192.168.1.10"})
        app, tab = self.run_import(imp.parse_mobaxterm(MOBA), [mine])
        self.assertEqual(tab.tree.item("0", "values")[4], "already in RC")
        self.assertNotIn("0", tab.tree.selection())
        self.assertNotIn("Web server", [h["name"] for h in app.hosts])
        self.assertEqual(len(app.hosts), 8)                                                         # 7 new + the one already there
        self.assertIn("Imported 7", app.status.get())

    def test_empty_import(self):
        app, tab = self.run_import([])
        self.assertEqual(app.hosts, [])


if __name__ == "__main__":
    unittest.main()
