"""REGTeches RC - import servers from MobaXterm and Xshell (File > Import servers).

* MobaXterm: File "MobaXterm sessions.mxtsessions" (Sessions > Export in MobaXterm) or the [Bookmarks...] part of
  MobaXterm.ini. Handles SSH, SFTP, Telnet, RDP, VNC, FTP and Serial sessions; folders become groups.
* Xshell: a Sessions folder of .xsh files (usually Documents\\NetSarang Computer\\<version>\\Xshell\\Sessions). Handles
  SSH, SFTP, Telnet and FTP; sub-folders become groups.

Passwords are never imported: MobaXterm keeps them in its own encrypted store and Xshell encrypts them per machine.
The result goes to the same selection list as the PuTTY / WinSCP import (rc_tools.ImportTab). The parsers are pure."""

import os
import re

KIND_PORT = {"ssh": 22, "telnet": 23, "rdp": 3389, "vnc": 5900, "ftp": 21, "serial": 0}
# MobaXterm's session type numbers (the first number after the icon) -> what RC calls it
MOBA_TYPES = {"0": "ssh", "1": "telnet", "4": "rdp", "5": "vnc", "6": "ftp", "7": "ssh", "8": "serial", "13": "ssh"}
MOBA_LABEL = {"ssh": "SSH", "telnet": "Telnet", "rdp": "RDP", "vnc": "VNC", "ftp": "FTP", "serial": "Serial"}


def _clean_user(u):
    u = (u or "").strip()
    return "" if u in ("<default>", "<none>", "None") else u


def _port(text, kind):
    return int(text) if text and text.isdigit() and 0 < int(text) < 65536 else KIND_PORT[kind]


def read_text(path):
    """Read a text file whatever its encoding (Xshell writes UTF-16)."""
    with open(path, "rb") as f:
        raw = f.read()
    for bom, enc in ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"), (b"\xef\xbb\xbf", "utf-8-sig")):
        if raw.startswith(bom):
            return raw.decode(enc, "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def parse_mobaxterm(text):
    """MobaXterm export text -> list of items for the import list: name, address, user, port, group, kind, source, ..."""
    out, group = [], ""
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("["):
            group = ""
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "SubRep":
            group = value.strip().replace("\\", " / ").strip(" /")
            continue
        if key == "ImgNum" or not value.startswith("#"):
            continue
        m = re.match(r"#\d+#(\d+)%([^%]*)%([^%]*)%([^%]*)", value)
        if not m:
            continue
        kind = MOBA_TYPES.get(m.group(1))
        host, port, user = m.group(2).strip(), m.group(3).strip(), _clean_user(m.group(4))
        if kind is None or not host or not key.strip():
            continue
        item = {"name": key.strip(), "address": host, "user": user, "port": _port(port, kind), "group": group, "kind": kind,
                "source": "MobaXterm · " + MOBA_LABEL[kind], "note": ""}
        if kind == "serial":
            item.update(address="localhost", user="", serial=f"{host}, {port}" if port.isdigit() else host)
        if m.group(1) == "7":
            item["mode"] = "SFTP"
        out.append(item)
    return out


def parse_xshell_session(text, name, group=""):
    """One .xsh file's text -> an item, or None when it is a kind RC cannot open."""
    section, vals = "", {}
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].upper()
        elif "=" in line:
            k, _, v = line.partition("=")
            vals[(section, k.strip().lower())] = v.strip()
    host = vals.get(("CONNECTION", "host"), "")
    proto = vals.get(("CONNECTION", "protocol"), "SSH").upper()
    kind = {"SSH": "ssh", "SFTP": "ssh", "TELNET": "telnet", "FTP": "ftp"}.get(proto)
    if not host or kind is None:
        return None
    return {"name": name, "address": host, "user": _clean_user(vals.get(("CONNECTION:AUTHENTICATION", "username"))),
            "port": _port(vals.get(("CONNECTION", "port"), ""), kind), "group": group, "kind": kind,
            "source": "Xshell · " + proto.capitalize() if proto != "SSH" else "Xshell", "note": "",
            **({"mode": "SFTP"} if proto == "SFTP" else {})}


def scan_xshell_folder(root):
    """Every .xsh under `root` (sub-folders become the group) -> list of items."""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        group = "" if rel == "." else rel.replace(os.sep, " / ")
        for fn in sorted(files):
            if not fn.lower().endswith(".xsh"):
                continue
            try:
                item = parse_xshell_session(read_text(os.path.join(dirpath, fn)), fn[:-4], group)
            except OSError:
                continue
            if item:
                out.append(item)
    return out


def default_xshell_folder():
    docs = os.path.join(os.path.expanduser("~"), "Documents", "NetSarang Computer")
    best = ""
    if os.path.isdir(docs):
        for ver in sorted(os.listdir(docs), reverse=True):
            cand = os.path.join(docs, ver, "Xshell", "Sessions")
            if os.path.isdir(cand):
                return cand
    return best


# ─── Devolutions Remote Desktop Manager ──────────────────────────────────────

_RDM_HOST = ("host", "hostname", "computername", "server", "address", "url", "ipaddress")
_RDM_USER = ("username", "user", "login")
_RDM_PORT = ("port", "hostport", "sshport", "rdpport")


def _rdm_find(d, names, depth=0):
    """First non-empty value whose key (any case) is in `names`, looking one level into nested objects (RDM keeps settings in
    sub-objects such as "RDP" or "SSH" depending on the version)."""
    for k, v in d.items():
        if isinstance(k, str) and k.lower() in names and isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    if depth < 2:
        for v in d.values():
            if isinstance(v, dict):
                found = _rdm_find(v, names, depth + 1)
                if found:
                    return found
    return ""


def parse_rdm(text):
    """Devolutions RDM JSON export (.rdm / .json: {"Connections": [...]} or a list) -> import items. Entries RC cannot open (folders,
    web pages, credentials, documents) are skipped. Passwords are never read."""
    import json
    try:
        data = json.loads((text or "").lstrip("﻿"))
    except ValueError:
        raise ValueError("That file is not a Devolutions export (it is not JSON).")
    entries = data.get("Connections") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError("No connections were found in that file.")
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ctype = str(e.get("ConnectionType") or e.get("Type") or "").lower()
        kind = ("ssh" if "ssh" in ctype or "sftp" in ctype else "telnet" if "telnet" in ctype else "rdp" if "rdp" in ctype or "remotedesktop" in ctype
                else "vnc" if "vnc" in ctype else "ftp" if "ftp" in ctype else None)
        host = _rdm_find(e, _RDM_HOST)
        host = re.sub(r"^[a-z]+://", "", host).split("/")[0]
        if kind is None or not host or not str(e.get("Name") or "").strip():
            continue
        item = {"name": str(e["Name"]).strip(), "address": host.split(":")[0] if host.count(":") == 1 else host,
                "user": _clean_user(_rdm_find(e, _RDM_USER)),
                "port": _port(_rdm_find(e, _RDM_PORT) or (host.split(":")[1] if host.count(":") == 1 else ""), kind),
                "group": str(e.get("Group") or "").replace("\\", " / ").strip(" /"), "kind": kind,
                "source": "Devolutions RDM · " + kind.upper(), "note": str(e.get("Description") or "")[:200]}
        if "sftp" in ctype:
            item["mode"] = "SFTP"
        out.append(item)
    return out
