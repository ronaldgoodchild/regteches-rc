"""Tests for the file manager upgrades in rc_files.py: breadcrumbs, sorting, the right-click menu, copy / cut / paste on the
PC and on the server, new files, copying paths, and the transfer queue's pause and retry. The 'server' is a folder on this PC
behind the same backend interface as SFTP.  Run from the RC folder:  python -m unittest discover -s tests -v"""

import os
import posixpath
import queue
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_files_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_files as rf  # noqa: E402
import rc_manager as core  # noqa: E402


class DirBackend:
    """A 'server' kept in a folder: paths look like /a/b but live under `root`. Same methods as SftpBackend."""
    kind = "dir"

    def __init__(self, root):
        self.root = root
        self.fail_uploads = 0
        self.uploads = []

    def real(self, path):
        return os.path.join(self.root, *[p for p in path.split("/") if p])

    join = staticmethod(posixpath.join)
    parent = staticmethod(core.SftpBackend.parent)

    def home(self):
        return "/"

    def listdir(self, path):
        out = []
        for n in os.listdir(self.real(path)):
            st = os.stat(os.path.join(self.real(path), n))
            out.append((n, os.path.isdir(os.path.join(self.real(path), n)), st.st_size, st.st_mtime))
        return out

    def stat(self, path):
        try:
            st = os.stat(self.real(path))
        except OSError:
            return None
        return os.path.isdir(self.real(path)), st.st_size, st.st_mtime, 0o644

    def download(self, remote, local, progress=None):
        shutil.copyfile(self.real(remote), local)
        if progress:
            progress(1, 1)

    def upload(self, local, remote, progress=None):
        if self.fail_uploads > 0:
            self.fail_uploads -= 1
            raise OSError("disk is full (pretend)")
        shutil.copyfile(local, self.real(remote))
        self.uploads.append(remote)
        if progress:
            progress(1, 1)

    def set_mtime(self, path, mtime):
        pass

    def mkdir(self, path):
        os.mkdir(self.real(path))

    def touch(self, path):
        open(self.real(path), "x").close()

    def copy(self, src, dst, is_dir):
        (shutil.copytree if is_dir else shutil.copy2)(self.real(src), self.real(dst))

    def ensure_dir(self, path):
        os.makedirs(self.real(path), exist_ok=True)

    def remove(self, path, is_dir):
        (os.rmdir if is_dir else os.remove)(self.real(path))

    def rename(self, old, new):
        os.rename(self.real(old), self.real(new))

    def chmod(self, path, mode):
        pass

    def close(self):
        pass


def entry(name, is_dir=False, size=0, mtime=0):
    return (name, is_dir, size, mtime)


