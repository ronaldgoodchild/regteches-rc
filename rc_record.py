"""REGTeches RC - terminal session recording and playback.

* ⏺ Record (button on every terminal tab) saves what the server sends, with timing, as an asciicast v2 file
  (`recordings\\<server>_<time>.cast`: one JSON header line, then one `[seconds, "o", text]` line per chunk). The files
  also play in the asciinema player and tools. Only the server's output is recorded, not your keystrokes, but a
  recording shows everything that was on the screen, so treat it like a log (it may contain secrets).
* Tools > Play a terminal recording opens a player: play / pause, speed, a slider to jump around, and an option to
  skip long pauses. It uses the same terminal screen as a live session, so colours and full-screen programs look right.

The format reader and the timeline arithmetic are pure and tested without a window."""

import bisect
import json
import os
import re
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import rc_manager as core

log = core.log

MAX_GAP = 2.0            # "skip long pauses": the longest wait kept between two chunks, in seconds
MAX_COLS, MAX_ROWS = 500, 200


class CastError(Exception):
    """A readable problem with a recording file."""


def recordings_dir():
    return os.path.join(core.APP_DIR, "recordings")


# ─── writing ─────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self, path, cols, rows, title=""):
        self.path, self.start, self.closed = path, time.time(), False
        self.f = open(path, "w", encoding="utf-8", newline="\n")
        header = {"version": 2, "width": int(cols), "height": int(rows), "timestamp": int(self.start),
                  "title": title, "env": {"TERM": "xterm-256color"}}
        self.f.write(json.dumps(header) + "\n")
        self.f.flush()

    @classmethod
    def for_session(cls, name, cols, rows, title=""):
        os.makedirs(recordings_dir(), exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "session"
        path = os.path.join(recordings_dir(), f"{slug}_{time.strftime('%Y%m%d_%H%M%S')}.cast")
        n = 1
        while os.path.exists(path):                       # two recordings in the same second
            n += 1
            path = os.path.join(recordings_dir(), f"{slug}_{time.strftime('%Y%m%d_%H%M%S')}_{n}.cast")
        return cls(path, cols, rows, title)

    def event(self, kind, data):
        if self.closed:
            return
        try:
            self.f.write(json.dumps([round(time.time() - self.start, 6), kind, data]) + "\n")
            self.f.flush()
        except (OSError, ValueError):
            self.closed = True

    def output(self, text):
        if text:
            self.event("o", text)

    def resize(self, cols, rows):
        self.event("r", f"{int(cols)}x{int(rows)}")

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.f.close()
            except OSError:
                pass
        return self.path


# ─── reading ─────────────────────────────────────────────────────────────────

def parse_cast(text):
    """asciicast v2 text -> (header dict, events [(seconds, kind, data)]) with events in time order. Lines that are not
    events are skipped; a bad header raises CastError."""
    lines = [l for l in (text or "").replace("\r", "").split("\n") if l.strip()]
    if not lines:
        raise CastError("That file is empty.")
    try:
        header = json.loads(lines[0])
    except ValueError:
        raise CastError("That is not a terminal recording (the first line is not an asciicast header).")
    if not isinstance(header, dict) or header.get("version") != 2:
        raise CastError("Only asciicast version 2 recordings can be played (this is " +
                        (f"version {header.get('version')}" if isinstance(header, dict) and header.get("version") else "not one") + ").")
    try:
        cols, rows = int(header.get("width")), int(header.get("height"))
    except (TypeError, ValueError):
        raise CastError("The recording does not say how big the screen was.")
    header["width"], header["height"] = max(10, min(MAX_COLS, cols)), max(3, min(MAX_ROWS, rows))
    events = []
    for line in lines[1:]:
        try:
            ev = json.loads(line)
            t, kind, data = ev[0], ev[1], ev[2]
            if isinstance(t, (int, float)) and t >= 0 and isinstance(kind, str) and isinstance(data, str):
                events.append((float(t), kind, data))
        except (ValueError, IndexError, TypeError, KeyError):
            continue
    events.sort(key=lambda e: e[0])
    return header, events


class Timeline:
    """The arithmetic of playback: how long to wait between events and where a time falls in the list."""

    def __init__(self, events, skip_pauses=True, max_gap=MAX_GAP):
        self.events, self.skip_pauses, self.max_gap = events, skip_pauses, max_gap
        self.times = [e[0] for e in events]

    @property
    def duration(self):
        return self.times[-1] if self.times else 0.0

    def delay_after(self, i, speed=1.0):
        """Seconds to wait after event i before showing event i+1."""
        if i + 1 >= len(self.events):
            return 0.0
        gap = self.times[i + 1] - self.times[i]
        if self.skip_pauses:
            gap = min(gap, self.max_gap)
        return max(0.0, gap / max(speed, 0.01))

    def index_at(self, t):
        """Number of events at or before time t (so events[:n] is everything shown by then)."""
        return bisect.bisect_right(self.times, t)


def fmt_time(sec):
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"


# ─── the player tab ──────────────────────────────────────────────────────────

SPEEDS = ("0.5x", "1x", "2x", "4x", "8x")


class PlayerTab(ttk.Frame):
    def __init__(self, app, path, header, events):
        super().__init__(app.nb)
        import rc_terminal
        self.app, self.path, self.header = app, path, header
        self.timeline = Timeline(events)
        self.i = 0                     # next event to show
        self.playing = False
        self.job = None
        self.speed = tk.StringVar(value="1x")
        self.skip = tk.BooleanVar(value=True)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=4)
        self.play_btn = ttk.Button(bar, text="▶ Play", width=9, command=self.toggle)
        self.play_btn.pack(side="left")
        ttk.Button(bar, text="⟲ Restart", command=self.restart).pack(side="left", padx=4)
        ttk.Label(bar, text="Speed").pack(side="left", padx=(10, 2))
        box = ttk.Combobox(bar, textvariable=self.speed, values=SPEEDS, state="readonly", width=5)
        box.pack(side="left")
        ttk.Checkbutton(bar, text="Skip long pauses", variable=self.skip, command=self.options_changed).pack(side="left", padx=10)
        self.pos_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.pos_var, width=14).pack(side="right")
        self.scale = ttk.Scale(bar, from_=0, to=max(self.timeline.duration, 0.001), orient="horizontal", command=self.on_scale)
        self.scale.pack(side="left", fill="x", expand=True, padx=10)
        self.scale.bind("<ButtonRelease-1>", self.on_release)
        self._seeking = False
        title = header.get("title") or os.path.basename(path)
        ttk.Label(self, text=f"{title}   ·   {header['width']}x{header['height']}   ·   {fmt_time(self.timeline.duration)}   ·   {os.path.basename(path)}",
                  foreground="#555").pack(anchor="w", padx=8)
        self.screen = rc_terminal.TerminalTab(app, {"name": title, "address": ""}, "", "", parent=self, playback=True)
        self.screen.pack(fill="both", expand=True, padx=6, pady=6)
        self.set_size(header["width"], header["height"])
        self.update_position()

    # screen
    def set_size(self, cols, rows):
        t = self.screen
        t.fixed_size = (cols, rows)
        t.t.configure(width=cols, height=rows)
        t.apply_size()

    def show(self, kind, data):
        if kind == "o":
            self.screen.feed(data)
        elif kind == "r":
            m = re.fullmatch(r"(\d+)x(\d+)", data)
            if m:
                self.set_size(max(10, min(MAX_COLS, int(m.group(1)))), max(3, min(MAX_ROWS, int(m.group(2)))))

    def reset_screen(self):
        t = self.screen
        t.fixed_size = None
        t.fixed_size = (self.header["width"], self.header["height"])
        t.clear_for_playback()
        self.set_size(*t.fixed_size)

    # playing
    def toggle(self):
        if self.playing:
            self.pause()
        else:
            if self.i >= len(self.timeline.events):
                self.restart()
            else:
                self.play()

    def play(self):
        if self.playing or not self.timeline.events:
            return
        self.playing = True
        self.play_btn.configure(text="⏸ Pause")
        self.job = self.after(0, self.tick)

    def pause(self):
        self.playing = False
        self.play_btn.configure(text="▶ Play")
        if self.job:
            self.after_cancel(self.job)
            self.job = None

    def restart(self):
        self.pause()
        self.seek(0)
        self.play()

    def tick(self):
        self.job = None
        if not self.playing or self.i >= len(self.timeline.events):
            return self.finished()
        _, kind, data = self.timeline.events[self.i]
        self.show(kind, data)
        i = self.i
        self.i += 1
        self.update_position()
        if self.i >= len(self.timeline.events):
            return self.finished()
        delay = self.timeline.delay_after(i, self.speed_value())
        self.job = self.after(max(1, int(delay * 1000)), self.tick)

    def finished(self):
        self.playing = False
        self.play_btn.configure(text="▶ Replay")
        self.update_position()

    def speed_value(self):
        try:
            return float(self.speed.get().rstrip("x"))
        except ValueError:
            return 1.0

    def options_changed(self):
        self.timeline.skip_pauses = bool(self.skip.get())

    # seeking
    def seek(self, t):
        """Show the screen as it was at time t (re-plays everything up to there at once)."""
        was = self.playing
        if was:
            self.pause()
        self.reset_screen()
        n = self.timeline.index_at(t) if t > 0 else 0
        for _, kind, data in self.timeline.events[:n]:
            self.show(kind, data)
        self.i = n
        self.update_position()
        if was:
            self.play()

    def on_scale(self, value):
        if self._seeking:
            self.pos_var.set(f"{fmt_time(float(value))} / {fmt_time(self.timeline.duration)}")

    def on_release(self, _e):
        self._seeking = False
        self.seek(float(self.scale.get()))

    def update_position(self):
        t = self.timeline.times[self.i - 1] if 0 < self.i <= len(self.timeline.times) else 0.0
        if not self._seeking:
            self.scale.set(t)
        self.pos_var.set(f"{fmt_time(t)} / {fmt_time(self.timeline.duration)}")

    def close(self):
        self.pause()
        self.screen.close()


def play_file(app, path=None):
    """Tools > Play a terminal recording."""
    if path is None:
        os.makedirs(recordings_dir(), exist_ok=True)
        path = filedialog.askopenfilename(title="Choose a recording", initialdir=recordings_dir(),
                                          filetypes=[("Terminal recordings", "*.cast"), ("All files", "*.*")])
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            header, events = parse_cast(f.read())
    except (OSError, UnicodeDecodeError) as e:
        messagebox.showerror("Play a recording", f"Could not read that file:\n{e}", parent=app.root)
        return None
    except CastError as e:
        messagebox.showerror("Play a recording", str(e), parent=app.root)
        return None
    if not events:
        messagebox.showinfo("Play a recording", "That recording has no output in it.", parent=app.root)
        return None
    tab = PlayerTab(app, path, header, events)
    app.add_tab(tab, "▶ " + (os.path.basename(path)[:28]))
    return tab
