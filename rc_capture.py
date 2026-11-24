"""REGTeches RC - packet capture page of the Stats tab (tcpdump on the server, over SSH).

Two ways to use it: **Show packets** lists what tcpdump sees as it happens (time, protocol, source, destination, details), and
**Record to a .pcap file** streams the raw capture to a file on this PC that Wireshark opens. Both stop on their own after the packet
count or number of seconds you set, so a forgotten capture cannot run for ever. tcpdump needs root: when the login is not root it is
run with `sudo -n` (passwordless sudo). The filter is a normal tcpdump filter (`port 443`, `host 10.0.0.5 and tcp`); it is checked
against a short list of safe characters and quoted, so it can never become a shell command.
Command building and line parsing are pure and tested without a server; the page is at the bottom."""

import re
import shlex
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import rc_monitor as mon

IFACE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,32}$")
FILTER_RE = re.compile(r"^[A-Za-z0-9_ .:/!=<>()\[\]&|-]{0,200}$")
MAX_PACKETS = 100000
MAX_SECONDS = 3600
MAX_ROWS = 5000

DETECT_COMMAND = ("sh -c 'echo @@uid; id -u; echo @@tool; command -v tcpdump; echo @@timeout; command -v timeout; "
                  "echo @@ifaces; ls /sys/class/net 2>/dev/null; echo @@end'")

# 2026-11-24 09:00:00.123456 IP 10.0.0.1.22 > 10.0.0.2.5555: Flags [P.], seq 1:37, length 36
LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(\S+)\s+(.*)$")


class CaptureError(ValueError):
    """A readable problem with the settings."""


def parse_detect(text):
    """-> (is_root, has_tcpdump, has_timeout, interfaces)."""
    sec = mon.split_sections(text)
    uid = (sec.get("uid") or ["1"])[0].strip()
    has = lambda name: any(l.strip() for l in sec.get(name, []))   # noqa: E731
    ifaces = [l.strip() for l in sec.get("ifaces", []) if IFACE_RE.match(l.strip())]
    return uid == "0", has("tool"), has("timeout"), sorted(ifaces)


def clean_iface(name):
    name = (name or "any").strip() or "any"
    if not IFACE_RE.match(name):
        raise CaptureError("That is not a valid interface name.")
    return name


def clean_filter(text):
    text = " ".join((text or "").split())
    if not FILTER_RE.match(text):
        raise CaptureError("The filter has characters that tcpdump filters do not need (letters, numbers, spaces and . : / ! = < > ( ) [ ] & | - only).")
    return text


def clean_limit(value, low, high, what):
    try:
        n = int(str(value).strip())
    except ValueError:
        raise CaptureError(f"{what} must be a whole number.")
    if not low <= n <= high:
        raise CaptureError(f"{what} must be between {low} and {high}.")
    return n


def build_command(iface, bpf, packets, seconds, is_root, has_timeout, pcap=False):
    """The shell command to run on the server. Text mode adds -l (line buffered) and readable timestamps; pcap mode writes the
    raw capture to stdout (-w -) flushed per packet (-U)."""
    iface, bpf = clean_iface(iface), clean_filter(bpf)
    packets = clean_limit(packets, 1, MAX_PACKETS, "The packet count")
    seconds = clean_limit(seconds, 1, MAX_SECONDS, "The time limit")
    parts = []
    if not is_root:
        parts.append("sudo -n")
    if has_timeout:
        parts.append(f"timeout -s INT {seconds}")
    parts.append("tcpdump -i " + shlex.quote(iface) + " -n -c " + str(packets))
    parts.append("-U -s 0 -w -" if pcap else "-l -tttt")
    if bpf:
        parts.append(shlex.quote(bpf))
    return " ".join(parts) + (" 2>/dev/null" if pcap else " 2>&1")


def parse_line(line):
    """One tcpdump line -> (time, proto, source, destination, info), or None for lines that are not packets (the listening banner...)."""
    line = line.rstrip("\r\n")
    m = LINE_RE.match(line)
    if not m:
        return None
    when, proto, rest = m.groups()
    when = when.split(" ", 1)[1]                      # keep only the time of day
    if " > " in rest:
        src, _, tail = rest.partition(" > ")
        dst, _, info = tail.partition(": ")
        return when, proto, src.strip(), dst.strip(), info.strip()
    return when, proto, "", "", rest.strip()


def split_lines(buffer, data):
    """Add `data` (bytes) to `buffer` (bytes left from before) and return (complete lines as str, new leftover)."""
    buffer += data
    *lines, rest = buffer.split(b"\n")
    return [l.decode("utf-8", "replace") for l in lines], rest


