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
import rc_quick
import rc_schemes
import rc_suggest
import rc_totp
import rc_zmodem

log = core.log

PALETTE = ["#1e1e1e", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#d7dae0",
           "#5c6370", "#ff7b86", "#b5e890", "#ffd88a", "#7cc4ff", "#e08cf5", "#6fd6e0", "#ffffff"]
DEFAULT_FG, DEFAULT_BG = PALETTE[7], PALETTE[0]

def _scheme(bg, fg, cursor, sel, bold, ansi):
    return {"bg": bg, "fg": fg, "cursor": cursor, "sel": sel, "bold": bold, "ansi": ansi.split()}


# Terminal colour schemes: background, text, cursor, selection, bold-default text and the 16 ANSI colours
# (black red green yellow blue magenta cyan white, then the bright versions).
SCHEMES = {
    "Dark": _scheme("#1e1e1e", "#d7dae0", "#c8ccd4", "#3e4451", "#ffffff", " ".join(PALETTE)),
    "Light": _scheme("#fafafa", "#383a42", "#526fff", "#cfd8f6", "#000000",
                     "#383a42 #e45649 #50a14f #c18401 #4078f2 #a626a4 #0184bc #a0a1a7 "
                     "#4f525e #e06c75 #60b35f #d19a1a #5a8df5 #c678dd #1aa3d6 #6b6e78"),
    "Solarized Dark": _scheme("#002b36", "#839496", "#93a1a1", "#073642", "#93a1a1",
                              "#073642 #dc322f #859900 #b58900 #268bd2 #d33682 #2aa198 #eee8d5 "
                              "#586e75 #cb4b16 #93a100 #cfa000 #4ea5e8 #6c71c4 #3cc4b9 #fdf6e3"),
    "Dracula": _scheme("#282a36", "#f8f8f2", "#f8f8f2", "#44475a", "#ffffff",
                       "#21222c #ff5555 #50fa7b #f1fa8c #bd93f9 #ff79c6 #8be9fd #f8f8f2 "
                       "#6272a4 #ff6e6e #69ff94 #ffffa5 #d6acff #ff92df #a4ffff #ffffff"),
    "Monokai": _scheme("#272822", "#f8f8f2", "#f8f8f0", "#49483e", "#ffffff",
                       "#272822 #f92672 #a6e22e #f4bf75 #66d9ef #ae81ff #a1efe4 #f8f8f2 "
                       "#75715e #ff4d8d #bdf24a #ffd08c #8ae6ff #c4a3ff #c0fff2 #f9f8f5"),
    "Nord": _scheme("#2e3440", "#d8dee9", "#d8dee9", "#434c5e", "#eceff4",
                    "#3b4252 #bf616a #a3be8c #ebcb8b #81a1c1 #b48ead #88c0d0 #e5e9f0 "
                    "#4c566a #d57780 #b5d19c #f5d99a #94b3d3 #c79fc0 #8fbcbb #eceff4"),
    "Gruvbox Dark": _scheme("#282828", "#ebdbb2", "#ebdbb2", "#504945", "#fbf1c7",
                            "#282828 #cc241d #98971a #d79921 #458588 #b16286 #689d6a #a89984 "
                            "#928374 #fb4934 #b8bb26 #fabd2f #83a598 #d3869b #8ec07c #ebdbb2"),
    "Green on black": _scheme("#000000", "#33ff33", "#33ff33", "#14501a", "#ccffcc",
                              "#000000 #cc3333 #33cc33 #cccc33 #3333cc #cc33cc #33cccc #33ff33 "
                              "#555555 #ff5555 #55ff55 #ffff55 #6666ff #ff55ff #55ffff #ccffcc"),
}
BUILTIN_SCHEMES = tuple(SCHEMES)
CUSTOM_SCHEMES = rc_schemes.register(SCHEMES, BUILTIN_SCHEMES)        # the ones imported with View > Terminal colours
DEFAULT_SCHEME = "Dark"
FONT_DEFAULT_FAMILY = "Consolas"
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
    """Force every setting into a sane value, whatever the file held."""
    data = data if isinstance(data, dict) else {}
    try:
        size = int(data.get("font_size", FONT_DEFAULT))
    except (TypeError, ValueError):
        size = FONT_DEFAULT
    try:
        sb = int(data.get("scrollback", SCROLLBACK_CHOICES[1]))
    except (TypeError, ValueError):
        sb = SCROLLBACK_CHOICES[1]
    scheme = data.get("scheme") if data.get("scheme") in SCHEMES else DEFAULT_SCHEME
    font = data.get("font") if isinstance(data.get("font"), str) and data.get("font").strip() else FONT_DEFAULT_FAMILY
    flag = lambda key, default: data.get(key) if isinstance(data.get(key), bool) else default
    return {"font_size": max(FONT_MIN, min(FONT_MAX, size)), "scrollback": max(500, min(200000, sb)),
            "scheme": scheme, "font": font.strip(), "copy_on_select": flag("copy_on_select", False),
            "right_click_paste": flag("right_click_paste", False), "middle_click_paste": flag("middle_click_paste", True),
            "zmodem": flag("zmodem", True), "log_timestamps": flag("log_timestamps", False), "line_numbers": flag("line_numbers", False), "zmodem_dir": data.get("zmodem_dir") if isinstance(data.get("zmodem_dir"), str) else ""}


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


