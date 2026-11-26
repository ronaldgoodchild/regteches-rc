"""Builds docs/features.html: every feature of REGTeches RC grouped by area, with the total counted from the list below.

    python make_features.py

Edit FEATURES when RC gains a feature, then run this again: the totals and the version follow by themselves.
Only list what the program really does (README.md and CHANGELOG.md are the reference)."""
import html
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
OUT = os.path.join(SRC, "docs", "features.html")
VERSION = re.search(r'APP_VERSION = "([^"]+)"', open(os.path.join(SRC, "rc_manager.py"), encoding="utf-8").read()).group(1)

# (icon, area, picture in docs/assets or "", [features])
FEATURES = [
    ("🔌", "Ways to connect", "m02_server_buttons", [
        "SSH terminal inside the window", "SSH in Windows' own console window", "File manager (in app)", "Live server stats",
        "Remote Desktop (RDP)", "VNC screen sharing", "RustDesk (by ID or IP address)", "Web console in your browser",
        "Remote PowerShell", "Telnet terminal", "Serial (COM port) terminal", "Webmail shortcut", "Open network shares in Explorer",
        "Ping", "Wake-on-LAN", "Several connections per server"]),
    ("🗂", "Server list and Overview", "m01_overview", [
        "Add, edit, duplicate and delete servers", "Groups", "Search box", "Favorites", "Recently connected list",
        "Live status dots", "Overview home screen", "Not-answering list", "Default double-click action per server",
        "Verify settings before saving", "Check this server (DNS and service tests)", "Test login", "Server notes field",
        "Export server list to CSV"]),
    ("🖥", "SSH terminal", "m04_ssh_terminal", [
        "Full-screen programs (vim, htop, tmux)", "256 and true colour", "Scrollback up to 50,000 lines", "Find in output",
        "Text zoom", "Automatic reconnect", "Keep-alive packets", "8 built-in colour schemes", "Import colour schemes",
        "Choice of fixed-width font", "Copy on select", "Right-click and middle-click paste", "Multi-line paste check",
        "Log session to a file", "Timestamps in log files", "Save output to a file", "Line numbers", "Clear screen, keep scrollback"]),
    ("⚡", "Speed and automation", "m05_quick_panel", [
        "Ctrl+K quick connect", "Quick commands panel", "Command history", "Command suggestions (Ctrl+Space)", "Saved snippets",
        "Snippet groups", "Run a snippet on many servers", "After-login scripts", "Keyboard shortcuts"]),
    ("🧩", "Tabs, split view and workspaces", "m06_split_view", [
        "Tabbed sessions", "Split view with up to 9 terminals", "Grid, side-by-side and stacked layouts", "Pane zoom",
        "Broadcast typing to many terminals", "Saved workspaces", "Duplicate and rename sessions", "Lock tabs",
        "Drag to reorder tabs", "Close other, left or right tabs", "Move a tab into a split view", "Reopen last session's tabs"]),
    ("📼", "Recording and notes", "m19_recording_player", [
        "Session recording (asciicast)", "Recording player with speed and seek", "Markdown notes", "Note tags and search",
        "Notes attached to servers"]),
    ("📁", "File manager", "m07_file_manager", [
        "Two-pane layout", "SFTP", "FTP and FTPS", "WebDAV (Nextcloud, NAS)", "S3-compatible storage", "Drag and drop",
        "Transfer queue with progress", "Pause, resume and retry transfers", "Overwrite rules", "Keeps file timestamps",
        "Folder synchronise with preview", "Built-in editor", "Open in a Windows program with auto-upload",
        "Copy, cut and paste on the server", "New file, folder and rename", "Change permissions", "Symbolic links",
        "Clickable path trail", "Sortable and choosable columns", "Type-ahead and filter boxes", "Hidden files switch",
        "Folder bookmarks", "Type a path into the terminal", "Zmodem upload and download (rz / sz)"]),
    ("📊", "Monitoring", "m08_stats", [
        "CPU graph and load", "Memory and swap", "Network speed graph", "Disk usage warnings", "Process list with filter",
        "Process details and signals", "Listening ports", "systemd services: start, stop, enable", "Service logs",
        "System information and clock check", "Glances support (including Windows PCs)", "Selectable refresh interval"]),
    ("🛠", "Server administration", "m08b_docker", [
        "Docker container list", "Start, stop, restart and remove containers", "Container logs", "Temperatures and fans",
        "SMART disk health", "IPMI sensors, event log and chassis", "ufw firewall rules", "firewalld ports and services",
        "View nftables and iptables", "Lock-out warnings before firewall changes", "Live packet capture (tcpdump)",
        "Save captures as .pcap for Wireshark"]),
    ("🌐", "Network tools", "m09_network_scan", [
        "Network scan", "Deep scan of all 65,535 ports", "Machine names and MAC addresses", "Add a found machine as a server",
        "Export scan results to CSV", "SSH tunnels: local, remote and dynamic", "SOCKS5 and HTTP proxies", "Jump servers",
        "Alternate addresses (LAN and VPN)", "Temporary TFTP server", "Temporary HTTP file server", "List serial ports"]),
    ("🔑", "Logins and security", "m17_keys", [
        "Saved passwords encrypted by Windows", "Reusable identities", "SSH key files and SSH agent", "One-click key login for a server",
        "Keys for all servers at once", "Authenticator (TOTP) two-step logins", "Trusted host key checks", "Host key manager",
        "Forget all saved passwords"]),
    ("💾", "Moving, backup and sync", "m21b_folder_sync", [
        "Encrypted backup and restore", "Encrypted sync between PCs", "Automatic sync at start and exit",
        "Import from PuTTY", "Import from WinSCP", "Import from ssh config", "Import from MobaXterm", "Import from Xshell",
        "Import from Devolutions RDM", "Import from CSV or text"]),
    ("🎨", "Look, help and upkeep", "m23_theme_neon_purple", [
        "Seven colour themes", "Interface size 80% to 200%", "Preferences page", "Web manual with video", "Built-in quick start guide",
        "Activity log with slow-step timing", "Damaged settings are set aside, not overwritten", "Portable: no installer needed",
        "Free and open source (MIT)"]),
]

