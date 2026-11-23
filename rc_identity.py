"""REGTeches RC - Identities (saved logins) and Proxies: the two manager tabs.

An identity is a reusable login (username + password, or username + key file, or username + the SSH agent) that
any number of servers can use, so a password changed once is changed everywhere. A proxy is a SOCKS5 or HTTP
(CONNECT) proxy that SSH connections go through. The data, the lookups and the proxy client live in rc_manager.py
(identities.json / proxies.json, passwords encrypted for this Windows account); this file is only the windows."""

import os
import threading
import tkinter as tk
import uuid
from tkinter import filedialog, messagebox, simpledialog, ttk

import rc_manager as core

log = core.log


class ListForm(ttk.Frame):
    """A list on the left, a form on the right. Subclasses fill in the form and the load/save/clear methods."""
    title = ""
    blurb = ""

    def __init__(self, app):
        super().__init__(app.nb, padding=12)
        self.app = app
        self.items = []
        self.cur = None          # id of the item shown in the form (None = a new one)
        ttk.Label(self, text=self.title, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self, foreground="#555", wraplength=900, justify="left", text=self.blurb).pack(anchor="w", pady=(2, 8))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(left, width=30, height=14, exportselection=False)
        self.listbox.pack(fill="y")
        self.listbox.bind("<<ListboxSelect>>", lambda _: self.pick())
        lb = ttk.Frame(left)
        lb.pack(fill="x", pady=4)
        ttk.Button(lb, text="➕ New", command=self.new).pack(side="left")
        ttk.Button(lb, text="🗑 Delete", command=self.delete).pack(side="left", padx=4)
        self.form = ttk.Frame(body)
        self.form.pack(side="left", fill="both", expand=True, padx=(14, 0), anchor="n")
        self.build_form()
        self.refill()
        self.new()

    # to be provided
    def build_form(self): ...
    def load_items(self): return []
    def label(self, it): return it["name"]
    def show(self, it): ...
    def clear(self): ...
    def collect(self): ...
    def write(self, items): ...
    def users_of(self, it): return []
    def forget(self, it): ...

    def refill(self, select=None):
        self.items = sorted(self.load_items(), key=lambda i: i["name"].lower())
        self.listbox.delete(0, "end")
        for it in self.items:
            self.listbox.insert("end", self.label(it))
        if select:
            for n, it in enumerate(self.items):
                if it["id"] == select:
                    self.listbox.selection_set(n)

    def pick(self):
        sel = self.listbox.curselection()
        if sel:
            self.cur = self.items[sel[0]]["id"]
            self.show(self.items[sel[0]])

    def new(self):
        self.cur = None
        self.listbox.selection_clear(0, "end")
        self.clear()

    def save(self):
        res = self.collect()
        if res is None:
            return
        res["id"] = self.cur or uuid.uuid4().hex
        items = self.load_items()
        if any(i["name"].lower() == res["name"].lower() and i["id"] != self.cur for i in items):
            messagebox.showwarning(self.title, f"There is already one called “{res['name']}”.", parent=self)
            return
        if self.cur:
            items = [res if i["id"] == self.cur else i for i in items]
        else:
            items.append(res)
        try:
            self.write(items)
        except OSError as e:
            messagebox.showerror(self.title, f"Could not save:\n{e}", parent=self)
            return
        self.cur = res["id"]
        self.refill(select=res["id"])
        self.app.status.set(f"Saved “{res['name']}”")
        self.show(next(i for i in self.load_items() if i["id"] == res["id"]))

    def delete(self):
        it = next((i for i in self.items if i["id"] == self.cur), None)
        if it is None:
            return
        users = self.users_of(it)
        extra = (f"\n\n{len(users)} server(s) use it ({', '.join(users[:6])}{'…' if len(users) > 6 else ''}) and will go back to "
                 "their own settings.") if users else ""
        if not messagebox.askyesno(self.title, f"Delete “{it['name']}”?{extra}", parent=self):
            return
        self.write([i for i in self.load_items() if i["id"] != it["id"]])
        self.forget(it)
        self.cur = None
        self.refill()
        self.clear()

    def browse_row(self, parent, row, label, var, width=40):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=var, width=width).grid(row=row, column=1, sticky="w", pady=3)


