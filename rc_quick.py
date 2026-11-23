"""REGTeches RC - the terminal's Quick commands + History panel, the post-login script (expect/send) and the
multi-line paste check.

* History: commands you type in a terminal are remembered (history.json, this PC only). Only lines typed with
  plain keys and Backspace are kept: anything edited with arrow keys, Tab or Ctrl keys, lines typed into a
  full-screen program, lines that start with a space, and anything typed at a Password / passphrase / code
  prompt are skipped, so passwords are not recorded. It can be switched off and cleared.
* Post-login script: per server, lines of `wait-for (regex) => text to send`, run after every connect.
* The pure logic (parsing, the recorder, the runner) has no Tk in it so it can be tested without a window."""

import json
import os
import re
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import rc_manager as core

log = core.log

MAX_HISTORY = 500
MAX_LINE = 2000
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_]|[()][0-9A-Za-z])")
SECRET_PROMPT = re.compile(r"(pass(word|phrase|code)|verification code|one.?time|otp|token|secret|\bpin\b)[^$#%>]*$", re.I)


# ─── history store ───────────────────────────────────────────────────────────

def history_path():
    return os.path.join(core.APP_DIR, "history.json")


def load_store():
    """{'record': bool, 'confirm_paste': bool, 'items': [{'cmd', 'count', 'last'}]} - tolerant of a damaged file."""
    out = {"record": True, "confirm_paste": True, "items": []}
    try:
        with open(history_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as e:
        log.warning("history.json could not be read (%s); starting empty", e)
        return out
    if not isinstance(data, dict):
        return out
    out["record"] = data.get("record") is not False
    out["confirm_paste"] = data.get("confirm_paste") is not False
    for it in data.get("items") or []:
        if isinstance(it, dict) and isinstance(it.get("cmd"), str) and it["cmd"].strip():
            try:
                count = max(1, int(it.get("count") or 1))
            except (TypeError, ValueError):
                count = 1
            try:
                last = float(it.get("last") or 0)
            except (TypeError, ValueError):
                last = 0.0
            out["items"].append({"cmd": it["cmd"], "count": count, "last": last})
    return out


def save_store(store):
    path = history_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1)
    os.replace(tmp, path)


def add_history(cmd, now=None):
    """Remember one command (most recent first, duplicates merged). Returns False if recording is off."""
    store = load_store()
    if not store["record"]:
        return False
    now = time.time() if now is None else now
    for it in store["items"]:
        if it["cmd"] == cmd:
            it["count"] += 1
            it["last"] = now
            break
    else:
        store["items"].append({"cmd": cmd, "count": 1, "last": now})
    store["items"].sort(key=lambda i: i["last"], reverse=True)
    del store["items"][MAX_HISTORY:]
    try:
        save_store(store)
    except OSError as e:
        log.warning("history.json could not be saved: %s", e)
        return False
    return True


class HistoryRecorder:
    """Rebuilds the command lines a person typed from the keys sent to the terminal (see the module notes)."""

    def __init__(self, line_before_cursor, in_alt, save=add_history):
        self.line_before_cursor, self.in_alt, self.save = line_before_cursor, in_alt, save
        self.buf, self.unreliable = [], False

    def typed(self, s):
        if not s:
            return
        if s in ("\x7f", "\b"):
            if self.buf:
                self.buf.pop()
            return
        if s.startswith("\x1b") or s == "\t" or (len(s) == 1 and ord(s) < 32 and s != "\r"):
            self.unreliable = True            # arrows, Tab, Ctrl keys: the shell edits the line, we cannot follow it
            return
        for ch in s.replace("\r\n", "\r").replace("\n", "\r"):
            if ch == "\r":
                self.finish()
            elif ord(ch) >= 32:
                self.buf.append(ch)

    def finish(self):
        line, bad = "".join(self.buf), self.unreliable
        self.buf, self.unreliable = [], False
        cmd = line.strip()
        if bad or not cmd or line.startswith(" ") or len(cmd) > MAX_LINE or self.in_alt():
            return
        if SECRET_PROMPT.search(self.line_before_cursor() or ""):
            return
        self.save(cmd)


