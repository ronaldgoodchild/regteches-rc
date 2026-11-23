"""Light / dark mode for RC: ttk styles plus the plain Tk widgets that ttk theming doesn't reach."""
import json
import sys
import tkinter as tk
from tkinter import ttk

DARK = dict(bg="#25272b", panel="#2d3035", field="#1b1d20", fg="#e4e6ea", dim="#9aa0a8", sel="#3d5a99",
            selfg="#ffffff", border="#454952", active="#3a3e45", menu="#2d3035", menufg="#e4e6ea")
LIGHT = dict(bg="SystemButtonFace", field="SystemWindow", fg="SystemWindowText", sel="SystemHighlight",
             selfg="SystemHighlightText", menu="SystemMenu", menufg="SystemMenuText")
DARK.update(btn="#3d5a99", btnact="#4d6fb8", btnfg="#ffffff", tab="#2d3035", head="#e4e6ea")
_W = "#ffffff"
# name -> (is_dark, colours). "Classic Light" is the plain Windows look; the rest are coloured themes (ttk "clam").
PALETTES = {
    "Sky Blue": (False, dict(bg="#e9f2fd", panel="#cfe2fa", field=_W, fg="#12263f", dim="#52667f", sel="#1e88e5", selfg=_W,
                             border="#9bbbe3", active="#b7d3f5", menu="#f3f8ff", menufg="#12263f", btn="#2f80ed",
                             btnact="#1565c0", btnfg=_W, tab="#bcd6f6", head="#1e5aa8")),
    "Sunset": (False, dict(bg="#fff3e3", panel="#ffd8a8", field="#fffdf9", fg="#3b2312", dim="#8a6240", sel="#f4511e",
                           selfg=_W, border="#e8b27a", active="#ffc78a", menu="#fff8ef", menufg="#3b2312", btn="#f4511e",
                           btnact="#c63d0f", btnfg=_W, tab="#ffd1a1", head="#b8400f")),
    "Mint Fresh": (False, dict(bg="#e6f7ee", panel="#bfeacf", field=_W, fg="#0f2e1f", dim="#4d7360", sel="#16a361",
                               selfg=_W, border="#8ccaa6", active="#a5e0bc", menu="#f2fcf6", menufg="#0f2e1f",
                               btn="#16a361", btnact="#0e7d49", btnfg=_W, tab="#b2e6c8", head="#0e7d49")),
    "Royal Dark": (True, dict(bg="#101528", panel="#1c2447", field="#0b0f1e", fg="#e8ecff", dim="#8fa0d6", sel="#3b82f6",
                              selfg=_W, border="#34408a", active="#2a3670", menu="#1c2447", menufg="#e8ecff",
                              btn="#2563eb", btnact="#3b82f6", btnfg=_W, tab="#1c2447", head="#7cb0ff")),
    "Neon Purple": (True, dict(bg="#160d2b", panel="#2a1a52", field="#0f0820", fg="#f1e9ff", dim="#b79de6", sel="#c026d3",
                               selfg=_W, border="#5b2f9e", active="#42277d", menu="#2a1a52", menufg="#f1e9ff",
                               btn="#a21caf", btnact="#d946ef", btnfg=_W, tab="#2a1a52", head="#f0abfc")),
    "Classic Dark": (True, DARK),
    "Classic Light": (False, LIGHT),
}
DEFAULT_THEME = "Sky Blue"
# the Connect buttons: one bright colour per kind of connection, bold white text (all 4.5+ contrast with white)
CONN = {"ssh": "#15803d", "files": "#c2410c", "stats": "#854d0e", "telnet": "#0f766e", "serial": "#7e22ce", "rdp": "#1d4ed8", "web": "#0e7490", "ps": "#4338ca", "ssh_ext": "#475569",
        "vnc": "#7c3aed", "rustdesk": "#0369a1", "mail": "#a21caf", "smb": "#0f766e", "ping": "#b45309", "verify": "#be123c"}


def shade(hexcol, f):
    """f < 1 darkens, f > 1 lightens (towards white)."""
    h = hexcol.lstrip("#")
    v = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    v = [int(x * f) if f <= 1 else int(x + (255 - x) * (f - 1)) for x in v]
    return "#%02x%02x%02x" % tuple(max(0, min(255, x)) for x in v)
# tab colours per theme: (idle tab, selected tab). Tab text is always bold white, so every colour here is dark enough for it.
TABS = {"Sky Blue": ("#4a78b5", "#0d47a1"), "Sunset": ("#b0662e", "#d1380f"), "Mint Fresh": ("#2f7d58", "#0b6b3a"),
        "Royal Dark": ("#2f3f8f", "#2563eb"), "Neon Purple": ("#5b2f9e", "#c026d3"), "Classic Dark": ("#3a3e45", "#3d5a99")}
