"""RC tools: quick-connect palette, key-login setup, SSH tunnels and snippets.

Imported by rc_manager when needed (this module imports rc_manager by name, so the app must be started
through rc_manager.main / RC.bat)."""

import base64
import os
import re
import select
import socket
import struct
import subprocess
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from tkinter import messagebox, ttk

import rc_manager as core

MAX_OUTPUT = 200_000


# ─── helpers shared by tunnels / snippets / key setup ────────────────────────

def auto_login(h):
    """(user, password) to use without asking: the saved login, else just the username (keys/agent)."""
    login = core.saved_login(h)
    if login:
        return login
    return (h["user"] or None), ""


def run_remote(client, command, timeout=60):
    """Run one command on an open SSH client. Returns (exit_status, stdout, stderr)."""
    chan = client.get_transport().open_session()
    chan.settimeout(timeout)
    chan.exec_command(command)
    out, err, deadline = [], [], time.time() + timeout
    size = 0
    while True:
        if chan.recv_ready():
            d = chan.recv(32768)
            if size < MAX_OUTPUT:
                out.append(d)
            size += len(d)
        elif chan.recv_stderr_ready():
            d = chan.recv_stderr(32768)
            if size < MAX_OUTPUT:
                err.append(d)
            size += len(d)
        elif chan.exit_status_ready():
            while chan.recv_ready():
                out.append(chan.recv(32768))
            while chan.recv_stderr_ready():
                err.append(chan.recv_stderr(32768))
            break
        else:
            if time.time() > deadline:
                chan.close()
                raise TimeoutError(f"no result after {timeout} seconds")
            time.sleep(0.03)
    code = chan.recv_exit_status()
    chan.close()
    return code, b"".join(out).decode("utf-8", "replace"), b"".join(err).decode("utf-8", "replace")


# ─── key login setup ─────────────────────────────────────────────────────────

def _slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "server"


def make_keypair(path, passphrase=""):
    """Create an ed25519 key pair in OpenSSH format. Returns the public key line."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    enc = ser.BestAvailableEncryption(passphrase.encode()) if passphrase else ser.NoEncryption()
    private = key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, enc)
    comment = "rc@" + _slug(socket.gethostname())
    public = key.public_key().public_bytes(ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH).decode() + " " + comment
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(private)
    with open(path + ".pub", "w") as f:
        f.write(public + "\n")
    try:  # ssh.exe refuses private keys other accounts can read
        subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
                       capture_output=True, creationflags=core.CREATE_NO_WINDOW, timeout=15)
    except Exception:
        pass
    return public


def public_line_for(path, passphrase=""):
    """Public key line for an existing private key file (uses the .pub next to it, else derives it)."""
    pub = path + ".pub"
    if os.path.isfile(pub):
        line = open(pub, "r", errors="ignore").read().strip().splitlines()[0]
        if line.startswith(("ssh-", "ecdsa-")):
            return line
    key = core.paramiko.PKey.from_path(path, passphrase.encode() if passphrase else None)
    return f"{key.get_name()} {key.get_base64()} rc@{_slug(socket.gethostname())}"


def _ps_encoded(script):
    return "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16-le")).decode()


def install_commands(public_line, is_windows, is_admin):
    """The command that adds the key to the server's authorized keys (idempotent)."""
    pub = public_line.replace("'", "")
    if not is_windows:
        return ("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
                f"(grep -qxF '{pub}' ~/.ssh/authorized_keys || printf '%s\\n' '{pub}' >> ~/.ssh/authorized_keys) && "
                "chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && chmod go-w ~ && echo RC_OK")
    body = pub.split()[1]
    if is_admin:  # administrators on Windows OpenSSH use one shared file
        target = "$p = Join-Path $env:ProgramData 'ssh\\administrators_authorized_keys'; $grant = 'Administrators:F','SYSTEM:F'"
        mk = ""
    else:
        target = ("$d = Join-Path $env:USERPROFILE '.ssh'; New-Item -ItemType Directory -Force -Path $d | Out-Null; "
                  "$p = Join-Path $d 'authorized_keys'; $grant = ($env:USERNAME + ':F'),'SYSTEM:F'")
        mk = ""
    script = (target + mk + "; if (-not (Test-Path $p)) { New-Item -ItemType File -Force -Path $p | Out-Null }; "
              f"if (-not (Select-String -Path $p -SimpleMatch -Quiet -Pattern '{body}')) {{ Add-Content -Path $p -Value '{pub}' }}; "
              "icacls $p /inheritance:r | Out-Null; foreach ($g in $grant) { icacls $p /grant $g | Out-Null }; 'RC_OK'")
    return _ps_encoded(script)


def setup_key_login(app, host, user, password, passphrase, log):
    """Create/choose a key, install it on the server, prove it works. Returns the private key path or None."""
    if core.paramiko is None:
        raise RuntimeError("paramiko is not installed")
    key_path = host.get("key_file") or os.path.join(os.path.expanduser("~"), ".ssh", "rc_" + _slug(host["name"]))
    if os.path.isfile(key_path):
        log(f"Using your existing key {key_path}")
        pub = public_line_for(key_path, passphrase)
    else:
        log(f"Creating a new ed25519 key at {key_path}")
        pub = make_keypair(key_path, passphrase)
    log(f"Logging in to {host['address']} as {user} to install the public key ...")
    client = core.open_ssh_client(app, host, user, password)
    try:
        code, out, _ = run_remote(client, "uname -s", 20)
        is_windows = code != 0 or not out.strip()
        is_admin = False
        if is_windows:
            code, out, _ = run_remote(client, "cmd /c ver", 20)
            if "windows" not in out.lower():
                raise RuntimeError("Could not tell what kind of server this is (not Linux/Unix or Windows).")
            check = _ps_encoded("([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
                                ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)")
            is_admin = run_remote(client, check, 30)[1].strip().lower() == "true"
            log("Windows server" + (" (administrator account: using administrators_authorized_keys)" if is_admin else ""))
        else:
            log(f"{out.strip()} server")
        code, out, err = run_remote(client, install_commands(pub, is_windows, is_admin), 60)
        if "RC_OK" not in out:
            raise RuntimeError("The server did not accept the key install: " + (err.strip() or out.strip() or f"exit {code}")[:200])
    finally:
        client.close()
    log("Key installed. Testing a login with the key only ...")
    test = dict(host, key_file=key_path)
    c2 = core.open_ssh_client(app, test, user, passphrase, key_only=True)
    c2.close()
    log("✔ Key login works.")
    return key_path


# ─── SSH tunnels ─────────────────────────────────────────────────────────────