def stream_capture(client, command, on_lines=None, on_bytes=None, stop=None, pty=True, timeout_idle=30.0):
    """Run `command` and feed its output to on_lines(list of str) (text) or on_bytes(bytes) (raw). Returns the exit status
    (None if stopped). `stop` is a threading.Event; setting it closes the channel."""
    chan = client.get_transport().open_session()
    chan.settimeout(1.0)
    if pty:
        chan.get_pty()                                # closing a pty channel ends tcpdump too
    chan.exec_command(command)
    left = b""
    try:
        while True:
            if stop is not None and stop.is_set():
                return None
            try:
                data = chan.recv(65536)
            except Exception as e:                    # paramiko raises socket.timeout while waiting for packets
                if type(e).__name__ == "timeout" or "timed out" in str(e):
                    if chan.exit_status_ready() and not chan.recv_ready():
                        break
                    continue
                raise
            if not data:
                break
            if on_bytes:
                on_bytes(data)
            if on_lines:
                lines, left = split_lines(left, data)
                if lines:
                    on_lines(lines)
        if on_lines and left:
            on_lines([left.decode("utf-8", "replace")])
        return chan.recv_exit_status() if chan.exit_status_ready() else None
    finally:
        try:
            chan.close()
        except Exception:
            pass


# ─── the page ────────────────────────────────────────────────────────────────