for _n, (_idle, _sel) in TABS.items():
    PALETTES[_n][1].update(tab=_idle, tabsel=_sel, tabfg="#ffffff")
# darker accents so white text stays readable (contrast 4.5+ checked for every theme)
FIXES = {"Sky Blue": dict(btn="#1a63c9", btnact="#0d47a1", sel="#1565c0"),
         "Sunset": dict(head="#9a3208", btn="#d1380f", btnact="#a82c08", sel="#c43c0e", tab="#a35a25"),
         "Mint Fresh": dict(head="#0a6a3b", btn="#0f7d49", btnact="#0a6a3b", sel="#0f7d49"),
         "Royal Dark": dict(btn="#1d4ed8", btnact="#2563eb", sel="#2563eb"),
         "Neon Purple": dict(btnact="#c026d3")}
for _n, _fix in FIXES.items():
    PALETTES[_n][1].update(_fix)
# colours a few labels / tree tags set by hand, and what reads well on a dark background instead
FG_MAP = {"#555": "#a3a9b1", "#777": "#8b919a", "#b3261e": "#ff7b72", "#1a7f37": "#56d364", "#0b5cad": "#6cb6ff", "#d32f2f": "#ff6b6b", "#b45309": "#f0b45a"}
TREE_TAGS = ("running", "error", "done", "extra", "warn", "crit")


