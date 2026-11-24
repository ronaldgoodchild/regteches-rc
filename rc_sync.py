"""REGTeches RC - folder sync (File > Backup > Sync through a folder).

Keeps servers, snippets, workspaces, identities, proxies, notes and trusted host keys the same on several PCs, using any folder
the PCs share (OneDrive, Dropbox, a NAS share, a USB stick).

How it works: every PC writes ONE file of its own into the folder (RC-sync-<pc>-<id>.rcsync, locked with the sync password: scrypt +
AES-256-GCM, saved passwords travel opened inside it and are re-locked for each PC's Windows account) and reads the files of the
other PCs. Nobody ever writes someone else's file, so the cloud service never makes "conflicted copy" files.

Merging is per record: the newest change wins. RC notices changes by remembering a fingerprint of every record from the last sync
(sync_state.json), so no other part of RC had to learn about timestamps. A deleted record leaves a small "deleted" marker for 90 days
so other PCs delete it too instead of bringing it back. On the very first sync, records that already exist in the folder win over this
PC's copy of the same record (so a new PC picks up the shared version rather than overwriting it).

The merge itself (`merge_entries`, `local_view`) is pure and tested without a window; `sync_now` does the file work."""

import hashlib
import json
import os
import platform
import re
import time
import uuid

import rc_backup
import rc_manager as core

log = core.log

FORMAT = "RC-SYNC"
VERSION = 1
AAD = b"RC-SYNC/1"
EXT = ".rcsync"
TOMBSTONE_DAYS = 90
COLLECTIONS = ("hosts", "snippets", "workspaces", "identities", "proxies", "notes")
LABELS = {"hosts": "Servers", "snippets": "Snippets", "workspaces": "Workspaces", "identities": "Identities", "proxies": "Proxies", "notes": "Notes"}
LOCAL_ONLY = {"hosts": ("last_path",)}      # fields each PC keeps for itself (not compared, not copied over)


class SyncError(Exception):
    """A readable problem to show the user."""


def config_path():
    return os.path.join(core.APP_DIR, "sync.json")


def state_path():
    return os.path.join(core.APP_DIR, "sync_state.json")


# ─── settings and remembered state ───────────────────────────────────────────

def clean_config(d):
    d = d if isinstance(d, dict) else {}
    dev = d.get("device") if isinstance(d.get("device"), str) and re.fullmatch(r"[0-9a-f]{8}", d.get("device", "")) else uuid.uuid4().hex[:8]
    return {"folder": d["folder"] if isinstance(d.get("folder"), str) else "",
            "password_enc": d["password_enc"] if isinstance(d.get("password_enc"), str) else "",
            "auto": bool(d.get("auto", False)), "device": dev,
            "last": d["last"] if isinstance(d.get("last"), str) else ""}


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return clean_config(json.load(f))
    except (OSError, ValueError):
        return clean_config({})


def save_config(cfg):
    _write_json(config_path(), clean_config(cfg))


def get_password(cfg):
    return core.unprotect_password(cfg["password_enc"]) if cfg.get("password_enc") else ""


def set_password(cfg, pw):
    cfg["password_enc"] = core.protect_password(pw) if pw else ""


def load_state():
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    out = {}
    for coll in COLLECTIONS:
        out[coll] = {}
        items = data.get(coll) if isinstance(data, dict) else None
        for k, v in (items.items() if isinstance(items, dict) else []):
            if isinstance(v, dict) and isinstance(v.get("t"), (int, float)):
                out[coll][k] = {"t": float(v["t"]), "h": str(v.get("h", "")), "gone": bool(v.get("gone"))}
    return out


def save_state(state):
    _write_json(state_path(), state)


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# ─── encryption of one PC's file ─────────────────────────────────────────────