class IdentitiesTab(ListForm):
    title = "Identities (saved logins)"
    blurb = ("A reusable login. Give a server an identity on its Edit tab (\"Saved login\") and it connects with that username "
             "and password or key, replacing the server's own. Change a password here once and every server using it follows. "
             "Passwords are stored encrypted for this Windows account only.")

    def build_form(self):
        f = self.form
        self.name, self.user = tk.StringVar(), tk.StringVar()
        self.auth, self.pw, self.key = tk.StringVar(value="password"), tk.StringVar(), tk.StringVar()
        self.show_pw, self.forget_pw = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        self.saved_secret = ""
        self.browse_row(f, 0, "Name", self.name)
        self.browse_row(f, 1, "Username", self.user)
        ttk.Label(f, text="Sign in with").grid(row=2, column=0, sticky="w", pady=3)
        radios = ttk.Frame(f)
        radios.grid(row=2, column=1, sticky="w")
        for val, text in core.IDENTITY_AUTH:
            ttk.Radiobutton(radios, text=text, value=val, variable=self.auth, command=self.sync_state).pack(side="left", padx=(0, 10))
        self.pw_label = ttk.Label(f, text="Password")
        self.pw_label.grid(row=3, column=0, sticky="w", pady=3)
        self.pw_entry = ttk.Entry(f, textvariable=self.pw, width=40, show="•")
        self.pw_entry.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Checkbutton(f, text="Show", variable=self.show_pw,
                        command=lambda: self.pw_entry.configure(show="" if self.show_pw.get() else "•")).grid(row=3, column=2, padx=4)
        self.forget_box = ttk.Checkbutton(f, text="🔑 saved - tick to forget it", variable=self.forget_pw)
        self.forget_box.grid(row=4, column=1, sticky="w")
        ttk.Label(f, text="Key file").grid(row=5, column=0, sticky="w", pady=3)
        self.key_entry = ttk.Entry(f, textvariable=self.key, width=40)
        self.key_entry.grid(row=5, column=1, sticky="w", pady=3)
        self.key_btn = ttk.Button(f, text="Browse…", command=self.browse_key)
        self.key_btn.grid(row=5, column=2, padx=4)
        self.used = tk.StringVar()
        ttk.Label(f, textvariable=self.used, foreground="#555", wraplength=520, justify="left").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(10, 4))
        ttk.Button(f, text="💾 Save", command=self.save).grid(row=7, column=1, sticky="w", pady=6)

    def sync_state(self):
        a = self.auth.get()
        self.pw_label.configure(text="Key passphrase (if any)" if a == "key" else "Password")
        self.pw_entry.configure(state="normal" if a in ("password", "key") else "disabled")
        self.key_entry.configure(state="normal" if a == "key" else "disabled")
        self.key_btn.configure(state="normal" if a == "key" else "disabled")

    def browse_key(self):
        p = filedialog.askopenfilename(parent=self, title="Choose the private key file",
                                       initialdir=os.path.join(os.path.expanduser("~"), ".ssh"))
        if p:
            self.key.set(p)

    def load_items(self):
        return core.load_identities()

    def label(self, it):
        return it["name"] + (f"   ({it['user']})" if it["user"] else "")

    def users_of(self, it):
        return [h["name"] for h in self.app.hosts if h.get("identity") == it["id"]]

    def forget(self, it):
        for h in self.app.hosts:
            if h.get("identity") == it["id"]:
                h["identity"] = ""
        core.save_hosts(self.app.hosts)

    def clear(self):
        for v in (self.name, self.user, self.pw, self.key):
            v.set("")
        self.auth.set("password")
        self.saved_secret = ""
        self.forget_pw.set(False)
        self.forget_box.grid_remove()
        self.used.set("")
        self.sync_state()

    def show(self, it):
        self.name.set(it["name"])
        self.user.set(it["user"])
        self.auth.set(it["auth"])
        self.key.set(it["key_file"])
        self.pw.set("")
        self.saved_secret = it["secret_enc"]
        self.forget_pw.set(False)
        (self.forget_box.grid if it["secret_enc"] else self.forget_box.grid_remove)()
        users = self.users_of(it)
        self.used.set(f"Used by {len(users)} server(s): " + ", ".join(users[:8]) + ("…" if len(users) > 8 else "") if users
                      else "Not used by any server yet.")
        self.sync_state()

    def collect(self):
        name, user, auth = self.name.get().strip(), self.user.get().strip(), self.auth.get()
        if not name:
            messagebox.showwarning(self.title, "Give it a name.", parent=self)
            return None
        key = self.key.get().strip()
        if auth == "key":
            if not key or not os.path.isfile(key):
                messagebox.showwarning(self.title, "Choose the private key file (the one without .pub).", parent=self)
                return None
            if key.lower().endswith(".pub"):
                messagebox.showwarning(self.title, "That is the public key (.pub). Choose the private key file.", parent=self)
                return None
        if not user:
            if not messagebox.askyesno(self.title, "No username. RC will ask for it each time. Save anyway?", parent=self):
                return None
        secret = self.saved_secret
        if self.forget_pw.get() or auth == "agent":
            secret = ""
        if self.pw.get() and auth in ("password", "key"):
            secret = core.protect_password(self.pw.get())
        return {"id": self.cur or "", "name": name, "user": user, "auth": auth, "secret_enc": secret,
                "key_file": key if auth == "key" else ""}

    def write(self, items):
        core.save_identities([core.clean_identity(i) for i in items])


