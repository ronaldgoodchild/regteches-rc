"""Remote Connection Manager (RC).

One window that holds every server you connect to (IP or DNS name), grouped and
searchable, with a click for each way in: SSH and SFTP/FTP inside the app, plus
Remote Desktop, remote PowerShell, web, mail and ping using the programs Windows
already has.

Hosts live in hosts.json next to this file. Saved passwords are encrypted with the
Windows account that saved them (DPAPI) and cannot be read on another PC or user.
"""

import base64
import calendar
import csv
import ctypes
import hashlib
import ipaddress
import json
import logging
import logging.handlers
import os
import posixpath
import queue
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse
import webbrowser
from ctypes import wintypes
from ftplib import FTP, FTP_TLS, error_perm
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import paramiko
except ImportError:  # shown as a friendly message when SSH/SFTP is used
    paramiko = None

APP_NAME = "Remote Connection Manager"
APP_VERSION = "2.0.2"
COMPANY = "REGTeches"
AUTHOR = "Ronald Goodchild"
FULL_TITLE = f"{COMPANY} {APP_NAME} v{APP_VERSION}  —  Developed by {AUTHOR}"
if os.environ.get("RC_DATA_DIR"):  # demo / portable override: keep servers, settings and keys in this folder
    APP_DIR = os.path.abspath(os.environ["RC_DATA_DIR"])
    os.makedirs(APP_DIR, exist_ok=True)
elif getattr(sys, "frozen", False):  # the .exe: data files live next to it
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Where the program itself lives (README, screenshots, license): not the same as the data folder when RC_DATA_DIR is set
PROGRAM_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
               else os.path.dirname(os.path.abspath(__file__)))
HOSTS_FILE = os.path.join(APP_DIR, "hosts.json")
KNOWN_HOSTS = os.path.join(APP_DIR, "known_hosts")
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ACTIONS = [
    ("ssh", "SSH (in app)"),
    ("files", "Files - SFTP/FTP (in app)"),
    ("rdp", "Remote Desktop"),
    ("vnc", "VNC"),
    ("rustdesk", "RustDesk"),
    ("web", "Web"),
    ("ps", "PowerShell (remote)"),
    ("ssh_ext", "SSH (separate window)"),
    ("mail", "Mail"),
    ("smb", "Network share (SMB)"),
    ("ping", "Ping"),
    ("wake", "⏻ Wake-on-LAN"),
    ("verify", "✔ Check this server"),
]
ACTION_LABEL = dict(ACTIONS)
SHORT_LABEL = {"ssh": "SSH", "files": "Files", "rdp": "Remote Desktop", "vnc": "VNC", "rustdesk": "RustDesk", "web": "Web", "ps": "PowerShell",
               "ssh_ext": "SSH window", "mail": "Mail", "smb": "Shares", "ping": "Ping", "wake": "⏻ Wake", "verify": "✔ Check"}
# Extra connections a server can have besides its main ones (e.g. a second web page, FTP as well as SFTP)
EXTRA_TYPES = [("ssh", "SSH"), ("sftp", "SFTP"), ("ftp", "FTP"), ("ftps", "FTPS"), ("rdp", "Remote Desktop"),
               ("vnc", "VNC"), ("rustdesk", "RustDesk"), ("web", "Web"), ("mail", "Mail"), ("smb", "Network share"), ("ps", "PowerShell")]
EXTRA_LABEL = dict(EXTRA_TYPES)
EXTRA_ACTION = {"ssh": "ssh", "sftp": "files", "ftp": "files", "ftps": "files", "rdp": "rdp", "vnc": "vnc", "rustdesk": "rustdesk", "web": "web",
                "mail": "mail", "smb": "smb", "ps": "ps"}