def style_colors(style, scheme=None):
    """(foreground, background) hex colours for a style tuple, or None where the widget default applies."""
    scheme = scheme or SCHEMES[DEFAULT_SCHEME]
    fg, bg, bold, _u, _s, reverse = style

    def conv(name, is_fg):
        if name == "default":
            return None
        if name in COLOR_INDEX:
            i = COLOR_INDEX[name]
            if is_fg and bold and i < 8:
                i += 8  # bold ANSI colours are drawn bright, like most terminals
            return scheme["ansi"][i]
        if re.fullmatch(r"[0-9a-fA-F]{6}", name):
            return "#" + name.lower()
        return None
    f, b = conv(fg, True), conv(bg, False)
    if bold and f is None:
        f = scheme["bold"]
    if reverse:
        f, b = (b or scheme["bg"]), (f or scheme["fg"])
    return f, b


# ─── The terminal tab ────────────────────────────────────────────────────────

def add_timestamps(text, at_line_start, now=None):
    """Put [HH:MM:SS] at the start of every line of `text`. `at_line_start` says whether `text` begins a new line.
    Returns (stamped text, whether the output now ends at the start of a line)."""
    stamp = "[" + time.strftime("%H:%M:%S", time.localtime(now)) + "] "
    out = []
    for piece in text.splitlines(keepends=True):
        out.append((stamp if at_line_start else "") + piece)
        at_line_start = piece.endswith("\n")
    return "".join(out), at_line_start


