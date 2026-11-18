# REGTeches RC - Version history

Newest first. The version number lives in one place, `APP_VERSION` in `rc_manager.py`; the window title, status bar,
About box, guide page, log file, `RC.exe` file properties and the release file names all follow it.

## 2.0.2

- Built with the current libraries: paramiko 5.1 (SSH/SFTP) and pyte 0.8.2 (terminal emulator). No feature changes.

## 2.0.1

- Help > Quick start guide finds its README and pictures next to the program even when `RC_DATA_DIR` points the data somewhere else (needed for the Scoop install).
- Install with Scoop: `scoop bucket add regteches https://github.com/ronaldgoodchild/scoop-regteches` then `scoop install rc`. Your servers and logins are kept in `scoop\persistc` and survive updates.

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
