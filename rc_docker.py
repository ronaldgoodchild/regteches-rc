"""REGTeches RC - Docker page of the Stats tab (containers over SSH: list, CPU / memory, start, stop, restart, remove, logs).

Uses the `docker` command on the server through the Stats tab's SSH connection. If the login may not talk to Docker directly
(it is not root and not in the docker group) RC retries with `sudo -n` (passwordless sudo). Container names are checked against
Docker's own naming rules before they go into a command. Parsing and command building are pure and tested without a server; the
page and the log window are at the bottom."""

import json
import re
import tkinter as tk
from tkinter import messagebox, ttk

import rc_monitor as mon

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ACTIONS = {"start": "Start", "stop": "Stop", "restart": "Restart", "pause": "Pause", "unpause": "Unpause", "rm": "Remove"}


class DockerError(ValueError):
    """A readable problem."""


def list_command(use_sudo):
    s = "sudo -n " if use_sudo else ""
    return ("sh -c 'command -v docker >/dev/null 2>&1 || echo @@nodocker; echo @@ps; "
            f"{s}docker ps -a --format \"{{{{json .}}}}\" 2>&1; echo @@stats; "
            f"{s}docker stats --no-stream --format \"{{{{json .}}}}\" 2>/dev/null; echo @@end'")


def action_command(action, name, use_sudo):
    if action not in ACTIONS:
        raise DockerError("Unknown action.")
    if not NAME_RE.match(name or ""):
        raise DockerError("That container name is not valid.")
    return f"{'sudo -n ' if use_sudo else ''}docker {action} {name} 2>&1"


def logs_command(name, use_sudo, lines=300):
    if not NAME_RE.match(name or ""):
        raise DockerError("That container name is not valid.")
    return f"{'sudo -n ' if use_sudo else ''}docker logs --tail {int(lines)} {name} 2>&1"


def needs_sudo(text):
    low = text.lower()
    return "permission denied" in low or "got permission denied" in low


def _json_lines(lines):
    out = []
    for line in lines:
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
    return out


def parse_list(text):
    """-> (state, containers). state: 'ok', 'nodocker', 'denied' (needs sudo) or 'error:<first line>'.
    containers = [{name, id, image, state, status, ports, cpu, mem}] (cpu / mem are '' for containers that are not running)."""
    sec = mon.split_sections(text)
    if "nodocker" in sec or "@@nodocker" in text:
        return "nodocker", []
    ps = sec.get("ps", [])
    joined = "\n".join(ps)
    if needs_sudo(joined):
        return "denied", []
    rows = _json_lines(ps)
    if not rows and joined.strip():
        return "error:" + joined.strip().splitlines()[0][:160], []
    stats = {}
    for d in _json_lines(sec.get("stats", [])):
        for key in (d.get("Name"), d.get("Container"), d.get("ID")):
            if key:
                stats[key] = d
    out = []
    for d in rows:
        name = str(d.get("Names") or d.get("ID") or "?").split(",")[0]
        st = stats.get(name) or stats.get(d.get("ID", "")[:12]) or {}
        out.append({"name": name, "id": str(d.get("ID", ""))[:12], "image": str(d.get("Image", "")), "state": str(d.get("State", "")).lower(),
                    "status": str(d.get("Status", "")), "ports": str(d.get("Ports", "")), "cpu": str(st.get("CPUPerc", "")),
                    "mem": str(st.get("MemUsage", "")).split("/")[0].strip()})
    out.sort(key=lambda c: (c["state"] != "running", c["name"].lower()))
    return "ok", out


