"""Builds the REGTeches RC distribution: RC.exe + support files + a zip.

    python build_release.py

Output (next to this script):  dist_pkg\\REGTeches_RC_v<version>\\   and   REGTeches_RC_v<version>.zip
Never copies hosts.json / known_hosts / settings.json / keys: the zip contains no personal data."""
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
sys.path.insert(0, SRC)
os.environ["RC_DATA_DIR"] = os.path.join(HERE, "_tmp_data")   # importing rc_manager must not touch the real data folder
import rc_manager  # noqa: E402

VER = rc_manager.APP_VERSION
NAME = f"REGTeches_RC_v{VER}"
import tempfile
WORK = os.path.join(tempfile.gettempdir(), "rc_build_work")   # outside OneDrive: its sync locks build folders
shutil.rmtree(WORK, ignore_errors=True)
PKG = os.path.join(HERE, "dist_pkg", NAME)
ZIP = os.path.join(HERE, NAME + ".zip")
MEDIA = os.environ.get("RC_MEDIA", "")          # optional folder of screenshots to include

# ─── icon ───────────────────────────────────────────────────────────────────
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

os.makedirs(WORK, exist_ok=True)
ico = os.path.join(WORK, "rc.ico")
big = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(big)
d.rounded_rectangle((8, 8, 248, 248), radius=48, fill=(183, 28, 28, 255))
d.rounded_rectangle((8, 8, 248, 128), radius=48, fill=(211, 47, 47, 255))
try:
    f = ImageFont.truetype("segoeuib.ttf", 112)
except OSError:
    f = ImageFont.load_default()
d.text((128, 118), "RC", fill="white", font=f, anchor="mm")
d.rectangle((52, 196, 204, 206), fill=(255, 205, 210, 255))
big.save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])

# ─── version resource (shows in the exe's Properties > Details) ─────────────
v = tuple(int(x) for x in (VER.split(".") + ["0", "0", "0"])[:4])
version_txt = os.path.join(WORK, "version.txt")
with open(version_txt, "w", encoding="utf-8") as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'REGTeches'),
    StringStruct('FileDescription', 'REGTeches Remote Connection Manager'),
    StringStruct('FileVersion', '{VER}'),
    StringStruct('InternalName', 'RC'),
    StringStruct('LegalCopyright', 'Copyright (c) 2026 REGTeches - Developed by Ronald Goodchild'),
    StringStruct('OriginalFilename', 'RC.exe'),
    StringStruct('ProductName', 'REGTeches RC'),
    StringStruct('ProductVersion', '{VER}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")

# ─── compile ────────────────────────────────────────────────────────────────
shutil.rmtree(os.path.join(WORK, "dist"), ignore_errors=True)
cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--noupx",
       "--name", "RC", "--icon", ico, "--version-file", version_txt,
       "--distpath", os.path.join(WORK, "dist"), "--workpath", os.path.join(WORK, "work"), "--specpath", WORK,
       "--paths", SRC,
       "--hidden-import", "rc_tools", "--hidden-import", "rc_files", "--hidden-import", "rc_theme",
       "--hidden-import", "rc_terminal", "--hidden-import", "rc_monitor", "--hidden-import", "rc_identity", "--hidden-import", "rc_quick", "--hidden-import", "rc_transport", "--hidden-import", "rc_backup", "--hidden-import", "rc_netscan", "--hidden-import", "rc_localsrv", "--hidden-import", "rc_import", "--hidden-import", "rc_record", "--hidden-import", "rc_notes", "--hidden-import", "rc_glances", "--hidden-import", "rc_zmodem", "--hidden-import", "rc_prefs", "--hidden-import", "rc_sync", "--hidden-import", "rc_firewall", "--hidden-import", "rc_capture", "--hidden-import", "rc_totp", "--hidden-import", "rc_schemes", "--hidden-import", "rc_suggest", "--hidden-import", "rc_docker", "--hidden-import", "rc_cloud", "--hidden-import", "rc_hardware", "--hidden-import", "serial", "--hidden-import", "serial.tools.list_ports", "--hidden-import", "serial.tools.list_ports_windows", "--hidden-import", "pyte", "--hidden-import", "wcwidth",
       "--hidden-import", "paramiko", "--hidden-import", "cryptography", "--hidden-import", "PIL",
       os.path.join(SRC, "rc_manager.py")]