TOTAL = sum(len(f[3]) for f in FEATURES)


def build():
    cards, jump = [], []
    for i, (icon, area, pic, items) in enumerate(FEATURES):
        lis = "".join(f"<li>{html.escape(t)}</li>" for t in items)
        img = (f'<img src="assets/{pic}.png" alt="{html.escape(area)} in REGTeches RC" loading="lazy">'
               if pic and os.path.isfile(os.path.join(SRC, "docs", "assets", pic + ".png")) else "")
        jump.append(f'<a href="#a{i}">{icon} {html.escape(area)} <b>{len(items)}</b></a>')
        cards.append(f'<section class="area" id="a{i}"><h2><span>{icon}</span> {html.escape(area)} <em>{len(items)}</em></h2>{img}<ul>{lis}</ul></section>')
    page = PAGE
    for key, value in (("{{VERSION}}", VERSION), ("{{TOTAL}}", str(TOTAL)), ("{{AREAS}}", str(len(FEATURES))),
                       ("{{JUMP}}", "\n".join(jump)), ("{{CARDS}}", "\n".join(cards))):
        page = page.replace(key, value)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    print(f"{OUT}: {TOTAL} features in {len(FEATURES)} areas (version {VERSION})")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>REGTeches RC - {{TOTAL}} Features</title>
