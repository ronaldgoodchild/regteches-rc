"""RC export / import: pack everything RC knows into ONE file and bring it back on another PC.

  File > Export everything...   writes  RC_export_<PC>_<date>.rcpack   (a zip with a manifest)
  File > Import everything...   reads such a file, merging into (or replacing) what this RC already has

What goes in the pack
  servers (hosts.json: groups, extra connections, tunnels, bookmarks, favorites, jump hosts, notes ...),
  snippets.json, settings.json (colour theme ...), terminal.json (font size), known_hosts (trusted SSH host keys)
  and - only if you give a pack passphrase - the SAVED PASSWORDS and the SSH PRIVATE KEYS the servers use.

Why a passphrase: saved passwords are protected by Windows (DPAPI) for ONE user on ONE PC; the encrypted blobs in
hosts.json are useless anywhere else. On export RC decrypts them and re-encrypts them (with the passphrase you
choose) inside the pack; on import it re-protects them for the new PC. Without a passphrase nothing secret is written.

Kept free of tkinter so it can be tested without a window. Developed by Ronald Goodchild - REGTeches.
"""

import base64
import copy
import io
import json
import os
import re
import shutil
import socket
import time
import zipfile

import rc_manager as core

PACK_FORMAT = 1
PACK_EXT = ".rcpack"
MAX_MEMBER = 25 * 1024 * 1024        # refuse anything bigger inside a pack (zip-bomb guard)
KNOWN_MEMBERS = ("manifest.json", "hosts.json", "snippets.json", "settings.json", "terminal.json", "known_hosts",
                 "secrets.enc")
SECRET_FIELDS = ("password_enc", "rustdesk_password_enc")
log = core.log


class PackError(Exception):
    """A problem the person can read: wrong passphrase, not an RC pack, damaged file ..."""


# ─── helpers ─────────────────────────────────────────────────────────────────

def default_pack_name():
    return f"RC_export_{socket.gethostname()}_{time.strftime('%Y-%m-%d')}{PACK_EXT}"


def _fernet(passphrase, salt):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    key = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def _read_file(path):
    try:
        with open(path, "rb") as f:
            return f.read(MAX_MEMBER + 1)
    except OSError:
        return None


def _clean_host(h):
    """Copy of a server entry with the machine-bound secrets and transient fields removed."""
    c = copy.deepcopy({k: v for k, v in h.items() if not k.startswith("_")})
    for f in SECRET_FIELDS:
        c[f] = ""
    for conn in c.get("connections", []):
        conn.pop("password_enc", None)
    return c


def _key_name(path):
    return re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path)) or "key"


def _collect_secrets(hosts, include_keys):
    """{'passwords': {host_id: {...}}, 'keys': {file name: base64}} - everything that must stay encrypted."""
    pw, keys = {}, {}
    for h in hosts:
        entry = {}
        if h.get("password_enc"):
            entry["password"] = core.unprotect_password(h["password_enc"])
        if h.get("rustdesk_password_enc"):
            entry["rustdesk"] = core.unprotect_password(h["rustdesk_password_enc"])
        extra = {str(i): core.unprotect_password(c["password_enc"])
                 for i, c in enumerate(h.get("connections", [])) if c.get("password_enc")}
        if extra:
            entry["extra"] = extra
        entry = {k: v for k, v in entry.items() if v}  # a blob this PC cannot decrypt gives "", skip it
        if entry:
            pw[h["id"]] = entry
        kf = h.get("key_file") or ""
        if include_keys and kf and os.path.isfile(kf):
            data = _read_file(kf)
            if data is not None and len(data) <= MAX_MEMBER:
                keys[_key_name(kf)] = base64.b64encode(data).decode("ascii")
    return {"passwords": pw, "keys": keys}


# ─── export ──────────────────────────────────────────────────────────────────

def export_pack(path, hosts, passphrase="", include_keys=True):
    """Write the pack. Returns a summary dict (counts) for the confirmation message."""
    members = {}
    members["hosts.json"] = json.dumps({"version": core.HOSTS_VERSION, "hosts": [_clean_host(h) for h in hosts]},
                                       indent=2).encode("utf-8")
    for name, src in (("snippets.json", core.SNIPPETS_FILE), ("settings.json", core.SETTINGS_FILE),
                      ("terminal.json", os.path.join(core.APP_DIR, "terminal.json")),
                      ("known_hosts", core.KNOWN_HOSTS)):
        data = _read_file(src)
        if data is not None and len(data) <= MAX_MEMBER:
            members[name] = data
    manifest = {"format": PACK_FORMAT, "app": "REGTeches RC", "app_version": core.APP_VERSION,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"), "from_pc": socket.gethostname(),
                "servers": len(hosts), "secrets": False}
    summary = {"servers": len(hosts), "passwords": 0, "keys": 0,
               "tunnels": sum(len(h.get("tunnels", [])) for h in hosts),
               "snippets": 0, "trusted_keys": 0}
    try:
        summary["snippets"] = len(json.loads(members["snippets.json"])) if "snippets.json" in members else 0
    except ValueError:
        pass
    if "known_hosts" in members:
        summary["trusted_keys"] = sum(1 for ln in members["known_hosts"].decode("utf-8", "replace").splitlines()
                                      if ln.strip() and not ln.lstrip().startswith("#"))
    if passphrase:
        secrets = _collect_secrets(hosts, include_keys)
        summary["passwords"] = sum(len(v) for v in secrets["passwords"].values())
        summary["keys"] = len(secrets["keys"])
        salt = os.urandom(16)
        manifest["secrets"] = True
        manifest["salt"] = base64.b64encode(salt).decode("ascii")
        members["secrets.enc"] = _fernet(passphrase, salt).encrypt(json.dumps(secrets).encode("utf-8"))
    members["manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")
    tmp = path + ".tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name in ("manifest.json",) + tuple(n for n in KNOWN_MEMBERS if n in members and n != "manifest.json"):
                z.writestr(name, members[name])
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    log.info("Exported %d server(s) to %s (passwords: %d, keys: %d)", summary["servers"], path,
             summary["passwords"], summary["keys"])
    return summary


