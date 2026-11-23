"""Assembles the manual's demo video from what capture_manual.py recorded.

    python make_manual_video.py <capture_folder> <output.mp4>

Title card + the recorded walk-through (frames/) + a captioned slide for each menu + closing card. Needs ffmpeg."""
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

SRC = os.path.abspath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2])
W, H = 1280, 720
RATE = 4                    # recorded frames were grabbed a few times a second: show them at that pace
NAVY_TOP, NAVY_BOTTOM = (11, 24, 56), (20, 44, 110)


def font(size, bold=True):
    for f in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf" if bold else "arial.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def gradient():
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line((0, y, W, y), fill=tuple(int(NAVY_TOP[i] + (NAVY_BOTTOM[i] - NAVY_TOP[i]) * t) for i in range(3)))
    return img


def card(title, sub, small=""):
    img = gradient()
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((W // 2 - 330, 150, W // 2 - 250, 230), 16, fill=(183, 28, 28))
    d.text((W // 2 - 290, 190), "RC", font=font(44), fill="white", anchor="mm")
    d.text((W // 2 - 225, 190), "REGTeches", font=font(40), fill="white", anchor="lm")
    d.text((W // 2, 330), title, font=font(60), fill="white", anchor="mm")
    d.text((W // 2, 410), sub, font=font(30, False), fill=(190, 210, 240), anchor="mm")
    if small:
        d.text((W // 2, 520), small, font=font(24, False), fill=(150, 175, 215), anchor="mm")
    return img


def slide(picture, text):
    pic = Image.open(picture).convert("RGB")
    scale = min(W / pic.width, (H - 70) / pic.height)
    pic = pic.resize((int(pic.width * scale), int(pic.height * scale)), Image.LANCZOS)
    img = Image.new("RGB", (W, H), (13, 27, 62))
    img.paste(pic, ((W - pic.width) // 2, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, H - 70, W, H), fill=(8, 17, 41))
    d.rectangle((0, H - 70, 8, H), fill=(33, 150, 243))
    d.text((28, H - 50), text, font=font(26), fill="white")
    return img


work = tempfile.mkdtemp(prefix="rc_video_")
n = [0]


def add(img, seconds):
    for _ in range(max(1, int(round(seconds * RATE)))):
        img.save(os.path.join(work, "v%05d.jpg" % n[0]), quality=90)
        n[0] += 1


add(card("Every server. One window.", "REGTeches RC 2.13.1 - a short demonstration", "Sample data only: no real servers are shown"), 4)
frames = sorted(f for f in os.listdir(os.path.join(SRC, "frames")) if f.endswith(".jpg"))
for f in frames:
    shutil.copy(os.path.join(SRC, "frames", f), os.path.join(work, "v%05d.jpg" % n[0]))
    n[0] += 1
menus = (("menu_file", "File: add, import, back up and organise your servers"), ("menu_connect", "Connect: every way in to the selected server"),
         ("menu_session", "Session: split view, workspaces, broadcast, tab control"), ("menu_view", "View: Overview, colour themes and terminal looks"),
         ("menu_tools", "Tools: scan, tunnels, proxies, identities, notes, preferences"), ("menu_help", "Help: this manual, the guide and shortcuts"))
for name, text in menus:
    p = os.path.join(SRC, name + ".png")
    if os.path.isfile(p):
        add(slide(p, text), 3.5)
add(card("Free and open source", "MIT licence  ·  Windows", "github.com/ronaldgoodchild/regteches-rc"), 5)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(RATE), "-i", os.path.join(work, "v%05d.jpg"),
                "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-crf", "26", "-preset", "slow", "-movflags", "+faststart", OUT], check=True)
shutil.rmtree(work, ignore_errors=True)
print("saved", OUT, f"({os.path.getsize(OUT) / 1e6:.1f} MB, {n[0] / RATE:.0f} s)")
