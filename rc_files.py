"""RC file manager: two panes (this PC | the server), drag and drop, transfer queue with progress,
synchronize with a preview, edit in place, permissions, bookmarks.

Imported by rc_manager when the Files button is used (this module imports rc_manager by name)."""

import os
import posixpath
import shutil
import stat as statmod
import tempfile
import threading
import time
import tkinter as tk
import uuid
from tkinter import messagebox, simpledialog, ttk

import rc_manager as core

TOL = 2  # seconds: timestamps closer than this count as equal (servers round times)
SCAN_LIMIT = 30000


def stamp(mtime):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else ""


def breadcrumb_segments(path, backend, limit=7):
    """[(label, full_path)] from the root down to `path`, using the backend's own rules (so it works for both / and C:\\).
    A long path keeps its last `limit` pieces and starts with an ellipsis entry that goes to the first one dropped."""
    if path and backend.parent(path) != path:
        path = path.rstrip("/\\") or path            # '/home/ron/' and '/home/ron' are the same place
    out, cur = [], path
    while cur:
        par = backend.parent(cur)
        label = cur if par == cur else (cur.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or cur)
        out.append((label, cur))
        if par == cur:
            break
        cur = par
    out.reverse()
    if len(out) > limit:
        out = [("…", out[-limit - 1][1])] + out[-limit:]
    return out


def unique_name(name, existing, marker="copy"):
    """'a.txt' -> 'a (copy).txt' -> 'a (copy 2).txt' ... the first one not in `existing` (case-insensitive)."""
    taken = {e.lower() for e in existing}
    if name.lower() not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    n = 1
    while True:
        cand = f"{stem} ({marker}{'' if n == 1 else ' ' + str(n)}){ext}"
        if cand.lower() not in taken:
            return cand
        n += 1


def sort_entries(entries, col="name", reverse=False, show_hidden=True, needle=""):
    """Folders first, then files; both ordered by the chosen column (folders by name unless sorting by name). Optional name
    filter and hiding of dot-files."""
    needle = (needle or "").lower()
    rows = [e for e in entries if (show_hidden or not e[0].startswith(".")) and (not needle or needle in e[0].lower())]
    key = {"name": lambda e: e[0].lower(), "size": lambda e: (e[2], e[0].lower()), "modified": lambda e: (e[3], e[0].lower())}[col]
    dirs = sorted((e for e in rows if e[1]), key=(key if col == "name" else (lambda e: e[0].lower())), reverse=(reverse and col == "name"))
    files = sorted((e for e in rows if not e[1]), key=key, reverse=reverse)
    return dirs + files


FILE_COLUMNS = ("size", "modified", "link")
COLUMN_LABELS = {"size": "Size", "modified": "Modified", "link": "Links to"}
TYPE_AHEAD_RESET = 1.0       # seconds without typing before the typed letters start over


def type_ahead_match(names, typed, current=-1):
    """Index of the entry to select when `typed` letters have been typed: the first name starting with them (case-insensitive),
    looking after `current` first when the same single letter is pressed again (so a letter cycles through its files).
    None if nothing matches."""
    t = (typed or "").lower()
    if not t:
        return None
    n = len(names)
    start = current + 1 if (len(t) > 0 and len(set(t)) == 1 and 0 <= current < n and names[current].lower().startswith(t[0])) else 0
    for k in range(n):
        i = (start + k) % n
        if names[i].lower().startswith(t):
            return i
    if len(set(t)) == 1:                    # "aaa" cycling: fall back to the single letter
        for k in range(n):
            i = (start + k) % n
            if names[i].lower().startswith(t[0]):
                return i
    return None


def clean_columns(value):
    """The saved list of visible columns -> a list of known names, in display order (default: size and modified)."""
    if not isinstance(value, (list, tuple)):
        return ["size", "modified"]
    return [c for c in FILE_COLUMNS if c in value]


# ─── this PC ─────────────────────────────────────────────────────────────────

class LocalBackend:
    kind = "local"

    @staticmethod
    def join(base, name):
        return os.path.join(base, name)

    @staticmethod
    def parent(path):
        stripped = path.rstrip("\\/")
        p = os.path.dirname(stripped)
        return path if (not p or p == stripped) else p      # a drive or share root is its own parent (not "C:", which means "the current folder on C")

    def home(self):
        return os.path.expanduser("~")

    def listdir(self, path):
        out, links = [], {}
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat()
                    out.append((e.name, e.is_dir(), 0 if e.is_dir() else st.st_size, st.st_mtime))
                    if e.is_symlink():
                        links[e.name] = os.readlink(e.path).removeprefix("\\\\?\\")      # Windows adds \\?\ to the path
                except OSError:
                    continue
        self.last_links = links
        return out

    def symlink(self, target, path):
        os.symlink(target, path, target_is_directory=os.path.isdir(target))

    def stat(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        return statmod.S_ISDIR(st.st_mode), st.st_size, st.st_mtime, statmod.S_IMODE(st.st_mode)

    def mkdir(self, path):
        os.mkdir(path)

    def touch(self, path):
        open(path, "x").close()

    def copy(self, src, dst, is_dir):
        (shutil.copytree if is_dir else shutil.copy2)(src, dst)

    def ensure_dir(self, path):
        os.makedirs(path, exist_ok=True)

    def remove(self, path, is_dir):
        (os.rmdir if is_dir else os.remove)(path)

    def rename(self, old, new):
        os.rename(old, new)

    def chmod(self, path, mode):
        os.chmod(path, mode)

    def set_mtime(self, path, mtime):
        try:
            os.utime(path, (mtime, mtime))
        except OSError:
            pass

    def close(self):
        pass


def quick_folders():
    home = os.path.expanduser("~")
    out = [("Home", home)]
    for n in ("Desktop", "Documents", "Downloads"):
        p = os.path.join(home, n)
        if os.path.isdir(p):
            out.append((n, p))
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        if os.path.isdir(letter + ":\\"):
            out.append((f"Drive {letter}:", letter + ":\\"))
    return out


# ─── synchronize: what to copy, decided without touching anything ────────────

def differs(src, dst, mode):
    """src/dst are (is_dir, size, mtime). Should the file be copied from src to dst?"""
    known = bool(src[2] and dst[2])
    if mode == "mirror":
        return src[1] != dst[1] or (known and abs(src[2] - dst[2]) > TOL)
    if known:
        return src[2] > dst[2] + TOL
    return src[1] != dst[1]


def plan_sync(L, R, direction, mode="sync", delete=False):
    """L and R map relative path -> (is_dir, size, mtime). Returns [(action, rel, size)].
    direction: to_remote | to_local | both.  Actions: upload download mkdir_remote mkdir_local
    delete_remote delete_local."""
    acts = []
    deletes = set()
    for rel in sorted(set(L) | set(R)):
        l, r = L.get(rel), R.get(rel)
        if direction == "both":
            if l and not r:
                acts.append(("mkdir_remote" if l[0] else "upload", rel, l[1]))
            elif r and not l:
                acts.append(("mkdir_local" if r[0] else "download", rel, r[1]))
            elif not l[0] and not r[0] and l[2] and r[2] and abs(l[2] - r[2]) > TOL:
                acts.append(("upload", rel, l[1]) if l[2] > r[2] else ("download", rel, r[1]))
            continue
        to_remote = direction == "to_remote"
        src, dst = (l, r) if to_remote else (r, l)
        up = "upload" if to_remote else "download"
        mk = "mkdir_remote" if to_remote else "mkdir_local"
        if src and not dst:
            acts.append((mk if src[0] else up, rel, src[1]))
        elif src and dst and src[0] == dst[0] and not src[0] and differs(src, dst, mode):
            acts.append((up, rel, src[1]))
        elif dst and not src and delete:
            deletes.add(rel)
    # delete only the top-most missing folders/files (a deleted folder takes its contents with it)
    for rel in sorted(deletes):
        if not any(rel.startswith(d + "/") for d in deletes):
            acts.append(("delete_remote" if direction == "to_remote" else "delete_local", rel, 0))
    order = {"mkdir_remote": 0, "mkdir_local": 0, "upload": 1, "download": 1, "delete_remote": 2, "delete_local": 2}
    acts.sort(key=lambda a: (order[a[0]], a[1].count("/") if order[a[0]] == 0 else 0, a[1]))
    return acts


def scan_local(root):
    out, count = {}, 0
    for base, dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root).replace("\\", "/")
        rel_base = "" if rel_base == "." else rel_base
        for d in dirs:
            out[(rel_base + "/" + d).lstrip("/")] = (True, 0, 0)
        for f in files:
            try:
                st = os.stat(os.path.join(base, f))
            except OSError:
                continue
            out[(rel_base + "/" + f).lstrip("/")] = (False, st.st_size, st.st_mtime)
        count += len(files) + len(dirs)
        if count > SCAN_LIMIT:
            raise RuntimeError(f"More than {SCAN_LIMIT} items - pick a smaller folder to synchronize.")
    return out