def pipe(sock_a, sock_b):
    """Copy bytes both ways between two socket-like objects until either closes."""
    def pump(src, dst):
        try:
            while True:
                data = src.recv(32768)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            for x in (src, dst):
                try:
                    x.close()
                except Exception:
                    pass
    threading.Thread(target=pump, args=(sock_a, sock_b), daemon=True).start()
    threading.Thread(target=pump, args=(sock_b, sock_a), daemon=True).start()


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            raise ConnectionError("closed")
        buf += d
    return buf


def socks5_handshake(conn):
    """Minimal SOCKS5 (no auth, CONNECT only). Returns (host, port) the client wants."""
    ver, n = _recv_exact(conn, 2)
    _recv_exact(conn, n)
    if ver != 5:
        raise ConnectionError("not SOCKS5")
    conn.sendall(b"\x05\x00")
    ver, cmd, _, atyp = _recv_exact(conn, 4)
    if cmd != 1:
        conn.sendall(b"\x05\x07\x00\x01" + b"\0" * 6)
        raise ConnectionError("only CONNECT is supported")
    if atyp == 1:
        host = socket.inet_ntoa(_recv_exact(conn, 4))
    elif atyp == 3:
        host = _recv_exact(conn, _recv_exact(conn, 1)[0]).decode()
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(conn, 16))
    else:
        raise ConnectionError("bad address type")
    port = struct.unpack("!H", _recv_exact(conn, 2))[0]
    return host, port


class TunnelRunner:
    """One running tunnel: its own SSH connection plus a listener."""

    def __init__(self, app, host, spec):
        self.app, self.host, self.spec = app, host, spec
        self.state, self.detail = "starting", ""
        self.client = self.listener = None
        self.stopping = False
        self.conns = 0
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        s = self.spec
        try:
            user, pw = auto_login(self.host)
            if not user:
                raise RuntimeError("no username saved for this server")
            self.client = core.open_ssh_client(self.app, self.host, user, pw)
            tr = self.client.get_transport()
            tr.set_keepalive(30)
            bind = s.get("bind") or "127.0.0.1"
            port = int(s["listen_port"])
            if s["kind"] == "remote":
                tr.request_port_forward("" if bind in ("127.0.0.1", "localhost") else bind, port, handler=self.on_remote)
                self.state, self.detail = "running", f"server port {port} -> {s['dest_host']}:{s['dest_port']}"
            else:
                self.listener = socket.socket()
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listener.bind((bind, port))
                self.listener.listen(20)
                self.state = "running"
                self.detail = (f"{bind}:{port} -> {s['dest_host']}:{s['dest_port']}" if s["kind"] == "local"
                               else f"SOCKS proxy on {bind}:{port}")
                threading.Thread(target=self.accept_loop, daemon=True).start()
            while not self.stopping and tr.is_active():
                time.sleep(0.5)
            if not self.stopping:
                self.state, self.detail = "error", "the SSH connection dropped"
        except Exception as e:
            if not self.stopping:
                self.state, self.detail = "error", (str(e) or e.__class__.__name__)[:120]
        finally:
            self.cleanup()

    def accept_loop(self):
        while not self.stopping:
            try:
                conn, addr = self.listener.accept()
            except OSError:
                break
            threading.Thread(target=self.handle, args=(conn, addr), daemon=True).start()

    def handle(self, conn, addr):
        try:
            tr = self.client.get_transport()
            if self.spec["kind"] == "dynamic":
                dest = socks5_handshake(conn)
            else:
                dest = (self.spec["dest_host"], int(self.spec["dest_port"]))
            chan = tr.open_channel("direct-tcpip", dest, addr)
            if self.spec["kind"] == "dynamic":
                conn.sendall(b"\x05\x00\x00\x01" + b"\0" * 6)
            self.conns += 1
            pipe(conn, chan)
        except Exception:
            try:
                if self.spec["kind"] == "dynamic":
                    conn.sendall(b"\x05\x05\x00\x01" + b"\0" * 6)
            except Exception:
                pass
            conn.close()

    def on_remote(self, chan, origin, server):
        try:
            sock = socket.create_connection((self.spec["dest_host"], int(self.spec["dest_port"])), 10)
            self.conns += 1
            pipe(sock, chan)
        except Exception:
            chan.close()

    def stop(self):
        self.stopping = True
        self.cleanup()
        if self.state != "error":
            self.state, self.detail = "stopped", ""

    def cleanup(self):
        for c in (self.listener, self.client):
            try:
                c and c.close()
            except Exception:
                pass
        self.listener = None


class TunnelManager:
    def __init__(self, app):
        self.app, self.running = app, {}

    @staticmethod
    def key(host, spec):
        return f"{host['id']}:{spec['id']}"

    def start(self, host, spec):
        k = self.key(host, spec)
        if k in self.running and self.running[k].state in ("starting", "running"):
            return
        self.running[k] = TunnelRunner(self.app, host, spec)

    def stop(self, host, spec):
        r = self.running.get(self.key(host, spec))
        if r:
            r.stop()

    def status(self, host, spec):
        r = self.running.get(self.key(host, spec))
        return (r.state, r.detail, r.conns) if r else ("stopped", "", 0)

    def stop_all(self):
        for r in list(self.running.values()):
            r.stop()


KIND_LABEL = {"local": "Local (a port here reaches a service there)",
              "remote": "Remote (a port there reaches a service here)",
              "dynamic": "Dynamic (SOCKS proxy through the server)"}
KIND_SHORT = {"local": "Local", "remote": "Remote", "dynamic": "SOCKS"}