class HelperTests(unittest.TestCase):
    def test_breadcrumbs_posix(self):
        self.assertEqual(rf.breadcrumb_segments("/home/ron/docs", core.SftpBackend),
                         [("/", "/"), ("home", "/home"), ("ron", "/home/ron"), ("docs", "/home/ron/docs")])
        self.assertEqual(rf.breadcrumb_segments("/", core.SftpBackend), [("/", "/")])
        self.assertEqual(rf.breadcrumb_segments("/trailing/", core.SftpBackend), [("/", "/"), ("trailing", "/trailing")])
        self.assertEqual(rf.breadcrumb_segments("", core.SftpBackend), [])

    def test_breadcrumbs_windows(self):
        segs = rf.breadcrumb_segments("C:\\Users\\Admin\\Documents", rf.LocalBackend())
        self.assertEqual([l for l, _ in segs], ["C:\\", "Users", "Admin", "Documents"])
        self.assertEqual([p for _, p in segs][:2], ["C:\\", "C:\\Users"])
        self.assertEqual(rf.breadcrumb_segments("C:\\", rf.LocalBackend()), [("C:\\", "C:\\")])

    def test_long_paths_keep_the_end_and_an_ellipsis_that_goes_up(self):
        path = "/" + "/".join(f"d{i}" for i in range(12))
        segs = rf.breadcrumb_segments(path, core.SftpBackend, limit=4)
        self.assertEqual([l for l, _ in segs], ["…", "d8", "d9", "d10", "d11"])
        self.assertEqual(segs[0][1], "/d0/d1/d2/d3/d4/d5/d6/d7")                 # the ellipsis opens the first folder that was dropped

    def test_unique_names(self):
        self.assertEqual(rf.unique_name("a.txt", []), "a.txt")
        self.assertEqual(rf.unique_name("a.txt", ["a.txt"]), "a (copy).txt")
        self.assertEqual(rf.unique_name("a.txt", ["A.TXT", "a (copy).txt"]), "a (copy 2).txt")
        self.assertEqual(rf.unique_name("dir", ["dir"]), "dir (copy)")
        self.assertEqual(rf.unique_name(".bashrc", [".bashrc"]), ".bashrc (copy)")
        self.assertEqual(rf.unique_name("a.tar.gz", ["a.tar.gz"]), "a.tar (copy).gz")

    def test_sorting(self):
        rows = [entry("b.txt", size=5, mtime=30), entry("A.txt", size=50, mtime=10), entry("zdir", True), entry("adir", True),
                entry(".hidden", size=1, mtime=20)]
        names = lambda **kw: [e[0] for e in rf.sort_entries(rows, **kw)]
        self.assertEqual(names(), ["adir", "zdir", ".hidden", "A.txt", "b.txt"])
        self.assertEqual(names(reverse=True), ["zdir", "adir", "b.txt", "A.txt", ".hidden"])
        self.assertEqual(names(col="size"), ["adir", "zdir", ".hidden", "b.txt", "A.txt"])
        self.assertEqual(names(col="size", reverse=True), ["adir", "zdir", "A.txt", "b.txt", ".hidden"])          # folders stay on top, A-Z
        self.assertEqual(names(col="modified"), ["adir", "zdir", "A.txt", ".hidden", "b.txt"])
        self.assertEqual(names(show_hidden=False), ["adir", "zdir", "A.txt", "b.txt"])
        self.assertEqual(names(needle="TXT"), ["A.txt", "b.txt"])


class BackendTests(unittest.TestCase):
    def test_local_touch_and_copy(self):
        d = tempfile.mkdtemp(prefix="rc_lb_")
        b = rf.LocalBackend()
        b.touch(os.path.join(d, "new.txt"))
        self.assertEqual(os.path.getsize(os.path.join(d, "new.txt")), 0)
        with self.assertRaises(FileExistsError):
            b.touch(os.path.join(d, "new.txt"))
        os.makedirs(os.path.join(d, "src", "sub"))
        with open(os.path.join(d, "src", "sub", "f.txt"), "w") as f:
            f.write("data")
        b.copy(os.path.join(d, "src"), os.path.join(d, "dst"), True)
        with open(os.path.join(d, "dst", "sub", "f.txt")) as f:
            self.assertEqual(f.read(), "data")
        b.copy(os.path.join(d, "src", "sub", "f.txt"), os.path.join(d, "one.txt"), False)
        self.assertTrue(os.path.exists(os.path.join(d, "one.txt")))

    def test_sftp_copy_runs_cp_with_quoting_and_reports_errors(self):
        be = core.SftpBackend.__new__(core.SftpBackend)
        be.client, be.sftp = mock.Mock(), mock.Mock()
        import rc_tools
        with mock.patch.object(rc_tools, "run_remote", return_value=(0, "", "")) as run:
            be.copy("/srv/my files/a b.txt", "/srv/it's/x.txt", False)
        cmd = run.call_args[0][1]
        self.assertEqual(cmd, "cp -a -- '/srv/my files/a b.txt' '/srv/it'\"'\"'s/x.txt'")
        with mock.patch.object(rc_tools, "run_remote", return_value=(1, "", "cp: cannot stat: No such file")):
            with self.assertRaisesRegex(OSError, "cannot stat"):
                be.copy("/a", "/b", True)
        with mock.patch.object(rc_tools, "run_remote", return_value=(127, "", "")):
            with self.assertRaisesRegex(OSError, "may not have cp"):
                be.copy("/a", "/b", True)

    def test_sftp_touch_and_ftp_limits(self):
        be = core.SftpBackend.__new__(core.SftpBackend)
        be.client, be.sftp = mock.Mock(), mock.Mock()
        be.touch("/x/new")
        be.sftp.open.assert_called_once_with("/x/new", "x")
        ftp = core.FtpBackend.__new__(core.FtpBackend)
        ftp.ftp = mock.Mock()
        ftp.touch("/x/new")
        self.assertEqual(ftp.ftp.storbinary.call_args[0][0], "STOR /x/new")
        with self.assertRaisesRegex(OSError, "needs SFTP"):
            ftp.copy("/a", "/b", False)