def scan_remote(backend, root):
    out, stack, count = {}, [("", root)], 0
    while stack:
        rel, path = stack.pop()
        for name, is_dir, size, mtime in backend.listdir(path):
            r = (rel + "/" + name).lstrip("/")
            out[r] = (is_dir, size, mtime)
            if is_dir:
                stack.append((r, backend.join(path, name)))
        count = len(out)
        if count > SCAN_LIMIT:
            raise RuntimeError(f"More than {SCAN_LIMIT} items - pick a smaller folder to synchronize.")
    return out


# ─── one pane ────────────────────────────────────────────────────────────────

class Pane(ttk.Frame):
    def __init__(self, tab, side, backend, lock):
        super().__init__(tab.panes)
        self.tab, self.side, self.backend, self.lock = tab, side, backend, lock
        self.path, self.history, self.hpos, self.token = "", [], -1, 0
        self.raw, self.entries, self.links = [], {}, {}
        self.typed, self.typed_at = "", 0.0
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        title = "This PC" if side == "local" else tab.host["name"]
        ttk.Label(bar, text=("💻 " if side == "local" else "🖥 ") + title, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(2, 8))
        for text, cmd in (("◀", self.back), ("▶", self.forward), ("⬆", self.up), ("⟳", self.refresh)):
            ttk.Button(bar, text=text, width=3, command=cmd).pack(side="left")
        self.book = ttk.Menubutton(bar, text="★ ▾", width=5)
        self.book.pack(side="left", padx=2)
        self.book.configure(menu=tk.Menu(self.book, tearoff=0, postcommand=self.fill_menu))
        bar2 = ttk.Frame(self)
        bar2.pack(fill="x", pady=2)
        self.path_var = tk.StringVar()
        e = ttk.Entry(bar2, textvariable=self.path_var)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda _: self.cd(self.path_var.get()))
        self.crumbs = ttk.Frame(self)           # a clickable trail: click any folder in the path to go there
        self.crumbs.pack(fill="x")
        self.sort_col, self.sort_rev = "name", False
        self.show_hidden = tk.BooleanVar(value=True)
        self.filter_var = tk.StringVar()
        ttk.Label(bar2, text="🔍").pack(side="left", padx=(6, 0))
        ttk.Entry(bar2, textvariable=self.filter_var, width=12).pack(side="left")
        self.filter_var.trace_add("write", lambda *_: self.render())
        self.tree = ttk.Treeview(self, columns=("size", "modified", "link"), selectmode="extended", height=14)
        for col, key in (("#0", "name"), ("size", "size"), ("modified", "modified")):
            self.tree.heading(col, text={"name": "Name", "size": "Size", "modified": "Modified"}[key], command=lambda k=key: self.sort_by(k))
        self.tree.heading("link", text="Links to")
        self.tree.column("#0", width=230)
        self.tree.column("size", width=70, anchor="e")
        self.tree.column("modified", width=115)
        self.tree.column("link", width=160)
        self.apply_columns()
        sb = ttk.Scrollbar(self, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, foreground="#555").pack(side="bottom", anchor="w")
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self.on_open)
        self.tree.bind("<Return>", self.on_open)
        self.tree.bind("<BackSpace>", lambda e: self.up())
        self.tree.bind("<Delete>", lambda e: tab.delete())
        self.tree.bind("<Key>", self.on_type, add="+")
        self.tree.bind("<Button-3>", self.context_menu)
        self.tree.bind("<Control-a>", lambda e: (self.tree.selection_set(self.tree.get_children()), "break")[1])
        self.tree.bind("<Control-c>", lambda e: (tab.clip_set(self, cut=False), "break")[1])
        self.tree.bind("<Control-x>", lambda e: (tab.clip_set(self, cut=True), "break")[1])
        self.tree.bind("<Control-v>", lambda e: (tab.paste(self), "break")[1])
        self.tree.bind("<FocusIn>", lambda e: setattr(tab, "focus_pane", self))
        self.tree.bind("<Button-1>", lambda e: setattr(tab, "focus_pane", self), add="+")
        self.tree.bind("<ButtonPress-1>", self.drag_start)
        self.tree.bind("<B1-Motion>", self.drag_move)
        self.tree.bind("<ButtonRelease-1>", self.drag_end)
        self.drag = None

    # listing
    def cd(self, path, push=True, quiet=False):
        """Go to a folder. A quiet reload (refresh) never overrides a navigation made meanwhile."""
        path = path.strip() or self.backend.home()
        if not quiet:
            self.token += 1
        token = self.token
        self.status.set("Loading ...")

        def work():
            try:
                with self.lock:
                    entries = self.backend.listdir(path)
                    links = dict(getattr(self.backend, "last_links", None) or {})
                err = None
            except Exception as e:
                entries, links, err = None, {}, str(e) or e.__class__.__name__

            def done():
                if token != self.token or self.tab.closed or (quiet and self.path != path):
                    return
                if err:
                    self.status.set("Error: " + err)
                    messagebox.showerror("Files", f"Can't open {path}:\n{err}")
                    return
                self.path, self.raw, self.links = path, entries, links
                self.path_var.set(path)
                if push:
                    del self.history[self.hpos + 1:]
                    self.history.append(path)
                    self.hpos = len(self.history) - 1
                self.render()
                self.tab.remember(self, path)
            self.tab.app.post(done)
        threading.Thread(target=work, daemon=True).start()

    def render(self):
        self.tree.delete(*self.tree.get_children())
        self.entries = {}
        flt = self.filter_var.get().lower()
        for e in sort_entries(self.raw, self.sort_col, self.sort_rev, self.show_hidden.get(), flt):
            iid = self.tree.insert("", "end", text=("🔗 " if e[0] in self.links else "📁 " if e[1] else "📄 ") + e[0],
                                   values=("" if e[1] else core.fmt_size(e[2]), stamp(e[3]), self.links.get(e[0], "")))
            self.entries[iid] = e
        hidden = self.show_hidden.get()
        self.status.set(f"{len(self.raw)} items" + (f" ({len(self.entries)} shown)" if flt or not hidden else ""))
        for col, key, label in (("#0", "name", "Name"), ("size", "size", "Size"), ("modified", "modified", "Modified")):
            self.tree.heading(col, text=label + ((" ▼" if self.sort_rev else " ▲") if key == self.sort_col else ""))
        self.render_crumbs()

    def columns(self):
        prefs = getattr(self.tab.app, "prefs", None)
        return clean_columns(prefs.get("file_columns") if isinstance(prefs, dict) else None)

    def apply_columns(self):
        self.tree.configure(displaycolumns=self.columns())

    def toggle_column(self, name):
        cols = self.columns()
        cols = [c for c in cols if c != name] if name in cols else [c for c in FILE_COLUMNS if c in cols or c == name]
        prefs = getattr(self.tab.app, "prefs", None)
        if isinstance(prefs, dict):
            prefs["file_columns"] = cols
            try:
                import rc_prefs
                rc_prefs.save(prefs)
            except Exception:
                core.log.warning("could not save the file columns", exc_info=True)
        for pane in (getattr(self.tab, "local", None), getattr(self.tab, "remote", None)):
            if pane is not None:
                pane.apply_columns()

    def on_type(self, e):
        """Type a few letters to jump to the file that starts with them (letters start over after a second)."""
        if e.state & 0x4 or e.state & 0x20000 or len(e.char) != 1 or not e.char.isprintable() or (e.char == " " and not self.typed):
            return None
        now = time.time()
        if now - self.typed_at > TYPE_AHEAD_RESET:
            self.typed = ""
        self.typed, self.typed_at = self.typed + e.char, now
        rows = list(self.tree.get_children())
        names = [self.entries[i][0] for i in rows]
        cur = rows.index(self.tree.selection()[0]) if self.tree.selection() and self.tree.selection()[0] in rows else -1
        hit = type_ahead_match(names, self.typed, cur)
        if hit is not None:
            self.tree.selection_set(rows[hit])
            self.tree.focus(rows[hit])
            self.tree.see(rows[hit])
            self.status.set(f"Jumped to “{names[hit]}” (typed: {self.typed})")
        return "break"

    def render_crumbs(self):
        for w in self.crumbs.winfo_children():
            w.destroy()
        if not self.path or self.backend is None:
            return
        for label, full in breadcrumb_segments(self.path, self.backend):
            b = ttk.Button(self.crumbs, text=label, style="Toolbutton", command=lambda p=full: self.cd(p))
            b.pack(side="left")
            ttk.Label(self.crumbs, text="›", foreground="#888").pack(side="left")

    def sort_by(self, key):
        self.sort_rev = not self.sort_rev if key == self.sort_col else False
        self.sort_col = key
        self.render()

    def context_menu(self, e):
        """Right-click: select the row under the pointer (unless it is already part of the selection), then the menu."""
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tab.focus_pane = self
        m = self.tab.build_context_menu(self)
        self.tab.app.theme.paint(m) if hasattr(self.tab.app, "theme") else None
        m.tk_popup(e.x_root, e.y_root)

    def refresh(self):
        self.cd(self.path or self.backend.home(), push=False, quiet=True)

    def up(self):
        if self.path:
            self.cd(self.backend.parent(self.path))

    def back(self):
        if self.hpos > 0:
            self.hpos -= 1
            self.cd(self.history[self.hpos], push=False)

    def forward(self):
        if self.hpos < len(self.history) - 1:
            self.hpos += 1
            self.cd(self.history[self.hpos], push=False)

    def on_open(self, _=None):
        sel = self.tree.selection()
        if sel:
            name, is_dir = self.entries[sel[0]][:2]
            if is_dir:
                self.cd(self.backend.join(self.path, name))
            else:
                self.tab.edit(self)

    def selected(self):
        return [self.entries[i] for i in self.tree.selection() if i in self.entries]

    # bookmarks / quick folders
    def fill_menu(self):
        m = self.nametowidget(self.book.cget("menu"))
        m.delete(0, "end")
        if self.side == "local":
            for label, p in quick_folders():
                m.add_command(label=label, command=lambda p=p: self.cd(p))
            return
        src = self.tab.src
        m.add_command(label="★ Bookmark this folder", command=self.add_bookmark)
        if src["bookmarks"]:
            m.add_separator()
            for b in src["bookmarks"]:
                m.add_command(label=b, command=lambda b=b: self.cd(b))
            m.add_separator()
            rm = tk.Menu(m, tearoff=0)
            for b in src["bookmarks"]:
                rm.add_command(label=b, command=lambda b=b: self.del_bookmark(b))
            m.add_cascade(label="Remove bookmark", menu=rm)

    def add_bookmark(self):
        src = self.tab.src
        if self.path and self.path not in src["bookmarks"]:
            src["bookmarks"].append(self.path)
            core.save_hosts(self.tab.app.hosts)

    def del_bookmark(self, b):
        src = self.tab.src
        if b in src["bookmarks"]:
            src["bookmarks"].remove(b)
            core.save_hosts(self.tab.app.hosts)

    # drag and drop between panes
    def drag_start(self, e):
        iid = self.tree.identify_row(e.y)
        self.drag = {"iid": iid, "x": e.x, "y": e.y, "active": False}
        if iid and iid in self.tree.selection() and len(self.tree.selection()) > 1:
            return "break"  # keep a multi-selection while dragging it

    def drag_move(self, e):
        d = self.drag
        if d and d["iid"] and not d["active"] and (abs(e.x - d["x"]) > 8 or abs(e.y - d["y"]) > 8):
            d["active"] = True
            if d["iid"] not in self.tree.selection():
                self.tree.selection_set(d["iid"])
            self.tree.configure(cursor="hand2")

    def drag_end(self, e):
        d, self.drag = self.drag, None
        self.tree.configure(cursor="")
        if not d:
            return
        if not d["active"]:
            if d["iid"] and d["iid"] in self.tree.selection() and len(self.tree.selection()) > 1:
                self.tree.selection_set(d["iid"])
            return
        target = self.tab.winfo_containing(e.x_root, e.y_root)
        other = self.tab.remote if self.side == "local" else self.tab.local
        if target is None or not str(target).startswith(str(other.tree)):
            return
        dest = other.path
        row = other.tree.identify_row(e.y_root - other.tree.winfo_rooty())
        if row in other.entries and other.entries[row][1]:
            dest = other.backend.join(other.path, other.entries[row][0])
        self.tab.transfer(self, other, dest)


