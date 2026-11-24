"""REGTeches RC - program preferences (Tools > Preferences) and remembering which tabs were open.

Stored in `prefs.json` (separate from settings.json, which the colour theme owns) and `session.json` (the tabs that were open
when RC was closed). The loading, cleaning and the session list are pure and tested without a window; the Preferences tab is at
the bottom."""

import json
import os
import tkinter as tk
from tkinter import messagebox, ttk

import rc_manager as core

log = core.log

CHECK_CHOICES = (("Off (only when I click ⟳)", 0), ("Every 30 seconds", 30), ("Every minute", 60), ("Every 5 minutes", 300), ("Every 15 minutes", 900))
STATS_CHOICES = (2, 5, 10, 30, 60)
SCALE_CHOICES = (80, 90, 100, 110, 125, 150, 175, 200)
MAX_SESSION_TABS = 12

DEFAULTS = {"confirm_exit": True, "reopen_tabs": False, "check_interval": 60, "stats_interval": 5, "ui_scale": 100, "file_columns": ["size", "modified"]}

SHORTCUTS = (
    ("Ctrl+K", "Quick connect"), ("Ctrl+N", "Add a server"), ("Ctrl+W", "Disconnect the current tab"),
    ("Ctrl+1 … Ctrl+9", "Go to the 1st … 9th tab (Ctrl+9 = the last one)"), ("Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous tab"),
    ("Ctrl+Shift+D", "Duplicate this terminal session"), ("Middle-click a tab", "Disconnect it"), ("Drag a tab", "Change the tab order"),
    ("Ctrl+mouse wheel (terminal)", "Bigger / smaller terminal text"), ("Ctrl+Shift+C / Ctrl+V", "Copy / paste in a terminal"),
    ("F5 F6 F7 F8 F2 F4 (Files)", "Copy, move, new folder, delete, rename, edit"), ("Ctrl+A / C / X / V (Files)", "Select all, copy, cut, paste"),
)


def prefs_path():
    return os.path.join(core.APP_DIR, "prefs.json")


def session_path():
    return os.path.join(core.APP_DIR, "session.json")


def clean(data):
    data = data if isinstance(data, dict) else {}
    out = dict(DEFAULTS)
    for key in ("confirm_exit", "reopen_tabs"):
        if isinstance(data.get(key), bool):
            out[key] = data[key]
    if data.get("check_interval") in dict((v, v) for _, v in CHECK_CHOICES):
        out["check_interval"] = data["check_interval"]
    if data.get("stats_interval") in STATS_CHOICES:
        out["stats_interval"] = data["stats_interval"]
    if data.get("ui_scale") in SCALE_CHOICES:
        out["ui_scale"] = data["ui_scale"]
    if isinstance(data.get("file_columns"), list):
        out["file_columns"] = [c for c in ("size", "modified", "link") if c in data["file_columns"]]
    return out


def load():
    try:
        with open(prefs_path(), "r", encoding="utf-8") as f:
            return clean(json.load(f))
    except FileNotFoundError:
        return clean({})
    except (OSError, ValueError) as e:
        log.warning("prefs.json unreadable (%s) - using defaults", e)
        return clean({})


def save(prefs):
    tmp = prefs_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean(prefs), f, indent=2)
    os.replace(tmp, prefs_path())


# ─── the tabs that were open ─────────────────────────────────────────────────

def clean_session(items):
    """[{'action', 'host', 'over', 'active'}] -> only well-formed entries (at most MAX_SESSION_TABS)."""
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("action") in ("ssh", "files", "stats", "telnet", "serial") and isinstance(it.get("host"), str) and it["host"]:
            over = it.get("over") if isinstance(it.get("over"), dict) else {}
            out.append({"action": it["action"], "host": it["host"], "over": {k: v for k, v in over.items() if isinstance(k, str)},
                        "active": bool(it.get("active"))})
    return out[:MAX_SESSION_TABS]


def load_session():
    try:
        with open(session_path(), "r", encoding="utf-8") as f:
            return clean_session(json.load(f))
    except (OSError, ValueError):
        return []


def save_session(items):
    items = clean_session(items)
    if not items:
        try:
            os.remove(session_path())
        except OSError:
            pass
        return
    tmp = session_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1)
    os.replace(tmp, session_path())


# ─── the tab ─────────────────────────────────────────────────────────────────

