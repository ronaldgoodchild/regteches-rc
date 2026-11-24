"""REGTeches RC - firewall page of the Stats tab (ufw and firewalld rules over SSH, iptables / nftables read-only).

What it does: finds which firewall the server uses, lists its rules, and lets you add an allow/deny rule for a port, remove a rule,
and turn ufw on/off. Every change asks first and warns when it could cut your own SSH connection. iptables and nftables are shown
as plain text only (their rule sets are too free-form to edit safely from a table).
Commands are built here from validated pieces only (ports, protocols, addresses, service names) so nothing typed in a box can
become shell code. The parsing and command building are pure and tested without a server; the page is at the bottom."""

import ipaddress
import re
import tkinter as tk
from tkinter import messagebox, ttk

import rc_monitor as mon

SERVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PROTOS = ("tcp", "udp", "any")

DETECT_COMMAND = ("sh -c 'echo @@uid; id -u; echo @@tools; for t in ufw firewall-cmd nft iptables; do "
                  "command -v $t >/dev/null 2>&1 && echo $t; done; echo @@end'")


class FirewallError(ValueError):
    """A readable problem with what was typed."""


# ─── detection ───────────────────────────────────────────────────────────────

def parse_detect(text):
    """-> (is_root, tools list) from DETECT_COMMAND output."""
    sec = mon.split_sections(text)
    uid = (sec.get("uid") or ["1"])[0].strip()
    tools = [l.strip() for l in sec.get("tools", []) if l.strip() in ("ufw", "firewall-cmd", "nft", "iptables")]
    return uid == "0", tools


def pick_backend(tools):
    """ufw and firewalld can be edited; otherwise show iptables / nftables as text. None if there is no firewall tool."""
    for t, name in (("ufw", "ufw"), ("firewall-cmd", "firewalld"), ("nft", "nftables"), ("iptables", "iptables")):
        if t in tools:
            return name
    return None


def sudo(is_root):
    return "" if is_root else "sudo -n "


def list_command(backend, is_root):
    s = sudo(is_root)
    if backend == "ufw":
        return f"{s}ufw status numbered 2>&1"
    if backend == "firewalld":
        return f"sh -c '{s}firewall-cmd --state 2>&1; echo @@zone; {s}firewall-cmd --list-all 2>&1'"
    if backend == "nftables":
        return f"{s}nft list ruleset 2>&1"
    if backend == "iptables":
        return f"{s}iptables -S 2>&1"
    raise FirewallError("No firewall tool found on this server.")


# ─── validation of what the user types ───────────────────────────────────────

def clean_port(text):
    """'22', '8000:8100' or '8000-8100' -> '22' / '8000:8100'."""
    t = (text or "").strip().replace("-", ":")
    m = re.fullmatch(r"(\d{1,5})(?::(\d{1,5}))?", t)
    if not m:
        raise FirewallError("A port is a number from 1 to 65535, or a range like 8000-8100.")
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    if not (1 <= a <= 65535 and 1 <= b <= 65535) or a > b:
        raise FirewallError("A port is a number from 1 to 65535, or a range like 8000-8100.")
    return str(a) if a == b else f"{a}:{b}"


def clean_source(text):
    """'' / 'any' -> '' ; an IP or network -> its normal text."""
    t = (text or "").strip()
    if t.lower() in ("", "any", "anywhere"):
        return ""
    try:
        return str(ipaddress.ip_network(t, strict=False)) if "/" in t else str(ipaddress.ip_address(t))
    except ValueError:
        raise FirewallError("The source must be an IP address (10.0.0.5) or a network (10.0.0.0/24), or empty for anywhere.")


def clean_proto(text):
    t = (text or "any").strip().lower()
    if t not in PROTOS:
        raise FirewallError("The protocol must be tcp, udp or any.")
    return t


# ─── ufw ─────────────────────────────────────────────────────────────────────

RULE_NUM = re.compile(r"^\[\s*(\d+)\]\s+(.*)$")


def parse_ufw(text):
    """-> (state, rules). state 'active' / 'inactive' / 'unknown'; rules = [{num, to, action, source, v6}]."""
    low = text.lower()
    state = "active" if "status: active" in low else "inactive" if "status: inactive" in low else "unknown"
    rules = []
    for line in text.splitlines():
        m = RULE_NUM.match(line.strip())
        if not m:
            continue
        parts = re.split(r"\s{2,}", m.group(2).strip())
        if len(parts) < 3:
            continue
        to, action, src = parts[0], parts[1], parts[2]
        rules.append({"num": int(m.group(1)), "to": to.replace("(v6)", "").strip(), "action": action, "source": src.replace("(v6)", "").strip(),
                      "v6": "(v6)" in line})
    return state, rules


