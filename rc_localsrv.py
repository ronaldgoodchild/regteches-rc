"""REGTeches RC - temporary file servers on THIS PC (Tools > Network > Local servers): TFTP and HTTP.

For pushing a firmware or config file to a switch, router or camera that can only fetch from a TFTP/HTTP server, or
sharing a folder with another device for a few minutes. They serve one folder, log every request, can stop
themselves after a time, and refuse anything outside the folder. TFTP is read-only unless you allow uploads.
Anyone on your network can read the served folder while a server runs: serve a folder that holds only what you mean to share.

TFTP is written here (RFC 1350 with the blksize / tsize options): the transfer is always binary ("netascii" is treated as
binary). HTTP uses Python's own server, read-only."""

import http.server
import os
import socket
import socketserver
import struct
import threading
import time
import tkinter as tk
import urllib.parse
from tkinter import filedialog, messagebox, ttk

import rc_manager as core

log = core.log

RRQ, WRQ, DATA, ACK, ERROR, OACK = 1, 2, 3, 4, 5, 6
E_NOTFOUND, E_ACCESS, E_DISK, E_ILLEGAL, E_TID, E_EXISTS, E_OPTION = 1, 2, 3, 4, 5, 6, 8
MAX_UPLOAD = 1024 * 1024 * 1024        # 1 GB
TIMEOUT, RETRIES = 3.0, 5


def port_available(port, udp=False):
    """Can this PC listen on the port right now? (Windows keeps some ranges for itself, e.g. for Hyper-V / WSL / Docker.)"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def first_free(candidates, udp=False):
    """The first port in the list this PC can listen on (the first one if none can, so the user sees the real error)."""
    return next((p for p in candidates if port_available(p, udp)), candidates[0])


def explain_bind_error(e, port):
    code = getattr(e, "winerror", None) or getattr(e, "errno", None)
    if code in (10013, 13):
        return (f"Windows does not allow this program to listen on port {port}. That usually means Windows has reserved the port "
                "(Hyper-V, WSL and Docker reserve ranges such as 7980-8179), or another program holds it exclusively. "
                "Try a different port, for example 8888 or 9000.")
    if code in (10048, 98, 48):
        return f"Another program is already using port {port}. Close it or choose a different port."
    return f"{e}"


def local_addresses():
    """This PC's IPv4 addresses (the ones other devices would use), the one on the network it reaches the world through first."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("203.0.113.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    seen = []
    for a in found:
        if a not in seen and not a.startswith("127."):
            seen.append(a)
    return seen


def safe_path(root, name):
    """The file inside `root` that `name` points to, or None if it would leave the folder. Leading slashes are ignored
    and both / and \\ are accepted as separators."""
    name = name.replace("\\", "/").lstrip("/")
    if not name or "\x00" in name or any(part == ".." for part in name.split("/")):
        return None
    full = os.path.normpath(os.path.join(root, *name.split("/")))
    base = os.path.normcase(os.path.abspath(root))
    full_n = os.path.normcase(os.path.abspath(full))
    if full_n != base and not full_n.startswith(base + os.sep):
        return None
    return full


# ─── TFTP ────────────────────────────────────────────────────────────────────

def parse_request(pkt):
    """RRQ/WRQ packet -> (opcode, filename, mode, options dict) or None."""
    if len(pkt) < 4:
        return None
    op = struct.unpack("!H", pkt[:2])[0]
    if op not in (RRQ, WRQ):
        return None
    parts = pkt[2:].split(b"\x00")
    if len(parts) < 3:
        return None
    try:
        name, mode = parts[0].decode("utf-8"), parts[1].decode("ascii").lower()
        opts = {}
        rest = parts[2:]
        for k, v in zip(rest[0::2], rest[1::2]):
            if k:
                opts[k.decode("ascii").lower()] = v.decode("ascii")
    except UnicodeDecodeError:
        return None
    return op, name, mode, opts


def error_packet(code, msg):
    return struct.pack("!HH", ERROR, code) + msg.encode("ascii", "replace") + b"\x00"


