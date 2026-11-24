"""REGTeches RC - Hardware and IPMI pages of the Stats tab (temperatures, fans, disk health, machine details; IPMI sensors and event log).

Everything is read-only and goes over the Stats tab's SSH connection:
  * Hardware: temperatures and fans from the kernel's /sys/class/thermal and /sys/class/hwmon (no extra software needed), machine and
    CPU details, and, on request, disk health (SMART) with `smartctl` when the server has it. SMART needs root: when the login is not
    root RC uses `sudo -n` (passwordless sudo) and says so if that is missing.
  * IPMI: the sensors, event log and chassis status of a server's management controller through `ipmitool` (servers only; most PCs have
    no IPMI).
Parsing is pure and tested without a server; the pages are at the bottom."""

import re
import tkinter as tk
from tkinter import ttk

import rc_monitor as mon

DISK_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")

HW_COMMAND = (
    "sh -c 'echo @@uid; id -u; echo @@dmi; for f in sys_vendor product_name product_version board_name bios_version bios_date; do "
    "echo \"$f=$(cat /sys/class/dmi/id/$f 2>/dev/null)\"; done; echo @@cpu; lscpu 2>/dev/null | head -30; echo @@thermal; "
    "for z in /sys/class/thermal/thermal_zone*; do [ -r $z/temp ] && echo \"$(cat $z/type 2>/dev/null)|$(cat $z/temp 2>/dev/null)\"; done; "
    "echo @@hwmon; for h in /sys/class/hwmon/hwmon*; do n=$(cat $h/name 2>/dev/null); for t in $h/temp*_input; do [ -r \"$t\" ] || continue; "
    "b=${t%_input}; echo \"$n|$(cat ${b}_label 2>/dev/null)|$(cat $t 2>/dev/null)|$(cat ${b}_crit 2>/dev/null)|$(cat ${b}_max 2>/dev/null)\"; done; done; "
    "echo @@fans; for h in /sys/class/hwmon/hwmon*; do n=$(cat $h/name 2>/dev/null); for f in $h/fan*_input; do [ -r \"$f\" ] || continue; "
    "b=${f%_input}; echo \"$n|$(cat ${b}_label 2>/dev/null)|$(cat $f 2>/dev/null)\"; done; done; "
    "echo @@disks; lsblk -dno NAME,TYPE,SIZE,MODEL 2>/dev/null; echo @@smartctl; command -v smartctl; echo @@end'")

IPMI_COMMAND = ("sh -c 'echo @@uid; id -u; echo @@tool; command -v ipmitool; echo @@end'")


def _num(text, default=None):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


# ─── hardware ────────────────────────────────────────────────────────────────

def parse_hw(text):
    """HW_COMMAND output -> dict(root, dmi{}, cpu[lines], temps[(source, label, C, high, crit)], fans[(source, label, rpm)],
    disks[{name,type,size,model}], smartctl bool)."""
    sec = mon.split_sections(text)
    dmi = {}
    for line in sec.get("dmi", []):
        k, _, v = line.partition("=")
        if v.strip():
            dmi[k.strip()] = v.strip()
    temps = []
    for line in sec.get("thermal", []):
        name, _, raw = line.partition("|")
        c = _num(raw)
        if c is not None:
            temps.append((name.strip() or "thermal", "", c / 1000.0, None, None))
    for line in sec.get("hwmon", []):
        p = line.split("|")
        if len(p) >= 3 and _num(p[2]) is not None:
            high, crit = _num(p[4]) if len(p) > 4 else None, _num(p[3])
            temps.append((p[0].strip(), p[1].strip(), _num(p[2]) / 1000.0, high / 1000.0 if high else None, crit / 1000.0 if crit else None))
    fans = []
    for line in sec.get("fans", []):
        p = line.split("|")
        if len(p) >= 3 and _num(p[2]) is not None:
            fans.append((p[0].strip(), p[1].strip(), int(_num(p[2]))))
    disks = []
    for line in sec.get("disks", []):
        p = line.split(None, 3)
        if len(p) >= 3 and p[1] == "disk" and DISK_RE.match(p[0]):
            disks.append({"name": p[0], "type": p[1], "size": p[2], "model": p[3].strip() if len(p) > 3 else ""})
    uid = (sec.get("uid") or ["1"])[0].strip()
    return {"root": uid == "0", "dmi": dmi, "cpu": [l for l in sec.get("cpu", []) if l.strip()], "temps": temps, "fans": fans,
            "disks": disks, "smartctl": any(l.strip() for l in sec.get("smartctl", []))}