# ─── the tab ─────────────────────────────────────────────────────────────────

class FilesTab(ttk.Frame):
    def __init__(self, app, host, user, password):
        super().__init__(app.nb)
        self.app, self.host, self.user, self.password = app, host, user, password
        self.src = host.get("_src", host)
        self.closed = False
        self.rlock, self.xlock = threading.Lock(), threading.Lock()
        self.remote_backend = self.xb = None
        self.jobs, self.qlock, self.wake = [], threading.Lock(), threading.Event()
        self.go = threading.Event()          # set = the queue may start the next job; cleared by Pause
        self.go.set()
        self.clip = None                     # {"side", "dir", "items": [entries], "cut": bool}
        self.worker = None
        self.tmp = tempfile.mkdtemp(prefix="rc_edit_")
        self.policy = tk.StringVar(value="Overwrite if newer")
        self.policy_value = self.policy.get()  # the worker thread reads this copy (Tk may only be used on the UI thread)
        self.policy.trace_add("write", lambda *_: setattr(self, "policy_value", self.policy.get()))

        bars = ttk.Frame(self)  # two rows so the bold buttons never run off the edge of a normal-width window
        bars.pack(fill="x", padx=4, pady=(4, 2))
        rows = (ttk.Frame(bars), ttk.Frame(bars))
        for r in rows:
            r.pack(fill="x", pady=1)
        buttons = (("Copy ▶  F5", lambda: self.copy_over()), ("◀ Copy", lambda: self.copy_over(True)),
                   ("Move  F6", lambda: self.copy_over(move=True)), ("✎ Edit  F4", lambda: self.edit()),
                   ("↗ Open", lambda: self.open_external()), ("Rename  F2", self.rename),
                   ("📁 New folder  F7", self.mkdir), ("🗑 Delete  F8", self.delete),
                   ("🔒 Properties", self.properties), ("⇄ Sync…", self.toggle_sync))
        for i, (text, cmd) in enumerate(buttons):
            ttk.Button(rows[0 if i < 5 else 1], text=text, command=cmd).pack(side="left", padx=2)
        self.panes = ttk.PanedWindow(self, orient="horizontal")
        self.local = Pane(self, "local", LocalBackend(), threading.Lock())
        self.remote = Pane(self, "remote", None, self.rlock)
        self.panes.add(self.local, weight=1)
        self.panes.add(self.remote, weight=1)

        self.sync_frame = ttk.LabelFrame(self, text=" Synchronize this PC's folder with the server's folder ", padding=6)
        self.build_sync()
        self.build_queue()
        self.panes.pack(fill="both", expand=True, padx=4)
        self.focus_pane = self.local
        for w in (self.local.tree, self.remote.tree):
            w.bind("<F5>", lambda e: self.copy_over(e.widget is self.remote.tree))
            w.bind("<F6>", lambda e: self.copy_over(e.widget is self.remote.tree, move=True))
            w.bind("<F4>", lambda e: self.edit())
            w.bind("<F2>", lambda e: self.rename())
            w.bind("<F7>", lambda e: self.mkdir())
            w.bind("<F8>", lambda e: self.delete())
        self.local.cd(self.local.backend.home())
        threading.Thread(target=self.connect, daemon=True).start()

    # connection
    def make_backend(self):
        mode, host = self.host.get("file_mode", "SFTP"), self.host
        port = int(host.get("file_port") or core.DEFAULT_FILE_PORT[mode])
        if mode == "SFTP":
            return core.SftpBackend(core.open_ssh_client(self.app, dict(host, ssh_port=port), self.user, self.password))
        if mode in core.CLOUD_FILE_MODES:
            import rc_cloud
            return rc_cloud.open_backend(host, port, self.user, self.password)
        return core.FtpBackend(core.open_ftp(host, port, self.user, self.password))

    def connect(self):
        try:
            backend = self.make_backend()
            start = self.src.get("last_path") or backend.home()
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            self.app.post(lambda: self.remote.status.set("Connection failed: " + msg))
            self.app.post(lambda: messagebox.showerror("Files", f"Could not connect to {self.host['address']}:\n{msg}"))
            return

        def ready():
            self.remote_backend = self.remote.backend = backend
            self.remote.cd(start)
        self.app.post(ready)

    def remember(self, pane, path):
        if pane is self.remote and self.src.get("last_path") != path:
            self.src["last_path"] = path
            try:
                core.save_hosts(self.app.hosts)
            except OSError:
                pass

    def close(self):
        self.closed = True
        self.wake.set()
        for b in (self.remote_backend, self.xb):
            if b:
                threading.Thread(target=b.close, daemon=True).start()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # small blocking helpers on a pane's backend (rename, mkdir ...) --------
    def call(self, pane, label, work, done=None):
        if pane.backend is None:
            return
        pane.status.set(label)

        def run():
            try:
                with pane.lock:
                    result = work()
                err = None
            except Exception as e:
                result, err = None, str(e) or e.__class__.__name__

            def finish():
                if self.closed:
                    return
                if err:
                    pane.status.set("Error: " + err)
                    messagebox.showerror("Files", err)
                elif done:
                    done(result)
            self.app.post(finish)
        threading.Thread(target=run, daemon=True).start()

    def active(self):
        """The pane that has a selection (the one you last clicked in)."""
        if self.remote.tree.selection():
            return self.remote
        if self.local.tree.selection():
            return self.local
        return self.focus_pane

    def rename(self):
        pane = self.active()
        sel = pane.selected()
        if len(sel) != 1:
            messagebox.showinfo("Rename", "Select one item to rename.", parent=self)
            return
        old = sel[0][0]
        new = simpledialog.askstring("Rename", f"New name for “{old}”:", initialvalue=old, parent=self)
        if new and new.strip() and new != old:
            b = pane.backend
            self.call(pane, "Renaming ...", lambda: b.rename(b.join(pane.path, old), b.join(pane.path, new.strip())),
                      lambda _: pane.refresh())

    def mkdir(self):
        pane = self.active()
        name = simpledialog.askstring("New folder", "Folder name:", parent=self)
        if name and name.strip():
            b = pane.backend
            self.call(pane, "Creating folder ...", lambda: b.mkdir(b.join(pane.path, name.strip())), lambda _: pane.refresh())

    # new file, clipboard, paths ---------------------------------------------
    def new_file(self):
        pane = self.active()
        name = simpledialog.askstring("New file", "File name:", parent=self)
        if name and name.strip():
            b = pane.backend
            self.call(pane, "Creating file ...", lambda: b.touch(b.join(pane.path, name.strip())), lambda _: pane.refresh())

    def new_link(self, pane):
        b = pane.backend
        if not hasattr(b, "symlink"):
            messagebox.showinfo("Files", "This kind of connection cannot make symbolic links.", parent=self)
            return
        sel = pane.selected()
        default = b.join(pane.path, sel[0][0]) if len(sel) == 1 else ""
        target = simpledialog.askstring("New symbolic link", "What should the link point to (a path)?", initialvalue=default, parent=self)
        if not target or not target.strip():
            return
        name = simpledialog.askstring("New symbolic link", "Name of the link (made in this folder):",
                                      initialvalue=(sel[0][0] + "-link") if len(sel) == 1 else "", parent=self)
        if not name or not name.strip() or "/" in name or "\\" in name:
            if name and name.strip():
                messagebox.showinfo("Files", "Give the link a plain name, without slashes.", parent=self)
            return

        def work():
            b.symlink(target.strip(), b.join(pane.path, name.strip()))
        self.call(pane, "Creating the link ...", work, lambda _: pane.refresh())

    def clip_set(self, pane, cut=False):
        sel = pane.selected()
        if not sel:
            return False
        self.clip = {"side": pane.side, "dir": pane.path, "items": sel, "cut": cut}
        pane.status.set(f"{'Cut' if cut else 'Copied'} {len(sel)} item(s): paste with Ctrl+V or right-click > Paste here")
        return True

    def paste(self, pane):
        """Paste what was copied/cut. Same side (server to server, PC to PC): done in place, with a new name if it exists.
        Other side: queued as an upload / download."""
        clip = self.clip
        if not clip or pane.backend is None:
            return False
        src_pane = self.local if clip["side"] == "local" else self.remote
        if clip["side"] != pane.side:
            self.transfer(src_pane, pane, pane.path, move=clip["cut"], items=clip["items"], src_dir=clip["dir"])
            if clip["cut"]:
                self.clip = None
            return True
        b, dst_dir, items, cut = pane.backend, pane.path, clip["items"], clip["cut"]
        if cut and clip["dir"] == dst_dir:
            return False                                           # moving into the folder it is already in

        def work():
            with_names = {e[0] for e in b.listdir(dst_dir)}
            done = 0
            for name, is_dir, _size, _mtime in items:
                src = b.join(clip["dir"], name)
                new = name if cut and name not in with_names else unique_name(name, with_names)
                if cut and name in with_names:
                    raise OSError(f"“{name}” already exists in {dst_dir}")
                dst = b.join(dst_dir, new)
                if cut:
                    b.rename(src, dst)
                else:
                    b.copy(src, dst, is_dir)
                with_names.add(new)
                done += 1
            return done

        def finished(n):
            if cut:
                self.clip = None
            self.app.status.set(f"{'Moved' if cut else 'Copied'} {n} item(s)")
            pane.refresh()
        self.call(pane, "Pasting ...", work, finished)
        return True

    def paths_text(self, pane, quote=False):
        """The full path of each selected item (or of the current folder when nothing is selected), one per line."""
        import shlex
        sel = pane.selected()
        paths = [pane.backend.join(pane.path, e[0]) for e in sel] or [pane.path]
        return "\n".join(shlex.quote(p) if quote else p for p in paths)

    def copy_path(self, pane):
        self.clipboard_clear()
        self.clipboard_append(self.paths_text(pane))
        pane.status.set("Path copied to the clipboard")

    def terminals_for_server(self):
        fn = getattr(self.app, "all_terminals", None)
        hid = self.src.get("id")
        return [t for t in (fn() if fn else []) if t.host.get("_src", t.host).get("id") == hid and t.kind == "ssh"]

    def path_to_terminal(self, pane):
        """Type the selected path(s) (quoted for the shell, no Enter) into this server's open SSH terminal."""
        terms = self.terminals_for_server()
        if not terms:
            messagebox.showinfo("Files", "Open an SSH terminal tab for this server first.", parent=self)
            return False
        text = " ".join(self.paths_text(pane, quote=True).split("\n"))
        terms[0].send_user(text + " ")
        pane.status.set("Path typed into the terminal")
        return True

    def build_context_menu(self, pane):
        m = tk.Menu(self, tearoff=0)
        sel = pane.selected()
        one = len(sel) == 1
        other = "server" if pane.side == "local" else "this PC"
        if sel:
            if one and not sel[0][1]:
                m.add_command(label="✎ Edit  F4", command=lambda: self.edit(pane))
                m.add_command(label="↗ Open with its program", command=lambda: self.open_external(pane))
            m.add_command(label=f"Copy to {other}  F5", command=lambda: self.transfer(pane, self.remote if pane is self.local else self.local,
                                                                                       (self.remote if pane is self.local else self.local).path))
            m.add_command(label=f"Move to {other}  F6", command=lambda: self.transfer(pane, self.remote if pane is self.local else self.local,
                                                                                       (self.remote if pane is self.local else self.local).path, move=True))
            m.add_separator()
            m.add_command(label="Copy  Ctrl+C", command=lambda: self.clip_set(pane, False))
            m.add_command(label="Cut  Ctrl+X", command=lambda: self.clip_set(pane, True))
        paste = tk.NORMAL if self.clip else tk.DISABLED
        m.add_command(label="Paste here  Ctrl+V", command=lambda: self.paste(pane), state=paste)
        m.add_separator()
        if one:
            m.add_command(label="Rename  F2", command=self.rename)
        m.add_command(label="📁 New folder  F7", command=self.mkdir)
        m.add_command(label="📄 New file", command=self.new_file)
        m.add_command(label="🔗 New symbolic link…", command=lambda: self.new_link(pane),
                      state=tk.NORMAL if hasattr(pane.backend, "symlink") else tk.DISABLED)
        if sel:
            m.add_command(label="🗑 Delete  F8", command=self.delete)
            m.add_command(label="🔒 Properties", command=self.properties)
        m.add_separator()
        m.add_command(label="Copy path", command=lambda: self.copy_path(pane))
        if pane.side == "remote":
            m.add_command(label="Type path into the SSH terminal", command=lambda: self.path_to_terminal(pane),
                          state=tk.NORMAL if self.terminals_for_server() else tk.DISABLED)
        m.add_separator()
        m.add_command(label="Select all  Ctrl+A", command=lambda: pane.tree.selection_set(pane.tree.get_children()))
        m.add_checkbutton(label="Show hidden files (.name)", variable=pane.show_hidden, command=pane.render)
        cm = tk.Menu(m, tearoff=0)
        shown = pane.columns()
        cm._vars = []                       # keep the variables alive as long as the menu
        for c in FILE_COLUMNS:
            v = tk.BooleanVar(value=c in shown)
            cm._vars.append(v)
            cm.add_checkbutton(label=COLUMN_LABELS[c], variable=v, command=lambda c=c: pane.toggle_column(c))
        m.add_cascade(label="Columns", menu=cm)
        m.add_command(label="⟳ Refresh", command=pane.refresh)
        return m

    def toggle_pause(self):
        if self.go.is_set():
            self.go.clear()
            self.pause_btn.configure(text="▶ Resume")
        else:
            self.go.set()
            self.pause_btn.configure(text="⏸ Pause")
            self.wake.set()
        self.update_qstatus()

    def retry_failed(self):
        """Put every failed transfer back in the queue (the ones that finished are left alone)."""
        again = []
        with self.qlock:
            for j in self.jobs:
                if j["state"] == "error":
                    j.update(state="queued", error="", done=0, cancel=threading.Event())
                    again.append(j)
        for j in again:
            self.paint(j)
        if again:
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self.work_queue, daemon=True)
                self.worker.start()
            self.wake.set()
        return len(again)

    def properties(self):
        pane = self.active()
        sel = pane.selected()
        if not sel:
            messagebox.showinfo("Properties", "Select something first.", parent=self)
            return
        b = pane.backend
        paths = [b.join(pane.path, e[0]) for e in sel]

        def work():
            return [(p, b.stat(p)) for p in paths]

        def done(infos):
            first = infos[0][1]
            if not first:
                messagebox.showerror("Properties", "That item no longer exists.", parent=self)
                return
            mode = first[3]
            head = (f"{sel[0][0]}\n{'Folder' if first[0] else core.fmt_size(first[1])}   {stamp(first[2])}\n"
                    if len(sel) == 1 else f"{len(sel)} items selected\n")
            ans = simpledialog.askstring("Properties / permissions", head + "\nPermissions (octal, e.g. 755). "
                                         "Leave as is to change nothing:", initialvalue=f"{mode:o}", parent=self)
            if ans and ans.strip() != f"{mode:o}":
                try:
                    new = int(ans.strip(), 8)
                    if not 0 <= new <= 0o7777:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("Permissions", "Use octal digits like 644 or 755.", parent=self)
                    return
                self.call(pane, "Changing permissions ...", lambda: [b.chmod(p, new) for p in paths], lambda _: pane.refresh())
        self.call(pane, "Reading ...", work, done)

    def delete(self):
        pane = self.active()
        sel = pane.selected()
        if not sel or pane.backend is None:
            return
        n_dirs = sum(1 for e in sel if e[1])
        where = "this PC" if pane.side == "local" else "the server"
        warn = f"Delete {len(sel)} item(s) from {where}?" + (f"\n{n_dirs} folder(s) will be deleted with everything inside." if n_dirs else "")
        if not messagebox.askyesno("Delete", warn + "\n\nThis can't be undone.", icon="warning", parent=self):
            return
        paths = [(pane.backend.join(pane.path, e[0]), e[1]) for e in sel]
        if pane.side == "local":
            def work():
                for p, is_dir in paths:
                    shutil.rmtree(p) if is_dir else os.remove(p)
            self.call(pane, "Deleting ...", work, lambda _: pane.refresh())
        else:
            for p, is_dir in paths:
                self.enqueue("rmr", p, "", name=posixpath.basename(p), is_dir=is_dir)

    # copy / move ------------------------------------------------------------
    def copy_over(self, reverse=False, move=False):
        src, dst = (self.remote, self.local) if reverse else (self.local, self.remote)
        if not src.selected():
            # F5 on the other pane's selection
            other = self.remote if src is self.local else self.local
            if other.selected():
                src, dst = other, src
        self.transfer(src, dst, dst.path, move=move)

    def transfer(self, src, dst, dest_dir, move=False, items=None, src_dir=None):
        sel = items if items is not None else src.selected()
        base = src_dir or src.path
        if not sel or src is dst or self.remote.backend is None:
            return
        for name, is_dir, size, mtime in sel:
            if src.side == "local":
                s, d = src.backend.join(base, name), posixpath.join(dest_dir, name)
                self.enqueue("upd" if is_dir else "up", s, d, name=name, size=size, mtime=mtime, move=move)
            else:
                s, d = posixpath.join(base, name), os.path.join(dest_dir, name)
                self.enqueue("downd" if is_dir else "down", s, d, name=name, size=size, mtime=mtime, move=move)

    # edit -------------------------------------------------------------------
    def edit(self, pane=None):
        pane = pane or self.active()
        sel = [e for e in pane.selected() if not e[1]]
        if len(sel) != 1:
            messagebox.showinfo("Edit", "Select one file to edit.", parent=self)
            return
        name, _, size, _ = sel[0]
        if size > 2_000_000:
            messagebox.showinfo("Edit", "That file is over 2 MB - too big for the built-in editor.", parent=self)
            return
        if pane.side == "local":
            EditorTab.open(self, pane.backend.join(pane.path, name), None, name)
            return
        remote_path = pane.backend.join(pane.path, name)
        local = os.path.join(self.tmp, uuid.uuid4().hex[:6] + "_" + name)

        def work():
            with self.xlock:
                self.ensure_xb()
                self.xb.download(remote_path, local)

        def run():
            try:
                work()
                self.app.post(lambda: EditorTab.open(self, local, remote_path, name))
            except Exception as e:
                msg = str(e) or e.__class__.__name__
                self.app.post(lambda: messagebox.showerror("Edit", f"Could not fetch {name}:\n{msg}"))
        pane.status.set(f"Fetching {name} ...")
        threading.Thread(target=run, daemon=True).start()

    def open_external(self, pane=None):
        """Open the file in its Windows program; every time you save it there, it is uploaded again."""
        pane = pane or self.active()
        sel = [e for e in pane.selected() if not e[1]]
        if len(sel) != 1:
            messagebox.showinfo("Open", "Select one file to open.", parent=self)
            return
        name = sel[0][0]
        if pane.side == "local":
            os.startfile(pane.backend.join(pane.path, name))
            return
        remote_path = pane.backend.join(pane.path, name)
        folder = os.path.join(self.tmp, uuid.uuid4().hex[:6])
        os.makedirs(folder)
        local = os.path.join(folder, name)

        def run():
            try:
                with self.xlock:
                    self.ensure_xb()
                    self.xb.download(remote_path, local)
                os.startfile(local)
            except Exception as e:
                msg = str(e) or e.__class__.__name__
                self.app.post(lambda: messagebox.showerror("Open", f"Could not open {name}:\n{msg}"))
                return
            self.app.post(lambda: self.app.status.set(f"{name} is open - saving it there uploads it again"))
            self.watch(local, remote_path, name)
        pane.status.set(f"Fetching {name} ...")
        threading.Thread(target=run, daemon=True).start()

    def watch(self, local, remote_path, name, hours=4):
        """Upload the file again whenever its timestamp changes (checked every 1.5 s)."""
        last = os.path.getmtime(local)
        deadline = time.time() + hours * 3600
        while not self.closed and time.time() < deadline:
            time.sleep(1.5)
            try:
                m = os.path.getmtime(local)
            except OSError:
                return
            if m != last:
                time.sleep(0.6)  # let the program finish writing
                last = os.path.getmtime(local)
                size = os.path.getsize(local)
                self.app.post(lambda: self.enqueue("up", local, remote_path, name=name, size=size, force=True))

    # queue ------------------------------------------------------------------
    def build_queue(self):
        box = ttk.LabelFrame(self, text=" Transfers ", padding=4)
        box.pack(fill="x", padx=4, pady=4, side="bottom")
        top = ttk.Frame(box)
        top.pack(fill="x")
        self.q = ttk.Treeview(box, columns=("dir", "size", "progress", "state"), height=4, selectmode="extended")
        for c, t, w in (("#0", "Item", 260), ("dir", "", 70), ("size", "Size", 80), ("progress", "Progress", 170),
                        ("state", "Status", 280)):
            self.q.heading(c, text=t)
            self.q.column(c, width=w)
        self.q.tag_configure("error", foreground="#b3261e")
        self.q.tag_configure("done", foreground="#1a7f37")
        ttk.Button(top, text="Cancel selected", command=self.cancel_selected).pack(side="left")
        ttk.Button(top, text="Cancel all", command=self.cancel_all).pack(side="left", padx=4)
        ttk.Button(top, text="Clear finished", command=self.clear_finished).pack(side="left")
        self.pause_btn = ttk.Button(top, text="⏸ Pause", command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=4)
        ttk.Button(top, text="↻ Retry failed", command=self.retry_failed).pack(side="left")
        ttk.Label(top, text="   If a file already exists:").pack(side="left")
        ttk.Combobox(top, textvariable=self.policy, state="readonly", width=20,
                     values=["Overwrite if newer", "Overwrite", "Skip", "Ask"]).pack(side="left", padx=4)
        self.qstatus = tk.StringVar()
        ttk.Label(top, textvariable=self.qstatus, foreground="#555").pack(side="right")
        self.q.pack(fill="x", pady=(4, 0))

    def enqueue(self, kind, src, dst, name="", size=0, mtime=0, move=False, is_dir=False, force=False):
        job = {"id": uuid.uuid4().hex[:8], "kind": kind, "src": src, "dst": dst, "name": name or os.path.basename(src),
               "size": size, "mtime": mtime, "move": move, "is_dir": is_dir, "force": force, "done": 0,
               "state": "queued", "error": "", "cancel": threading.Event(), "t0": 0}
        with self.qlock:
            self.jobs.append(job)
        arrow = {"up": "▶", "upd": "▶", "down": "◀", "downd": "◀", "rmr": "🗑", "rml": "🗑", "mkdir": "📁"}[kind]
        self.q.insert("", "end", iid=job["id"], text=job["name"], values=(arrow, "" if kind in ("rmr", "rml", "mkdir") else core.fmt_size(size), "", "queued"))
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self.work_queue, daemon=True)
            self.worker.start()
        self.wake.set()
        self.update_qstatus()
        return job

    def update_qstatus(self):
        with self.qlock:
            waiting = sum(1 for j in self.jobs if j["state"] in ("queued", "running"))
            failed = sum(1 for j in self.jobs if j["state"] == "error")
        self.qstatus.set((f"{waiting} waiting" if waiting else "idle") + (f", {failed} failed" if failed else "")
                         + ("  (paused)" if not self.go.is_set() else ""))

    def paint(self, job):
        if self.closed or not self.q.exists(job["id"]):
            return
        total = job["size"] or 0
        pct = f"{min(100, job['done'] * 100 // total)}%  {core.fmt_size(job['done'])}" if total and job["state"] == "running" else ""
        if job["state"] == "done" and total:
            pct = "100%"
        label = {"queued": "queued", "running": "working ...", "done": "✔ done", "skipped": "skipped (already there)",
                 "cancelled": "cancelled", "error": "✖ " + job["error"]}[job["state"]]
        self.q.item(job["id"], values=(self.q.set(job["id"], "dir"), self.q.set(job["id"], "size"), pct, label),
                    tags=(job["state"],) if job["state"] in ("error", "done") else ())
        self.update_qstatus()

    def cancel_selected(self):
        for iid in self.q.selection():
            self.cancel(iid)

    def cancel_all(self):
        with self.qlock:
            ids = [j["id"] for j in self.jobs if j["state"] in ("queued", "running")]
        for i in ids:
            self.cancel(i)

    def cancel(self, iid):
        with self.qlock:
            for j in self.jobs:
                if j["id"] == iid and j["state"] in ("queued", "running"):
                    j["cancel"].set()
                    if j["state"] == "queued":
                        j["state"] = "cancelled"
                        self.paint(j)

    def clear_finished(self):
        with self.qlock:
            gone = [j for j in self.jobs if j["state"] in ("done", "error", "cancelled", "skipped")]
            self.jobs = [j for j in self.jobs if j not in gone]
        for j in gone:
            if self.q.exists(j["id"]):
                self.q.delete(j["id"])
        self.update_qstatus()

    def ensure_xb(self):
        if self.xb is None:
            self.xb = self.make_backend()

    def work_queue(self):
        while not self.closed:
            while not self.go.wait(0.5):             # Pause: finish the current job, hold the rest
                if self.closed:
                    return
            with self.qlock:
                job = next((j for j in self.jobs if j["state"] == "queued"), None)
            if job is None:
                refresh_needed = getattr(self, "_dirty", False)
                if refresh_needed:
                    self._dirty = False
                    self.app.post(self.refresh_both)
                self.wake.wait(2.0)
                self.wake.clear()
                continue
            job["state"] = "running"
            job["t0"] = time.time()
            self.app.post(lambda j=job: self.paint(j))
            try:
                self.run_job(job)
                if job["state"] == "running":
                    job["state"] = "done"
            except core.Cancelled:
                job["state"] = "cancelled"
            except Exception as e:
                job["state"], job["error"] = "error", (str(e) or e.__class__.__name__)[:150]
            self._dirty = True
            self.app.post(lambda j=job: self.paint(j))

    def refresh_both(self):
        if not self.closed:
            self.local.refresh()
            if self.remote.backend is not None:
                self.remote.refresh()

    def progress_cb(self, job, base=0):
        last = [0.0]

        def cb(done, total):
            if job["cancel"].is_set():
                raise core.Cancelled()
            job["done"] = base + done
            now = time.time()
            if now - last[0] > 0.15:
                last[0] = now
                self.app.post(lambda: self.paint(job))
        return cb

    def should_copy(self, job, src_stat, dst_stat):
        """Apply the 'if a file already exists' choice. src_stat/dst_stat are (is_dir,size,mtime,mode)."""
        if dst_stat is None or job["force"]:
            return True
        policy = self.policy_value
        if policy == "Overwrite":
            return True
        if policy == "Skip":
            return False
        if policy == "Overwrite if newer":
            return src_stat[2] > dst_stat[2] + TOL or (not (src_stat[2] and dst_stat[2]) and src_stat[1] != dst_stat[1])
        name = job["name"]
        ans = core.ui_call(self.app, lambda: messagebox.askyesnocancel(
            "File exists", f"“{name}” already exists at the destination.\n\nOverwrite it?  (No = skip this one)", parent=self))
        if ans is None:
            raise core.Cancelled()
        return bool(ans)

    def run_job(self, job):
        kind = job["kind"]
        if kind == "rml":
            (shutil.rmtree if os.path.isdir(job["src"]) else os.remove)(job["src"])
            return
        with self.xlock:
            self.ensure_xb()
            xb = self.xb
            if kind == "mkdir":
                xb.ensure_dir(job["dst"])
            elif kind == "rmr":
                self.remote_delete(xb, job["src"], job["is_dir"], job)
            elif kind == "up":
                self.upload_file(xb, job, job["src"], job["dst"], 0)
            elif kind == "down":
                self.download_file(xb, job, job["src"], job["dst"], 0)
            elif kind == "upd":
                self.upload_tree(xb, job)
            elif kind == "downd":
                self.download_tree(xb, job)
        if job["move"] and job["state"] == "running":
            self.after_move(job)

    def after_move(self, job):
        if job["kind"] in ("up", "upd"):
            (shutil.rmtree if os.path.isdir(job["src"]) else os.remove)(job["src"])
        else:
            with self.xlock:
                self.remote_delete(self.xb, job["src"], job["kind"] == "downd", job)

    def remote_delete(self, xb, path, is_dir, job):
        if job["cancel"].is_set():
            raise core.Cancelled()
        if is_dir is None:  # synchronize doesn't know whether the server-only item is a file or a folder
            st = xb.stat(path)
            is_dir = bool(st and st[0])
        if is_dir:
            for name, d, _, _ in xb.listdir(path):
                self.remote_delete(xb, xb.join(path, name), d, job)
        xb.remove(path, is_dir)

    def upload_file(self, xb, job, local, remote, base):
        st = xb.stat(remote)
        ls = os.stat(local)
        if not self.should_copy(job, (False, ls.st_size, ls.st_mtime, 0), st):
            job["state"] = "skipped" if job["kind"] == "up" else job["state"]
            return
        xb.ensure_dir(posixpath.dirname(remote))
        try:
            xb.upload(local, remote, self.progress_cb(job, base))
        except core.Cancelled:
            try:
                xb.remove(remote, False)
            except Exception:
                pass
            raise
        xb.set_mtime(remote, ls.st_mtime)

    def download_file(self, xb, job, remote, local, base):
        st = xb.stat(remote)
        if st is None:
            raise FileNotFoundError(remote)
        ls = os.stat(local) if os.path.exists(local) else None
        if not self.should_copy(job, st, (False, ls.st_size, ls.st_mtime, 0) if ls else None):
            job["state"] = "skipped" if job["kind"] == "down" else job["state"]
            return
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        try:
            xb.download(remote, local, self.progress_cb(job, base))
        except core.Cancelled:
            try:
                os.remove(local)
            except OSError:
                pass
            raise
        if st[2]:
            try:
                os.utime(local, (st[2], st[2]))
            except OSError:
                pass

    def upload_tree(self, xb, job):
        files = []
        for base, dirs, names in os.walk(job["src"]):
            rel = os.path.relpath(base, job["src"]).replace("\\", "/")
            rel = "" if rel == "." else rel
            files.append((rel, None))
            for n in names:
                files.append((rel, n))
        job["size"] = sum(os.path.getsize(os.path.join(job["src"], r, n)) for r, n in files if n)
        done = 0
        for rel, n in files:
            if job["cancel"].is_set():
                raise core.Cancelled()
            rdir = posixpath.join(job["dst"], rel) if rel else job["dst"]
            if n is None:
                xb.ensure_dir(rdir)
                continue
            local = os.path.join(job["src"], rel, n) if rel else os.path.join(job["src"], n)
            self.upload_file(xb, job, local, posixpath.join(rdir, n), done)
            done += os.path.getsize(local)
            job["done"] = done

    def download_tree(self, xb, job):
        files, stack = [], [("", job["src"])]
        while stack:
            rel, path = stack.pop()
            files.append((rel, None, 0))
            for name, is_dir, size, _ in xb.listdir(path):
                if is_dir:
                    stack.append((posixpath.join(rel, name) if rel else name, xb.join(path, name)))
                else:
                    files.append((rel, name, size))
        job["size"] = sum(f[2] for f in files)
        done = 0
        for rel, n, size in files:
            if job["cancel"].is_set():
                raise core.Cancelled()
            ldir = os.path.join(job["dst"], *rel.split("/")) if rel else job["dst"]
            if n is None:
                os.makedirs(ldir, exist_ok=True)
                continue
            self.download_file(xb, job, posixpath.join(job["src"], rel, n) if rel else posixpath.join(job["src"], n),
                               os.path.join(ldir, n), done)
            done += size
            job["done"] = done

    # synchronize ------------------------------------------------------------
    def build_sync(self):
        f = self.sync_frame
        self.sv = {"dir": tk.StringVar(value="Update the server (this PC → server)"),
                   "mode": tk.StringVar(value="Synchronize: copy new and newer files"),
                   "delete": tk.BooleanVar(value=False)}
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Direction").pack(side="left")
        ttk.Combobox(row, textvariable=self.sv["dir"], state="readonly", width=38,
                     values=["Update the server (this PC → server)", "Update this PC (server → this PC)",
                             "Both ways (newer file wins)"]).pack(side="left", padx=6)
        ttk.Label(row, text="Mode").pack(side="left", padx=(8, 0))
        ttk.Combobox(row, textvariable=self.sv["mode"], state="readonly", width=38,
                     values=["Synchronize: copy new and newer files", "Mirror: make the target match (any difference)"]
                     ).pack(side="left", padx=6)
        ttk.Checkbutton(row, text="Delete files that exist only on the target", variable=self.sv["delete"]).pack(side="left", padx=6)
        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=4)
        ttk.Button(row2, text="🔍 Preview changes", command=self.sync_preview).pack(side="left")
        ttk.Button(row2, text="Check all", command=lambda: self.check_all(True)).pack(side="left", padx=4)
        ttk.Button(row2, text="Uncheck all", command=lambda: self.check_all(False)).pack(side="left")
        ttk.Button(row2, text="▶ Apply checked changes", command=self.sync_apply).pack(side="left", padx=10)
        ttk.Button(row2, text="Close", command=self.toggle_sync).pack(side="left")
        self.sync_msg = tk.StringVar()
        ttk.Label(row2, textvariable=self.sync_msg, foreground="#555").pack(side="left", padx=10)
        self.plan_tree = ttk.Treeview(f, columns=("action", "size"), height=6, selectmode="browse")
        self.plan_tree.heading("#0", text="  ☑  File or folder (relative path)")
        self.plan_tree.heading("action", text="What will happen")
        self.plan_tree.heading("size", text="Size")
        self.plan_tree.column("#0", width=520)
        self.plan_tree.column("action", width=200)
        self.plan_tree.column("size", width=90, anchor="e")
        self.plan_tree.pack(fill="x")
        self.plan_tree.bind("<Button-1>", self.toggle_check)
        self.plan, self.sync_open = [], False

    def toggle_sync(self):
        if self.sync_open:
            self.sync_frame.pack_forget()
        else:
            self.sync_frame.pack(fill="x", padx=4, pady=2, before=self.panes)
        self.sync_open = not self.sync_open

    ACTION_TEXT = {"upload": "copy to server", "download": "copy to this PC", "mkdir_remote": "create folder on server",
                   "mkdir_local": "create folder on this PC", "delete_remote": "DELETE on server",
                   "delete_local": "DELETE on this PC"}

    def sync_direction(self):
        d = self.sv["dir"].get()
        return "to_remote" if d.startswith("Update the server") else "to_local" if d.startswith("Update this PC") else "both"

    def sync_preview(self):
        if self.remote.backend is None:
            return
        lroot, rroot = self.local.path, self.remote.path
        direction = self.sync_direction()
        mode = "mirror" if self.sv["mode"].get().startswith("Mirror") else "sync"
        delete = self.sv["delete"].get() and direction != "both"
        self.sync_msg.set(f"Comparing {lroot}  ⇄  {rroot} ...")
        self.plan_tree.delete(*self.plan_tree.get_children())

        def work():
            try:
                L = scan_local(lroot)
                with self.xlock:
                    self.ensure_xb()
                    R = scan_remote(self.xb, rroot)
                plan = plan_sync(L, R, direction, mode, delete)
                err = None
            except Exception as e:
                plan, err = None, str(e) or e.__class__.__name__

            def show():
                if err:
                    self.sync_msg.set("Error: " + err)
                    return
                self.plan = [{"action": a, "rel": r, "size": s, "checked": True} for a, r, s in plan]
                self.sync_roots = (lroot, rroot)
                self.paint_plan()
                self.sync_msg.set(f"{len(plan)} change(s) found." if plan else "Already in sync - nothing to do.")
            self.app.post(show)
        threading.Thread(target=work, daemon=True).start()

    def paint_plan(self):
        self.plan_tree.delete(*self.plan_tree.get_children())
        for i, p in enumerate(self.plan):
            self.plan_tree.insert("", "end", iid=str(i), text=("☑  " if p["checked"] else "☐  ") + p["rel"],
                                  values=(self.ACTION_TEXT[p["action"]], "" if p["action"].startswith(("mkdir", "delete")) else core.fmt_size(p["size"])))

    def toggle_check(self, e):
        iid = self.plan_tree.identify_row(e.y)
        if iid:
            self.plan[int(iid)]["checked"] = not self.plan[int(iid)]["checked"]
            self.paint_plan()
            return "break"

    def check_all(self, value):
        for p in self.plan:
            p["checked"] = value
        self.paint_plan()

    def sync_apply(self):
        chosen = [p for p in self.plan if p["checked"]]
        if not chosen:
            self.sync_msg.set("Nothing checked.")
            return
        n_del = sum(1 for p in chosen if p["action"].startswith("delete"))
        if n_del and not messagebox.askyesno("Synchronize", f"{n_del} item(s) will be DELETED. This can't be undone.\n\nContinue?",
                                             icon="warning", parent=self):
            return
        lroot, rroot = self.sync_roots
        for p in chosen:
            rel, local, remote = p["rel"], os.path.join(lroot, *p["rel"].split("/")), posixpath.join(rroot, p["rel"])
            a = p["action"]
            if a == "mkdir_local":
                os.makedirs(local, exist_ok=True)
            elif a == "mkdir_remote":
                self.enqueue("mkdir", "", remote, name=rel)
            elif a == "upload":
                self.enqueue("up", local, remote, name=rel, size=p["size"], force=True)
            elif a == "download":
                self.enqueue("down", remote, local, name=rel, size=p["size"], force=True)
            elif a == "delete_remote":
                self.enqueue("rmr", remote, "", name=rel, is_dir=None)
            elif a == "delete_local":
                self.enqueue("rml", local, "", name=rel)
        self.sync_msg.set(f"{len(chosen)} change(s) queued below.")
        self.plan = []
        self.plan_tree.delete(*self.plan_tree.get_children())


