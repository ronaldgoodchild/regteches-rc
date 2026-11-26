"""Builds the marketing pictures + animated tour for RC from FAKE demo data (never your real servers).

    python capture_marketing.py <demo_data_folder> <output_folder>

It runs the real program with RC_DATA_DIR pointing at the demo folder (make_demo_data.py makes it), fakes the
terminal and file-server so no real connection is needed, then screenshots the window.
Windows only (uses the screen to grab real menus). Leave the mouse alone while it runs (~1 minute)."""
import ctypes
import os
import posixpath
import shutil
import sys
import time

DEMO = os.path.abspath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2])
os.makedirs(OUT, exist_ok=True)
os.environ["RC_DATA_DIR"] = DEMO
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageDraw, ImageFont, ImageGrab

import rc_manager as core
import rc_files
import rc_theme
import rc_tools

# ─── fake PC folder (neutral path, no real user name) ───────────────────────
LOCAL = r"C:\RC_Demo\Projects"
shutil.rmtree(r"C:\RC_Demo", ignore_errors=True)
os.makedirs(LOCAL)
now = time.time()
for name, kb, days in (("website", 0, 2), ("backups", 0, 5), ("scripts", 0, 9), ("invoice-nov.pdf", 412, 1),
                       ("network-map.png", 1830, 3), ("deploy.sh", 3, 1), ("notes.txt", 7, 4), ("logo.svg", 24, 12),
                       ("report-q3.xlsx", 96, 20), ("server-list.csv", 14, 6)):
    p = os.path.join(LOCAL, name)
    if kb == 0:
        os.makedirs(p, exist_ok=True)
    else:
        with open(p, "wb") as f:
            f.truncate(kb * 1024)
    os.utime(p, (now - days * 86400, now - days * 86400))
rc_files.LocalBackend.home = lambda self: LOCAL
import socket
socket.gethostname = lambda: "OFFICE-PC"      # no real PC or user name in any picture
os.environ["USERPROFILE"] = r"C:\Users\you"


class FakeRemote:
    kind = "sftp"
    TREE = {
        "/var/www": [("html", True, 0, 1), ("api", True, 0, 3), ("logs", True, 0, 1), ("releases", True, 0, 6),
                     ("index.html", False, 18200, 2), ("robots.txt", False, 120, 30), ("deploy.sh", False, 2900, 1),
                     (".env.example", False, 640, 40), ("docker-compose.yml", False, 4100, 8),
                     ("backup-2026-11-03.tar.gz", False, 482_000_000, 1), ("README.md", False, 5300, 12)],
    }

    def __init__(self):
        self.t = {k: [(n, d, s, now - days * 86400) for n, d, s, days in v] for k, v in self.TREE.items()}

    join = staticmethod(posixpath.join)

    @staticmethod
    def parent(path):
        return posixpath.dirname(path.rstrip("/")) or "/"

    def home(self):
        return "/var/www"

    def listdir(self, path):
        return self.t.get(path, [("sub-folder", True, 0, now), ("readme.txt", False, 800, now - 86400)])

    def stat(self, path):
        return True, 0, now, 0o755

    def mkdir(self, path): pass
    def ensure_dir(self, path): pass
    def remove(self, path, is_dir): pass
    def rename(self, old, new): pass
    def chmod(self, path, mode): pass
    def set_mtime(self, path, mtime): pass
    def close(self): pass


rc_files.FilesTab.make_backend = lambda self: FakeRemote()

ESC = "\x1b["
G, C, Y, B, R, M, D, X = (ESC + "1;32m", ESC + "1;36m", ESC + "1;33m", ESC + "1;34m", ESC + "1;31m", ESC + "1;35m",
                          ESC + "90m", ESC + "0m")
SESSION = (
    "Last login: Tue Nov  3 09:12:44 2026 from 10.50.4.21\r\n"
    f"{G}deploy@web-prod-01{X}:{B}~{X}$ uptime\r\n"
    " 09:41:07 up 41 days,  6:02,  2 users,  load average: 0.21, 0.18, 0.15\r\n"
    f"{G}deploy@web-prod-01{X}:{B}~{X}$ df -h /\r\n"
    "Filesystem      Size  Used Avail Use% Mounted on\r\n"
    f"/dev/sda1        80G   {Y}31G{X}   45G  41% /\r\n"
    f"{G}deploy@web-prod-01{X}:{B}~{X}$ docker ps --format 'table {{{{.Names}}}}\\t{{{{.Status}}}}'\r\n"
    f"{C}NAMES          STATUS{X}\r\n"
    f"nginx          {G}Up 41 days{X}\r\n"
    f"api            {G}Up 12 days (healthy){X}\r\n"
    f"redis          {G}Up 41 days{X}\r\n"
    f"worker         {Y}Up 2 hours (restarting){X}\r\n"
    f"{G}deploy@web-prod-01{X}:{B}~{X}$ sudo systemctl status nginx --no-pager | head -4\r\n"
    f"{G}\u25cf{X} nginx.service - A high performance web server\r\n"
    f"     Active: {G}active (running){X} since Sat 2026-09-23 03:01:12 EDT; 41 days ago\r\n"
    f"{G}deploy@web-prod-01{X}:{B}~{X}$ "
)


