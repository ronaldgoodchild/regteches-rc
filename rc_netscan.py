"""REGTeches RC - network scanner (Tools > Network > Network scan).

Finds the machines on a network and what they offer: for every address in a range it tries a list of common TCP ports
(a "refused" answer also proves a machine is there), then adds each machine's name (reverse DNS) and MAC address (from
the PC's ARP table). A found machine becomes a server entry in one click. Plain TCP connects only: nothing is sent
that a normal connection would not send, and nothing is installed anywhere.

Scan only networks you are allowed to scan. The pure functions at the top are tested without a window."""

import csv
import ipaddress
import re
import socket
import subprocess
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, simpledialog, ttk

import rc_manager as core

log = core.log

MAX_HOSTS = 1024
MAX_PORTS = 10000
COMMON_PORTS = [(21, "FTP"), (22, "SSH"), (23, "Telnet"), (25, "SMTP"), (53, "DNS"), (80, "HTTP"), (110, "POP3"), (139, "NetBIOS"),
                (143, "IMAP"), (443, "HTTPS"), (445, "SMB"), (515, "LPD"), (554, "RTSP"), (631, "IPP"), (1883, "MQTT"),
                (3389, "RDP"), (5000, "DSM"), (5001, "DSM-SSL"), (5900, "VNC"), (8000, "HTTP-alt"), (8080, "HTTP-alt"),
                (8443, "HTTPS-alt"), (9100, "JetDirect"), (32400, "Plex"), (61208, "Glances")]
SERVICE = dict(COMMON_PORTS)


# ─── parsing (pure) ──────────────────────────────────────────────────────────

def local_subnet():
    """This PC's /24, e.g. '192.168.1.0/24' ('' if it has no network)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("203.0.113.1", 9))          # nothing is sent: this only picks the outgoing interface
            ip = s.getsockname()[0]
        return str(ipaddress.ip_network(ip + "/24", strict=False))
    except OSError:
        return ""


def parse_targets(text):
    """'192.168.1.0/24', '192.168.1.10-50', '192.168.1.10-192.168.1.60', '10.0.0.5' (comma or space separated) -> list of
    address strings. Raises ValueError with a message for the user."""
    out, seen = [], set()
    for part in [p for p in re.split(r"[,\s]+", (text or "").strip()) if p]:
        try:
            if "/" in part:
                net = ipaddress.ip_network(part, strict=False)
                if net.version != 4:
                    raise ValueError("Only IPv4 networks can be scanned.")
                hosts = list(net.hosts()) if net.prefixlen < 31 else list(net)
            elif "-" in part:
                a, b = part.split("-", 1)
                first = ipaddress.ip_address(a)
                last = ipaddress.ip_address(b) if "." in b else ipaddress.ip_address(".".join(a.split(".")[:3] + [b]))
                if last < first:
                    raise ValueError(f"{part}: the end comes before the start.")
                if int(last) - int(first) + 1 > MAX_HOSTS:
                    raise ValueError(f"That range is more than {MAX_HOSTS} addresses. Scan a smaller piece.")
                hosts = [ipaddress.ip_address(n) for n in range(int(first), int(last) + 1)]
            else:
                hosts = [ipaddress.ip_address(part)]
        except ValueError as e:
            msg = str(e)
            raise ValueError(msg if msg.endswith(".") or "range" in msg else f"“{part}” is not an IP address, range or network.")
        if any(h.version != 4 for h in hosts):
            raise ValueError("Only IPv4 addresses can be scanned.")
        if len(hosts) > MAX_HOSTS:
            raise ValueError(f"{part} is more than {MAX_HOSTS} addresses. Scan a smaller piece (for example a /24).")
        for h in hosts:
            if str(h) not in seen:
                seen.add(str(h))
                out.append(str(h))
    if not out:
        raise ValueError("Enter a network such as 192.168.1.0/24, or a range such as 192.168.1.10-50.")
    if len(out) > MAX_HOSTS:
        raise ValueError(f"That is {len(out)} addresses; the limit is {MAX_HOSTS}.")
    return out


def parse_ports(text):
    """'common', '22,80,8000-8100', '1-1024' -> sorted list of ports. Raises ValueError."""
    text = (text or "").strip().lower()
    if text in ("", "common"):
        return [p for p, _ in COMMON_PORTS]
    ports = set()
    for part in [p for p in re.split(r"[,\s]+", text) if p]:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"“{part}” is not a port or a range like 1-1024.")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= a <= 65535 or not 1 <= b <= 65535 or b < a:
            raise ValueError(f"“{part}”: ports go from 1 to 65535.")
        ports.update(range(a, b + 1))
    if len(ports) > MAX_PORTS:
        raise ValueError(f"That is {len(ports)} ports; scan at most {MAX_PORTS} at a time.")
    return sorted(ports)


def parse_arp(text):
    """Windows/Linux `arp -a` output -> {ip: 'AA:BB:CC:DD:EE:FF'}."""
    out = {}
    for line in (text or "").splitlines():
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)\)?\s+(?:at\s+)?([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})\b", line)
        if m:
            mac = core.normalize_mac(m.group(2))
            if mac and mac not in ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00") and not mac.startswith("01:00:5E"):
                out[m.group(1)] = mac
    return out


def describe_ports(ports):
    return ", ".join(f"{p} {SERVICE[p]}" if p in SERVICE else str(p) for p in ports)


def suggest_host(ip, name, mac, ports):
    """A server entry for a scanned machine (what to open on double-click depends on what it offers)."""
    h = dict(core.NEW_HOST, name=name or ip, address=ip, mac=mac or "", group="Scanned")
    if 61208 in ports:
        h["glances"] = True              # a machine already running Glances: Stats can use it without any login
    if 22 in ports:
        h["default_action"] = "ssh"
    elif 3389 in ports:
        h["default_action"] = "rdp"
    elif 5900 in ports:
        h["default_action"] = "vnc"
    elif {80, 443, 8080, 8000, 8443, 5000, 5001, 32400} & set(ports):
        h["default_action"] = "web"
        for p, scheme in ((443, "https"), (8443, "https"), (5001, "https"), (80, "http"), (8080, "http"), (8000, "http"), (5000, "http"), (32400, "http")):
            if p in ports:
                h["web_url"] = f"{scheme}://{ip}" + ("" if p in (80, 443) else f":{p}")
                break
    return h


# ─── probing ─────────────────────────────────────────────────────────────────

def probe(ip, port, timeout=0.5):
    """'open', 'closed' (the machine answered 'no') or 'none' (silence or unreachable)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        code = s.connect_ex((ip, port))
    except OSError:
        return "none"
    finally:
        s.close()
    if code == 0:
        return "open"
    return "closed" if code in (10061, 111, 61) else "none"          # WSAECONNREFUSED / ECONNREFUSED