class TftpServer:
    def __init__(self, root, port=69, allow_write=False, overwrite=False, bind="0.0.0.0", logger=None):
        self.root, self.port, self.allow_write, self.overwrite, self.bind = os.path.abspath(root), port, allow_write, overwrite, bind
        self.logger = logger or (lambda m: None)
        self.sock = None
        self.running = False
        self.timeout, self.retries = TIMEOUT, RETRIES
        self.clients = set()

    def say(self, msg):
        try:
            self.logger(msg)
        except Exception:
            pass

    def start(self):
        if not os.path.isdir(self.root):
            raise OSError(f"The folder {self.root} does not exist.")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) if os.name != "nt" else None
        s.bind((self.bind, self.port))
        s.settimeout(0.5)
        self.sock, self.running = s, True
        self.port = s.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True, name="tftp").start()
        self.say(f"TFTP server listening on port {self.port}, folder {self.root}"
                 + (", uploads allowed" + (" (may overwrite)" if self.overwrite else " (new files only)") if self.allow_write else ", read-only"))

    def stop(self):
        was, self.running = self.running, False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if was:
            self.say("TFTP server stopped")

    def loop(self):
        while self.running:
            try:
                pkt, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            req = parse_request(pkt)
            if req is None:
                self.reply_error(addr, E_ILLEGAL, "Illegal TFTP operation")
                continue
            threading.Thread(target=self.handle, args=(req, addr), daemon=True, name="tftp-xfer").start()

    def reply_error(self, addr, code, msg):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(error_packet(code, msg), addr)
        except OSError:
            pass

    # one transfer
    def handle(self, req, addr):
        op, name, mode, opts = req
        who = f"{addr[0]}"
        path = safe_path(self.root, name)
        if path is None:
            self.say(f"{who}: refused {'read' if op == RRQ else 'write'} of {name!r} (outside the folder)")
            return self.reply_error(addr, E_ACCESS, "Access violation")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.bind, 0))
        sock.settimeout(self.timeout)
        try:
            if op == RRQ:
                self.send_file(sock, addr, path, name, opts, who)
            else:
                self.receive_file(sock, addr, path, name, opts, who)
        except OSError as e:
            self.say(f"{who}: {name}: failed ({e})")
        finally:
            sock.close()

    def negotiate(self, opts, size=None):
        """(accepted options dict, blksize)."""
        accepted, blk = {}, 512
        if "blksize" in opts and opts["blksize"].isdigit():
            blk = max(8, min(65464, int(opts["blksize"])))
            accepted["blksize"] = str(blk)
        if "tsize" in opts:
            accepted["tsize"] = str(size) if size is not None else opts["tsize"]
        return accepted, blk

    @staticmethod
    def oack(accepted):
        return struct.pack("!H", OACK) + b"".join(k.encode() + b"\x00" + v.encode() + b"\x00" for k, v in accepted.items())

    def wait_for(self, sock, addr, packet, want):
        """Send `packet`, wait for a packet whose opcode/block pass want(op, block); resend on silence. None if the peer gave up."""
        for _ in range(self.retries):
            if packet:
                sock.sendto(packet, addr)
            try:
                while True:
                    pkt, src = sock.recvfrom(65535)
                    if src != addr:
                        sock.sendto(error_packet(E_TID, "Unknown transfer ID"), src)
                        continue
                    if len(pkt) >= 2 and struct.unpack("!H", pkt[:2])[0] == ERROR:
                        return "error"
                    got = want(pkt)
                    if got is not None:
                        return got
            except socket.timeout:
                continue
        return None

    def send_file(self, sock, addr, path, name, opts, who):
        if not os.path.isfile(path):
            self.say(f"{who}: {name}: not found")
            return sock.sendto(error_packet(E_NOTFOUND, "File not found"), addr)
        size = os.path.getsize(path)
        accepted, blk = self.negotiate(opts, size)
        started = time.time()
        with open(path, "rb") as f:
            if accepted:
                r = self.wait_for(sock, addr, self.oack(accepted),
                                  lambda p: True if len(p) >= 4 and struct.unpack("!HH", p[:4]) == (ACK, 0) else None)
                if r is not True:
                    return self.say(f"{who}: {name}: client did not accept the options")
            block = 1
            sent = 0
            while True:
                data = f.read(blk)
                pkt = struct.pack("!HH", DATA, block & 0xFFFF) + data
                r = self.wait_for(sock, addr, pkt, lambda p, b=block & 0xFFFF: True if len(p) >= 4 and struct.unpack("!HH", p[:4]) == (ACK, b) else None)
                if r != True:                                            # noqa: E712 (r can be True, None or "error")
                    return self.say(f"{who}: {name}: transfer stopped after {sent} bytes ({'client error' if r == 'error' else 'no answer'})")
                sent += len(data)
                if len(data) < blk:
                    break
                block += 1
        secs = max(time.time() - started, 0.001)
        self.say(f"{who}: sent {name} ({sent} bytes, {sent / secs / 1024:.0f} KB/s)")

    def receive_file(self, sock, addr, path, name, opts, who):
        if not self.allow_write:
            self.say(f"{who}: refused upload of {name} (uploads are off)")
            return sock.sendto(error_packet(E_ACCESS, "Write not allowed"), addr)
        if os.path.exists(path) and not self.overwrite:
            self.say(f"{who}: refused upload of {name} (file exists)")
            return sock.sendto(error_packet(E_EXISTS, "File already exists"), addr)
        if os.path.isdir(path):
            return sock.sendto(error_packet(E_ACCESS, "Access violation"), addr)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            return sock.sendto(error_packet(E_ACCESS, "Access violation"), addr)
        accepted, blk = self.negotiate(opts)
        part = path + ".part"
        expected, total, started = 1, 0, time.time()
        first = self.oack(accepted) if accepted else struct.pack("!HH", ACK, 0)
        try:
            with open(part, "wb") as f:
                packet, finished = first, False
                while not finished:
                    def is_next(p, e=expected):
                        if len(p) >= 4 and struct.unpack("!H", p[:2])[0] == DATA:
                            b = struct.unpack("!H", p[2:4])[0]
                            return p if b == e & 0xFFFF else (b"" if b == (e - 1) & 0xFFFF else None)
                        return None
                    r = self.wait_for(sock, addr, packet, is_next)
                    if r is None or r == "error":
                        raise OSError("client stopped sending" if r is None else "client reported an error")
                    if r == b"":                                          # a repeat of the block we already have: just re-ack
                        packet = struct.pack("!HH", ACK, (expected - 1) & 0xFFFF)
                        continue
                    data = r[4:]
                    total += len(data)
                    if total > MAX_UPLOAD:
                        sock.sendto(error_packet(E_DISK, "File too large"), addr)
                        raise OSError("upload too large")
                    f.write(data)
                    packet = struct.pack("!HH", ACK, expected & 0xFFFF)
                    finished = len(data) < blk
                    expected += 1
                sock.sendto(packet, addr)                                  # the final ACK
            os.replace(part, path)
        except OSError as e:
            try:
                os.remove(part)
            except OSError:
                pass
            self.say(f"{who}: upload of {name} failed ({e})")
            return
        secs = max(time.time() - started, 0.001)
        self.say(f"{who}: received {name} ({total} bytes, {total / secs / 1024:.0f} KB/s)")