def fake_connect(self, gen, user, password, cols, rows):
    # wait for the tab to be laid out so the text uses the real window width, like a real connection would
    self.app.post(lambda: self.after(600, lambda: (self.write_local("Connected.\r\n"), self.feed(SESSION),
                                                       self.set_state("● Connected", False))))


import rc_terminal
rc_terminal.TerminalTab.connect = fake_connect

# Believable live status for the FAKE servers (nothing is really probed)
_DOWN = {"10.50.0.2"}
_MS = {"10.50.0.1": 1, "10.50.2.20": 2, "10.50.2.53": 3, "10.50.3.10": 2, "10.50.3.11": 4}


def fake_status(h, timeout=1.5):
    a = h["address"]
    if a == "10.50.4.22":   # reachable only through its alternate (Tailscale) address -> amber dot
        return {"state": "up", "addr": "100.64.0.22", "ms": 31, "port": 22, "service": True, "via_alt": True}
    if a in _DOWN:
        return {"state": "down", "addr": a, "ms": 0, "port": 0, "service": False, "via_alt": False}
    return {"state": "up", "addr": a, "ms": _MS.get(a, 6), "port": 22, "service": True, "via_alt": False}


core.check_host_status = fake_status

# ─── start the real app ─────────────────────────────────────────────────────
root = tk.Tk()
try:
    ttk.Style().theme_use("vista")
except tk.TclError:
    pass
app = core.App(root)
root.geometry("1640x900+10+5")
for h in app.hosts:
    if h["address"].startswith("10.50.") and h["default_action"] != "rdp":
        h["key_file"] = "demo"          # in memory only: lets the app skip the password prompt
root.lift()
root.attributes("-topmost", True)
root.update()


def pump(sec=0.5):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def win_box():
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    rect = ctypes.wintypes.RECT() if hasattr(ctypes, "wintypes") else None
    import ctypes.wintypes as wt
    rect = wt.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def grab(extra=None):
    pump(0.35)
    l, t, r, b = win_box()
    if extra:
        l, t, r, b = min(l, extra[0]), min(t, extra[1]), max(r, extra[2]), max(b, extra[3])
    return ImageGrab.grab(bbox=(l, t + 5, r, b), all_screens=True).convert("RGB")   # +5: skip the 1-4 px edge that shows what is behind the window


frames = []      # (image, seconds) for the tour GIF


def shot(name, tour=0.0, extra=None):
    img = grab(extra)
    img.save(os.path.join(OUT, name + ".png"))
    if tour:
        frames.append((img, tour))
    print("saved", name)
    return img


def host(name):
    return next(h for h in app.hosts if h["name"] == name)


def pick(name):
    app.select_host(host(name)["id"])
    pump(0.3)


def close_extra_tabs():
    for w in list(app.nb.tabs()):
        wd = root.nametowidget(w)
        if wd is not app.home:
            app.close_widget(wd)
    pump(0.2)


def menu_shot(index, name, tour=0.0):
    """Open a menu-bar menu with real key presses (Windows menus close themselves otherwise) and grab it."""
    import threading
    user32 = ctypes.windll.user32
    VK = {"alt": 0x12, "right": 0x27, "down": 0x28, "esc": 0x1B}

    def key(vk):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)

    path = os.path.join(OUT, name + ".png")

    l, t, r, b = win_box()      # Tk may only be used from this thread, so measure before the helper starts

    def grab_then_close():
        time.sleep(1.6)
        ImageGrab.grab(bbox=(l, t + 5, r, b), all_screens=True).convert("RGB").save(path)
        key(VK["esc"]); time.sleep(0.15); key(VK["esc"])

    root.focus_force()
    try:   # make sure Windows really treats this window as the active one, or the Alt key goes elsewhere
        user32.SetForegroundWindow(user32.GetParent(root.winfo_id()))
    except Exception:
        pass
    pump(1.0)
    key(VK["alt"])
    time.sleep(0.25)
    for _ in range(index):
        key(VK["right"])
    key(VK["down"])
    threading.Thread(target=grab_then_close, daemon=True).start()
    end = time.time() + 4
    while time.time() < end and not os.path.exists(path):
        root.update()          # the native menu loop runs inside this call until the helper thread presses Esc
        time.sleep(0.05)
    pump(0.5)
    if os.path.exists(path) and tour:
        frames.append((Image.open(path).convert("RGB"), tour))
    print("saved", name)


