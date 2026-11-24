"""REGTeches RC - live server stats ("Stats" button): CPU, memory, disks, network, processes and listening ports.

How it works: one SSH connection stays open and, every few seconds, runs ONE read-only shell script that prints
the contents of /proc, `df`, `ps` and `ss` in labelled sections. The parsing and the maths (CPU %, network speed)
live in plain functions at the top of this file so they can be tested without a server. Nothing is installed on or
changed on the server. It needs a Linux/Unix-style server (NAS boxes, routers with busybox and Raspberry Pi all
work, to the extent they have /proc); a Windows SSH server answers "not supported".

Threading: a worker thread does the SSH and parsing; results come back to the window through app.post()."""

import re
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox, ttk

import rc_manager as core

log = core.log

# One script, no single quotes in it (it is wrapped in sh -c '...'). Every command is read-only.
SCRIPT = (
    "echo @@os; uname -sr 2>/dev/null; hostname 2>/dev/null; grep PRETTY_NAME /etc/os-release 2>/dev/null; "
    "nproc 2>/dev/null; "
    "echo @@up; cat /proc/uptime /proc/loadavg 2>/dev/null; "
    "echo @@stat; grep ^cpu /proc/stat 2>/dev/null; "
    "echo @@mem; cat /proc/meminfo 2>/dev/null; "
    "echo @@disk; df -kP 2>/dev/null; "
    "echo @@net; cat /proc/net/dev 2>/dev/null; "
    "echo @@ps; "
    "P=$(ps -eo pid,user,pcpu,pmem,rss,etime,args --sort=-pcpu 2>/dev/null) || "
    "P=$(ps -eo pid,user,pcpu,pmem,rss,etime,args 2>/dev/null); echo \"$P\" | head -n 301; "
    "echo @@ports; (ss -tulnp 2>/dev/null || ss -tuln 2>/dev/null || netstat -tuln 2>/dev/null); "
    "echo @@end"
)
COMMAND = "sh -c '" + SCRIPT + "'"

PSEUDO_FS = {"tmpfs", "devtmpfs", "udev", "overlay", "shm", "none", "efivarfs", "squashfs", "cgroup", "cgroup2"}
PSEUDO_MOUNT = ("/sys", "/proc", "/run", "/dev", "/snap")
HISTORY = 90          # samples kept for the graphs
INTERVALS = (2, 5, 10, 30, 60)


def retry_delay(interval, failures):
    """Seconds to wait before the next try: the normal refresh interval while all is well, then doubling (up to 60 s) after
    each failure, so a server that is refusing connections (busy, or rate-limiting new logins) is not hammered every few seconds."""
    return float(interval) if failures <= 0 else float(min(60, max(interval, 1) * 2 ** min(failures, 5)))


class Unsupported(Exception):
    """The server did not answer like a Linux/Unix machine with /proc."""


# ─── parsing (pure functions) ────────────────────────────────────────────────