class TunnelsTab(ttk.Frame):
    def __init__(self, app):
        super().__init__(app.nb, padding=12)
        self.app = app
        ttk.Label(self, text="SSH tunnels", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, foreground="#555", wraplength=760, justify="left",
                  text="A tunnel carries traffic through a server you can SSH into. Example: Local tunnel, listen 8081, "
                       "destination 10.0.0.20:5000 - then http://127.0.0.1:8081 opens that NAS page through the "
                       "server. Tunnels use the server's saved login or SSH key.").pack(anchor="w", pady=(2, 8))
        cols = ("server", "kind", "listen", "dest", "state")
        self.tree = ttk.Treeview(self, columns=cols, height=8, selectmode="browse")
        for c, t, w in (("#0", "Name", 160), ("server", "Through server", 150), ("kind", "Type", 70),
                        ("listen", "Listen", 150), ("dest", "Destination", 170), ("state", "Status", 330)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.load_form())
        self.tree.tag_configure("running", foreground="#1a7f37")
        self.tree.tag_configure("error", foreground="#b3261e")
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=6)
        for text, cmd in (("▶ Start", self.start), ("■ Stop", self.stop), ("🌐 Open in browser", self.open_browser),
                          ("🗑 Remove", self.remove)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))

        form = ttk.LabelFrame(self, text=" Add or change a tunnel ", padding=8)
        form.pack(fill="x", pady=(8, 0))
        self.v = {k: tk.StringVar() for k in ("server", "name", "kind", "bind", "listen", "dhost", "dport")}
        self.v["kind"].set(KIND_LABEL["local"])
        self.v["bind"].set("127.0.0.1")
        rows = [("Through server", "server"), ("Name", "name"), ("Type", "kind"), ("Listen on (address)", "bind"),
                ("Listen port", "listen"), ("Destination host", "dhost"), ("Destination port", "dport")]
        for r, (label, key) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=2, padx=(0, 10))
            if key == "server":
                self.server_box = ttk.Combobox(form, textvariable=self.v[key], state="readonly", width=44)
                w = self.server_box
            elif key == "kind":
                w = ttk.Combobox(form, textvariable=self.v[key], state="readonly", width=44,
                                 values=[KIND_LABEL[k] for k in core.TUNNEL_KINDS])
            else:
                w = ttk.Entry(form, textvariable=self.v[key], width=46 if key != "listen" else 10)
            w.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(form, foreground="#777", wraplength=520, justify="left",
                  text="Dynamic tunnels need no destination (point a browser's SOCKS5 proxy at the listen address). "
                       "Use 127.0.0.1 to keep the tunnel private to this PC.").grid(row=len(rows), column=0, columnspan=2,
                                                                                   sticky="w", pady=(4, 0))
        fb = ttk.Frame(form)
        fb.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(fb, text="💾 Save tunnel", command=self.save).pack(side="left")
        ttk.Button(fb, text="Clear form", command=self.clear_form).pack(side="left", padx=6)
        self.msg = tk.StringVar()
        ttk.Label(fb, textvariable=self.msg, foreground="#b3261e").pack(side="left", padx=10)
        self.editing = None
        self.refresh()
        self.tick()

    # data
    def hosts_by_label(self):
        return {f"{h['group'] or 'Ungrouped'}  ›  {h['name']}  ({h['address']})": h
                for h in sorted(self.app.hosts, key=lambda h: (h["group"].lower(), h["name"].lower()))}

    def all_specs(self):
        return [(h, t) for h in self.app.hosts for t in h["tunnels"]]

    def refresh(self):
        self.server_box["values"] = list(self.hosts_by_label())
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for h, t in self.all_specs():
            state, detail, n = self.app.tunnel_mgr.status(h, t)
            dest = "SOCKS proxy" if t["kind"] == "dynamic" else f"{t['dest_host']}:{t['dest_port']}"
            text = {"running": f"● running ({n} connections)", "starting": "… starting", "stopped": "stopped",
                    "error": f"✖ {detail}"}[state]
            self.tree.insert("", "end", iid=f"{h['id']}:{t['id']}", text=t["name"], values=(
                h["name"], KIND_SHORT[t["kind"]], f"{t.get('bind') or '127.0.0.1'}:{t['listen_port']}", dest, text),
                tags=(state,))
        for s in sel:
            if self.tree.exists(s):
                self.tree.selection_set(s)

    def tick(self):
        try:
            if self.winfo_exists():
                self.refresh()
                self.after(900, self.tick)
        except tk.TclError:
            pass

    def current(self):
        sel = self.tree.selection()
        if not sel:
            return None
        hid, tid = sel[0].split(":")
        for h, t in self.all_specs():
            if h["id"] == hid and t["id"] == tid:
                return h, t
        return None

    # form
    def load_form(self):
        cur = self.current()
        if not cur:
            return
        h, t = cur
        self.editing = (h["id"], t["id"])
        self.v["server"].set(next((l for l, x in self.hosts_by_label().items() if x["id"] == h["id"]), ""))
        self.v["name"].set(t["name"])
        self.v["kind"].set(KIND_LABEL[t["kind"]])
        self.v["bind"].set(t.get("bind") or "127.0.0.1")
        self.v["listen"].set(str(t["listen_port"]))
        self.v["dhost"].set(t.get("dest_host", ""))
        self.v["dport"].set(str(t.get("dest_port") or ""))

    def clear_form(self):
        self.editing = None
        for k, var in self.v.items():
            var.set("")
        self.v["kind"].set(KIND_LABEL["local"])
        self.v["bind"].set("127.0.0.1")
        self.msg.set("")

    def save(self):
        h = self.hosts_by_label().get(self.v["server"].get())
        kind = next((k for k in core.TUNNEL_KINDS if KIND_LABEL[k] == self.v["kind"].get()), "local")
        name, bind = self.v["name"].get().strip(), self.v["bind"].get().strip() or "127.0.0.1"
        dhost, dport, listen = self.v["dhost"].get().strip(), self.v["dport"].get().strip(), self.v["listen"].get().strip()
        err = None
        if not h:
            err = "Pick the server to tunnel through."
        elif not (listen.isdigit() and 1 <= int(listen) <= 65535):
            err = "Listen port must be a number from 1 to 65535."
        elif kind != "dynamic" and not core.valid_address(dhost):
            err = "Destination host must be a valid IP address or DNS name."
        elif kind != "dynamic" and not (dport.isdigit() and 1 <= int(dport) <= 65535):
            err = "Destination port must be a number from 1 to 65535."
        elif not (core.valid_address(bind) or bind in ("localhost",)):
            err = "Listen address must be an IP like 127.0.0.1 (this PC only) or 0.0.0.0 (everyone on your network)."
        if err:
            self.msg.set(err)
            return
        if bind not in ("127.0.0.1", "localhost", "::1") and not messagebox.askyesno(
                "Tunnel", f"Listening on {bind} lets other computers on your network use this tunnel.\n\nContinue?", parent=self):
            return
        spec = {"id": uuid.uuid4().hex[:8], "name": name or f"{KIND_SHORT[kind]} {listen}", "kind": kind, "bind": bind,
                "listen_port": int(listen), "dest_host": dhost, "dest_port": int(dport) if dport else 0}
        if self.editing:
            old_h, old_t = next(((x, y) for x, y in self.all_specs() if (x["id"], y["id"]) == self.editing), (None, None))
            if old_h:
                self.app.tunnel_mgr.stop(old_h, old_t)
                old_h["tunnels"].remove(old_t)
                spec["id"] = old_t["id"]
        h["tunnels"].append(spec)
        core.save_hosts(self.app.hosts)
        self.clear_form()
        self.msg.set("")
        self.refresh()
        self.tree.selection_set(f"{h['id']}:{spec['id']}")

    def remove(self):
        cur = self.current()
        if cur and messagebox.askyesno("Tunnel", f"Remove tunnel “{cur[1]['name']}”?", parent=self):
            self.app.tunnel_mgr.stop(*cur)
            cur[0]["tunnels"].remove(cur[1])
            core.save_hosts(self.app.hosts)
            self.clear_form()
            self.refresh()

    def start(self):
        cur = self.current()
        if cur:
            self.app.tunnel_mgr.start(*cur)
            self.refresh()

    def stop(self):
        cur = self.current()
        if cur:
            self.app.tunnel_mgr.stop(*cur)
            self.refresh()

    def open_browser(self):
        cur = self.current()
        if cur and cur[1]["kind"] == "local":
            webbrowser.open(f"http://127.0.0.1:{cur[1]['listen_port']}")
        elif cur:
            messagebox.showinfo("Tunnel", "Only Local tunnels can be opened as a web page.", parent=self)

    def close(self):
        self.app.tunnels_tab = None


