"""REGTeches RC - the in-app SSH terminal.

A real terminal emulator: the `pyte` library interprets everything the server sends (colours, cursor movement,
the alternate screen), so full-screen programs such as vim, nano, htop, less, mc and tmux work inside the tab.
This file draws pyte's screen into a Tk Text widget, keeps a scrollback above it, and adds:

  * font size (Ctrl + mouse wheel, A- / A+ buttons) and scrollback size, remembered in terminal.json
  * Find in the output, snippets, log-to-file
  * automatic reconnect when the connection drops (a clean `exit` is respected), plus a Reconnect button

Keep this file next to rc_manager.py. Importing it needs `pip install pyte`.
"""

import codecs
import json
import os
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import defaultdict
from tkinter import messagebox, ttk

import pyte
from pyte import screens as _pscreens

import rc_manager as core

log = core.log

PALETTE = ["#1e1e1e", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#d7dae0",
           "#5c6370", "#ff7b86", "#b5e890", "#ffd88a", "#7cc4ff", "#e08cf5", "#6fd6e0", "#ffffff"]
DEFAULT_FG, DEFAULT_BG = PALETTE[7], PALETTE[0]
_NAMES = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"]
COLOR_INDEX = {n: i for i, n in enumerate(_NAMES)}
COLOR_INDEX.update({"bright" + n: i + 8 for i, n in enumerate(_NAMES)})
COLOR_INDEX["yellow"], COLOR_INDEX["brightyellow"] = 3, 11
_ESC = re.compile(r"\x1b(?:\[([0-9;?<=>!:]*)[ -/]*([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z]|[=>78MNOcDEHZ])")

KEYS = {"Return": "\r", "BackSpace": "\x7f", "Tab": "\t", "ISO_Left_Tab": "\x1b[Z", "Escape": "\x1b",
        "Delete": "\x1b[3~", "Prior": "\x1b[5~", "Next": "\x1b[6~", "Insert": "\x1b[2~",
        "F1": "\x1bOP", "F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS", "F5": "\x1b[15~", "F6": "\x1b[17~",
        "F7": "\x1b[18~", "F8": "\x1b[19~", "F9": "\x1b[20~", "F10": "\x1b[21~", "F11": "\x1b[23~",
        "F12": "\x1b[24~"}
CURSOR_KEYS = {"Up": "A", "Down": "B", "Right": "C", "Left": "D", "Home": "H", "End": "F"}  # CSI x, or SS3 x in application mode

FONT_MIN, FONT_MAX, FONT_DEFAULT = 7, 40, 11
SCROLLBACK_CHOICES = (2000, 10000, 50000)
RECONNECT_DELAYS = (2, 4, 8, 15, 30, 30, 60, 60)  # seconds before each automatic retry; then it stops and waits for you


# ─── Preferences (terminal.json) ─────────────────────────────────────────────

def prefs_path():
    return os.path.join(core.APP_DIR, "terminal.json")


def clean_prefs(data):
    """Force font size and scrollback into sane ranges, whatever the file held."""
    data = data if isinstance(data, dict) else {}
    try:
        size = int(data.get("font_size", FONT_DEFAULT))
    except (TypeError, ValueError):
        size = FONT_DEFAULT
    try:
        sb = int(data.get("scrollback", SCROLLBACK_CHOICES[1]))
    except (TypeError, ValueError):
        sb = SCROLLBACK_CHOICES[1]
    return {"font_size": max(FONT_MIN, min(FONT_MAX, size)), "scrollback": max(500, min(200000, sb))}


def load_prefs():
    try:
        with open(prefs_path(), "r", encoding="utf-8") as f:
            return clean_prefs(json.load(f))
    except FileNotFoundError:
        return clean_prefs({})
    except (OSError, ValueError) as e:
        log.warning("terminal.json unreadable (%s) - using defaults", e)
        return clean_prefs({})


def save_prefs(prefs):
    try:
        with open(prefs_path(), "w", encoding="utf-8") as f:
            json.dump(clean_prefs(prefs), f, indent=2)
    except OSError as e:
        log.warning("Could not save terminal.json: %s", e)


# ─── The emulated screen ─────────────────────────────────────────────────────