# ─── import ──────────────────────────────────────────────────────────────────

class Pack:
    """An opened pack: manifest + the plain parts. Secrets stay encrypted until decrypt() is called."""
    def __init__(self, manifest, parts):
        self.manifest, self.parts = manifest, parts

    @property
    def has_secrets(self):
        return bool(self.manifest.get("secrets")) and "secrets.enc" in self.parts

    def describe(self):
        m = self.manifest
        try:
            n_hosts = len(json.loads(self.parts["hosts.json"])["hosts"])
        except (KeyError, ValueError, TypeError):
            n_hosts = m.get("servers", 0)
        return (f"{n_hosts} server(s) from {m.get('from_pc', '?')} (RC {m.get('app_version', '?')}), "
                f"saved {m.get('created', '?')}" + ("; includes passwords and keys" if self.has_secrets else ""))

    def decrypt(self, passphrase):
        if not self.has_secrets:
            return {"passwords": {}, "keys": {}}
        try:
            salt = base64.b64decode(self.manifest["salt"])
            raw = _fernet(passphrase, salt).decrypt(self.parts["secrets.enc"])
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # InvalidToken, bad base64, bad JSON
            raise PackError("That passphrase did not open the pack (or the pack is damaged).") from e
        data.setdefault("passwords", {})
        data.setdefault("keys", {})
        return data