# ─── Snippets ────────────────────────────────────────────────────────────────

class KeysTab(ttk.Frame):
    """🔑 One key for this PC, installed on every server you pick. Keys never expire (no passphrase, no certificate
    lifetime). Run it once on each PC you connect from (home, work) - each PC gets its own key, nothing private is copied."""

    SKIP_GROUPS = {"devices"}  # cameras, TVs etc. rarely run SSH; not ticked by default

    def __init__(self, app):
        super().__init__(app.nb, padding=12)
        self.app = app
        self.busy = False
        ttk.Label(self, text="🔑 Keys for the whole lab", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, foreground="#555", wraplength=900, justify="left",
                  text="RC makes one ed25519 key for THIS PC (no passphrase, no expiry) and installs it on every server you "
                       "tick. You type each server's password once; after that RC logs in with the key - at home on the LAN, "
                       "or from anywhere over a VPN such as Tailscale (use the same addresses through a subnet router). "
                       "Working from a second PC? Open this tab there and run it too: every PC gets its own key.").pack(anchor="w", pady=(2, 8))
        kf = ttk.Frame(self)
        kf.pack(fill="x")
        ttk.Label(kf, text="Key file for this PC:").pack(side="left")
        self.key_path = tk.StringVar(value=os.path.join(os.path.expanduser("~"), ".ssh",
                                                         "rc_homelab_" + _slug(socket.gethostname())))
        ttk.Entry(kf, textvariable=self.key_path, width=56).pack(side="left", padx=6)
        ttk.Button(kf, text="📋 Copy public key", command=self.copy_pub).pack(side="left")
        self.tree = ttk.Treeview(self, columns=("addr", "user", "state"), height=10, selectmode="extended")
        for c, t, w in (("#0", "Server", 230), ("addr", "Address", 140), ("user", "User", 110), ("state", "Result", 420)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, pady=8)
        self.rows = sorted(app.hosts, key=lambda h: (h["group"].lower(), h["name"].lower()))
        for i, h in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i), text=f"{h['group'] or 'Ungrouped'}  ›  {h['name']}",
                             values=(h["address"], h["user"] or "?", "has key ✔" if self.has_key(h) else ""))
            if (h["group"] or "").lower() not in self.SKIP_GROUPS:
                self.tree.selection_add(str(i))
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.go = ttk.Button(bar, text="🔑 Set up keys on the selected servers", command=self.start)
        self.go.pack(side="left")
        ttk.Button(bar, text="Select all", command=lambda: self.tree.selection_set(self.tree.get_children())).pack(side="left", padx=6)
        self.status = tk.StringVar(value="Ctrl/Shift-click to change the selection.")
        ttk.Label(bar, textvariable=self.status, foreground="#555").pack(side="left", padx=8)

    @staticmethod
    def has_key(h):
        return bool(h.get("key_file")) and os.path.isfile(h["key_file"])

    def copy_pub(self):
        p = self.key_path.get().strip()
        try:
            line = public_line_for(p) if os.path.isfile(p) else None
        except Exception as e:
            line = None
        if not line:
            self.status.set("No key yet - it is created when you set up the first server.")
            return
        self.clipboard_clear()
        self.clipboard_append(line)
        self.status.set("Public key copied (safe to share; the private file never leaves this PC).")

    def set_state(self, iid, text, user=None):
        def do():
            if self.tree.exists(iid):
                self.tree.set(iid, "state", text)
                if user:
                    self.tree.set(iid, "user", user)
        self.app.post(do)

    def ask(self, title, prompt, secret=False):
        from tkinter import simpledialog
        return core.ui_call(self.app, lambda: simpledialog.askstring(title, prompt, show="*" if secret else None,
                                                                      parent=self.app.root))

    def start(self):
        if self.busy:
            return
        sel = [int(i) for i in self.tree.selection()]
        if not sel:
            self.status.set("Pick at least one server.")
            return
        path = self.key_path.get().strip()
        if not path:
            return
        self.busy = True
        self.go.state(["disabled"])
        threading.Thread(target=self.work, args=(sel, path), daemon=True).start()

    def work(self, sel, path):
        ok = fail = 0
        for n, i in enumerate(sel, 1):
            h = self.rows[i]
            iid = str(i)
            self.app.post(lambda n=n: self.status.set(f"Working: {n} of {len(sel)} ..."))
            try:
                user = h["user"]
                if user and os.path.isfile(path):  # already works with this PC's key? then nothing to do
                    try:
                        core.open_ssh_client(self.app, dict(h, key_file=path), user, "", key_only=True).close()
                        self.finish(h, path, user)
                        self.set_state(iid, "✔ already works with this PC's key")
                        ok += 1
                        continue
                    except Exception:
                        pass
                login = core.saved_login(h)
                if login:
                    user, pw = login
                else:
                    self.set_state(iid, "waiting for login ...")
                    user = user or self.ask(f"{h['name']}", f"Username for {h['name']} ({h['address']}):")
                    if not user:
                        self.set_state(iid, "skipped")
                        continue
                    pw = self.ask(f"{h['name']}", f"Password for {user}@{h['address']} (used once, not saved):", True)
                    if pw is None:
                        self.set_state(iid, "skipped")
                        continue
                self.set_state(iid, "installing ...", user)
                lines = []
                setup_key_login(self.app, dict(h, key_file=path), user, pw, "", lines.append)
                self.finish(h, path, user)
                self.set_state(iid, "✔ key works")
                ok += 1
            except Exception as e:
                fail += 1
                self.set_state(iid, "✖ " + (str(e) or e.__class__.__name__)[:200])
        self.app.post(lambda: self.done(ok, fail))

    def finish(self, h, path, user):
        def do():
            h["key_file"] = path
            if not h["user"]:
                h["user"] = user
            core.save_hosts(self.app.hosts)
        self.app.post(do)

    def done(self, ok, fail):
        self.busy = False
        self.go.state(["!disabled"])
        self.status.set(f"Done: {ok} working, {fail} failed. Failures keep their old login - fix and run again.")


