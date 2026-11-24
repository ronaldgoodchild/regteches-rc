"""REGTeches RC - your own terminal colour schemes (View > Terminal colours > Import a colour scheme...).

Reads the formats colour schemes are shared in: iTerm2 (`.itermcolors`), Windows Terminal (a scheme or a whole settings.json /
`schemes` list) and a simple RC/JSON form ({"name", "background", "foreground", "cursor", "selection", "colors": [16 colours]}).
Imported schemes are kept in `schemes.json` and appear next to the built-in ones in every colour menu. Only plain `#rrggbb`
colours are accepted; a scheme with missing colours is topped up from the built-in Dark scheme, so a partial file still works.
Parsing and storing are pure and tested without a window; the dialog functions are at the bottom."""

import json
import os
import plistlib
import re

import rc_manager as core

log = core.log

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
ANSI_KEYS = ("black", "red", "green", "yellow", "blue", "purple", "cyan", "white")        # Windows Terminal names
MAX_CUSTOM = 40


class SchemeError(ValueError):
    """A readable problem with a scheme file."""


def schemes_path():
    return os.path.join(core.APP_DIR, "schemes.json")


# ─── cleaning ────────────────────────────────────────────────────────────────

def clean_color(value):
    if isinstance(value, str):
        v = value.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{3}", v):
            v = "#" + "".join(c * 2 for c in v[1:])
        if HEX.match(v):
            return v.lower()
    return None


def clean_name(name):
    name = " ".join(str(name or "").split())[:40]
    return name


def make_scheme(bg, fg, cursor, sel, bold, ansi, base):
    """Fill every missing colour from `base` (a complete scheme) so a partial file still gives a usable scheme."""
    ansi = list(ansi or [])
    out_ansi = [clean_color(ansi[i]) if i < len(ansi) else None for i in range(16)]
    out_ansi = [c or base["ansi"][i] for i, c in enumerate(out_ansi)]
    bg, fg = clean_color(bg) or base["bg"], clean_color(fg) or base["fg"]
    return {"bg": bg, "fg": fg, "cursor": clean_color(cursor) or fg, "sel": clean_color(sel) or base["sel"],
            "bold": clean_color(bold) or fg, "ansi": out_ansi}


def valid_scheme(d):
    return (isinstance(d, dict) and all(clean_color(d.get(k)) for k in ("bg", "fg", "cursor", "sel", "bold"))
            and isinstance(d.get("ansi"), list) and len(d["ansi"]) == 16 and all(clean_color(c) for c in d["ansi"]))


# ─── readers ─────────────────────────────────────────────────────────────────

def _rgb(d):
    try:
        r, g, b = (max(0.0, min(1.0, float(d[k]))) for k in ("Red Component", "Green Component", "Blue Component"))
    except (KeyError, TypeError, ValueError):
        return None
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def parse_iterm(data, base):
    """iTerm2 .itermcolors (an XML or binary property list) -> scheme."""
    try:
        plist = plistlib.loads(data)
    except Exception:
        raise SchemeError("That file is not a readable iTerm2 colour scheme.")
    if not isinstance(plist, dict):
        raise SchemeError("That file is not a readable iTerm2 colour scheme.")
    get = lambda key: _rgb(plist.get(key) or {}) if isinstance(plist.get(key), dict) else None   # noqa: E731
    ansi = [get(f"Ansi {i} Color") for i in range(16)]
    if not get("Background Color") and not any(ansi):
        raise SchemeError("That file has no colours in iTerm2's format.")
    return make_scheme(get("Background Color"), get("Foreground Color"), get("Cursor Color"), get("Selection Color"),
                       get("Bold Color"), ansi, base)


def parse_windows_terminal_one(d, base):
    ansi = [d.get(k) for k in ANSI_KEYS] + [d.get("bright" + k[0].upper() + k[1:]) for k in ANSI_KEYS]
    return make_scheme(d.get("background"), d.get("foreground"), d.get("cursorColor"), d.get("selectionBackground"), None, ansi, base)