def ufw_add_command(action, port, proto, source, is_root):
    if action not in ("allow", "deny", "reject", "limit"):
        raise FirewallError("The action must be allow, deny, reject or limit.")
    port, proto, source = clean_port(port), clean_proto(proto), clean_source(source)
    suffix = "" if proto == "any" else f"/{proto}"
    if source:
        pr = "" if proto == "any" else f" proto {proto}"
        return f"{sudo(is_root)}ufw {action} from {source} to any port {port}{pr} 2>&1"
    return f"{sudo(is_root)}ufw {action} {port}{suffix} 2>&1"


def ufw_delete_command(num, is_root):
    return f"{sudo(is_root)}ufw --force delete {int(num)} 2>&1"


def ufw_power_command(on, is_root):
    return f"{sudo(is_root)}ufw --force {'enable' if on else 'disable'} 2>&1"


# ─── firewalld ───────────────────────────────────────────────────────────────

def parse_firewalld(text):
    """-> (state, zone, rules). rules = [{kind, value}] for ports / services / sources / rich rules of the default zone."""
    head, _, rest = text.partition("@@zone")
    state = (head.strip().splitlines() or ["unknown"])[0].strip()
    lines = [l.rstrip() for l in rest.splitlines()]
    zone = next((l.split("(")[0].strip() for l in lines if l.strip() and not l.startswith((" ", "	"))), "")
    rules = []
    for line in lines:
        m = re.match(r"^\s+(ports|services|sources):\s*(.*)$", line)
        if m:
            kind = {"ports": "port", "services": "service", "sources": "source"}[m.group(1)]
            rules += [{"kind": kind, "value": v} for v in m.group(2).split()]
        elif line.strip().startswith("rule "):
            rules.append({"kind": "rich", "value": line.strip()})
    return state, zone, rules


def firewalld_command(change, kind, value, is_root):
    """change 'add'/'remove', kind 'port'/'service', value '8080/tcp' or 'http'. Permanent, then reload."""
    if change not in ("add", "remove"):
        raise FirewallError("bad change")
    if kind == "port":
        m = re.fullmatch(r"(\d{1,5}(?:-\d{1,5})?)/(tcp|udp)", value.strip().replace(":", "-"))
        if not m:
            raise FirewallError("bad port")
        clean_port(m.group(1))
        arg = f"--{change}-port={m.group(1)}/{m.group(2)}"
    elif kind == "service":
        if not SERVICE_RE.match(value):
            raise FirewallError("bad service name")
        arg = f"--{change}-service={value}"
    else:
        raise FirewallError("Only ports and services can be changed from here.")
    s = sudo(is_root)
    return f"sh -c '{s}firewall-cmd --permanent {arg} 2>&1 && {s}firewall-cmd --reload 2>&1'"


def firewalld_port(port, proto):
    port, proto = clean_port(port), clean_proto(proto)
    if proto == "any":
        raise FirewallError("firewalld needs tcp or udp for a port.")
    return f"{port.replace(':', '-')}/{proto}"


# ─── safety check ────────────────────────────────────────────────────────────

def covers_port(spec, port):
    """Does a rule's port text ('22', '22/tcp', '8000:9000/tcp', '22 (v6)') include `port`?"""
    m = re.match(r"^(\d{1,5})(?:[:-](\d{1,5}))?(?:/\w+)?", spec.strip())
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    return a <= int(port) <= b


def lockout_warning(action, port, source, ssh_port):
    """A sentence if this new rule could block the connection RC itself uses, else ''."""
    if action in ("deny", "reject") and not source and covers_port(clean_port(port), ssh_port):
        return f"This blocks port {ssh_port}, which is the port RC uses to reach this server. You would be locked out."
    return ""


def ssh_allowed(rules, ssh_port):
    return any(covers_port(r["to"], ssh_port) and r["action"].upper().startswith(("ALLOW", "LIMIT")) for r in rules)


# ─── the page ────────────────────────────────────────────────────────────────