def seal(payload, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64
    if not password:
        raise SyncError("Set a sync password first.")
    salt, nonce = os.urandom(16), os.urandom(12)
    data = AESGCM(rc_backup._key(password, salt, **rc_backup.SCRYPT)).encrypt(nonce, json.dumps(payload).encode("utf-8"), AAD)
    box = {"format": FORMAT, "version": VERSION, "kdf": dict(name="scrypt", salt=base64.b64encode(salt).decode(), **rc_backup.SCRYPT),
           "nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(data).decode()}
    return json.dumps(box, indent=1).encode("utf-8")


def unseal(blob, password):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64
    try:
        box = json.loads(blob.decode("utf-8-sig"))
        if box.get("format") != FORMAT:
            raise ValueError
        kdf = box["kdf"]
        salt, nonce, data = (base64.b64decode(x) for x in (kdf["salt"], box["nonce"], box["data"]))
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        raise SyncError("not an RC sync file (or damaged)")
    if int(box.get("version", 0)) > VERSION:
        raise SyncError("made by a newer RC")
    if not 2 ** 10 <= n <= 2 ** 20 or r > 32 or p > 8:
        raise SyncError("not an RC sync file (or damaged)")
    try:
        plain = AESGCM(rc_backup._key(password, salt, n, r, p)).decrypt(nonce, data, AAD)
        payload = json.loads(plain.decode("utf-8"))
    except InvalidTag:
        raise SyncError("different sync password")
    except ValueError:
        raise SyncError("could not be read")
    if not isinstance(payload, dict) or not isinstance(payload.get("coll"), dict):
        raise SyncError("could not be read")
    return payload


# ─── records ─────────────────────────────────────────────────────────────────

def _key(coll, rec):
    if coll == "snippets":
        return str(rec.get("id") or rec.get("name") or "")
    if coll == "workspaces":
        return str(rec.get("name", "")).lower()
    return str(rec.get("id", ""))


def digest(coll, rec):
    if rec is None:
        return ""
    skip = LOCAL_ONLY.get(coll, ())
    clean = {k: v for k, v in rec.items() if k not in skip}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]


def collect(hosts):
    """This PC's data as {collection: {key: portable record}} (saved passwords opened) + the trusted host keys text."""
    bundle, _lost = rc_backup.make_bundle(hosts)
    out = {}
    for coll in COLLECTIONS:
        recs = {}
        for rec in bundle.get(coll, []):
            if isinstance(rec, dict):
                k = _key(coll, rec)
                if k:
                    recs[k] = rec
        out[coll] = recs
    return out, bundle.get("known_hosts", "")


def local_view(coll, records, state, remote_keys, now):
    """Turn this PC's records into sync entries {key: {t, d, h}}. A record that is new or changed since the last sync gets `now`;
    one that is gone since the last sync becomes a deletion marker."""
    out = {}
    for k, rec in records.items():
        h = digest(coll, rec)
        st = state.get(k)
        if st and not st["gone"] and st["h"] == h:
            t = st["t"]
        elif st is None and k in remote_keys:
            t = 0.0                                   # first sync: the shared copy wins over this PC's copy
        else:
            t = now
        out[k] = {"t": t, "d": rec, "h": h, "src": "local"}
    for k, st in state.items():
        if k not in records:
            out[k] = {"t": st["t"] if st["gone"] else now, "d": None, "h": "", "src": "local"}
    return out


def merge_entries(local, remote, now):
    """Merge two {key: entry} maps; the newer `t` wins, a tie keeps `local`. Old deletion markers are dropped."""
    merged = {}
    for k in set(local) | set(remote):
        a, b = local.get(k), remote.get(k)
        win = a if b is None or (a is not None and a["t"] >= b["t"]) else b
        if win["d"] is None and now - win["t"] > TOMBSTONE_DAYS * 86400:
            continue
        merged[k] = win
    return merged


def _remote_entries(payloads):
    """Fold the other PCs' files into one {coll: {key: entry}} (newest entry per key)."""
    out = {c: {} for c in COLLECTIONS}
    for p in payloads:
        for coll in COLLECTIONS:
            items = p["coll"].get(coll)
            for k, e in (items.items() if isinstance(items, dict) else []):
                if not isinstance(e, dict) or not isinstance(e.get("t"), (int, float)):
                    continue
                d = e.get("d")
                if d is not None and not isinstance(d, dict):
                    continue
                cur = out[coll].get(k)
                if cur is None or e["t"] > cur["t"]:
                    out[coll][k] = {"t": float(e["t"]), "d": d, "h": digest(coll, d), "src": "remote"}
    return out


# ─── putting remote records into this PC ─────────────────────────────────────

def _lock_identity(rec):
    rec = dict(rec)
    rec["secret_enc"] = rc_backup._lock(rec.pop("secret", "") or "")
    return core.clean_identity(rec)


def _lock_proxy(rec):
    rec = dict(rec)
    rec["password_enc"] = rc_backup._lock(rec.pop("password", "") or "")
    return core.clean_proxy(rec)


def _convert(coll, rec):
    """Portable record -> the form this PC stores (or None if it is unusable)."""
    if coll == "hosts":
        h = rc_backup.restore_host(rec)
        return h if h.get("id") else None
    if coll == "identities":
        return _lock_identity(rec)
    if coll == "proxies":
        return _lock_proxy(rec)
    if coll == "notes":
        import rc_notes
        return rc_notes.clean_note(rec)
    if coll == "snippets":
        return dict(rec) if rec.get("name") and rec.get("command") else None
    if coll == "workspaces":
        return dict(rec) if rec.get("name") and isinstance(rec.get("hosts"), list) else None
    return None


def apply_changes(coll, current, merged, local_records):
    """Return (new list, added, updated, deleted) after applying the entries that came from other PCs
    (a record that is already identical here is left untouched)."""
    keyed = {}
    order = []
    for rec in current:
        k = _key_stored(coll, rec)
        if k not in keyed:
            order.append(k)
        keyed[k] = rec
    added = updated = deleted = 0
    for k, e in merged.items():
        if e["src"] != "remote":
            continue
        if e["d"] is None:
            if k in keyed:
                del keyed[k]
                order.remove(k)
                deleted += 1
            continue
        if k in local_records and digest(coll, local_records[k]) == e["h"]:
            continue
        new = _convert(coll, e["d"])
        if new is None:
            continue
        if k in keyed:
            if coll == "hosts":
                for f in LOCAL_ONLY["hosts"]:
                    if keyed[k].get(f):
                        new[f] = keyed[k][f]
            keyed[k] = new
            updated += 1
        else:
            if coll == "hosts":
                new["last_path"] = ""                  # another PC's folder is no use here
            keyed[k] = new
            order.append(k)
            added += 1
    return [keyed[k] for k in order], added, updated, deleted


def _key_stored(coll, rec):
    """The key of a record as this PC stores it (same rule as `_key`)."""
    return _key(coll, rec)


# ─── the whole sync ──────────────────────────────────────────────────────────

def device_filename(cfg):
    name = re.sub(r"[^A-Za-z0-9_-]+", "", platform.node() or "pc")[:20] or "pc"
    return f"RC-sync-{name}-{cfg['device']}{EXT}"


def sync_now(hosts, cfg, password, now=None):
    """Run one sync. `hosts` is the live server list. Writes this PC's file into the folder and updates local data.
    Returns (new_hosts, report) where report = {"counts": {coll: (added, updated, deleted)}, "files": n, "skipped": [(file, why)],
    "host_keys": n}. Raises SyncError for problems that stop the sync."""
    now = time.time() if now is None else now
    folder = cfg.get("folder", "")
    if not folder:
        raise SyncError("Choose the sync folder first.")
    if not os.path.isdir(folder):
        raise SyncError(f"The sync folder is not available:\n{folder}")
    if not password:
        raise SyncError("Set a sync password first.")
    mine = device_filename(cfg)
    payloads, skipped = [], []
    for fn in sorted(os.listdir(folder)):
        if not (fn.startswith("RC-sync-") and fn.endswith(EXT)) or fn == mine:
            continue
        try:
            with open(os.path.join(folder, fn), "rb") as f:
                payloads.append(unseal(f.read(), password))
        except (OSError, SyncError) as e:
            skipped.append((fn, str(e)))
            log.warning("sync: skipped %s (%s)", fn, e)
    remote = _remote_entries(payloads)
    state = load_state()
    records, known = collect(hosts)

    merged_all, report_counts = {}, {}
    new_hosts = list(hosts)
    for coll in COLLECTIONS:
        local = local_view(coll, records[coll], state[coll], set(remote[coll]), now)
        merged = merge_entries(local, remote[coll], now)
        merged_all[coll] = merged
        if coll == "hosts":
            current = list(hosts)
        else:
            current = _load_collection(coll)
        new_list, a, u, d = apply_changes(coll, current, merged, records[coll])
        report_counts[coll] = (a, u, d)
        if a or u or d:
            if coll == "hosts":
                new_hosts = new_list
            else:
                _save_collection(coll, new_list)

    # trusted host keys: everybody's lines together
    have = set(l for l in rc_backup.read_known_hosts().splitlines() if l.strip())
    theirs = []
    for p in payloads:
        for line in str(p.get("known_hosts") or "").splitlines():
            if line.strip() and line not in have and line not in theirs:
                theirs.append(line)
    if theirs:
        with core.KNOWN_HOSTS_LOCK:
            existing = rc_backup.read_known_hosts()
            with open(core.KNOWN_HOSTS, "a", encoding="utf-8") as f:
                f.write(("" if not existing or existing.endswith("\n") else "\n") + "\n".join(theirs) + "\n")
    all_known = sorted(have | set(theirs))

    # write this PC's file, then remember what everyone agreed on
    payload = {"device": platform.node(), "id": cfg["device"], "written": now, "app_version": core.APP_VERSION,
               "coll": {c: {k: {"t": e["t"], "d": e["d"]} for k, e in merged_all[c].items()} for c in COLLECTIONS},
               "known_hosts": "\n".join(all_known)}
    blob = seal(payload, password)
    tmp = os.path.join(folder, mine + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, os.path.join(folder, mine))
    except OSError as e:
        raise SyncError(f"Could not write into the sync folder:\n{e}")

    new_records, _ = collect(new_hosts)
    new_state = {}
    for coll in COLLECTIONS:
        new_state[coll] = {}
        for k, e in merged_all[coll].items():
            if e["d"] is None:
                new_state[coll][k] = {"t": e["t"], "h": "", "gone": True}
            elif k in new_records[coll]:
                new_state[coll][k] = {"t": e["t"], "h": digest(coll, new_records[coll][k]), "gone": False}
    save_state(new_state)
    cfg["last"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    save_config(cfg)
    return new_hosts, {"counts": report_counts, "files": len(payloads), "skipped": skipped, "host_keys": len(theirs)}


def _load_collection(coll):
    import rc_notes
    return {"snippets": core.load_snippets, "workspaces": core.load_workspaces, "identities": core.load_identities,
            "proxies": core.load_proxies, "notes": rc_notes.load_notes}[coll]()


def _save_collection(coll, items):
    import rc_notes
    {"snippets": core.save_snippets, "workspaces": core.save_workspaces, "identities": core.save_identities,
     "proxies": core.save_proxies, "notes": rc_notes.save_notes}[coll](items)


def describe(report):
    lines = []
    for coll in COLLECTIONS:
        a, u, d = report["counts"][coll]
        bits = [f"{a} added"] * bool(a) + [f"{u} updated"] * bool(u) + [f"{d} deleted"] * bool(d)
        lines.append(f"{LABELS[coll]}: " + (", ".join(bits) if bits else "no change"))
    if report["host_keys"]:
        lines.append(f"Trusted host keys added: {report['host_keys']}")
    lines.append(f"Read {report['files']} file(s) from other PCs.")
    for fn, why in report["skipped"]:
        lines.append(f"Skipped {fn}: {why}")
    return "\n".join(lines)


def changed(report):
    return any(any(c) for c in report["counts"].values())


# ─── running it from the app ─────────────────────────────────────────────────

def run(app, quiet=False):
    """Sync using the saved settings. quiet=True (automatic runs) only reports on the status bar."""
    from tkinter import messagebox
    cfg = load_config()
    try:
        new_hosts, report = sync_now(app.hosts, cfg, get_password(cfg))
    except SyncError as e:
        if quiet:
            app.status.set("Sync skipped: " + str(e).splitlines()[0])
        else:
            messagebox.showerror("Sync", str(e), parent=app.root)
        return None
    except Exception as e:
        log.exception("sync failed")
        if quiet:
            app.status.set(f"Sync failed: {e}")
        else:
            messagebox.showerror("Sync", f"Sync failed:\n{e}", parent=app.root)
        return None
    if report["counts"]["hosts"] != (0, 0, 0):
        app.hosts[:] = new_hosts
        app.commit()
    app.status.set("Synced " + time.strftime("%H:%M") + (" (changes from other PCs applied)" if changed(report) else " (already up to date)"))
    if not quiet:
        messagebox.showinfo("Sync", describe(report), parent=app.root)
    return report


def auto_sync_start(app):
    cfg = load_config()
    if cfg["auto"] and cfg["folder"] and cfg["password_enc"]:
        run(app, quiet=True)


def auto_sync_exit(app):
    cfg = load_config()
    if cfg["auto"] and cfg["folder"] and cfg["password_enc"]:
        run(app, quiet=True)


def open_dialog(app):
    from tkinter import filedialog, messagebox, ttk
    import tkinter as tk
    cfg = load_config()
    win = tk.Toplevel(app.root)
    win.title("Sync through a folder")
    win.transient(app.root)
    body = ttk.Frame(win, padding=14)
    body.pack(fill="both", expand=True)
    ttk.Label(body, wraplength=460, justify="left", text="Keeps servers, snippets, workspaces, identities, proxies and notes the same on your "
              "PCs through a folder they all can reach (OneDrive, Dropbox, a network share). Use the same folder and the same sync password "
              "on every PC. Each PC writes its own encrypted file there.").pack(anchor="w")
    folder = tk.StringVar(value=cfg["folder"])
    pw = tk.StringVar(value=get_password(cfg))
    auto = tk.BooleanVar(value=cfg["auto"])
    grid = ttk.Frame(body)
    grid.pack(fill="x", pady=10)
    ttk.Label(grid, text="Sync folder").grid(row=0, column=0, sticky="w")
    ttk.Entry(grid, textvariable=folder, width=44).grid(row=0, column=1, padx=6, pady=3)
    ttk.Button(grid, text="Browse…", command=lambda: folder.set(filedialog.askdirectory(parent=win, initialdir=folder.get() or None) or folder.get())).grid(row=0, column=2)
    ttk.Label(grid, text="Sync password").grid(row=1, column=0, sticky="w")
    ttk.Entry(grid, textvariable=pw, show="•", width=44).grid(row=1, column=1, padx=6, pady=3)
    ttk.Checkbutton(body, text="Sync automatically when RC starts and when it closes", variable=auto).pack(anchor="w")
    ttk.Label(body, foreground="#555", text="Last sync: " + (cfg["last"] or "never")).pack(anchor="w", pady=(6, 0))

    def store():
        cfg["folder"], cfg["auto"] = folder.get().strip(), auto.get()
        set_password(cfg, pw.get())
        try:
            save_config(cfg)
        except OSError as e:
            messagebox.showerror("Sync", f"Could not save the settings:\n{e}", parent=win)
            return False
        return True

    def go():
        if store():
            run(app)
            win.destroy()

    bar = ttk.Frame(body)
    bar.pack(fill="x", pady=(10, 0))
    ttk.Button(bar, text="Sync now", command=go).pack(side="left")
    ttk.Button(bar, text="Save settings", command=lambda: store() and win.destroy()).pack(side="left", padx=6)
    ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right")
    return win