def temp_level(c, high, crit):
    """'crit', 'warn' or '' for a temperature, using the sensor's own limits when it reports them, else common rules of thumb."""
    if crit and c >= crit - 2:
        return "crit"
    if high and c >= high - 5:
        return "warn"
    if not crit and not high:
        return "crit" if c >= 90 else "warn" if c >= 80 else ""
    return ""


def smart_command(names, is_root):
    names = [n for n in names if DISK_RE.match(n)]
    if not names:
        raise ValueError("no disks")
    s = "" if is_root else "sudo -n "
    loop = " ".join(names)
    return f"sh -c 'for d in {loop}; do echo \"@@disk $d\"; {s}smartctl -H -A -i /dev/$d 2>&1; done'"


def parse_smart(text):
    """smartctl output for several disks -> {name: dict(health, temp, hours, realloc, pending, used, model, note)}."""
    out, cur, name = {}, None, None
    for line in text.splitlines():
        m = re.match(r"^@@disk (\S+)", line)
        if m:
            name = m.group(1)
            cur = out[name] = {"health": "", "temp": None, "hours": None, "realloc": None, "pending": None, "used": None, "model": "", "note": ""}
            continue
        if cur is None:
            continue
        low = line.lower()
        if "permission denied" in low or "sudo:" in low or "a password is required" in low:
            cur["note"] = "needs root (passwordless sudo)"
        elif "smartctl: command not found" in low or "command not found" in low:
            cur["note"] = "smartctl is not installed"
        elif "smart support is: unavailable" in low or "unsupported" in low and "smart" in low:
            cur["note"] = cur["note"] or "no SMART on this disk"
        m = re.search(r"overall-health self-assessment test result:\s*(\S+)", line, re.I) or re.search(r"SMART Health Status:\s*(\S+)", line, re.I)
        if m:
            cur["health"] = m.group(1).upper()
        m = re.match(r"^(Device Model|Model Number|Product):\s*(.+)$", line.strip())
        if m and not cur["model"]:
            cur["model"] = m.group(2).strip()
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)", line)       # ATA attribute table
        if m:
            attr, raw = m.group(2), int(m.group(3))
            if attr in ("Temperature_Celsius", "Airflow_Temperature_Cel", "Temperature_Internal"):
                cur["temp"] = raw & 0xFF
            elif attr == "Power_On_Hours":
                cur["hours"] = raw
            elif attr in ("Reallocated_Sector_Ct", "Reallocated_Event_Count"):
                cur["realloc"] = raw
            elif attr == "Current_Pending_Sector":
                cur["pending"] = raw
        m = re.match(r"^Temperature:\s+(\d+)\s*Celsius", line.strip())                 # NVMe
        if m:
            cur["temp"] = int(m.group(1))
        m = re.match(r"^Power On Hours:\s+([\d,]+)", line.strip())
        if m:
            cur["hours"] = int(m.group(1).replace(",", ""))
        m = re.match(r"^Percentage Used:\s+(\d+)%", line.strip())
        if m:
            cur["used"] = int(m.group(1))
    return out


def smart_level(d):
    """'crit' / 'warn' / '' for one disk's SMART summary."""
    if d["health"] and d["health"] not in ("PASSED", "OK"):
        return "crit"
    if d["pending"] or (d["realloc"] or 0) > 0 or (d["used"] or 0) >= 90 or (d["temp"] or 0) >= 55:
        return "warn"
    return ""


def format_dmi(hw):
    d = hw["dmi"]
    lines = []
    for label, key in (("Maker", "sys_vendor"), ("Model", "product_name"), ("Version", "product_version"), ("Board", "board_name"),
                       ("BIOS", "bios_version"), ("BIOS date", "bios_date")):
        if d.get(key):
            lines.append(f"{label}:  {d[key]}")
    if hw["cpu"]:
        lines += ["", "CPU"] + ["  " + l.strip() for l in hw["cpu"]]
    return "\n".join(lines) or "The server does not report machine details (virtual machines and some boards do not)."