class RcScreen(pyte.Screen):
    """pyte's Screen plus what pyte leaves out: scrollback capture, the alternate screen (vim, less, htop),
    application cursor keys, bracketed paste, the bell, and answers to terminal queries."""

    MAX_PENDING = 100000

    def __init__(self, cols, rows, on_input=None, on_bell=None):
        self.scrolled = []       # lines that scrolled off the top of the main screen, oldest first
        self.alt = False         # True while a full-screen program is using the alternate screen
        self.app_cursor = False  # DECCKM: arrow keys send ESC O x instead of ESC [ x
        self.bracketed = False   # bracketed paste requested by the program
        self._main = None
        self.on_input, self.on_bell = on_input, on_bell
        super().__init__(cols, rows)

    # scrollback -------------------------------------------------------------
    def index(self):
        top, bottom = self.margins or _pscreens.Margins(0, self.lines - 1)
        if not self.alt and top == 0 and bottom == self.lines - 1 and self.cursor.y == bottom:
            self.scrolled.append(self.buffer[0])
            if len(self.scrolled) > self.MAX_PENDING:
                del self.scrolled[:len(self.scrolled) - self.MAX_PENDING]
        super().index()

    # private modes pyte ignores ---------------------------------------------
    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private"):
            rest = []
            for m in modes:
                if m in (47, 1047, 1049):
                    self._enter_alt()
                elif m == 1:
                    self.app_cursor = True
                elif m == 2004:
                    self.bracketed = True
                else:
                    rest.append(m)
            modes = tuple(rest)
            if not modes:
                return
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get("private"):
            rest = []
            for m in modes:
                if m in (47, 1047, 1049):
                    self._leave_alt()
                elif m == 1:
                    self.app_cursor = False
                elif m == 2004:
                    self.bracketed = False
                else:
                    rest.append(m)
            modes = tuple(rest)
            if not modes:
                return
        super().reset_mode(*modes, **kwargs)

    def _enter_alt(self):
        if self.alt:
            return
        self._main = (self.buffer, self.cursor, self.margins)
        self.buffer = defaultdict(lambda: _pscreens.StaticDefaultDict(self.default_char))
        self.cursor = _pscreens.Cursor(0, 0)
        self.margins = None
        self.alt = True
        self.dirty.update(range(self.lines))

    def _leave_alt(self):
        if not self.alt or self._main is None:
            return
        self.buffer, self.cursor, self.margins = self._main
        self._main = None
        self.alt = False
        self.dirty.update(range(self.lines))

    # talking back -----------------------------------------------------------
    def bell(self, *args):
        if self.on_bell:
            self.on_bell()

    def write_process_input(self, data):
        if self.on_input:
            self.on_input(data)


DEFAULT_STYLE = ("default", "default", False, False, False, False)


def line_runs(line, cols, min_len=0):
    """One screen line -> [(text, style)] runs, style = (fg, bg, bold, underline, strike, reverse).
    Trailing blank cells in the default style are dropped, but at least `min_len` characters are kept so the
    cursor always has a cell to sit on. `line` may be None for an empty line."""
    cells = []
    if line is not None:
        for x in range(cols):
            ch = line[x]
            if ch.data == "":  # right half of a double-width character
                continue
            cells.append((ch.data, (ch.fg, ch.bg, ch.bold, ch.underscore, ch.strikethrough, ch.reverse)))
    while len(cells) > min_len and cells and cells[-1][0] == " " and cells[-1][1] == DEFAULT_STYLE:
        cells.pop()
    while len(cells) < min_len:
        cells.append((" ", DEFAULT_STYLE))
    runs = []
    for data, style in cells:
        if runs and runs[-1][1] == style:
            runs[-1][0].append(data)
        else:
            runs.append(([data], style))
    return [("".join(chars), style) for chars, style in runs]


def style_colors(style):
    """(foreground, background) hex colours for a style tuple, or None where the widget default applies."""
    fg, bg, bold, _u, _s, reverse = style

    def conv(name, is_fg):
        if name == "default":
            return None
        if name in COLOR_INDEX:
            i = COLOR_INDEX[name]
            if is_fg and bold and i < 8:
                i += 8  # bold ANSI colours are drawn bright, like most terminals
            return PALETTE[i]
        if re.fullmatch(r"[0-9a-fA-F]{6}", name):
            return "#" + name.lower()
        return None
    f, b = conv(fg, True), conv(bg, False)
    if bold and f is None:
        f = PALETTE[15]
    if reverse:
        f, b = (b or DEFAULT_BG), (f or DEFAULT_FG)
    return f, b


# ─── The terminal tab ────────────────────────────────────────────────────────