class SnippetsTab(ttk.Frame):
    def __init__(self, app, preselect=None):
        super().__init__(app.nb, padding=12)
        self.app = app
        self.snips = core.load_snippets()
        self.cur = None
        ttk.Label(self, text="Snippets", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, foreground="#555", wraplength=900, justify="left",
                  text="Saved commands. Run one on any servers you pick (it connects with each server's saved login or key "
                       "and shows what came back), or paste one into an SSH terminal tab from its Snippets button.").pack(
            anchor="w", pady=(2, 8))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(left, width=28, height=9, exportselection=False)
        self.listbox.pack(fill="y")
        self.listbox.bind("<<ListboxSelect>>", lambda _: self.load())
        lb = ttk.Frame(left)
        lb.pack(fill="x", pady=4)
        ttk.Button(lb, text="➕ New", command=self.new).pack(side="left")
        ttk.Button(lb, text="🗑 Delete", command=self.delete).pack(side="left", padx=4)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.name = tk.StringVar()
        self.confirm = tk.BooleanVar(value=True)
        ttk.Label(right, text="Name").grid(row=0, column=0, sticky="w")
        ttk.Entry(right, textvariable=self.name, width=40).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(right, text="Command").grid(row=1, column=0, sticky="nw")
        self.cmd = tk.Text(right, width=70, height=4, font=("Consolas", 10))
        self.cmd.grid(row=1, column=1, sticky="we", pady=2)
        ttk.Checkbutton(right, text="Ask before running", variable=self.confirm).grid(row=2, column=1, sticky="w")
        ttk.Button(right, text="💾 Save snippet", command=self.save).grid(row=3, column=1, sticky="w", pady=4)

        run = ttk.LabelFrame(self, text=" Run on these servers ", padding=8)
        run.pack(fill="both", expand=True, pady=(8, 0))
        top = ttk.Frame(run)
        top.pack(fill="x")
        self.servers = tk.Listbox(top, selectmode="extended", height=6, width=44, exportselection=False)
        self.servers.pack(side="left")
        self.rows = sorted(app.hosts, key=lambda h: (h["group"].lower(), h["name"].lower()))
        for h in self.rows:
            self.servers.insert("end", f"{h['group'] or 'Ungrouped'}  ›  {h['name']}  ({h['address']})")
            if h["id"] == preselect:
                self.servers.selection_set("end")
        side = ttk.Frame(top)
        side.pack(side="left", padx=10, anchor="n")
        self.group_pick = ttk.Combobox(side, state="readonly", width=22,
                                       values=sorted({h["group"] or "Ungrouped" for h in self.rows}))
        self.group_pick.pack(anchor="w")
        ttk.Button(side, text="Select this group", command=self.select_group).pack(anchor="w", pady=2)
        ttk.Button(side, text="▶ Run snippet", command=self.run).pack(anchor="w", pady=(10, 0))
        self.status = tk.StringVar()
        ttk.Label(side, textvariable=self.status, foreground="#555", wraplength=220).pack(anchor="w", pady=4)
        res = ttk.Frame(run)
        res.pack(fill="both", expand=True, pady=(8, 0))
        self.results = ttk.Treeview(res, columns=("state",), height=5, selectmode="browse")
        self.results.heading("#0", text="Server")
        self.results.heading("state", text="Result")
        self.results.column("#0", width=200)
        self.results.column("state", width=170)
        self.results.pack(side="left", fill="y")
        self.results.bind("<<TreeviewSelect>>", self.show_output)
        self.out = tk.Text(res, height=8, font=("Consolas", 9), wrap="none", state="disabled")
        self.out.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.outputs = {}
        self.refill()

    # list
    def refill(self, select=None):
        self.listbox.delete(0, "end")
        for s in self.snips:
            self.listbox.insert("end", s["name"])
        if select is not None and 0 <= select < len(self.snips):
            self.listbox.selection_set(select)
            self.load()

    def load(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.cur = self.snips[sel[0]]
        self.name.set(self.cur["name"])
        self.cmd.delete("1.0", "end")
        self.cmd.insert("1.0", self.cur["command"])
        self.confirm.set(self.cur.get("confirm", True))

    def new(self):
        self.cur = None
        self.listbox.selection_clear(0, "end")
        self.name.set("")
        self.cmd.delete("1.0", "end")
        self.confirm.set(True)

    def save(self):
        name, command = self.name.get().strip(), self.cmd.get("1.0", "end").strip()
        if not name or not command:
            messagebox.showerror("Snippet", "Give the snippet a name and a command.", parent=self)
            return
        if self.cur is None:
            self.cur = {"id": uuid.uuid4().hex[:8]}
            self.snips.append(self.cur)
        self.cur.update(name=name, command=command, confirm=self.confirm.get())
        core.save_snippets(self.snips)
        self.refill(self.snips.index(self.cur))

    def delete(self):
        if self.cur in self.snips and messagebox.askyesno("Snippet", f"Delete “{self.cur['name']}”?", parent=self):
            self.snips.remove(self.cur)
            core.save_snippets(self.snips)
            self.new()
            self.refill()

    def select_group(self):
        g = self.group_pick.get()
        self.servers.selection_clear(0, "end")
        for i, h in enumerate(self.rows):
            if (h["group"] or "Ungrouped") == g:
                self.servers.selection_set(i)

    # run
    def run(self):
        command = self.cmd.get("1.0", "end").strip()
        targets = [self.rows[i] for i in self.servers.curselection()]
        if not command or not targets:
            messagebox.showinfo("Snippet", "Write a command and pick at least one server.", parent=self)
            return
        if self.confirm.get() and not messagebox.askyesno(
                "Run snippet", f"Run this on {len(targets)} server(s)?\n\n{command[:400]}", parent=self):
            return
        self.results.delete(*self.results.get_children())
        self.outputs.clear()
        self.status.set(f"Running on {len(targets)} server(s) ...")
        self.pending = len(targets)
        for h in targets:
            self.results.insert("", "end", iid=h["id"], text=h["name"], values=("running ...",))
            threading.Thread(target=self.run_one, args=(h, command), daemon=True).start()

    def run_one(self, h, command):
        try:
            user, pw = auto_login(h)
            if not user:
                raise RuntimeError("no username saved for this server")
            client = core.open_ssh_client(self.app, h, user, pw)
            try:
                code, out, err = run_remote(client, command, 60)
            finally:
                client.close()
            text = out + (("\n[stderr]\n" + err) if err.strip() else "")
            state = "✔ done" if code == 0 else f"✖ exit {code}"
        except Exception as e:
            text, state = str(e) or e.__class__.__name__, "✖ failed"
        self.app.post(lambda: self.finish_one(h["id"], state, text))

    def finish_one(self, hid, state, text):
        if not self.winfo_exists():
            return
        self.outputs[hid] = text
        if self.results.exists(hid):
            self.results.item(hid, values=(state,))
        self.pending -= 1
        self.status.set("Finished." if self.pending <= 0 else f"{self.pending} still running ...")
        if self.results.selection() == (hid,) or not self.results.selection():
            self.results.selection_set(hid)

    def show_output(self, _):
        sel = self.results.selection()
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        if sel:
            self.out.insert("1.0", self.outputs.get(sel[0], "(still running)"))
        self.out.configure(state="disabled")

    def close(self):
        self.app.snippets_tab = None


# ─── Quick connect palette ───────────────────────────────────────────────────

class QuickConnect(ttk.Frame):
    """Ctrl+K: type part of a server or connection name, press Enter."""

    def __init__(self, app):
        super().__init__(app.root, padding=8, relief="solid", borderwidth=1)
        self.app = app
        self.items = self.build_items()
        self.shown = []
        ttk.Label(self, text="Quick connect - type a server, a connection (ssh, web, files...) or a command",
                  foreground="#555").pack(anchor="w")
        self.q = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.q, width=70)
        self.entry.pack(fill="x", pady=4)
        self.lb = tk.Listbox(self, height=12, activestyle="dotbox", exportselection=False)
        self.lb.pack(fill="both", expand=True)
        self.q.trace_add("write", lambda *_: self.refilter())
        self.entry.bind("<Return>", lambda e: self.go())
        self.entry.bind("<Escape>", lambda e: self.close())
        self.entry.bind("<Down>", lambda e: self.move(1))
        self.entry.bind("<Up>", lambda e: self.move(-1))
        self.lb.bind("<Double-1>", lambda e: self.go())
        self.lb.bind("<Escape>", lambda e: self.close())
        self.place(relx=0.5, y=70, anchor="n", width=640)
        self.lift()
        self.refilter()
        self.entry.focus_set()

    def build_items(self):
        app, items = self.app, []
        for h in sorted(app.hosts, key=lambda h: (h["group"].lower(), h["name"].lower())):
            base = f"{h['group'] or 'Ungrouped'} {h['name']} {h['address']}"
            items.append((f"{h['name']}  ({h['address']})  -  select", base + " select open", ("select", h)))
            for c in core.connection_list(h):
                items.append((f"{h['name']}  ›  {self.app.conn_text(c)}", f"{base} {c['label']} {c['detail']} {c['action']}",
                              ("conn", h, c)))
            for t in h["tunnels"]:
                running = app.tunnel_mgr.status(h, t)[0] in ("running", "starting")
                items.append((f"{h['name']}  ›  Tunnel: {t['name']}  -  {'stop' if running else 'start'}",
                              f"{base} tunnel {t['name']} {t['kind']} {'stop' if running else 'start'}", ("tunnel", h, t)))
        for s in core.load_snippets():
            items.append((f"Snippet: {s['name']}  -  run on servers...", f"snippet {s['name']} run", ("snippet", s)))
        for label, key in (("Open the Tunnels tab", "tunnels"), ("Open the Snippets tab", "snippets"),
                           ("Add a server", "add"), ("Import servers from PuTTY / WinSCP / ssh config", "import")):
            items.append((f"Command: {label}", f"command {label}", ("cmd", key)))
        return items

    def refilter(self):
        tokens = self.q.get().lower().split()
        scored = []
        for text, hay, payload in self.items:
            low = hay.lower()
            if all(t in low for t in tokens):
                first = text.lower()
                score = sum(0 if first.startswith(t) else 1 for t in tokens) + (0 if payload[0] == "conn" else 0.5)
                scored.append((score, len(text), text, payload))
        scored.sort(key=lambda x: (x[0], x[1]))
        self.shown = scored[:14]
        self.lb.delete(0, "end")
        for _, _, text, _ in self.shown:
            self.lb.insert("end", text)
        if self.shown:
            self.lb.selection_set(0)

    def move(self, d):
        if not self.shown:
            return "break"
        cur = self.lb.curselection()
        i = max(0, min(len(self.shown) - 1, (cur[0] if cur else 0) + d))
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(i)
        self.lb.see(i)
        return "break"

    def go(self):
        cur = self.lb.curselection()
        if not cur:
            return
        payload = self.shown[cur[0]][3]
        self.close()
        app, kind = self.app, payload[0]
        if kind == "select":
            app.select_host(payload[1]["id"])
        elif kind == "conn":
            app.connect_to(payload[1], payload[2])
        elif kind == "tunnel":
            h, t = payload[1], payload[2]
            if app.tunnel_mgr.status(h, t)[0] in ("running", "starting"):
                app.tunnel_mgr.stop(h, t)
            else:
                app.tunnel_mgr.start(h, t)
            app.status.set(f"Tunnel “{t['name']}” toggled")
        elif kind == "snippet":
            app.open_snippets()
        elif kind == "cmd":
            {"tunnels": app.open_tunnels, "snippets": app.open_snippets, "add": app.add_host,
             "import": app.import_sessions}[payload[1]]()

    def close(self):
        self.app.palette = None
        self.destroy()
        self.app.root.focus_set()


