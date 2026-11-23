"""REGTeches RC - encrypted backup and restore (File > Back up everything / Restore from a backup).

Why: saved passwords in hosts.json are encrypted for ONE Windows account on ONE PC (DPAPI), so copying hosts.json to
another PC loses them. A backup opens the saved passwords, then locks the whole bundle with a password you choose
(scrypt key + AES-256-GCM), so it can be restored on any PC; restoring re-encrypts them for that PC and account.

Contents: servers (with their extra connections, tunnels, bookmarks), snippets, workspaces, identities, proxies, notes and
trusted host keys. Not included: SSH key files themselves (only their paths), command history, and settings.
The pure functions at the top can be tested without a window."""

import base64
import json
import os
import time

import rc_manager as core

log = core.log

FORMAT = "RC-BACKUP"
VERSION = 1
AAD = b"RC-BACKUP/1"
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


class BackupError(Exception):
    """A readable problem: wrong password, not a backup, damaged or too new."""


# ─── encryption ──────────────────────────────────────────────────────────────

def _key(password, salt, n, r, p):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def encrypt(bundle, password):
    """dict -> bytes (a JSON container holding the AES-GCM encrypted bundle)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not password:
        raise BackupError("Choose a password for the backup.")
    salt, nonce = os.urandom(16), os.urandom(12)
    data = AESGCM(_key(password, salt, **SCRYPT)).encrypt(nonce, json.dumps(bundle).encode("utf-8"), AAD)
    box = {"format": FORMAT, "version": VERSION, "kdf": dict(name="scrypt", salt=base64.b64encode(salt).decode(), **SCRYPT),
           "nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(data).decode()}
    return json.dumps(box, indent=1).encode("utf-8")


def decrypt(blob, password):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        box = json.loads(blob.decode("utf-8-sig"))
        if box.get("format") != FORMAT:
            raise ValueError
        kdf = box["kdf"]
        salt, nonce, data = (base64.b64decode(x) for x in (kdf["salt"], box["nonce"], box["data"]))
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        raise BackupError("That file is not an RC backup (or it is damaged).")
    if int(box.get("version", 0)) > VERSION:
        raise BackupError("This backup was made by a newer RC. Update RC first.")
    if not 2 ** 10 <= n <= 2 ** 20 or r > 32 or p > 8:
        raise BackupError("That file is not an RC backup (or it is damaged).")
    try:
        plain = AESGCM(_key(password, salt, n, r, p)).decrypt(nonce, data, AAD)
    except InvalidTag:
        raise BackupError("Wrong password, or the file is damaged.")
    try:
        bundle = json.loads(plain.decode("utf-8"))
    except ValueError:
        raise BackupError("The backup could not be read.")
    if not isinstance(bundle, dict) or not isinstance(bundle.get("hosts"), list):
        raise BackupError("The backup could not be read.")
    return bundle


# ─── making and applying a bundle ────────────────────────────────────────────

def _open(blob):
    """DPAPI blob -> plain text ('' if it cannot be opened here)."""
    if not blob:
        return ""
    try:
        return core.unprotect_password(blob) or ""
    except Exception:
        return ""


def _lock(text):
    return core.protect_password(text) if text else ""


def portable_host(h):
    """A copy of a server with its saved passwords written in clear (they only ever exist inside the encrypted bundle)."""
    out = {k: json.loads(json.dumps(v)) for k, v in h.items() if not k.startswith("_")}
    lost = 0
    for blob_key, plain_key in (("password_enc", "password"), ("rustdesk_password_enc", "rustdesk_password")):
        blob = out.pop(blob_key, "")
        out[plain_key] = _open(blob)
        lost += bool(blob and not out[plain_key])
    for c in out.get("connections", []):
        blob = c.pop("password_enc", "")
        if blob:
            c["password"] = _open(blob)
            lost += not c["password"]
    return out, lost


def restore_host(d):
    h = json.loads(json.dumps(d))
    for blob_key, plain_key in (("password_enc", "password"), ("rustdesk_password_enc", "rustdesk_password")):
        h[blob_key] = _lock(h.pop(plain_key, "") or "")
    for c in h.get("connections", []):
        pw = c.pop("password", "")
        if pw:
            c["password_enc"] = _lock(pw)
    return core.sanitize_host(h)


def read_known_hosts():
    try:
        with open(core.KNOWN_HOSTS, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def make_bundle(hosts):
    """Everything RC keeps, in one dict. Returns (bundle, number of saved passwords that could not be opened)."""
    lost, out_hosts = 0, []
    for h in hosts:
        ph, n = portable_host(h)
        out_hosts.append(ph)
        lost += n
    idents = []
    for i in core.load_identities():
        i = dict(i)
        i["secret"] = _open(i.pop("secret_enc", ""))
        idents.append(i)
    proxies = []
    for p in core.load_proxies():
        p = dict(p)
        p["password"] = _open(p.pop("password_enc", ""))
        proxies.append(p)
    return ({"app": "RC", "app_version": core.APP_VERSION, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
             "hosts": out_hosts, "snippets": core.load_snippets(), "workspaces": core.load_workspaces(),
             "identities": idents, "proxies": proxies, "known_hosts": read_known_hosts(),
             "notes": __import__("rc_notes").load_notes()}, lost)


def _match_key(h):
    return (h.get("name", "").lower(), h.get("address", "").lower())


def merge_list(existing, incoming, replace, key):
    """Merge two lists of dicts by `key(item)`. Returns (merged, added, replaced, skipped)."""
    merged, added, replaced, skipped = list(existing), 0, 0, 0
    index = {key(x): n for n, x in enumerate(merged)}
    for item in incoming:
        k = key(item)
        if k in index:
            if replace:
                merged[index[k]] = item
                replaced += 1
            else:
                skipped += 1
        else:
            index[k] = len(merged)
            merged.append(item)
            added += 1
    return merged, added, replaced, skipped


def apply_bundle(bundle, hosts, replace):
    """Merge a bundle into this PC's data. `hosts` is the live server list. Writes the small stores and returns
    (new_hosts, report); the caller saves/refreshes the server list. replace=True overwrites matches, else new ones only."""
    report = {}
    # servers: match by id, else by name + address
    current, incoming = list(hosts), [restore_host(h) for h in bundle["hosts"] if isinstance(h, dict)]
    id_map, by_id, by_name = {}, {h["id"]: n for n, h in enumerate(current)}, {_match_key(h): n for n, h in enumerate(current)}
    added = replaced = skipped = 0
    for h in incoming:
        n = by_id.get(h["id"], by_name.get(_match_key(h)))
        if n is None:
            current.append(h)
            by_id[h["id"]], by_name[_match_key(h)] = len(current) - 1, len(current) - 1
            id_map[h["id"]] = h["id"]
            added += 1
        elif replace:
            keep = current[n]["id"]
            id_map[h["id"]] = keep
            h["id"] = keep
            current[n] = h
            replaced += 1
        else:
            id_map[h["id"]] = current[n]["id"]
            skipped += 1
    for h in current:                         # a jump server that was matched to an existing entry keeps pointing at it
        if h.get("jump_host") in id_map:
            h["jump_host"] = id_map[h["jump_host"]]
    report["servers"] = (added, replaced, skipped)

    snips, a, r, s = merge_list(core.load_snippets(), [x for x in bundle.get("snippets", []) if isinstance(x, dict) and x.get("name") and x.get("command")],
                                replace, lambda x: x.get("id") or x["name"])
    if a or r:
        core.save_snippets(snips)
    report["snippets"] = (a, r, s)

    wss = []
    for w in bundle.get("workspaces", []):
        if isinstance(w, dict) and w.get("name") and isinstance(w.get("hosts"), list):
            wss.append(dict(w, hosts=[id_map.get(i, i) for i in w["hosts"]]))
    merged, a, r, s = merge_list(core.load_workspaces(), wss, replace, lambda x: x["name"].lower())
    if a or r:
        core.save_workspaces(merged)
    report["workspaces"] = (a, r, s)

    idents = []
    for i in bundle.get("identities", []):
        if isinstance(i, dict):
            i = dict(i)
            i["secret_enc"] = _lock(i.pop("secret", "") or "")
            c = core.clean_identity(i)
            if c:
                idents.append(c)
    merged, a, r, s = merge_list(core.load_identities(), idents, replace, lambda x: x["id"])
    if a or r:
        core.save_identities(merged)
    report["identities"] = (a, r, s)

    proxies = []
    for p in bundle.get("proxies", []):
        if isinstance(p, dict):
            p = dict(p)
            p["password_enc"] = _lock(p.pop("password", "") or "")
            c = core.clean_proxy(p)
            if c:
                proxies.append(c)
    merged, a, r, s = merge_list(core.load_proxies(), proxies, replace, lambda x: x["id"])
    if a or r:
        core.save_proxies(merged)
    report["proxies"] = (a, r, s)

    import rc_notes
    inc_notes = []
    for n in bundle.get("notes", []):
        n = rc_notes.clean_note(n)
        if n:
            n["server"] = id_map.get(n["server"], n["server"])
            inc_notes.append(n)
    merged, a, r, s = merge_list(rc_notes.load_notes(), inc_notes, replace, lambda x: x["id"])
    if a or r:
        rc_notes.save_notes(merged)
    report["notes"] = (a, r, s)

    have = set(read_known_hosts().splitlines())
    new_lines = [l for l in (bundle.get("known_hosts") or "").splitlines() if l.strip() and l not in have]
    if new_lines:
        with core.KNOWN_HOSTS_LOCK:
            with open(core.KNOWN_HOSTS, "a", encoding="utf-8") as f:
                existing = read_known_hosts()
                f.write(("" if not existing or existing.endswith("\n") else "\n") + "\n".join(new_lines) + "\n")
    report["host_keys"] = len(new_lines)
    return current, report


def describe(report):
    def line(label, t):
        a, r, s = t
        bits = [f"{a} added"] + ([f"{r} replaced"] if r else []) + ([f"{s} already there, left alone"] if s else [])
        return f"{label}: " + ", ".join(bits)
    out = [line("Servers", report["servers"]), line("Snippets", report["snippets"]), line("Workspaces", report["workspaces"]),
           line("Identities", report["identities"]), line("Proxies", report["proxies"]), line("Notes", report.get("notes", (0, 0, 0))),
           f"Trusted host keys added: {report['host_keys']}"]
    return "\n".join(out)


# ─── the two menu commands ───────────────────────────────────────────────────

def backup_dialog(app):
    from tkinter import filedialog, messagebox, simpledialog
    pw = simpledialog.askstring("Back up everything", "Choose a password for the backup file.\n"
                                "You will need it to restore. It cannot be recovered.", show="•", parent=app.root)
    if not pw:
        return None
    if len(pw) < 8 and not messagebox.askyesno("Back up everything", "That password is short (under 8 characters). Use it anyway?",
                                               parent=app.root):
        return None
    if simpledialog.askstring("Back up everything", "Type the password again.", show="•", parent=app.root) != pw:
        messagebox.showerror("Back up everything", "The two passwords were not the same. Nothing was saved.", parent=app.root)
        return None
    path = filedialog.asksaveasfilename(parent=app.root, title="Save the backup", defaultextension=".rcbackup",
                                        initialfile="RC-backup-" + time.strftime("%Y%m%d") + ".rcbackup",
                                        filetypes=[("RC backup", "*.rcbackup"), ("All files", "*.*")])
    if not path:
        return None
    try:
        bundle, lost = make_bundle(app.hosts)
        blob = encrypt(bundle, pw)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
    except (OSError, BackupError) as e:
        messagebox.showerror("Back up everything", f"Could not make the backup:\n{e}", parent=app.root)
        return None
    keys = sum(1 for h in app.hosts if h.get("key_file"))
    note = (f"\n\n{lost} saved password(s) could not be opened (they belong to another Windows account) and were left out." if lost else "")
    note += (f"\n\n{keys} server(s) use an SSH key file. The key files themselves are not in the backup: copy them yourself." if keys else "")
    messagebox.showinfo("Back up everything", f"Saved {len(app.hosts)} server(s) and your other settings to:\n{path}\n\n"
                        "Keep it and its password safe: anyone with both can read your saved passwords." + note, parent=app.root)
    app.status.set(f"Backup saved: {path}")
    log.info("Backup written (%d servers)", len(app.hosts))
    return path


def restore_dialog(app, path=None):
    from tkinter import filedialog, messagebox, simpledialog
    path = path or filedialog.askopenfilename(parent=app.root, title="Choose the backup file",
                                              filetypes=[("RC backup", "*.rcbackup"), ("All files", "*.*")])
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as e:
        messagebox.showerror("Restore", f"Could not read that file:\n{e}", parent=app.root)
        return None
    pw = simpledialog.askstring("Restore", "Password for this backup:", show="•", parent=app.root)
    if not pw:
        return None
    try:
        bundle = decrypt(blob, pw)
    except BackupError as e:
        messagebox.showerror("Restore", str(e), parent=app.root)
        return None
    n = len(bundle["hosts"])
    ans = messagebox.askyesnocancel(
        "Restore", f"This backup (made {bundle.get('created', '?')}) holds {n} server(s).\n\n"
                   "Yes = add what is missing AND overwrite servers that match (same id, or same name and address).\n"
                   "No = only add what is missing and leave everything already here alone.\n"
                   "Cancel = do nothing.", icon="question", parent=app.root)
    if ans is None:
        return None
    try:
        keep = os.path.join(core.APP_DIR, "hosts.json.before-restore-" + time.strftime("%Y%m%d-%H%M%S"))
        if os.path.exists(core.HOSTS_FILE):
            import shutil
            shutil.copy2(core.HOSTS_FILE, keep)      # a safety copy of the list as it was
        new_hosts, report = apply_bundle(bundle, app.hosts, replace=bool(ans))
        app.hosts[:] = new_hosts
        app.commit()
    except (OSError, ValueError) as e:
        messagebox.showerror("Restore", f"The restore failed part-way:\n{e}", parent=app.root)
        return None
    messagebox.showinfo("Restore", "Restored.\n\n" + describe(report) + "\n\nThe server list as it was before is saved as\n" + os.path.basename(keep), parent=app.root)
    app.status.set("Backup restored")
    log.info("Backup restored: %s", report)
    return report
