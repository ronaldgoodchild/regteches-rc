"""Writes a folder of FAKE servers (hosts.json, snippets.json, settings.json) for screenshots and demos.
Usage:  python make_demo_data.py <folder> [theme]
Then run RC with the environment variable RC_DATA_DIR pointing at that folder.
Nothing here is real: names and addresses are made up (10.50.x.x is a private range)."""
import json
import os
import sys
import uuid

out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "demo_data")
theme = sys.argv[2] if len(sys.argv) > 2 else "Sky Blue"
os.makedirs(out, exist_ok=True)


def web(label, url):
    return {"type": "web", "label": label, "port": 0, "url": url, "user": ""}


def host(name, addr, group, user, notes, web_url="", action="ssh", conns=(), tunnels=(), ssh=True, rdp=False, bm=()):
    return {"name": name, "address": addr, "group": group, "user": user, "ssh_port": 22, "rdp_port": 3389,
            "file_mode": "SFTP", "file_port": 0, "key_file": "", "web_url": web_url, "mail_url": "",
            "default_action": action, "notes": notes, "password_enc": "", "connections": list(conns),
            "tunnels": list(tunnels), "bookmarks": list(bm), "last_path": "", "id": uuid.uuid4().hex}


def tun(name, kind, listen, dest, dport):
    return {"id": uuid.uuid4().hex[:8], "name": name, "kind": kind, "bind": "127.0.0.1", "listen_port": listen,
            "dest_host": dest, "dest_port": dport}


hosts = [
    host("web-prod-01", "10.50.1.10", "Production", "deploy", "Public website, nginx + Node. Deploys on Fridays.",
         "https://10.50.1.10/", "web", [web("Grafana", "http://10.50.1.10:3000"), web("Status page", "http://10.50.1.10:8080")],
         [tun("Database (local)", "local", 5433, "10.50.1.20", 5432)], bm=["/var/www", "/etc/nginx"]),
    host("db-prod-01", "10.50.1.20", "Production", "dbadmin", "PostgreSQL 16 primary. Nightly dump at 02:00.",
         "", "ssh", [web("pgAdmin", "http://10.50.1.20:5050")], bm=["/var/lib/postgresql", "/backups"]),
    host("mail-gateway", "10.50.1.30", "Production", "mailadmin", "Postfix relay and spam filter.",
         "https://10.50.1.30:8443/", "web"),
    host("proxmox-lab", "10.50.2.5", "Home Lab", "root", "Proxmox cluster, 3 nodes.", "https://10.50.2.5:8006/", "web",
         [web("Node 2", "https://10.50.2.6:8006"), web("Node 3", "https://10.50.2.7:8006")]),
    host("docker-host", "10.50.2.20", "Home Lab", "ops", "Docker host: Portainer, Plex, Home Assistant, Grafana.",
         "http://10.50.2.20:9000/", "ssh",
         [web("Portainer", "http://10.50.2.20:9000"), web("Plex", "http://10.50.2.20:32400/web"),
          web("Home Assistant", "http://10.50.2.20:8123"), web("Sonarr", "http://10.50.2.20:8989"),
          web("Radarr", "http://10.50.2.20:7878"), web("Grafana", "http://10.50.2.20:3001")],
         [tun("Home Assistant", "local", 8124, "127.0.0.1", 8123), tun("SOCKS proxy", "dynamic", 1080, "", 0)],
         bm=["/opt/stacks", "/mnt/media"]),
    host("pi-hole", "10.50.2.53", "Home Lab", "pi", "Network-wide ad blocking and DNS.", "http://10.50.2.53/admin", "web"),
    host("nas-main", "10.50.3.10", "NAS & Storage", "admin", "Synology, 4 x 8 TB. Media, backups, photos.",
         "http://10.50.3.10:5000/", "files", [web("DSM", "http://10.50.3.10:5000")], bm=["/volume1/media", "/volume1/backup"]),
    host("nas-backup", "10.50.3.11", "NAS & Storage", "admin", "Offsite-sync target. Wakes at 01:00.",
         "http://10.50.3.11:5000/", "files"),
    host("office-pc", "10.50.4.21", "Workstations", "ronald", "Main desktop. Remote Desktop on.", "", "rdp", ssh=False, rdp=True),
    host("laptop-field", "10.50.4.22", "Workstations", "ronald", "Field laptop (reachable over Tailscale).", "", "rdp", rdp=True),
    host("core-router", "10.50.0.1", "Network", "admin", "Gateway, DHCP, firewall.", "http://10.50.0.1/", "web"),
    host("wifi-controller", "10.50.0.2", "Network", "admin", "Access-point controller.", "https://10.50.0.2:8443/", "web"),
]
import time
_now = time.time()
_extra = {
    "docker-host": {"favorite": True, "last_connected": _now - 600},
    "web-prod-01": {"favorite": True, "last_connected": _now - 3 * 3600},
    "nas-main": {"favorite": True, "last_connected": _now - 26 * 3600},
    "db-prod-01": {"last_connected": _now - 2 * 86400},
    "office-pc": {"rustdesk_id": "482193760", "mac": "AA:BB:CC:00:11:22", "last_connected": _now - 5 * 86400},
    "laptop-field": {"alt_addresses": ["100.64.0.22"], "rustdesk_id": "317 842 905"},
    "nas-backup": {"mac": "AA:BB:CC:33:44:55", "jump_host_name": "docker-host"},
}
_by_name = {h["name"]: h for h in hosts}
for _n, _f in _extra.items():
    _j = _f.pop("jump_host_name", None)
    _by_name[_n].update(_f)
    if _j:
        _by_name[_n]["jump_host"] = _by_name[_j]["id"]

for h in hosts:
    if h["default_action"] == "rdp":
        h["notes"] += " Remote Desktop ready."
json.dump({"hosts": hosts}, open(os.path.join(out, "hosts.json"), "w", encoding="utf-8"), indent=2)

snips = [
    {"id": "df", "name": "Disk space", "command": "df -h", "confirm": False},
    {"id": "up", "name": "Uptime and load", "command": "uptime", "confirm": False},
    {"id": "mem", "name": "Memory", "command": "free -h", "confirm": False},
    {"id": "upd", "name": "Updates waiting (Debian/Ubuntu)", "command": "apt list --upgradable 2>/dev/null | head -25", "confirm": False},
    {"id": "dock", "name": "Docker containers", "command": "docker ps --format 'table {{.Names}}\\t{{.Status}}'", "confirm": False},
    {"id": "rest", "name": "Restart nginx", "command": "sudo systemctl restart nginx", "confirm": True},
]
json.dump(snips, open(os.path.join(out, "snippets.json"), "w", encoding="utf-8"), indent=2)
json.dump({"theme": theme, "dark": theme in ("Royal Dark", "Neon Purple", "Classic Dark")},
          open(os.path.join(out, "settings.json"), "w", encoding="utf-8"), indent=2)
print("demo data written to", out)