# ─── IPMI ────────────────────────────────────────────────────────────────────

def parse_ipmi_probe(text):
    sec = mon.split_sections(text)
    return (sec.get("uid") or ["1"])[0].strip() == "0", any(l.strip() for l in sec.get("tool", []))


def ipmi_command(what, is_root):
    s = "" if is_root else "sudo -n "
    cmds = {"sdr": "ipmitool sdr list", "sel": "ipmitool sel elist last 60", "chassis": "ipmitool chassis status"}
    if what not in cmds:
        raise ValueError("unknown IPMI query")
    return f"{s}{cmds[what]} 2>&1"


def parse_sdr(text):
    """`ipmitool sdr list` -> [(name, reading, status)]."""
    out = []
    for line in text.splitlines():
        p = [x.strip() for x in line.split("|")]
        if len(p) >= 3 and p[0]:
            out.append((p[0], p[1], p[2]))
    return out


def sdr_level(status):
    s = status.lower()
    if s in ("ok", "ns", "na", "") or s.startswith("0x"):
        return ""
    return "crit" if s in ("cr", "nr", "lnr", "unc", "ucr") or "crit" in s or "fail" in s else "warn"


def parse_sel(text):
    """`ipmitool sel elist` -> [(id, date, time, sensor, event, state)] (newest first)."""
    out = []
    for line in text.splitlines():
        p = [x.strip() for x in line.split("|")]
        if len(p) >= 5 and re.fullmatch(r"[0-9a-fA-F]+", p[0]):
            out.append((p[0], p[1], p[2], p[3], p[4], p[5] if len(p) > 5 else ""))
    out.reverse()
    return out


def ipmi_problem(text):
    low = text.lower()
    if "could not open device" in low or "no such file" in low:
        return "This machine has no IPMI device (that is normal for PCs)."
    if "a password is required" in low or "sudo:" in low or "permission denied" in low:
        return "IPMI needs root. This login is not root and has no passwordless sudo (sudo -n)."
    if "unable to establish" in low or "insufficient privilege" in low:
        return "ipmitool could not talk to the management controller."
    return ""


# ─── the pages ───────────────────────────────────────────────────────────────

def _tree(parent, cols, widths, anchors=None):
    t = ttk.Treeview(parent, columns=[c for c, _ in cols], show="headings", selectmode="browse")
    for (c, title), w in zip(cols, widths):
        t.heading(c, text=title)
        t.column(c, width=w, anchor=(anchors or {}).get(c, "w"))
    t.tag_configure("crit", foreground="#b00020")
    t.tag_configure("warn", foreground="#b26a00")
    return t