class FakeApp:
    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.hosts = []
        self.status = tk.StringVar()
        self.q = queue.Queue()
        self.terminals = []

    def post(self, fn):
        self.q.put(fn)

    def all_terminals(self):
        return self.terminals

    def pump(self, until=lambda: False, seconds=6):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.q.get(timeout=0.03)()
            except queue.Empty:
                pass
            if until():
                return True
        return until()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1200x700")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self._save = core.save_hosts
        core.save_hosts = lambda hosts: None
        self.srv_root = tempfile.mkdtemp(prefix="rc_server_")
        self.pc_root = tempfile.mkdtemp(prefix="rc_pc_")
        self.app = FakeApp(self.root)
        self.errors = []
        for name in ("showinfo", "showerror", "showwarning"):
            p = mock.patch.object(rf.messagebox, name, side_effect=lambda *a, **k: self.errors.append(a[1] if len(a) > 1 else ""))
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(rf.FilesTab, "connect", lambda self: None):
            self.host = core.sanitize_host({"id": "h1", "name": "nas", "address": "10.0.0.2"})
            self.tab = rf.FilesTab(self.app, self.host, "u", "p")
        self.app.nb.add(self.tab, text="files")
        self.backend = DirBackend(self.srv_root)
        self.tab.remote.backend = self.backend
        self.tab.xb = DirBackend(self.srv_root)
        self.tab.xb.uploads = self.backend.uploads
        self.tab.local.backend = rf.LocalBackend()
        self.go(self.tab.local, self.pc_root)
        self.go(self.tab.remote, "/")

    def tearDown(self):
        self.tab.closed = True
        self.tab.go.set()
        self.app.nb.destroy()
        core.save_hosts = self._save

    def go(self, pane, path):
        pane.cd(path)
        self.assertTrue(self.app.pump(lambda: pane.path == path), pane.status.get())

    def put(self, root, rel, data=b"x"):
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def reload(self, pane, expect=None):
        pane.refresh()
        self.assertTrue(self.app.pump(lambda: (expect is None or expect in self.names(pane)) and pane.status.get() != "Loading ..."))

    def names(self, pane):
        return [pane.tree.item(i, "text")[2:] for i in pane.tree.get_children()]

    def select(self, pane, *names):
        pane.tree.selection_set([i for i in pane.tree.get_children() if pane.tree.item(i, "text")[2:] in names])

    def wait_jobs(self, states=("done", "skipped", "error", "cancelled")):
        return self.app.pump(lambda: self.tab.jobs and all(j["state"] in states for j in self.tab.jobs), 10)


