# REGTeches Remote Connection Manager (RC) v2.0

**One window for every server, PC and device you connect to.**
Developed by Ronald Goodchild · REGTeches · Jacksonville, FL

![RC banner](00_hero_facebook.png)

---

## The problem

If you run a home lab, a small business, or manage servers for clients, you know the routine: PuTTY for SSH, WinSCP for files, `mstsc` for Remote Desktop, a browser bookmark folder for web panels, and a spreadsheet to remember which password goes with which IP address.

## The answer

**RC** is one Windows app with your servers on the left and your sessions as tabs on the right. Add a server once, then connect with one click.

![Server list and connection buttons](03_docker_host_connections.png)

### Colour-coded Connect buttons

| Button | What it does |
|---|---|
| **SSH** (green) | Terminal tab inside the window: colours, history, tab-completion, Ctrl+C, copy/paste, search, session logging |
| **Files** (orange) | Two-pane file manager: this PC on the left, the server on the right. SFTP, FTP or FTPS |
| **Remote Desktop** (blue) | Opens Windows Remote Desktop with the address, port and user filled in |
| **Web** (teal) | Opens the server's web panel in your browser. A server can have many (Portainer, Plex, Grafana...) |
| **PowerShell** (indigo) | Remote PowerShell session for Windows servers |
| **SSH window** (slate) | Full-screen programs such as `nano`, `vim`, `htop` |
| **Mail**, **Shares**, **Ping** | Webmail, Windows network shares, four-ping test |
| **✔ Check** (red) | Tests DNS and every service on the server and tells you what's up |

## Feature tour

### 🖥️ SSH terminal tabs
![SSH terminal](04_ssh_terminal.png)
Open as many as you like. Saved commands ("snippets") paste into any terminal with one click.

### 📁 Drag-and-drop file manager
![File manager](05_file_manager.png)
Modelled on the classic commander layout. Drag files between panes, or use F5/F6/F7/F8. A transfer queue with progress bars runs on a separate connection so you can keep browsing. **Synchronize** compares a local and a remote folder, shows a checklist of what would change, and only then applies it. Built-in text editor saves straight back to the server.

### 🔑 One-click keys for the whole lab
![Key setup](08_key_setup.png)
Tick your servers, click once. RC creates an ed25519 key for your PC, logs into each server with its password (asked once, never stored by the key tool), installs the public key (Linux, Synology, Windows OpenSSH, routers) and then proves the key works. From then on there are no more passwords. The key has no passphrase and no expiry. Working from a second PC? Run it there too and each PC gets its own key, so you never copy a private key around.

### 🔀 SSH tunnels
![Tunnels](06_tunnels.png)
Local, remote and SOCKS (dynamic) forwards, started and stopped from one list. Perfect for reaching a database or a web panel that is only listening on `localhost`.

### ▤ Saved commands
![Snippets](07_snippets.png)
Write a command once, then run it on any set of servers and see each answer: disk space, uptime, pending updates, restart a service.

### ⌨️ Quick connect (Ctrl+K)
![Quick connect](09_quick_connect.png)
Type `docker`, `nas web` or `tunnel` and press Enter. Searches servers, connections, tunnels and snippets.

### 🎨 Colour themes
![Themes](13_theme_gallery.png)
Sky Blue, Sunset, Mint Fresh, Royal Dark, Neon Purple, plus Classic Dark and Classic Light. Bold, high-contrast text everywhere; every colour pair was checked for readability.

### Menus for everything
![Servers menu](10_servers_menu.png)
![Tools menu](11_tools_menu.png)

---

## Why people will like it

* **Nothing to install.** One EXE. Unzip and run. Copy the folder to a USB stick and your whole server list goes with you.
* **Private by design.** Your servers, settings and trusted-host list live next to the EXE. RC never phones home and has no accounts.
* **Safe with passwords.** Saved passwords are encrypted for your Windows account, or use keys and save none. New servers show their fingerprint and ask before they are trusted; if a trusted server's identity changes, RC refuses and warns you.
* **Works over VPNs.** Using Tailscale or WireGuard? Same addresses, same keys, from anywhere.
* **Import what you already have.** Pulls servers from PuTTY, WinSCP and your `ssh config`, or from a CSV.

## Requirements

* Windows 10 or 11 (64-bit)
* Remote Desktop and PowerShell buttons use the tools built into Windows
* SSH to a Windows PC needs OpenSSH Server turned on there

## Getting started (2 minutes)

1. Unzip `REGTeches_RC_v2.0.zip`
2. Double-click **RC.exe** (if Windows SmartScreen appears: *More info → Run anyway*)
3. Click **➕ Add**, type a name and the IP/DNS name
4. Pick the server, click **SSH**, **Files**, **Remote Desktop**...
5. Click **🔑 Keys** to retire your passwords

## About the developer

RC is built by **Ronald Goodchild** of **REGTeches** in Jacksonville, FL, as part of a suite of IT tools used to run a real home lab and a real IT-support business: network discovery, NOC dashboards, backup and drive-mapping tools. RC started as the thing he wished existed and got bigger from there.

*Version 2.0 · © 2026 REGTeches · Developed by Ronald Goodchild*