def parse_json(text, base):
    """-> [(name, scheme)] from a Windows Terminal scheme / settings file or RC's own simple form."""
    try:
        data = json.loads(re.sub(r"^\s*//.*$", "", text.lstrip("﻿"), flags=re.M))      # settings.json may hold // comments
    except ValueError:
        raise SchemeError("That file is not valid JSON.")
    items = data.get("schemes") if isinstance(data, dict) and isinstance(data.get("schemes"), list) else \
        data if isinstance(data, list) else [data]
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        name = clean_name(d.get("name"))
        if isinstance(d.get("colors"), list):                 # RC's simple form
            sc = make_scheme(d.get("background"), d.get("foreground"), d.get("cursor"), d.get("selection"), None, d["colors"], base)
        elif d.get("background") or d.get("foreground") or d.get("black"):
            sc = parse_windows_terminal_one(d, base)
        else:
            continue
        out.append((name, sc))
    if not out:
        raise SchemeError("No colour scheme was found in that file.")
    return out


def parse_file(path, base):
    """-> [(name, scheme)]. The name of an .itermcolors file is its file name."""
    try:
        with open(path, "rb") as f:
            data = f.read(2_000_000)
    except OSError as e:
        raise SchemeError(f"Could not read the file: {e}")
    stem = clean_name(os.path.splitext(os.path.basename(path))[0])
    if path.lower().endswith(".itermcolors") or data.lstrip().startswith((b"<?xml", b"bplist")):
        return [(stem or "Imported", parse_iterm(data, base))]
    out = parse_json(data.decode("utf-8", "replace"), base)
    return [(n or stem or "Imported", sc) for n, sc in out]


# ─── storage ─────────────────────────────────────────────────────────────────

def load_custom():
    """{name: scheme} from schemes.json; entries that are not complete valid schemes are ignored."""
    try:
        with open(schemes_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("schemes.json unreadable (%s)", e)
        return {}
    out = {}
    for name, sc in (data.items() if isinstance(data, dict) else []):
        if isinstance(name, str) and clean_name(name) and valid_scheme(sc):
            out[clean_name(name)] = {"bg": clean_color(sc["bg"]), "fg": clean_color(sc["fg"]), "cursor": clean_color(sc["cursor"]),
                                     "sel": clean_color(sc["sel"]), "bold": clean_color(sc["bold"]), "ansi": [clean_color(c) for c in sc["ansi"]]}
        if len(out) >= MAX_CUSTOM:
            break
    return out


def save_custom(custom):
    tmp = schemes_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(custom, f, indent=1)
    os.replace(tmp, schemes_path())


def add_custom(name, scheme, builtin_names):
    """Store an imported scheme; a name that clashes with a built-in one gets ' (custom)' added. Returns the name used."""
    custom = load_custom()
    name = clean_name(name) or "Imported"
    if name in builtin_names:
        name += " (custom)"
    if name not in custom and len(custom) >= MAX_CUSTOM:
        raise SchemeError(f"You can keep up to {MAX_CUSTOM} imported schemes. Remove one first.")
    custom[name] = scheme
    save_custom(custom)
    return name


def remove_custom(name):
    custom = load_custom()
    if name not in custom:
        return False
    del custom[name]
    save_custom(custom)
    return True


def register(target, builtin_names):
    """Make `target` (the SCHEMES dict) hold the built-in schemes plus the stored custom ones. Returns the custom names."""
    for k in [k for k in target if k not in builtin_names]:
        del target[k]
    custom = load_custom()
    for name, sc in custom.items():
        if name not in builtin_names:
            target[name] = sc
    return list(custom)


# ─── dialogs (used by the menus) ─────────────────────────────────────────────

def import_dialog(parent, schemes, builtin_names, base):
    """Ask for a file, import its scheme(s). Returns the list of names added (empty if cancelled or failed)."""
    from tkinter import filedialog, messagebox
    path = filedialog.askopenfilename(parent=parent, title="Choose a colour scheme file",
                                      filetypes=[("Colour schemes", "*.itermcolors *.json"), ("All files", "*.*")])
    if not path:
        return []
    try:
        found = parse_file(path, base)
        names = [add_custom(n, sc, builtin_names) for n, sc in found[:10]]
    except (SchemeError, OSError) as e:
        messagebox.showerror("Colour scheme", str(e), parent=parent)
        return []
    register(schemes, builtin_names)
    messagebox.showinfo("Colour scheme", "Added: " + ", ".join(names), parent=parent)
    return names


def remove_dialog(parent, schemes, builtin_names, name):
    from tkinter import messagebox
    if not messagebox.askyesno("Colour scheme", f"Remove the imported scheme “{name}”?", parent=parent):
        return False
    try:
        ok = remove_custom(name)
    except OSError as e:
        messagebox.showerror("Colour scheme", f"Could not remove it:\n{e}", parent=parent)
        return False
    register(schemes, builtin_names)
    return ok
