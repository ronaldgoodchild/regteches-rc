"""Makes the RC tour video (MP4) from the marketing screenshots: title card, one captioned slide per feature,
cross-fades, closing card. Needs ffmpeg on PATH.

    python make_video.py <screenshots_folder> <output.mp4>

The slides are drawn here with Pillow (nothing real is shown: the screenshots come from FAKE demo data)."""
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SRC = os.path.abspath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2])
W, H, FPS, FADE = 1280, 720, 30, 0.7
NAVY_TOP, NAVY_BOTTOM = (11, 24, 56), (20, 44, 110)
RED = (183, 28, 28)

# (picture or None, title, subtitle, seconds)
SLIDES = [
    (None, "REGTeches RC 2.0", "Every server. One window.", 4.0),
    ("01_home.png", "Know what is up at a glance", "Live status dots, favorites and recent servers on the Overview", 5.0),
    ("03_docker_host_connections.png", "Every way in, one click", "SSH, Files, Remote Desktop, VNC, RustDesk, Web, PowerShell and more", 4.5),
    ("04_ssh_terminal.png", "A real SSH terminal", "Full-screen programs, colours, scrollback, font zoom, auto-reconnect", 5.0),
    ("05_file_manager.png", "Drag-and-drop file manager", "SFTP, FTP and FTPS - your PC on the left, the server on the right", 4.5),
    ("06_tunnels.png", "SSH tunnels", "Reach private web pages and databases safely", 4.0),
    ("07_snippets.png", "Saved commands", "Run your favourite commands on any server", 4.0),
    ("08_key_setup.png", "One-click key login", "Set up passwordless SSH for the whole lab", 4.0),
    ("09_quick_connect.png", "Ctrl+K quick connect", "Type a few letters, press Enter", 4.0),
    ("14_help_guide.png", "Built-in guide", "Help opens a formatted guide in your browser", 4.5),
    ("13_theme_gallery.png", "Seven colour themes", "Light, dark, neon - make it yours", 4.5),
    (None, "Free and open source", "MIT license  ·  github.com/ronaldgoodchild/regteches-rc", 5.0),
]


def font(size, bold=True):
    for f in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf" if bold else "arial.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def background():
    img = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        px.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(NAVY_TOP, NAVY_BOTTOM)))
    return img


def centered(d, text, y, fnt, fill):
    w = d.textlength(text, font=fnt)
    d.text(((W - w) / 2, y), text, font=fnt, fill=fill)


def card(title, sub, closing=False):
    img = background()
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 14, H], fill=RED)
    reg, teches = "REG", "Teches"
    f = font(120)
    total = d.textlength(reg, font=f) + d.textlength(teches, font=f)
    x = (W - total) / 2
    d.text((x, 150), reg, font=f, fill="white")
    d.text((x + d.textlength(reg, font=f), 150), teches, font=f, fill=(255, 205, 210))
    centered(d, title if closing else "Remote Connection Manager  2.0", 310, font(54), "white")
    centered(d, sub, 400, font(34, False), (200, 215, 245))
    centered(d, "Developed by Ronald Goodchild  ·  Jacksonville, FL", 640, font(26, False), (150, 170, 215))
    return img


def slide(pic, title, sub):
    img = background()
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 14, H], fill=RED)
    d.text((48, 20), title, font=font(40), fill="white")
    d.text((50, 70), sub, font=font(23, False), fill=(200, 215, 245))
    shot = Image.open(pic).convert("RGB")
    box_w, box_h = W - 120, H - 150 - 36
    scale = min(box_w / shot.width, box_h / shot.height)
    shot = shot.resize((int(shot.width * scale), int(shot.height * scale)), Image.LANCZOS)
    x, y = (W - shot.width) // 2, 118
    shadow = Image.new("RGBA", (shot.width + 60, shot.height + 60), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle([30, 36, shot.width + 30, shot.height + 36], fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    img.paste(shadow, (x - 30, y - 30), shadow)
    img.paste(shot, (x, y))
    d.text((W - 330, H - 34), "REGTeches RC 2.0  ·  free & open source", font=font(18, False), fill=(150, 170, 215))
    return img


def main():
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    work = tempfile.mkdtemp(prefix="rc_video_")
    files = []
    for i, (pic, title, sub, secs) in enumerate(SLIDES):
        if pic:
            path = os.path.join(SRC, pic)
            if not os.path.isfile(path):
                print("skipping missing", pic)
                continue
            img = slide(path, title, sub)
        else:
            img = card(title, sub, closing=i == len(SLIDES) - 1)
        out = os.path.join(work, f"s{i:02d}.png")
        img.save(out)
        files.append((out, secs))
    # one input per slide, cross-faded: each clip is `secs` long and overlaps its neighbour by FADE
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for path, secs in files:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{secs + FADE:.2f}", "-i", path]
    chain, last, offset = [], "[0:v]", 0.0
    for i in range(1, len(files)):
        offset += files[i - 1][1]
        tag = f"[v{i}]"
        chain.append(f"{last}[{i}:v]xfade=transition=fade:duration={FADE}:offset={offset:.2f}{tag}")
        last = tag
    cmd += ["-filter_complex", ";".join(chain), "-map", last, "-c:v", "libx264", "-preset", "slow", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-r", str(FPS), OUT]
    subprocess.run(cmd, check=True)
    shutil.rmtree(work, ignore_errors=True)
    total = sum(s for _, s in files) + FADE
    print(f"video saved: {OUT} ({os.path.getsize(OUT) / 1e6:.1f} MB, about {total:.0f} s)")


if __name__ == "__main__":
    main()