def arp_table():
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10,
                             creationflags=core.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_arp(out)


def reverse_dns(ip, wait=1.5):
    box = []

    def look():
        try:
            box.append(socket.gethostbyaddr(ip)[0])
        except (OSError, UnicodeError):
            pass
    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(wait)
    return box[0] if box else ""


class ScanJob:
    """Probes every (address, port) pair on a thread pool. on_host(ip, up, open_ports) is called from a worker thread as
    soon as one address has been fully tried; on_progress(done, total); on_done(stopped). stop() ends it early."""

    def __init__(self, ips, ports, on_host, on_progress, on_done, workers=96, timeout=0.5):
        self.ips, self.ports, self.on_host, self.on_progress, self.on_done = ips, ports, on_host, on_progress, on_done
        self.workers, self.timeout = workers, timeout
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.left = {ip: len(ports) for ip in ips}
        self.open = {ip: [] for ip in ips}
        self.up = {ip: False for ip in ips}
        self.done, self.total = 0, len(ips) * len(ports)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="netscan")
        self.thread.start()
        return self

    def stop(self):
        self.stop_evt.set()

    def task(self, ip, port):
        if self.stop_evt.is_set():
            return
        r = probe(ip, port, self.timeout)
        finished = None
        with self.lock:
            if r == "open":
                self.open[ip].append(port)
            if r != "none":
                self.up[ip] = True
            self.left[ip] -= 1
            self.done += 1
            done = self.done
            if self.left[ip] == 0:
                finished = (ip, self.up[ip], sorted(self.open[ip]))
        if finished:
            self.on_host(*finished)
        if done % 25 == 0 or done == self.total:
            self.on_progress(done, self.total)

    def run(self):
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for port in self.ports:                      # port-major order: every address gets its first answers early
                    for ip in self.ips:
                        pool.submit(self.task, ip, port)
        finally:
            self.on_progress(self.done, self.total)
            self.on_done(self.stop_evt.is_set())