print(" ".join(cmd))
subprocess.run(cmd, check=True)
exe = os.path.join(WORK, "dist", "RC.exe")
assert os.path.isfile(exe), "build failed"

# ─── assemble the support files ─────────────────────────────────────────────
import time
for _try in range(8):   # OneDrive may briefly lock files it is syncing
    shutil.rmtree(os.path.dirname(PKG), ignore_errors=True)
    if not os.path.exists(os.path.dirname(PKG)):
        break
    time.sleep(2)
os.makedirs(PKG, exist_ok=True)
shutil.copy(exe, os.path.join(PKG, "RC.exe"))
shutil.copy(os.path.join(SRC, "README.md"), os.path.join(PKG, "README.md"))
shutil.copy(os.path.join(SRC, "CHANGELOG.md"), os.path.join(PKG, "CHANGELOG.md"))
shutil.copy(os.path.join(SRC, "LICENSE"), os.path.join(PKG, "LICENSE"))
for fn in ("RC-Scoop.cmd", "RC-Scoop.vbs"):   # launchers used by the Scoop bucket (keep data in scoop\persist\rc)
    shutil.copy(os.path.join(HERE, "scoop", fn), os.path.join(PKG, fn))
with open(os.path.join(HERE, "QUICK_START.txt"), encoding="utf-8") as f:
    quick = f.read().replace("{VERSION}", VER)
with open(os.path.join(PKG, "QUICK START.txt"), "w", encoding="utf-8") as f:
    f.write(quick)
with open(os.path.join(PKG, "requirements-source.txt"), "w") as f:
    f.write("# Only needed to run the source code instead of RC.exe\nparamiko>=3.4\npyte>=0.8\ncryptography\npyserial>=3.5\n")
src_dir = os.path.join(PKG, "source")
os.makedirs(src_dir, exist_ok=True)
for fn in ("rc_manager.py", "rc_files.py", "rc_tools.py", "rc_theme.py", "rc_terminal.py", "rc_monitor.py", "rc_identity.py", "rc_quick.py", "rc_transport.py", "rc_backup.py", "rc_netscan.py", "rc_localsrv.py", "rc_import.py", "rc_record.py", "rc_notes.py", "rc_glances.py", "rc_zmodem.py", "rc_prefs.py", "rc_sync.py", "rc_firewall.py", "rc_capture.py", "rc_totp.py", "rc_schemes.py", "rc_suggest.py", "rc_docker.py", "rc_cloud.py", "rc_hardware.py", "RC.bat"):
    shutil.copy(os.path.join(SRC, fn), src_dir)
if MEDIA and os.path.isdir(MEDIA):
    shots = os.path.join(PKG, "screenshots")
    os.makedirs(shots, exist_ok=True)
    for fn in sorted(os.listdir(MEDIA)):
        if fn.lower().endswith((".png", ".gif", ".mp4")) and not fn.startswith("theme_"):
            shutil.copy(os.path.join(MEDIA, fn), shots)

# ─── safety: nothing personal may be in the package ─────────────────────────
banned = ("hosts.json", "known_hosts", "settings.json", "snippets.json")
for root_, _, files in os.walk(PKG):
    for fn in files:
        assert fn not in banned and not fn.endswith((".pem", ".ppk")) and "id_ed25519" not in fn, f"personal file in package: {fn}"
for root_, _, files in os.walk(PKG):
    for fn in files:
        if fn.endswith((".py", ".md", ".txt")):
            text = open(os.path.join(root_, fn), encoding="utf-8", errors="ignore").read()
            for pat in (r"192\.168\.8\.\d+", r"ronaldgoodchild(?!/(regteches-rc|scoop-regteches)\b)", r"tail4b8cdb"):  # RC's own public repo and bucket are fine; any other repo name is not
                assert not re.search(pat, text), f"private detail {pat} found in {fn}"

# ─── zip ────────────────────────────────────────────────────────────────────
if os.path.isfile(ZIP):
    os.remove(ZIP)
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root_, _, files in os.walk(PKG):
        for fn in files:
            full = os.path.join(root_, fn)
            z.write(full, os.path.join(NAME, os.path.relpath(full, PKG)))
print("\nBuilt:", PKG)
print("Zip:  ", ZIP, f"({os.path.getsize(ZIP) / 1e6:.1f} MB)")
shutil.rmtree(os.path.join(HERE, "_tmp_data"), ignore_errors=True)