# ─── post-login script ───────────────────────────────────────────────────────

MAX_STEPS = 20
DEFAULT_STEP_TIMEOUT = 10


def parse_login_script(text):
    """Lines of `wait-for-regex => text to send`. A line starting with `=>` sends at once; `//` starts a comment.
    Returns [{'wait', 'send', 'line'}]. Raises ValueError('Line N: ...') for a bad regex or too many steps."""
    steps = []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if "=>" not in line:
            raise ValueError(f"Line {n}: write it as  wait-for-text => text to send  (use => even if there is nothing to wait for)")
        wait, _, send = line.partition("=>")
        wait, send = wait.strip(), send.strip()
        if wait == "$":
            wait = "\\$"      # a lone $ would mean "end of text" in a pattern, which is always true: take it as the prompt character
        if wait:
            try:
                re.compile(wait)
            except re.error as e:
                raise ValueError(f"Line {n}: “{wait}” is not a valid pattern ({e})")
        steps.append({"wait": wait, "send": send, "line": n})
    if len(steps) > MAX_STEPS:
        raise ValueError(f"At most {MAX_STEPS} steps.")
    return steps


class ExpectRunner:
    """Waits for each step's pattern in the server's output, then sends its text plus Enter. Gives up (and says so)
    when a step's pattern does not show up in time, so a wrong script cannot type into the wrong place for long."""

    def __init__(self, steps, send, after, cancel, note, subst=lambda s: s, timeout=DEFAULT_STEP_TIMEOUT):
        self.steps, self.send, self.after, self.cancel, self.note, self.subst = steps, send, after, cancel, note, subst
        self.timeout, self.i, self.buf, self.done, self.jobs = timeout, 0, "", False, []

    def start(self):
        self.arm()

    def stop(self):
        self.done = True
        self.clear_jobs()

    def clear_jobs(self):
        for j in self.jobs:
            try:
                self.cancel(j)
            except Exception:
                pass
        self.jobs = []

    def arm(self):
        self.clear_jobs()
        if self.i >= len(self.steps):
            self.done = True
            self.note("login script finished")
            return
        step, idx = self.steps[self.i], self.i
        self.jobs.append(self.after(int(self.timeout * 1000), lambda: self.timed_out(idx)))
        if not step["wait"]:
            self.jobs.append(self.after(400, lambda: self.fire(idx)))

    def feed(self, text):
        if self.done or self.i >= len(self.steps):
            return
        self.buf = (self.buf + _ANSI.sub("", text).replace("\r", ""))[-4000:]
        step = self.steps[self.i]
        if step["wait"] and re.search(step["wait"], self.buf, re.I):
            self.fire(self.i)

    def fire(self, idx):
        if self.done or idx != self.i:
            return
        step = self.steps[self.i]
        self.send(self.subst(step["send"]) + "\r")
        log.info("Login script: step %d of %d sent", self.i + 1, len(self.steps))     # never logs the text (it may be a password)
        self.buf = ""
        self.i += 1
        self.arm()

    def timed_out(self, idx):
        if self.done or idx != self.i:
            return
        self.done = True
        wait = self.steps[idx]["wait"]
        self.note(f"login script stopped: “{wait}” did not appear within {self.timeout:g} s")


def substitute(text, user, host, password):
    return text.replace("${user}", user or "").replace("${host}", host or "").replace("${password}", password or "")


# ─── the panel ───────────────────────────────────────────────────────────────