class TerminalTab(ttk.Frame):
    AUTO_CONNECT = True  # tests switch this off

    def __init__(self, app, host, user, password, parent=None, playback=False):
        super().__init__(parent if parent is not None else app.nb)
        self.playback = playback          # True: a viewer for a recording (no connection, no toolbar)
        self.fixed_size = None            # (cols, rows) a recording forces on the screen
        self.recorder = None
        self.app, self.host, self.creds = app, host, (user, password)
        self.client = self.chan = None
        self.closed = False
        self.ever_connected = False
        self.attempt = 0
        self.reconnect_job = self.render_job = self.resize_job = None
        self.kind = host.get("_kind", "ssh")      # "ssh", "telnet" or "serial"
        self.panel, self.expect = None, None
        self.history = rc_quick.HistoryRecorder(self.cursor_line, lambda: self.screen.alt)
        self.generation = 0  # bumps on every (re)connect so a stale reader thread can't touch the new session
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.prefs = load_prefs()
        self.scheme = SCHEMES[self.prefs["scheme"]]
        family = self.prefs["font"]
        if family not in tkfont.families(self):
            family = FONT_DEFAULT_FAMILY if FONT_DEFAULT_FAMILY in tkfont.families(self) else "Courier New"
        self.font = tkfont.Font(family=family, size=self.prefs["font_size"])
        self.sb_limit = self.prefs["scrollback"]
        self.sb = 0  # scrollback lines held at the top of the Text widget
        self.styles = {}
        self.log_file = None
        self.log_bol = True                 # the log is at the start of a line (for timestamps)
        self.suggest = None                 # the open command-suggestion popup
        self.tail, self.totp_last = "", 0.0  # recent output and when an authenticator code was last typed
        bar = ttk.Frame(self)
        if not playback:
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
        self.zm_cancel_btn = ttk.Button(bar, text="✕ Cancel Zmodem", command=self.zmodem_cancel)      # shown only during a transfer
        self.zm_cancel = threading.Event()
        self.rec_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="⏺ Record", variable=self.rec_var, command=self.toggle_record).pack(side="left", padx=2)
        self.bcast_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="📡 Broadcast", variable=self.bcast_var, command=self.bcast_changed).pack(side="left", padx=4)
        ttk.Button(bar, text="⚡ Quick", command=self.toggle_panel).pack(side="left", padx=2)
        ttk.Button(bar, text="A−", width=3, command=lambda: self.change_font(-1)).pack(side="left")
        ttk.Button(bar, text="A+", width=3, command=lambda: self.change_font(+1)).pack(side="left", padx=(2, 0))
        self.status_var = tk.StringVar(value="Connecting ...")
        ttk.Label(bar, textvariable=self.status_var).pack(side="right", padx=8)
        self.reconnect_btn = ttk.Button(bar, text="⟳ Reconnect", command=self.reconnect_now, state="disabled")
        self.reconnect_btn.pack(side="right", padx=2)

        self.t = tk.Text(self, bg=self.scheme["bg"], fg=self.scheme["fg"], insertbackground=self.scheme["cursor"], insertwidth=2,
                         font=self.font, wrap="none", undo=False, borderwidth=0, padx=4, pady=2,
                         selectbackground=self.scheme["sel"], exportselection=False, cursor="xterm")
        self.t._rc_fixed = True  # the terminal keeps its own colours in every theme
        sb = self.sbar = ttk.Scrollbar(self, command=self.t.yview)
        self.t.configure(yscrollcommand=self.on_yscroll)
        sb.pack(side="right", fill="y")
        self.gutter = tk.Text(self, width=6, bg=self.scheme["bg"], fg="#7d8590", font=self.font, wrap="none", borderwidth=0, padx=4, pady=2,
                              state="disabled", cursor="arrow", takefocus=0, highlightthickness=0, selectbackground=self.scheme["bg"])
        self.gutter._rc_fixed = True
        self.gutter.tag_configure("num", justify="right")
        self.gutter_on = False
        self.t.pack(side="left", fill="both", expand=True)
        self.apply_gutter()
        self.t.tag_configure("tcursor", background=self.scheme["cursor"], foreground=self.scheme["bg"])
        self.t.tag_configure("found", background="#e5c07b", foreground="#000000")
        self.t.bind("<Key>", self.on_key)
        self.t.bind("<Button-1>", lambda e: self.after_idle(self.t.focus_set))
        self.t.bind("<Button-3>", self.popup)
        self.t.bind("<Button-2>", self.middle_click)
        self.t.bind("<ButtonRelease-1>", self.select_release, add="+")
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
        self.menu.add_separator()
        sch = tk.Menu(self.menu, tearoff=0, postcommand=lambda: self.fill_scheme_menu(sch))
        self.scheme_var = tk.StringVar(value=self.prefs["scheme"])
        self.menu.add_cascade(label="Colours", menu=sch)
        self.menu.add_command(label="Font…", command=self.choose_font)
        self.opt_vars = {}
        for key, label in (("copy_on_select", "Copy when I select text"), ("right_click_paste", "Right-click pastes (no menu)"),
                           ("middle_click_paste", "Middle-click pastes"), ("zmodem", "Zmodem file transfers (rz / sz)"),
                           ("log_timestamps", "Timestamp every line in the log file"),
                           ("line_numbers", "Show line numbers")):
            self.opt_vars[key] = tk.BooleanVar(value=self.prefs[key])
            self.menu.add_checkbutton(label=label, variable=self.opt_vars[key], command=lambda k=key: self.set_option(k))
        self.menu.add_separator()
        self.menu.add_command(label="Save output to a file…", command=self.save_output)
        self.menu.add_command(label="Clear screen (keep scrollback)", command=self.clear_keep_history)
        self.menu.add_separator()
        self.menu.add_command(label="⇧ Upload files with Zmodem (runs rz -be)…", command=self.zmodem_upload)
        self.menu.add_command(label="Folder for files received with Zmodem…", command=self.zmodem_pick_dir)

        self.update_idletasks()
        self.cols, self.rows = self.size()
        self.screen = RcScreen(self.cols, self.rows, on_input=self.send, on_bell=self.t.bell)
        self.stream = pyte.Stream(self.screen, strict=False)
        self.t.insert("1.0", "\n" * (self.rows - 1))
        if not playback:
            self.write_local(f"Connecting to {host['address']} ...\r\n")
        if self.AUTO_CONNECT and not playback:
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
        kind = self.kind
        try:
            if kind == "telnet":
                import rc_transport
                client, chan = None, rc_transport.open_telnet(self.host, self.cols, self.rows)
            elif kind == "serial":
                import rc_transport
                client, chan = None, rc_transport.open_serial(self.host.get("serial_spec"), self.host.get("serial_baud") or 0)
            else:
                client = core.open_ssh_client(self.app, self.host, user, password)
                tr = client.get_transport()
                if tr:
                    tr.set_keepalive(30)  # notices a dead link instead of freezing silently
                chan = client.invoke_shell(term="xterm-256color", width=self.cols, height=self.rows)  # latest size: the tab has been laid out by now
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            fatal = (kind == "serial" or isinstance(e, (paramiko.AuthenticationException, paramiko.BadHostKeyException))
                     or "not trusted" in msg)          # a busy or missing COM port will not fix itself by retrying
            log.warning("%s tab: connection to %s failed: %s", kind.capitalize(), self.host["address"], msg)
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
        log.info("%s tab: connected to %s", kind.capitalize(), self.host["address"])
        self.app.post(lambda: self.connected(gen))
        zdetect = rc_zmodem.Detector() if kind == "ssh" else None     # rz / sz work over SSH only (Telnet and Serial are not 8-bit clean here)
        while not self.closed and gen == self.generation:
            try:
                data = chan.recv(8192)
            except Exception:
                break
            if not data:
                break
            zstart = None
            if zdetect is not None and self.prefs.get("zmodem", True):
                data, zstart = zdetect.feed(data)
            if data:
                text = self.decoder.decode(data)
                self.app.post(lambda t=text, g=gen: g == self.generation and self.remote_data(t))
            if zstart is not None:
                self.run_zmodem(chan, zstart)
        clean = False
        try:
            if client is None:                  # telnet / serial: the channel itself knows (server hung up = log-out)
                clean = bool(chan.exit_status_ready())
            else:
                tr = client.get_transport()
                clean = bool(chan.exit_status_ready() and tr is not None and tr.is_active())
        except Exception:
            pass
        log.info("%s tab: connection to %s ended (%s)", kind.capitalize(), self.host["address"], "shell exited" if clean else "dropped")
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
        self.start_login_script()

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
        if self.expect:
            self.expect.stop()
        if self.recorder:
            self.recorder.close()
            self.recorder = None
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
        if self.fixed_size:
            return self.fixed_size
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
        if self.recorder:
            self.recorder.resize(cols, rows)
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
        groups = {}
        for s in snips:
            groups.setdefault((s.get("group") or "").strip(), []).append(s)
        for s in groups.pop("", []):
            menu.add_command(label=s["name"], command=lambda s=s: self.run_snippet(s))
        for g in sorted(groups, key=str.lower):
            sub = tk.Menu(menu, tearoff=0)
            for s in groups[g]:
                sub.add_command(label=s["name"], command=lambda s=s: self.run_snippet(s))
            menu.add_cascade(label=g, menu=sub)
        if not snips:
            menu.add_command(label="(no snippets - Tools > Snippets)", state="disabled")

    def run_snippet(self, s):
        if s.get("confirm", True) and not messagebox.askyesno("Snippet", f"Type this into the terminal?\n\n{s['command']}",
                                                              parent=self):
            return
        self.send_user(s["command"].replace("\r\n", "\n").replace("\n", "\r") + "\r")
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

    def terminals(self):
        return [self]

    # quick commands panel, history, login script
    def toggle_panel(self):
        if self.panel is not None and self.panel.winfo_manager():
            self.panel.pack_forget()
            self.t.focus_set()
            return
        if self.panel is None:
            self.panel = rc_quick.QuickPanel(self, self)
            theme = getattr(self.app, "theme", None)
            if theme:
                theme.paint(self.panel)
        self.panel.pack(side="right", fill="y", before=self.sbar)
        self.panel.refresh()

    def cursor_line(self):
        """The screen line the cursor is on, up to the cursor (used to spot a Password: prompt)."""
        try:
            c = self.screen.cursor
            return self.screen.display[c.y][:c.x]
        except Exception:
            return ""

    # ── Zmodem (rz / sz) ──
    def zmodem_cancel(self):
        self.zm_cancel.set()

    def zmodem_pick_dir(self, ask=True):
        """The folder received files go in: remembered, asked for the first time (or when `ask`)."""
        from tkinter import filedialog
        current = self.prefs.get("zmodem_dir", "")
        if current and os.path.isdir(current) and not ask:
            return current
        start = current if os.path.isdir(current or "") else os.path.join(os.path.expanduser("~"), "Downloads")
        folder = filedialog.askdirectory(parent=self, title="Save files received with Zmodem in ...", initialdir=start)
        if folder:
            self.set_pref(zmodem_dir=folder)
        return folder or ""

    def zmodem_upload(self):
        """Right-click > Upload files with Zmodem: type `rz -be` and let the server's rz start the transfer."""
        if self.kind != "ssh" or not self.chan:
            messagebox.showinfo("Zmodem", "Zmodem works in a connected SSH terminal.", parent=self)
            return
        self.send("rz -be\r")

    def run_zmodem(self, chan, first):
        """Called from the reader thread when the server starts Zmodem (`sz` = it sends, `rz` = it waits): this thread
        takes over the connection until the transfer ends, then the terminal carries on."""
        import socket as _socket
        sending = first.startswith(b"**\x18B01")           # ZRINIT: the server (rz) is waiting for files
        pending = bytearray(first)
        app, post = self.app, self.app.post

        def read(n, timeout):
            if pending:
                d = bytes(pending)
                pending.clear()
                return d
            chan.settimeout(timeout)
            try:
                return chan.recv(n)
            except _socket.timeout:
                return None

        def say(msg):
            post(lambda: self.write_local(f"\r\n\x1b[36m[{msg}]\x1b[0m\r\n"))
        self.zm_cancel.clear()
        post(lambda: self.zm_cancel_btn.pack(side="left", padx=4))
        ui = _ZmodemUi(self)
        try:
            if sending:
                files = core.ui_call(app, self.zmodem_choose_files)
                if not files:
                    chan.sendall(rc_zmodem.abort_bytes())
                    say("Zmodem: upload cancelled")
                    return
                post(lambda: app.status.set(f"Zmodem: sending {len(files)} file(s) ..."))
                results = rc_zmodem.Sender(read, chan.sendall, ui, log.debug).send(files)
                sent = [r for r in results if r[2] == "sent"]
                say(f"Zmodem: sent {len(sent)} file(s): " + ", ".join(f"{n} ({core.fmt_size(sz)})" for n, sz, _ in sent)
                    + (f"; {len(results) - len(sent)} skipped by the server" if len(sent) != len(results) else ""))
            else:
                folder = core.ui_call(app, lambda: self.zmodem_pick_dir(ask=False))
                if not folder:
                    chan.sendall(rc_zmodem.abort_bytes())
                    say("Zmodem: download cancelled (no folder chosen)")
                    return
                ui.folder = folder
                post(lambda: app.status.set("Zmodem: receiving ..."))
                saved = rc_zmodem.Receiver(read, chan.sendall, ui, log.debug).receive()
                say("Zmodem: received " + (", ".join(f"{n} ({core.fmt_size(sz)})" for n, sz, _ in saved) or "nothing") + f" in {folder}")
        except rc_zmodem.ZCancelled as e:
            say(f"Zmodem stopped: {e}")
        except (rc_zmodem.ZError, OSError) as e:
            say(f"Zmodem failed: {e}")
        except Exception as e:
            log.exception("Zmodem transfer crashed")
            say(f"Zmodem failed: {e}")
        finally:
            try:
                chan.settimeout(None)
            except Exception:
                pass
            post(lambda: (self.zm_cancel_btn.pack_forget(), app.status.set("")))

    def zmodem_choose_files(self):
        from tkinter import filedialog
        files = filedialog.askopenfilenames(parent=self, title="Choose the files to send with Zmodem")
        return list(files)

    def clear_keep_history(self):
        """Clear the screen but scroll what was on it into the scrollback (nothing is lost, unlike Clear screen and scrollback)."""
        self.feed("\r\n" * self.rows)
        self.send("\x0c")

    def clear_for_playback(self):
        """Empty the screen and scrollback (the recording player replays from a point)."""
        self.clear()

    def toggle_record(self):
        """⏺ Record: save what the server sends (with timing) as an asciicast file you can replay (Tools > Play a recording)."""
        import rc_record
        if self.rec_var.get():
            try:
                self.recorder = rc_record.Recorder.for_session(self.host.get("name", "session"), self.cols, self.rows,
                                                               f"{self.kind} {self.host.get('name', '')}")
            except OSError as e:
                self.rec_var.set(False)
                messagebox.showerror("Record", f"Could not start the recording:\n{e}", parent=self)
                return
            self.app.status.set(f"⏺ Recording to {self.recorder.path}")
        elif self.recorder:
            path = self.recorder.close()
            self.recorder = None
            self.app.status.set(f"Recording saved: {path}")

    def totp_secret(self):
        blob = self.host.get("totp_secret_enc") or ""
        try:
            return core.unprotect_password(blob) if blob else ""
        except Exception:
            log.warning("could not open the authenticator secret for %s", self.host.get("name"))
            return ""

    def maybe_send_totp(self, text):
        """If the server just asked for a verification code and this server has an authenticator secret, type the code (once per prompt)."""
        self.tail = (self.tail + _ESC.sub("", text))[-300:]
        if self.playback or self.kind != "ssh" or not self.host.get("totp_secret_enc") or not rc_totp.is_code_prompt(self.tail):
            return
        now = time.time()
        if now - self.totp_last < 20:
            return
        secret = self.totp_secret()
        if not secret:
            return
        self.totp_last, self.tail = now, ""
        try:
            self.send(rc_totp.fresh_code(secret) + "\r")
            self.app.status.set("Typed the authenticator code for you")
        except Exception:
            log.warning("could not send the authenticator code", exc_info=True)

    # ── command suggestions (Ctrl+Space) ──
    def show_suggestions(self):
        self.close_suggestions()
        if self.screen.alt or self.playback:
            return
        typed = "" if self.history.unreliable else "".join(self.history.buf)
        cmds = rc_suggest.suggestions_for(typed)
        if not cmds:
            self.app.status.set("No remembered commands match" if typed else "No commands remembered yet")
            return
        box = self.t.bbox("insert") or (0, 0, 0, 16)
        x, y = self.t.winfo_rootx() + box[0], self.t.winfo_rooty() + box[1] + box[3] + 2
        self.suggest = rc_suggest.SuggestPopup(self, cmds, x, y, lambda c: self.fill_line(typed, c))

    def close_suggestions(self):
        if self.suggest is not None:
            self.suggest.close()
            self.suggest = None

    def fill_line(self, typed, cmd):
        """Replace what is on the command line with `cmd` (not run). Ctrl+U clears the line in bash, zsh and most shells."""
        self.suggest = None
        clear = "\x15" if (typed or self.history.unreliable) else ""
        self.send(clear + cmd)
        self.history.buf, self.history.unreliable = list(cmd), False

    def suggest_key(self, key):
        """Keys while the suggestion list is open. Returns True when the key was used."""
        pop = self.suggest
        if key == "Up":
            pop.move(-1)
        elif key == "Down":
            pop.move(1)
        elif key in ("Return", "Tab"):
            self.suggest = None
            pop.choose()
        elif key == "Escape":
            self.close_suggestions()
        else:
            self.close_suggestions()
            return False
        return True

    def remote_data(self, text):
        if self.recorder:
            self.recorder.output(text)
        self.feed(text)
        self.maybe_send_totp(text)
        if self.expect is not None:
            self.expect.feed(text)

    def start_login_script(self):
        if self.expect:
            self.expect.stop()
            self.expect = None
        script = (self.host.get("login_script") or "").strip()
        if not script:
            return
        try:
            steps = rc_quick.parse_login_script(script)
        except ValueError as e:
            self.write_local(f"\r\n\x1b[31m[login script not run: {e}]\x1b[0m\r\n")
            return
        if not steps:
            return
        user, pw = self.creds
        note = lambda m: self.write_local(f"\r\n\x1b[36m[{m}]\x1b[0m\r\n")
        self.expect = rc_quick.ExpectRunner(
            steps, self.send, self.after, self.after_cancel, note,
            subst=lambda t: rc_quick.substitute(t, user, self.host.get("address", ""), pw,
                                                rc_totp.fresh_code(self.totp_secret()) if "${totp}" in t and self.totp_secret() else ""))
        self.expect.start()

    def bcast_changed(self):
        ask = getattr(self.app, "confirm_broadcast", None)
        if self.bcast_var.get() and ask and not ask():
            self.bcast_var.set(False)
        fn = getattr(self.app, "broadcast_changed", None)
        if fn:
            fn()

    def send_user(self, s, paste_text=None):
        """Something the user typed, pasted or ran from a snippet: goes to this session and, with Broadcast
        ticked, to every other terminal that also has Broadcast ticked. (send() alone is used for the
        terminal's own replies to the server, which must never be copied to other sessions.)"""
        self.send(s)
        try:
            self.history.typed(paste_text if paste_text is not None else s)
            if self.panel is not None and self.panel.winfo_ismapped() and s == "\r":
                self.after(200, self.panel.refresh)
        except Exception:
            log.debug("history recorder failed", exc_info=True)
        if not self.bcast_var.get():
            return
        peers = getattr(self.app, "broadcast_peers", None)
        for peer in (peers(self) if peers else []):
            peer.send(peer.wrap_paste(paste_text) if paste_text is not None else s)

    def wrap_paste(self, text):
        return "\x1b[200~" + text + "\x1b[201~" if self.screen.bracketed else text

    def on_key(self, e):
        ctrl, shift = e.state & 0x4, e.state & 0x1
        alt = e.state & 0x20000
        key = e.keysym
        if self.suggest is not None and self.suggest_key(key):
            return "break"
        if ctrl and key == "space":
            self.show_suggestions()
            return "break"
        handler = getattr(self.app, "handle_shortcut", None)
        if ctrl and handler and handler(key, True, bool(shift)):
            return "break"                                  # Ctrl+1..9 / Ctrl+Tab / Ctrl+Shift+D belong to the program, not the server
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
            self.send_user(("\x1bO" if self.screen.app_cursor else "\x1b[") + CURSOR_KEYS[key])
        elif key in KEYS:
            self.send_user(KEYS[key])
        elif e.char:
            self.send_user(("\x1b" + e.char) if (alt and not ctrl) else e.char)
        return "break"

    def popup(self, e):
        if self.prefs["right_click_paste"]:
            self.paste()
            return "break"
        self.menu.tk_popup(e.x_root, e.y_root)

    def middle_click(self, e):
        if self.prefs["middle_click_paste"]:
            self.paste()
        return "break"

    def select_release(self, e):
        if self.prefs["copy_on_select"] and self.t.tag_ranges("sel"):
            self.copy()

    # ── colours, font, options, saving the output ──
    def terminals_to_update(self):
        fn = getattr(self.app, "all_terminals", None)
        return fn() if fn else [self]

    def set_pref(self, **kw):
        prefs = load_prefs()
        prefs.update(kw)
        save_prefs(prefs)
        for t in self.terminals_to_update():
            t.prefs = load_prefs()

    def fill_scheme_menu(self, menu):
        menu.delete(0, "end")
        for name in SCHEMES:
            menu.add_radiobutton(label=name, variable=self.scheme_var, value=name, command=lambda n=name: self.choose_scheme(n))
        menu.add_separator()
        menu.add_command(label="Import a colour scheme…", command=self.import_scheme)
        custom = [n for n in SCHEMES if n not in BUILTIN_SCHEMES]
        if custom:
            rm = tk.Menu(menu, tearoff=0)
            for name in custom:
                rm.add_command(label=name, command=lambda n=name: self.remove_scheme(n))
            menu.add_cascade(label="Remove an imported scheme", menu=rm)

    def import_scheme(self):
        names = rc_schemes.import_dialog(self, SCHEMES, BUILTIN_SCHEMES, SCHEMES[DEFAULT_SCHEME])
        if names:
            self.choose_scheme(names[0])

    def remove_scheme(self, name):
        if rc_schemes.remove_dialog(self, SCHEMES, BUILTIN_SCHEMES, name) and self.scheme_var.get() == name:
            self.choose_scheme(DEFAULT_SCHEME)

    def choose_scheme(self, name):
        self.set_pref(scheme=name)
        for t in self.terminals_to_update():
            t.apply_scheme(name)

    def apply_scheme(self, name):
        if name not in SCHEMES:
            return
        sc = self.scheme = SCHEMES[name]
        self.scheme_var.set(name)
        self.t.configure(bg=sc["bg"], fg=sc["fg"], insertbackground=sc["cursor"], selectbackground=sc["sel"])
        self.t.tag_configure("tcursor", background=sc["cursor"], foreground=sc["bg"])
        for style, tag in self.styles.items():              # recolour what is already on screen and in the scrollback
            fg, bg = style_colors(style, sc)
            self.t.tag_configure(tag, foreground=fg or sc["fg"], background=bg or sc["bg"])
        self.t.tag_raise("sel")
        self.schedule_render()

    def choose_font(self):
        FontDialog(self)

    def apply_font(self, family):
        self.font.configure(family=family)
        self.after_idle(self.apply_size)

    def set_option(self, key):
        self.set_pref(**{key: bool(self.opt_vars[key].get())})
        if key == "line_numbers":
            for t in self.terminals_to_update():
                t.apply_gutter()

    # ── line numbers ──
    def on_yscroll(self, first, last):
        self.sbar.set(first, last)
        if self.gutter_on:
            self.gutter.yview_moveto(first)

    def apply_gutter(self):
        want = bool(self.prefs.get("line_numbers"))
        if want and not self.gutter_on:
            self.gutter_on = True
            self.gutter.pack(side="left", fill="y", before=self.t)
            self.gutter.configure(font=self.font, bg=self.scheme["bg"])
            self.gutter_count = 0
            self.sync_gutter()
        elif not want and self.gutter_on:
            self.gutter_on = False
            self.gutter.pack_forget()
        if "line_numbers" in getattr(self, "opt_vars", {}):
            self.opt_vars["line_numbers"].set(want)

    def sync_gutter(self):
        """Keep one number per line of the terminal text (scrollback and screen), counted from the top of what is kept."""
        if not self.gutter_on:
            return
        total = int(self.t.index("end-1c").split(".")[0])
        have = getattr(self, "gutter_count", 0)
        if total == have:
            return
        g = self.gutter
        g.configure(state="normal")
        if total > have:
            g.insert("end", "".join(("" if n == 1 else "\n") + str(n) for n in range(have + 1, total + 1)), "num")
        else:
            g.delete(f"{total}.end", "end")
        g.configure(state="disabled")
        self.gutter_count = total
        digits = max(3, len(str(total)))
        g.configure(width=digits + 1)
        g.yview_moveto(self.t.yview()[0])

    def output_text(self):
        """Everything in the scrollback and on the screen as plain text."""
        self.render()
        lines = self.t.get("1.0", "end-1c").split("\n")
        return "\n".join(l.rstrip() for l in lines).rstrip("\n") + "\n"

    def save_output(self):
        from tkinter import filedialog
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", self.host.get("name", "session")) + time.strftime("_%Y%m%d_%H%M%S") + ".txt"
        path = filedialog.asksaveasfilename(parent=self, title="Save the terminal output", initialfile=name,
                                            defaultextension=".txt", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return None
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.output_text())
        except OSError as e:
            messagebox.showerror("Save output", f"Could not save:\n{e}", parent=self)
            return None
        self.app.status.set(f"Saved the terminal output to {path}")
        return path

    def copy(self):
        if self.t.tag_ranges("sel"):
            self.clipboard_clear()
            self.clipboard_append(self.t.get("sel.first", "sel.last"))

    def paste(self):
        try:
            self.paste_text(self.clipboard_get())
        except tk.TclError:
            return

    def paste_text(self, text):
        """Paste (or quick-command paste) with a check before several lines go in at once: Enter inside the text
        would run them, and with Broadcast on they would run on every ticked terminal."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.rstrip("\n").split("\n")
        if len(lines) > 1 and rc_quick.load_store()["confirm_paste"]:
            peers = len(self.app.broadcast_peers(self)) if self.bcast_var.get() and hasattr(self.app, "broadcast_peers") else 0
            preview = "\n".join(l[:80] for l in lines[:6]) + ("\n..." if len(lines) > 6 else "")
            if not messagebox.askyesno("Paste several lines", f"Paste {len(lines)} lines into {self.host['name']}"
                                       + (f" and {peers} other terminal(s)" if peers else "")
                                       + f"?\nEach line is run as you paste.\n\n{preview}", parent=self, icon="warning"):
                return
        text = text.replace("\n", "\r")
        self.send_user(self.wrap_paste(text), paste_text=text)

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
                plain = _ESC.sub("", data).replace("\r\n", "\n").replace("\r", "")
                if self.prefs.get("log_timestamps"):
                    plain, self.log_bol = add_timestamps(plain, self.log_bol)
                self.log_file.write(plain)
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
            fg, bg = style_colors(style, self.scheme)
            tag = f"st{len(self.styles)}"
            self.styles[style] = tag
            self.t.tag_configure(tag, foreground=fg or self.scheme["fg"], background=bg or self.scheme["bg"],
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
        self.sync_gutter()


class SplitTab(ttk.Frame):
    """Several SSH terminals tiled in one tab (Session > New split view).

    Each pane is a normal TerminalTab (reconnect, find, logging, scrollback all work per pane). The tab's
    📡 button turns Broadcast on for every pane at once: whatever you type in one pane goes to all of them."""
    MAX_PANES = 9

    def __init__(self, app):
        super().__init__(app.nb)
        self.app = app
        self.panes = []          # each: {"frame", "title", "tab", "host"}
        self.active = None
        self.zoomed = None
        self.closed = False
        self.layout_var = tk.StringVar(value="grid")
        self.bcast_var = tk.BooleanVar(value=False)

        bar = ttk.Frame(self)
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="➕ Add server ▾", command=self.add_menu).pack(side="left", padx=4, pady=2)
        lay = ttk.Menubutton(bar, text="▦ Layout ▾")
        lay.pack(side="left", padx=2)
        lm = tk.Menu(lay, tearoff=0)
        for label, val in (("Grid (automatic)", "grid"), ("Side by side", "cols"), ("Stacked", "rows")):
            lm.add_radiobutton(label=label, variable=self.layout_var, value=val, command=self.relayout)
        lay.configure(menu=lm)
        ttk.Checkbutton(bar, text="📡 Type in ALL panes", variable=self.bcast_var,
                        command=self.toggle_broadcast).pack(side="left", padx=10)
        ttk.Button(bar, text="💾 Save workspace", command=lambda: self.app.save_workspace()).pack(side="left")
        self.info = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.info).pack(side="right", padx=8)

        self.area = ttk.Frame(self)
        self.area.pack(fill="both", expand=True)
        self.hint = ttk.Label(self.area, text="Click ➕ Add server to place a terminal here.\n"
                              "Add up to 9. Double-click a pane's title to zoom it.", justify="center")
        self.hint.place(relx=0.5, rely=0.5, anchor="center")

    # ── panes ──
    def terminals(self):
        return [p["tab"] for p in self.panes]

    def add_menu(self):
        m = tk.Menu(self, tearoff=0)
        hosts = sorted(self.app.hosts, key=lambda h: (h["group"].lower(), h["name"].lower()))
        if not hosts:
            m.add_command(label="(no servers yet)", state="disabled")
        groups = {}
        for h in hosts:
            groups.setdefault(h["group"] or "Ungrouped", []).append(h)
        for g, items in groups.items():
            gm = tk.Menu(m, tearoff=0)
            for h in items:
                gm.add_command(label=f"{h['name']}  ({h['address']})", command=lambda h=h: self.add_host(h))
            m.add_cascade(label=f"{g}  [{len(items)}]", menu=gm)
        self.app.theme.paint(m)
        m.tk_popup(self.winfo_rootx() + 20, self.winfo_rooty() + 30)

    def add_host(self, h, creds=None):
        if len(self.panes) >= self.MAX_PANES:
            messagebox.showinfo("Split view", f"A split view holds up to {self.MAX_PANES} terminals.", parent=self)
            return None
        c = creds or self.app.credentials(h)
        if not c:
            return None
        frame = ttk.Frame(self.area, relief="solid", borderwidth=1)
        head = ttk.Frame(frame)
        head.pack(side="top", fill="x")
        title = ttk.Label(head, text=h["name"], font=("Segoe UI", 9, "bold"))
        title.pack(side="left", padx=6)
        pane = {"frame": frame, "title": title, "host": h}
        ttk.Button(head, text="✕", width=3, command=lambda: self.remove_pane(pane)).pack(side="right", padx=2)
        ttk.Button(head, text="⛶", width=3, command=lambda: self.zoom(pane)).pack(side="right")
        title.bind("<Double-1>", lambda e: self.zoom(pane))
        tab = TerminalTab(self.app, h, *c, parent=frame)
        tab.pack(side="top", fill="both", expand=True)
        pane["tab"] = tab
        tab.t.bind("<FocusIn>", lambda e: self.set_active(pane), add="+")
        if self.bcast_var.get():
            tab.bcast_var.set(True)
        self.panes.append(pane)
        self.hint.place_forget()
        self.relayout()
        self.app.theme.paint(frame)
        try:
            self.app.mark_connected(h)
        except Exception:
            core.log.debug("mark_connected failed for split pane", exc_info=True)
        tab.t.focus_set()
        return pane

    def remove_pane(self, pane):
        if pane not in self.panes:
            return
        self.panes.remove(pane)
        pane["tab"].close()
        pane["frame"].destroy()
        if self.zoomed is pane:
            self.zoomed = None
        if self.active is pane:
            self.active = None
        if not self.panes:
            self.hint.place(relx=0.5, rely=0.5, anchor="center")
        self.relayout()
        self.app.broadcast_changed()

    def set_active(self, pane):
        self.active = pane
        for p in self.panes:
            p["title"].configure(text=("▶ " if p is pane else "") + p["host"]["name"])

    def zoom(self, pane):
        self.zoomed = None if self.zoomed is pane else pane
        self.relayout()
        pane["tab"].t.focus_set()

    # ── layout ──
    def relayout(self):
        for w in self.area.winfo_children():
            if w is not self.hint:
                w.grid_forget()
        for i in range(self.MAX_PANES + 1):
            self.area.rowconfigure(i, weight=0)
            self.area.columnconfigure(i, weight=0)
        shown = [self.zoomed] if self.zoomed in self.panes else self.panes
        n = len(shown)
        if n == 0:
            self.info.set("")
            return
        mode = self.layout_var.get()
        cols = 1 if n == 1 else (n if mode == "cols" else 1 if mode == "rows" else int(n ** 0.5 + 0.999))
        rows = -(-n // cols)
        for i, p in enumerate(shown):
            r, c = divmod(i, cols)
            span = cols - c if i == n - 1 else 1   # a last, lonely pane fills its row
            p["frame"].grid(row=r, column=c, columnspan=span, sticky="nsew", padx=1, pady=1)
        for r in range(rows):
            self.area.rowconfigure(r, weight=1, uniform="r")
        for c in range(cols):
            self.area.columnconfigure(c, weight=1, uniform="c")
        self.info.set(f"{len(self.panes)} terminal(s)" + ("  ·  zoomed" if shown != self.panes else ""))

    # ── broadcast ──
    def toggle_broadcast(self):
        on = self.bcast_var.get()
        if on and not self.app.confirm_broadcast():
            self.bcast_var.set(False)
            return
        for p in self.panes:
            p["tab"].bcast_var.set(on)
        self.app.broadcast_changed()

    def refresh_broadcast(self):
        """Keep the 📡 box in step when single panes are ticked or unticked."""
        self.bcast_var.set(bool(self.panes) and all(p["tab"].bcast_var.get() for p in self.panes))

    def close(self):
        self.closed = True
        for p in self.panes:
            p["tab"].close()


_MONO_CACHE = []


def monospace_families(widget):
    """Installed fixed-width font families (cached; the first call takes a moment)."""
    if not _MONO_CACHE:
        found = []
        for fam in sorted(set(tkfont.families(widget)), key=str.lower):
            if fam.startswith("@"):
                continue
            try:
                if tkfont.Font(widget, family=fam, size=10).metrics("fixed"):
                    found.append(fam)
            except tk.TclError:
                pass
        _MONO_CACHE.extend(found)
    return list(_MONO_CACHE)


class FontDialog(tk.Toplevel):
    """Pick the terminal font family (fixed-width fonts only) with a live sample; applies to every open terminal."""

    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self.title("Terminal font")
        self.transient(tab.winfo_toplevel())
        self.geometry("+%d+%d" % (tab.winfo_rootx() + 80, tab.winfo_rooty() + 60))
        fams = monospace_families(self)
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Font (fixed-width fonts only)").pack(anchor="w")
        self.var = tk.StringVar(value=tab.font.cget("family"))
        self.box = ttk.Combobox(body, textvariable=self.var, values=fams, state="readonly", width=34)
        self.box.pack(anchor="w", pady=(2, 8))
        self.box.bind("<<ComboboxSelected>>", lambda e: self.preview())
        self.sample = tk.Label(body, text="ls -la /var/log   0O  1lI  {}[]  ~/.ssh", anchor="w", bg=tab.scheme["bg"], fg=tab.scheme["fg"],
                               font=(tab.font.cget("family"), tab.font.cget("size")), padx=8, pady=8)
        self.sample._rc_fixed = True
        self.sample.pack(fill="x")
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="Use this font", command=self.ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=6)

    def preview(self):
        self.sample.configure(font=(self.var.get(), self.tab.font.cget("size")))

    def ok(self):
        fam = self.var.get()
        if fam:
            self.tab.set_pref(font=fam)
            for t in self.tab.terminals_to_update():
                t.apply_font(fam)
        self.destroy()


class _ZmodemUi(rc_zmodem.Ui):
    """Connects a running Zmodem transfer to the terminal tab: where received files go, the progress line, Cancel."""

    def __init__(self, tab):
        self.tab, self.folder, self.last = tab, "", 0.0

    def accept_file(self, name, size):
        from rc_files import unique_name
        taken = os.listdir(self.folder) if os.path.isdir(self.folder) else []
        final = unique_name(name, taken, "copy")
        return ("save", os.path.join(self.folder, final))

    def progress(self, name, done, total, sending):
        now = time.time()
        if now - self.last < 0.2 and done < total:
            return
        self.last = now
        pct = f"{min(100, done * 100 // total)}%  " if total else ""
        msg = f"Zmodem {'sending' if sending else 'receiving'} {name}: {pct}{core.fmt_size(done)}" + (f" of {core.fmt_size(total)}" if total else "")
        self.tab.app.post(lambda: self.tab.app.status.set(msg))

    def cancelled(self):
        return self.tab.zm_cancel.is_set()