# ─── Import from PuTTY / WinSCP / ssh config ─────────────────────────────────

def _unquote(name):
    from urllib.parse import unquote
    return unquote(name)


def parse_ssh_config(path):
    out, cur = [], None
    try:
        lines = open(path, "r", encoding="utf-8", errors="ignore").read().splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s+|\s*=\s*", line, maxsplit=1)
        if len(parts) < 2:
            continue
        key, val = parts[0].lower(), parts[1].strip().strip('"')
        if key == "host":
            names = [n for n in val.split() if not re.search(r"[*?!]", n)]
            cur = {"name": names[0], "address": names[0], "user": "", "port": 22, "key": "", "source": "ssh config"} if names else None
            if cur:
                out.append(cur)
        elif cur is not None:
            if key == "hostname":
                cur["address"] = val
            elif key == "user":
                cur["user"] = val
            elif key == "port" and val.isdigit():
                cur["port"] = int(val)
            elif key == "identityfile":
                cur["key"] = os.path.expanduser(val)
    return out


def _reg_sessions(root_path, source, winscp):
    import winreg
    out = []
    try:
        base = winreg.OpenKey(winreg.HKEY_CURRENT_USER, root_path)
    except OSError:
        return out
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(base, i)
        except OSError:
            break
        i += 1
        if sub == "Default%20Settings":
            continue
        try:
            k = winreg.OpenKey(base, sub)

            def val(n, default=""):
                try:
                    return winreg.QueryValueEx(k, n)[0]
                except OSError:
                    return default
            host = val("HostName")
            if not host:
                continue
            name = _unquote(sub)
            group = ""
            if "/" in name:
                group, name = name.rsplit("/", 1)
            proto = val("FSProtocol", 1) if winscp else val("Protocol", "ssh")
            item = {"name": name, "address": host, "user": val("UserName"), "group": group, "key": "", "source": source,
                    "note": ""}
            if winscp:
                if proto in (6, 7):
                    continue  # WebDAV / S3: nothing RC can open
                if proto == 5:
                    item["mode"], item["port"] = ("FTPS" if val("Ftps", 0) else "FTP"), int(val("PortNumber", 21))
                else:
                    item["mode"], item["port"] = "SFTP", int(val("PortNumber", 22))
            else:
                if proto != "ssh":
                    continue
                item["mode"], item["port"] = "SFTP", int(val("PortNumber", 22))
            ppk = val("PublicKeyFile")
            if ppk:
                item["note"] = f"Used PuTTY key {ppk} (convert it to OpenSSH format to use it here)."
            out.append(item)
        except OSError:
            continue
    return out