class PaneTests(Base):
    def test_breadcrumbs_follow_the_path_and_navigate(self):
        self.put(self.srv_root, "a/b/c/f.txt")
        self.go(self.tab.remote, "/a/b/c")
        self.reload(self.tab.remote, "f.txt")
        labels = [w.cget("text") for w in self.tab.remote.crumbs.winfo_children() if isinstance(w, ttk.Button)]
        self.assertEqual(labels, ["/", "a", "b", "c"])
        buttons = [w for w in self.tab.remote.crumbs.winfo_children() if isinstance(w, ttk.Button)]
        buttons[2].invoke()                                                          # click "b"
        self.assertTrue(self.app.pump(lambda: self.tab.remote.path == "/a/b"))
        self.assertEqual(self.names(self.tab.remote), ["c"])

    def test_header_click_sorts_and_shows_the_arrow(self):
        self.put(self.srv_root, "small.txt", b"1")
        self.put(self.srv_root, "big.txt", b"x" * 100)
        self.put(self.srv_root, "adir/f")
        pane = self.tab.remote
        self.reload(pane, "small.txt")
        self.assertEqual(self.names(pane), ["adir", "big.txt", "small.txt"])
        pane.sort_by("size")
        self.assertEqual(self.names(pane), ["adir", "small.txt", "big.txt"])
        self.assertIn("▲", pane.tree.heading("size", "text"))
        pane.sort_by("size")
        self.assertEqual(self.names(pane), ["adir", "big.txt", "small.txt"])
        self.assertIn("▼", pane.tree.heading("size", "text"))
        pane.sort_by("name")
        self.assertIn("▲", pane.tree.heading("#0", "text"))
        self.assertNotIn("▲", pane.tree.heading("size", "text"))

    def test_hidden_files_toggle(self):
        self.put(self.srv_root, ".secret")
        self.put(self.srv_root, "shown.txt")
        pane = self.tab.remote
        self.reload(pane, "shown.txt")
        self.assertEqual(self.names(pane), [".secret", "shown.txt"])
        pane.show_hidden.set(False)
        pane.render()
        self.assertEqual(self.names(pane), ["shown.txt"])
        self.assertIn("(1 shown)", pane.status.get())

    def test_context_menu_depends_on_what_is_selected(self):
        self.put(self.srv_root, "f.txt")
        self.put(self.srv_root, "adir/x")
        pane = self.tab.remote
        self.reload(pane, "f.txt")
        labels = lambda m: [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) != "separator"]
        self.select(pane)
        empty = labels(self.tab.build_context_menu(pane))
        self.assertNotIn("Rename  F2", empty)
        self.assertIn("📄 New file", empty)
        self.assertIn("Paste here  Ctrl+V", empty)
        self.select(pane, "f.txt")
        one_file = labels(self.tab.build_context_menu(pane))
        for want in ("✎ Edit  F4", "Rename  F2", "Copy to this PC  F5", "Move to this PC  F6", "Copy  Ctrl+C", "Cut  Ctrl+X", "🗑 Delete  F8",
                     "Copy path", "Type path into the SSH terminal"):
            self.assertIn(want, one_file)
        self.select(pane, "adir")
        self.assertNotIn("✎ Edit  F4", labels(self.tab.build_context_menu(pane)))                 # no editor for a folder
        self.select(pane, "f.txt", "adir")
        self.assertNotIn("Rename  F2", labels(self.tab.build_context_menu(pane)))                 # rename is for one item
        local = labels(self.tab.build_context_menu(self.tab.local))
        self.assertIn("Select all  Ctrl+A", local)
        self.assertNotIn("Type path into the SSH terminal", local)
        self.assertIn("Copy to server  F5", labels(self.tab.build_context_menu(self._with_selection(self.tab.local))))

    def _with_selection(self, pane):
        self.put(self.pc_root, "local.txt")
        pane.refresh()
        self.app.pump(lambda: "local.txt" in self.names(pane))
        self.select(pane, "local.txt")
        return pane

    def test_paste_item_is_disabled_until_something_was_copied(self):
        pane = self.tab.remote
        m = self.tab.build_context_menu(pane)
        idx = next(i for i in range(m.index("end") + 1) if m.type(i) != "separator" and m.entrycget(i, "label").startswith("Paste here"))
        self.assertEqual(str(m.entrycget(idx, "state")), "disabled")
        self.put(self.srv_root, "f.txt")
        pane.refresh()
        self.app.pump(lambda: "f.txt" in self.names(pane))
        self.select(pane, "f.txt")
        self.tab.clip_set(pane)
        m = self.tab.build_context_menu(pane)
        self.assertEqual(str(m.entrycget(idx, "state")), "normal")