# ─── the tab ─────────────────────────────────────────────────────────────────

def ip_key(ip):
    try:
        return int(ipaddress.ip_address(ip))
    except ValueError:
        return 0


class NetScanTab(ttk.Frame):
    def __init__(self, app):
        super().__init__(app.nb, padding=12)
        self.app, self.job = app, None
        self.rows = {}          # ip -> {"name", "mac", "ports"}
        ttk.Label(self, text="Network scan", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, foreground="#555", wraplength=900, justify="left", text=(
            "Finds machines on a network and what they offer. Only plain connections are made. Scan only networks you are allowed to scan.")
        ).pack(anchor="w", pady=(2, 8))
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.target = tk.StringVar(value=local_subnet())
        self.portsv = tk.StringVar(value="common")
        ttk.Label(top, text="Network or range").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.target, width=36).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(top, text="e.g. 192.168.1.0/24   or   192.168.1.10-50", foreground="#555").grid(row=0, column=2, sticky="w")
        ttk.Label(top, text="Ports").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(top, textvariable=self.portsv, width=36).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(top, text="common   or   22,80,443   or   1-1024", foreground="#555").grid(row=1, column=2, sticky="w")
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=6)
        self.go_btn = ttk.Button(bar, text="▶ Scan", command=self.start)
        self.go_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(bar, length=260, maximum=100)
        self.progress.pack(side="left", padx=8)
        self.info = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.info).pack(side="left")
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        cols = [("ip", "IP address", 120), ("name", "Name", 200), ("mac", "MAC", 140), ("ports", "Open ports", 330), ("rc", "In RC", 150)]
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for cid, label, w in cols:
            self.tree.heading(cid, text=label, command=lambda c=cid: self.sort_by(c))
            self.tree.column(cid, width=w, stretch=(cid == "ports"))
        sb = ttk.Scrollbar(wrap, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("known", foreground="#0b5cad")
        self.tree.bind("<Double-1>", lambda e: self.add_server())
        acts = ttk.Frame(self)
        acts.pack(fill="x", pady=(6, 0))
        ttk.Button(acts, text="➕ Add as server…", command=self.add_server).pack(side="left")
        ttk.Button(acts, text="Scan more ports on this machine…", command=self.deep_scan).pack(side="left", padx=6)
        ttk.Button(acts, text="Copy", command=self.copy).pack(side="left")
        ttk.Button(acts, text="Export CSV…", command=self.export).pack(side="left", padx=6)
        self.sort_col, self.sort_rev = "ip", False

    # scanning
    def start(self, ips=None, ports=None):
        if self.job:
            return
        try:
            ips = ips or parse_targets(self.target.get())
            ports = ports or parse_ports(self.portsv.get())
        except ValueError as e:
            messagebox.showwarning("Network scan", str(e), parent=self)
            return
        if len(ips) * len(ports) > 200000:
            messagebox.showwarning("Network scan", "That is a very large scan. Use fewer addresses or ports.", parent=self)
            return
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.go_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self.info.set(f"Scanning {len(ips)} address(es), {len(ports)} port(s) each ...")
        self.progress["value"] = 0
        self.started = time.time()
        self.job = ScanJob(ips, ports, lambda *a: self.app.post(lambda: self.host_done(*a)),
                           lambda d, t: self.app.post(lambda: self.progress.configure(value=100.0 * d / max(1, t))),
                           lambda stopped: self.app.post(lambda: self.finished(stopped))).start()

    def stop(self):
        if self.job:
            self.job.stop()
            self.info.set("Stopping ...")

    def host_done(self, ip, up, ports):
        if not up or not self.winfo_exists():
            return
        self.rows[ip] = {"name": "", "mac": "", "ports": ports}
        self.tree.insert("", "end", iid=ip, values=(ip, "", "", describe_ports(ports), self.known(ip, "")))
        self.resort()

    def finished(self, stopped):
        if not self.winfo_exists():
            return
        self.job = None
        self.go_btn.state(["!disabled"])
        self.stop_btn.state(["disabled"])
        n = len(self.rows)
        self.info.set(f"{'Stopped' if stopped else 'Done'}: {n} machine(s) found in {time.time() - self.started:.0f} s. Looking up names ...")
        threading.Thread(target=self.lookup_names, daemon=True, name="netscan-names").start()

    def lookup_names(self):
        arp = arp_table()
        ips = list(self.rows)
        with ThreadPoolExecutor(max_workers=16) as pool:
            names = dict(zip(ips, pool.map(reverse_dns, ips)))
        self.app.post(lambda: self.apply_names(arp, names))

    def apply_names(self, arp, names):
        if not self.winfo_exists():
            return
        for ip, row in self.rows.items():
            row["name"], row["mac"] = names.get(ip, ""), arp.get(ip, "")
            if self.tree.exists(ip):
                self.tree.item(ip, values=(ip, row["name"], row["mac"], describe_ports(row["ports"]), self.known(ip, row["mac"])),
                               tags=("known",) if self.known(ip, row["mac"]) else ())
        self.info.set(f"{len(self.rows)} machine(s) found.")

    def known(self, ip, mac):
        """Name of the server already in RC with this address or MAC ('' if none)."""
        for h in self.app.hosts:
            if h.get("address") == ip or (mac and h.get("mac") and core.normalize_mac(h["mac"]) == mac) or ip in h.get("alt_addresses", []):
                return "✔ " + h["name"]
        return ""

    # list actions
    def sort_by(self, col):
        self.sort_rev = not self.sort_rev if col == self.sort_col else False
        self.sort_col = col
        self.resort()

    def resort(self):
        col = self.sort_col
        idx = {"ip": 0, "name": 1, "mac": 2, "ports": 3, "rc": 4}[col]
        items = [(self.tree.item(i, "values"), i) for i in self.tree.get_children()]
        items.sort(key=lambda t: ip_key(t[1]) if col == "ip" else str(t[0][idx]).lower(), reverse=self.sort_rev)
        for n, (_, i) in enumerate(items):
            self.tree.move(i, "", n)

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def add_server(self):
        ip = self.selected()
        if not ip:
            messagebox.showinfo("Network scan", "Click a machine in the list first.", parent=self)
            return None
        row = self.rows[ip]
        h = suggest_host(ip, row["name"].split(".")[0] if row["name"] else "", row["mac"], row["ports"])
        self.app.open_editor(h, "new")
        return h

    def deep_scan(self):
        ip = self.selected()
        if not ip:
            messagebox.showinfo("Network scan", "Click a machine in the list first.", parent=self)
            return
        text = simpledialog.askstring("Scan more ports", f"Ports to try on {ip}  (for example 1-1024 or 20-25,8000-9000):",
                                      initialvalue="1-1024", parent=self)
        if not text:
            return
        try:
            ports = parse_ports(text)
        except ValueError as e:
            messagebox.showwarning("Network scan", str(e), parent=self)
            return
        self.info.set(f"Scanning {len(ports)} port(s) on {ip} ...")
        result = {}

        def on_host(i, up, found):
            result["found"] = found

        def done(stopped):
            found = sorted(set(self.rows[ip]["ports"]) | set(result.get("found", [])))
            self.app.post(lambda: self.deep_done(ip, found))
        ScanJob([ip], ports, on_host, lambda d, t: None, done, workers=128, timeout=0.6).start()

    def deep_done(self, ip, found):
        if not self.winfo_exists():
            return
        row = self.rows[ip]
        row["ports"] = found
        self.tree.item(ip, values=(ip, row["name"], row["mac"], describe_ports(found), self.known(ip, row["mac"])))
        self.info.set(f"{ip}: {len(found)} open port(s).")

    def text_rows(self):
        return [(ip, r["name"], r["mac"], describe_ports(r["ports"])) for ip, r in sorted(self.rows.items(), key=lambda kv: ip_key(kv[0]))]

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join("\t".join(r) for r in self.text_rows()))
        self.info.set("Copied the results.")

    def export(self):
        path = filedialog.asksaveasfilename(parent=self, title="Save the scan", defaultextension=".csv",
                                            initialfile="network-scan-" + time.strftime("%Y%m%d-%H%M") + ".csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return None
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ip", "name", "mac", "open_ports"])
                w.writerows(self.text_rows())
        except OSError as e:
            messagebox.showerror("Network scan", f"Could not save:\n{e}", parent=self)
            return None
        self.info.set(f"Saved {path}")
        return path

    def close(self):
        if self.job:
            self.job.stop()
