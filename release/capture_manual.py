"""Makes the pictures and the demo video for the RC web manual (docs/) from FAKE data (never your real servers).

    python capture_manual.py <output_folder>          # pictures + video frames
    python capture_manual.py <output_folder> --stills # pictures only

It writes a folder of fake demo data (make_demo_data.py plus notes, identities, proxies, workspaces, command history),
starts the REAL program on it with demo_env.py (fake terminal, file server and live status), walks through every
feature and saves a picture of each, then plays the same walk as a video (frames in <output>/frames, then ffmpeg).
Windows only: it grabs the real screen, so leave the mouse and keyboard alone while it runs (about 3 minutes)."""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(sys.argv[1])
STILLS_ONLY = "--stills" in sys.argv
os.makedirs(OUT, exist_ok=True)

# ─── 1. fake data folder ────────────────────────────────────────────────────
DEMO = tempfile.mkdtemp(prefix="rc_manual_demo_")
subprocess.run([sys.executable, os.path.join(HERE, "make_demo_data.py"), DEMO, "Sky Blue"], check=True, capture_output=True)
hosts = json.load(open(os.path.join(DEMO, "hosts.json"), encoding="utf-8"))["hosts"]
ID = {h["name"]: h["id"] for h in hosts}
now = time.time()


def dump(name, data):
    with open(os.path.join(DEMO, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


dump("notes.json", [
    {"id": "n1", "title": "Friday deploy checklist", "tags": ["deploy", "web"], "server": ID["web-prod-01"], "updated": now - 3600,
     "body": "# Friday deploy\n\n1. Tell the team in chat\n2. `git pull` on **web-prod-01**\n3. Run the smoke test\n4. Watch the logs for 10 minutes\n\n"
             "Rollback: `./deploy.sh --rollback`"},
    {"id": "n2", "title": "Database backup restore test", "tags": ["database", "backup"], "server": ID["db-prod-01"], "updated": now - 86400,
     "body": "Nightly dump runs at **02:00** to `/backups`.\n\nRestore test every first Monday:\n\n- copy the newest dump\n- restore into a scratch database\n- compare row counts"},
    {"id": "n3", "title": "Home lab port map", "tags": ["network"], "server": "", "updated": now - 5 * 86400,
     "body": "| Service | Port |\n|---|---|\n| Portainer | 9000 |\n| Plex | 32400 |\n| Home Assistant | 8123 |"},
])
dump("identities.json", [
    {"id": "i1", "name": "Admin key (all Linux servers)", "user": "deploy", "auth": "key", "secret_enc": "", "key_file": "C:/Users/you/.ssh/rc_homelab_OFFICE-PC"},
    {"id": "i2", "name": "NAS administrator", "user": "admin", "auth": "password", "secret_enc": "", "key_file": ""},
    {"id": "i3", "name": "SSH agent (Pageant)", "user": "ops", "auth": "agent", "secret_enc": "", "key_file": ""},
])
dump("proxies.json", [
    {"id": "p1", "name": "Office SOCKS5 gateway", "kind": "socks5", "host": "10.50.0.1", "port": 1080, "user": "", "password_enc": ""},
    {"id": "p2", "name": "Corporate HTTP proxy", "kind": "http", "host": "proxy.example.com", "port": 8080, "user": "ron", "password_enc": ""},
])
dump("workspaces.json", [
    {"name": "Production watch", "layout": "grid", "hosts": [ID["web-prod-01"], ID["db-prod-01"], ID["mail-gateway"], ID["docker-host"]]},
    {"name": "Home lab", "layout": "cols", "hosts": [ID["proxmox-lab"], ID["docker-host"], ID["pi-hole"]]},
])
dump("history.json", {"record": True, "confirm_paste": True, "items": [
    {"cmd": "docker ps -a", "count": 14, "last": now - 600}, {"cmd": "sudo journalctl -u nginx -n 50", "count": 6, "last": now - 3000},
    {"cmd": "df -h", "count": 9, "last": now - 7200}, {"cmd": "tail -f /var/log/syslog", "count": 4, "last": now - 86400},
    {"cmd": "git pull && npm run build", "count": 3, "last": now - 90000}]})
os.makedirs(os.path.join(DEMO, "recordings"), exist_ok=True)
with open(os.path.join(DEMO, "recordings", "demo-session.cast"), "w", encoding="utf-8") as f:
    f.write(json.dumps({"version": 2, "width": 100, "height": 24, "timestamp": int(now), "title": "web-prod-01 demo"}) + "\n")
    t = 0.0
    for chunk in ("\x1b[1;32mdeploy@web-prod-01\x1b[0m:\x1b[1;34m~\x1b[0m$ ", "uptime\r\n", " 09:41:07 up 41 days,  load average: 0.21, 0.18, 0.15\r\n",
                  "\x1b[1;32mdeploy@web-prod-01\x1b[0m:\x1b[1;34m~\x1b[0m$ ", "df -h /\r\n", "/dev/sda1   80G   31G   45G  41% /\r\n"):
        t += 0.7
        f.write(json.dumps([round(t, 2), "o", chunk]) + "\n")

os.environ["RC_DEMO"], os.environ["RC_OUT"] = DEMO, OUT
sys.path.insert(0, HERE)
import demo_env as E  # noqa: E402  (starts the real program on the demo data)

import tkinter as tk  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from tkinter import messagebox, simpledialog, filedialog  # noqa: E402
import rc_monitor  # noqa: E402
import rc_terminal  # noqa: E402

app, root = E.app, E.root
messagebox.showinfo = messagebox.showwarning = messagebox.showerror = lambda *a, **k: None
messagebox.askyesno = messagebox.askokcancel = messagebox.askretrycancel = lambda *a, **k: True
messagebox.askyesnocancel = lambda *a, **k: True
E.pump(3.0)
E.app.theme.set_theme("Sky Blue")
E.pump(1.0)

# ─── 2. video recorder (frames are written to disk as they are grabbed) ─────
FRAMES = os.path.join(OUT, "frames")
caption = {"text": ""}
frame_no = [0]
RECORDING = not STILLS_ONLY
if RECORDING:
    import shutil
    shutil.rmtree(FRAMES, ignore_errors=True)
    os.makedirs(FRAMES)
VW, VH = 1280, 720


def snap_frame():
    """One video frame of the window with the current caption on a bar at the bottom."""
    l, t, r, b = E.win_box()
    img = E.ImageGrab.grab(bbox=(l, t + 5, r, b), all_screens=True).convert("RGB")
    scale = min(VW / img.width, (VH - 70) / img.height)
    img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    canvas = Image.new("RGB", (VW, VH), (13, 27, 62))
    canvas.paste(img, ((VW - img.width) // 2, 0))
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, VH - 70, VW, VH), fill=(8, 17, 41))
    d.rectangle((0, VH - 70, 8, VH), fill=(33, 150, 243))
    try:
        f = ImageFont.truetype("segoeuib.ttf", 26)
    except OSError:
        f = ImageFont.load_default()
    d.text((28, VH - 50), caption["text"], fill=(255, 255, 255), font=f)
    canvas.save(os.path.join(FRAMES, "f%05d.jpg" % frame_no[0]), quality=88)
    frame_no[0] += 1


def play(seconds, text=None, fps=8):
    """Let the program run for `seconds` while recording (video) - the same call just pumps when only taking pictures."""
    if text is not None:
        caption["text"] = text
    end = time.time() + seconds
    nxt = 0.0
    while time.time() < end:
        root.update()
        if RECORDING and time.time() >= nxt:
            snap_frame()
            nxt = time.time() + 1.0 / fps
        time.sleep(0.01)


def typing(widget, text, per_char=0.05):
    """Type into an Entry/Combobox/Text like a person does (video shows each letter)."""
    for ch in text:
        widget.insert("end", ch)
        play(per_char)


def still(name, wait=0.4):
    play(wait)
    E.shot(name)


def clean():
    for w in list(root.winfo_children()):
        if isinstance(w, tk.Toplevel):
            w.destroy()
    E.close_extra_tabs()
    for attr in ("netscan_tab", "localsrv_tab", "notes_tab", "prefs_tab", "identities_tab", "proxies_tab"):
        setattr(app, attr, None)
    play(0.2)


def modal_shot(name, trigger, delay=0.9):
    """A dialog that blocks (simple prompts): take the picture from a timer, then close it."""
    def grab_and_close():
        play(0.3)
        E.shot(name)
        for w in list(root.winfo_children()):
            if isinstance(w, tk.Toplevel):
                w.destroy()
    root.after(int(delay * 1000), grab_and_close)
    try:
        trigger()
    except tk.TclError:
        pass


app.theme.set_theme("Sky Blue")
E.pick("docker-host")
play(0.5)

# ─── 3. the pictures (and the video) ────────────────────────────────────────
caption["text"] = "REGTeches RC  -  every server, one window"
play(3.0, "The Overview: live status dots, favourites and recent servers")
E.shot("m01_overview")

play(0.1, "Pick a server on the left to see every way to connect")
E.pick("docker-host")
play(2.0)
E.shot("m02_server_buttons")

# add a server (the form is a tab with seven pages)
play(0.1, "File > Add server: a form with one page per topic")
app.add_host()
play(1.0)
ed = root.nametowidget(app.nb.select())
pages = ed.pages
try:
    e_name = [w for w in ed.winfo_children()[1].winfo_children()] if False else None
except Exception:
    e_name = None
play(0.4)
names = ["General", "Login", "Connections", "Network", "Glances", "More connections", "After login"]
try:
    pages.select(0)
    play(0.4)
    E.shot("m03_add_general")
    for idx, nm in enumerate(names[1:], start=1):
        pages.select(idx)
        play(1.2, f"Add server - the {nm} page")
        E.shot("m03_add_" + nm.lower().replace(" ", "_"))
except Exception as e:
    print("editor pages:", e)
pages.select(0)
play(0.3)
try:      # type a new server like a person
    for w in ed.vars.values() if hasattr(ed, "vars") else []:
        pass
    name_var, addr_var = ed.vars.get("name"), ed.vars.get("address")
    play(0.1, "Type a name and the IP address - that is all that is needed")
    if name_var:
        for ch in "build-server":
            name_var.set(name_var.get() + ch)
            play(0.06)
    if addr_var:
        for ch in "10.50.2.99":
            addr_var.set(addr_var.get() + ch)
            play(0.06)
    play(1.2)
    E.shot("m03_add_filled")
except Exception as e:
    print("typing the new server skipped:", e)
clean()

# SSH terminal, quick commands, history
play(0.1, "Double-click or press SSH: a real terminal opens in a tab")
E.pick("web-prod-01")
app.act_ssh(E.host("web-prod-01"))
play(3.0)
E.shot("m04_ssh_terminal")
try:
    tab = root.nametowidget(app.nb.select())
    tab.toggle_panel()
    play(1.5, "⚡ Quick commands and History: click to run, paste or copy")
    E.shot("m05_quick_panel")
    tab.toggle_panel()
    play(0.4, "Line numbers: right-click the terminal to show them")
    tab.term.set_pref(line_numbers=True) if hasattr(tab, "term") else tab.set_pref(line_numbers=True)
    for t in ([tab.term] if hasattr(tab, "term") else [tab]):
        t.apply_gutter()
    play(1.5)
    E.shot("m04b_line_numbers")
    for t in ([tab.term] if hasattr(tab, "term") else [tab]):
        t.set_pref(line_numbers=False)
        t.apply_gutter()
except Exception as e:
    print("quick panel:", e)
clean()

# split view + broadcast
play(0.1, "Session > New split view: several servers tiled in one tab")
sp = app.new_split_view()
for n in ("web-prod-01", "db-prod-01", "docker-host", "mail-gateway"):
    sp.add_host(E.host(n))
    play(0.8)
play(2.5)
E.shot("m06_split_view")
try:
    sp.bcast_var.set(True)
    sp.toggle_broadcast()
    play(1.2, "📡 Type in ALL panes: one command goes to every server")
    E.shot("m06_broadcast")
except Exception as e:
    print("broadcast:", e)
clean()

# file manager
play(0.1, "Files: your PC on the left, the server on the right - drag and drop")
app.act_files(E.host("web-prod-01"))
play(2.5)
E.shot("m07_file_manager")
clean()

# stats (fake numbers; the real tab reads /proc over SSH or Glances)
play(0.1, "📊 Stats: live CPU, memory, network, disks, processes and ports")
E.pick("docker-host")
rc_monitor.MonitorTab.AUTO_START = False
mt = rc_monitor.MonitorTab(app, E.host("docker-host"), "ops", "")
app.add_tab(mt, "Stats · docker-host")
import random
random.seed(7)
cpu_state = [1_000_000, 4_000_000]
net_state = [120_000_000_000]


def sample_text(i):
    busy = random.randint(30, 70) if i % 9 else random.randint(75, 95)
    cpu_state[0] += busy * 40
    cpu_state[1] += (100 - busy) * 40
    net_state[0] += random.randint(900_000, 3_600_000)
    base = net_state[0]
    lines = ["@@os", "6.8.0-48-generic", "docker-host", 'PRETTY_NAME="Ubuntu 24.04.1 LTS"', "8",
             "@@up", "3567420.5 7231000.2", "0.%02d 0.%02d 0.15 2/430 9100" % (random.randint(20, 60), random.randint(15, 40)),
             "@@stat", "cpu  %d 0 %d %d 0 0 0 0" % (cpu_state[0], cpu_state[0] // 6, cpu_state[1])]
    for c in range(8):
        lines.append("cpu%d  %d 0 %d %d 0 0 0 0" % (c, cpu_state[0] // 8 + c * 9, cpu_state[0] // 50, cpu_state[1] // 8))
    lines += ["@@mem", "MemTotal:       16384000 kB", "MemFree:         1500000 kB", "MemAvailable:    9100000 kB", "Buffers:          200000 kB",
              "Cached:          6000000 kB", "SwapTotal:       4194304 kB", "SwapFree:        4000000 kB",
              "@@disk", "Filesystem 1K-blocks Used Available Use% Mounted on",
              "/dev/sda1 83886080 32505856 47185920 41% /", "/dev/sdb1 3906250000 2734375000 1171875000 70% /mnt/media",
              "@@net", "Inter-|   Receive", " face |bytes packets errs drop fifo frame compressed multicast|bytes",
              "    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0",
              "  eth0: %d 100 0 0 0 0 0 0 %d 90 0 0 0 0 0 0" % (base, base // 3 + i * 400_000),
              "@@ps", "PID USER %CPU %MEM RSS ELAPSED COMMAND",
              "1432 ops 38.2 4.1 671000 3-04:11:00 /usr/bin/dockerd -H fd://",
              "2210 root 22.5 2.6 428000 3-04:10:00 plexmediaserver",
              "3101 ops 9.8 1.2 196000 1-02:00:11 /usr/bin/python3 /opt/homeassistant",
              "908 www-data 3.1 0.4 64000 41-06:02:00 nginx: worker process",
              "1 root 0.2 0.1 12000 41-06:02:00 /sbin/init", "4410 ops 0.1 0.3 52000 05:12 sshd: ops@pts/0",
              "@@ports", "Netid State Recv-Q Send-Q Local Address:Port Peer Address:Port",
              'tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=900,fd=3))',
              'tcp LISTEN 0 4096 0.0.0.0:9000 0.0.0.0:* users:(("docker-proxy",pid=2200,fd=4))',
              'tcp LISTEN 0 4096 0.0.0.0:32400 0.0.0.0:* users:(("Plex",pid=2210,fd=6))',
              'tcp LISTEN 0 4096 0.0.0.0:8123 0.0.0.0:* users:(("python3",pid=3101,fd=7))', "@@end"]
    return "\n".join(lines)


try:
    for i in range(26):
        mt.apply(rc_monitor.parse_sample(sample_text(i), now=1_000_000 + i * 5))
        mt.status_var.set("Live - updated just now")
        play(0.18)
    play(1.0)
    E.shot("m08_stats")
except Exception as e:
    print("stats:", e)

# the other Stats pages: the commands they would send over SSH are answered here with made-up output
import rc_capture
import rc_docker
import rc_firewall
import rc_hardware


def fake_output(command):
    if command == rc_firewall.DETECT_COMMAND:
        return "@@uid\n0\n@@tools\nufw\niptables\n@@end"
    if command == rc_capture.DETECT_COMMAND:
        return "@@uid\n0\n@@tool\n/usr/bin/tcpdump\n@@timeout\n/usr/bin/timeout\n@@ifaces\neth0\nlo\ndocker0\n@@end"
    if command == rc_hardware.IPMI_COMMAND:
        return "@@uid\n0\n@@tool\n/usr/bin/ipmitool\n@@end"
    if command == rc_hardware.HW_COMMAND:
        return ("@@uid\n0\n@@dmi\nsys_vendor=Demo Systems\nproduct_name=Rack Server R200\nboard_name=DS-R200\nbios_version=2.4.1\n"
                "bios_date=03/14/2026\n@@cpu\nModel name: 8-core x86 processor\nCPU(s): 8\n@@thermal\nx86_pkg_temp|52000\nacpitz|34000\n"
                "@@hwmon\ncoretemp|Core 0|49000|100000|84000\ncoretemp|Core 1|51000|100000|84000\ncoretemp|Core 2|81000|100000|84000\n"
                "nvme|Composite|41000|85000|75000\n@@fans\nnct6775|CPU fan|1240\nnct6775|Front fan|880\nnct6775|Rear fan|915\n"
                "@@disks\nsda disk 80G DemoSSD 80\nsdb disk 3.6T DemoDisk 4TB\n@@smartctl\n/usr/sbin/smartctl\n@@end")
    if command.startswith("sh -c 'for d in "):
        return ("@@disk sda\nDevice Model: DemoSSD 80\nSMART overall-health self-assessment test result: PASSED\n"
                "  9 Power_On_Hours 0x0032 099 099 000 Old_age Always - 8123\n194 Temperature_Celsius 0x0022 064 050 000 Old_age Always - 36\n"
                "@@disk sdb\nDevice Model: DemoDisk 4TB\nSMART overall-health self-assessment test result: PASSED\n"
                "  5 Reallocated_Sector_Ct 0x0033 100 100 010 Pre-fail Always - 0\n  9 Power_On_Hours 0x0032 080 080 000 Old_age Always - 17540\n"
                "194 Temperature_Celsius 0x0022 112 099 000 Old_age Always - 38\n197 Current_Pending_Sector 0x0012 100 100 000 Old_age Always - 0\n")
    if "ipmitool sdr" in command:
        return ("CPU Temp         | 48 degrees C      | ok\nSystem Temp      | 31 degrees C      | ok\nFAN1             | 3100 RPM          | ok\n"
                "FAN2             | 2900 RPM          | ok\n12V              | 12.19 Volts       | ok\n5V               | 5.04 Volts        | ok\n"
                "PS1 Status       | 0x01              | ok\nPS2 Status       | 0x00              | nc\n")
    if "ipmitool sel" in command:
        return ("   1 | 11/02/2026 | 09:14:22 | Power Supply PS2 | Failure detected | Asserted\n"
                "   2 | 11/02/2026 | 09:31:05 | Power Supply PS2 | Failure detected | Deasserted\n"
                "   3 | 11/18/2026 | 02:00:41 | System Event | OEM System boot | Asserted\n")
    if "ipmitool chassis" in command:
        return "System Power         : on\nPower Overload       : false\nMain Power Fault     : false\nCooling/Fan Fault    : false\n"
    if "docker ps" in command:
        rows = [("portainer", "running", "Up 41 days", "portainer/portainer-ce:2.21", "9000/tcp", "0.4%", "88MiB"),
                ("plex", "running", "Up 41 days", "plexinc/pms-docker:latest", "32400/tcp", "12.6%", "612MiB"),
                ("homeassistant", "running", "Up 3 days", "homeassistant/home-assistant", "8123/tcp", "4.2%", "420MiB"),
                ("nginx", "running", "Up 12 days", "nginx:1.27", "80/tcp, 443/tcp", "0.1%", "24MiB"),
                ("backup-job", "exited", "Exited (0) 5 hours ago", "demo/backup:3", "", "", ""),
                ("worker", "exited", "Exited (1) 2 hours ago", "demo/worker:1.8", "", "", "")]
        ps = "\n".join(json.dumps({"Names": n, "State": s, "Status": st, "Image": i, "Ports": p, "ID": "a1b2c3d4e5f6"}) for n, s, st, i, p, _, _ in rows)
        stats = "\n".join(json.dumps({"Name": n, "CPUPerc": c, "MemUsage": m + " / 16GiB"}) for n, s, _, _, _, c, m in rows if s == "running")
        return f"@@ps\n{ps}\n@@stats\n{stats}\n@@end"
    if "ufw" in command:
        return ("Status: active\n\n     To                         Action      From\n     --                         ------      ----\n"
                "[ 1] 22/tcp                     ALLOW IN    10.50.0.0/16\n[ 2] 80/tcp                     ALLOW IN    Anywhere\n"
                "[ 3] 443/tcp                    ALLOW IN    Anywhere\n[ 4] 32400/tcp                  ALLOW IN    Anywhere\n"
                "[ 5] 23/tcp                     DENY IN     Anywhere\n[ 6] 9000/tcp                   ALLOW IN    10.50.4.0/24\n")
    return ""


try:
    mt.run_async = lambda command, done, timeout=30: root.after(60, lambda: done(0, fake_output(command), "", None))
    for page, shot_name, text in (
            (mt.page_docker, "m08b_docker", "🐳 Docker: containers with start, stop, restart and logs"),
            (mt.page_hardware, "m08c_hardware", "Hardware: temperatures, fans and disk health"),
            (mt.page_ipmi, "m08d_ipmi", "IPMI: sensors and event log from the management controller"),
            (mt.page_firewall, "m08e_firewall", "Firewall: see and change ufw or firewalld rules"),
            (mt.page_capture, "m08f_capture", "Capture: watch network packets live with tcpdump")):
        play(0.1, text)
        mt.book.select(page)
        play(0.3)
        if not page.loaded:
            page.load()
        play(1.2)
        if page is mt.page_capture:
            lines = []
            for n in range(22):
                src, dst, info = random.choice((
                    ("10.50.4.21.51544", "10.50.2.20.22", "Flags [P.], seq 1:53, ack 1, win 501, length 52"),
                    ("10.50.2.20.32400", "10.50.4.77.49210", "Flags [.], ack 1449, win 624, length 1448"),
                    ("10.50.2.20.41822", "10.50.2.53.53", "UDP, length 41"),
                    ("10.50.4.21.60022", "10.50.2.20.9000", "Flags [S], seq 3021, win 64240, length 0")))
                lines.append(f"2026-11-24 09:41:{7 + n // 3:02d}.{random.randint(100000, 999999)} IP {src} > {dst}: {info}")
            page.add_lines(lines)
        play(1.2)
        E.shot(shot_name)
except Exception as e:
    print("stats pages:", repr(e))
clean()

# network scan
play(0.1, "Tools > Network > Network scan: find the machines on your network")
app.open_netscan()
play(0.8)
try:
    ns = app.netscan_tab
    fake = [("10.50.0.1", "core-router", "AA:BB:CC:00:00:01", [53, 80, 443]), ("10.50.1.10", "web-prod-01", "AA:BB:CC:00:10:10", [22, 80, 443]),
            ("10.50.1.20", "db-prod-01", "AA:BB:CC:00:10:20", [22, 5432]), ("10.50.2.20", "docker-host", "AA:BB:CC:00:20:20", [22, 9000, 32400, 61208]),
            ("10.50.2.53", "pi-hole", "AA:BB:CC:00:20:53", [22, 53, 80]), ("10.50.3.10", "nas-main", "AA:BB:CC:00:30:10", [22, 139, 445, 5000]),
            ("10.50.4.21", "office-pc", "AA:BB:CC:00:40:21", [135, 445, 3389]), ("10.50.4.77", "", "AA:BB:CC:00:40:77", [80, 9100])]
    ns.target.set("10.50.0.0/16")
    for ip, nm, mac, ports in fake:
        ns.host_done(ip, True, ports)
        ns.progress.configure(value=min(100, ns.progress["value"] + 12))
        play(0.35)
    ns.apply_names({ip: mac for ip, _, mac, _ in fake}, {ip: nm for ip, nm, _, _ in fake})
    ns.info.set("8 machine(s) found.")
    ns.progress.configure(value=100)
    play(1.0, "Double-click a machine to add it as a server; Scan more ports digs deeper")
    ns.tree.selection_set("10.50.2.20")
    E.shot("m09_network_scan")
except Exception as e:
    print("netscan:", e)
clean()

# notes, identities, proxies, local servers, preferences
for key, fn, text, shot_name in (
        ("notes", app.open_notes, "📝 Notes: tagged Markdown notes, attached to servers", "m10_notes"),
        ("identities", app.open_identities, "👤 Identities: one saved login shared by many servers", "m11_identities"),
        ("proxies", app.open_proxies, "🌐 Proxies: reach servers through SOCKS5 or HTTP proxies", "m12_proxies"),
        ("local servers", app.open_localsrv, "Local servers: a temporary TFTP or web server with a request log", "m13_local_servers"),
        ("preferences", app.open_prefs, "⚙ Preferences: confirmations, reopening tabs, refresh timing", "m14_preferences")):
    try:
        play(0.1, text)
        fn()
        play(1.5)
        E.shot(shot_name)
    except Exception as e:
        print(key, "skipped:", e)
    clean()

# snippets, tunnels, keys, trusted host keys
for key, fn, text, shot_name in (
        ("snippets", app.open_snippets, "Snippets: saved commands you can run on any server", "m15_snippets"),
        ("tunnels", app.open_tunnels, "SSH tunnels: reach private web pages and databases", "m16_tunnels"),
        ("keys", app.open_keys, "🔑 Keys: set up passwordless SSH for the whole lab", "m17_keys"),
        ("known hosts", app.open_known_hosts, "🛡 Trusted host keys: what RC has learned to trust", "m18_known_hosts")):
    try:
        play(0.1, text)
        fn()
        play(1.3)
        E.shot(shot_name)
    except Exception as e:
        print(key, "skipped:", e)
    clean()

# recording playback
try:
    import rc_record
    play(0.1, "Play back a recorded terminal session")
    rc_record.play_file(app, os.path.join(DEMO, "recordings", "demo-session.cast"))
    play(0.5)
    root.nametowidget(app.nb.select()).restart()
    play(4.5)
    E.shot("m19_recording_player")
except Exception as e:
    print("player skipped:", e)
clean()

# quick connect
try:
    play(0.1, "Ctrl+K: type a few letters of a server name, press Enter")
    app.quick_connect()
    play(0.5)
    ent = getattr(app.palette, "entry", None)
    if ent is not None:
        typing(ent, "dock", 0.12)
    play(1.0)
    E.shot("m20_quick_connect")
    app.palette.close()
except Exception as e:
    print("quick connect skipped:", e)
play(0.3)

# modal prompts: backup, shortcuts
modal_shot("m21_backup_password", app.backup_everything)
try:
    play(0.1, "Folder sync: keep your PCs the same through a shared folder")
    app.open_sync()
    play(0.6)
    for w in root.winfo_children():
        if isinstance(w, tk.Toplevel):
            w.geometry("+%d+%d" % (root.winfo_rootx() + 480, root.winfo_rooty() + 260))
            w.attributes("-topmost", True)
    play(1.5)
    E.shot("m21b_folder_sync")
    clean()
except Exception as e:
    print("sync skipped:", e)
try:
    play(0.1, "Help > Keyboard shortcuts")
    app.show_shortcuts()
    play(1.0)
    E.shot("m22_shortcuts")
except Exception as e:
    print("shortcuts skipped:", e)
clean()

# menus (real key presses, so Windows opens the native menus)
E.menu_shot(3, "zz_warmup")
try:
    os.remove(os.path.join(OUT, "zz_warmup.png"))
except OSError:
    pass
for idx, nm in enumerate(("file", "connect", "servers", "session", "view", "tools", "help")):
    E.menu_shot(idx, "menu_" + nm)

# themes + about
for tname in ("Royal Dark", "Neon Purple"):
    try:
        E.pick("docker-host")
        app.theme.set_theme(tname)
        app.sync_theme_button()
        play(1.6, f"Colour themes: {tname}" if True else "")
        E.shot("m23_theme_" + tname.lower().replace(" ", "_"))
    except Exception as e:
        print("theme", e)
app.theme.set_theme("Sky Blue")
app.sync_theme_button()
play(0.8)
app.about()
play(1.0)
E.shot("m24_about")
clean()
caption["text"] = ""
play(0.5)
print("pictures:", len([f for f in os.listdir(OUT) if f.endswith(".png")]), "| video frames:", frame_no[0])
root.destroy()

# ─── 4. the video file ──────────────────────────────────────────────────────
if RECORDING and frame_no[0]:
    mp4 = os.path.join(OUT, "RC_demo_raw.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "8", "-i", os.path.join(FRAMES, "f%05d.jpg"),
                    "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-crf", "24", "-preset", "medium", mp4], check=True)
    print("saved", mp4)