class HardwarePage(ttk.Frame):
    """Added to the Stats tab as "Hardware"."""

    def __init__(self, mon_tab):
        super().__init__(mon_tab.book, padding=4)
        self.mon = mon_tab
        self.loaded = False
        self.hw = None
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 4))
        self.info = tk.StringVar(value="Opens when you click this tab.")
        ttk.Label(top, textvariable=self.info, foreground="#555").pack(side="left")
        ttk.Button(top, text="⟳ Refresh", command=self.load).pack(side="right")
        self.sub = ttk.Notebook(self)
        self.sub.pack(fill="both", expand=True)
        f1, f2, f3, f4 = (ttk.Frame(self.sub, padding=2) for _ in range(4))
        for f, t in ((f1, "Temperatures"), (f2, "Fans"), (f3, "Disk health"), (f4, "Machine")):
            self.sub.add(f, text=f"  {t}  ")
        self.temps = _tree(f1, [("src", "Sensor"), ("label", "Label"), ("c", "°C"), ("high", "High"), ("crit", "Critical")], (160, 200, 70, 70, 80),
                           {"c": "e", "high": "e", "crit": "e"})
        self.temps.pack(fill="both", expand=True)
        self.fans = _tree(f2, [("src", "Chip"), ("label", "Fan"), ("rpm", "RPM")], (200, 200, 90), {"rpm": "e"})
        self.fans.pack(fill="both", expand=True)
        bar = ttk.Frame(f3)
        bar.pack(fill="x")
        self.smart_btn = ttk.Button(bar, text="Check disk health (SMART)", command=self.check_smart)
        self.smart_btn.pack(side="left")
        self.smart_info = tk.StringVar()
        ttk.Label(bar, textvariable=self.smart_info, foreground="#555").pack(side="left", padx=8)
        self.disks = _tree(f3, [("name", "Disk"), ("model", "Model"), ("size", "Size"), ("health", "Health"), ("temp", "°C"), ("hours", "Power-on h"),
                                ("realloc", "Reallocated"), ("pending", "Pending"), ("used", "Worn %")], (70, 220, 70, 80, 50, 90, 90, 70, 70),
                           {"temp": "e", "hours": "e", "realloc": "e", "pending": "e", "used": "e"})
        self.disks.pack(fill="both", expand=True, pady=(4, 0))
        self.text = tk.Text(f4, wrap="none", font=("Consolas", 9), state="disabled", height=14)
        self.text.pack(fill="both", expand=True)

    def load(self):
        self.info.set("Loading ...")

        def done(code, out, err, error):
            self.loaded = True
            if error:
                self.info.set(f"Could not read the hardware: {error}")
                return
            self.hw = parse_hw(out)
            self.show()
        self.mon.run_async(HW_COMMAND, done, timeout=40)

    def show(self):
        hw = self.hw
        self.temps.delete(*self.temps.get_children())
        for i, (src, label, c, high, crit) in enumerate(sorted(hw["temps"], key=lambda t: -t[2])):
            self.temps.insert("", "end", iid=f"t{i}", values=(src, label, f"{c:.0f}", f"{high:.0f}" if high else "", f"{crit:.0f}" if crit else ""),
                              tags=(temp_level(c, high, crit),) if temp_level(c, high, crit) else ())
        self.fans.delete(*self.fans.get_children())
        for i, (src, label, rpm) in enumerate(hw["fans"]):
            self.fans.insert("", "end", iid=f"f{i}", values=(src, label or f"fan {i + 1}", rpm), tags=("crit",) if rpm == 0 else ())
        self.disks.delete(*self.disks.get_children())
        for d in hw["disks"]:
            self.disks.insert("", "end", iid=d["name"], values=(d["name"], d["model"], d["size"], "", "", "", "", "", ""))
        self.smart_info.set("" if hw["smartctl"] else "smartctl is not installed on this server (package smartmontools).")
        self.smart_btn.state(["!disabled"] if hw["smartctl"] and hw["disks"] else ["disabled"])
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", format_dmi(hw))
        self.text.configure(state="disabled")
        hot = [t for t in hw["temps"] if temp_level(t[2], t[3], t[4]) == "crit"]
        peak = max((t[2] for t in hw["temps"]), default=None)
        self.info.set((f"{len(hw['temps'])} temperature sensor(s), hottest {peak:.0f} °C" if peak is not None else "No temperature sensors reported")
                      + (f"  ·  {len(hot)} too hot" if hot else "") + (f"  ·  {len(hw['fans'])} fan(s)" if hw["fans"] else ""))

    def check_smart(self):
        if not self.hw or not self.hw["disks"]:
            return
        self.smart_info.set("Reading SMART ...")
        try:
            cmd = smart_command([d["name"] for d in self.hw["disks"]], self.hw["root"])
        except ValueError:
            return

        def done(code, out, err, error):
            if error:
                self.smart_info.set(f"Could not read SMART: {error}")
                return
            res = parse_smart(out + err)
            notes = set()
            for d in self.hw["disks"]:
                r = res.get(d["name"])
                if not r:
                    continue
                if r["note"] and not r["health"]:
                    notes.add(r["note"])
                lvl = smart_level(r)
                self.disks.item(d["name"], values=(d["name"], r["model"] or d["model"], d["size"], r["health"] or "?",
                                                   "" if r["temp"] is None else r["temp"], "" if r["hours"] is None else r["hours"],
                                                   "" if r["realloc"] is None else r["realloc"], "" if r["pending"] is None else r["pending"],
                                                   "" if r["used"] is None else r["used"]), tags=(lvl,) if lvl else ())
            bad = [n for n, r in res.items() if smart_level(r) == "crit"]
            self.smart_info.set(("; ".join(sorted(notes)) if notes else f"Read {len(res)} disk(s)") + (f"  ·  {len(bad)} failing: {', '.join(bad)}" if bad else ""))
        self.mon.run_async(cmd, done, timeout=60)