def import_candidates():
    found = parse_ssh_config(os.path.join(os.path.expanduser("~"), ".ssh", "config"))
    found += _reg_sessions(r"Software\SimonTatham\PuTTY\Sessions", "PuTTY", False)
    found += _reg_sessions(r"Software\Martin Prikryl\WinSCP 2\Sessions", "WinSCP", True)
    return found


class ImportTab(ttk.Frame):
    def __init__(self, app, candidates=None):
        super().__init__(app.nb, padding=12)
        self.app = app
        self.items = candidates if candidates is not None else import_candidates()
        have = {h["address"].lower() for h in app.hosts}
        ttk.Label(self, text="Import servers", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, foreground="#555", wraplength=760, justify="left",
                  text="Servers found in your PuTTY, WinSCP and ssh config. Passwords stored by those programs can't be "
                       "read (they're encrypted for them), so you'll enter a password once or use a key. Servers already in "
                       "RC (same address) are marked.").pack(anchor="w", pady=(2, 8))
        self.tree = ttk.Treeview(self, columns=("source", "address", "user", "port", "status"), selectmode="extended", height=14)
        for c, t, w in (("#0", "Name", 180), ("source", "From", 80), ("address", "Address", 190), ("user", "User", 90),
                        ("port", "Port", 50), ("status", "", 160)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True)
        for i, it in enumerate(self.items):
            dup = it["address"].lower() in have
            self.tree.insert("", "end", iid=str(i), text=it["name"], values=(
                it["source"], it["address"], it["user"], it["port"], "already in RC" if dup else "new"))
            if not dup:
                self.tree.selection_add(str(i))
        if not self.items:
            ttk.Label(self, text="Nothing found: no ~/.ssh/config entries and no saved PuTTY or WinSCP sessions.").pack(
                anchor="w", pady=8)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=8)
        ttk.Button(bar, text="Import selected", command=self.do_import).pack(side="left")
        ttk.Button(bar, text="Cancel", command=lambda: app.close_widget(self)).pack(side="left", padx=6)

    def do_import(self):
        added = 0
        for iid in self.tree.selection():
            it = self.items[int(iid)]
            h = dict(core.NEW_HOST, id=uuid.uuid4().hex, name=it["name"], address=it["address"], user=it["user"],
                     group=it.get("group", "") or it["source"], ssh_port=it["port"] if it.get("mode", "SFTP") == "SFTP" else 22,
                     file_mode=it.get("mode", "SFTP"), file_port=0 if it.get("mode", "SFTP") == "SFTP" else it["port"],
                     key_file=it.get("key", "") if os.path.isfile(it.get("key", "") or "") else "",
                     notes=(f"Imported from {it['source']}. " + it.get("note", "")).strip(), web_url=f"http://{it['address']}")
            if core.validate_host(h, self.app.hosts)[0]:
                continue
            self.app.hosts.append(h)
            added += 1
        core.save_hosts(self.app.hosts)
        self.app.refill()
        self.app.status.set(f"Imported {added} server(s)")
        self.app.close_widget(self)


# ─── Trusted SSH host keys (known_hosts manager) ─────────────────────────────

class KnownHostsTab(ttk.Frame):
    """Lists every server key RC has trusted and lets you forget one. Needed after a server is rebuilt: its new key
    no longer matches, RC refuses to connect, and the fix is to forget the old key here and trust the new one."""

    def __init__(self, app):
        super().__init__(app.nb, padding=12)
        self.app = app
        ttk.Label(self, text="Trusted host keys", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, foreground="#555", wraplength=760, justify="left",
                  text="The first time you connect to a server over SSH you are asked to trust its key; RC remembers it here "
                       "and refuses to connect if the key ever changes (a rebuilt server, or someone pretending to be it). "
                       "If you know a server was rebuilt, select it and click Forget - the next connection asks again."
                  ).pack(anchor="w", pady=(2, 8))
        self.tree = ttk.Treeview(self, columns=("type", "fp"), height=14, selectmode="extended")
        for c, t, w in (("#0", "Server", 220), ("type", "Key type", 110), ("fp", "Fingerprint (SHA256)", 480)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=6)
        for text, cmd in (("🗑 Forget selected", self.forget), ("📋 Copy fingerprint", self.copy_fp), ("⟳ Refresh", self.refill)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        self.status = tk.StringVar()
        ttk.Label(bar, textvariable=self.status, foreground="#555").pack(side="left", padx=8)
        self.rows = []
        self.refill()

    def refill(self):
        self.tree.delete(*self.tree.get_children())
        self.rows = core.list_known_hosts()
        for i, r in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i), text=r["host"], values=(r["type"], r["fp"]))
        self.status.set(f"{len(self.rows)} trusted key(s)" if self.rows else "No keys are trusted yet.")

    def selected(self):
        return [self.rows[int(i)] for i in self.tree.selection()]

    def forget(self):
        sel = self.selected()
        if not sel:
            self.status.set("Select one or more servers first.")
            return
        names = sorted({r["host"] for r in sel})
        if not messagebox.askyesno("Forget key", "Forget the trusted key for:\n\n  " + "\n  ".join(names) +
                                   "\n\nThe next connection will ask whether to trust the server again.", parent=self):
            return
        n = sum(core.remove_known_host(r["host"], r["type"]) for r in sel)
        self.refill()
        self.status.set(f"Forgot {n} key(s).")

    def copy_fp(self):
        sel = self.selected()
        if sel:
            self.clipboard_clear()
            self.clipboard_append(sel[0]["fp"])
            self.status.set("Fingerprint copied.")


# ─── README as a web page (Help > Quick start guide) ─────────────────────────