<meta name="description" content="All {{TOTAL}} features of REGTeches RC, the free Remote Connection Manager for Windows.">
<link rel="icon" href="assets/favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#0b1220;--panel:#111b2e;--panel2:#16233b;--line:#243556;--text:#e6edf7;--muted:#9db0cd;--blue:#3b9eff;--blue2:#1f6fd1}
@media (prefers-color-scheme:light){:root{--bg:#f4f7fc;--panel:#fff;--panel2:#eef3fb;--line:#d4deee;--text:#14213b;--muted:#53627f;--blue:#1a6fd4;--blue2:#0f55a8}}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:16px}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 "Segoe UI",system-ui,Roboto,Arial,sans-serif}
a{color:var(--blue);text-decoration:none}
.wrap{max-width:1300px;margin:0 auto;padding:0 20px}
header{text-align:center;padding:42px 0 18px}
.brand{display:inline-block;background:linear-gradient(135deg,#d32f2f,#b71c1c);color:#fff;border-radius:10px;padding:5px 16px;font-weight:800;font-size:1.2rem;letter-spacing:.5px}
h1{font-size:clamp(1.8rem,5vw,3.2rem);line-height:1.15;margin:.45em 0 .1em}
h1 span{color:var(--blue);display:block}
.ver{color:var(--muted)}
.total{margin:22px auto 8px;max-width:520px;background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:14px 20px 18px}
.total b{display:block;font-size:clamp(4.5rem,18vw,8rem);line-height:1;color:var(--blue);font-variant-numeric:tabular-nums}
.total span{font-size:1.25rem;font-weight:700}
.total small{display:block;color:var(--muted)}
.btns{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin:18px 0}
.btn{background:var(--blue2);color:#fff;border:1px solid var(--blue2);border-radius:10px;padding:9px 18px;font-weight:700}
.btn.ghost{background:transparent;color:var(--text);border-color:var(--line)}
nav{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin:10px 0 20px}
nav a{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:999px;padding:5px 13px;font-size:.9rem}
nav a b{color:var(--blue)}
#q{display:block;width:100%;max-width:520px;margin:0 auto 6px;padding:11px 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:1rem}
#found{text-align:center;color:var(--muted);min-height:1.6em;margin:0 0 12px}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),1fr));gap:18px;align-items:start;padding-bottom:30px}
.area{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.area h2{display:flex;align-items:center;gap:9px;font-size:1.15rem;margin:0;padding:13px 16px;background:var(--panel2)}
.area h2 em{margin-left:auto;font-style:normal;background:var(--blue2);color:#fff;border-radius:999px;padding:0 11px;font-size:.95rem}
.area img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;object-position:top left;border-bottom:1px solid var(--line)}
.area ul{list-style:none;margin:0;padding:8px 16px 14px}
.area li{padding:4px 0 4px 26px;position:relative}
.area li::before{content:"\\2713";position:absolute;left:3px;color:var(--blue);font-weight:800}
.hide{display:none}
footer{border-top:1px solid var(--line);text-align:center;color:var(--muted);padding:24px 20px 40px;font-size:.92rem}
@media print{nav,#q,#found,.btns,.area img{display:none}.area{break-inside:avoid}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="brand">REGTeches</div>
  <h1>REGTeches RC <span>Remote Connection Manager</span></h1>
  <div class="ver">Version {{VERSION}} &middot; Windows &middot; Free and open source (MIT) &middot; Developed by Ronald Goodchild</div>
  <div class="total"><b>{{TOTAL}}</b><span>features in one window</span><small>across {{AREAS}} areas</small></div>
  <div class="btns">
    <a class="btn" href="https://github.com/ronaldgoodchild/regteches-rc/releases/latest">&#11015; Download</a>
    <a class="btn ghost" href="index.html">Manual and video</a>
    <a class="btn ghost" href="https://github.com/ronaldgoodchild/regteches-rc">Source on GitHub</a>
  </div>
</header>
<nav>
{{JUMP}}
</nav>
<input id="q" type="search" placeholder="Search the features..." aria-label="Search the features">
<p id="found" aria-live="polite"></p>
<main>
{{CARDS}}
</main>
</div>
<footer>REGTeches RC &middot; Remote Connection Manager {{VERSION}} &middot; REGTeches, Jacksonville FL. Pictures use sample data.</footer>
<script>
var q = document.getElementById('q'), found = document.getElementById('found');
q.addEventListener('input', function () {
  var s = q.value.trim().toLowerCase(), n = 0;
  document.querySelectorAll('.area').forEach(function (a) {
    var shown = 0, areaHit = a.querySelector('h2').textContent.toLowerCase().indexOf(s) > -1;
    a.querySelectorAll('li').forEach(function (li) {
      var ok = !s || areaHit || li.textContent.toLowerCase().indexOf(s) > -1;
      li.classList.toggle('hide', !ok);
      if (ok) shown++;
    });
    a.classList.toggle('hide', !shown);
    var img = a.querySelector('img');
    if (img) img.classList.toggle('hide', !!s);
    n += shown;
  });
  found.textContent = s ? n + (n === 1 ? ' feature matches' : ' features match') : '';
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    build()
