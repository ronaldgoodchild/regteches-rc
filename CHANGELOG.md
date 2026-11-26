# REGTeches RC - Version history

Newest first. The version number lives in one place, `APP_VERSION` in `rc_manager.py`; the window title, status bar,
About box, guide page, log file, `RC.exe` file properties and the release file names all follow it.

## 2.19.1

**Fixed**
- **Stats view tab names** are no longer cut off: the tabs now take the largest size that fits the window and adjust when it is resized.

## 2.19.0

**New**
- **Line numbers** in the terminal (right-click > Show line numbers).
- **Move a terminal tab into a split view**: from the tab's right-click menu, or by dragging the tab down into the window.

## 2.18.0

**New**
- **Hardware page** in the Stats tab (`rc_hardware.py`): temperatures, fans, machine and CPU details, and on request SMART disk health.
- **IPMI page**: sensors, event log and chassis status through ipmitool.

## 2.17.0

**New**
- **WebDAV, WebDAVS and S3** in the file manager (`rc_cloud.py`): choose them under *File transfer*; browse, transfer, copy, rename and delete
  on Nextcloud-style WebDAV servers and S3-compatible storage (buckets, folders as prefixes, AWS Signature V4).
- S3 region field in Edit server > Connections.

## 2.16.0

**New**
- **Files:** type-ahead in both panes, symbolic links shown (🔗, "Links to" column) and created (right-click), choosable columns.
- **Docker page** in the Stats tab (`rc_docker.py`): containers over SSH with start / stop / restart / remove / logs.
- **Import from Devolutions Remote Desktop Manager** (JSON export).

## 2.15.0

**New**
- **Authenticator (TOTP) logins** (`rc_totp.py`): save a server's authenticator secret and RC answers the password and verification
  code prompts of a two-step SSH login, types the code at later code prompts, and offers `${totp}` in login scripts.
- **Command suggestions** (`rc_suggest.py`): Ctrl+Space in a terminal lists matching commands from your history.
- **Colour scheme import** (`rc_schemes.py`): iTerm2, Windows Terminal and simple JSON schemes; removable.
- Optional `[HH:MM:SS]` timestamps in terminal log files.

## 2.14.0

**New**
- **Firewall page** in the Stats tab (`rc_firewall.py`): ufw rules (add, delete, on/off) and firewalld ports/services over SSH, nftables and
  iptables as read-only text, with lock-out warnings before anything that could cut the SSH connection.
- **Capture page** in the Stats tab (`rc_capture.py`): tcpdump over SSH, shown live in a table or recorded to a `.pcap` for Wireshark,
  with packet and time limits so a capture always ends.

## 2.13.1

**New**
- **A web manual** (`docs/`, also shipped as the `manual` folder next to `RC.exe`): a web page with a menu bar, a contents list with search, pictures of every screen, a video demonstration, a first-five-minutes guide, a walk through each menu, how to add servers and use every feature, shortcuts, where RC keeps its files, and a help / FAQ section. All pictures and the video use sample data.
- **Help > Manual (web page)** opens it (the local copy if present, otherwise the online page).
- The picture and video tools used to make it (`release/capture_manual.py`, `release/demo_env.py`).

**Changed**
- The README's "What's new" section now covers everything since 2.0, and `QUICK START.txt` covers the newer features and the manual.
- Version history note: a short-lived 2.1.0 build published on GitHub in November 2026 contained a one-file settings export (`.rcpack`). It was replaced by the encrypted **Backup and restore** (2.7.0) and is not part of the later versions. The 2.1.0 entry below lists what 2.1.0 means in the current source (split view, broadcast and so on).

## 2.13.0

**New**
- **Folder sync** (`rc_sync.py`, File > Backup > Sync through a folder): servers, snippets, workspaces, identities, proxies, notes and
  trusted host keys stay the same on several PCs through a shared folder. One encrypted file per PC (no conflicted copies), newest
  change per record wins, deletions propagate, optional automatic sync at start and exit.
- **Deep scan: all ports.** In the Network scan, **Scan more ports on this machine…** (and the Ports box when one address is scanned) accepts `all` to try every port 1-65535 on that one machine, with progress and a Stop button that keeps what was found.

**Removed**
- The old stand-alone `netscan.py` script (the built-in scan replaces it).

## 2.12.0

**New**
- **Preferences** (`rc_prefs.py`, Tools > Preferences): confirm before exiting, reopen last session's tabs, server check interval,
  Stats interval, interface size, and a shortcut list.