EXTRA_PORT = {"ssh": 22, "sftp": 22, "ftp": 21, "ftps": 21, "rdp": 3389, "vnc": 5900, "rustdesk": 0, "web": 0, "mail": 0, "smb": 445, "ps": 0}
FILE_MODES = ["SFTP", "FTP", "FTPS"]
DEFAULT_FILE_PORT = {"SFTP": 22, "FTP": 21, "FTPS": 21}
TUNNEL_KINDS = ("local", "remote", "dynamic")
SNIPPETS_FILE = os.path.join(APP_DIR, "snippets.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
LOG_FILE = os.path.join(APP_DIR, "rc.log")
HOSTS_VERSION = 1  # layout version of hosts.json; bump when the stored fields change
MIN_PARAMIKO = (3, 4)  # matches requirements.txt

# ─── Threading model ─────────────────────────────────────────────────────────
# * The Tk UI runs on the main thread ONLY. Worker threads (connects, probes, transfers, tunnels) never touch
#   widgets: they hand work back with App.post(fn) (a queue drained by App.pump on the UI thread), or with
#   ui_call(app, fn) when they need the answer (e.g. "trust this host key?").
# * All threads are daemons, so closing the window never waits on a stuck network call.
# * Shared files are guarded by module locks: HOSTS_LOCK (hosts.json is saved from the main window, the file
#   manager and the tools tabs) and KNOWN_HOSTS_LOCK (several parallel SSH connections can trust new keys at
#   once). Each file manager tab also has its own xlock/rlock for its SFTP/FTP connections, which are not
#   thread-safe. Always take a lock for as short a time as possible and never call ui_call while holding one.
HOSTS_LOCK = threading.RLock()
KNOWN_HOSTS_LOCK = threading.RLock()


# ─── Logging and timing ──────────────────────────────────────────────────────
# rc.log lives next to hosts.json (512 KB x 3 files). Passwords, passphrases and saved-login blobs are
# never written to it: only addresses, ports, user names, results and how long things took.

log = logging.getLogger("rc")
log.addHandler(logging.NullHandler())  # silent until setup_logging() runs (so importing for tests writes nothing)


def setup_logging(path=None, level=logging.INFO):
    """Start writing rc.log. Safe to call twice. Set RC_LOG_LEVEL=DEBUG for more detail."""
    level = getattr(logging, os.environ.get("RC_LOG_LEVEL", "").upper(), level)
    log.setLevel(level)
    path = path or LOG_FILE
    for h in log.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            return path
    try:
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
    except OSError:  # read-only folder: run without a log rather than refuse to start
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"))
    log.addHandler(handler)
    log.info("=== %s v%s starting (Python %s, paramiko %s, data folder %s)", APP_NAME, APP_VERSION,
             sys.version.split()[0], getattr(paramiko, "__version__", "missing"), APP_DIR)
    return path


class timed:
    """Time a block and log it: `with timed("DNS lookup", addr) as t: ...` then t.ms is the elapsed milliseconds.
    Anything slower than `slow_ms` is logged as a warning so sluggish servers stand out in rc.log."""

    def __init__(self, what, detail="", slow_ms=2000):
        self.what, self.detail, self.slow_ms, self.ms = what, detail, slow_ms, 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ms = (time.perf_counter() - self.t0) * 1000
        state = "failed: " + (str(exc) or exc_type.__name__) if exc_type else "ok"
        lvl = logging.WARNING if (self.ms >= self.slow_ms or exc_type) else logging.INFO
        log.log(lvl, "%s %s - %s in %.0f ms%s", self.what, self.detail, state, self.ms,
                " (SLOW)" if self.ms >= self.slow_ms else "")
        return False


# ─── Password protection (Windows DPAPI) ─────────────────────────────────────

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    ebuf = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    extra = _Blob(len(_ENTROPY), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if not fn(ctypes.byref(src), None, ctypes.byref(extra), None, None, 0, ctypes.byref(out)):
        raise OSError("Windows could not " + ("encrypt" if protect else "decrypt") + " the password")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


_ENTROPY = b"RC-Remote-Connection-Manager-v1"  # other programs need this too to decrypt our saved logins


class _Cred(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def cred_write(target, user, password, kind=1):
    """Put a login in Windows Credential Manager for this Windows session only (kind 1 = generic, 2 = domain)."""
    blob = password.encode("utf-16-le")
    buf = (ctypes.c_ubyte * max(len(blob), 1)).from_buffer_copy(blob or b"\0")
    c = _Cred()
    c.Type, c.TargetName, c.UserName = kind, target, user
    c.CredentialBlobSize, c.CredentialBlob = len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    c.Persist = 1  # CRED_PERSIST_SESSION: gone at sign-out
    adv = ctypes.windll.advapi32
    adv.CredWriteW.argtypes = [ctypes.POINTER(_Cred), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    if not adv.CredWriteW(ctypes.byref(c), 0):
        raise OSError("Windows would not store the login temporarily")


def cred_delete(target, kind=1):
    adv = ctypes.windll.advapi32
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    return bool(adv.CredDeleteW(target, kind, 0))


def protect_password(pw):
    return base64.b64encode(_dpapi(pw.encode("utf-8"), True)).decode("ascii")


def unprotect_password(blob):
    try:
        return _dpapi(base64.b64decode(blob), False).decode("utf-8")
    except Exception as e:
        log.warning("Could not decrypt a saved password (saved by another PC/user, or damaged): %s", e)
        return ""


# ─── Host storage ────────────────────────────────────────────────────────────

NEW_HOST = {
    "name": "", "address": "", "group": "", "user": "", "ssh_port": 22, "rdp_port": 3389, "vnc_port": 5900, "rustdesk_id": "", "rustdesk_password_enc": "",
    "file_mode": "SFTP", "file_port": 0, "key_file": "", "web_url": "", "mail_url": "",
    "default_action": "ssh", "notes": "", "password_enc": "", "connections": [],
    "tunnels": [], "bookmarks": [], "last_path": "",
    "favorite": False, "last_connected": 0, "mac": "", "alt_addresses": [], "jump_host": "",
}


def _port(value, default):
    """A usable TCP port (1-65535), or `default` if the stored value is missing, text, or out of range."""
    try:
        p = int(value)
    except (TypeError, ValueError):
        return default
    return p if 1 <= p <= 65535 else default


def sanitize_host(h, problems=None):
    """Fill in missing fields and repair values that would crash the app later (bad ports, unknown modes).
    Anything repaired is appended to `problems` so it can be logged. Returns the cleaned copy."""
    problems = problems if problems is not None else []
    merged = dict(NEW_HOST)
    merged.update(h)
    merged.setdefault("id", uuid.uuid4().hex)
    name = merged.get("name") or merged["address"]
    for field, default in (("ssh_port", 22), ("rdp_port", 3389), ("vnc_port", 5900)):
        fixed = _port(merged.get(field), default)
        if fixed != merged.get(field):
            problems.append(f"{name}: {field} {merged.get(field)!r} is not a valid port, using {default}")
        merged[field] = fixed
    if merged.get("file_port") not in (0, None, ""):  # 0 means "the default for the file mode"
        fixed = _port(merged["file_port"], 0)
        if fixed != merged["file_port"]:
            problems.append(f"{name}: file_port {merged['file_port']!r} is not a valid port, using the default")
        merged["file_port"] = fixed
    else:
        merged["file_port"] = 0
    if merged.get("file_mode") not in FILE_MODES:
        problems.append(f"{name}: file_mode {merged.get('file_mode')!r} unknown, using SFTP")
        merged["file_mode"] = "SFTP"
    if merged.get("default_action") not in ACTION_LABEL:
        problems.append(f"{name}: default_action {merged.get('default_action')!r} unknown, using ssh")
        merged["default_action"] = "ssh"
    for field in ("name", "group", "user", "key_file", "web_url", "mail_url", "notes", "password_enc", "last_path", "rustdesk_id", "rustdesk_password_enc"):
        if not isinstance(merged.get(field), str):
            merged[field] = "" if merged.get(field) is None else str(merged[field])
    merged["favorite"] = bool(merged.get("favorite"))
    try:
        merged["last_connected"] = max(0.0, float(merged.get("last_connected") or 0))
    except (TypeError, ValueError):
        merged["last_connected"] = 0
    mac = normalize_mac(merged.get("mac"))
    if merged.get("mac") and not mac:
        problems.append(f"{name}: MAC address {merged.get('mac')!r} is not valid, ignored")
    merged["mac"] = mac or ""
    alts = merged.get("alt_addresses")
    if isinstance(alts, str):
        alts = re.split(r"[,;\s]+", alts)
    merged["alt_addresses"] = [a.strip() for a in (alts or []) if isinstance(a, str) and a.strip()
                               and valid_address(a.strip()) and a.strip().lower() != merged["address"].lower()]
    merged["jump_host"] = merged["jump_host"] if isinstance(merged.get("jump_host"), str) else ""
    merged["connections"] = [c for c in (merged.get("connections") or [])
                             if isinstance(c, dict) and c.get("type") in EXTRA_ACTION]
    merged["tunnels"] = [t for t in (merged.get("tunnels") or []) if isinstance(t, dict) and t.get("kind") in TUNNEL_KINDS]
    merged["bookmarks"] = [b for b in (merged.get("bookmarks") or []) if isinstance(b, str)]
    return merged


def load_hosts():
    """Read hosts.json. A damaged file is copied aside (hosts.json.corrupt-<time>) instead of being silently
    replaced by an empty list on the next save; a file written by a NEWER RC is loaded but flagged in the log."""
    if not os.path.exists(HOSTS_FILE):
        log.info("No hosts.json yet (%s) - starting with an empty list", HOSTS_FILE)
        return []
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and not isinstance(data.get("hosts", []), list):
            raise ValueError("'hosts' is not a list")
        hosts = data.get("hosts", []) if isinstance(data, dict) else data
        if not isinstance(hosts, list):
            raise ValueError("top level is not a list of servers")
    except (OSError, ValueError) as e:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        aside = f"{HOSTS_FILE}.corrupt-{stamp}"
        try:
            os.replace(HOSTS_FILE, aside)
        except OSError:
            aside = "(could not be moved)"
        log.error("hosts.json could not be read (%s). Moved to %s and starting empty.", e, aside)
        return []
    version = data.get("version", 0) if isinstance(data, dict) else 0
    if not isinstance(version, int) or version > HOSTS_VERSION:
        log.warning("hosts.json is format version %r but this RC understands up to %d - newer fields may be "
                    "ignored. Update RC before editing servers.", version, HOSTS_VERSION)
    elif version < HOSTS_VERSION:
        log.info("hosts.json is an older format (v%s); it will be upgraded to v%d on the next save", version, HOSTS_VERSION)
    out, problems, skipped = [], [], 0
    for h in hosts:
        if isinstance(h, dict) and isinstance(h.get("address"), str) and h["address"].strip():
            out.append(sanitize_host(h, problems))
        else:
            skipped += 1
    if skipped:
        log.warning("Skipped %d entr%s in hosts.json with no address", skipped, "y" if skipped == 1 else "ies")
    for p in problems:
        log.warning("hosts.json repaired: %s", p)
    log.info("Loaded %d server(s) from hosts.json", len(out))
    return out


def save_hosts(hosts):
    tmp = HOSTS_FILE + ".tmp"
    with HOSTS_LOCK, timed("Save hosts.json", f"({len(hosts)} servers)", slow_ms=1000):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": HOSTS_VERSION, "hosts": hosts}, f, indent=2)
            os.replace(tmp, HOSTS_FILE)
        except Exception:
            try:
                os.remove(tmp)  # don't leave a half-written temp file behind
            except OSError:
                pass
            raise


def check_settings_file(path=None):
    """Startup check of settings.json (owned by rc_theme). Returns a list of problems; a bad file is only
    reported, never deleted, since the theme code falls back to its defaults."""
    path = path or SETTINGS_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [f"settings.json is unreadable ({e}); defaults will be used"]
    if not isinstance(data, dict):
        return ["settings.json should hold a JSON object; defaults will be used"]
    return []


DEFAULT_SNIPPETS = [
    {"id": "df", "name": "Disk space", "command": "df -h", "confirm": False},
    {"id": "up", "name": "Uptime and load", "command": "uptime", "confirm": False},
    {"id": "mem", "name": "Memory", "command": "free -h", "confirm": False},
    {"id": "upd", "name": "Updates waiting (Debian/Ubuntu)", "command": "apt list --upgradable 2>/dev/null | head -25",
     "confirm": False},
]


def load_snippets():
    try:
        with open(SNIPPETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [s for s in data if isinstance(s, dict) and s.get("name") and s.get("command")]
    except FileNotFoundError:
        return [dict(s) for s in DEFAULT_SNIPPETS]
    except (OSError, ValueError, TypeError) as e:
        log.warning("snippets.json could not be read (%s) - using the built-in snippets", e)
        return [dict(s) for s in DEFAULT_SNIPPETS]


def save_snippets(snips):
    tmp = SNIPPETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snips, f, indent=2)
    os.replace(tmp, SNIPPETS_FILE)


def web_url_for(h):
    url = (h.get("web_url") or "").strip()
    if not url:
        return "http://" + h["address"]
    return url if "://" in url else "http://" + url


def read_setting(key):
    """One value from settings.json (the file the colour theme also uses), or None if missing/unreadable."""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(key) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def rustdesk_target(h):
    """What to hand RustDesk: the server's RustDesk ID if it has one, else its address (RustDesk's direct IP access)."""
    return re.sub(r"\s+", "", h.get("rustdesk_id") or "") or h["address"]


def find_rustdesk():
    """Path of rustdesk.exe (settings.json 'rustdesk_path', PATH, or the usual install folders), or None."""
    cands = [read_setting("rustdesk_path") or "", shutil.which("rustdesk") or ""]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            cands.append(os.path.join(base, "RustDesk", "rustdesk.exe"))
    return next((c for c in cands if c and os.path.isfile(c)), None)


VNC_VIEWERS = ["TightVNC/tvnviewer.exe", "RealVNC/VNC Viewer/vncviewer.exe", "uvnc bvba/UltraVNC/vncviewer.exe",
               "TigerVNC/vncviewer.exe", "TigerVNC/vncviewer64.exe"]


def find_vnc_viewer():
    """Path of an installed VNC viewer (settings.json 'vnc_viewer', then TightVNC/RealVNC/UltraVNC/TigerVNC), or None."""
    cands = [read_setting("vnc_viewer") or ""]
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            cands += [os.path.join(base, *rel.split("/")) for rel in VNC_VIEWERS]
    cands += [shutil.which("tvnviewer") or "", shutil.which("vncviewer") or ""]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def vnc_command(viewer, address, port):
    """Command line for a VNC viewer. 'host::port' is understood by TightVNC, RealVNC, UltraVNC and TigerVNC;
    no password is put on the command line (the viewer asks, which keeps it out of the process list)."""
    return [viewer, f"{address}::{port}"]


def rustdesk_command(exe, target, password=""):
    """Command line for RustDesk 1.2+. The password (optional) is visible to other programs on this PC while
    RustDesk starts, which is why saving it is optional."""
    cmd = [exe, "--connect", target]
    if password:
        cmd += ["--password", password]
    return cmd


def connection_list(h):
    """Every way to reach a server: the built-in ones, then its extras.
    Each item: action (what to run), label, detail (shown to the user), over (settings that replace the
    server's own for this one connection)."""
    fport = h["file_port"] or DEFAULT_FILE_PORT[h["file_mode"]]
    out = [
        {"action": "ssh", "label": "SSH", "detail": f"port {h['ssh_port']}", "over": {}},
        {"action": "files", "label": f"Files ({h['file_mode']})", "detail": f"port {fport}", "over": {}},
        {"action": "rdp", "label": "Remote Desktop", "detail": f"port {h['rdp_port']}", "over": {}},
        {"action": "vnc", "label": "VNC", "detail": f"port {h['vnc_port']}", "over": {}},
        {"action": "rustdesk", "label": "RustDesk",
         "detail": rustdesk_target(h) + ("" if h.get("rustdesk_id") else " (direct IP)"), "over": {}},
        {"action": "web", "label": "Web", "detail": web_url_for(h), "over": {}},
        {"action": "ps", "label": "PowerShell (remote)", "detail": "WinRM", "over": {}},
        {"action": "ssh_ext", "label": "SSH window", "detail": f"port {h['ssh_port']}", "over": {}},
        {"action": "mail", "label": "Mail", "detail": h["mail_url"] or "(not set)", "over": {}},
        {"action": "smb", "label": "Network share (SMB)", "detail": "\\\\" + h["address"], "over": {}},
        {"action": "ping", "label": "Ping", "detail": h["address"], "over": {}},
        {"action": "wake", "label": "Wake-on-LAN", "detail": h.get("mac") or "(set a MAC address in Edit)", "over": {}},
        {"action": "verify", "label": "Check this server", "detail": "", "over": {}},
    ]
    for c in h.get("connections", []):
        t, port, url = c["type"], int(c.get("port") or 0), (c.get("url") or "").strip()
        label = c.get("label") or EXTRA_LABEL[t]
        over, detail = {}, ""
        if t == "ssh":
            over, detail = {"ssh_port": port or 22}, f"port {port or 22}"
        elif t in ("sftp", "ftp", "ftps"):
            over, detail = {"file_mode": t.upper(), "file_port": port or EXTRA_PORT[t]}, f"{t.upper()} port {port or EXTRA_PORT[t]}"
        elif t == "rdp":
            over, detail = {"rdp_port": port or 3389}, f"port {port or 3389}"
        elif t == "vnc":
            over, detail = {"vnc_port": port or 5900}, f"port {port or 5900}"
        elif t == "rustdesk":
            over, detail = {"rustdesk_id": url}, url or "(direct IP)"
        elif t == "web":
            over, detail = {"web_url": url}, url
        elif t == "mail":
            over, detail = {"mail_url": url}, url
        elif t == "smb":
            over, detail = {"smb_path": url}, url or "\\\\" + h["address"]
        if c.get("user"):
            over["user"] = c["user"]
        if c.get("password_enc"):
            over["password_enc"] = c["password_enc"]
        out.append({"action": EXTRA_ACTION[t], "label": label, "detail": detail, "over": over, "extra": True})
    return out


def saved_login(h):
    """(user, password) if a login is saved for this connection, else None."""
    if h.get("password_enc") and h.get("user"):
        pw = unprotect_password(h["password_enc"])
        if pw:
            return h["user"], pw
    return None


def variant(h, over):
    """A copy of server h with one connection's settings applied. '_src' points back at the real server."""
    v = dict(h, **over)
    v["_src"] = h.get("_src", h)
    if "password_enc" in over:
        v["_nosave"] = True  # this connection has its own saved login
    elif over.get("user") and over["user"] != h["user"]:
        v["password_enc"], v["_nosave"] = "", True  # the saved password belongs to the server's own user
    return v


# ─── Wake-on-LAN, address fallback, live status ──────────────────────────────

def normalize_mac(value):
    """'aa-bb-cc-dd-ee-ff' / 'AABB.CCDD.EEFF' / 'aabbccddeeff' -> 'AA:BB:CC:DD:EE:FF', or None if it isn't a MAC."""
    if not isinstance(value, str) or re.search(r"[^0-9A-Fa-f:.\-\s]", value):
        return None
    digits = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(digits) != 12:
        return None
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()


def wol_packet(mac):
    """The 'magic packet': 6 bytes of FF, then the MAC address 16 times."""
    m = normalize_mac(mac)
    if not m:
        raise ValueError(f"'{mac}' is not a valid MAC address")
    return b"\xff" * 6 + bytes.fromhex(m.replace(":", "")) * 16


def send_wol(mac, target_ip=None):
    """Broadcast a Wake-on-LAN packet (UDP ports 9 and 7). Sent to the limited broadcast address and, when the
    server's IPv4 address is known, to its /24 broadcast too (that one leaves through the right network card on
    PCs with several). Returns the list of destinations used."""
    packet = wol_packet(mac)
    dests = ["255.255.255.255"]
    try:
        ip = ipaddress.ip_address(target_ip or "")
        if ip.version == 4:
            dests.append(str(ipaddress.ip_network(f"{ip}/24", strict=False).broadcast_address))
    except ValueError:
        pass
    sent = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for dest in dests:
            for port in (9, 7):
                try:
                    s.sendto(packet, (dest, port))
                    sent.append(f"{dest}:{port}")
                except OSError as e:
                    log.warning("Wake-on-LAN to %s:%s failed: %s", dest, port, e)
    if not sent:
        raise OSError("No network accepted the Wake-on-LAN packet")
    log.info("Wake-on-LAN sent for %s to %s", normalize_mac(mac), ", ".join(sent))
    return sent


def host_addresses(h):
    """The main address first, then the alternates (tried in this order)."""
    return [h["address"]] + [a for a in (h.get("alt_addresses") or []) if a]


def tcp_probe(addr, port, timeout=2.0):
    """'open' if something answers, 'refused' if the machine answered with a refusal (so it IS up, just not
    running that service), or None if nothing came back (off, unreachable, or the firewall drops it)."""
    try:
        with socket.create_connection((addr, port), timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return None


_ADDR_CACHE = {}
_ADDR_LOCK = threading.Lock()


def resolve_address(host, port=None, timeout=2.0, max_age=60):
    """Which of the server's addresses to use right now. With no alternates this is just the main address.
    Otherwise each address is tried in order on `port` and the first one that answers wins; the answer is
    remembered for `max_age` seconds so a burst of connections doesn't re-test every time."""
    alts = host.get("alt_addresses") or []
    primary = host["address"]
    if not alts:
        return primary
    port = port or _port(host.get("ssh_port"), 22)
    key = (primary, tuple(alts), port)
    with _ADDR_LOCK:
        hit = _ADDR_CACHE.get(key)
        if hit and time.time() - hit[1] < max_age:
            return hit[0]
    chosen = primary
    for cand in host_addresses(host):
        if tcp_probe(cand, port, timeout):
            chosen = cand
            break
    with _ADDR_LOCK:
        _ADDR_CACHE[key] = (chosen, time.time())
    if chosen != primary:
        log.info("Address fallback: %s did not answer on port %s, using %s", primary, port, chosen)
    return chosen


def _ping_ms(addr, wait_ms=1000):
    """One Windows ping. Returns round-trip ms (0 for '<1ms') or None."""
    try:
        out = subprocess.run(["ping", "-n", "1", "-w", str(wait_ms), addr], capture_output=True, text=True,
                             timeout=wait_ms / 1000 + 3, creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"TTL=\d+", out) and re.search(r"time[=<]\s*(\d+)\s*ms", out)
    return int(m.group(1)) if m else None


def check_host_status(h, timeout=1.5):
    """Is this server up? Tries each of its addresses: the SSH port, then the Remote Desktop port (a refusal
    counts as up), then a ping. Returns {state 'up'|'down', addr, ms, port, via_alt, service}."""
    ports = []
    for p in (_port(h.get("ssh_port"), 22), _port(h.get("rdp_port"), 3389)):
        if p not in ports:
            ports.append(p)
    for addr in host_addresses(h):
        for port in ports:
            t0 = time.perf_counter()
            r = tcp_probe(addr, port, timeout)
            if r:
                return {"state": "up", "addr": addr, "ms": round((time.perf_counter() - t0) * 1000), "port": port,
                        "service": r == "open", "via_alt": addr != h["address"]}
    for addr in host_addresses(h):
        ms = _ping_ms(addr)
        if ms is not None:
            return {"state": "up", "addr": addr, "ms": ms, "port": 0, "service": False, "via_alt": addr != h["address"]}
    return {"state": "down", "addr": h["address"], "ms": 0, "port": 0, "service": False, "via_alt": False}


class StatusMonitor:
    """Checks every server in the background and remembers the answer (state[id] = result + 'checked' time).
    `on_update(id, result)` is called from a worker thread; the caller must hand it to the UI thread."""

    def __init__(self, get_hosts, on_update, interval=60, workers=12):
        self.get_hosts, self.on_update, self.interval, self.workers = get_hosts, on_update, interval, workers
        self.state = {}
        self.wake = threading.Event()
        self.stopped = False
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="status-monitor")
        self.thread.start()

    def stop(self):
        self.stopped = True
        self.wake.set()

    def refresh(self):
        """Check everything again right now."""
        self.wake.set()

    def run(self):
        while not self.stopped:
            try:
                self.round()
            except Exception:
                log.exception("Status monitor round failed")
            self.wake.wait(self.interval)
            self.wake.clear()

    def round(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        hosts = [{k: h.get(k) for k in ("id", "address", "alt_addresses", "ssh_port", "rdp_port")}
                 for h in list(self.get_hosts())]
        if not hosts:
            return
        with timed("Status round", f"({len(hosts)} servers)", slow_ms=15000), ThreadPoolExecutor(self.workers) as ex:
            futures = {ex.submit(check_host_status, h): h for h in hosts}
            for fut in as_completed(futures):
                h = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    log.warning("Status check of %s failed: %s", h["address"], e)
                    continue
                res["checked"] = time.time()
                old = self.state.get(h["id"])
                self.state[h["id"]] = res
                if old and old["state"] != res["state"]:
                    log.info("Status change: %s (%s) is now %s", h["id"][:8], h["address"], res["state"].upper())
                if self.stopped:
                    return
                self.on_update(h["id"], res)

    def summary(self, hosts):
        ids = {h["id"] for h in hosts}
        up = sum(1 for i, r in self.state.items() if i in ids and r["state"] == "up")
        down = sum(1 for i, r in self.state.items() if i in ids and r["state"] == "down")
        return up, down, len(ids) - up - down


def ago(ts, now=None):
    """'just now', '5 min ago', '3 h ago', '2 d ago', or 'never'."""
    if not ts:
        return "never"
    secs = max(0, (now or time.time()) - ts)
    if secs < 45:
        return "just now"
    if secs < 3600:
        return f"{round(secs / 60)} min ago"
    if secs < 86400:
        return f"{round(secs / 3600)} h ago"
    return f"{round(secs / 86400)} d ago"


# ─── Trusted SSH host keys (known_hosts) ─────────────────────────────────────

def list_known_hosts(path=None):
    """[{host, type, fp}] for every key RC has trusted, sorted by host."""
    if paramiko is None:
        return []
    keys = paramiko.HostKeys()
    try:
        with KNOWN_HOSTS_LOCK:
            keys.load(path or KNOWN_HOSTS)
    except OSError:
        return []
    rows = []
    for hostname in list(keys):
        for key_type, key in keys[hostname].items():
            rows.append({"host": hostname, "type": key_type, "fp": fingerprint(key)})
    return sorted(rows, key=lambda r: (r["host"].lower(), r["type"]))


def remove_known_host(host, key_type=None, path=None):
    """Forget the trusted key(s) for a host (one key type, or all). Returns how many were removed. The next
    connection asks whether to trust the server's current key."""
    if paramiko is None:
        return 0
    path = path or KNOWN_HOSTS
    with KNOWN_HOSTS_LOCK:
        keys = paramiko.HostKeys()
        try:
            keys.load(path)
        except OSError:
            return 0
        if host not in keys:
            return 0
        n = len(keys[host]) if key_type is None else int(key_type in keys[host])
        if n == 0:
            return 0
        if key_type is None:
            del keys[host]
        else:
            del keys[host][key_type]
        keys.save(path)
    log.info("Removed %d trusted key(s) for %s from known_hosts", n, host)
    return n


# ─── Checking what was typed in ──────────────────────────────────────────────

_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def valid_address(addr):
    try:
        ipaddress.ip_address(addr.strip("[]"))
        return True
    except ValueError:
        pass
    if not addr or len(addr) > 253 or addr.replace(".", "").isdigit():
        return False  # all digits and dots but not a real IP, e.g. 192.168.1.300
    return all(_LABEL.match(p) for p in addr.rstrip(".").split("."))


def validate_host(h, others=()):
    """Checks that need no network. Returns (errors, warnings)."""
    errors, warns = [], []
    addr = h["address"]
    if re.search(r"[\s/\\@]", addr):
        errors.append("The address can't contain spaces, slashes or @ - put the username in the Username box.")
    elif re.fullmatch(r".+:\d+", addr) and addr.count(":") == 1:
        errors.append("Leave the port off the address and enter it in the port box instead.")
    elif not valid_address(addr):
        errors.append(f"“{addr}” isn't a valid IP address or DNS name (check for typos or a number over 255).")
    if h["user"] and " " in h["user"]:
        warns.append("The username contains a space - is that right?")
    for alt in h.get("alt_addresses") or []:
        if not valid_address(alt):
            errors.append(f"The other address “{alt}” isn't a valid IP address or DNS name.")
    if h.get("mac") and not normalize_mac(h["mac"]):
        errors.append("The MAC address should look like AA:BB:CC:DD:EE:FF (12 hex digits).")
    if h.get("jump_host") and h.get("id") and h["jump_host"] == h["id"]:
        errors.append("A server can't be its own jump server.")
    key = h["key_file"]
    if key:
        if not os.path.isfile(key):
            errors.append("The SSH key file doesn't exist at that path.")
        elif key.lower().endswith(".pub"):
            errors.append("That's a public key (.pub). Pick the private key file (same name without .pub).")
        elif paramiko is not None:
            try:
                with open(key, "r", errors="ignore") as f:
                    head = f.read(200)
                if "PRIVATE KEY" not in head:
                    errors.append("That file doesn't look like an SSH private key.")
            except OSError as e:
                errors.append(f"The SSH key file can't be read: {e}")
    for field, label, schemes in (("web_url", "Web address", ("http", "https")),
                                  ("mail_url", "Mail address", ("http", "https", "mailto"))):
        v = h[field]
        if not v:
            continue
        parsed = urlparse(v if "://" in v or v.startswith("mailto:") else "http://" + v)
        if parsed.scheme not in schemes or (parsed.scheme != "mailto" and not parsed.hostname):
            errors.append(f"{label} must be a web address like https://{h['address']}/")
    for o in others:
        if o["id"] != h.get("id") and o["address"].lower() == addr.lower() and o["name"].lower() == h["name"].lower():
            warns.append(f"“{o['name']}” with this address is already in your list.")
            break
    return errors, warns


def _tcp(addr, port, timeout=4):
    """Open the port and read whatever greeting the server volunteers."""
    with socket.create_connection((addr, port), timeout) as s:
        s.settimeout(timeout)
        try:
            return s.recv(128).decode("latin-1", "replace").strip()
        except OSError:
            return ""


def probe_host(h):
    """Network checks, run in parallel. Returns [(state, text)] with state ok/warn/fail."""
    addr = h["address"]
    try:
        with timed("DNS lookup", addr, slow_ms=1500) as dns:
            ips = sorted({i[4][0] for i in socket.getaddrinfo(addr, None)})
    except socket.gaierror:
        return [("fail", f"“{addr}” doesn't resolve - check the spelling or your DNS/VPN.")]
    out = {"name": ("ok", f"Found: {', '.join(ips[:3])} ({dns.ms:.0f} ms)")}

    def reach(key, label, port, kind):
        try:
            with timed(f"TCP connect {label}", f"{addr}:{port}", slow_ms=3000) as t:
                banner = _tcp(addr, port)
        except socket.timeout:
            out[key] = ("warn", f"{label} (port {port}): no answer - firewalled, or not used on this server.")
            return
        except OSError:
            out[key] = ("warn", f"{label} (port {port}): refused - nothing listening (fine if you don't use it).")
            return
        if kind == "ssh" and not banner.startswith("SSH-"):
            out[key] = ("warn", f"{label} (port {port}) is open but doesn't look like SSH ({banner[:40] or 'silent'}).")
        elif kind == "ftp" and not banner.startswith("220"):
            out[key] = ("warn", f"{label} (port {port}) is open but doesn't look like FTP ({banner[:40] or 'silent'}).")
        else:
            out[key] = ("ok", f"{label} (port {port}) answering ({t.ms:.0f} ms)"
                        + (f": {banner[:50]}" if kind in ("ssh", "ftp") else ""))

    def web(key="web", url=None, label="Web"):
        url = url or web_url_for(h)
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "RC-check"})
            with timed(f"HTTP HEAD {label}", url, slow_ms=3000) as t:
                try:
                    r = urllib.request.urlopen(req, timeout=5, context=ctx)
                    code = r.status
                except urllib.error.HTTPError as e:
                    code = e.code
            out[key] = ("ok", f"{label} {url}: answered ({code}, {t.ms:.0f} ms)")
        except Exception as e:
            out[key] = ("warn", f"{label} {url}: no answer ({str(e)[:60]})")

    mode = h["file_mode"]
    fport = h["file_port"] or DEFAULT_FILE_PORT[mode]
    jobs = [threading.Thread(target=reach, args=("ssh", "SSH", h["ssh_port"], "ssh")),
            threading.Thread(target=reach, args=("rdp", "Remote Desktop", h["rdp_port"], "tcp")),
            threading.Thread(target=web)]
    if not (mode == "SFTP" and fport == h["ssh_port"]):
        jobs.append(threading.Thread(target=reach, args=("files", f"{mode} files", fport, "ssh" if mode == "SFTP" else "ftp")))
    order = ["name", "ssh", "files", "rdp", "web"]
    for i, c in enumerate(h.get("connections", [])):
        t, key = c["type"], f"x{i}"
        label = c.get("label") or EXTRA_LABEL[t]
        order.append(key)
        if t == "web":
            jobs.append(threading.Thread(target=web, args=(key, c["url"].strip() if "://" in c["url"] else "http://" + c["url"].strip(), label)))
        elif t in ("ssh", "sftp", "ftp", "mail", "rdp", "smb", "ps") and not (t == "mail" and c.get("url")):
            port = int(c.get("port") or EXTRA_PORT[t] or (5985 if t == "ps" else 0))
            kind = "ssh" if t in ("ssh", "sftp") else "ftp" if t == "ftp" else "tcp"
            if port:
                jobs.append(threading.Thread(target=reach, args=(key, label, port, kind)))
        elif t == "ftps":
            port = int(c.get("port") or 21)  # port 990 is implicit TLS: no plain greeting to read
            jobs.append(threading.Thread(target=reach, args=(key, label, port, "ftp" if port != 990 else "tcp")))
    with timed("Server check", f"{addr} ({len(jobs)} probes)", slow_ms=8000):
        for j in jobs:
            j.start()
        for j in jobs:
            j.join(timeout=12)
    return [out[k] for k in order if k in out]


MARK = {"ok": "✔", "warn": "⚠", "fail": "✖"}


def format_probe(results):
    return "\n".join(f"{MARK[s]} {t}" for s, t in results)


# ─── SSH helpers ─────────────────────────────────────────────────────────────

def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def ui_call(app, fn, timeout=600):
    """Run fn on the UI thread from a worker thread and return its result.
    Returns None if fn raised (the error is logged with its traceback, so a dialog that failed to open is
    visible in rc.log instead of looking like a "No" answer) or if the UI never answered within `timeout` s
    (for example the window was closed while a worker was waiting) so worker threads can't hang forever."""
    done, box = threading.Event(), []

    def run():
        try:
            box.append(fn())
        except Exception:
            log.exception("ui_call: the dialog/function run on the UI thread raised an error")
            box.append(None)
        done.set()
    app.post(run)
    if not done.wait(timeout):
        log.error("ui_call: no answer from the UI thread after %s s - giving up", timeout)
        return None
    return box[0] if box else None


def check_dependencies():
    """Startup check of optional/required packages. Returns a list of problems (also written to the log)."""
    problems = []
    if paramiko is None:
        problems.append("paramiko is not installed - SSH, SFTP, tunnels and key setup are unavailable "
                        "(pip install -r requirements.txt)")
    else:
        have = getattr(paramiko, "__version__", "0")
        try:
            parts = tuple(int(p) for p in re.findall(r"\d+", have)[:2])
        except ValueError:
            parts = (0, 0)
        if parts < MIN_PARAMIKO:
            problems.append(f"paramiko {have} is older than the tested minimum "
                            f"{'.'.join(map(str, MIN_PARAMIKO))} - upgrade with: pip install -U paramiko")
    try:
        import pyte  # noqa: F401
    except ImportError:
        problems.append("pyte is not installed - the SSH terminal tab cannot start (pip install -r requirements.txt)")
    if sys.platform != "win32":
        problems.append("RC is built for Windows (mstsc, DPAPI); saved passwords and Remote Desktop will not work here")
    for p in problems:
        log.warning("Dependency check: %s", p)
    return problems


def find_host(app, host_id):
    """The server entry with this id, or None."""
    if not host_id:
        return None
    return next((h for h in getattr(app, "hosts", []) if h.get("id") == host_id), None)


def _open_jump(app, jump, chain):
    """Log in to a jump server using its saved login or key (asking if there is neither)."""
    login = saved_login(jump)
    if not login and jump.get("user") and jump.get("key_file"):
        login = (jump["user"], "")
    if not login:
        login = ui_call(app, lambda: app.credentials(jump))
    if not login:
        raise RuntimeError(f"The login for the jump server “{jump.get('name')}” was cancelled.")
    return open_ssh_client(app, jump, login[0], login[1], _chain=chain)


def open_ssh_client(app, host, user, password, key_only=False, _chain=()):
    """Log in over SSH and return the paramiko client. Honours the server's jump host (reach it through another
    server) and its alternate addresses (first one that answers), and asks before trusting a new host key."""
    if paramiko is None:
        raise RuntimeError("The 'paramiko' package is missing. Run: pip install paramiko")
    try:
        with open(KNOWN_HOSTS, "a"):  # create it if missing; the handle is closed straight away
            pass
    except OSError as e:
        log.error("Cannot create/open known_hosts (%s): %s", KNOWN_HOSTS, e)
        raise RuntimeError(f"RC can't use its known_hosts file ({e}). Is the folder read-only?")

    class AskPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            ok = ui_call(app, lambda: messagebox.askyesno(
                "New server",
                f"First connection to {hostname}.\n\n{key.get_name()} key fingerprint:\n{fingerprint(key)}\n\n"
                "Trust this server and remember its key?", parent=app.root))
            if not ok:
                log.warning("SSH: user declined the new host key for %s (%s)", hostname, fingerprint(key))
                raise paramiko.SSHException("Server key was not trusted.")
            log.info("SSH: trusted new host key for %s (%s %s)", hostname, key.get_name(), fingerprint(key))
            client._host_keys.add(hostname, key.get_name(), key)
            with KNOWN_HOSTS_LOCK:
                # Re-read the file first: another thread (bulk key setup, snippets on many servers) may have
                # trusted a different server a moment ago, and saving only our copy would erase that line.
                keys = paramiko.HostKeys()
                try:
                    keys.load(KNOWN_HOSTS)
                except OSError as e:
                    log.warning("known_hosts re-read failed (%s); saving this server only", e)
                keys.add(hostname, key.get_name(), key)
                keys.save(KNOWN_HOSTS)

    client = paramiko.SSHClient()
    with KNOWN_HOSTS_LOCK:
        client.load_host_keys(KNOWN_HOSTS)
    client.set_missing_host_key_policy(AskPolicy())
    port = _port(host.get("ssh_port"), 22)
    method = "key" if host.get("key_file") else "password" if password else "agent/keys"
    jump = find_host(app, host.get("jump_host"))
    jump_client, sock, via = None, None, ""
    try:
        if jump and jump["id"] != host.get("id") and jump["id"] not in _chain and len(_chain) < 3:
            jump_client = _open_jump(app, jump, _chain + (host.get("id"),))
            try:
                sock = jump_client.get_transport().open_channel("direct-tcpip", (host["address"], port),
                                                                ("127.0.0.1", 0), timeout=15)
            except Exception as e:
                raise RuntimeError(f"The jump server “{jump['name']}” could not reach {host['address']}:{port} ({e}).")
            via = f" via jump {jump['name']}"
        elif host.get("alt_addresses"):
            addr = resolve_address(host, port)
            if addr != host["address"]:  # connect the socket ourselves so the host key is still checked under the main address
                sock = socket.create_connection((addr, port), 12)
                via = f" via alternate address {addr}"
        with timed("SSH connect", f"{user or '?'}@{host['address']}:{port} via {method}{via}", slow_ms=4000):
            client.connect(
                host["address"], port=port, username=user or None, sock=sock,
                password=password or None, key_filename=(host.get("key_file") or None),
                passphrase=password or None, timeout=12, banner_timeout=15, auth_timeout=15,
                allow_agent=not key_only, look_for_keys=not key_only)
    except paramiko.BadHostKeyException:
        log.error("SSH: HOST KEY MISMATCH for %s - connection refused", host["address"])
        if jump_client:
            jump_client.close()
        raise RuntimeError(f"WARNING: the key for {host['address']} does not match the one saved earlier. "
                           "The server may have been rebuilt, or someone may be impersonating it. "
                           "If you know it changed, open Tools > Trusted host keys, remove that server, and reconnect.")
    except Exception:
        if jump_client:
            jump_client.close()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        raise
    if jump_client:
        plain_close = client.close

        def close_both():
            plain_close()
            jump_client.close()
        client.close = close_both
    return client


# ─── File transfer (SFTP / FTP / FTPS) ───────────────────────────────────────

class Cancelled(Exception):
    """Raised from a transfer's progress callback to stop it."""


class SftpBackend:
    kind = "sftp"

    def __init__(self, client):
        self.client, self.sftp = client, client.open_sftp()

    # paths
    @staticmethod
    def join(base, name):
        return posixpath.join(base, name)

    @staticmethod
    def parent(path):
        return posixpath.dirname(path.rstrip("/")) or "/"

    def home(self):
        return self.sftp.normalize(".")

    def listdir(self, path):
        out = []
        for a in self.sftp.listdir_attr(path):
            is_dir = stat.S_ISDIR(a.st_mode or 0)
            if stat.S_ISLNK(a.st_mode or 0):
                try:
                    is_dir = stat.S_ISDIR(self.sftp.stat(posixpath.join(path, a.filename)).st_mode)
                except OSError:
                    pass
            out.append((a.filename, is_dir, a.st_size or 0, a.st_mtime or 0))
        return out

    def stat(self, path):
        """(is_dir, size, mtime, mode) or None if it doesn't exist."""
        try:
            a = self.sftp.stat(path)
        except (OSError, IOError):
            return None
        return stat.S_ISDIR(a.st_mode or 0), a.st_size or 0, a.st_mtime or 0, stat.S_IMODE(a.st_mode or 0)

    # transfers (progress(done, total) may raise Cancelled)
    def download(self, remote, local, progress=None):
        self.sftp.get(remote, local, callback=progress)

    def upload(self, local, remote, progress=None):
        self.sftp.put(local, remote, callback=progress, confirm=False)

    def set_mtime(self, path, mtime):
        try:
            self.sftp.utime(path, (mtime, mtime))
        except (OSError, IOError):
            pass

    # changes
    def mkdir(self, path):
        self.sftp.mkdir(path)

    def ensure_dir(self, path):
        """mkdir -p"""
        parts, cur = [p for p in path.split("/") if p], "/" if path.startswith("/") else ""
        for p in parts:
            cur = posixpath.join(cur, p) if cur else p
            if self.stat(cur) is None:
                self.sftp.mkdir(cur)

    def remove(self, path, is_dir):
        (self.sftp.rmdir if is_dir else self.sftp.remove)(path)

    def rename(self, old, new):
        try:
            self.sftp.posix_rename(old, new)
        except (OSError, IOError):
            self.sftp.rename(old, new)

    def chmod(self, path, mode):
        self.sftp.chmod(path, mode)

    def close(self):
        for c in (self.sftp, self.client):
            try:
                c.close()
            except Exception:
                pass


_LIST_LINE = re.compile(r"^([\-dl])\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3}\s+\d+\s+[\d:]+)\s+(.+)$")


def _mlsd_time(s):
    try:
        return calendar.timegm(time.strptime(s[:14], "%Y%m%d%H%M%S"))
    except (ValueError, TypeError):
        return 0


class FtpBackend:
    kind = "ftp"

    def __init__(self, ftp):
        self.ftp = ftp

    @staticmethod
    def join(base, name):
        return posixpath.join(base, name)

    @staticmethod
    def parent(path):
        return posixpath.dirname(path.rstrip("/")) or "/"

    def home(self):
        return self.ftp.pwd()

    def listdir(self, path):
        self.ftp.cwd(path)
        out = []
        try:
            for name, facts in self.ftp.mlsd(facts=["type", "size", "modify"]):
                kind = facts.get("type", "")
                if kind in ("cdir", "pdir"):
                    continue
                out.append((name, kind == "dir", int(facts.get("size", 0) or 0), _mlsd_time(facts.get("modify", ""))))
            return out
        except (error_perm, AttributeError):
            pass
        lines = []
        self.ftp.retrlines("LIST", lines.append)
        for line in lines:
            m = _LIST_LINE.match(line)
            if m and m.group(4) not in (".", ".."):
                name = m.group(4).split(" -> ")[0]
                out.append((name, m.group(1) in "dl", int(m.group(2)), 0))
        return out

    def stat(self, path):
        here = self.ftp.pwd()
        try:
            try:
                self.ftp.cwd(path)
                return True, 0, 0, 0o755
            except error_perm:
                pass
            finally:
                self.ftp.cwd(here)
            self.ftp.voidcmd("TYPE I")
            size = self.ftp.size(path)
            if size is None:
                return None
            try:
                mtime = _mlsd_time(self.ftp.voidcmd("MDTM " + path)[4:].strip())
            except Exception:
                mtime = 0
            return False, size, mtime, 0o644
        except Exception:
            return None

    def download(self, remote, local, progress=None):
        try:
            total = self.ftp.size(remote) or 0
        except Exception:
            total = 0
        done = [0]
        with open(local, "wb") as f:
            def write(block):
                f.write(block)
                done[0] += len(block)
                if progress:
                    progress(done[0], total)
            self.ftp.retrbinary("RETR " + remote, write)

    def upload(self, local, remote, progress=None):
        total, done = os.path.getsize(local), [0]

        def cb(block):
            done[0] += len(block)
            if progress:
                progress(done[0], total)
        with open(local, "rb") as f:
            self.ftp.storbinary("STOR " + remote, f, callback=cb)

    def set_mtime(self, path, mtime):
        try:
            self.ftp.voidcmd("MFMT %s %s" % (time.strftime("%Y%m%d%H%M%S", time.gmtime(mtime)), path))
        except Exception:
            pass  # server doesn't support it; comparisons fall back to size

    def mkdir(self, path):
        self.ftp.mkd(path)

    def ensure_dir(self, path):
        cur = "/" if path.startswith("/") else ""
        for p in [p for p in path.split("/") if p]:
            cur = posixpath.join(cur, p) if cur else p
            if self.stat(cur) is None:
                self.ftp.mkd(cur)

    def remove(self, path, is_dir):
        (self.ftp.rmd if is_dir else self.ftp.delete)(path)

    def rename(self, old, new):
        self.ftp.rename(old, new)

    def chmod(self, path, mode):
        self.ftp.sendcmd("SITE CHMOD %o %s" % (mode, path))

    def close(self):
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass


class ImplicitFTPTLS(FTP_TLS):
    """FTPS on port 990: TLS starts immediately instead of after an AUTH TLS command."""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), self.timeout, source_address=source_address)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


def open_ftp(host, port, user, password):
    """Log in over FTP or FTPS (explicit on 21, implicit on 990). Returns the ftplib connection."""
    mode = host.get("file_mode", "FTP")
    if mode == "FTPS":
        # Home servers usually have self-signed certificates, so the certificate isn't checked.
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        ftp = (ImplicitFTPTLS if port == 990 else FTP_TLS)(timeout=15, context=ctx)
    else:
        ftp = FTP(timeout=15)
    ftp.connect(resolve_address(host, port), port)
    ftp.login(user or "anonymous", password or "anonymous@")
    if mode == "FTPS":
        ftp.prot_p()
    return ftp


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class PasswordDialog(tk.Toplevel):
    def __init__(self, parent, title, prompt, can_remember=True):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        ttk.Label(self, text=prompt, wraplength=320).grid(row=0, column=0, columnspan=2, padx=12, pady=(12, 6))
        self.pw = tk.StringVar()
        e = ttk.Entry(self, textvariable=self.pw, show="*", width=34)
        e.grid(row=1, column=0, columnspan=2, padx=12)
        self.remember = tk.BooleanVar(value=False)
        if can_remember:
            ttk.Checkbutton(self, text="Remember (encrypted for this Windows account)",
                            variable=self.remember).grid(row=2, column=0, columnspan=2, padx=12, pady=6, sticky="w")
        row = ttk.Frame(self)
        row.grid(row=3, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(row, text="Connect", command=self.ok).pack(side="left", padx=4)
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="left", padx=4)
        self.bind("<Return>", lambda _: self.ok())
        self.bind("<Escape>", lambda _: self.destroy())
        e.focus_set()
        self.grab_set()
        self.wait_window(self)

    def ok(self):
        self.result = (self.pw.get(), self.remember.get())
        self.destroy()


class HostEditor(ttk.Frame):
    """Add/edit form shown as a tab inside the main window."""

    def __init__(self, app, host, groups, key):
        super().__init__(app.nb, padding=16)
        self.app, self.others, self.key, self.on_done = app, app.hosts, key, app.editor_done
        self.host, self.result = dict(host), None
        self.vars = {}
        head = ttk.Frame(self)  # buttons live at the top so they never fall off the bottom of a small window
        head.pack(fill="x", pady=(0, 8))
        ttk.Label(head, text="Edit server" if host.get("id") else "Add a server",
                  font=("Segoe UI", 16, "bold")).pack(side="left", padx=(0, 20))
        row = ttk.Frame(head)
        row.pack(side="left")
        self.report = tk.StringVar()
        ttk.Label(self, textvariable=self.report, justify="left", wraplength=700).pack(anchor="w", pady=(0, 6))
        f = ttk.Frame(self)
        f.pack(anchor="w")
        fields = [
            ("name", "Name"), ("address", "IP address or DNS name"), ("group", "Group"),
            ("user", "Username"), ("_password", "Password (or key passphrase)"), ("ssh_port", "SSH port"), ("rdp_port", "Remote Desktop port"),
            ("vnc_port", "VNC port"), ("rustdesk_id", "RustDesk ID (blank = connect by IP address)"),
            ("_rd_password", "RustDesk password (optional)"),
            ("file_mode", "File transfer"), ("file_port", "File port (0 = default)"),
            ("key_file", "SSH key file (optional)"), ("web_url", "Web address (blank = http://address)"),
            ("mail_url", "Mail / webmail address"),
            ("alt_addresses", "Other addresses (comma-separated, tried in order)"),
            ("jump_host", "Connect through (jump server)"), ("mac", "MAC address (for Wake-on-LAN)"),
            ("default_action", "Double-click does"),
        ]
        self.jump_map = {"(none)": ""}
        for o in sorted(app.hosts, key=lambda x: x["name"].lower()):
            if o["id"] != host.get("id"):
                self.jump_map[f"{o['name']}  ({o['address']})"] = o["id"]
        for r, (key, label) in enumerate(fields):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2, padx=(0, 10))
            if key == "alt_addresses":
                v = tk.StringVar(value=", ".join(host.get("alt_addresses") or []))
            elif key == "jump_host":
                v = tk.StringVar(value=next((l for l, i in self.jump_map.items() if i == host.get("jump_host")), "(none)"))
            else:
                v = tk.StringVar(value=str(host.get(key, "")))
            if key == "_rd_password":
                self.rd_pw_var = v
                v.set("")
                ttk.Entry(f, textvariable=v, width=36, show="•").grid(row=r, column=1, sticky="w", pady=2)
                self.clear_rd_pw = tk.BooleanVar(value=False)
                if host.get("rustdesk_password_enc"):
                    ttk.Checkbutton(f, text="🔑 saved - tick to forget", variable=self.clear_rd_pw).grid(
                        row=r, column=2, sticky="w", padx=4)
                continue
            if key == "_password":
                self.pw_var = v
                self.pw_entry = ttk.Entry(f, textvariable=v, width=36, show="•")
                self.pw_entry.grid(row=r, column=1, sticky="w", pady=2)
                self.show_pw = tk.BooleanVar(value=False)
                ttk.Checkbutton(f, text="Show", variable=self.show_pw, command=self.toggle_pw).grid(
                    row=r, column=2, sticky="w", padx=4)
                continue
            self.vars[key] = v
            if key == "file_mode":
                w = ttk.Combobox(f, textvariable=v, values=FILE_MODES, state="readonly", width=12)
            elif key == "default_action":
                w = ttk.Combobox(f, state="readonly", width=28, values=[l for _, l in ACTIONS])
                w.set(ACTION_LABEL.get(host.get("default_action"), ACTIONS[0][1]))
                self.action_box = w
            elif key == "group":
                w = ttk.Combobox(f, textvariable=v, values=groups, width=34)
            elif key == "jump_host":
                w = ttk.Combobox(f, textvariable=v, values=list(self.jump_map), state="readonly", width=34)
            else:
                w = ttk.Entry(f, textvariable=v, width=36)
            w.grid(row=r, column=1, sticky="w", pady=2)
            if key == "key_file":
                ttk.Button(f, text="Browse…", command=self.browse_key).grid(row=r, column=2, padx=4)
        r = len(fields)
        ttk.Label(f, text="Notes").grid(row=r, column=0, sticky="nw", pady=2)
        self.notes = tk.Text(f, width=36, height=2, wrap="word")
        self.notes.insert("1.0", host.get("notes", ""))
        self.notes.grid(row=r, column=1, sticky="w", pady=2)
        self.clear_pw = tk.BooleanVar(value=False)
        if host.get("password_enc"):
            ttk.Checkbutton(f, text="🔑 A password is saved (encrypted, this Windows account only). Tick to forget it; "
                                    "type a new one above to replace it.", variable=self.clear_pw).grid(
                row=r + 1, column=0, columnspan=3, sticky="w", pady=2)
        else:
            ttk.Label(f, text="🔑 Anything typed in Password is saved encrypted on this PC (Windows account only) and "
                              "filled in for you when you connect.", foreground="#555").grid(
                row=r + 1, column=0, columnspan=3, sticky="w", pady=2)
        self.extras = [dict(c) for c in host.get("connections", [])]
        box = ttk.LabelFrame(f, text=" More connections for this server (optional) ", padding=8)
        box.grid(row=r + 2, column=0, columnspan=3, sticky="we", pady=(10, 0))
        self.extra_tree = ttk.Treeview(box, columns=("type", "port", "to", "login"), height=2, selectmode="browse")
        self.extra_tree.heading("#0", text="Name")
        self.extra_tree.heading("type", text="Type")
        self.extra_tree.heading("port", text="Port")
        self.extra_tree.heading("to", text="Web / mail / share address")
        self.extra_tree.heading("login", text="Login")
        for col, w in (("#0", 130), ("type", 95), ("port", 50), ("to", 230), ("login", 110)):
            self.extra_tree.column(col, width=w)
        self.extra_tree.grid(row=0, column=0, columnspan=7, sticky="we")
        self.x_type = tk.StringVar(value="Web")
        self.x_label, self.x_port, self.x_url, self.x_user, self.x_pw = (tk.StringVar() for _ in range(5))
        for c, (text, var, w) in enumerate((("Type", None, 12), ("Name", self.x_label, 13), ("Port", self.x_port, 6),
                                            ("Address (web/mail/share)", self.x_url, 24), ("User", self.x_user, 10),
                                            ("Password", self.x_pw, 10))):
            ttk.Label(box, text=text).grid(row=1, column=c, sticky="w", pady=(6, 0))
            if var is None:
                ttk.Combobox(box, textvariable=self.x_type, values=[l for _, l in EXTRA_TYPES], state="readonly",
                             width=w).grid(row=2, column=c, sticky="w", padx=(0, 4))
            else:
                e = ttk.Entry(box, textvariable=var, width=w, show="•" if var is self.x_pw else "")
                e.grid(row=2, column=c, sticky="w", padx=(0, 4))
                if var is self.x_pw:
                    self.x_pw_entry = e
        bb = ttk.Frame(box)
        bb.grid(row=3, column=0, columnspan=7, sticky="w", pady=(6, 0))
        ttk.Button(bb, text="➕ Add this connection", command=self.add_extra).pack(side="left")
        ttk.Button(bb, text="Remove selected", command=self.remove_extra).pack(side="left", padx=6)
        self.refresh_extras()
        ttk.Button(row, text="Save", command=self.ok).pack(side="left", padx=4)
        self.verify_btn = ttk.Button(row, text="✔ Verify", command=self.verify)
        self.verify_btn.pack(side="left", padx=4)
        ttk.Button(row, text="🔑 Test login", command=self.test_login).pack(side="left", padx=4)
        ttk.Button(row, text="🔐 Set up key login", command=self.setup_key).pack(side="left", padx=4)
        ttk.Button(row, text="Cancel", command=self.cancel).pack(side="left", padx=4)
        self.bind("<Escape>", lambda _: self.cancel())

    def toggle_pw(self):
        self.pw_entry.configure(show="" if self.show_pw.get() else "•")
        self.x_pw_entry.configure(show="" if self.show_pw.get() else "•")

    def refresh_extras(self):
        self.extra_tree.delete(*self.extra_tree.get_children())
        for i, c in enumerate(self.extras):
            login = (c.get("user") or "") + (" 🔑" if c.get("password_enc") else "")
            self.extra_tree.insert("", "end", iid=str(i), text=c.get("label") or EXTRA_LABEL[c["type"]],
                                   values=(EXTRA_LABEL[c["type"]], c.get("port") or "", c.get("url", ""), login.strip()))

    def add_extra(self):
        t = next(k for k, l in EXTRA_TYPES if l == self.x_type.get())
        url, port = self.x_url.get().strip(), self.x_port.get().strip()
        if port and not (port.isdigit() and 1 <= int(port) <= 65535):
            messagebox.showerror("Connection", "Port must be a number from 1 to 65535.", parent=self)
            return
        if t in ("web", "mail") and not url:
            messagebox.showerror("Connection", "Enter the web or mail address for this connection.", parent=self)
            return
        if t == "web":
            parsed = urlparse(url if "://" in url else "http://" + url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                messagebox.showerror("Connection", "That doesn't look like a web address (try http://host:port).", parent=self)
                return
        entry = {"type": t, "label": self.x_label.get().strip(), "port": int(port) if port else 0,
                 "url": url, "user": self.x_user.get().strip()}
        if self.x_pw.get():
            if not entry["user"]:
                messagebox.showerror("Connection", "Enter the username that goes with this password.", parent=self)
                return
            entry["password_enc"] = protect_password(self.x_pw.get())
        self.extras.append(entry)
        for v in (self.x_label, self.x_port, self.x_url, self.x_user, self.x_pw):
            v.set("")
        self.refresh_extras()

    def remove_extra(self):
        sel = self.extra_tree.selection()
        if sel:
            del self.extras[int(sel[0])]
            self.refresh_extras()

    def cancel(self):
        self.on_done(self, None)

    def close(self):
        self.app.editors.pop(self.key, None)

    def browse_key(self):
        p = filedialog.askopenfilename(parent=self, title="SSH private key",
                                       initialdir=os.path.expanduser("~/.ssh"))
        if p:
            self.vars["key_file"].set(p)

    def collect(self):
        """Read the form. Returns (host, errors, warnings)."""
        h = dict(self.host)
        for k, v in self.vars.items():
            h[k] = v.get().strip()
        h["address"] = re.sub(r"^\w+://", "", h["address"]).strip("/ ")
        h["alt_addresses"] = [a for a in re.split(r"[,;\s]+", h.get("alt_addresses", "")) if a]
        h["jump_host"] = self.jump_map.get(self.vars["jump_host"].get().strip(), "")
        h["mac"] = normalize_mac(h.get("mac", "")) or h.get("mac", "")
        errors = []
        if not h["address"]:
            errors.append("Enter an IP address or DNS name.")
        for k, default, lo, label in (("ssh_port", 22, 1, "SSH port"), ("rdp_port", 3389, 1, "Remote Desktop port"),
                                      ("vnc_port", 5900, 1, "VNC port"),
                                      ("file_port", 0, 0, "File port")):
            try:
                h[k] = int(h[k]) if h[k] else default
                if not lo <= h[k] <= 65535:
                    raise ValueError
            except ValueError:
                errors.append(f"{label} must be a number from {lo} to 65535.")
                h[k] = default
        label = self.action_box.get()
        h["default_action"] = next((k for k, l in ACTIONS if l == label), "ssh")
        h["name"] = h["name"] or h["address"]
        h["notes"] = self.notes.get("1.0", "end").strip()
        h["connections"] = [dict(c) for c in self.extras]
        h["rustdesk_id"] = re.sub(r"\s+", "", h.get("rustdesk_id", ""))
        if self.rd_pw_var.get():
            h["rustdesk_password_enc"] = protect_password(self.rd_pw_var.get())
        elif self.clear_rd_pw.get():
            h["rustdesk_password_enc"] = ""
        if self.pw_var.get():
            h["password_enc"] = protect_password(self.pw_var.get())
        elif self.clear_pw.get():
            h["password_enc"] = ""
        if h["password_enc"] and not h["user"]:
            errors.append("A saved password needs a username - fill in Username.")
        warns = []
        if h["address"]:
            e, warns = validate_host(h, self.others)
            errors += e
        return h, errors, warns

    def verify(self):
        h, errors, warns = self.collect()
        if errors:
            self.report.set("\n".join("✖ " + e for e in errors))
            return
        self.verify_btn.state(["disabled"])
        self.report.set("Checking " + h["address"] + " ...")

        def work():
            results = [("warn", w) for w in warns] + probe_host(h)
            self.app.post(lambda: self.finish_verify(results))
        threading.Thread(target=work, daemon=True).start()

    def test_login(self):
        """Really log in with the typed username/password (or key) and report whether it worked."""
        h, errors, _ = self.collect()
        errors = [e for e in errors if "saved password needs" not in e]
        if errors:
            self.report.set("\n".join("✖ " + e for e in errors))
            return
        user = h["user"]
        if not user:
            self.report.set("✖ Enter a username to test.")
            return
        pw = self.pw_var.get() or (unprotect_password(self.host["password_enc"]) if self.host.get("password_enc") else "")
        self.report.set(f"Logging in to {h['address']} as {user} ...")

        def work():
            lines = []
            log.info("Login test for %s@%s", user, h["address"])
            try:
                c = open_ssh_client(self.app, h, user, pw)
                c.close()
                lines.append(("ok", f"SSH login worked for {user}@{h['address']}."))
            except Exception as e:
                log.warning("Login test: SSH login to %s failed: %s", h["address"], e)
                lines.append(("fail", f"SSH login failed: {str(e) or e.__class__.__name__}"))
            if h["file_mode"] != "SFTP":
                try:
                    port = int(h["file_port"] or DEFAULT_FILE_PORT[h["file_mode"]])
                    with timed(f"{h['file_mode']} login test", f"{user}@{h['address']}:{port}", slow_ms=4000):
                        ftp = open_ftp(h, port, user, pw)
                        FtpBackend(ftp).close()
                    lines.append(("ok", f"{h['file_mode']} login worked."))
                except Exception as e:
                    lines.append(("fail", f"{h['file_mode']} login failed: {str(e) or e.__class__.__name__}"))
            self.app.post(lambda: self.report.set(format_probe(lines)))
        threading.Thread(target=work, daemon=True).start()

    def setup_key(self):
        """Make an SSH key (or use the chosen one), install it on the server, prove it works."""
        import rc_tools
        h, errors, _ = self.collect()
        # a key path that doesn't exist yet is fine here: that's where the new key will be created
        errors = [e for e in errors if "saved password needs" not in e and "key file doesn't exist" not in e]
        if errors:
            self.report.set("\n".join("✖ " + e for e in errors))
            return
        if not h["user"]:
            self.report.set("✖ Enter the username first - the key is installed for that account.")
            return
        pw = self.pw_var.get() or (unprotect_password(self.host["password_enc"]) if self.host.get("password_enc") else "")
        if not pw and not h["key_file"]:
            self.report.set("✖ Type the password once (above) so RC can log in and install the key.")
            return
        phrase = simpledialog.askstring("Key passphrase", "Passphrase to protect the key (leave empty for none):",
                                        show="*", parent=self)
        if phrase is None:
            return
        lines = []

        def log(t):
            lines.append(t)
            self.app.post(lambda: self.report.set("\n".join(lines)))

        def work():
            try:
                path = rc_tools.setup_key_login(self.app, h, h["user"], pw, phrase, log)
                self.app.post(lambda: self.key_done(path))
            except Exception as e:
                log("✖ " + (str(e) or e.__class__.__name__))
        log("Starting ...")
        threading.Thread(target=work, daemon=True).start()

    def key_done(self, path):
        self.vars["key_file"].set(path)
        msg = "Key login works. Click Save to keep it."
        if (self.pw_var.get() or self.host.get("password_enc")) and messagebox.askyesno(
                "Key login ready", "The key works, so the password isn't needed any more.\n\n"
                                   "Forget the saved password for this server?", parent=self):
            self.pw_var.set("")
            self.clear_pw.set(True)
            msg += " The password will be forgotten when you save."
        self.report.set(self.report.get() + "\n" + msg)

    def finish_verify(self, results):
        if self.winfo_exists():
            self.report.set(format_probe(results))
            self.verify_btn.state(["!disabled"])

    def ok(self):
        h, errors, warns = self.collect()
        if errors:
            messagebox.showerror("Check these", "\n\n".join(errors), parent=self)
            return
        if warns and not messagebox.askyesno("Check this", "\n\n".join(warns) + "\n\nSave anyway?", parent=self):
            return
        self.result = h
        self.on_done(self, h)


# ─── Main window ─────────────────────────────────────────────────────────────

class App:
    def __init__(self, root):
        self.root = root
        root.title(FULL_TITLE)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1440, sw - 40)}x{min(800, sh - 80)}")
        root.minsize(900, 520)
        self.q = queue.Queue()
        for problem in check_settings_file():
            log.warning("Config check: %s", problem)
        self.hosts = load_hosts()
        self.editors, self.picker_rows = {}, []
        self.overview_job = None
        self.monitor = StatusMonitor(lambda: self.hosts, lambda i, r: self.post(lambda: self.on_status(i, r)))
        self.make_dots()
        import rc_tools
        self.tunnel_mgr = rc_tools.TunnelManager(self)
        self.palette = self.tunnels_tab = self.snippets_tab = None
        import rc_theme
        self.theme = rc_theme.Theme(root, SETTINGS_FILE)
        self.dark_var = tk.BooleanVar(value=self.theme.dark)
        self.theme_var = tk.StringVar(value=self.theme.name)
        self.build()
        self.refill()
        self.pump()
        self.monitor.start()
        self.root.after(30000, self.tick)
        self.theme.apply()
        self.sync_theme_button()
        root.bind("<Control-w>", lambda _: self.close_tab())
        root.bind("<Control-k>", lambda _: self.quick_connect())
        root.protocol("WM_DELETE_WINDOW", self.on_exit)

    def tick(self):
        """Every 30 s: keep the 'checked 2 min ago' texts honest."""
        self.update_status_line()
        self.schedule_overview()
        self.root.after(30000, self.tick)

    def toggle_dark(self):
        self.theme.set_theme(self.theme_var.get())
        self.sync_theme_button()

    def flip_dark(self):
        """The colours button: next colour theme."""
        self.theme.next_theme()
        self.sync_theme_button()

    def sync_theme_button(self):
        self.theme_var.set(self.theme.name)
        self.dark_var.set(self.theme.dark)
        self.theme_btn.configure(text="🎨 " + self.theme.name)

    def on_exit(self):
        self.monitor.stop()
        self.tunnel_mgr.stop_all()
        self.root.destroy()

    # tools
    def quick_connect(self):
        import rc_tools
        if self.palette is not None:
            self.palette.close()
            return
        self.palette = rc_tools.QuickConnect(self)

    def open_tunnels(self):
        import rc_tools
        if self.tunnels_tab is not None:
            self.nb.select(self.tunnels_tab)
            return
        self.tunnels_tab = rc_tools.TunnelsTab(self)
        self.add_tab(self.tunnels_tab, "⇄ Tunnels")

    def open_snippets(self, preselect=None):
        import rc_tools
        if self.snippets_tab is not None:
            self.close_widget(self.snippets_tab)
        self.snippets_tab = rc_tools.SnippetsTab(self, preselect or self.current_id())
        self.add_tab(self.snippets_tab, "▤ Snippets")

    def open_known_hosts(self):
        import rc_tools
        if getattr(self, "known_tab", None) is not None and self.known_tab.winfo_exists():
            self.nb.select(self.known_tab)
            return
        self.known_tab = rc_tools.KnownHostsTab(self)
        self.add_tab(self.known_tab, "🛡 Trusted keys")

    def open_log(self):
        if os.path.isfile(LOG_FILE):
            try:
                os.startfile(LOG_FILE)
                return
            except OSError as e:
                log.warning("Could not open the log: %s", e)
        messagebox.showinfo(APP_NAME, f"There is no log file yet ({LOG_FILE}).", parent=self.root)

    def open_keys(self):
        import rc_tools
        self.add_tab(rc_tools.KeysTab(self), "🔑 Keys")

    def import_sessions(self):
        import rc_tools
        self.add_tab(rc_tools.ImportTab(self), "⇩ Import")

    def close_widget(self, widget):
        if hasattr(widget, "close"):
            widget.close()
        try:
            self.nb.forget(widget)
        except tk.TclError:
            pass
        widget.destroy()
        self.nb.select(self.home)

    # thread-safe UI queue
    def post(self, fn):
        self.q.put(fn)

    def pump(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        except Exception:  # keep the UI alive if one update fails
            log.exception("UI update failed")
        self.root.after(30, self.pump)

    # layout
    def build(self):
        self.build_menus()
        top = ttk.Frame(self.root, padding=(8, 6, 8, 2))
        top.pack(fill="x")
        row1 = ttk.Frame(top)
        row1.pack(fill="x")
        ttk.Label(row1, text="Server:").pack(side="left")
        self.picker = ttk.Combobox(row1, state="readonly", width=30, postcommand=self.fill_picker)
        self.picker.pack(side="left", padx=6)
        self.picker.bind("<<ComboboxSelected>>", self.on_pick)
        for text, cmd in (("➕ Add", self.add_host), ("✏ Edit", self.edit_host), ("🗑 Delete", self.delete_host),
                          ("Import…", self.import_csv)):
            ttk.Button(row1, text=text, command=cmd).pack(side="left", padx=(0, 4))
        self.theme_btn = ttk.Button(row1, text="🎨 Colors", command=self.flip_dark)
        self.theme_btn.pack(side="right", padx=(8, 0))
        for text, cmd in (("🔑 Keys", self.open_keys), ("▤ Snippets", self.open_snippets), ("⇄ Tunnels", self.open_tunnels),
                          ("⌕ Quick  Ctrl+K", self.quick_connect)):
            ttk.Button(row1, text=text, command=cmd).pack(side="right", padx=(4, 0))
        row2 = ttk.Frame(top)
        row2.pack(fill="x", pady=(6, 4))
        ttk.Label(row2, text="Connect:").pack(side="left")
        self.buttons = {}
        for key, label in ACTIONS:
            b = ttk.Button(row2, text=SHORT_LABEL[key], style=f"{key}.Conn.TButton")
            b.configure(command=lambda k=key, b=b: self.press(k, b))
            b.pack(side="left", padx=(6, 0))
            self.buttons[key] = b
        ttk.Separator(self.root).pack(fill="x")

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=6)
        paned.add(left, weight=1)

        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refill())
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Label(row, text="🔍").pack(side="left")
        ttk.Entry(row, textvariable=self.search).pack(side="left", fill="x", expand=True, padx=4)

        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.tag_configure("group", font=("Segoe UI", 9, "bold"))
        self.tree.pack(fill="both", expand=True, pady=6)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.show_selected())
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Button-3>", self.on_right)
        self.tree.bind("<Delete>", lambda _: self.delete_host())

        right = ttk.Frame(paned)
        paned.add(right, weight=4)
        strip = ttk.Frame(right)
        strip.pack(fill="x", pady=(0, 2))
        ttk.Button(strip, text="✕ Disconnect all", command=self.disconnect_all).pack(side="right")
        ttk.Button(strip, text="✕ Disconnect this tab", command=self.close_tab).pack(side="right", padx=4)
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.nb.bind("<Button-2>", self.middle_close)
        self.nb.bind("<Button-3>", self.tab_menu)
        self.tab_popup = tk.Menu(self.root, tearoff=0)
        self.tab_popup.add_command(label="Disconnect / close tab", command=self.close_tab)

        self.home = ttk.Frame(self.nb, padding=20)
        self.nb.add(self.home, text="  Servers  ")
        self.build_overview()
        self.detail = ttk.Frame(self.home)
        head = ttk.Frame(self.detail)
        head.pack(fill="x")
        self.title_var = tk.StringVar(value="Select a server on the left")
        self.detail_var = tk.StringVar()
        self.stat_var = tk.StringVar()
        ttk.Label(head, textvariable=self.title_var, font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Button(head, text="🏠 Overview", command=self.show_overview).pack(side="right")
        self.fav_btn = ttk.Button(head, text="☆ Favorite", command=self.toggle_favorite)
        self.fav_btn.pack(side="right", padx=6)
        self.stat_label = ttk.Label(self.detail, textvariable=self.stat_var, font=("Segoe UI", 11, "bold"))
        self.stat_label.pack(anchor="w", pady=(6, 0))
        ttk.Label(self.detail, textvariable=self.detail_var, justify="left", foreground="#555").pack(anchor="w", pady=(6, 14))
        ttk.Label(self.detail, text="Connections (double-click one to open it)",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        cwrap = ttk.Frame(self.detail)
        cwrap.pack(fill="both", expand=True, anchor="w")
        self.conn_tree = ttk.Treeview(cwrap, columns=("detail",), height=18, selectmode="browse")
        self.conn_tree.heading("#0", text="Connection")
        self.conn_tree.heading("detail", text="Goes to")
        self.conn_tree.column("#0", width=250)
        self.conn_tree.column("detail", width=380)
        self.conn_tree.tag_configure("extra", foreground="#0b5cad")
        csb = ttk.Scrollbar(cwrap, command=self.conn_tree.yview)
        self.conn_tree.configure(yscrollcommand=csb.set)
        self.conn_tree.pack(side="left", fill="both", expand=True)
        csb.pack(side="left", fill="y")
        self.conn_tree.bind("<Double-1>", self.on_conn_double)
        self.conn_rows = []
        self.set_buttons(False)

        self.status = tk.StringVar()
        foot = ttk.Frame(self.root)
        foot.pack(fill="x", side="bottom")
        ttk.Label(foot, textvariable=self.status, anchor="w", relief="sunken").pack(side="left", fill="x", expand=True)
        brand = ttk.Label(foot, text=f"  {COMPANY} RC v{APP_VERSION}  •  Developed by {AUTHOR}  ", relief="sunken",
                          font=("Segoe UI", 9, "bold"), foreground="#d32f2f", cursor="hand2")
        brand.pack(side="right")
        brand.bind("<Button-1>", lambda _: self.about())

    def build_menus(self):
        bar = tk.Menu(self.root)
        self.root.configure(menu=bar)
        file_m = tk.Menu(bar, tearoff=0)
        file_m.add_command(label="Add server…", accelerator="Ctrl+N", command=self.add_host)
        file_m.add_command(label="Import servers from a CSV file…", command=self.import_csv)
        file_m.add_separator()
        file_m.add_command(label="Disconnect current tab", accelerator="Ctrl+W", command=self.close_tab)
        file_m.add_command(label="Disconnect all", command=self.disconnect_all)
        file_m.add_command(label="Exit", command=self.on_exit)
        bar.add_cascade(label="File", menu=file_m)
        edit_m = tk.Menu(bar, tearoff=0)
        edit_m.add_command(label="Edit selected server…", command=self.edit_host)
        edit_m.add_command(label="Delete selected server", command=self.delete_host)
        edit_m.add_command(label="Copy address", command=self.copy_address)
        edit_m.add_separator()
        edit_m.add_command(label="Forget ALL saved passwords…", command=self.forget_all_passwords)
        bar.add_cascade(label="Server", menu=edit_m)
        conn = tk.Menu(bar, tearoff=0)
        for key, label in ACTIONS:
            conn.add_command(label=label, command=lambda k=key: self.press(k))
        bar.add_cascade(label="Connect", menu=conn)
        self.servers_menu = tk.Menu(bar, tearoff=0)
        bar.add_cascade(label="Servers", menu=self.servers_menu)
        tools = tk.Menu(bar, tearoff=0)
        tools.add_command(label="Quick connect…", accelerator="Ctrl+K", command=self.quick_connect)
        tools.add_command(label="🔑 Set up keys for all servers…", command=self.open_keys)
        tools.add_command(label="SSH tunnels…", command=self.open_tunnels)
        tools.add_command(label="Snippets (saved commands)…", command=self.open_snippets)
        tools.add_command(label="🛡 Trusted host keys…", command=self.open_known_hosts)
        tools.add_command(label="⟳ Check all servers now", command=self.refresh_status)
        tools.add_command(label="📄 Open the log file", command=self.open_log)
        tools.add_separator()
        themes = tk.Menu(tools, tearoff=0)
        import rc_theme
        for name in rc_theme.PALETTES:
            themes.add_radiobutton(label=name, variable=self.theme_var, value=name, command=self.toggle_dark)
        tools.add_cascade(label="🎨 Color theme", menu=themes)
        tools.add_command(label="Import from PuTTY / WinSCP / ssh config…", command=self.import_sessions)
        bar.add_cascade(label="Tools", menu=tools)
        help_m = tk.Menu(bar, tearoff=0)
        help_m.add_command(label="\U0001f4d6 Quick start guide (README)", command=self.open_readme)
        help_m.add_command(label=f"\u2139 About {COMPANY} RC", command=self.about)
        bar.add_cascade(label="Help", menu=help_m)
        self.root.bind("<Control-n>", lambda _: self.add_host())

    def open_readme(self):
        """Help > Quick start guide: README.md shown as a formatted web page in the default browser."""
        try:
            from rc_tools import open_readme_html
            if open_readme_html(PROGRAM_DIR, f"{COMPANY} {APP_NAME}", f"Version {APP_VERSION}  \u00b7  Developed by {AUTHOR}"):
                return
        except Exception:
            log.exception("Could not show the README as a web page")
            for name in ("README.md", "README.txt"):  # fall back to whatever program opens .md files
                path = os.path.join(PROGRAM_DIR, name)
                if os.path.isfile(path):
                    try:
                        os.startfile(path)
                        return
                    except OSError:
                        pass
        messagebox.showinfo("Quick start", "Add a server with the \u2795 Add button, pick it from the list, then click "
                            "how you want to connect (SSH, Files, Remote Desktop, Web ...).", parent=self.root)

    def about(self):
        win = tk.Toplevel(self.root)
        win.title(f"About {COMPANY} RC")
        win.transient(self.root)
        win.resizable(False, False)
        head = tk.Frame(win, bg="#b71c1c")
        head.pack(fill="x")
        tk.Label(head, text="REG", bg="#b71c1c", fg="white", font=("Segoe UI", 26, "bold")).pack(side="left", padx=(18, 0), pady=10)
        tk.Label(head, text="Teches", bg="#b71c1c", fg="#ffcdd2", font=("Segoe UI", 26, "bold")).pack(side="left", pady=10)
        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"{APP_NAME}", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(body, text=f"Version {APP_VERSION}", font=("Segoe UI", 10, "bold"), foreground="#d32f2f").pack(anchor="w")
        ttk.Label(body, justify="left", wraplength=420, text=(
            "\nOne window for every server in your lab or office: SSH terminal tabs, a two-pane SFTP/FTP file manager, "
            "Remote Desktop, web consoles, SSH tunnels, saved commands, one-click SSH key setup and seven colour "
            "themes.\n")).pack(anchor="w")
        ttk.Label(body, text=f"Developed by {AUTHOR}", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(body, text=f"\u00a9 2026 {COMPANY}  \u2022  Jacksonville, FL  \u2022  All rights reserved").pack(anchor="w", pady=(2, 12))
        ttk.Button(body, text="Close", command=win.destroy).pack(anchor="e")
        win.update_idletasks()
        win.geometry("+%d+%d" % (self.root.winfo_rootx() + 120, self.root.winfo_rooty() + 100))
        win.grab_set()

    def rebuild_servers_menu(self):
        """Servers > group > server > how to connect."""
        m = self.servers_menu
        m.delete(0, "end")
        m.add_command(label="⟳ Check all servers now", command=self.refresh_status)
        m.add_separator()
        if not self.hosts:
            m.add_command(label="(no servers yet - use File > Add server)", state="disabled")
            return
        groups = {}
        for h in sorted(self.hosts, key=lambda h: (h["group"].lower(), h["name"].lower())):
            groups.setdefault(h["group"] or "Ungrouped", []).append(h)
        for g, items in groups.items():
            gm = tk.Menu(m, tearoff=0)
            for h in items:
                sm = tk.Menu(gm, tearoff=0)
                for c in connection_list(h):
                    sm.add_command(label=self.conn_text(c), command=lambda h=h, c=c: self.connect_to(h, c))
                gm.add_cascade(label=f"{h['name']}  ({h['address']})", menu=sm)
            m.add_cascade(label=f"{g}  [{len(items)}]", menu=gm)

    def connect_to(self, h, conn):
        """conn is an item from connection_list(h) (or just an action name for the main connection)."""
        self.select_host(h["id"])
        if isinstance(conn, str):
            self.press(conn)
        else:
            self.do(conn["action"], conn["over"])

    def select_host(self, hid):
        if not self.tree.exists(hid):
            self.search.set("")
            self.root.update_idletasks()
        if self.tree.exists(hid):
            self.tree.selection_set(hid)
            self.tree.see(hid)

    def picker_labels(self):
        rows = sorted(self.hosts, key=lambda h: (h["group"].lower(), h["name"].lower()))
        return rows, [f"{h['group'] or 'Ungrouped'}  ›  {h['name']}  ({h['address']})" for h in rows]

    def fill_picker(self):
        rows, labels = self.picker_labels()
        self.picker_rows = rows
        self.picker["values"] = labels or ["(no servers yet - click Add)"]

    def on_pick(self, _):
        self.fill_picker()
        i = self.picker.current()
        if 0 <= i < len(self.picker_rows):
            self.select_host(self.picker_rows[i]["id"])

    def on_conn_double(self, e):
        iid = self.conn_tree.identify_row(e.y)
        if iid and int(iid) < len(self.conn_rows):
            c = self.conn_rows[int(iid)]
            self.do(c["action"], c["over"])

    def forget_all_passwords(self):
        n = sum(bool(h["password_enc"]) + sum(bool(c.get("password_enc")) for c in h["connections"]) for h in self.hosts)
        if not n:
            messagebox.showinfo(APP_NAME, "No passwords are saved.")
            return
        if not messagebox.askyesno(APP_NAME, f"Remove all {n} saved password(s) from every server?\n"
                                             "Usernames, keys and settings are kept."):
            return
        for h in self.hosts:
            h["password_enc"] = ""
            for c in h["connections"]:
                c.pop("password_enc", None)
        self.commit(self.current_id())
        self.status.set(f"Forgot {n} saved password(s)")

    def copy_address(self):
        h = self.current()
        if h:
            self.root.clipboard_clear()
            self.root.clipboard_append(h["address"])
            self.status.set("Copied " + h["address"])

    def set_buttons(self, on):
        for b in self.buttons.values():
            b.state(["!disabled"] if on else ["disabled"])

    # host list
    DOT_COLORS = {"up": "#2e9e4f", "alt": "#e0a800", "down": "#d64545", "?": "#9aa0a6"}

    def make_dots(self):
        """Little coloured circles for the server list (drawn once; Tk keeps them alive through self.dots)."""
        self.dots = {}
        for name, color in self.DOT_COLORS.items():
            img = tk.PhotoImage(width=14, height=14)
            for y in range(14):
                for x in range(14):
                    if (x - 6.5) ** 2 + (y - 6.5) ** 2 <= 5.2 ** 2:
                        img.put(color, (x, y))
            self.dots[name] = img

    def dot_for(self, hid):
        r = self.monitor.state.get(hid)
        if not r:
            return self.dots["?"]
        return self.dots["down"] if r["state"] == "down" else self.dots["alt" if r.get("via_alt") else "up"]

    @staticmethod
    def base_id(iid):
        """Tree rows in the Favorites group are 'fav:<id>'; everything else is just the server id."""
        return iid[4:] if iid.startswith("fav:") else iid

    def refill(self):
        keep = self.current_id()
        needle = self.search.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        shown = [h for h in sorted(self.hosts, key=lambda h: (h["group"].lower(), h["name"].lower()))
                 if not needle or needle in f"{h['name']} {h['address']} {h['group']} {h['notes']}".lower()]
        favs = [h for h in shown if h.get("favorite")]
        if favs:
            fg = self.tree.insert("", "end", text="★ Favorites", open=True, tags=("group",))
            for h in favs:
                self.tree.insert(fg, "end", iid="fav:" + h["id"], text=f"{h['name']}   ({h['address']})",
                                 image=self.dot_for(h["id"]))
        groups = {}
        for h in shown:
            g = h["group"] or "Ungrouped"
            if g not in groups:
                groups[g] = self.tree.insert("", "end", text=f"📂 {g}", open=True, tags=("group",))
            iid = self.tree.insert(groups[g], "end", iid=h["id"], text=f"{h['name']}   ({h['address']})",
                                   image=self.dot_for(h["id"]))
            if h["id"] == keep:
                self.tree.selection_set(iid)
        self.show_selected()
        self.rebuild_servers_menu()
        self.fill_picker()

    def current_id(self):
        sel = self.tree.selection()
        hid = self.base_id(sel[0]) if sel else None
        return hid if hid in {h["id"] for h in self.hosts} else None

    def current(self):
        hid = self.current_id()
        return next((h for h in self.hosts if h["id"] == hid), None)

    # live status ---------------------------------------------------------------
    def on_status(self, hid, res):
        """A background check finished (runs on the UI thread): repaint that server's dot and the overview."""
        for iid in (hid, "fav:" + hid):
            if self.tree.exists(iid):
                self.tree.item(iid, image=self.dot_for(hid))
        if hid == self.current_id():
            self.update_status_line()
        self.schedule_overview()

    def refresh_status(self):
        self.status.set("Checking all servers ...")
        self.monitor.refresh()
        self.root.after(4000, lambda: self.status.set(""))

    def update_status_line(self):
        h = self.current()
        r = self.monitor.state.get(h["id"]) if h else None
        if not h or not r:
            self.stat_var.set("… not checked yet" if h else "")
            return
        if r["state"] == "up":
            how = f"port {r['port']} answered in {r['ms']} ms" if r.get("service") and r.get("port") else (
                "machine is up" if r.get("port") else f"ping {r['ms']} ms")
            alt = f"  ·  via alternate address {r['addr']}" if r.get("via_alt") else ""
            self.stat_var.set(f"●  Online  ·  {how}{alt}  ·  checked {ago(r['checked'])}")
        else:
            self.stat_var.set(f"○  Not answering  ·  checked {ago(r['checked'])}  (Wake-on-LAN works if a MAC address is set)"
                              if h.get("mac") else f"○  Not answering  ·  checked {ago(r['checked'])}")

    def toggle_favorite(self):
        h = self.current()
        if not h:
            return
        h["favorite"] = not h.get("favorite")
        self.commit(h["id"])
        self.status.set(("★ Added " if h["favorite"] else "Removed ") + h["name"] + (" to favorites" if h["favorite"] else " from favorites"))

    def mark_connected(self, h):
        """Remember when we last connected, for the Recent list."""
        src = h.get("_src", h)
        src["last_connected"] = time.time()
        try:
            save_hosts(self.hosts)
        except OSError as e:
            log.warning("Could not save last-connected time: %s", e)
        self.schedule_overview()

    # overview (shown when no server is selected) ---------------------------------
    def build_overview(self):
        self.dash = ttk.Frame(self.home)
        top = ttk.Frame(self.dash)
        top.pack(fill="x")
        ttk.Label(top, text="Overview", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Button(top, text="⟳ Check now", command=self.refresh_status).pack(side="right")
        self.dash_summary = tk.StringVar()
        ttk.Label(self.dash, textvariable=self.dash_summary, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(6, 12))
        grid = ttk.Frame(self.dash)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure((0, 1), weight=1, uniform="col")
        grid.rowconfigure(1, weight=3)
        grid.rowconfigure(3, weight=2)
        self.dash_trees = {}
        for key, title, row, col, span in (("fav", "★ Favorites", 0, 0, 1), ("recent", "🕘 Recently connected", 0, 1, 1),
                                           ("down", "⚠ Not answering", 2, 0, 2)):
            ttk.Label(grid, text=title, font=("Segoe UI", 10, "bold")).grid(row=row, column=col, columnspan=span,
                                                                           sticky="w", pady=(8, 2))
            tree = ttk.Treeview(grid, show="tree", selectmode="browse", height=6)
            tree.grid(row=row + 1, column=col, columnspan=span, sticky="nsew", padx=(0, 8) if col == 0 and span == 1 else 0)
            tree.bind("<Double-1>", lambda e, t=tree: self.dash_open(t, e))
            tree.bind("<Return>", lambda e, t=tree: self.dash_open(t, None))
            tree.bind("<Button-3>", lambda e, t=tree: self.dash_menu(t, e))
            self.dash_trees[key] = tree
        ttk.Label(self.dash, foreground="#777", wraplength=700, justify="left",
                  text="Pick a server on the left to connect. Dots show whether each server answers (green = up, "
                       "amber = up through an alternate address, red = not answering, grey = not checked yet). "
                       "Double-click a server here to use its default connection; right-click one in the list to add it to "
                       "your favorites.").pack(anchor="w", pady=(10, 0))
        self.overview_job = None

    def schedule_overview(self):
        if self.overview_job is None:
            self.overview_job = self.root.after(400, self.fill_overview)

    def fill_overview(self):
        self.overview_job = None
        up, down, unknown = self.monitor.summary(self.hosts)
        self.dash_summary.set(f"●  {up} up     ○  {down} not answering     …  {unknown} not checked yet     "
                              f"·     {len(self.hosts)} servers" if self.hosts else "No servers yet - click ➕ Add")
        by_name = sorted(self.hosts, key=lambda h: h["name"].lower())
        recent = sorted((h for h in self.hosts if h.get("last_connected")), key=lambda h: -h["last_connected"])[:8]
        downs = [h for h in by_name if (self.monitor.state.get(h["id"]) or {}).get("state") == "down"]
        data = {
            "fav": ([(h, f"{h['name']}   ·   {h['address']}") for h in by_name if h.get("favorite")],
                    "No favorites yet - right-click a server, then ★ Add to favorites"),
            "recent": ([(h, f"{h['name']}   ·   {ago(h['last_connected'])}") for h in recent],
                       "Nothing yet - servers you connect to show up here"),
            "down": ([(h, f"{h['name']}   ·   {h['address']}   ·   checked {ago(self.monitor.state[h['id']]['checked'])}")
                      for h in downs],
                     "✔ Everything that has been checked is answering" if self.monitor.state else "Checking ..."),
        }
        for key, tree in self.dash_trees.items():
            tree.delete(*tree.get_children())
            rows, empty = data[key]
            for h, text in rows:
                tree.insert("", "end", iid=h["id"], text=text, image=self.dot_for(h["id"]))
            if not rows:
                tree.insert("", "end", text="   " + empty, tags=("hint",))
        for tree in self.dash_trees.values():
            tree.tag_configure("hint", foreground="#777")

    def dash_pick(self, tree, e):
        iid = tree.identify_row(e.y) if e else (tree.selection()[0] if tree.selection() else "")
        return iid if iid in {h["id"] for h in self.hosts} else None

    def dash_open(self, tree, e):
        hid = self.dash_pick(tree, e)
        if hid:
            self.select_host(hid)
            self.do(self.current().get("default_action", "ssh"))

    def dash_menu(self, tree, e):
        hid = self.dash_pick(tree, e)
        if hid:
            self.select_host(hid)
            self.on_right(type("E", (), {"x": 0, "y": 0, "x_root": e.x_root, "y_root": e.y_root})(), hid)

    def show_overview(self):
        self.tree.selection_remove(*self.tree.selection())
        self.show_selected()
        self.nb.select(self.home)

    def show_selected(self):
        h = self.current()
        self.set_buttons(bool(h))
        if h:
            self.dash.pack_forget()
            self.detail.pack(fill="both", expand=True)
            self.buttons["wake"].state(["!disabled"] if h.get("mac") else ["disabled"])
            self.fav_btn.configure(text="★ Favorite" if h.get("favorite") else "☆ Add to favorites")
        else:
            self.detail.pack_forget()
            self.dash.pack(fill="both", expand=True)
            self.fill_overview()
        if h:
            rows, labels = self.picker_labels()
            self.picker.set(next((l for r, l in zip(rows, labels) if r["id"] == h["id"]), ""))
        else:
            self.picker.set("")
        self.conn_tree.delete(*self.conn_tree.get_children())
        self.conn_rows = connection_list(h) if h else []
        counts = {}
        for i, c in enumerate(self.conn_rows):
            counts[c["action"]] = counts.get(c["action"], 0) + 1
            self.conn_tree.insert("", "end", iid=str(i), text=c["label"], values=(c["detail"],),
                                  tags=("extra",) if c.get("extra") else ())
        for key, b in self.buttons.items():
            b.configure(text=SHORT_LABEL[key] + (" ▾" if counts.get(key, 0) > 1 else ""))
        if not h:
            self.title_var.set("")
            self.detail_var.set("")
            self.stat_var.set("")
            return
        self.title_var.set(("★ " if h.get("favorite") else "") + h["name"])
        self.update_status_line()
        lines = [f"Address: {h['address']}" + (f"   (also: {', '.join(h['alt_addresses'])})" if h.get("alt_addresses") else ""),
                 f"User: {h['user'] or '(asks when connecting)'}",
                 f"SSH {h['ssh_port']}  ·  RDP {h['rdp_port']}  ·  Files: {h['file_mode']}"]
        jump = find_host(self, h.get("jump_host"))
        if jump:
            lines.append(f"Reached through: {jump['name']} (jump server)")
        lines.append("Last connected: " + ago(h.get("last_connected")))
        if h["notes"]:
            lines.append("Notes: " + h["notes"])
        self.detail_var.set("\n".join(lines))

    def groups(self):
        return sorted({h["group"] for h in self.hosts if h["group"]})

    def add_host(self):
        self.open_editor(dict(NEW_HOST), "new")

    def edit_host(self):
        h = self.current()
        if not h:
            messagebox.showinfo(APP_NAME, "Select a server first.")
            return
        self.open_editor(h, h["id"])

    def open_editor(self, host, key):
        if key in self.editors:
            self.nb.select(self.editors[key])
            return
        ed = HostEditor(self, host, self.groups(), key)
        self.editors[key] = ed
        self.add_tab(ed, "➕ New server" if key == "new" else f"✏ {host['name']}")

    def editor_done(self, ed, h):
        """Called when the in-window editor is saved (h) or cancelled (None)."""
        if h:
            if ed.key == "new":
                h["id"] = uuid.uuid4().hex
                self.hosts.append(h)
            else:
                old = next((x for x in self.hosts if x["id"] == ed.key), None)
                if old is not None:
                    self.hosts[self.hosts.index(old)] = h
                else:
                    self.hosts.append(h)
        self.nb.select(ed)
        self.close_tab()
        self.nb.select(self.home)
        if h:
            self.commit(h["id"])

    def delete_host(self):
        h = self.current()
        if h and messagebox.askyesno(APP_NAME, f"Delete “{h['name']}” from the list?\n(The server itself is not touched.)"):
            self.hosts.remove(h)
            self.commit()

    def commit(self, select=None):
        try:
            save_hosts(self.hosts)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not save the server list:\n{e}")
        self.refill()
        if select and self.tree.exists(select):
            self.tree.selection_set(select)

    def import_csv(self):
        path = filedialog.askopenfilename(
            title="Import servers", filetypes=[("CSV / text", "*.csv *.txt"), ("All files", "*.*")])
        if not path:
            return
        added, skipped = 0, []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.reader(f):
                    row = [c.strip() for c in row]
                    if not row or not row[0] or row[0].lower() in ("name", "address", "host"):
                        continue
                    # name,address,group,user  -or-  just an address per line
                    name, addr = (row[0], row[1]) if len(row) > 1 and row[1] else (row[0], row[0])
                    h = dict(NEW_HOST, id=uuid.uuid4().hex, name=name, address=addr)
                    h["group"] = row[2] if len(row) > 2 else ""
                    h["user"] = row[3] if len(row) > 3 else ""
                    if validate_host(h, self.hosts)[0]:
                        skipped.append(addr)
                    elif any(o["address"].lower() == addr.lower() and o["name"].lower() == name.lower()
                             for o in self.hosts):
                        skipped.append(f"{addr} (already listed)")
                    else:
                        self.hosts.append(h)
                        added += 1
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            messagebox.showerror(APP_NAME, f"Could not read that file:\n{e}")
            return
        self.commit()
        self.status.set(f"Imported {added} server(s)")
        if skipped:
            messagebox.showwarning("Import", f"Imported {added}. Skipped {len(skipped)} that looked wrong or were "
                                             "duplicates:\n\n" + "\n".join(skipped[:15]) + ("\n…" if len(skipped) > 15 else ""))

    # menus / clicks
    def on_double(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and self.current():
            self.do(self.current().get("default_action", "ssh"))

    def on_right(self, e, hid=None):
        iid = hid or self.tree.identify_row(e.y)
        if not iid:
            return
        if not hid:
            self.tree.selection_set(iid)
        h = self.current()
        if not h:
            return
        m = tk.Menu(self.root, tearoff=0)
        for c in connection_list(h):
            m.add_command(label=self.conn_text(c), command=lambda c=c: self.do(c["action"], c["over"]))
        m.add_separator()
        m.add_command(label="Copy address", command=lambda: (self.root.clipboard_clear(),
                                                             self.root.clipboard_append(h["address"])))
        m.add_command(label="☆ Remove from favorites" if h.get("favorite") else "★ Add to favorites",
                      command=self.toggle_favorite)
        m.add_command(label="Edit…", command=self.edit_host)
        m.add_command(label="Delete", command=self.delete_host)
        m.tk_popup(e.x_root, e.y_root)

    def tab_menu(self, e):
        try:
            self.nb.select(self.nb.index(f"@{e.x},{e.y}"))
            self.tab_popup.tk_popup(e.x_root, e.y_root)
        except tk.TclError:
            pass

    def middle_close(self, e):
        try:
            self.nb.select(self.nb.index(f"@{e.x},{e.y}"))
            self.close_tab()
        except tk.TclError:
            pass

    def close_tab(self):
        tab = self.nb.nametowidget(self.nb.select()) if self.nb.select() else None
        if tab is None or tab is self.home:
            return
        if not self.confirm_close([tab]):
            return
        if hasattr(tab, "close"):
            tab.close()
        self.nb.forget(tab)
        tab.destroy()

    @staticmethod
    def busy_tab(tab):
        return any(j["state"] in ("queued", "running") for j in getattr(tab, "jobs", []))

    def confirm_close(self, tabs):
        if any(self.busy_tab(t) for t in tabs):
            return messagebox.askyesno("Transfers running",
                                       "Files are still being copied. Disconnect and cancel them?")
        return True

    def disconnect_all(self):
        tabs = [self.nb.nametowidget(t) for t in self.nb.tabs()]
        tabs = [t for t in tabs if t is not self.home]
        if not tabs or not self.confirm_close(tabs):
            return
        for tab in tabs:
            if hasattr(tab, "close"):
                tab.close()
            self.nb.forget(tab)
            tab.destroy()
        self.nb.select(self.home)

    # actions
    def do(self, action, over=None):
        h = self.current()
        if not h:
            return
        if over:
            h = variant(h, over)
        if action in ("rdp", "vnc", "rustdesk", "web", "smb", "ps", "ssh_ext", "ping", "verify"):
            h = self.live_address(h)  # SSH and file transfers pick their own address when they connect
        log.info("Action %s on %s (%s)", action, h.get("name"), h.get("address"))
        try:
            getattr(self, "act_" + action)(h)
            if action in ("ssh", "files", "rdp", "vnc", "rustdesk", "web", "ps", "ssh_ext", "smb"):
                self.mark_connected(h)
        except Exception as e:
            log.exception("Action %s on %s failed", action, h.get("address"))
            messagebox.showerror(APP_NAME, f"{ACTION_LABEL.get(action, action)} failed:\n{e}")

    def live_address(self, h):
        """If the server has alternate addresses and the last background check found it on one of them, use that."""
        if not h.get("alt_addresses"):
            return h
        r = self.monitor.state.get(h.get("_src", h).get("id"))
        if r and r["state"] == "up" and r["addr"] != h["address"] and time.time() - r["checked"] < 300:
            self.status.set(f"Using alternate address {r['addr']} for {h['name']}")
            return dict(h, address=r["addr"])
        return h

    @staticmethod
    def conn_text(c):
        return f"{c['label']}  -  {c['detail']}" if c["detail"] else c["label"]

    def press(self, action, widget=None):
        """A Connect button was clicked: run it, or ask which one if the server has several of that kind."""
        h = self.current()
        if not h:
            return
        options = [c for c in connection_list(h) if c["action"] == action]
        if len(options) == 1:
            self.do(action, options[0]["over"])
            return
        m = tk.Menu(self.root, tearoff=0)
        for c in options:
            m.add_command(label=self.conn_text(c), command=lambda c=c: self.do(c["action"], c["over"]))
        x = widget.winfo_rootx() if widget else self.root.winfo_pointerx()
        y = widget.winfo_rooty() + widget.winfo_height() if widget else self.root.winfo_pointery()
        m.tk_popup(x, y)

    def ask_user(self, h):
        if h["user"]:
            return h["user"]
        u = simpledialog.askstring("Username", f"Username for {h['address']}:", parent=self.root)
        return (u or "").strip() or None

    def credentials(self, h, allow_anonymous=False):
        """Returns (user, password) or None if cancelled."""
        user = h["user"] or self.ask_user(h)
        if not user and not allow_anonymous:
            return None
        if h.get("password_enc"):
            return user, unprotect_password(h["password_enc"])
        if h.get("key_file"):
            return user, ""
        d = PasswordDialog(self.root, "Password", f"Password for {user or 'anonymous'}@{h['address']}\n"
                           "(leave empty to use your SSH keys or ssh-agent)", can_remember=not h.get("_nosave"))
        if d.result is None:
            return None
        pw, remember = d.result
        if remember and pw:
            src = h.get("_src", h)  # save on the real server entry, not on this one connection's copy
            src["password_enc"] = protect_password(pw)
            if not src["user"] and user:
                src["user"] = user
            save_hosts(self.hosts)
        return user, pw

    def add_tab(self, widget, title):
        self.nb.add(widget, text=f"  {title}  ")
        self.nb.select(widget)
        self.theme.paint(widget)

    def act_ssh(self, h):
        c = self.credentials(h)
        if c:
            from rc_terminal import TerminalTab
            t = TerminalTab(self, h, *c)
            self.add_tab(t, f"SSH · {h['name']}")
            t.t.focus_set()

    def act_files(self, h):
        c = self.credentials(h, allow_anonymous=h["file_mode"] != "SFTP")
        if c:
            from rc_files import FilesTab
            self.add_tab(FilesTab(self, h, *c), f"Files · {h['name']}")

    def act_ssh_ext(self, h):
        user = self.ask_user(h)
        if not user:
            return
        cmd = ["ssh", "-p", str(h["ssh_port"])]
        env = None
        if h.get("key_file"):
            cmd += ["-i", h["key_file"]]
        elif saved_login(h) and saved_login(h)[0] == user:
            # Hand the saved password to ssh through its askpass hook (kept in the environment, never on the command line)
            askpass = os.path.join(tempfile.gettempdir(), "rc_askpass.bat")
            with open(askpass, "w") as f:
                f.write("@echo off\r\necho %RC_PW%\r\n")
            env = dict(os.environ, SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE="force", DISPLAY="rc:0",
                       RC_PW=unprotect_password(h["password_enc"]))
            cmd += ["-o", "NumberOfPasswordPrompts=1"]
        subprocess.Popen(cmd + [f"{user}@{h['address']}"], creationflags=CREATE_NEW_CONSOLE, env=env)
        self.status.set(f"Opened SSH window for {h['address']}")

    def act_rdp(self, h):
        login = saved_login(h)
        lines = [f"full address:s:{h['address']}:{h['rdp_port']}", f"prompt for credentials:i:{0 if login else 1}"]
        if h["user"]:
            lines.append(f"username:s:{h['user']}")
        path = os.path.join(tempfile.gettempdir(), f"rc_{h['id'][:8]}.rdp")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if login:
            # Windows Credential Manager entry that Remote Desktop picks up, removed again after two minutes
            target = f"TERMSRV/{h['address']}"
            cred_write(target, login[0], login[1])
            log.info("RDP: staged a temporary Credential Manager login for %s (removed in 120 s)", target)
            threading.Timer(120, cred_delete, args=(target,)).start()
        subprocess.Popen(["mstsc.exe", path])
        self.status.set(f"Opened Remote Desktop to {h['address']}" + (" with the saved login" if login else ""))

    def act_vnc(self, h):
        viewer = find_vnc_viewer()
        if not viewer:
            messagebox.showinfo("VNC", "No VNC viewer was found on this PC.\n\nInstall a free one - TightVNC "
                                "(tightvnc.com), TigerVNC or RealVNC Viewer - and click VNC again. RC finds them "
                                "automatically.\n\nIf yours is somewhere else, add  \"vnc_viewer\": \"C:\\\\path\\\\viewer.exe\"  "
                                "to settings.json next to RC.")
            return
        subprocess.Popen(vnc_command(viewer, h["address"], h["vnc_port"]))
        self.status.set(f"Opened {os.path.basename(viewer)} to {h['address']}:{h['vnc_port']} (it asks for the VNC password)")

    def act_rustdesk(self, h):
        target = rustdesk_target(h)
        exe = find_rustdesk()
        if not exe:
            messagebox.showinfo("RustDesk", "RustDesk is not installed on this PC.\n\nGet the free client from "
                                "rustdesk.com, install it, and click RustDesk again. (If it is somewhere unusual, "
                                "add  \"rustdesk_path\": \"C:\\\\path\\\\rustdesk.exe\"  to settings.json next to RC.)")
            return
        pw = unprotect_password(h["rustdesk_password_enc"]) if h.get("rustdesk_password_enc") else ""
        subprocess.Popen(rustdesk_command(exe, target, pw))
        log.info("RustDesk: connecting to %s%s", target, " with the saved password" if pw else "")
        self.status.set(f"Opened RustDesk to {target}" + (" with the saved password" if pw else ""))

    def act_web(self, h):
        webbrowser.open(web_url_for(h))

    def act_mail(self, h):
        url = (h.get("mail_url") or "").strip()
        if not url:
            messagebox.showinfo(APP_NAME, "No mail address set for this server.\nEdit the server and fill in "
                                          "“Mail / webmail address” (for example https://mail.example.com).")
            return
        webbrowser.open(url if "://" in url or url.startswith("mailto:") else "https://" + url)

    def act_ps(self, h):
        q = lambda s: "'" + s.replace("'", "''") + "'"
        cmd = f"Enter-PSSession -ComputerName {q(h['address'])}"
        env, login = None, saved_login(h)
        if login:
            # password travels in the new window's environment, is turned into a credential and then erased
            env = dict(os.environ, RC_USER=login[0], RC_PW=login[1])
            cmd = ("$c = New-Object System.Management.Automation.PSCredential($env:RC_USER, "
                   "(ConvertTo-SecureString $env:RC_PW -AsPlainText -Force)); Remove-Item Env:RC_PW; "
                   f"Enter-PSSession -ComputerName {q(h['address'])} -Credential $c")
        elif h["user"]:
            cmd += f" -Credential {q(h['user'])}"
        subprocess.Popen(["powershell.exe", "-NoExit", "-Command", cmd], creationflags=CREATE_NEW_CONSOLE, env=env)
        self.status.set(f"Opened PowerShell session to {h['address']}")

    def act_wake(self, h):
        if not normalize_mac(h.get("mac")):
            messagebox.showinfo("Wake-on-LAN", "No MAC address is set for this server.\nEdit the server and fill in "
                                "“MAC address (for Wake-on-LAN)” - it looks like AA:BB:CC:DD:EE:FF.\n\n"
                                "The machine also needs Wake-on-LAN switched on in its BIOS and network card settings.")
            return
        sent = send_wol(h["mac"], h["address"])
        self.status.set(f"Wake-on-LAN sent to {h['name']} ({normalize_mac(h['mac'])}). It can take a minute to start.")
        log.info("Wake requested for %s via %s", h["name"], ", ".join(sent))
        for delay in (20000, 45000, 90000):  # watch for it coming up
            self.root.after(delay, self.monitor.refresh)

    def act_verify(self, h):
        errors, warns = validate_host(h, [])
        if errors:
            messagebox.showwarning("Check this server", "\n".join("✖ " + e for e in errors))
            return
        self.status.set(f"Checking {h['address']} ...")

        def work():
            text = format_probe([("warn", w) for w in warns] + probe_host(h))
            self.post(lambda: (self.status.set(""), messagebox.showinfo(f"Check {h['name']}", text)))
        threading.Thread(target=work, daemon=True).start()

    def act_smb(self, h):
        path = (h.get("smb_path") or "").strip() or "\\\\" + h["address"]
        if not path.startswith("\\\\"):
            path = "\\\\" + h["address"] + "\\" + path.lstrip("\\/")
        login = saved_login(h)
        if login:
            # lets File Explorer sign in to this machine without asking; Windows forgets it at sign-out
            cred_write(h["address"], login[0], login[1], kind=2)
        subprocess.Popen(["explorer.exe", path])
        self.status.set(f"Opened {path} in File Explorer" + (" with the saved login" if login else ""))

    def act_ping(self, h):
        self.status.set(f"Pinging {h['address']} ...")

        def work():
            try:
                with timed("Ping x4", h["address"], slow_ms=8000):
                    out = subprocess.run(["ping", "-n", "4", h["address"]], capture_output=True, text=True,
                                         timeout=30, creationflags=CREATE_NO_WINDOW).stdout
            except Exception as e:
                out = str(e)
            self.post(lambda: (self.status.set(""), messagebox.showinfo(f"Ping {h['address']}", out.strip() or "No reply")))
        threading.Thread(target=work, daemon=True).start()


def main():
    setup_logging()
    sys.excepthook = lambda t, v, tb: log.critical("Unhandled error", exc_info=(t, v, tb))
    threading.excepthook = lambda a: log.error("Unhandled error in thread %s", a.thread.name if a.thread else "?",
                                               exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    root = tk.Tk()
    root.report_callback_exception = lambda t, v, tb: log.error("Unhandled UI error", exc_info=(t, v, tb))
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    app = App(root)
    missing = [p for p in check_dependencies() if p.startswith(("paramiko", "pyte"))]
    if missing:
        root.after(500, lambda: messagebox.showwarning(APP_NAME, "\n\n".join(missing), parent=root))
    root.mainloop()
    log.info("=== RC closed ===")


if __name__ == "__main__":
    # rc_files / rc_tools import this module by name, so run that copy: one set of globals, not two
    import rc_manager
    rc_manager.main()