class PrefsTab(ttk.Frame):
    def __init__(self, app):
        super().__init__(app.nb, padding=14)
        self.app = app
        p = app.prefs
        ttk.Label(self, text="Preferences", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, foreground="#555", text="Changes apply and are saved straight away.").pack(anchor="w", pady=(0, 8))
        box = ttk.LabelFrame(self, text=" Starting and closing ", padding=10)
        box.pack(fill="x")
        self.confirm = tk.BooleanVar(value=p["confirm_exit"])
        self.reopen = tk.BooleanVar(value=p["reopen_tabs"])
        ttk.Checkbutton(box, text="Ask before closing RC while sessions are open or files are being transferred", variable=self.confirm,
                        command=lambda: self.set(confirm_exit=self.confirm.get())).pack(anchor="w")
        ttk.Checkbutton(box, text="Reopen my SSH / Files / Stats tabs from last time when RC starts", variable=self.reopen,
                        command=lambda: self.set(reopen_tabs=self.reopen.get())).pack(anchor="w", pady=2)
        ttk.Label(box, foreground="#555", text="(Servers without a saved login ask for it again, as usual.)").pack(anchor="w", padx=22)

        box2 = ttk.LabelFrame(self, text=" Checking servers and Stats ", padding=10)
        box2.pack(fill="x", pady=8)
        row = ttk.Frame(box2)
        row.pack(fill="x")
        ttk.Label(row, text="Check whether each server is up").grid(row=0, column=0, sticky="w")
        self.check = ttk.Combobox(row, state="readonly", width=28, values=[l for l, _ in CHECK_CHOICES])
        self.check.set(next(l for l, v in CHECK_CHOICES if v == p["check_interval"]))
        self.check.grid(row=0, column=1, padx=8, pady=3)
        self.check.bind("<<ComboboxSelected>>", lambda e: self.set(check_interval=dict(CHECK_CHOICES)[self.check.get()]))
        ttk.Label(row, text="New Stats tabs refresh every").grid(row=1, column=0, sticky="w")
        self.stats = ttk.Combobox(row, state="readonly", width=8, values=[f"{s} s" for s in STATS_CHOICES])
        self.stats.set(f"{p['stats_interval']} s")
        self.stats.grid(row=1, column=1, padx=8, pady=3, sticky="w")
        self.stats.bind("<<ComboboxSelected>>", lambda e: self.set(stats_interval=int(self.stats.get().split()[0])))

        box3 = ttk.LabelFrame(self, text=" Display ", padding=10)
        box3.pack(fill="x")
        ttk.Label(box3, text="Interface size").grid(row=0, column=0, sticky="w")
        self.scale = ttk.Combobox(box3, state="readonly", width=8, values=[f"{s}%" for s in SCALE_CHOICES])
        self.scale.set(f"{p['ui_scale']}%")
        self.scale.grid(row=0, column=1, padx=8, pady=3, sticky="w")
        self.scale.bind("<<ComboboxSelected>>", lambda e: self.set(ui_scale=int(self.scale.get().rstrip("%"))))
        ttk.Label(box3, foreground="#555", text="Text in the terminal has its own size: Ctrl+mouse wheel, or View > Terminal text size. "
                                                "Colours: View > Color theme and Terminal colours.").grid(row=1, column=0, columnspan=3, sticky="w")

        box4 = ttk.LabelFrame(self, text=" Keyboard shortcuts ", padding=10)
        box4.pack(fill="both", expand=True, pady=8)
        t = tk.Text(box4, height=len(SHORTCUTS) + 1, borderwidth=0, font=("Segoe UI", 9), wrap="none")
        t.pack(fill="both", expand=True)
        t.tag_configure("key", font=("Segoe UI", 9, "bold"))
        for key, what in SHORTCUTS:
            t.insert("end", f"{key:<30}", "key")
            t.insert("end", what + "\n")
        t.configure(state="disabled")

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, foreground="#555", text="Data folder: " + core.APP_DIR).pack(side="left")
        ttk.Button(bar, text="Open the folder", command=lambda: os.startfile(core.APP_DIR) if hasattr(os, "startfile") else None).pack(side="right")

    def set(self, **kw):
        self.app.prefs.update(kw)
        try:
            save(self.app.prefs)
        except OSError as e:
            messagebox.showerror("Preferences", f"Could not save:\n{e}", parent=self)
        self.app.apply_prefs()