# ─── built-in text editor ────────────────────────────────────────────────────

class EditorTab(ttk.Frame):
    """Edit a text file; Save writes it back (to the server through the transfer queue, for remote files)."""

    @classmethod
    def open(cls, files_tab, local_path, remote_path, name):
        try:
            raw = open(local_path, "rb").read()
        except OSError as e:
            messagebox.showerror("Edit", f"Can't read the file:\n{e}")
            return
        if b"\x00" in raw[:8000]:
            messagebox.showinfo("Edit", f"“{name}” looks like a binary file, so it won't be opened as text.")
            return
        for enc in ("utf-8", "cp1252"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        tab = cls(files_tab, local_path, remote_path, name, text, enc)
        files_tab.app.add_tab(tab, f"✎ {name}")

    def __init__(self, files_tab, local_path, remote_path, name, text, enc):
        super().__init__(files_tab.app.nb)
        self.ft, self.local_path, self.remote_path, self.name, self.enc = files_tab, local_path, remote_path, name, enc
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=4, pady=3)
        ttk.Button(bar, text="💾 Save  Ctrl+S", command=self.save).pack(side="left")
        ttk.Button(bar, text="Close", command=lambda: files_tab.app.close_widget(self)).pack(side="left", padx=6)
        self.info = tk.StringVar(value=(f"{name} on {files_tab.host['name']}" if remote_path else local_path) + f"   ({enc})")
        ttk.Label(bar, textvariable=self.info, foreground="#555").pack(side="left", padx=10)
        self.t = tk.Text(self, wrap="none", undo=True, font=("Consolas", 10))
        ys = ttk.Scrollbar(self, command=self.t.yview)
        xs = ttk.Scrollbar(self, orient="horizontal", command=self.t.xview)
        self.t.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.t.pack(fill="both", expand=True)
        self.t.insert("1.0", text.replace("\r\n", "\n"))
        self.crlf = "\r\n" in text
        self.t.edit_reset()
        self.t.edit_modified(False)
        self.t.bind("<Control-s>", lambda e: (self.save(), "break")[1])
        self.t.bind("<<Modified>>", self.on_mod)
        self.dirty = False

    def on_mod(self, _):
        if self.t.edit_modified():
            self.dirty = True
            self.info.set(self.info.get().rstrip("*") + "  * unsaved")

    def save(self):
        text = self.t.get("1.0", "end-1c")
        if self.crlf:
            text = text.replace("\n", "\r\n")
        try:
            with open(self.local_path, "wb") as f:
                f.write(text.encode(self.enc, errors="replace"))
        except OSError as e:
            messagebox.showerror("Save", f"Could not save:\n{e}")
            return
        if self.remote_path:
            st = os.stat(self.local_path)
            self.ft.enqueue("up", self.local_path, self.remote_path, name=self.name, size=st.st_size, force=True)
        self.t.edit_modified(False)
        self.dirty = False
        self.info.set(self.info.get().replace("  * unsaved", "") + "  (saved)")

    def close(self):
        pass