class FirewallPage(ttk.Frame):
    """Added to the Stats tab's notebook as "Firewall". Uses the tab's run_async for every command."""

    def __init__(self, mon_tab):
        super().__init__(mon_tab.book, padding=4)
        self.mon = mon_tab
        self.loaded = False
        self.is_root, self.backend = None, None
        self.state, self.rules = "unknown", []
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 4))
        self.info = tk.StringVar(value="Opens when you click this tab.")
        ttk.Label(top, textvariable=self.info, foreground="#555").pack(side="left")
        ttk.Button(top, text="⟳ Refresh", command=self.load).pack(side="right")
        self.acts = ttk.Frame(self)
        self.acts.pack(fill="x", pady=(0, 4))
        self.add_btn = ttk.Button(self.acts, text="Add rule…", command=self.add_rule)
        self.del_btn = ttk.Button(self.acts, text="Delete selected rule", command=self.delete_rule)
        self.power_btn = ttk.Button(self.acts, text="Turn ufw on", command=self.toggle_power)
        for b in (self.add_btn, self.del_btn, self.power_btn):
            b.pack(side="left", padx=(0, 6))
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        cols = [("to", "To (port / service)"), ("action", "Action"), ("source", "From")]
        self.tree = ttk.Treeview(self.body, columns=[c for c, _ in cols], show="headings", selectmode="browse")
        for (c, t), w in zip(cols, (260, 160, 260)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("deny", foreground="#b00020")
        self.tree.pack(fill="both", expand=True)
        self.text = tk.Text(self.body, wrap="none", font=("Consolas", 9), height=12)

    # loading
    def load(self):
        self.info.set("Loading ...")

        def detected(code, out, err, error):
            self.loaded = True
            if error:
                self.info.set(f"Could not read the firewall: {error}")
                return
            self.is_root, tools = parse_detect(out)
            self.backend = pick_backend(tools)
            if not self.backend:
                self.info.set("No firewall tool (ufw, firewalld, nftables, iptables) was found on this server.")
                self.show_mode()
                return
            self.show_mode()
            self.mon.run_async(list_command(self.backend, self.is_root), self.listed)
        self.mon.run_async(DETECT_COMMAND, detected)

    def show_mode(self):
        editable = self.backend in ("ufw", "firewalld")
        for b in (self.add_btn, self.del_btn):
            b.state(["!disabled"] if editable else ["disabled"])
        if self.backend == "ufw":
            self.power_btn.pack(side="left")
        else:
            self.power_btn.pack_forget()
        if editable or not self.backend:
            self.text.pack_forget()
            self.tree.pack(fill="both", expand=True)
        else:
            self.tree.pack_forget()
            self.text.pack(fill="both", expand=True)

    def listed(self, code, out, err, error):
        if error:
            self.info.set(f"Could not read the rules: {error}")
            return
        text = out + err
        low = text.lower()
        if "a password is required" in low or "sudo:" in low or "permission denied" in low or "you need to be root" in low:
            self.info.set("Reading the firewall needs root. This login is not root and has no passwordless sudo (sudo -n).")
            return
        self.tree.delete(*self.tree.get_children())
        if self.backend == "ufw":
            self.state, self.rules = parse_ufw(text)
            for r in self.rules:
                self.tree.insert("", "end", iid=str(r["num"]), values=(r["to"] + (" (v6)" if r["v6"] else ""), r["action"], r["source"]),
                                 tags=("deny",) if r["action"].upper().startswith(("DENY", "REJECT")) else ())
            self.power_btn.configure(text="Turn ufw off" if self.state == "active" else "Turn ufw on")
            self.info.set(f"ufw is {self.state}  ·  {len(self.rules)} rule(s)" + ("" if self.is_root else "  ·  changes use sudo -n"))
        elif self.backend == "firewalld":
            self.state, zone, self.rules = parse_firewalld(text)
            for n, r in enumerate(self.rules):
                self.tree.insert("", "end", iid=str(n), values=(r["value"], r["kind"], zone))
            self.info.set(f"firewalld is {self.state}  ·  default zone {zone or '?'}  ·  {len(self.rules)} entr(ies)")
        else:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text.strip() or "(empty)")
            self.text.configure(state="disabled")
            self.info.set(f"{self.backend} rules (read-only: change them in a terminal)")

    # changes
    def run_change(self, command, what):
        self.info.set(f"{what} ...")

        def done(code, out, err, error):
            text = (error and str(error)) or (out + err).strip()
            if error or code not in (0, None):
                messagebox.showwarning("Firewall", f"{what} failed:\n\n{text or 'no details'}"
                                       + ("" if self.is_root else "\n\nThis login is not root: it needs passwordless sudo (sudo -n)."), parent=self)
            self.load()
        self.mon.run_async(command, done)

    def add_rule(self):
        AddRuleDialog(self)

    def submit_rule(self, action, port, proto, source):
        """Called by the dialog with what was typed. Returns an error text, or '' when it was accepted."""
        try:
            ssh_port = int(self.mon.host.get("ssh_port") or 22)
            warn = lockout_warning(action, port, clean_source(source), ssh_port) if self.backend == "ufw" else ""
            if self.backend == "ufw":
                cmd = ufw_add_command(action, port, proto, source, self.is_root)
            elif self.backend == "firewalld":
                if action != "allow":
                    raise FirewallError("firewalld rules here can only allow a port (remove it to close it).")
                cmd = firewalld_command("add", "port", firewalld_port(port, proto), self.is_root)
            else:
                raise FirewallError("This firewall is read-only here.")
        except FirewallError as e:
            return str(e)
        if not messagebox.askyesno("Firewall", f"On {self.mon.host['name']}: {action} {port}/{proto}"
                                   + (f" from {source}" if source.strip() else "") + "?" + (f"\n\n{warn}" if warn else ""),
                                   parent=self, icon="warning" if warn else "question"):
            return "cancelled"
        self.run_change(cmd, "Adding the rule")
        return ""

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def delete_rule(self):
        iid = self.selected()
        if iid is None:
            messagebox.showinfo("Firewall", "Click a rule in the list first.", parent=self)
            return
        ssh_port = int(self.mon.host.get("ssh_port") or 22)
        if self.backend == "ufw":
            rule = next((r for r in self.rules if str(r["num"]) == iid), None)
            if not rule:
                return
            warn = ""
            if covers_port(rule["to"], ssh_port) and rule["action"].upper().startswith(("ALLOW", "LIMIT")) \
                    and sum(1 for r in self.rules if covers_port(r["to"], ssh_port) and r["action"].upper().startswith(("ALLOW", "LIMIT"))) == 1:
                warn = f"\n\nThis is the only rule that allows port {ssh_port} (what RC connects to). If ufw is on, you could be locked out."
            if not messagebox.askyesno("Firewall", f"Delete rule {rule['num']}: {rule['to']} {rule['action']} {rule['source']}?{warn}",
                                       parent=self, icon="warning" if warn else "question"):
                return
            self.run_change(ufw_delete_command(rule["num"], self.is_root), "Deleting the rule")
        elif self.backend == "firewalld":
            rule = self.rules[int(iid)]
            if rule["kind"] not in ("port", "service"):
                messagebox.showinfo("Firewall", "Only ports and services can be removed from here.", parent=self)
                return
            if not messagebox.askyesno("Firewall", f"Remove {rule['kind']} {rule['value']} from the default zone (permanent)?", parent=self):
                return
            try:
                cmd = firewalld_command("remove", rule["kind"], rule["value"], self.is_root)
            except FirewallError as e:
                messagebox.showwarning("Firewall", str(e), parent=self)
                return
            self.run_change(cmd, "Removing")

    def toggle_power(self):
        on = self.state != "active"
        ssh_port = int(self.mon.host.get("ssh_port") or 22)
        warn = ""
        if on and not ssh_allowed(self.rules, ssh_port):
            warn = f"\n\nNo rule allows port {ssh_port} (what RC connects to). Turning ufw on now would lock you out. Add an allow rule for it first."
        if not messagebox.askyesno("Firewall", f"Turn ufw {'on' if on else 'off'} on {self.mon.host['name']}?{warn}", parent=self,
                                   icon="warning" if warn else "question"):
            return
        self.run_change(ufw_power_command(on, self.is_root), f"Turning ufw {'on' if on else 'off'}")


