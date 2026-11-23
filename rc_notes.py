"""REGTeches RC - Notes (Tools > Notes, or right-click a server > Notes).

Free-form notes with tags, kept on this PC (notes.json) and optionally attached to a server: rack positions, which
switch port goes where, "how I fixed it last time", a checklist. Notes are written in Markdown (headings, bullets,
**bold**, `code` and code blocks) and the Preview switch shows them formatted. They are saved as you type.
The storage and Markdown reading at the top are pure and tested without a window."""

import json
import os
import re
import time
import tkinter as tk
import uuid
from tkinter import messagebox, ttk

import rc_manager as core

log = core.log

ALL, NO_SERVER = "(all)", "(not attached to a server)"


def notes_path():
    return os.path.join(core.APP_DIR, "notes.json")


def parse_tags(text):
    """'Network, rack 3 ,  network' -> ['network', 'rack 3'] (lower case, no duplicates, in order)."""
    out = []
    for t in re.split(r"[,;]+", text or ""):
        t = " ".join(t.split()).lower()
        if t and t not in out:
            out.append(t)
    return out[:20]


def clean_note(d):
    if not isinstance(d, dict):
        return None
    body = d.get("body") if isinstance(d.get("body"), str) else ""
    title = d.get("title") if isinstance(d.get("title"), str) else ""
    tags = parse_tags(", ".join(d["tags"])) if isinstance(d.get("tags"), list) else parse_tags(d.get("tags") if isinstance(d.get("tags"), str) else "")
    try:
        updated = float(d.get("updated") or 0)
    except (TypeError, ValueError):
        updated = 0.0
    server = d.get("server") if isinstance(d.get("server"), str) else ""
    return {"id": d.get("id") if isinstance(d.get("id"), str) and d.get("id") else uuid.uuid4().hex, "title": title.strip(),
            "body": body, "tags": tags, "server": server, "updated": updated}


def load_notes():
    """All notes, newest first. A damaged file is moved aside (never overwritten) and the list starts empty."""
    try:
        with open(notes_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("not a list")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.warning("notes.json could not be read (%s)", e)
        try:
            os.replace(notes_path(), notes_path() + time.strftime(".corrupt-%Y%m%d-%H%M%S"))
        except OSError:
            pass
        return []
    seen, out = set(), []
    for d in data:
        n = clean_note(d)
        if n and n["id"] not in seen:
            seen.add(n["id"])
            out.append(n)
    return sorted(out, key=lambda n: n["updated"], reverse=True)


def save_notes(notes):
    path = notes_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=1)
    os.replace(tmp, path)


def display_title(n):
    if n["title"]:
        return n["title"]
    first = next((l.strip().lstrip("#").strip() for l in n["body"].splitlines() if l.strip()), "")
    return first[:60] or "(empty note)"


def all_tags(notes):
    return sorted({t for n in notes for t in n["tags"]})


def filter_notes(notes, text="", tag="", server=ALL):
    """Notes matching the search words (all of them, in title, body or tags), a tag, and a server id / NO_SERVER / ALL."""
    words = [w for w in (text or "").lower().split() if w]
    out = []
    for n in notes:
        if tag and tag not in n["tags"]:
            continue
        if server == NO_SERVER and n["server"]:
            continue
        if server not in (ALL, NO_SERVER, "") and n["server"] != server:
            continue
        hay = (display_title(n) + "\n" + n["body"] + "\n" + " ".join(n["tags"])).lower()
        if all(w in hay for w in words):
            out.append(n)
    return out


# ─── Markdown (small subset) ─────────────────────────────────────────────────

INLINE = re.compile(r"(\*\*[^*\n]+\*\*|(?<!\*)\*[^*\s][^*\n]*?\*(?!\*)|`[^`\n]+`)")


def md_blocks(body):
    """Markdown text -> [(kind, text)] with kind in h1 h2 h3 li code p blank."""
    out, in_code = [], False
    if not body:
        return out
    for raw in body.replace("\r", "").split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append(("code", line))
            continue
        m = re.match(r"(#{1,3})\s+(.*)", line)
        if m:
            out.append((f"h{len(m.group(1))}", m.group(2)))
        elif re.match(r"\s*[-*]\s+", line):
            out.append(("li", re.sub(r"^\s*[-*]\s+", "", line)))
        elif not line.strip():
            out.append(("blank", ""))
        else:
            out.append(("p", line))
    return out


def inline_spans(text):
    """'a **b** `c`' -> [('a ', ''), ('b', 'bold'), (' ', ''), ('c', 'code')]"""
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], ""))
        tok = m.group(0)
        out.append((tok[2:-2], "bold") if tok.startswith("**") else (tok[1:-1], "code" if tok.startswith("`") else "italic"))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], ""))
    return out