class TerminalTab(ttk.Frame):
    AUTO_CONNECT = True  # tests switch this off

    def __init__(self, app, host, user, password):
        super().__init__(app.nb)
        self.app, self.host, self.creds = app, host, (user, password)
        self.client = self.chan = None
        self.closed = False
        self.ever_connected = False
        self.attempt = 0
        self.reconnect_job = self.render_job = self.resize_job = None
        self.generation = 0  # bumps on every (re)connect so a stale reader thread can't touch the new session
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.prefs = load_prefs()
        self.font = tkfont.Font(family="Consolas", size=self.prefs["font_size"])
        self.sb_limit = self.prefs["scrollback"]
        self.sb = 0  # scrollback lines held at the top of the Text widget
        self.styles = {}
        self.log_file = None

        bar = ttk.Frame(self)
        bar.pack(side="top", fill="x")
        self.snip_btn = ttk.Menubutton(bar, text="▤ Snippets ▾")
        self.snip_btn.pack(side="left", padx=4, pady=2)
        self.snip_btn.configure(menu=tk.Menu(self.snip_btn, tearoff=0, postcommand=self.fill_snippet_menu))
        ttk.Button(bar, text="🔍 Find", command=self.find_dialog).pack(side="left", padx=2)
        self.find_var = tk.StringVar()
        self.find_entry = ttk.Entry(bar, textvariable=self.find_var, width=20)
        self.find_entry.pack(side="left", padx=2)
        self.find_entry.bind("<Return>", lambda e: self.find_next())
        ttk.Button(bar, text="▲", width=3, command=lambda: self.find_next(True)).pack(side="left")
        ttk.Button(bar, text="▼", width=3, command=self.find_next).pack(side="left")
        self.log_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Log to file", variable=self.log_var, command=self.toggle_log).pack(side="left", padx=8)
        ttk.Button(bar, text="A−", width=3, command=lambda: self.change_font(-1)).pack(side="left")
        ttk.Button(bar, text="A+", width=3, command=lambda: self.change_font(+1)).pack(side="left", padx=(2, 0))
        self.status_var = tk.StringVar(value="Connecting ...")
        ttk.Label(bar, textvariable=self.status_var).pack(side="right", padx=8)
        self.reconnect_btn = ttk.Button(bar, text="⟳ Reconnect", command=self.reconnect_now, state="disabled")
        self.reconnect_btn.pack(side="right", padx=2)

        self.t = tk.Text(self, bg=DEFAULT_BG, fg=DEFAULT_FG, insertbackground="#ffffff", insertwidth=2,
                         font=self.font, wrap="none", undo=False, borderwidth=0, padx=4, pady=2,
                         selectbackground="#3e4451", exportselection=False, cursor="xterm")
        self.t._rc_fixed = True  # the terminal keeps its own colours in every theme
        sb = ttk.Scrollbar(self, command=self.t.yview)
        self.t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.t.pack(side="left", fill="both", expand=True)
        self.t.tag_configure("tcursor", background="#c8ccd4", foreground="#1e1e1e")
        self.t.tag_configure("found", background="#e5c07b", foreground="#000000")
        self.t.bind("<Key>", self.on_key)
        self.t.bind("<Button-1>", lambda e: self.after_idle(self.t.focus_set))
        self.t.bind("<Button-3>", self.popup)
        self.t.bind("<Control-MouseWheel>", self.on_zoom)
        self.t.bind("<Configure>", lambda e: self.schedule_resize())

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Copy", command=self.copy)
        self.menu.add_command(label="Paste", command=self.paste)
        self.menu.add_command(label="Clear screen and scrollback", command=self.clear)
        self.menu.add_separator()
        self.menu.add_command(label="Bigger text", command=lambda: self.change_font(+1))
        self.menu.add_command(label="Smaller text", command=lambda: self.change_font(-1))
        self.menu.add_command(label="Reset text size", command=lambda: self.change_font(0))
        sbm = tk.Menu(self.menu, tearoff=0)
        self.sb_var = tk.IntVar(value=self.sb_limit)
        for n in SCROLLBACK_CHOICES:
            sbm.add_radiobutton(label=f"{n:,} lines", variable=self.sb_var, value=n, command=lambda n=n: self.set_scrollback(n))
        self.menu.add_cascade(label="Scrollback", menu=sbm)

        self.update_idletasks()
        self.cols, self.rows = self.size()
        self.screen = RcScreen(self.cols, self.rows, on_input=self.send, on_bell=self.t.bell)
        self.stream = pyte.Stream(self.screen, strict=False)
        self.t.insert("1.0", "\n" * (self.rows - 1))
        self.write_local(f"Connecting to {host['address']} ...\r\n")
        if self.AUTO_CONNECT:
            self.start_connect()

    # ── connecting, dropping, reconnecting ──
    def set_state(self, text, can_reconnect):
        self.status_var.set(text)
        self.reconnect_btn.state(["!disabled"] if can_reconnect else ["disabled"])

    def start_connect(self):
        self.generation += 1
        gen = self.generation
        user, password = self.creds
        self.set_state("Connecting ...", False)
        threading.Thread(target=self.connect, args=(gen, user, password, self.cols, self.rows), daemon=True,
                         name="ssh-tab").start()

    def connect(self, gen, user, password, cols, rows):
        import paramiko
        try:
            client = core.open_ssh_client(self.app, self.host, user, password)
            tr = client.get_transport()
            if tr:
                tr.set_keepalive(30)  # notices a dead link instead of freezing silently
            chan = client.invoke_shell(term="xterm-256color", width=self.cols, height=self.rows)  # latest size: the tab has been laid out by now
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            fatal = isinstance(e, (paramiko.AuthenticationException, paramiko.BadHostKeyException)) or "not trusted" in msg
            log.warning("SSH tab: connection to %s failed: %s", self.host["address"], msg)
            self.app.post(lambda: self.connect_failed(gen, msg, fatal))
            return
        if self.closed or gen != self.generation:
            for c in (chan, client):
                try:
                    c.close()
                except Exception:
                    pass
            return
        self.client, self.chan = client, chan
        log.info("SSH tab: connected to %s", self.host["address"])
        self.app.post(lambda: self.connected(gen))
        while not self.closed and gen == self.generation:
            try:
                data = chan.recv(8192)
            except Exception:
                break
            if not data:
                break
            text = self.decoder.decode(data)
            self.app.post(lambda t=text, g=gen: g == self.generation and self.feed(t))
        clean = False
        try:
            tr = client.get_transport()
            clean = bool(chan.exit_status_ready() and tr is not None and tr.is_active())
        except Exception:
            pass
        log.info("SSH tab: connection to %s ended (%s)", self.host["address"], "shell exited" if clean else "dropped")
        if not self.closed and gen == self.generation:
            self.app.post(lambda: self.session_ended(gen, clean))

    def connected(self, gen):
        if self.closed or gen != self.generation:
            return
        if self.ever_connected:
            self.write_local("\r\n\x1b[32m[reconnected]\x1b[0m\r\n")
        else:
            self.write_local("Connected.\r\n")
        self.ever_connected, self.attempt = True, 0
        self.set_state("● Connected", False)
        self.apply_size()
        self.resize_pty()
        self.t.focus_set()

    def connect_failed(self, gen, msg, fatal):
        if self.closed or gen != self.generation:
            return
        self.write_local(f"\r\n\x1b[31mConnection failed: {msg}\x1b[0m\r\n")
        if self.ever_connected and not fatal:
            self.schedule_reconnect()
        else:
            self.set_state("○ Not connected", True)

    def session_ended(self, gen, clean):
        if self.closed or gen != self.generation:
            return
        if clean:
            self.write_local("\r\n\x1b[33m[session ended]\x1b[0m\r\n")
            self.set_state("○ Session ended", True)
        else:
            self.write_local("\r\n\x1b[31m[connection lost]\x1b[0m\r\n")
            self.schedule_reconnect()

    def schedule_reconnect(self):
        if self.attempt >= len(RECONNECT_DELAYS):
            self.write_local("\x1b[33mGave up reconnecting. Click ⟳ Reconnect to try again.\x1b[0m\r\n")
            self.set_state("○ Disconnected", True)
            return
        delay = RECONNECT_DELAYS[self.attempt]
        self.attempt += 1
        self.write_local(f"\x1b[33m[reconnecting in {delay} s - attempt {self.attempt} of {len(RECONNECT_DELAYS)}]\x1b[0m\r\n")
        self.set_state(f"⟳ Reconnecting ({self.attempt}/{len(RECONNECT_DELAYS)}) ...", True)
        self.reconnect_job = self.after(delay * 1000, self.do_reconnect)

    def do_reconnect(self):
        self.reconnect_job = None
        if not self.closed:
            self.close_connection()
            self.start_connect()

    def reconnect_now(self):
        if self.reconnect_job:
            self.after_cancel(self.reconnect_job)
            self.reconnect_job = None
        self.attempt = 0
        self.write_local("\r\n\x1b[36m[reconnecting ...]\x1b[0m\r\n")
        self.do_reconnect()

    def close_connection(self):
        chan, client = self.chan, self.client
        self.chan = self.client = None
        for c in (chan, client):
            try:
                c and c.close()
            except Exception as e:
                log.debug("SSH tab: error while closing %s: %s", type(c).__name__, e)

    def close(self):
        self.closed = True
        for job in (self.reconnect_job, self.render_job, self.resize_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        self.close_connection()

    # ── size ──
    def size(self):
        w, h = max(self.t.winfo_width(), 400), max(self.t.winfo_height(), 200)
        return (max(20, (w - 8) // max(self.font.measure("0"), 1)),
                max(5, (h - 4) // max(self.font.metrics("linespace"), 1)))

    def schedule_resize(self):
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(80, self.apply_size)

    def apply_size(self):
        self.resize_job = None
        if self.closed:
            return
        cols, rows = self.size()
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        self.screen.resize(rows, cols)
        self.rebuild_screen_region()
        self.resize_pty()
        self.schedule_render()

    def rebuild_screen_region(self):
        self.t.delete(f"{self.sb + 1}.0", "end")
        self.t.insert(f"{self.sb + 1}.0", "\n" * (self.rows - 1))
        self.screen.dirty.update(range(self.rows))

    def resize_pty(self):
        if self.chan and not self.closed:
            try:
                self.chan.resize_pty(width=self.cols, height=self.rows)
            except Exception as e:
                log.debug("SSH tab: resize_pty failed: %s", e)

    # ── font and scrollback ──
    def change_font(self, delta):
        size = FONT_DEFAULT if delta == 0 else max(FONT_MIN, min(FONT_MAX, self.font.cget("size") + delta))
        self.font.configure(size=size)
        self.prefs["font_size"] = size
        save_prefs(self.prefs)
        self.after_idle(self.apply_size)

    def on_zoom(self, e):
        self.change_font(+1 if e.delta > 0 else -1)
        return "break"

    def set_scrollback(self, n):
        self.sb_limit = self.prefs["scrollback"] = n
        save_prefs(self.prefs)
        self.trim_scrollback()

    def trim_scrollback(self):
        if self.sb > self.sb_limit:
            extra = self.sb - self.sb_limit
            self.t.delete("1.0", f"{extra + 1}.0")
            self.sb = self.sb_limit

    # ── snippets, search, logging ──
    def fill_snippet_menu(self):
        m = self.snip_btn.cget("menu")
        menu = self.snip_btn.nametowidget(m) if isinstance(m, str) else m
        menu.delete(0, "end")
        snips = core.load_snippets()
        for s in snips:
            menu.add_command(label=s["name"], command=lambda s=s: self.run_snippet(s))
        if not snips:
            menu.add_command(label="(no snippets - Tools > Snippets)", state="disabled")

    def run_snippet(self, s):
        if s.get("confirm", True) and not messagebox.askyesno("Snippet", f"Type this into the terminal?\n\n{s['command']}",
                                                              parent=self):
            return
        self.send(s["command"].replace("\r\n", "\n").replace("\n", "\r") + "\r")
        self.t.focus_set()

    def find_dialog(self):
        self.find_entry.focus_set()
        self.find_entry.selection_range(0, "end")

    def find_next(self, backwards=False):
        needle = self.find_var.get()
        self.t.tag_remove("found", "1.0", "end")
        if not needle:
            return
        pos = self.t.search(needle, self.t.index("insert"), backwards=backwards, nocase=True,
                            stopindex="1.0" if backwards else "end")
        if not pos:
            pos = self.t.search(needle, "end" if backwards else "1.0", backwards=backwards, nocase=True)
        if pos:
            end = f"{pos}+{len(needle)}c"
            self.t.tag_add("found", pos, end)
            self.t.tag_raise("found")
            self.t.see(pos)
            self.t.mark_set("insert", end if not backwards else pos)

    def toggle_log(self):
        if self.log_var.get():
            folder = os.path.join(core.APP_DIR, "logs")
            os.makedirs(folder, exist_ok=True)
            name = f"{re.sub(r'[^A-Za-z0-9._-]+', '_', self.host['name'])}_{time.strftime('%Y%m%d_%H%M%S')}.log"
            self.log_file = open(os.path.join(folder, name), "a", encoding="utf-8")
            self.app.status.set(f"Logging this session to logs\\{name}")
        elif self.log_file:
            self.log_file.close()
            self.log_file = None

    # ── keyboard, clipboard ──
    def send(self, s):
        chan = self.chan
        if chan and not self.closed:
            try:
                chan.send(s)
            except Exception as e:
                log.debug("SSH tab: send failed: %s", e)

    def on_key(self, e):
        ctrl, shift = e.state & 0x4, e.state & 0x1
        alt = e.state & 0x20000
        key = e.keysym
        if ctrl and key.lower() in ("c", "v") and (shift or key.lower() == "v"):
            self.copy() if key.lower() == "c" else self.paste()
            return "break"
        if ctrl and key.lower() == "c" and self.t.tag_ranges("sel"):
            self.copy()
            return "break"
        if shift and key == "Insert":
            self.paste()
            return "break"
        if key in CURSOR_KEYS:
            self.send(("\x1bO" if self.screen.app_cursor else "\x1b[") + CURSOR_KEYS[key])
        elif key in KEYS:
            self.send(KEYS[key])
        elif e.char:
            self.send(("\x1b" + e.char) if (alt and not ctrl) else e.char)
        return "break"

    def popup(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def copy(self):
        if self.t.tag_ranges("sel"):
            self.clipboard_clear()
            self.clipboard_append(self.t.get("sel.first", "sel.last"))

    def paste(self):
        try:
            text = self.clipboard_get().replace("\r\n", "\r").replace("\n", "\r")
        except tk.TclError:
            return
        if self.screen.bracketed:
            text = "\x1b[200~" + text + "\x1b[201~"
        self.send(text)

    def clear(self):
        self.t.delete("1.0", "end")
        self.sb = 0
        self.screen.scrolled.clear()
        self.screen.reset()
        self.t.insert("1.0", "\n" * (self.rows - 1))
        self.screen.dirty.update(range(self.rows))
        self.schedule_render()
        self.send("\x0c")  # Ctrl+L: the shell redraws its prompt

    # ── output ──
    def write_local(self, text):
        self.feed(text)

    def feed(self, data):
        if self.closed:
            return  # tab was closed while output was still queued
        if self.log_file:
            try:
                self.log_file.write(_ESC.sub("", data).replace("\r\n", "\n").replace("\r", ""))
                self.log_file.flush()
            except (OSError, ValueError):
                self.log_file = None
        try:
            self.stream.feed(data)
        except Exception:
            log.exception("Terminal emulator error (output skipped)")
        self.schedule_render()

    def schedule_render(self):
        if self.render_job is None and not self.closed:
            self.render_job = self.after(15, self.render)

    def tag_for(self, style):
        tag = self.styles.get(style)
        if tag is None:
            fg, bg = style_colors(style)
            tag = f"st{len(self.styles)}"
            self.styles[style] = tag
            self.t.tag_configure(tag, foreground=fg or DEFAULT_FG, background=bg or DEFAULT_BG,
                                 underline=style[3], overstrike=style[4])
            self.t.tag_raise("sel")
        return tag

    def draw_line(self, lineno, line, min_len=0):
        t = self.t
        t.delete(f"{lineno}.0", f"{lineno}.end")
        for text, style in line_runs(line, self.cols, min_len):
            t.insert(f"{lineno}.end", text, () if style == DEFAULT_STYLE else self.tag_for(style))

    def render(self):
        self.render_job = None
        if self.closed:
            return
        scr, t = self.screen, self.t
        at_bottom = t.yview()[1] >= 0.999
        if scr.scrolled:
            lines, scr.scrolled = scr.scrolled, []
            for ln in lines[-self.sb_limit:]:
                t.insert(f"{self.sb + 1}.0", "\n")
                self.sb += 1
                self.draw_line(self.sb, ln)
            self.trim_scrollback()
        cur = scr.cursor
        dirty = set(scr.dirty)
        scr.dirty.clear()
        dirty.add(min(cur.y, self.rows - 1))
        for y in sorted(dirty):
            if 0 <= y < self.rows:
                self.draw_line(self.sb + 1 + y, scr.buffer.get(y), (cur.x + 1) if y == cur.y else 0)
        t.tag_remove("tcursor", "1.0", "end")
        if not cur.hidden and 0 <= cur.y < self.rows:
            pos = f"{self.sb + 1 + cur.y}.{cur.x}"
            t.tag_add("tcursor", pos, f"{pos}+1c")
            t.tag_raise("tcursor")
            t.mark_set("insert", pos)
        if at_bottom:
            t.yview_moveto(1.0)