class AddRuleDialog(tk.Toplevel):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.title("Add a firewall rule")
        self.transient(page.winfo_toplevel())
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        self.action = tk.StringVar(value="allow")
        self.port = tk.StringVar()
        self.proto = tk.StringVar(value="tcp")
        self.source = tk.StringVar()
        rows = (("Action", ttk.Combobox(f, state="readonly", width=10, textvariable=self.action, values=["allow", "deny", "reject", "limit"]
                                        if page.backend == "ufw" else ["allow"])),
                ("Port (or 8000-8100)", ttk.Entry(f, width=14, textvariable=self.port)),
                ("Protocol", ttk.Combobox(f, state="readonly", width=10, textvariable=self.proto,
                                          values=["tcp", "udp", "any"] if page.backend == "ufw" else ["tcp", "udp"])),
                ("From (empty = anywhere)", ttk.Entry(f, width=22, textvariable=self.source)))
        for i, (label, w) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=3)
            w.grid(row=i, column=1, sticky="w", padx=8)
            if page.backend == "firewalld" and label.startswith("From"):
                w.state(["disabled"])
        self.err = tk.StringVar()
        ttk.Label(f, textvariable=self.err, foreground="#b00020", wraplength=320).grid(row=4, column=0, columnspan=2, sticky="w")
        bar = ttk.Frame(f)
        bar.grid(row=5, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(bar, text="Add", command=self.go).pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="left", padx=6)

    def go(self):
        err = self.page.submit_rule(self.action.get(), self.port.get(), self.proto.get(), self.source.get())
        if err == "":
            self.destroy()
        elif err != "cancelled":
            self.err.set(err)