def render_markdown(widget, body):
    """Fill a (disabled) Text widget with the formatted note."""
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    for kind, text in md_blocks(body):
        if kind == "blank":
            widget.insert("end", "\n")
        elif kind == "code":
            widget.insert("end", text + "\n", "codeblock")
        elif kind.startswith("h"):
            widget.insert("end", text + "\n", kind)
        else:
            if kind == "li":
                widget.insert("end", "  •  ", "li")
            for chunk, style in inline_spans(text):
                widget.insert("end", chunk, (style,) if style else (kind,))
            widget.insert("end", "\n")
    widget.configure(state="disabled")


# ─── the tab ─────────────────────────────────────────────────────────────────

class NotesTab(ttk.Frame):
    AUTOSAVE_MS = 700

    def __init__(self, app):
        super().__init__(app.nb, padding=10)
        self.app = app
        self.notes = load_notes()
        self.cur = None                 # the note being edited (a dict from self.notes) or None
        self.job = None
        self.loading = False
        left = ttk.Frame(self, width=300)
        left.pack(side="left", fill="y")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refill())
        ttk.Entry(left, textvariable=self.search).pack(fill="x")
        flt = ttk.Frame(left)
        flt.pack(fill="x", pady=4)
        self.tag_var, self.server_var = tk.StringVar(value=ALL), tk.StringVar(value=ALL)
        self.tag_box = ttk.Combobox(flt, textvariable=self.tag_var, state="readonly", width=14)
        self.tag_box.pack(side="left")
        self.tag_box.bind("<<ComboboxSelected>>", lambda e: self.refill())
        self.server_map = {ALL: ALL, NO_SERVER: NO_SERVER}
        self.server_box = ttk.Combobox(flt, textvariable=self.server_var, state="readonly", width=20)
        self.server_box.pack(side="left", padx=4)
        self.server_box.bind("<<ComboboxSelected>>", lambda e: self.refill())
        self.listbox = tk.Listbox(left, width=40, height=20, exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.pick())
        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="➕ New note", command=self.new).pack(side="left")
        ttk.Button(bar, text="🗑 Delete", command=self.delete).pack(side="left", padx=4)
        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.title_var, self.tags_var, self.attach_var = tk.StringVar(), tk.StringVar(), tk.StringVar(value=NO_SERVER)
        top = ttk.Frame(right)
        top.pack(fill="x")
        ttk.Label(top, text="Title").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.title_var, width=60).grid(row=0, column=1, sticky="we", padx=6, pady=2)
        ttk.Label(top, text="Tags").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.tags_var, width=60).grid(row=1, column=1, sticky="we", padx=6, pady=2)
        ttk.Label(top, text="Server").grid(row=2, column=0, sticky="w")
        self.attach_box = ttk.Combobox(top, textvariable=self.attach_var, state="readonly", width=40)
        self.attach_box.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        top.columnconfigure(1, weight=1)
        for v in (self.title_var, self.tags_var, self.attach_var):
            v.trace_add("write", lambda *_: self.changed())
        ctl = ttk.Frame(right)
        ctl.pack(fill="x", pady=(6, 2))
        self.preview = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Preview (formatted)", variable=self.preview, command=self.show_mode).pack(side="left")
        self.status = tk.StringVar(value="")
        ttk.Label(ctl, textvariable=self.status, foreground="#555").pack(side="right")
        wrap = ttk.Frame(right)
        wrap.pack(fill="both", expand=True)
        self.body = tk.Text(wrap, wrap="word", font=("Consolas", 10), undo=True)
        self.view = tk.Text(wrap, wrap="word", font=("Segoe UI", 10), state="disabled", cursor="arrow")
        self.view.tag_configure("h1", font=("Segoe UI", 17, "bold"))
        self.view.tag_configure("h2", font=("Segoe UI", 14, "bold"))
        self.view.tag_configure("h3", font=("Segoe UI", 11, "bold"))
        self.view.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        self.view.tag_configure("italic", font=("Segoe UI", 10, "italic"))
        self.view.tag_configure("code", font=("Consolas", 10), background="#eceff1", foreground="#37474f")
        self.view.tag_configure("codeblock", font=("Consolas", 10), background="#eceff1", foreground="#37474f", lmargin1=12, lmargin2=12)
        self.view.tag_configure("li", lmargin1=6)
        self.body.pack(fill="both", expand=True)
        self.body.bind("<<Modified>>", self.on_body_modified)
        self.refresh_filters()
        self.refill()
        self.set_form(None)

    # filters / list
    def hosts_list(self):
        return sorted(self.app.hosts, key=lambda h: h["name"].lower())

    def refresh_filters(self):
        self.tag_box.configure(values=[ALL] + all_tags(self.notes))
        self.server_map = {ALL: ALL, NO_SERVER: NO_SERVER}
        for h in self.hosts_list():
            self.server_map[h["name"]] = h["id"]
        self.server_box.configure(values=list(self.server_map))
        self.attach_map = {NO_SERVER: ""}
        for h in self.hosts_list():
            self.attach_map[h["name"]] = h["id"]
        self.attach_box.configure(values=list(self.attach_map))

    def refill(self, select=None):
        tag = "" if self.tag_var.get() == ALL else self.tag_var.get()
        server = self.server_map.get(self.server_var.get(), ALL)
        self.shown = filter_notes(self.notes, self.search.get(), tag, server)
        self.listbox.delete(0, "end")
        for n in self.shown:
            self.listbox.insert("end", display_title(n) + (f"   [{', '.join(n['tags'])}]" if n["tags"] else ""))
        if select is not None:
            for i, n in enumerate(self.shown):
                if n["id"] == select:
                    self.listbox.selection_set(i)

    def pick(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.flush()
        self.set_form(self.shown[sel[0]])

    # form
    def set_form(self, note):
        self.loading = True
        self.cur = note
        n = note or {"title": "", "body": "", "tags": [], "server": ""}
        self.title_var.set(n["title"])
        self.tags_var.set(", ".join(n["tags"]))
        name = next((h["name"] for h in self.app.hosts if h["id"] == n["server"]), NO_SERVER)
        self.attach_var.set(name)
        self.body.delete("1.0", "end")
        self.body.insert("1.0", n["body"])
        self.body.edit_reset()
        self.body.edit_modified(False)
        self.status.set("" if note else "Click ➕ New note, or pick one on the left.")
        self.loading = False
        if self.preview.get():
            self.show_mode()

    def new(self, server=""):
        self.flush()
        note = clean_note({"title": "", "body": "", "tags": [], "server": server, "updated": time.time()})
        self.notes.insert(0, note)
        self.refresh_filters()
        self.tag_var.set(ALL)
        self.search.set("")
        self.refill(select=note["id"])
        self.set_form(note)
        self.body.focus_set()
        return note

    def collect(self):
        if self.cur is None:
            return None
        self.cur["title"] = self.title_var.get().strip()
        self.cur["tags"] = parse_tags(self.tags_var.get())
        self.cur["body"] = self.body.get("1.0", "end-1c")
        self.cur["server"] = getattr(self, "attach_map", {}).get(self.attach_var.get(), "")
        return self.cur

    def changed(self):
        if self.loading or self.cur is None:
            return
        self.status.set("Editing ...")
        if self.job:
            self.after_cancel(self.job)
        self.job = self.after(self.AUTOSAVE_MS, self.flush)

    def on_body_modified(self, _e):
        if self.body.edit_modified():
            self.body.edit_modified(False)
            self.changed()

    def flush(self):
        """Save the open note now (the list on disk is the notes list; this note's fields come from the form)."""
        if self.job:
            self.after_cancel(self.job)
            self.job = None
        note = self.collect()
        if note is None:
            return False
        if not (note["title"] or note["body"].strip() or note["tags"]):
            return False                                   # a note nobody typed in is not worth keeping
        note["updated"] = time.time()
        try:
            save_notes([n for n in self.notes if n["title"] or n["body"].strip() or n["tags"]])
        except OSError as e:
            self.status.set(f"Could not save: {e}")
            return False
        self.status.set("Saved " + time.strftime("%H:%M:%S"))
        keep = self.listbox.curselection()
        self.refresh_filters()
        self.refill(select=note["id"])
        return True

    def delete(self):
        if self.cur is None:
            return
        if not messagebox.askyesno("Notes", f"Delete “{display_title(self.cur)}”?", parent=self):
            return
        self.notes = [n for n in self.notes if n["id"] != self.cur["id"]]
        if self.job:
            self.after_cancel(self.job)
            self.job = None
        self.cur = None
        try:
            save_notes(self.notes)
        except OSError as e:
            messagebox.showerror("Notes", f"Could not save:\n{e}", parent=self)
        self.refresh_filters()
        self.refill()
        self.set_form(None)

    def show_mode(self):
        if self.preview.get():
            self.collect()
            render_markdown(self.view, self.cur["body"] if self.cur else "")
            self.body.pack_forget()
            self.view.pack(fill="both", expand=True)
        else:
            self.view.pack_forget()
            self.body.pack(fill="both", expand=True)

    def focus_server(self, host_id):
        """Show this server's notes; start a new one attached to it if it has none."""
        self.search.set("")
        self.tag_var.set(ALL)
        name = next((h["name"] for h in self.app.hosts if h["id"] == host_id), ALL)
        self.server_var.set(name)
        self.refill()
        if self.shown:
            self.listbox.selection_set(0)
            self.pick()
        else:
            self.new(server=host_id)

    def close(self):
        self.flush()