- **Tab management:** lock a tab, close others / right / left, drag to reorder, **Ctrl+1-9**, **Ctrl+Tab**, **Ctrl+Shift+D**.
- RC remembers the open SSH / Files / Stats / Telnet / Serial tabs on exit (optional) and reopens them on the next start.

**Fixed**
- RC no longer stops at start-up on Python builds whose Tk does not know the key name `ISO_Left_Tab` (Ctrl+Shift+Tab now also uses `Control-Shift-Tab`).
- The background status check ignored a server's proxy, so servers reachable only through a proxy always showed red.

## 2.11.0

**New**
- **Zmodem** (`rc_zmodem.py`): `sz` on the server downloads into a folder you choose, `rz` (or the right-click menu's Upload
  files with Zmodem) uploads files you pick. Cancel button, progress in the status bar, no overwriting, safe file names,
  automatic re-send of damaged blocks. Tested against the real lrzsz `sz` and `rz` as well as against itself.
- **Clear screen (keep scrollback)** in the terminal's right-click menu.

## 2.10.0

**New (file manager)**
- A **right-click menu** with Copy / Cut / Paste here (on the server, copies run on the server itself), New file, Copy path,
  Type path into the SSH terminal, Select all and Show hidden files.
- A **clickable path trail**, **sortable columns**, Ctrl+A / C / X / V.
- Transfer queue **Pause / Resume** and **Retry failed**.

**Fixed**
- The PC pane's Up button on a drive root (C:) went to the current folder on that drive; it now stays at the root.
- Stats no longer hammers a server that is refusing logins: after a failure it waits 2x, 4x, 8x ... the refresh interval (up to a minute)
  and says when it will try again.
- Stats from Glances no longer rebuilds a TLS certificate store on every refresh (it was slow and only needed for https).

## 2.9.0

**New**
- **Glances support for Stats** (`rc_glances.py`): a per-server "Use Glances" setting (Edit > Glances, with port, login, https and a
  Detect / test button) makes the Stats tab read Glances' REST API instead of using SSH, so PCs and servers without an SSH login
  (including Windows) can be monitored. Adds a Containers tab and sensor temperatures. Tried against Glances 4.5.9.
- The Network scan recognises Glances (port 61208) and ticks it when you add the machine as a server.

## 2.8.0

**New**
- **Session recording and playback** (`rc_record.py`): ⏺ Record on every terminal saves an asciicast `.cast` file; Tools > Play a
  terminal recording has play / pause / speed / seek / skip long pauses.
- **Notes** (`rc_notes.py`, Tools > Notes): tagged Markdown notes, optionally attached to a server, searchable, in backups.
- **Import from MobaXterm and Xshell** (`rc_import.py`, File > Import servers).

**Changed**
- A server's right-click menu is shorter: the less-used connections (ping, wake, mail, shares ...) are in a "More" submenu,
  and it has a Notes… entry.

## 2.7.0

**New**
- **Encrypted backup and restore** (`rc_backup.py`, File > Backup): one password-locked file with servers (and their saved
  passwords, re-encrypted for the new PC on restore), snippets, workspaces, identities, proxies and trusted host keys.
- **Network scan** (`rc_netscan.py`, Tools > Network): find machines and their open ports on a network, with names, MACs,
  "already in RC" marks and one-click Add as server.
- **Local servers** (`rc_localsrv.py`, Tools > Network): temporary TFTP (read, optional uploads) and read-only HTTP servers
  with a request log and an auto-stop timer.

## 2.6.0

**New**
- **Telnet and Serial (COM port) terminals** (`rc_transport.py`): add them as a server's More connections; own Telnet
  client with option negotiation and window size, serial with speed / frame / flow-control settings and a port list.
- **Terminal colour schemes** (8), **font** choice, **copy on select**, **right-click paste**, **middle-click paste** and
  **Save output to a file**.

**Changed**
- **The Add / Edit server form is split into tabs** (General, Login, Connections, Network, More connections, After login)
  so it fits small screens; the buttons stay at the top.
- The Connect buttons are on two lines (Connect / More), and Telnet / Serial buttons only show for servers that have one.
- `pyserial` is now a requirement (for Serial).

## 2.5.0

**New**
- **⚡ Quick commands and History panel** on every SSH terminal (`rc_quick.py`): grouped snippets with Run / Paste / Copy,
  searchable history of what you typed (never passwords, full-screen programs or edited lines; switch-off and clear
  available), save a history entry as a quick command. Snippets can have a Group. `history.json`.
- **After-login script** per server: `wait-for-text => what to type` steps run after every connect (with `${user}`,
  `${host}`, `${password}`), stopping with a message if a step does not appear in 10 s.