def open_pack(path):
    """Read a pack WITHOUT touching anything. Only the known member names are read, never extracted by name."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "manifest.json" not in names or "hosts.json" not in names:
                raise PackError("That file is not an RC export (no manifest).")
            parts = {}
            for name in KNOWN_MEMBERS:
                if name in names:
                    info = z.getinfo(name)
                    if info.file_size > MAX_MEMBER:
                        raise PackError(f"{name} inside the pack is too large to be genuine.")
                    parts[name] = z.read(name)
        manifest = json.loads(parts["manifest.json"].decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("app") != "REGTeches RC":
            raise PackError("That file is not an RC export.")
        if int(manifest.get("format", 0)) > PACK_FORMAT:
            raise PackError("This pack was made by a newer RC. Update RC on this PC, then import again.")
        json.loads(parts["hosts.json"].decode("utf-8"))
    except PackError:
        raise
    except (OSError, zipfile.BadZipFile, ValueError, KeyError) as e:
        raise PackError(f"Could not read that file: {e}") from e
    return Pack(manifest, parts)


def backup_current(stamp=None):
    """Copy this RC's data files to <data folder>/backups/before-import-<time>/ and return that folder."""
    folder = os.path.join(core.APP_DIR, "backups", "before-import-" + (stamp or time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(folder, exist_ok=True)
    for src in (core.HOSTS_FILE, core.SNIPPETS_FILE, core.SETTINGS_FILE, core.KNOWN_HOSTS,
                os.path.join(core.APP_DIR, "terminal.json")):
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(folder, os.path.basename(src)))
    return folder


def _same_server(a, b):
    return a.get("id") == b.get("id") or (a["address"].lower() == b["address"].lower()
                                          and a.get("name", "").lower() == b.get("name", "").lower())


def _unique_path(path):
    base, ext = os.path.splitext(path)
    n, cand = 1, path
    while os.path.exists(cand):
        n += 1
        cand = f"{base}-{n}{ext}"
    return cand


def _restore_keys(keys, ssh_dir):
    """Write exported private keys into ssh_dir without ever overwriting an existing file. {name: new path}"""
    out = {}
    for name, b64 in keys.items():
        data = base64.b64decode(b64)
        target = os.path.join(ssh_dir, _key_name(name))
        if os.path.isfile(target) and _read_file(target) == data:
            out[name] = target  # already here, identical
            continue
        target = _unique_path(target)
        os.makedirs(ssh_dir, exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
        out[name] = target
    return out


def import_pack(pack, hosts, mode="merge", passphrase="", parts=("servers", "snippets", "settings", "known_hosts"),
                ssh_dir=None):
    """Bring a pack into this RC. `hosts` is the live server list; the NEW list is returned (the caller saves it)
    with a report dict. mode: 'merge' (add new, update matching servers, keep everything else) or 'replace'.
    parts: any of servers, snippets, settings, known_hosts. Secrets are restored when the pack has them and
    `passphrase` opens it. Nothing is written until the pack and passphrase have been verified."""
    secrets = pack.decrypt(passphrase) if (pack.has_secrets and passphrase) else {"passwords": {}, "keys": {}}
    report = {"added": 0, "updated": 0, "unchanged": 0, "passwords": 0, "keys": 0, "snippets": 0,
              "known_hosts": 0, "settings": False, "backup": "", "skipped": []}
    report["backup"] = backup_current()
    new_hosts = list(hosts)
    if "servers" in parts:
        incoming, problems = [], []
        for h in json.loads(pack.parts["hosts.json"].decode("utf-8")).get("hosts", []):
            if isinstance(h, dict) and isinstance(h.get("address"), str) and h["address"].strip():
                incoming.append(core.sanitize_host(h, problems))
            else:
                report["skipped"].append("an entry with no address")
        key_paths = _restore_keys(secrets["keys"], ssh_dir or os.path.join(os.path.expanduser("~"), ".ssh")) \
            if secrets["keys"] else {}
        report["keys"] = len(key_paths)
        for h in incoming:
            sec = secrets["passwords"].get(h["id"], {})
            if sec.get("password"):
                h["password_enc"] = core.protect_password(sec["password"])
                report["passwords"] += 1
            if sec.get("rustdesk"):
                h["rustdesk_password_enc"] = core.protect_password(sec["rustdesk"])
                report["passwords"] += 1
            for idx, pw in (sec.get("extra") or {}).items():
                try:
                    h["connections"][int(idx)]["password_enc"] = core.protect_password(pw)
                    report["passwords"] += 1
                except (IndexError, ValueError, KeyError):
                    pass
            kf = h.get("key_file") or ""
            if kf and _key_name(kf) in key_paths:
                h["key_file"] = key_paths[_key_name(kf)]  # the key now lives in THIS PC's .ssh folder
            elif kf and not os.path.isfile(kf):
                h["key_file"] = ""  # a path from the other PC that does not exist here
        if mode == "replace":
            report["updated"] = len(incoming)
            new_hosts = incoming
        else:
            for h in incoming:
                match = next((i for i, o in enumerate(new_hosts) if _same_server(o, h)), None)
                if match is None:
                    new_hosts.append(h)
                    report["added"] += 1
                else:
                    old = new_hosts[match]
                    for f in SECRET_FIELDS:   # never lose a password this PC already has
                        if not h.get(f) and old.get(f):
                            h[f] = old[f]
                    h["id"] = old.get("id", h["id"])
                    if old == h:
                        report["unchanged"] += 1
                    else:
                        new_hosts[match] = h
                        report["updated"] += 1
    if "snippets" in parts and "snippets.json" in pack.parts:
        try:
            snips = [s for s in json.loads(pack.parts["snippets.json"].decode("utf-8"))
                     if isinstance(s, dict) and s.get("name") and s.get("command")]
            if mode == "merge":
                have = {s.get("id") or s["name"] for s in core.load_snippets()}
                current = core.load_snippets()
                snips = current + [s for s in snips if (s.get("id") or s["name"]) not in have]
            core.save_snippets(snips)
            report["snippets"] = len(snips)
        except (ValueError, TypeError, OSError) as e:
            report["skipped"].append(f"snippets ({e})")
    if "settings" in parts:
        for name, dest in (("settings.json", core.SETTINGS_FILE),
                           ("terminal.json", os.path.join(core.APP_DIR, "terminal.json"))):
            if name in pack.parts:
                try:
                    json.loads(pack.parts[name].decode("utf-8"))
                    with open(dest, "wb") as f:
                        f.write(pack.parts[name])
                    report["settings"] = True
                except (ValueError, OSError) as e:
                    report["skipped"].append(f"{name} ({e})")
    if "known_hosts" in parts and "known_hosts" in pack.parts:
        try:
            incoming_lines = [ln for ln in pack.parts["known_hosts"].decode("utf-8", "replace").splitlines() if ln.strip()]
            with core.KNOWN_HOSTS_LOCK:
                have = []
                if mode == "merge" and os.path.isfile(core.KNOWN_HOSTS):
                    with open(core.KNOWN_HOSTS, "r", encoding="utf-8", errors="replace") as f:
                        have = [ln.rstrip("\n") for ln in f if ln.strip()]
                merged = have + [ln for ln in incoming_lines if ln not in set(have)]
                with open(core.KNOWN_HOSTS, "w", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(merged) + ("\n" if merged else ""))
            report["known_hosts"] = len(merged)
        except OSError as e:
            report["skipped"].append(f"known_hosts ({e})")
    log.info("Imported pack: %s", {k: v for k, v in report.items() if k != "skipped"})
    return new_hosts, report