def _inline(text):
    """Markdown inline styles (code, bold, italic, links) -> HTML, with everything else escaped."""
    import html
    codes = []

    def keep(m):
        codes.append("<code>" + html.escape(m.group(1)) + "</code>")
        return "\x00%d\x00" % (len(codes) - 1)

    p = re.sub(r"`([^`]+)`", keep, text)  # code spans first, so ** around them still works
    p = html.escape(p, quote=False)
    p = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', p)
    p = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", p)
    p = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", p)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], p)


def _heading_id(text, used):
    s = re.sub(r"[^a-z0-9]+", "-", re.sub(r"[`*_]", "", text).lower()).strip("-") or "section"
    base, n = s, 2
    while s in used:
        s, n = f"{base}-{n}", n + 1
    used.add(s)
    return s


_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")


def markdown_to_html(md, title="README", subtitle=""):
    """A small Markdown converter for RC's own README: headings, tables, bullet/numbered lists, code blocks,
    rules, paragraphs and inline styles. Returns a complete, self-contained HTML page (light/dark aware,
    with a contents sidebar). No outside files or internet needed."""
    import html
    lines, body, toc, used = md.splitlines(), [], [], set()
    i = 0
    para = []

    def flush_para():
        if para:
            body.append("<p>" + _inline(" ".join(s.strip() for s in para)) + "</p>")
            para.clear()

    def cells(s):
        return [c.strip() for c in s.strip().strip("|").split("|")]

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush_para()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            body.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush_para()
            level, text = len(m.group(1)), m.group(2).strip()
            sid = _heading_id(text, used)
            if level == 2:
                toc.append((sid, re.sub(r"[`*]", "", text)))
            body.append(f'<h{level} id="{sid}">{_inline(text)}</h{level}>')
            i += 1
            continue
        if re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            flush_para()
            body.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            flush_para()
            head = cells(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            t = ["<div class='tablewrap'><table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                 + "</tr></thead><tbody>"]
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            body.append("".join(t) + "</tbody></table></div>")
            continue
        lm = _LIST_ITEM.match(line)
        if lm:
            flush_para()
            ordered = lm.group(2)[0].isdigit()
            items = []
            while i < len(lines):
                lm = _LIST_ITEM.match(lines[i])
                if lm:
                    items.append([lm.group(3)])
                    i += 1
                elif items and lines[i].strip() and lines[i].startswith(" "):
                    items[-1].append(lines[i].strip())  # a wrapped line of the same item
                    i += 1
                else:
                    break
            tag = "ol" if ordered else "ul"
            body.append(f"<{tag}>" + "".join("<li>" + _inline(" ".join(it)) + "</li>" for it in items) + f"</{tag}>")
            continue
        if not line.strip():
            flush_para()
        else:
            para.append(line)
        i += 1
    flush_para()
    nav = "".join(f'<a href="#{sid}">{html.escape(t)}</a>' for sid, t in toc)
    return (_README_PAGE.replace("{{TITLE}}", html.escape(title)).replace("{{SUB}}", html.escape(subtitle))
            .replace("{{NAV}}", nav).replace("{{BODY}}", "\n".join(body)))


_README_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root{--bg:#f4f7fc;--card:#fff;--text:#1b2433;--muted:#5b6678;--accent:#1d4ed8;--accent2:#0e7490;--line:#dbe3f0;--code:#eef2fa;--head:#0b1f4d;--headtext:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#0d1420;--card:#151e2e;--text:#e4eaf5;--muted:#93a1b8;--accent:#6ea8ff;--accent2:#5cd0e6;--line:#26334a;--code:#1c2840;--head:#0a1530;--headtext:#fff}}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:16px}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.65 "Segoe UI",system-ui,sans-serif}
header{background:linear-gradient(135deg,var(--head),#1d4ed8);color:var(--headtext);padding:28px 32px}
header h1{margin:0;font-size:1.9rem;letter-spacing:.3px}header p{margin:6px 0 0;opacity:.85}
.layout{display:flex;gap:28px;max-width:1280px;margin:0 auto;padding:24px 20px 60px}
nav{position:sticky;top:16px;align-self:flex-start;width:250px;flex:none;max-height:calc(100vh - 32px);overflow:auto;
 background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
nav b{display:block;margin:2px 8px 8px;color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:1px}
nav a{display:block;padding:6px 10px;border-radius:8px;color:var(--text);text-decoration:none;font-size:.9rem}
nav a:hover{background:var(--code);color:var(--accent)}
main{flex:1;min-width:0;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px 36px 36px}
main>h1:first-child{display:none}
h2{margin:2.2rem 0 .6rem;padding-bottom:.35rem;border-bottom:2px solid var(--line);color:var(--accent);font-size:1.45rem}
h3{margin:1.6rem 0 .4rem;color:var(--accent2)}h4{margin:1.2rem 0 .3rem}
p,li{max-width:80ch}li{margin:.3rem 0}
a{color:var(--accent)}
code{background:var(--code);border:1px solid var(--line);padding:1px 6px;border-radius:6px;font:.88em Consolas,monospace}
pre{background:var(--code);border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow:auto}
pre code{background:none;border:0;padding:0}
.tablewrap{overflow-x:auto;margin:1rem 0;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:.93rem}
th{background:var(--code);text-align:left;padding:10px 14px;border-bottom:2px solid var(--line)}
td{padding:9px 14px;border-top:1px solid var(--line);vertical-align:top}
tr:hover td{background:var(--code)}
hr{border:0;border-top:1px solid var(--line);margin:2rem 0}
footer{text-align:center;color:var(--muted);font-size:.85rem;padding:0 0 30px}
@media (max-width:860px){.layout{display:block}nav{position:static;width:auto;margin-bottom:16px;max-height:220px}main{padding:4px 18px 24px}}
@media print{nav{display:none}body{background:#fff}}
</style></head><body>
<header><h1>{{TITLE}}</h1><p>{{SUB}}</p></header>
<div class="layout"><nav><b>Contents</b>{{NAV}}</nav><main>
{{BODY}}
</main></div>
<footer>REGTeches &middot; Ronald Goodchild &middot; Jacksonville, FL</footer>
</body></html>
"""


def open_readme_html(app_dir, title="REGTeches RC", subtitle=""):
    """Turn README.md (next to the program) into a web page and open it in the default browser, which
    brings it to the front. Returns False if there is no README to show."""
    import tempfile
    path = os.path.join(app_dir, "README.md")
    if not os.path.isfile(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        page = markdown_to_html(f.read(), title, subtitle)
    out = os.path.join(tempfile.gettempdir(), "REGTeches_RC_guide.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    webbrowser.open("file:///" + out.replace("\\", "/"))
    return True