class DockerPage(ttk.Frame):
    """Added to the Stats tab's notebook as "Docker"."""

    def __init__(self, mon_tab):
        super().__init__(mon_tab.book, padding=4)
        self.mon = mon_tab
        self.loaded = False
        self.use_sudo = False
        self.items = []
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 4))
        self.info = tk.StringVar(value="Opens when you click this tab.")
        ttk.Label(top, textvariable=self.info, foreground="#555").pack(side="left")
        ttk.Button(top, text="⟳ Refresh", command=self.load).pack(side="right")
        acts = ttk.Frame(self)
        acts.pack(fill="x", pady=(0, 4))
        ttk.Label(acts, text="Selected container:").pack(side="left")
        for action in ("start", "stop", "restart", "rm"):
            ttk.Button(acts, text=ACTIONS[action] + ("…" if action == "rm" else ""), command=lambda a=action: self.act(a)).pack(side="left", padx=(6, 0))
        ttk.Button(acts, text="Logs…", command=self.logs).pack(side="left", padx=(14, 0))
        cols = [("name", "Container"), ("state", "State"), ("status", "Status"), ("cpu", "CPU"), ("mem", "Memory"), ("image", "Image"), ("ports", "Ports")]
        self.tree = ttk.Treeview(self, columns=[c for c, _ in cols], show="headings", selectmode="browse")
        for (c, t), w in zip(cols, (180, 80, 160, 60, 90, 240, 280)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="e" if c in ("cpu", "mem") else "w", stretch=(c == "ports"))
        self.tree.tag_configure("down", foreground="#b00020")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.logs())

    def load(self, retry=True):
        self.info.set("Loading ...")

        def done(code, out, err, error):
            self.loaded = True
            if error:
                self.info.set(f"Could not read Docker: {error}")
                return
            state, items = parse_list(out + err)
            if state == "denied" and retry and not self.use_sudo:
                self.use_sudo = True
                self.load(retry=False)
                return
            if state == "nodocker":
                self.info.set("Docker is not installed on this server.")
            elif state == "denied":
                self.info.set("This login cannot use Docker (not root, not in the docker group, and no passwordless sudo).")
            elif state.startswith("error:"):
                self.info.set("Docker said: " + state[6:])
            else:
                self.show(items)
        self.mon.run_async(list_command(self.use_sudo), done, timeout=40)

    def show(self, items):
        self.items = items
        self.tree.delete(*self.tree.get_children())
        for c in items:
            self.tree.insert("", "end", iid=c["name"], values=(c["name"], c["state"], c["status"], c["cpu"], c["mem"], c["image"], c["ports"]),
                             tags=("down",) if c["state"] in ("exited", "dead") and "(0)" not in c["status"] else ())
        running = sum(1 for c in items if c["state"] == "running")
        self.info.set(f"{len(items)} container(s), {running} running" + ("  ·  via sudo" if self.use_sudo else ""))

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def act(self, action):
        name = self.selected()
        if not name:
            messagebox.showinfo("Docker", "Click a container in the list first.", parent=self)
            return
        extra = "\n\nThis deletes the container (its volumes are kept). It must be stopped first." if action == "rm" else ""
        if action in ("stop", "restart", "rm") and not messagebox.askyesno(
                "Docker", f"{ACTIONS[action]} “{name}” on {self.mon.host['name']}?{extra}", parent=self, icon="warning" if action == "rm" else "question"):
            return
        try:
            cmd = action_command(action, name, self.use_sudo)
        except DockerError as e:
            messagebox.showwarning("Docker", str(e), parent=self)
            return
        self.info.set(f"{ACTIONS[action]} {name} ...")

        def done(code, out, err, error):
            text = (error and str(error)) or (out + err).strip()
            if error or code not in (0, None):
                messagebox.showwarning("Docker", f"{ACTIONS[action]} {name} failed:\n\n{text or 'no details'}", parent=self)
            self.load()
        self.mon.run_async(cmd, done)

    def logs(self):
        name = self.selected()
        if name:
            DockerLogs(self, name)


class DockerLogs(tk.Toplevel):
    def __init__(self, page, name):
        super().__init__(page)
        self.page, self.name = page, name
        self.title(f"Logs · {name} · {page.mon.host['name']}")
        self.geometry("900x520")
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="⟳ Refresh", command=self.load).pack(side="left")
        self.status = tk.StringVar()
        ttk.Label(bar, textvariable=self.status, foreground="#555").pack(side="left", padx=8)
        self.text = tk.Text(self, wrap="none", font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.load()

    def load(self):
        self.status.set("Loading ...")

        def done(code, out, err, error):
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", (str(error) if error else (out + err)) or "(no output)")
            self.text.configure(state="disabled")
            self.text.see("end")
            self.status.set("Last 300 lines")
        try:
            cmd = logs_command(self.name, self.page.use_sudo)
        except DockerError as e:
            done(None, str(e), "", None)
            return
        self.page.mon.run_async(cmd, done, timeout=40)