# ─── HTTP ────────────────────────────────────────────────────────────────────

class HttpServer:
    def __init__(self, root, port=8000, listing=True, bind="0.0.0.0", logger=None):
        self.root, self.port, self.listing, self.bind = os.path.abspath(root), port, listing, bind
        self.logger = logger or (lambda m: None)
        self.httpd = None
        self.running = False

    def say(self, msg):
        try:
            self.logger(msg)
        except Exception:
            pass

    def start(self):
        if not os.path.isdir(self.root):
            raise OSError(f"The folder {self.root} does not exist.")
        outer = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=outer.root, **k)

            def log_message(self, fmt, *args):
                outer.say(f"{self.client_address[0]}: {self.command} {urllib.parse.unquote(self.path)}  ->  {args[1] if len(args) > 1 else ''}")

            def list_directory(self, path):
                if not outer.listing:
                    self.send_error(403, "Folder listings are off")
                    return None
                return super().list_directory(path)

            def do_POST(self):
                # Read and drop the request body first: closing a socket that still holds unread data makes Windows
                # reset the connection, and the client then never sees the 405 answer.
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    size = 0
                if 0 < size <= 1_000_000:
                    try:
                        self.rfile.read(size)
                    except OSError:
                        pass
                self.send_error(405, "Read-only")

            do_PUT = do_DELETE = do_POST

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = os.name != "nt"

        self.httpd = Server((self.bind, self.port), Handler)
        self.port = self.httpd.server_address[1]
        self.running = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True, name="http-share").start()
        self.say(f"HTTP server listening on port {self.port}, folder {self.root}" + ("" if self.listing else " (no folder listings)"))

    def stop(self):
        was, self.running = self.running, False
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except OSError:
                pass
            self.httpd = None
        if was:
            self.say("HTTP server stopped")


# ─── the tab ─────────────────────────────────────────────────────────────────