class IpmiPage(ttk.Frame):
    """Added to the Stats tab as "IPMI"."""

    def __init__(self, mon_tab):
        super().__init__(mon_tab.book, padding=4)
        self.mon = mon_tab
        self.loaded = False
        self.root_login = False
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 4))
        self.info = tk.StringVar(value="Opens when you click this tab.")
        ttk.Label(top, textvariable=self.info, foreground="#555").pack(side="left")
        ttk.Button(top, text="⟳ Refresh", command=self.load).pack(side="right")
        self.sub = ttk.Notebook(self)
        self.sub.pack(fill="both", expand=True)
        f1, f2, f3 = (ttk.Frame(self.sub, padding=2) for _ in range(3))
        for f, t in ((f1, "Sensors"), (f2, "Event log"), (f3, "Chassis")):
            self.sub.add(f, text=f"  {t}  ")
        self.sdr = _tree(f1, [("name", "Sensor"), ("value", "Reading"), ("status", "Status")], (260, 200, 100))
        self.sdr.pack(fill="both", expand=True)
        self.sel = _tree(f2, [("id", "#"), ("date", "Date"), ("time", "Time"), ("sensor", "Sensor"), ("event", "Event"), ("state", "State")],
                         (50, 90, 80, 220, 280, 90))
        self.sel.pack(fill="both", expand=True)
        self.text = tk.Text(f3, wrap="none", font=("Consolas", 9), state="disabled", height=14)
        self.text.pack(fill="both", expand=True)

    def load(self):
        self.info.set("Checking for ipmitool ...")

        def probed(code, out, err, error):
            self.loaded = True
            if error:
                self.info.set(f"Could not check the server: {error}")
                return
            self.root_login, has = parse_ipmi_probe(out)
            if not has:
                self.info.set("ipmitool is not installed on this server (package ipmitool), so there is no IPMI information.")
                return
            self.info.set("Reading the controller ...")
            for what, handler in (("sdr", self.got_sdr), ("sel", self.got_sel), ("chassis", self.got_chassis)):
                self.mon.run_async(ipmi_command(what, self.root_login), handler, timeout=60)
        self.mon.run_async(IPMI_COMMAND, probed)

    def problem(self, out, err, error):
        if error:
            self.info.set(f"IPMI failed: {error}")
            return True
        p = ipmi_problem(out + err)
        if p:
            self.info.set(p)
            return True
        return False

    def got_sdr(self, code, out, err, error):
        if self.problem(out, err, error):
            return
        rows = parse_sdr(out)
        self.sdr.delete(*self.sdr.get_children())
        for i, (n, v, s) in enumerate(rows):
            lvl = sdr_level(s)
            self.sdr.insert("", "end", iid=f"s{i}", values=(n, v, s), tags=(lvl,) if lvl else ())
        bad = sum(1 for r in rows if sdr_level(r[2]) == "crit")
        self.info.set(f"{len(rows)} sensor(s)" + (f"  ·  {bad} critical" if bad else "") + ("" if self.root_login else "  ·  via sudo -n"))

    def got_sel(self, code, out, err, error):
        if error or ipmi_problem(out + err):
            return
        self.sel.delete(*self.sel.get_children())
        for i, row in enumerate(parse_sel(out)):
            self.sel.insert("", "end", iid=f"e{i}", values=row, tags=("warn",) if "assert" in row[5].lower() and "going high" in row[4].lower() else ())

    def got_chassis(self, code, out, err, error):
        if error or ipmi_problem(out + err):
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", out.strip() or "(no output)")
        self.text.configure(state="disabled")
