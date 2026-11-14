"""Makes the 1200x630 social-media banner from a screenshot.   python make_hero.py <media_folder>"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter

M = os.path.abspath(sys.argv[1])


def font(size, bold=True):
    for f in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


W, H = 1200, 630
hero = Image.new("RGB", (W, H))
d = ImageDraw.Draw(hero)
for y in range(H):
    t = y / H
    d.line([(0, y), (W, y)], fill=(int(190 - 100 * t), int(30 - 14 * t), int(38 - 8 * t)))
d.text((52, 36), "REG", fill="white", font=font(88))
d.text((52 + d.textlength("REG", font=font(88)), 36), "Teches", fill=(255, 205, 210), font=font(88))
d.text((54, 142), "Remote Connection Manager", fill="white", font=font(35))
y = 215
for line in ("Full-screen SSH terminal", "Live status + Overview", "Files, RDP, VNC, RustDesk", "Tunnels, jump servers, Wake-on-LAN",
             "One-click SSH key setup"):
    d.text((58, y), "•  " + line, fill=(255, 235, 235), font=font(25, False))
    y += 40
d.text((54, 566), "Developed by Ronald Goodchild", fill="white", font=font(28))

shot = Image.open(os.path.join(M, "04_ssh_terminal.png")).convert("RGB")
sw = 620
shot = shot.resize((sw, int(shot.height * sw / shot.width)), Image.LANCZOS)
shadow = Image.new("RGBA", (sw + 60, shot.height + 60), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rectangle((30, 30, sw + 30, shot.height + 30), fill=(0, 0, 0, 150))
shadow = shadow.filter(ImageFilter.GaussianBlur(14))
x, ytop = 540, 70
hero.paste(shadow, (x - 30, ytop - 30), shadow)
hero.paste(shot.crop((0, 0, sw, min(shot.height, H - ytop - 30))), (x, ytop))
hero.save(os.path.join(M, "00_hero_facebook.png"))
print("hero saved")