class QuickPanel(ttk.Frame):
    """Right-hand side panel of a terminal tab: Quick commands (the snippets, grouped) and History."""

    def __init__(self, parent, tab):
        super().__init__(parent, width=320)
        self.tab = tab
        self.pack_propagate(False)
        head = ttk.Frame(self)
        head.pack(fill="x")
        ttk.Label(head, text="⚡ Quick commands", font=("Segoe UI", 10, "bold")).pack(side="left", padx=4, pady=2)
        ttk.Button(head, text="✕", width=3, command=tab.toggle_panel).pack(side="right", padx=2)
        self.book = ttk.Notebook(self)
        self.book.pack(fill="both", expand=True)
        self.book.bind("<<NotebookTabChanged>>", lambda e: self.refresh())
        self.snips, self.hist = [], []
        self.build_quick()
        self.build_history()

    # -- quick commands --
    def build_quick(self):
        f = ttk.Frame(self.book, padding=4)
        self.book.add(f, text=" Commands ")
        self.q_search = tk.StringVar()
        self.q_search.trace_add("write", lambda *_: self.refresh_quick())
        ttk.Entry(f, textvariable=self.q_search).pack(fill="x")
        self.q_tree = ttk.Treeview(f, show="tree", selectmode="browse", height=12)
        self.q_tree.pack(fill="both", expand=True, pady=4)
        self.q_tree.tag_configure("group", font=("Segoe UI", 9, "bold"))
        self.q_tree.bind("<Double-1>", lambda e: self.q_do("paste"))
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        for text, mode in (("▶ Run", "run"), ("Paste", "paste"), ("Copy", "copy")):
            ttk.Button(bar, text=text, command=lambda m=mode: self.q_do(m)).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="Edit…", command=lambda: self.tab.app.open_snippets()).pack(side="right")
        ttk.Label(f, foreground="#555", wraplength=290, justify="left",
                  text="Double-click pastes without pressing Enter. ▶ Run types it and presses Enter.").pack(anchor="w", pady=(4, 0))

    def refresh_quick(self):
        self.snips = core.load_snippets()
        word = self.q_search.get().strip().lower()
        tree = self.q_tree
        tree.delete(*tree.get_children())
        groups = {}
        for i, s in enumerate(self.snips):
            if word and word not in s["name"].lower() and word not in s["command"].lower():
                continue
            groups.setdefault((s.get("group") or "").strip() or "General", []).append((i, s))
        for g, items in sorted(groups.items(), key=lambda kv: (kv[0] != "General", kv[0].lower())):
            gid = tree.insert("", "end", text=f"{g}  ({len(items)})", open=True, tags=("group",))
            for i, s in items:
                tree.insert(gid, "end", iid=f"s{i}", text=s["name"])

    def q_selected(self):
        sel = self.q_tree.selection()
        if sel and sel[0].startswith("s"):
            return self.snips[int(sel[0][1:])]
        return None

    def q_do(self, mode):
        s = self.q_selected()
        if s is None:
            return
        self.use(s["command"], mode, ask=s.get("confirm", True), name=s["name"])

    # -- history --
    def build_history(self):
        f = ttk.Frame(self.book, padding=4)
        self.book.add(f, text=" History ")
        self.h_search = tk.StringVar()
        self.h_search.trace_add("write", lambda *_: self.refresh_history())
        ttk.Entry(f, textvariable=self.h_search).pack(fill="x")
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True, pady=4)
        self.h_tree = ttk.Treeview(wrap, columns=("n",), show="tree headings", selectmode="extended", height=12)
        self.h_tree.heading("#0", text="Command")
        self.h_tree.heading("n", text="×")
        self.h_tree.column("#0", width=210)
        self.h_tree.column("n", width=34, anchor="e", stretch=False)
        sb = ttk.Scrollbar(wrap, command=self.h_tree.yview)
        self.h_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.h_tree.pack(side="left", fill="both", expand=True)
        self.h_tree.bind("<Double-1>", lambda e: self.h_do("paste"))
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        for text, mode in (("▶ Run", "run"), ("Paste", "paste"), ("Copy", "copy")):
            ttk.Button(bar, text=text, command=lambda m=mode: self.h_do(m)).pack(side="left", padx=(0, 4))
        bar2 = ttk.Frame(f)
        bar2.pack(fill="x", pady=(4, 0))
        ttk.Button(bar2, text="Save as command…", command=self.h_save).pack(side="left")
        ttk.Button(bar2, text="Delete", command=self.h_delete).pack(side="left", padx=4)
        ttk.Button(bar2, text="Clear all", command=self.h_clear).pack(side="right")
        self.record = tk.BooleanVar(value=load_store()["record"])
        ttk.Checkbutton(f, text="Remember what I type in terminals", variable=self.record, command=self.set_record).pack(anchor="w", pady=(6, 0))
        ttk.Label(f, foreground="#555", wraplength=290, justify="left",
                  text="Not remembered: lines at a password prompt, lines edited with arrow keys or Tab, lines starting "
                       "with a space, and anything typed in vim/less/top. Stored on this PC only.").pack(anchor="w")

    def refresh_history(self):
        store = load_store()
        self.hist = store["items"]
        word = self.h_search.get().strip().lower()
        tree = self.h_tree
        tree.delete(*tree.get_children())
        for i, it in enumerate(self.hist):
            if word and word not in it["cmd"].lower():
                continue
            tree.insert("", "end", iid=f"h{i}", text=it["cmd"].replace("\n", " ⏎ "), values=(it["count"],))

    def h_selected(self):
        return [self.hist[int(i[1:])] for i in self.h_tree.selection() if i[1:].isdigit() and int(i[1:]) < len(self.hist)]

    def h_do(self, mode):
        sel = self.h_selected()
        if sel:
            self.use(sel[0]["cmd"] if mode == "run" else "\n".join(x["cmd"] for x in sel), mode, ask=False)

    def h_save(self):
        sel = self.h_selected()
        if not sel:
            return
        cmd = sel[0]["cmd"]
        name = simpledialog.askstring("Quick command", "Name for this command:", initialvalue=cmd[:40], parent=self)
        if not name or not name.strip():
            return
        snips = core.load_snippets()
        snips.append({"id": f"h{int(time.time())}", "name": name.strip(), "command": cmd, "confirm": True, "group": "From history"})
        try:
            core.save_snippets(snips)
        except OSError as e:
            messagebox.showerror("Quick command", f"Could not save:\n{e}", parent=self)
            return
        self.refresh_quick()
        self.book.select(0)

    def h_delete(self):
        sel = {s["cmd"] for s in self.h_selected()}
        if sel:
            store = load_store()
            store["items"] = [i for i in store["items"] if i["cmd"] not in sel]
            save_store(store)
            self.refresh_history()

    def h_clear(self):
        if messagebox.askyesno("History", "Forget every remembered command?", parent=self):
            store = load_store()
            store["items"] = []
            save_store(store)
            self.refresh_history()

    def set_record(self):
        store = load_store()
        store["record"] = bool(self.record.get())
        save_store(store)

    # -- shared --
    def use(self, text, mode, ask=True, name=""):
        tab = self.tab
        if mode == "copy":
            tab.clipboard_clear()
            tab.clipboard_append(text)
            tab.app.status.set("Copied to the clipboard")
        elif mode == "paste":
            tab.paste_text(text.replace("\r\n", "\n"))
        else:
            if ask and not messagebox.askyesno("Run", f"Run {'“' + name + '” ' if name else ''}in this terminal"
                                               f"{' and in every ticked Broadcast terminal' if tab.bcast_var.get() else ''}?\n\n{text[:400]}", parent=self):
                return
            tab.send_user(text.replace("\r\n", "\n").replace("\n", "\r") + "\r")
        tab.t.focus_set()

    def refresh(self):
        try:
            label = self.book.tab(self.book.select(), "text").strip()
        except tk.TclError:
            return
        (self.refresh_history if label == "History" else self.refresh_quick)()