class ClipboardTests(Base):
    def setUp(self):
        super().setUp()
        self.put(self.srv_root, "docs/report.txt", b"report")
        self.put(self.srv_root, "docs/tree/deep/x.bin", b"deep")
        self.put(self.srv_root, "notes.txt", b"notes")
        os.makedirs(os.path.join(self.srv_root, "archive"))
        self.pane = self.tab.remote
        self.pane.refresh()
        self.app.pump(lambda: "docs" in self.names(self.pane))

    def paste_in(self, path):
        self.go(self.pane, path)
        self.assertTrue(self.tab.paste(self.pane))
        self.app.pump(lambda: self.app.status.get().startswith(("Copied", "Moved")) or bool(self.errors))
        self.app.pump(lambda: False, 0.4)                                           # let the refresh that follows a paste finish
        self.app.pump(lambda: self.pane.status.get() != "Loading ...")

    def test_copy_a_file_into_the_same_folder_gets_a_new_name(self):
        self.select(self.pane, "notes.txt")
        self.assertTrue(self.tab.clip_set(self.pane))
        self.paste_in("/")
        self.assertIn("notes (copy).txt", self.names(self.pane))
        with open(os.path.join(self.srv_root, "notes (copy).txt"), "rb") as f:
            self.assertEqual(f.read(), b"notes")
        self.assertTrue(self.tab.paste(self.pane))                                   # again: (copy 2)
        self.app.pump(lambda: "notes (copy 2).txt" in self.names(self.pane))
        self.assertIn("notes (copy 2).txt", self.names(self.pane))
        self.assertIn("notes.txt", self.names(self.pane))                           # the original stays

    def test_copy_a_folder_tree_to_another_folder(self):
        self.select(self.pane, "docs")
        self.tab.clip_set(self.pane)
        self.paste_in("/archive")
        with open(os.path.join(self.srv_root, "archive", "docs", "tree", "deep", "x.bin"), "rb") as f:
            self.assertEqual(f.read(), b"deep")
        self.assertTrue(os.path.exists(os.path.join(self.srv_root, "docs", "report.txt")))      # a copy leaves the original
        self.assertIn("Copied 1 item", self.app.status.get())

    def test_cut_and_paste_moves(self):
        self.select(self.pane, "notes.txt", "docs")
        self.tab.clip_set(self.pane, cut=True)
        self.assertIn("Cut 2", self.pane.status.get())
        self.paste_in("/archive")
        self.assertFalse(os.path.exists(os.path.join(self.srv_root, "notes.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.srv_root, "docs")))
        self.assertTrue(os.path.exists(os.path.join(self.srv_root, "archive", "docs", "report.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.srv_root, "archive", "notes.txt")))
        self.assertIsNone(self.tab.clip)                                             # a cut can be pasted once

    def test_cut_into_the_same_folder_does_nothing(self):
        self.select(self.pane, "notes.txt")
        self.tab.clip_set(self.pane, cut=True)
        self.assertFalse(self.tab.paste(self.pane))
        self.assertTrue(os.path.exists(os.path.join(self.srv_root, "notes.txt")))

    def test_cut_onto_an_existing_name_is_refused_and_loses_nothing(self):
        self.put(self.srv_root, "archive/notes.txt", b"older")
        self.select(self.pane, "notes.txt")
        self.tab.clip_set(self.pane, cut=True)
        self.paste_in("/archive")
        self.assertTrue(any("already exists" in e for e in self.errors), self.errors)
        with open(os.path.join(self.srv_root, "notes.txt"), "rb") as f:
            self.assertEqual(f.read(), b"notes")                                      # still there
        with open(os.path.join(self.srv_root, "archive", "notes.txt"), "rb") as f:
            self.assertEqual(f.read(), b"older")

    def test_nothing_to_paste_and_empty_selection(self):
        self.assertFalse(self.tab.paste(self.pane))
        self.select(self.pane)
        self.assertFalse(self.tab.clip_set(self.pane))

    def test_a_copy_failure_is_reported(self):
        self.select(self.pane, "notes.txt")
        self.tab.clip_set(self.pane)
        with mock.patch.object(DirBackend, "copy", side_effect=OSError("cp: permission denied")):
            self.paste_in("/archive")
        self.assertTrue(any("permission denied" in e for e in self.errors))

    def test_pc_side_copy_and_paste_works_too(self):
        self.put(self.pc_root, "a.txt", b"local")
        local = self.tab.local
        local.refresh()
        self.app.pump(lambda: "a.txt" in self.names(local))
        self.select(local, "a.txt")
        self.tab.clip_set(local)
        self.assertTrue(self.tab.paste(local))
        self.app.pump(lambda: "a (copy).txt" in self.names(local))
        self.assertIn("a (copy).txt", self.names(local))

    def test_pasting_on_the_other_side_transfers(self):
        self.put(self.pc_root, "up.txt", b"uploaded")
        local = self.tab.local
        local.refresh()
        self.app.pump(lambda: "up.txt" in self.names(local))
        self.select(local, "up.txt")
        self.tab.clip_set(local)
        self.assertTrue(self.tab.paste(self.pane))                                    # copy on this PC, paste on the server = upload
        self.assertTrue(self.wait_jobs())
        with open(os.path.join(self.srv_root, "up.txt"), "rb") as f:
            self.assertEqual(f.read(), b"uploaded")
        self.assertTrue(os.path.exists(os.path.join(self.pc_root, "up.txt")))
        # server -> PC with a cut: download, then the server's copy is removed
        self.select(self.pane, "notes.txt")
        self.tab.clip_set(self.pane, cut=True)
        self.tab.paste(local)
        self.assertTrue(self.app.pump(lambda: os.path.exists(os.path.join(self.pc_root, "notes.txt")) and not os.path.exists(os.path.join(self.srv_root, "notes.txt")), 10))

    def test_new_file(self):
        with mock.patch.object(rf.simpledialog, "askstring", return_value="fresh.txt"):
            self.tab.focus_pane = self.pane
            self.select(self.pane)
            self.tab.new_file()
        self.app.pump(lambda: "fresh.txt" in self.names(self.pane))
        self.assertEqual(os.path.getsize(os.path.join(self.srv_root, "fresh.txt")), 0)
        with mock.patch.object(rf.simpledialog, "askstring", return_value="fresh.txt"):
            self.tab.new_file()                                                       # again: the server says it exists
        self.app.pump(lambda: bool(self.errors))
        self.assertTrue(self.errors)


class PathTests(Base):
    def setUp(self):
        super().setUp()
        self.put(self.srv_root, "my docs/a b.txt")
        self.put(self.srv_root, "my docs/it's.txt")
        self.go(self.tab.remote, "/my docs")

    def test_copy_path_and_quoting(self):
        pane = self.tab.remote
        self.select(pane)
        self.assertEqual(self.tab.paths_text(pane), "/my docs")                      # nothing selected: the folder itself
        self.select(pane, "a b.txt", "it's.txt")
        self.assertEqual(sorted(self.tab.paths_text(pane).split("\n")), ["/my docs/a b.txt", "/my docs/it's.txt"])
        quoted = sorted(self.tab.paths_text(pane, quote=True).split("\n"))
        self.assertEqual(quoted, ["'/my docs/a b.txt'", "'/my docs/it'\"'\"'s.txt'"])
        self.select(pane, "a b.txt")
        self.tab.copy_path(pane)
        self.assertEqual(self.tab.clipboard_get(), "/my docs/a b.txt")

    def test_path_is_typed_into_this_servers_ssh_terminal(self):
        pane = self.tab.remote
        self.select(pane, "a b.txt")
        self.assertFalse(self.tab.path_to_terminal(pane))                            # no terminal open: told so
        self.assertTrue(self.errors)
        term = mock.Mock(host={"id": "h1", "name": "nas"}, kind="ssh")
        other = mock.Mock(host={"id": "other"}, kind="ssh")
        telnet = mock.Mock(host={"id": "h1"}, kind="telnet")
        self.app.terminals = [other, telnet, term]
        self.assertTrue(self.tab.path_to_terminal(pane))
        term.send_user.assert_called_once_with("'/my docs/a b.txt' ")                # quoted, a space after, no Enter
        other.send_user.assert_not_called()
        telnet.send_user.assert_not_called()


class QueueTests(Base):
    def setUp(self):
        super().setUp()
        self.put(self.pc_root, "one.txt", b"1")
        self.put(self.pc_root, "two.txt", b"2")
        self.tab.local.refresh()
        self.app.pump(lambda: "two.txt" in self.names(self.tab.local))

    def upload_all(self):
        self.select(self.tab.local, "one.txt", "two.txt")
        self.tab.transfer(self.tab.local, self.tab.remote, "/")

    def test_pause_holds_the_queue_and_resume_runs_it(self):
        self.tab.toggle_pause()
        self.assertEqual(self.tab.pause_btn.cget("text"), "▶ Resume")
        self.upload_all()
        self.app.pump(lambda: False, 1.2)
        self.assertEqual([j["state"] for j in self.tab.jobs], ["queued", "queued"])
        self.assertIn("(paused)", self.tab.qstatus.get())
        self.assertEqual(os.listdir(self.srv_root), [])
        self.tab.toggle_pause()
        self.assertEqual(self.tab.pause_btn.cget("text"), "⏸ Pause")
        self.assertTrue(self.wait_jobs())
        self.assertEqual(sorted(os.listdir(self.srv_root)), ["one.txt", "two.txt"])
        self.assertNotIn("(paused)", self.tab.qstatus.get())

    def test_retry_failed_requeues_only_the_failed_ones(self):
        self.tab.xb.fail_uploads = 1                                                  # the first upload fails once
        self.upload_all()
        self.assertTrue(self.wait_jobs())
        states = sorted(j["state"] for j in self.tab.jobs)
        self.assertEqual(states, ["done", "error"])
        self.assertIn("1 failed", self.tab.qstatus.get())
        self.assertIn("disk is full", [j for j in self.tab.jobs if j["state"] == "error"][0]["error"])
        self.assertEqual(self.tab.retry_failed(), 1)
        self.assertTrue(self.wait_jobs())
        self.assertEqual(sorted(j["state"] for j in self.tab.jobs), ["done", "done"])
        self.assertEqual(sorted(os.listdir(self.srv_root)), ["one.txt", "two.txt"])
        self.assertEqual(self.tab.retry_failed(), 0)                                  # nothing left to retry
        self.assertNotIn("failed", self.tab.qstatus.get())

    def test_transfer_with_explicit_items_uses_the_remembered_folder(self):
        sub = os.path.join(self.pc_root, "sub")
        os.makedirs(sub)
        self.put(sub, "deep.txt", b"deep")
        local = self.tab.local
        self.tab.transfer(local, self.tab.remote, "/", items=[("deep.txt", False, 4, 0)], src_dir=sub)       # the pane itself is elsewhere
        self.assertTrue(self.wait_jobs())
        with open(os.path.join(self.srv_root, "deep.txt"), "rb") as f:
            self.assertEqual(f.read(), b"deep")


if __name__ == "__main__":
    unittest.main()