- **Multi-line paste check:** pasting several lines asks first (and says how many other Broadcast terminals get it).

## 2.4.0

**New**
- **Identities** (`rc_identity.py`, Tools > Credentials): reusable logins (password, key file or agent) that servers
  can share; a server's "Saved login" replaces its own username, password and key. `identities.json`.
- **Proxies** (Tools > Network): SOCKS5 and HTTP CONNECT proxies, with a Test button. SSH, SFTP, tunnels, Stats, snippets,
  key setup and the status dot go through the server's proxy. `proxies.json`.

## 2.3.0

**New**
- **Workspaces:** save a split view (its servers and layout) under a name and reopen it in one click (Session menu, or
  💾 in the split view). `workspaces.json`.
- **Stats: process details and signals** (TERM / HUP / INT / KILL, always confirmed), **Services** tab (systemd: state,
  start / stop / restart / enable / disable, journal log) and **System** tab (OS, CPU, clock difference from this PC,
  interfaces with MAC / speed / addresses, disks).

## 2.2.0

**New**
- **📊 Stats** button (`rc_monitor.py`): live CPU, memory, network, disks, processes and listening ports of a Linux/NAS
  server over one SSH connection, with graphs, sortable/filterable process list and automatic reconnect. Read-only.

## 2.1.0

**New**
- **Split view** (Session menu): several SSH terminals tiled in one tab, with grid / side-by-side / stacked layouts and pane zoom.
- **Broadcast input**: type or paste once and send it to every ticked terminal (split panes and ordinary tabs).
- **Duplicate session**, **Rename tab**, **Duplicate server** and **Export servers to CSV**; **Help > Keyboard shortcuts**.

**Changed**
- Menu bar reorganised: File (server list actions, Import/Export), Connect, Servers, Session, View, Tools (Network and
  Credentials submenus), Help. The tab right-click menu is built per tab.

## 2.0.2

- Built with the current libraries: paramiko 5.1 (SSH/SFTP) and pyte 0.8.2 (terminal emulator). No feature changes.

## 2.0.1

- Help > Quick start guide finds its README and pictures next to the program even when `RC_DATA_DIR` points the data somewhere else (needed for the Scoop install).
- Install with Scoop: `scoop bucket add regteches https://github.com/ronaldgoodchild/scoop-regteches` then `scoop install rc`. Your servers and logins are kept in `scoop\persist
c` and survive updates.

## 2.0

**New**
- **Real SSH terminal** (`rc_terminal.py`): full-screen programs (vim, nano, htop, less, tmux), 256/true colour,
  scrollback, font zoom, function and Alt keys, bracketed paste, and auto-reconnect when a connection drops.
- **Live status dots** (green / amber / red) for every server, checked in the background, and an **Overview**
  home screen with Favorites (★), Recently connected and Not answering.
- **Other addresses** per server (for example a ZeroTier or Tailscale address): the first one that answers is used.
- **Jump servers**: reach a server through another one.
- **⏻ Wake-on-LAN** button.
- **Trusted host keys** manager (Tools menu): see and forget saved server keys.
- **VNC** and **RustDesk** buttons: open your installed VNC viewer or RustDesk straight to the server.
- **Help > Quick start guide** now opens the README as a formatted web page.
- **Pictures and a 1-minute tour video** in the README and the guide page (`screenshots\` folder).
- **MIT license** (`LICENSE`).
- The SSH terminal now opens at the real window size, so the first lines of a login banner no longer wrap narrow.

**Improved**
- Logging to `rc.log` (rotating), with timings for slow steps.
- Settings and `hosts.json` are checked on start; a damaged file is set aside instead of being overwritten.
- Dependency check on start (paramiko version, pyte); clearer messages when something is missing.
- Thread-safety: only the window thread touches the screen; shared lists are locked.
- Unit tests (138), including in-process SSH servers.

**Needs** `pyte` as well as `paramiko` (`pip install -r requirements.txt`). RC is now five program files that stay
together: `rc_manager.py`, `rc_terminal.py`, `rc_files.py`, `rc_tools.py` and `rc_theme.py`.

Your servers, saved logins and settings carry over unchanged: `hosts.json` keeps format version 1, and new
fields are added automatically.

## 1.0

First release: SSH tabs, SFTP/FTP file manager, Remote Desktop, web, PowerShell, tunnels, snippets, quick connect
(Ctrl+K), bulk SSH key setup, import from PuTTY / WinSCP / ssh config, seven colour themes.