class ServerPanel(ttk.LabelFrame):
    """One server's settings and Start/Stop button."""

    def __init__(self, parent, tab, title, default_port, kind):
        super().__init__(parent, text=f" {title} ", padding=10)
        self.tab, self.kind, self.server = tab, kind, None
        self.folder, self.port = tk.StringVar(), tk.StringVar(
            value=str(default_port if isinstance(default_port, int) else first_free(default_port, kind == "tftp")))
        self.auto = tk.StringVar(value="30")
        self.opt1, self.opt2 = tk.BooleanVar(value=(kind == "http")), tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Stopped")
        self.timer = None
        ttk.Label(self, text="Folder to serve").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.folder, width=46).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(self, text="Browse…", command=self.browse).grid(row=0, column=2)
        ttk.Label(self, text="Port").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.port, width=8).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(self, text="Stop by itself after (minutes, 0 = never)").grid(row=2, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.auto, width=8).grid(row=2, column=1, sticky="w", padx=6)
        if kind == "tftp":
            ttk.Checkbutton(self, text="Allow uploads (devices may send files here)", variable=self.opt1).grid(row=3, column=1, sticky="w")
            ttk.Checkbutton(self, text="…and allow overwriting existing files", variable=self.opt2).grid(row=4, column=1, sticky="w")
        else:
            ttk.Checkbutton(self, text="Show folder listings", variable=self.opt1).grid(row=3, column=1, sticky="w")
        self.btn = ttk.Button(self, text="▶ Start", command=self.toggle)
        self.btn.grid(row=5, column=0, pady=(8, 0), sticky="w")
        ttk.Label(self, textvariable=self.status, font=("Segoe UI", 9, "bold")).grid(row=5, column=1, columnspan=2, sticky="w", pady=(8, 0), padx=6)

    def browse(self):
        p = filedialog.askdirectory(parent=self, title="Folder to serve")
        if p:
            self.folder.set(p)

    def toggle(self):
        if self.server:
            self.stop()
        else:
            self.start()

    def start(self):
        folder = self.folder.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("Local servers", "Choose an existing folder to serve.", parent=self)
            return False
        try:
            port = int(self.port.get())
            if not 1 <= port <= 65535:
                raise ValueError
            minutes = float(self.auto.get() or 0)
            if minutes < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Local servers", "The port must be 1 to 65535 and the minutes a number (0 = never).", parent=self)
            return False
        logger = lambda m: self.tab.post(lambda: self.tab.write(m))
        srv = (TftpServer(folder, port, allow_write=self.opt1.get(), overwrite=self.opt2.get(), logger=logger) if self.kind == "tftp"
               else HttpServer(folder, port, listing=self.opt1.get(), logger=logger))
        try:
            srv.start()
        except OSError as e:
            messagebox.showerror("Local servers", f"Could not start on port {port}.\n\n{explain_bind_error(e, port)}", parent=self)
            return False
        self.server = srv
        self.btn.configure(text="■ Stop")
        addrs = ", ".join(f"{a}:{srv.port}" for a in local_addresses()[:3]) or f"port {srv.port}"
        self.status.set(f"Running - other devices use {addrs}")
        if minutes > 0:
            self.timer = self.after(int(minutes * 60000), self.auto_stop)
        return True

    def auto_stop(self):
        self.timer = None
        if self.server:
            self.tab.write(f"{self.kind.upper()} server stopped by its timer")
            self.stop()

    def stop(self):
        if self.timer:
            self.after_cancel(self.timer)
            self.timer = None
        if self.server:
            self.server.stop()
            self.server = None
        self.btn.configure(text="▶ Start")
        self.status.set("Stopped")


class LocalServicesTab(ttk.Frame):
    def __init__(self, app):
        super().__init__(app.nb, padding=12)
        self.app = app
        ttk.Label(self, text="Local servers (TFTP and HTTP)", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, foreground="#555", wraplength=900, justify="left", text=(
            "Temporary servers on this PC for devices that fetch files (a switch loading a config or firmware by TFTP, a camera, "
            "a router). While one runs, anyone on your network can read the folder you serve, so serve only what you mean to "
            "share and let the timer stop it. Windows may ask to allow it through its firewall the first time."
        )).pack(anchor="w", pady=(2, 8))
        self.tftp = ServerPanel(self, self, "TFTP server", 69, "tftp")
        self.tftp.pack(fill="x")
        self.http = ServerPanel(self, self, "HTTP server (read-only)", [8000, 8888, 9000, 9090, 8800, 18000, 28000], "http")
        self.http.pack(fill="x", pady=(8, 0))
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(8, 2))
        ttk.Label(head, text="What the servers are doing", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(head, text="Clear", command=self.clear).pack(side="right")
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.out = tk.Text(wrap, height=10, font=("Consolas", 9), wrap="none", state="disabled")
        sb = ttk.Scrollbar(wrap, command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.out.pack(side="left", fill="both", expand=True)

    def post(self, fn):
        self.app.post(fn)

    def write(self, msg):
        if not self.winfo_exists():
            return
        self.out.configure(state="normal")
        self.out.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
        self.out.see("end")
        self.out.configure(state="disabled")

    def clear(self):
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.configure(state="disabled")

    def close(self):
        for p in (self.tftp, self.http):
            p.stop()