def split_sections(text):
    """'@@name' marker lines split the script output into {name: [lines]}."""
    out, cur = {}, None
    for line in text.replace("\r", "").split("\n"):
        if line.startswith("@@"):
            cur = line[2:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


def _int(s, default=0):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def parse_os(lines):
    lines = [l.strip() for l in lines if l.strip()]
    info = {"kernel": "", "hostname": "", "pretty": "", "cores": 0}
    if lines:
        info["kernel"] = lines[0]
    if len(lines) > 1:
        info["hostname"] = lines[1]
    for l in lines[2:]:
        if l.startswith("PRETTY_NAME="):
            info["pretty"] = l.split("=", 1)[1].strip().strip('"')
        elif l.isdigit():
            info["cores"] = int(l)
    return info


def parse_uptime(lines):
    """(uptime seconds, (load1, load5, load15))"""
    up, load = 0.0, (0.0, 0.0, 0.0)
    nums = [l.split() for l in lines if l.strip()]
    if nums and nums[0]:
        try:
            up = float(nums[0][0])
        except ValueError:
            pass
    if len(nums) > 1 and len(nums[1]) >= 3:
        try:
            load = tuple(float(x) for x in nums[1][:3])
        except ValueError:
            pass
    return up, load


def parse_cpu(lines):
    """{'cpu': (busy_total, idle_total), 'cpu0': ...}: raw jiffy counters, turned into % by cpu_percent()."""
    out = {}
    for l in lines:
        p = l.split()
        if not p or not p[0].startswith("cpu") or len(p) < 5:
            continue
        v = [_int(x) for x in p[1:9]] + [0] * 8
        idle = v[3] + v[4]                     # idle + iowait
        out[p[0]] = (sum(v[:8]), idle)
    return out


def cpu_percent(prev, cur):
    """Busy % between two parse_cpu() results, per counter name (None if there is no earlier sample)."""
    out = {}
    for name, (tot, idle) in cur.items():
        if name not in prev:
            continue
        dt, di = tot - prev[name][0], idle - prev[name][1]
        out[name] = max(0.0, min(100.0, 100.0 * (dt - di) / dt)) if dt > 0 else 0.0
    return out


def parse_mem(lines):
    """Sizes in bytes: total, used, available, swap_total, swap_used."""
    kb = {}
    for l in lines:
        m = re.match(r"(\w+):\s+(\d+)", l)
        if m:
            kb[m.group(1)] = int(m.group(2))
    total = kb.get("MemTotal", 0)
    avail = kb.get("MemAvailable", kb.get("MemFree", 0) + kb.get("Buffers", 0) + kb.get("Cached", 0))
    st, sf = kb.get("SwapTotal", 0), kb.get("SwapFree", 0)
    return {"total": total * 1024, "used": max(0, total - avail) * 1024, "available": avail * 1024,
            "swap_total": st * 1024, "swap_used": max(0, st - sf) * 1024}


def parse_disks(lines):
    out = []
    for l in lines[1:]:                         # first line is the header
        p = l.split()
        if len(p) < 6 or not p[1].isdigit():
            continue
        fs, mount = p[0], " ".join(p[5:])
        if fs in PSEUDO_FS or fs.startswith("/dev/loop") or mount.startswith(PSEUDO_MOUNT):
            continue
        size, used, avail = int(p[1]) * 1024, int(p[2]) * 1024, int(p[3]) * 1024
        pct = _int(p[4].rstrip("%"), round(100 * used / size) if size else 0)
        out.append({"mount": mount, "fs": fs, "size": size, "used": used, "avail": avail, "pct": pct})
    return sorted(out, key=lambda d: d["mount"])


def parse_net(lines):
    """{interface: (rx_bytes, tx_bytes)} (loopback left out)."""
    out = {}
    for l in lines:
        if ":" not in l:
            continue
        name, _, rest = l.partition(":")
        name, f = name.strip(), rest.split()
        if name == "lo" or len(f) < 9 or not f[0].isdigit():
            continue
        out[name] = (int(f[0]), int(f[8]))
    return out


def net_rates(prev, cur, dt):
    """{interface: (rx_bytes_per_s, tx_bytes_per_s)}; a counter that went backwards (reboot) counts as 0."""
    out = {}
    if dt <= 0:
        return out
    for name, (rx, tx) in cur.items():
        if name in prev:
            out[name] = (max(0, rx - prev[name][0]) / dt, max(0, tx - prev[name][1]) / dt)
    return out


def etime_seconds(s):
    """ps elapsed time ([[dd-]hh:]mm:ss) -> seconds."""
    try:
        days = 0
        if "-" in s:
            d, s = s.split("-", 1)
            days = int(d)
        parts = [int(x) for x in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return 0


def parse_ps(lines):
    out = []
    for l in lines[1:]:
        p = l.split(None, 6)
        if len(p) < 7 or not p[0].isdigit():
            continue
        out.append({"pid": int(p[0]), "user": p[1], "cpu": float(p[2]) if re.match(r"[\d.]+$", p[2]) else 0.0,
                    "mem": float(p[3]) if re.match(r"[\d.]+$", p[3]) else 0.0, "rss": _int(p[4]) * 1024,
                    "etime": p[5], "cmd": p[6].strip()})
    return out


def parse_ports(lines):
    """Listening TCP/UDP sockets from `ss -tulnp`, `ss -tuln` or `netstat -tuln`."""
    out, seen = [], set()
    for l in lines:
        p = l.split()
        if len(p) < 5 or p[0] not in ("tcp", "tcp6", "udp", "udp6"):
            continue
        local = p[4] if p[1].isalpha() and p[1] in ("LISTEN", "UNCONN", "ESTAB", "UNREPL") else p[3]
        addr, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        m = re.search(r'\(\("([^"]+)"', l)
        key = (p[0], addr, int(port))
        if key in seen:
            continue
        seen.add(key)
        out.append({"proto": p[0], "addr": addr.strip("[]") or "*", "port": int(port), "proc": m.group(1) if m else ""})
    return sorted(out, key=lambda r: (r["port"], r["proto"], r["addr"]))


def parse_sample(text, now=None):
    """Turn the script's output into a sample dict. Raises Unsupported if it was not a /proc machine."""
    sec = split_sections(text)
    cpu, mem = parse_cpu(sec.get("stat", [])), parse_mem(sec.get("mem", []))
    if "end" not in sec and not cpu:
        raise Unsupported("The server did not run the check script (is it a Windows server?).")
    if not cpu or not mem["total"]:
        raise Unsupported("This server has no /proc statistics (it is not a Linux machine).")
    up, load = parse_uptime(sec.get("up", []))
    return {"t": now if now is not None else time.time(), "os": parse_os(sec.get("os", [])), "uptime": up, "load": load,
            "cpu": cpu, "mem": mem, "disks": parse_disks(sec.get("disk", [])), "net": parse_net(sec.get("net", [])),
            "procs": parse_ps(sec.get("ps", [])), "ports": parse_ports(sec.get("ports", []))}


class Tracker:
    """Keeps the previous sample and the graph history; update() returns what the screen needs."""

    def __init__(self):
        self.prev = None
        self.cpu_hist, self.rx_hist, self.tx_hist = (deque(maxlen=HISTORY) for _ in range(3))

    def update(self, s):
        view = {"sample": s, "cpu": {}, "net": {}, "rx": 0.0, "tx": 0.0}
        # Glances has already worked the percentages and speeds out (cpu_pct / net_rates); the SSH collector gives raw counters
        if "cpu_pct" in s:
            view["cpu"] = dict(s["cpu_pct"])
        elif self.prev:
            view["cpu"] = cpu_percent(self.prev["cpu"], s["cpu"])
        if s.get("net_rates"):
            view["net"] = dict(s["net_rates"])
        elif self.prev:
            view["net"] = net_rates(self.prev["net"], s["net"], s["t"] - self.prev["t"])
        if self.prev or "cpu_pct" in s:
            view["rx"] = sum(r for r, _ in view["net"].values())
            view["tx"] = sum(t for _, t in view["net"].values())
            self.cpu_hist.append(view["cpu"].get("cpu", 0.0))
            self.rx_hist.append(view["rx"])
            self.tx_hist.append(view["tx"])
        self.prev = s
        return view


# ─── on-demand commands: process detail, signals, services, system info ─────

SIGNALS = (("TERM", "15 - ask it to stop (default)"), ("HUP", "1 - reload its configuration"),
           ("INT", "2 - interrupt (like Ctrl+C)"), ("KILL", "9 - force kill (cannot be caught)"))
UNIT_RE = re.compile(r"^[A-Za-z0-9@:._\\-]+$")
CRITICAL_UNITS = ("ssh", "sshd", "networking", "network", "networkmanager", "systemd-networkd", "dbus", "systemd-logind")


def detail_command(pid):
    pid = int(pid)
    return ("sh -c 'P=/proc/%d; echo @@cmd; tr \"\\0\" \" \" < $P/cmdline 2>/dev/null; echo; echo @@cwd; readlink $P/cwd 2>/dev/null; "
            "echo @@exe; readlink $P/exe 2>/dev/null; echo @@status; cat $P/status 2>/dev/null; "
            "echo @@fds; ls $P/fd 2>/dev/null | wc -l; echo @@io; cat $P/io 2>/dev/null; echo @@end'" % pid)


def parse_proc_detail(text):
    """Details of one process from /proc/PID. Raises LookupError if it has gone away."""
    sec = split_sections(text)
    status = {}
    for l in sec.get("status", []):
        k, _, v = l.partition(":")
        if v:
            status[k.strip()] = v.strip()
    if not status:
        raise LookupError("That process has ended (or its details cannot be read with this login).")
    io = {}
    for l in sec.get("io", []):
        k, _, v = l.partition(":")
        if v.strip().isdigit():
            io[k.strip()] = int(v.strip())
    fds = _int((sec.get("fds") or ["0"])[0].strip())
    one = lambda name: ((sec.get(name) or [""])[0]).strip()
    return {"cmdline": one("cmd"), "cwd": one("cwd"), "exe": one("exe"), "status": status, "fds": fds, "io": io}


def format_proc_detail(pid, d):
    st = d["status"]
    kb = lambda k: human_bytes(_int(st.get(k, "0 kB").split()[0]) * 1024) if k in st else "-"
    lines = [f"PID {pid}   {st.get('Name', '')}   state: {st.get('State', '?')}   parent PID: {st.get('PPid', '?')}   threads: {st.get('Threads', '?')}",
             "", f"Command line:   {d['cmdline'] or '(none - kernel thread or not readable)'}",
             f"Program:        {d['exe'] or '(not readable)'}", f"Working folder: {d['cwd'] or '(not readable)'}", "",
             f"Memory:  in RAM {kb('VmRSS')}   virtual {kb('VmSize')}   peak {kb('VmPeak')}   data {kb('VmData')}   stack {kb('VmStk')}",
             f"Open files/sockets: {d['fds']}",
             f"Context switches:   {st.get('voluntary_ctxt_switches', '-')} voluntary, {st.get('nonvoluntary_ctxt_switches', '-')} involuntary"]
    if d["io"]:
        lines.append(f"Disk I/O:  read {human_bytes(d['io'].get('read_bytes', 0))}   written {human_bytes(d['io'].get('write_bytes', 0))}"
                     f"   (characters: {human_bytes(d['io'].get('rchar', 0))} in, {human_bytes(d['io'].get('wchar', 0))} out)")
    return "\n".join(lines)


def kill_command(sig, pid, sudo=False):
    if sig not in dict(SIGNALS):
        raise ValueError("unknown signal")
    return f"{'sudo -n ' if sudo else ''}kill -{sig} {int(pid)} 2>&1"


SERVICES_COMMAND = ("sh -c 'echo @@uid; id -u; echo @@has; command -v systemctl; echo @@units; "
                    "systemctl list-units --type=service --all --no-pager --no-legend --plain 2>/dev/null; echo @@files; "
                    "systemctl list-unit-files --type=service --no-pager --no-legend 2>/dev/null; echo @@end'")


def parse_services(text):
    """(is_root, services). Raises Unsupported if the server does not use systemd."""
    sec = split_sections(text)
    if not [l for l in sec.get("has", []) if l.strip()]:
        raise Unsupported("This server does not use systemd, so there is no service list.")
    startup = {}
    for l in sec.get("files", []):
        p = l.split()
        if len(p) >= 2 and p[0].endswith(".service"):
            startup[p[0]] = p[1]
    out = []
    for l in sec.get("units", []):
        p = l.replace("●", " ").replace("*", " ").split(None, 4)
        if len(p) < 4 or not p[0].endswith(".service") or p[1] == "not-found":
            continue
        out.append({"unit": p[0], "load": p[1], "active": p[2], "sub": p[3], "desc": p[4].strip() if len(p) > 4 else "",
                    "startup": startup.get(p[0], "static" if p[1] == "loaded" else "-")})
    uid = (sec.get("uid") or ["1"])[0].strip()
    return uid == "0", sorted(out, key=lambda s: s["unit"])


SERVICE_ACTIONS = {"start": "Start", "stop": "Stop", "restart": "Restart", "enable": "Enable at boot", "disable": "Disable at boot"}


def service_command(action, unit, sudo=False):
    if action not in SERVICE_ACTIONS or not UNIT_RE.match(unit):
        raise ValueError("bad service action")
    return f"{'sudo -n ' if sudo else ''}systemctl {action} {unit} 2>&1"


def journal_command(unit, lines=200):
    if not UNIT_RE.match(unit):
        raise ValueError("bad unit name")
    return f"journalctl -u {unit} -n {int(lines)} --no-pager 2>&1"


INFO_COMMAND = (
    "sh -c 'echo @@os; uname -srm 2>/dev/null; grep PRETTY_NAME /etc/os-release 2>/dev/null; hostname 2>/dev/null; whoami 2>/dev/null; "
    "echo @@cpu; grep -m1 \"model name\" /proc/cpuinfo 2>/dev/null; grep -m1 \"cpu MHz\" /proc/cpuinfo 2>/dev/null; nproc 2>/dev/null; "
    "echo @@time; date +%s; date +%Z; cat /etc/timezone 2>/dev/null; "
    "echo @@ip; ip -o -4 addr show 2>/dev/null; ip -o -6 addr show scope global 2>/dev/null; "
    "echo @@ifaces; for i in /sys/class/net/*; do n=${i##*/}; echo \"$n|$(cat $i/operstate 2>/dev/null)|$(cat $i/address 2>/dev/null)|$(cat $i/speed 2>/dev/null)\"; done 2>/dev/null; "
    "echo @@blk; lsblk -P -o NAME,TYPE,SIZE,MOUNTPOINT,FSTYPE,MODEL,ROTA 2>/dev/null; echo @@end'")


def parse_info(text, local_before=None, local_after=None):
    """Static facts about the server. Clock skew = server clock minus this PC's clock (positive: server is ahead)."""
    sec = split_sections(text)
    osl = [l.strip() for l in sec.get("os", []) if l.strip()]
    info = {"kernel": osl[0] if osl else "", "pretty": "", "hostname": "", "user": ""}
    rest = []
    for l in osl[1:]:
        if l.startswith("PRETTY_NAME="):
            info["pretty"] = l.split("=", 1)[1].strip('"')
        else:
            rest.append(l)
    info["hostname"] = rest[0] if rest else ""
    info["user"] = rest[1] if len(rest) > 1 else ""
    cpu = {"model": "", "mhz": "", "cores": ""}
    for l in sec.get("cpu", []):
        if l.startswith("model name"):
            cpu["model"] = l.partition(":")[2].strip()
        elif l.startswith("cpu MHz"):
            cpu["mhz"] = l.partition(":")[2].strip()
        elif l.strip().isdigit():
            cpu["cores"] = l.strip()
    info["cpu"] = cpu
    t = [l.strip() for l in sec.get("time", []) if l.strip()]
    info["tz"] = " ".join(t[1:3]) if len(t) > 1 else ""
    info["skew"] = None
    if t and t[0].isdigit() and local_before is not None and local_after is not None:
        info["skew"] = int(t[0]) - (local_before + local_after) / 2.0
    addrs = {}
    for l in sec.get("ip", []):
        p = l.split()
        if len(p) >= 4 and p[2] in ("inet", "inet6"):
            addrs.setdefault(p[1], []).append(p[3])
    info["addrs"] = addrs
    ifaces = []
    for l in sec.get("ifaces", []):
        p = l.strip().split("|")
        if len(p) == 4 and p[0] != "lo":
            speed = p[3] if p[3].lstrip("-").isdigit() and int(p[3]) > 0 else ""
            ifaces.append({"name": p[0], "state": p[1] or "?", "mac": p[2], "speed": speed, "addrs": addrs.get(p[0], [])})
    info["ifaces"] = ifaces
    blk = []
    for l in sec.get("blk", []):
        d = dict(re.findall(r'(\w+)="([^"]*)"', l))
        if d.get("NAME"):
            blk.append(d)
    info["blk"] = blk
    return info


def format_info(i):
    skew = i["skew"]
    skew_txt = "unknown" if skew is None else (f"{abs(skew):.1f} s " + ("ahead of" if skew > 0 else "behind") + " this PC"
                                               + ("   <-- check NTP!" if abs(skew) > 60 else ""))
    out = ["SYSTEM", f"  Name:      {i['hostname']}", f"  System:    {i['pretty'] or i['kernel']}", f"  Kernel:    {i['kernel']}",
           f"  Logged in: {i['user']}", "",
           "CPU", f"  Model:     {i['cpu']['model'] or '?'}", f"  Cores:     {i['cpu']['cores'] or '?'}"
           + (f"   at {float(i['cpu']['mhz']):.0f} MHz" if i["cpu"]["mhz"].replace('.', '', 1).isdigit() else ""), "",
           "CLOCK", f"  Time zone: {i['tz'] or '?'}", f"  Clock:     {skew_txt}", "", "NETWORK INTERFACES"]
    for n in i["ifaces"]:
        out.append(f"  {n['name']:<12} {n['state']:<8} {n['mac']:<18} {(n['speed'] + ' Mbps') if n['speed'] else '':<10} {', '.join(n['addrs'])}")
    if not i["ifaces"]:
        out.append("  (none reported)")
    out += ["", "DISKS AND PARTITIONS"]
    for d in i["blk"]:
        kind = d.get("TYPE", "")
        media = (" SSD" if d.get("ROTA") == "0" else " HDD") if kind == "disk" and d.get("ROTA") in ("0", "1") else ""
        out.append(f"  {d['NAME']:<14} {kind + media:<10} {d.get('SIZE', ''):>8}  {d.get('FSTYPE', ''):<8} {d.get('MOUNTPOINT', ''):<18} {d.get('MODEL', '').strip()}")
    if not i["blk"]:
        out.append("  (lsblk is not available on this server)")
    return "\n".join(out)


# ─── formatting ──────────────────────────────────────────────────────────────

def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_rate(n):
    return human_bytes(n) + "/s"


def human_duration(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"


# ─── the tab ─────────────────────────────────────────────────────────────────

GRAPH_BG, GRAPH_GRID = "#1e2127", "#2f343c"
CPU_COLOR, RX_COLOR, TX_COLOR = "#56b6c2", "#98c379", "#e5c07b"


class Graph(tk.Canvas):
    """A small line graph of the last samples. Fixed 0-100 scale, or auto-scaled."""

    def __init__(self, parent, series, fixed_max=None, height=70):
        super().__init__(parent, height=height, bg=GRAPH_BG, highlightthickness=0, borderwidth=0)
        self._rc_fixed = True
        self.series, self.fixed_max = series, fixed_max   # [(deque, colour)]
        self.bind("<Configure>", lambda e: self.redraw())

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        for frac in (0.25, 0.5, 0.75):
            self.create_line(0, h * frac, w, h * frac, fill=GRAPH_GRID)
        top = self.fixed_max or max([1.0] + [v for hist, _ in self.series for v in hist]) * 1.15
        for hist, colour in self.series:
            pts = list(hist)
            if len(pts) < 2:
                continue
            step = w / (HISTORY - 1)
            x0 = w - step * (len(pts) - 1)
            coords = []
            for i, v in enumerate(pts):
                coords += [x0 + i * step, h - 2 - (h - 4) * min(v, top) / top]
            self.create_line(*coords, fill=colour, width=2)


class MonitorTab(ttk.Frame):
    AUTO_START = True     # tests switch this off
    DEFAULT_INTERVAL = 5

    def __init__(self, app, host, user, password):
        super().__init__(app.nb)
        self.app, self.host, self.creds = app, host, (user, password)
        self.glances = bool(host.get("glances"))       # read the stats from Glances' REST API instead of over SSH
        self.closed = False
        self.client = None
        self.generation = 0
        self.stop_evt, self.now_evt = threading.Event(), threading.Event()
        self.interval = tk.IntVar(value=self.DEFAULT_INTERVAL)
        self.paused = tk.BooleanVar(value=False)
        self.interval_s, self.paused_flag = self.DEFAULT_INTERVAL, False      # plain copies: the worker thread must not touch Tk variables
        self.interval.trace_add("write", lambda *_: self.sync_settings())
        self.tracker = Tracker()
        self.last = None
        self.proc_sort, self.proc_rev = "cpu", True
        self.bars_w = {}

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(bar, text=f"📊  {host['name']}", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Label(bar, text=f"  {host['address']}", foreground="#555").pack(side="left")
        self.status_var = tk.StringVar(value="Connecting ...")
        ttk.Label(bar, textvariable=self.status_var).pack(side="right", padx=6)
        ttk.Button(bar, text="⟳ Now", command=self.refresh_now).pack(side="right", padx=2)
        ttk.Checkbutton(bar, text="Pause", variable=self.paused, command=self.sync_settings).pack(side="right", padx=6)
        ttk.Label(bar, text="s").pack(side="right", padx=(0, 4))
        iv = ttk.Combobox(bar, width=4, state="readonly", values=[str(i) for i in INTERVALS], textvariable=self.interval)
        iv.pack(side="right")
        ttk.Label(bar, text="Refresh every").pack(side="right", padx=(0, 4))

        tiles = ttk.Frame(self)
        tiles.pack(fill="x", padx=6, pady=4)
        for c in range(4):
            tiles.columnconfigure(c, weight=1, uniform="tile")
        # CPU
        self.cpu_big, self.cpu_sub = tk.StringVar(value="-"), tk.StringVar()
        cpu = self.tile(tiles, 0, "CPU", self.cpu_big, self.cpu_sub)
        self.cpu_graph = Graph(cpu, [(self.tracker.cpu_hist, CPU_COLOR)], fixed_max=100)
        self.cpu_graph.pack(fill="x", pady=(4, 0))
        # memory
        self.mem_big, self.mem_sub = tk.StringVar(value="-"), tk.StringVar()
        mem = self.tile(tiles, 1, "Memory", self.mem_big, self.mem_sub)
        self.mem_bar = ttk.Progressbar(mem, maximum=100)
        self.mem_bar.pack(fill="x", pady=(10, 2))
        self.swap_bar = ttk.Progressbar(mem, maximum=100)
        self.swap_bar.pack(fill="x", pady=(2, 2))
        # network
        self.net_big, self.net_sub = tk.StringVar(value="-"), tk.StringVar()
        net = self.tile(tiles, 2, "Network (all interfaces)", self.net_big, self.net_sub)
        self.net_graph = Graph(net, [(self.tracker.rx_hist, RX_COLOR), (self.tracker.tx_hist, TX_COLOR)])
        self.net_graph.pack(fill="x", pady=(4, 0))
        # system
        self.sys_big, self.sys_sub = tk.StringVar(value="-"), tk.StringVar()
        self.tile(tiles, 3, "System", self.sys_big, self.sys_sub, big_size=12)

        self.book = ttk.Notebook(self)
        self.book.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.build_processes()
        self.build_disks()
        self.build_network()
        self.build_ports()
        self.is_root = None
        self.services, self.services_loaded, self.info_loaded = [], False, False
        self.build_services()
        self.build_system()
        if self.glances:
            # Glances is read-only: no listening ports or services from it, but it knows about containers
            self.book.forget(self.page_services)
            self.book.forget(self.page_ports)
            self.build_containers()
        else:
            import rc_capture
            import rc_firewall
            self.page_firewall = rc_firewall.FirewallPage(self)
            self.book.add(self.page_firewall, text="  Firewall  ")
            self.page_capture = rc_capture.CapturePage(self)
            self.book.add(self.page_capture, text="  Capture  ")
            import rc_hardware
            self.page_hardware = rc_hardware.HardwarePage(self)
            self.book.add(self.page_hardware, text="  Hardware  ")
            self.page_ipmi = rc_hardware.IpmiPage(self)
            self.book.add(self.page_ipmi, text="  IPMI  ")
            import rc_docker
            self.page_docker = rc_docker.DockerPage(self)
            self.book.insert(self.page_firewall, self.page_docker, text="  Docker  ")
        self.book.bind("<<NotebookTabChanged>>", self.tab_changed)

        self.thread = threading.Thread(target=self.worker_glances if self.glances else self.worker, args=(self.generation,), daemon=True,
                                       name="stats-" + host["name"])
        if self.AUTO_START:
            self.thread.start()

    def tile(self, parent, col, title, big, sub, big_size=20):
        f = ttk.LabelFrame(parent, text=title, padding=8)
        f.grid(row=0, column=col, sticky="nsew", padx=3)
        ttk.Label(f, textvariable=big, font=("Segoe UI", big_size, "bold")).pack(anchor="w")
        ttk.Label(f, textvariable=sub, foreground="#555", wraplength=260, justify="left").pack(anchor="w")
        return f

    def make_tree(self, parent, cols, widths, anchors=None):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for (cid, label), w in zip(cols, widths):
            tree.heading(cid, text=label)
            tree.column(cid, width=w, anchor=(anchors or {}).get(cid, "w"), stretch=(cid in ("cmd", "mount", "name")))
        sb = ttk.Scrollbar(wrap, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        for tag, colour in (("warn", "#b45309"), ("crit", "#b3261e")):
            tree.tag_configure(tag, foreground=colour)
        return tree

    def build_processes(self):
        f = ttk.Frame(self.book, padding=4)
        self.book.add(f, text="  Processes  ")
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(0, 4))
        ttk.Label(top, text="Filter").pack(side="left")
        self.proc_filter = tk.StringVar()
        self.proc_filter.trace_add("write", lambda *_: self.show_processes())
        ttk.Entry(top, textvariable=self.proc_filter, width=24).pack(side="left", padx=4)
        self.proc_info = tk.StringVar()
        ttk.Label(top, textvariable=self.proc_info, foreground="#555").pack(side="left", padx=8)
        ttk.Button(top, text="Details / send signal…", command=self.open_process).pack(side="right")
        cols = [("pid", "PID"), ("user", "User"), ("cpu", "CPU %"), ("mem", "Mem %"), ("rss", "Memory"),
                ("etime", "Running"), ("cmd", "Command")]
        self.procs = self.make_tree(f, cols, (70, 100, 70, 70, 90, 100, 500),
                                    {"pid": "e", "cpu": "e", "mem": "e", "rss": "e", "etime": "e"})
        for cid, label in cols:
            self.procs.heading(cid, text=label, command=lambda c=cid: self.sort_procs(c))
        self.procs.bind("<Double-1>", lambda e: self.open_process())

    def build_disks(self):
        f = ttk.Frame(self.book, padding=4)
        self.book.add(f, text="  Disks  ")
        cols = [("mount", "Mounted on"), ("fs", "Device"), ("size", "Size"), ("used", "Used"), ("avail", "Free"), ("pct", "Use %")]
        self.disks = self.make_tree(f, cols, (260, 220, 90, 90, 90, 80), {"size": "e", "used": "e", "avail": "e", "pct": "e"})

    def build_network(self):
        f = ttk.Frame(self.book, padding=4)
        self.book.add(f, text="  Network  ")
        cols = [("name", "Interface"), ("rx", "Download"), ("tx", "Upload"), ("rxt", "Received (total)"), ("txt", "Sent (total)")]
        self.nets = self.make_tree(f, cols, (200, 120, 120, 140, 140), {"rx": "e", "tx": "e", "rxt": "e", "txt": "e"})

    def build_ports(self):
        f = self.page_ports = ttk.Frame(self.book, padding=4)
        self.book.add(f, text="  Listening ports  ")
        cols = [("port", "Port"), ("proto", "Protocol"), ("addr", "Listening on"), ("proc", "Program")]
        self.ports = self.make_tree(f, cols, (80, 90, 240, 240), {"port": "e"})
        ttk.Label(f, foreground="#555", text="Program names only show when the saved login is root (ss needs it).").pack(anchor="w", pady=(4, 0))

    # ── worker thread ──
    def refresh_now(self):
        self.now_evt.set()

    def sync_settings(self):
        try:
            self.interval_s = max(1, int(self.interval.get()))
        except (tk.TclError, ValueError):
            pass
        self.paused_flag = bool(self.paused.get())
        self.now_evt.set()

    def post(self, fn):
        if not self.closed:
            self.app.post(lambda: (not self.closed) and fn())

    def worker(self, gen):
        import rc_tools
        failures = 0
        while not self.stop_evt.is_set() and gen == self.generation:
            if not self.paused_flag:
                try:
                    if self.client is None:
                        self.post(lambda: self.status_var.set("Connecting ..."))
                        self.client = core.open_ssh_client(self.app, self.host, *self.creds)
                    code, out, err = rc_tools.run_remote(self.client, COMMAND, timeout=25)
                    sample = parse_sample(out)
                    failures = 0
                    self.post(lambda s=sample: self.apply(s))
                except Unsupported as e:
                    self.post(lambda m=str(e): self.fail(m, fatal=True))
                    self.drop_client()
                    return
                except Exception as e:
                    failures += 1
                    log.warning("Stats %s: %s", self.host.get("name"), e)
                    self.drop_client()
                    msg = f"Connection problem, retrying in {retry_delay(self.interval_s, failures):.0f} s ... ({e})"
                    self.post(lambda m=msg: self.status_var.set(m[:160]))
            self.now_evt.wait(timeout=1 if self.paused_flag else retry_delay(self.interval_s, failures))
            self.now_evt.clear()
        self.drop_client()

    def worker_glances(self, gen):
        import rc_glances
        version, failures = None, 0
        while not self.stop_evt.is_set() and gen == self.generation:
            if not self.paused_flag:
                try:
                    if version is None:
                        self.post(lambda: self.status_var.set("Connecting to Glances ..."))
                        version, _desc = rc_glances.detect(self.host)
                    sample = rc_glances.sample_from_all(rc_glances.fetch_all(self.host, version))
                    failures = 0
                    self.post(lambda s=sample: self.apply(s))
                except Exception as e:
                    failures += 1
                    version = None
                    log.warning("Stats (Glances) %s: %s", self.host.get("name"), e)
                    msg = f"Glances problem, retrying in {retry_delay(self.interval_s, failures):.0f} s ... ({e})"
                    self.post(lambda m=msg: self.status_var.set(m[:160]))
            self.now_evt.wait(timeout=1 if self.paused_flag else retry_delay(self.interval_s, failures))
            self.now_evt.clear()

    def drop_client(self):
        c, self.client = self.client, None
        try:
            c and c.close()
        except Exception:
            pass

    def fail(self, msg, fatal=False):
        self.status_var.set(msg)
        if fatal:
            self.cpu_big.set("n/a")
            self.sys_sub.set(msg)

    # ── drawing (UI thread) ──
    def apply(self, sample):
        view = self.tracker.update(sample)
        self.last = view
        mem, osi = sample["mem"], sample["os"]
        cpu_all = view["cpu"].get("cpu")
        cores = [v for k, v in sorted(view["cpu"].items(), key=lambda kv: _int(kv[0][3:], -1)) if k != "cpu"]
        ncores = osi["cores"] or len(cores) or "?"
        self.cpu_big.set("-" if cpu_all is None else f"{cpu_all:.0f}%")
        l1, l5, l15 = sample["load"]
        self.cpu_sub.set(f"{ncores} cores  ·  load {l1:.2f} {l5:.2f} {l15:.2f}"
                         + (f"\nBusiest core {max(cores):.0f}%" if cores else ""))
        self.cpu_graph.redraw()
        pct = 100.0 * mem["used"] / mem["total"] if mem["total"] else 0
        self.mem_big.set(f"{pct:.0f}%")
        sw = (f"  ·  swap {human_bytes(mem['swap_used'])} / {human_bytes(mem['swap_total'])}" if mem["swap_total"] else "  ·  no swap")
        self.mem_sub.set(f"{human_bytes(mem['used'])} used of {human_bytes(mem['total'])}{sw}")
        self.mem_bar["value"] = pct
        self.swap_bar["value"] = 100.0 * mem["swap_used"] / mem["swap_total"] if mem["swap_total"] else 0
        self.net_big.set(f"↓ {human_rate(view['rx'])}")
        self.net_sub.set(f"↑ {human_rate(view['tx'])}  ·  green ↓  yellow ↑")
        self.net_graph.redraw()
        self.sys_big.set(osi["hostname"] or self.host["name"])
        self.sys_sub.set(f"{osi['pretty'] or osi['kernel']}\n{osi['kernel'] if osi['pretty'] else ''}\nUp {human_duration(sample['uptime'])}".strip())
        if sample.get("hottest"):
            label, deg = sample["hottest"]
            self.cpu_sub.set(self.cpu_sub.get() + f"\nHottest sensor: {label} {deg:.0f} °C")
        self.status_var.set(("Glances  ·  " if self.glances else "") + f"Updated {time.strftime('%H:%M:%S')}  ·  every {self.interval_s} s")
        self.show_processes()
        self.show_disks(sample["disks"])
        self.show_network(view, sample)
        if self.glances:
            self.show_containers(sample.get("containers", []))
            if self.info_loaded:
                self.load_info()
        else:
            self.show_ports(sample["ports"])

    @staticmethod
    def sync(tree, rows):
        """Update a Treeview in place (keeps selection and scroll position). rows: [(iid, values, tags)]"""
        want = [r[0] for r in rows]
        for iid in tree.get_children():
            if iid not in want:
                tree.delete(iid)
        for i, (iid, values, tags) in enumerate(rows):
            if tree.exists(iid):
                tree.item(iid, values=values, tags=tags)
                tree.move(iid, "", i)
            else:
                tree.insert("", i, iid=iid, values=values, tags=tags)

    def sort_procs(self, col):
        self.proc_rev = not self.proc_rev if col == self.proc_sort else col not in ("user", "cmd")
        self.proc_sort = col
        self.show_processes()

    def show_processes(self):
        if not self.last:
            return
        procs = self.last["sample"]["procs"]
        needle = self.proc_filter.get().strip().lower()
        if needle:
            procs = [p for p in procs if needle in p["cmd"].lower() or needle in p["user"].lower() or needle == str(p["pid"])]
        key = {"etime": lambda p: etime_seconds(p["etime"])}.get(self.proc_sort, lambda p: p[self.proc_sort])
        procs = sorted(procs, key=key, reverse=self.proc_rev)[:200]
        rows = [(str(p["pid"]), (p["pid"], p["user"], f"{p['cpu']:.1f}", f"{p['mem']:.1f}", human_bytes(p["rss"]), p["etime"], p["cmd"]),
                 ("crit",) if p["cpu"] >= 80 else ("warn",) if p["cpu"] >= 40 else ()) for p in procs]
        self.sync(self.procs, rows)
        self.proc_info.set(f"{len(procs)} shown of {len(self.last['sample']['procs'])}" if self.last["sample"]["procs"]
                           else "(this server's `ps` gave no list)")

    def show_disks(self, disks):
        rows = [(d["mount"], (d["mount"], d["fs"], human_bytes(d["size"]), human_bytes(d["used"]), human_bytes(d["avail"]), f"{d['pct']}%"),
                 ("crit",) if d["pct"] >= 90 else ("warn",) if d["pct"] >= 80 else ()) for d in disks]
        self.sync(self.disks, rows)

    def show_network(self, view, sample):
        rows = []
        for name, (rx, tx) in sorted(sample["net"].items()):
            r, t = view["net"].get(name, (0, 0))
            rows.append((name, (name, human_rate(r), human_rate(t), human_bytes(rx), human_bytes(tx)), ()))
        self.sync(self.nets, rows)

    def show_ports(self, ports):
        rows = [(f"{p['proto']}/{p['addr']}/{p['port']}", (p["port"], p["proto"].upper(), p["addr"], p["proc"]), ()) for p in ports]
        self.sync(self.ports, rows)

    # ── on-demand commands (process detail, signals, services, system info) ──
    def run_async(self, command, done, timeout=30):
        """Run one command on this tab's SSH connection in a thread; done(code, out, err, error) runs on the UI thread."""
        def work():
            try:
                client = self.client
                if client is None:
                    raise RuntimeError("Not connected right now - wait for the next refresh and try again.")
                import rc_tools
                code, out, err = rc_tools.run_remote(client, command, timeout=timeout)
                res = (code, out, err, None)
            except Exception as e:
                res = (None, "", "", e)
            self.post(lambda: done(*res))
        threading.Thread(target=work, daemon=True, name="stats-cmd").start()

    def paint(self, widget):
        theme = getattr(self.app, "theme", None)
        if theme:
            theme.paint(widget)

    def tab_changed(self, _e=None):
        try:
            label = self.book.tab(self.book.select(), "text").strip()
        except tk.TclError:
            return
        if label == "Services" and not self.services_loaded:
            self.load_services()
        elif label == "Hardware" and not self.page_hardware.loaded:
            self.page_hardware.load()
        elif label == "IPMI" and not self.page_ipmi.loaded:
            self.page_ipmi.load()
        elif label == "Docker" and not self.page_docker.loaded:
            self.page_docker.load()
        elif label == "Firewall" and not self.page_firewall.loaded:
            self.page_firewall.load()
        elif label == "Capture" and not self.page_capture.loaded:
            self.page_capture.load()
        elif label == "System" and not self.info_loaded:
            self.load_info()
        elif label == "System" and self.glances:
            self.load_info()

    def selected_pid(self):
        sel = self.procs.selection()
        return int(sel[0]) if sel else None

    def open_process(self):
        pid = self.selected_pid()
        if pid is None:
            messagebox.showinfo("Processes", "Click a process in the list first.", parent=self)
            return
        if self.glances:
            p = next((x for x in (self.last or {}).get("sample", {}).get("procs", []) if x["pid"] == pid), None)
            if p:
                messagebox.showinfo("Process " + str(pid), f"{p['cmd']}\n\nUser: {p['user']}\nCPU: {p['cpu']:.1f} %   Memory: {p['mem']:.1f} % ({human_bytes(p['rss'])})\n"
                                    f"State: {p.get('status', '')}   Threads: {p.get('threads', '')}\n\n"
                                    "Glances is read-only. To send this process a signal, untick Glances on this server's Glances tab so Stats uses SSH.",
                                    parent=self)
            return
        ProcessDialog(self, pid)

    # containers (Glances only)
    def build_containers(self):
        f = ttk.Frame(self.book, padding=4)
        self.book.insert(self.page_system, f, text="  Containers  ")      # just before System
        cols = [("name", "Container"), ("status", "Status"), ("cpu", "CPU %"), ("mem", "Memory"), ("image", "Image"), ("uptime", "Up")]
        self.cont = self.make_tree(f, cols, (220, 90, 70, 90, 300, 150), {"cpu": "e", "mem": "e"})
        self.cont_note = ttk.Label(f, foreground="#555", text="From Glances' containers plugin (Docker / Podman). Empty when the machine has none or Glances cannot reach them.")
        self.cont_note.pack(anchor="w", pady=(4, 0))

    def show_containers(self, items):
        rows = []
        for c in items:
            name = str(c.get("name") or c.get("id") or "?")
            img = c.get("image")
            mem = c.get("memory_usage")
            if isinstance(mem, dict):
                mem = mem.get("usage")
            try:
                cpu = float(c.get("cpu_percent") or 0)
            except (TypeError, ValueError):
                cpu = 0.0
            rows.append((name, (name, c.get("status", ""), f"{cpu:.1f}", human_bytes(mem) if isinstance(mem, (int, float)) else "",
                                ", ".join(img) if isinstance(img, list) else str(img or ""), str(c.get("uptime", ""))),
                         ("crit",) if str(c.get("status", "")).lower() in ("exited", "dead", "unhealthy") else ()))
        self.sync(self.cont, rows)

    # services
    def build_services(self):
        f = self.page_services = ttk.Frame(self.book, padding=4)
        self.book.add(f, text="  Services  ")
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(0, 4))
        ttk.Label(top, text="Filter").pack(side="left")
        self.svc_filter = tk.StringVar()
        self.svc_filter.trace_add("write", lambda *_: self.show_services())
        ttk.Entry(top, textvariable=self.svc_filter, width=20).pack(side="left", padx=4)
        self.svc_state = tk.StringVar(value="All")
        box = ttk.Combobox(top, width=10, state="readonly", textvariable=self.svc_state, values=["All", "Running", "Failed", "Enabled"])
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: self.show_services())
        self.svc_info = tk.StringVar(value="Opens when you click this tab.")
        ttk.Label(top, textvariable=self.svc_info, foreground="#555").pack(side="left", padx=8)
        ttk.Button(top, text="⟳ Refresh", command=self.load_services).pack(side="right")
        acts = ttk.Frame(f)
        acts.pack(fill="x", pady=(0, 4))
        ttk.Label(acts, text="Selected service:").pack(side="left")
        for action in ("start", "stop", "restart", "enable", "disable"):
            ttk.Button(acts, text=SERVICE_ACTIONS[action], command=lambda a=action: self.service_action(a)).pack(side="left", padx=(6, 0))
        ttk.Button(acts, text="Logs…", command=self.service_logs).pack(side="left", padx=(14, 0))
        cols = [("unit", "Service"), ("active", "State"), ("sub", "Detail"), ("startup", "At boot"), ("desc", "Description")]
        self.svc_tree = self.make_tree(f, cols, (260, 80, 90, 90, 450))
        self.svc_tree.column("unit", stretch=True)
        self.svc_tree.bind("<Double-1>", lambda e: self.service_logs())

    def load_services(self):
        self.svc_info.set("Loading ...")

        def done(code, out, err, error):
            self.services_loaded = True
            if error:
                self.svc_info.set(f"Could not read services: {error}")
                return
            try:
                self.is_root, self.services = parse_services(out)
            except Unsupported as e:
                self.svc_info.set(str(e))
                return
            self.show_services()
        self.run_async(SERVICES_COMMAND, done)

    def show_services(self):
        word = self.svc_filter.get().strip().lower()
        mode = self.svc_state.get()
        rows = []
        for s in self.services:
            if word and word not in s["unit"].lower() and word not in s["desc"].lower():
                continue
            if mode == "Running" and s["sub"] != "running" or mode == "Failed" and s["active"] != "failed" \
                    or mode == "Enabled" and s["startup"] != "enabled":
                continue
            rows.append((s["unit"], (s["unit"], s["active"], s["sub"], s["startup"], s["desc"]),
                         ("crit",) if s["active"] == "failed" else ()))
        self.sync(self.svc_tree, rows)
        if self.services:
            failed = sum(1 for s in self.services if s["active"] == "failed")
            self.svc_info.set(f"{len(rows)} shown of {len(self.services)}" + (f"  ·  {failed} failed" if failed else "")
                              + ("" if self.is_root else "  ·  not root: start/stop need passwordless sudo"))

    def selected_unit(self):
        sel = self.svc_tree.selection()
        return sel[0] if sel else None

    def service_action(self, action):
        unit = self.selected_unit()
        if not unit:
            messagebox.showinfo("Services", "Click a service in the list first.", parent=self)
            return
        warn = ""
        if action in ("stop", "restart", "disable") and unit.split(".")[0].lower() in CRITICAL_UNITS:
            warn = "\n\nThis service may carry your own connection - you could be cut off."
        if not messagebox.askyesno("Services", f"{SERVICE_ACTIONS[action]} {unit} on {self.host['name']}?{warn}", parent=self,
                                   icon="warning" if warn else "question"):
            return
        self.svc_info.set(f"{SERVICE_ACTIONS[action]} {unit} ...")

        def done(code, out, err, error):
            text = (error and str(error)) or (out + err).strip()
            if error or code != 0:
                messagebox.showwarning("Services", f"{SERVICE_ACTIONS[action]} {unit} failed:\n\n{text or 'no details'}"
                                       + ("\n\nThis login is not root: it needs passwordless sudo (sudo -n)." if not self.is_root else ""),
                                       parent=self)
            self.load_services()
        self.run_async(service_command(action, unit, sudo=not self.is_root), done)

    def service_logs(self):
        unit = self.selected_unit()
        if unit:
            LogDialog(self, unit)

    # system info
    def build_system(self):
        f = self.page_system = ttk.Frame(self.book, padding=4)
        self.book.add(f, text="  System  ")
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(0, 4))
        self.info_status = tk.StringVar(value="Opens when you click this tab.")
        ttk.Label(top, textvariable=self.info_status, foreground="#555").pack(side="left")
        ttk.Button(top, text="⟳ Refresh", command=self.load_info).pack(side="right")
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        self.info_text = tk.Text(wrap, wrap="none", font=("Consolas", 10), height=10, borderwidth=0)
        sb = ttk.Scrollbar(wrap, command=self.info_text.yview)
        self.info_text.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.info_text.pack(side="left", fill="both", expand=True)

    def load_info(self):
        if self.glances:
            self.info_loaded = True
            if self.last:
                import rc_glances
                self.info_text.configure(state="normal")
                self.info_text.delete("1.0", "end")
                self.info_text.insert("1.0", rc_glances.format_info(self.last["sample"]))
                self.info_text.configure(state="disabled")
                self.info_status.set(f"From Glances at {time.strftime('%H:%M:%S')}")
            else:
                self.info_status.set("Waiting for the first Glances reading ...")
            return
        self.info_status.set("Loading ...")
        before = time.time()

        def done(code, out, err, error):
            self.info_loaded = True
            if error:
                self.info_status.set(f"Could not read system info: {error}")
                return
            info = parse_info(out, before, time.time())
            self.info_text.configure(state="normal")
            self.info_text.delete("1.0", "end")
            self.info_text.insert("1.0", format_info(info))
            self.info_text.configure(state="disabled")
            self.info_status.set(f"Read at {time.strftime('%H:%M:%S')}")
        self.run_async(INFO_COMMAND, done)

    def close(self):
        self.closed = True
        page = getattr(self, "page_capture", None)
        if page is not None:
            page.close()
        self.generation += 1
        self.stop_evt.set()
        self.now_evt.set()
        # the worker closes its own client on exit; also close here so a blocked read is interrupted
        self.drop_client()


class ProcessDialog(tk.Toplevel):
    """Details of one process (read from /proc) and a button to send it a signal, after a confirmation."""

    def __init__(self, tab, pid):
        super().__init__(tab)
        self.tab, self.pid = tab, pid
        self.title(f"Process {pid} on {tab.host['name']}")
        self.geometry("820x420+%d+%d" % (tab.winfo_rootx() + 60, tab.winfo_rooty() + 40))
        self.transient(tab.winfo_toplevel())
        self.text = tk.Text(self, wrap="word", font=("Consolas", 10), borderwidth=0, state="disabled")
        self.text.pack(fill="both", expand=True, padx=8, pady=8)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(bar, text="Send signal:").pack(side="left")
        self.sig = tk.StringVar(value=SIGNALS[0][1])
        ttk.Combobox(bar, state="readonly", width=34, textvariable=self.sig, values=[s[1] for s in SIGNALS]).pack(side="left", padx=6)
        ttk.Button(bar, text="Send…", command=self.send).pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="⟳ Refresh", command=self.load).pack(side="right", padx=6)
        tab.paint(self)
        self.load()

    def show(self, text):
        if not self.winfo_exists():
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.configure(state="disabled")

    def load(self):
        self.show("Loading ...")

        def done(code, out, err, error):
            if error:
                return self.show(f"Could not read the process: {error}")
            try:
                self.show(format_proc_detail(self.pid, parse_proc_detail(out)))
            except LookupError as e:
                self.show(str(e))
        self.tab.run_async(detail_command(self.pid), done)

    def signal_name(self):
        return next(n for n, label in SIGNALS if label == self.sig.get())

    def send(self):
        name = self.signal_name()
        msg = f"Send signal {name} to process {self.pid} on {self.tab.host['name']}?"
        if name == "KILL":
            msg += "\n\nKILL stops it immediately, with no chance to save its work. Unsaved data in it is lost."
        if not messagebox.askyesno("Send signal", msg, parent=self, icon="warning" if name == "KILL" else "question"):
            return

        def done(code, out, err, error):
            text = (error and str(error)) or (out + err).strip()
            if not error and code != 0 and "not permitted" in text.lower() and not self.tab.is_root:
                self.tab.run_async(kill_command(name, self.pid, sudo=True), done_sudo)   # one retry as sudo
                return
            self.finished(code, text, error)

        def done_sudo(code, out, err, error):
            self.finished(code, (error and str(error)) or (out + err).strip(), error)
        self.tab.run_async(kill_command(name, self.pid), done)

    def finished(self, code, text, error):
        if not self.winfo_exists():
            return
        if error or code != 0:
            messagebox.showwarning("Send signal", f"The server said:\n\n{text or 'no details'}\n\n"
                                   "Processes of other users need a root login or passwordless sudo.", parent=self)
        self.after(600, self.load)


class LogDialog(tk.Toplevel):
    """The last lines of a service's journal (journalctl -u)."""

    def __init__(self, tab, unit):
        super().__init__(tab)
        self.tab, self.unit = tab, unit
        self.title(f"Log of {unit} on {tab.host['name']}")
        self.geometry("980x520+%d+%d" % (tab.winfo_rootx() + 40, tab.winfo_rooty() + 30))
        self.transient(tab.winfo_toplevel())
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=8, pady=8)
        self.text = tk.Text(wrap, wrap="none", font=("Consolas", 9), borderwidth=0, state="disabled")
        sb = ttk.Scrollbar(wrap, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="⟳ Refresh", command=self.load).pack(side="right", padx=6)
        tab.paint(self)
        self.load()

    def load(self):
        def done(code, out, err, error):
            if not self.winfo_exists():
                return
            body = str(error) if error else (out + err).strip() or "(no log lines)"
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", body)
            self.text.configure(state="disabled")
            self.text.see("end")
        self.tab.run_async(journal_command(self.unit), done)