class CapturePage(ttk.Frame):
    """Added to the Stats tab's notebook as "Capture"."""

    def __init__(self, mon_tab):
        super().__init__(mon_tab.book, padding=4)
        self.mon = mon_tab
        self.loaded = False
        self.is_root, self.has_tcpdump, self.has_timeout = None, False, False
        self.stop_evt = threading.Event()
        self.running = False
        self.shown = 0
        self.rows = []                                # every parsed packet, for "Save as text"
        self.pcap_file = None
        f = ttk.Frame(self)
        f.pack(fill="x")
        self.iface = tk.StringVar(value="any")
        self.filter = tk.StringVar()
        self.packets = tk.StringVar(value="500")
        self.seconds = tk.StringVar(value="60")
        self.mode = tk.StringVar(value="Show packets")
        ttk.Label(f, text="Interface").grid(row=0, column=0, sticky="w")
        self.iface_box = ttk.Combobox(f, width=14, textvariable=self.iface, values=["any"])
        self.iface_box.grid(row=0, column=1, padx=6, pady=2, sticky="w")
        ttk.Label(f, text="Filter").grid(row=0, column=2, sticky="w")
        ttk.Entry(f, width=34, textvariable=self.filter).grid(row=0, column=3, padx=6, sticky="w")
        ttk.Label(f, text="(e.g. port 443, host 10.0.0.5 and tcp)", foreground="#555").grid(row=0, column=4, sticky="w")
        ttk.Label(f, text="Stop after").grid(row=1, column=0, sticky="w")
        lim = ttk.Frame(f)
        lim.grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=2)
        ttk.Entry(lim, width=7, textvariable=self.packets).pack(side="left")
        ttk.Label(lim, text="packets or").pack(side="left", padx=4)
        ttk.Entry(lim, width=6, textvariable=self.seconds).pack(side="left")
        ttk.Label(lim, text="seconds   ").pack(side="left")
        ttk.Combobox(lim, width=18, state="readonly", textvariable=self.mode, values=["Show packets", "Record to a .pcap file"]).pack(side="left")
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=4)
        self.start_btn = ttk.Button(bar, text="▶ Start", command=self.start)
        self.stop_btn = ttk.Button(bar, text="■ Stop", command=self.stop, state="disabled")
        self.start_btn.pack(side="left")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left")
        ttk.Button(bar, text="Save list as text…", command=self.save_text).pack(side="left", padx=6)
        self.info = tk.StringVar(value="Opens when you click this tab.")
        ttk.Label(bar, textvariable=self.info, foreground="#555").pack(side="left", padx=8)
        cols = [("time", "Time", 100), ("proto", "Proto", 60), ("src", "Source", 200), ("dst", "Destination", 200), ("info", "Details", 520)]
        self.tree = ttk.Treeview(self, columns=[c for c, _, _ in cols], show="headings")
        for c, t, w in cols:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w", stretch=(c == "info"))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

    # setup
    def load(self):
        self.info.set("Checking the server ...")

        def done(code, out, err, error):
            self.loaded = True
            if error:
                self.info.set(f"Could not check the server: {error}")
                return
            self.is_root, self.has_tcpdump, self.has_timeout, ifaces = parse_detect(out)
            self.iface_box.configure(values=["any"] + ifaces)
            if not self.has_tcpdump:
                self.info.set("tcpdump is not installed on this server (install it with your package manager).")
            else:
                self.info.set("Ready" + ("" if self.is_root else "  ·  uses sudo -n (passwordless sudo needed)"))
        self.mon.run_async(DETECT_COMMAND, done)

    # running
    def set_running(self, on):
        self.running = on
        self.start_btn.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")

    def start(self):
        if self.running:
            return
        if not self.loaded:
            messagebox.showinfo("Capture", "Still checking the server. Try again in a moment.", parent=self)
            return
        if not self.has_tcpdump:
            messagebox.showwarning("Capture", "tcpdump is not installed on this server.", parent=self)
            return
        pcap = self.mode.get().startswith("Record")
        try:
            cmd = build_command(self.iface.get(), self.filter.get(), self.packets.get(), self.seconds.get(), self.is_root, self.has_timeout, pcap)
        except CaptureError as e:
            messagebox.showwarning("Capture", str(e), parent=self)
            return
        client = self.mon.client
        if client is None:
            messagebox.showinfo("Capture", "Not connected right now. Wait for the next refresh and try again.", parent=self)
            return
        path = None
        if pcap:
            path = filedialog.asksaveasfilename(parent=self, title="Save the capture", defaultextension=".pcap",
                                                initialfile=f"{self.mon.host['name']}-{time.strftime('%Y%m%d-%H%M%S')}.pcap",
                                                filetypes=[("Packet capture", "*.pcap"), ("All files", "*.*")])
            if not path:
                return
        self.stop_evt = threading.Event()
        self.shown = 0
        self.set_running(True)
        self.info.set("Capturing ...")
        threading.Thread(target=self.work, args=(client, cmd, pcap, path, self.stop_evt), daemon=True, name="capture").start()

    def work(self, client, cmd, pcap, path, stop):
        got = [0]
        error = None
        fh = None
        try:
            if pcap:
                fh = open(path, "wb")

                def on_bytes(b):
                    fh.write(b)
                    got[0] += len(b)
                    self.mon.post(lambda n=got[0]: self.info.set(f"Recording ... {n / 1024:.0f} KB"))
                code = stream_capture(client, cmd, on_bytes=on_bytes, stop=stop, pty=False)
            else:
                code = stream_capture(client, cmd, on_lines=lambda lines: self.mon.post(lambda l=lines: self.add_lines(l)), stop=stop, pty=True)
        except Exception as e:
            error, code = e, None
        finally:
            if fh:
                fh.close()
        self.mon.post(lambda: self.finished(code, error, pcap, path, got[0]))

    def add_lines(self, lines):
        banner = []
        for line in lines:
            p = parse_line(line)
            if p is None:
                if line.strip():
                    banner.append(line.strip())
                continue
            self.rows.append(p)
            self.shown += 1
            if self.shown <= MAX_ROWS:
                self.tree.insert("", "end", values=p)
        if self.shown and self.shown % 20 < len(lines):
            self.tree.yview_moveto(1.0)
        if banner and any(w in b.lower() for b in banner for w in ("permission", "sudo", "password", "not found", "no such device", "syntax error")):
            self.info.set(banner[-1][:120])
        else:
            self.info.set(f"{self.shown} packet(s)")

    def finished(self, code, error, pcap, path, size):
        self.set_running(False)
        if error:
            self.info.set(f"Stopped: {error}")
        elif pcap:
            self.info.set(f"Saved {size / 1024:.0f} KB to {path}" if size else "Nothing was captured (no matching packets, or root is needed).")
        elif code not in (0, None) and not self.shown:
            self.info.set(f"tcpdump ended with status {code}. If it needs root, this login must have passwordless sudo.")
        else:
            self.info.set(f"Finished: {self.shown} packet(s)")

    def stop(self):
        self.stop_evt.set()
        self.info.set("Stopping ...")

    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.shown = 0

    def save_text(self):
        if not self.rows:
            messagebox.showinfo("Capture", "There is nothing in the list yet.", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, title="Save the list", defaultextension=".txt",
                                            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for r in self.rows:
                    f.write("  ".join(x for x in r if x) + "\n")
        except OSError as e:
            messagebox.showerror("Capture", f"Could not save:\n{e}", parent=self)
            return
        self.info.set(f"Saved the list to {path}")

    def close(self):
        self.stop_evt.set()