# ─── the tour ───────────────────────────────────────────────────────────────
app.theme.set_theme("Sky Blue")
pump(3.5)          # let the background status round finish so the dots are coloured
shot("01_home", 2.2)

pick("web-prod-01")
shot("02_server_details", 1.8)
pick("docker-host")
shot("03_docker_host_connections", 1.8)

app.act_ssh(host("web-prod-01"))
pump(1.5)
shot("04_ssh_terminal", 2.4)
close_extra_tabs()

app.act_files(host("web-prod-01"))
pump(1.8)
shot("05_file_manager", 2.4)
close_extra_tabs()

app.open_tunnels()
pump(0.6)
shot("06_tunnels", 1.8)
close_extra_tabs()
app.open_snippets()
pump(0.6)
shot("07_snippets", 1.8)
close_extra_tabs()
app.open_keys()
pump(0.6)
root.nametowidget(app.nb.select()).key_path.set("C:/Users/you/.ssh/rc_homelab_OFFICE-PC")
shot("08_key_setup", 2.0)
close_extra_tabs()

try:
    app.quick_connect()
    pump(0.5)
    app.palette.entry.insert(0, "docker") if hasattr(app.palette, "entry") else None
    pump(0.5)
    shot("09_quick_connect", 1.8)
    app.palette.close()
    pump(0.3)
except Exception as e:
    print("quick connect skipped:", e)

menu_shot(5, "zz_warmup")      # the first menu open after a popup closes is unreliable (focus): throw this one away
try:
    os.remove(os.path.join(OUT, "zz_warmup.png"))
except OSError:
    pass
menu_shot(2, "10_servers_menu", 1.8)
menu_shot(5, "11_tools_menu", 1.8)

# colour themes: whole window in each theme
pick("docker-host")
gallery = []
for name in rc_theme.PALETTES:
    app.theme.set_theme(name)
    app.sync_theme_button()
    pump(0.5)
    img = shot("theme_" + name.lower().replace(" ", "_"), 0 if name == "Classic Light" else 1.0)
    if name != "Classic Light":
        gallery.append((name, img))
app.theme.set_theme("Sky Blue")
app.sync_theme_button()
pump(0.4)

app.about()
pump(0.6)
l, t, r, b = win_box()
shot("12_about", 2.0)
for w in root.winfo_children():
    if isinstance(w, tk.Toplevel):
        w.destroy()
pump(0.3)
root.destroy()

# ─── animated tour ──────────────────────────────────────────────────────────
W = 1100
seq = []
for img, sec in frames:
    h = int(img.height * W / img.width)
    seq.append((img.resize((W, h), Image.LANCZOS), sec))
H = max(i.height for i, _ in seq)
pal = []
for img, sec in seq:
    canvas = Image.new("RGB", (W, H), (16, 21, 40))
    canvas.paste(img, (0, 0))
    pal.append((canvas.convert("P", palette=Image.ADAPTIVE, colors=200), int(sec * 1000)))
pal[0][0].save(os.path.join(OUT, "RC_tour.gif"), save_all=True, append_images=[p for p, _ in pal[1:]],
               duration=[d for _, d in pal], loop=0, optimize=True)
print("saved RC_tour.gif", len(pal), "frames")

# ─── theme gallery + hero image ─────────────────────────────────────────────
tw = 640
thumbs = []
for name, img in gallery:
    th = img.resize((tw, int(img.height * tw / img.width)), Image.LANCZOS)
    thumbs.append((name, th))
cols = 2
rows = (len(thumbs) + cols - 1) // cols
cell_h = thumbs[0][1].height + 44
sheet = Image.new("RGB", (cols * (tw + 20) + 20, rows * cell_h + 100), (16, 21, 40))
d = ImageDraw.Draw(sheet)


def font(size, bold=True):
    for f in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


d.text((20, 22), "Colour themes - one click away", fill="white", font=font(42))
for i, (name, th) in enumerate(thumbs):
    x, y = 20 + (i % cols) * (tw + 20), 100 + (i // cols) * cell_h
    sheet.paste(th, (x, y + 34))
    d.text((x, y), name, fill=(255, 205, 210), font=font(24))
sheet.save(os.path.join(OUT, "13_theme_gallery.png"))

import subprocess
subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'make_hero.py'), OUT])
print("saved hero + gallery. done")
shutil.rmtree(r"C:\RC_Demo", ignore_errors=True)