class ProxiesTab(ListForm):
    title = "Proxies"
    blurb = ("A SOCKS5 or HTTP (CONNECT) proxy to reach servers that are only visible through it. Give a server a proxy on its "
             "Edit tab (\"Connect via proxy\"). SSH terminals, file transfers (SFTP), tunnels, Stats, snippets and key setup use it; "
             "FTP/FTPS, Remote Desktop, VNC, web and ping do not. A server with a jump server uses the jump server's own proxy setting for the first hop.")

    def build_form(self):
        f = self.form
        self.name, self.host, self.port = tk.StringVar(), tk.StringVar(), tk.StringVar(value="1080")
        self.kind, self.user, self.pw = tk.StringVar(value="socks5"), tk.StringVar(), tk.StringVar()
        self.show_pw, self.forget_pw = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        self.saved_secret = ""
        self.browse_row(f, 0, "Name", self.name)
        ttk.Label(f, text="Type").grid(row=1, column=0, sticky="w", pady=3)
        box = ttk.Combobox(f, state="readonly", width=20, values=[l for _, l in core.PROXY_KINDS])
        box.grid(row=1, column=1, sticky="w", pady=3)
        box.bind("<<ComboboxSelected>>", lambda e: self.kind_chosen())
        self.kind_box = box
        self.browse_row(f, 2, "Proxy address", self.host)
        self.browse_row(f, 3, "Port", self.port, width=10)
        self.browse_row(f, 4, "Username (optional)", self.user)
        ttk.Label(f, text="Password (optional)").grid(row=5, column=0, sticky="w", pady=3)
        self.pw_entry = ttk.Entry(f, textvariable=self.pw, width=40, show="•")
        self.pw_entry.grid(row=5, column=1, sticky="w", pady=3)
        ttk.Checkbutton(f, text="Show", variable=self.show_pw,
                        command=lambda: self.pw_entry.configure(show="" if self.show_pw.get() else "•")).grid(row=5, column=2, padx=4)
        self.forget_box = ttk.Checkbutton(f, text="🔑 saved - tick to forget it", variable=self.forget_pw)
        self.forget_box.grid(row=6, column=1, sticky="w")
        self.used = tk.StringVar()
        ttk.Label(f, textvariable=self.used, foreground="#555", wraplength=520, justify="left").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(10, 4))
        bar = ttk.Frame(f)
        bar.grid(row=8, column=1, sticky="w", pady=6)
        ttk.Button(bar, text="💾 Save", command=self.save).pack(side="left")
        ttk.Button(bar, text="✔ Test…", command=self.test).pack(side="left", padx=8)

    def kind_chosen(self):
        kind = next((k for k, l in core.PROXY_KINDS if l == self.kind_box.get()), "socks5")
        self.kind.set(kind)
        if self.port.get() in ("", "1080", "8080"):
            self.port.set("1080" if kind == "socks5" else "8080")

    def load_items(self):
        return core.load_proxies()

    def label(self, it):
        return f"{it['name']}   ({dict(core.PROXY_KINDS)[it['kind']]})"

    def users_of(self, it):
        return [h["name"] for h in self.app.hosts if h.get("proxy") == it["id"]]

    def forget(self, it):
        for h in self.app.hosts:
            if h.get("proxy") == it["id"]:
                h["proxy"] = ""
        core.save_hosts(self.app.hosts)

    def clear(self):
        for v in (self.name, self.host, self.user, self.pw):
            v.set("")
        self.port.set("1080")
        self.kind.set("socks5")
        self.kind_box.set(dict(core.PROXY_KINDS)["socks5"])
        self.saved_secret = ""
        self.forget_pw.set(False)
        self.forget_box.grid_remove()
        self.used.set("")

    def show(self, it):
        self.name.set(it["name"])
        self.host.set(it["host"])
        self.port.set(str(it["port"]))
        self.kind.set(it["kind"])
        self.kind_box.set(dict(core.PROXY_KINDS)[it["kind"]])
        self.user.set(it["user"])
        self.pw.set("")
        self.saved_secret = it["password_enc"]
        self.forget_pw.set(False)
        (self.forget_box.grid if it["password_enc"] else self.forget_box.grid_remove)()
        users = self.users_of(it)
        self.used.set(f"Used by {len(users)} server(s): " + ", ".join(users[:8]) + ("…" if len(users) > 8 else "") if users
                      else "Not used by any server yet.")

    def collect(self, for_test=False):
        name, host = self.name.get().strip(), self.host.get().strip()
        if not name and not for_test:
            messagebox.showwarning(self.title, "Give it a name.", parent=self)
            return None
        if not host or not core.valid_address(host):
            messagebox.showwarning(self.title, "Enter the proxy's IP address or DNS name (without http:// or a port).", parent=self)
            return None
        try:
            port = int(self.port.get())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showwarning(self.title, "The port must be a number from 1 to 65535.", parent=self)
            return None
        secret = "" if self.forget_pw.get() else self.saved_secret
        if self.pw.get():
            secret = core.protect_password(self.pw.get())
        return {"id": self.cur or "", "name": name or host, "kind": self.kind.get(), "host": host, "port": port,
                "user": self.user.get().strip(), "password_enc": secret if self.user.get().strip() else ""}

    def write(self, items):
        core.save_proxies([core.clean_proxy(i) for i in items])

    def test(self):
        px = self.collect(for_test=True)
        if px is None:
            return
        default = "example.com:443"
        users = [h for h in self.app.hosts if self.cur and h.get("proxy") == self.cur]
        if users:
            default = f"{users[0]['address']}:{users[0]['ssh_port']}"
        target = simpledialog.askstring("Test proxy", "Server and port to reach through the proxy (host:port):",
                                        initialvalue=default, parent=self)
        if not target:
            return
        host, _, port = target.strip().rpartition(":")
        if not host or not port.isdigit():
            messagebox.showwarning("Test proxy", "Type it as host:port, for example 192.168.1.20:22.", parent=self)
            return
        self.app.status.set("Testing the proxy ...")

        def work():
            try:
                s = core.proxy_connect(px, host, int(port))
                banner = ""
                try:
                    s.settimeout(3)
                    banner = s.recv(60).decode("latin-1", "replace").strip().splitlines()[0][:60]
                except (OSError, IndexError):
                    pass
                s.close()
                msg = f"The proxy connected to {host}:{port}." + (f"\nIt answered: {banner}" if banner else "")
                ok = True
            except core.ProxyError as e:
                msg, ok = str(e), False
            except Exception as e:
                msg, ok = f"Unexpected error: {e}", False
            self.app.post(lambda: (messagebox.showinfo if ok else messagebox.showwarning)("Test proxy", msg, parent=self))
        threading.Thread(target=work, daemon=True, name="proxy-test").start()