class Theme:
    def __init__(self, root, path):
        self.root, self.path = root, path
        self.style = ttk.Style(root)
        self.light_name = self.style.theme_use()
        data = self.load()
        name = data.get("theme")
        if name not in PALETTES:  # older settings only knew dark/light
            name = "Royal Dark" if data.get("dark") else DEFAULT_THEME
        self.name = name
        root.bind_class("Toplevel", "<Map>", lambda e: self.paint(e.widget), add="+")

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @property
    def dark(self):
        return PALETTES[self.name][0]

    def set_theme(self, name):
        if name in PALETTES:
            self.name = name
            self.apply()
            self.save()

    def next_theme(self):
        names = list(PALETTES)
        self.set_theme(names[(names.index(self.name) + 1) % len(names)])

    def save(self):
        data = self.load()
        data["theme"] = self.name
        data["dark"] = self.dark
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def colors(self):
        return PALETTES[self.name][1]

    def set_dark(self, dark, save=True):
        self.set_theme("Royal Dark" if dark else DEFAULT_THEME)

    def apply(self):
        if self.name == "Classic Light":
            self.style.theme_use(self.light_name)
            self.style.configure("Treeview", rowheight=24)
        else:
            self.style.theme_use("clam")
            self.configure_styles()
        self.options()
        self.title_bar()
        self.paint(self.root)

    def configure_styles(self):
        s, c = self.style, self.colors()
        s.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"], troughcolor=c["field"],
                    bordercolor=c["border"], lightcolor=c["panel"], darkcolor=c["border"], insertcolor=c["fg"],
                    selectbackground=c["sel"], selectforeground=c["selfg"], focuscolor=c["bg"])
        s.map(".", foreground=[("disabled", c["dim"])])
        s.configure("TButton", background=c["panel"], lightcolor=c["panel"], darkcolor=c["panel"], padding=(4, 3),
                    font=("Segoe UI", 9, "bold"))
        s.map("TButton", background=[("pressed", c["sel"]), ("active", c["active"])])
        # the big Connect buttons: solid colour with white text
        s.configure("Accent.TButton", background=c["btn"], foreground=c["btnfg"], lightcolor=c["btn"], darkcolor=c["btn"],
                    bordercolor=c["btn"], padding=(10, 5), font=("Segoe UI", 9, "bold"))
        s.map("Accent.TButton", background=[("pressed", c["sel"]), ("active", c["btnact"])],
              foreground=[("disabled", c["btnfg"])])
        for key, col in CONN.items():  # stays bold white and colourful even when greyed (no server picked yet)
            st = key + ".Conn.TButton"
            s.configure(st, background=col, foreground="#ffffff", lightcolor=col, darkcolor=col, bordercolor=shade(col, .7),
                        padding=(6, 5), font=("Segoe UI", 9, "bold"))
            s.map(st, background=[("disabled", shade(col, .72)), ("pressed", shade(col, .66)), ("active", shade(col, .86))],
                  foreground=[("disabled", "#f2f2f2"), ("!disabled", "#ffffff")],
                  lightcolor=[("disabled", shade(col, .72)), ("active", shade(col, .86))],
                  darkcolor=[("disabled", shade(col, .72)), ("active", shade(col, .86))])
        s.configure("TMenubutton", background=c["panel"], arrowcolor=c["fg"])
        for name in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(name, fieldbackground=c["field"], foreground=c["fg"], insertcolor=c["fg"],
                        arrowcolor=c["fg"], background=c["panel"])
            s.map(name, fieldbackground=[("disabled", c["panel"]), ("readonly", c["field"])],
                  selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["fg"])])
        s.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
        s.configure("TNotebook.Tab", background=c["tab"], foreground=c["tabfg"], padding=(14, 5),
                    font=("Segoe UI", 10, "bold"))
        s.map("TNotebook.Tab", background=[("selected", c["tabsel"]), ("active", c["btnact"])],
              foreground=[("selected", c["tabfg"]), ("active", c["tabfg"])], padding=[("selected", (16, 8))])
        s.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["fg"], rowheight=26)
        s.map("Treeview", background=[("selected", c["sel"])], foreground=[("selected", c["selfg"])])
        s.configure("Treeview.Heading", background=c["panel"], foreground=c["head"], relief="flat",
                    font=("Segoe UI", 9, "bold"))
        s.map("Treeview.Heading", background=[("active", c["active"])])
        s.configure("TScrollbar", background=c["panel"], troughcolor=c["field"], arrowcolor=c["fg"],
                    bordercolor=c["border"])
        s.map("TScrollbar", background=[("active", c["active"])])
        for name in ("TCheckbutton", "TRadiobutton"):
            s.configure(name, indicatorbackground=c["field"], indicatorforeground=c["fg"],
                        upperbordercolor=c["border"], lowerbordercolor=c["border"])
            s.map(name, background=[("active", c["bg"])])
        s.configure("TLabelframe", bordercolor=c["border"])
        s.configure("TLabelframe.Label", foreground=c["head"], font=("Segoe UI", 9, "bold"))
        s.configure("TPanedwindow", background=c["border"])
        s.configure("Sash", background=c["border"], bordercolor=c["border"])
        s.configure("TSeparator", background=c["border"])
        s.configure("TProgressbar", background=c["sel"], troughcolor=c["field"])

    def options(self):
        """Defaults for plain Tk widgets created from now on (existing ones are repainted by paint)."""
        c = self.colors()
        add = self.root.option_add
        for pattern, value in (
                ("*Text.background", c["field"]), ("*Text.foreground", c["fg"]), ("*Text.insertBackground", c["fg"]),
                ("*Text.selectBackground", c["sel"]), ("*Text.selectForeground", c["selfg"]),
                ("*Listbox.background", c["field"]), ("*Listbox.foreground", c["fg"]),
                ("*Listbox.selectBackground", c["sel"]), ("*Listbox.selectForeground", c["selfg"]),
                ("*Menu.background", c["menu"]), ("*Menu.foreground", c["menufg"]),
                ("*Menu.activeBackground", c["sel"]), ("*Menu.activeForeground", c["selfg"]),
                ("*Toplevel.background", c["bg"]),
                ("*TCombobox*Listbox.background", c["field"]), ("*TCombobox*Listbox.foreground", c["fg"]),
                ("*TCombobox*Listbox.selectBackground", c["sel"]), ("*TCombobox*Listbox.selectForeground", c["selfg"])):
            add(pattern, value, 60)

    def title_bar(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(1 if self.dark else 0)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (older builds used 19)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                    break
        except Exception:
            pass

    def paint(self, w):
        """Recolour w and everything under it. Safe to call repeatedly, also on widgets made later."""
        c, dark = self.colors(), self.dark
        try:
            cls = w.winfo_class()
            if cls in ("Tk", "Toplevel"):
                w.configure(bg=c["bg"])
            elif cls == "Text" and not getattr(w, "_rc_fixed", False):
                w.configure(bg=c["field"], fg=c["fg"], insertbackground=c["fg"], selectbackground=c["sel"],
                            selectforeground=c["selfg"], highlightbackground=c["bg"])
            elif cls == "Listbox":
                w.configure(bg=c["field"], fg=c["fg"], selectbackground=c["sel"], selectforeground=c["selfg"],
                            highlightbackground=c["bg"])
            elif cls == "Menu":
                w.configure(bg=c["menu"], fg=c["menufg"], activebackground=c["sel"], activeforeground=c["selfg"])
            elif cls == "TLabel":
                orig = getattr(w, "_rc_fg", None)
                if orig is None:
                    cur = str(w.cget("foreground"))
                    if cur in FG_MAP:
                        w._rc_fg = orig = cur
                if orig:
                    w.configure(foreground=FG_MAP[orig] if dark else orig)
            elif cls == "Treeview":
                tags = getattr(w, "_rc_tags", None)
                if tags is None:
                    tags = w._rc_tags = {}
                    for t in TREE_TAGS:
                        cur = str(w.tag_configure(t, "foreground"))
                        if cur in FG_MAP:
                            tags[t] = cur
                for t, orig in tags.items():
                    w.tag_configure(t, foreground=FG_MAP[orig] if dark else orig)
            children = w.winfo_children()
        except tk.TclError:
            return
        for ch in children:
            self.paint(ch)
