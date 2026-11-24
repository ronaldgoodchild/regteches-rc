"""REGTeches RC - command suggestions from your history (press Ctrl+Space in a terminal).

RC remembers the commands you type (the Quick panel's History). Ctrl+Space opens a small list under the cursor with the remembered
commands that start with what you have typed on the line, then ones that merely contain it; the ones you use most and most recently
come first. Up / Down choose, Enter or Tab fills the line (it is NOT run: press Enter yourself), Escape closes. If RC cannot tell what
is on the line (you used the arrow keys or Tab to edit it) the list shows your most recent commands instead.
The ranking is pure and tested without a window."""

import time
import tkinter as tk

import rc_quick

MAX_SHOWN = 8


def rank(items, typed, now=None, limit=MAX_SHOWN):
    """items = [{'cmd','count','last'}] from rc_quick.load_store()['items'] -> the best `limit` command strings for `typed`.
    Commands that start with `typed` beat ones that contain it; within each, a mix of how often and how recently it was used."""
    now = time.time() if now is None else now
    typed = (typed or "").strip()
    low = typed.lower()
    scored = []
    for it in items:
        cmd = it["cmd"]
        c = cmd.lower()
        if cmd.strip() == typed and typed:
            continue                                   # nothing to add
        if not low:
            tier = 2
        elif c.startswith(low):
            tier = 0
        elif low in c:
            tier = 1
        else:
            continue
        age_days = max(0.0, (now - it.get("last", 0)) / 86400.0)
        score = it.get("count", 1) / (1.0 + age_days / 7.0)           # frequent and recent, halving roughly every week
        scored.append((tier, -score, cmd))
    scored.sort()
    out, seen = [], set()
    for _t, _s, cmd in scored:
        if cmd not in seen:
            seen.add(cmd)
            out.append(cmd)
        if len(out) >= limit:
            break
    return out


def suggestions_for(typed, limit=MAX_SHOWN, now=None):
    return rank(rc_quick.load_store()["items"], typed, now, limit)


class SuggestPopup(tk.Toplevel):
    """The little list. `accept(command)` is called with the chosen command; the owner decides how to put it on the line."""

    def __init__(self, terminal, commands, x, y, accept):
        super().__init__(terminal)
        self.terminal, self.accept = terminal, accept
        self.wm_overrideredirect(True)
        self.commands = commands
        self.lb = tk.Listbox(self, height=min(len(commands), MAX_SHOWN), width=min(80, max(30, max(len(c) for c in commands) + 2)),
                             activestyle="none", exportselection=False, font=terminal.font, borderwidth=1, relief="solid")
        self.lb.pack()
        for c in commands:
            self.lb.insert("end", c)
        self.lb.selection_set(0)
        self.geometry(f"+{x}+{y}")
        self.lb.bind("<Double-1>", lambda e: self.choose())
        self.lb.bind("<ButtonRelease-1>", lambda e: self.choose())
        self.lift()

    def move(self, delta):
        cur = (self.lb.curselection() or (0,))[0]
        new = max(0, min(len(self.commands) - 1, cur + delta))
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(new)
        self.lb.see(new)

    def choose(self):
        sel = self.lb.curselection()
        cmd = self.commands[sel[0]] if sel else None
        self.close()
        if cmd:
            self.accept(cmd)

    def close(self):
        try:
            self.destroy()
        except tk.TclError:
            pass
