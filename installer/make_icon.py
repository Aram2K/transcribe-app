"""Generate assets/icon.ico from the same drawing used by the system tray.
Run from repo root before invoking Inno Setup."""
from PIL import Image, ImageDraw
import os, sys

ACCENT = "#3b82f6"

def make_icon(color=ACCENT, size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    s = size / 64
    d.ellipse([2*s, 2*s, 62*s, 62*s], fill=(r, g, b))
    d.rectangle([24*s, 12*s, 40*s, 40*s], fill="white")
    d.ellipse([18*s, 32*s, 46*s, 52*s],   fill="white")
    d.rectangle([30*s, 50*s, 34*s, 60*s], fill="white")
    d.rectangle([22*s, 58*s, 42*s, 62*s], fill="white")
    return img

if __name__ == "__main__":
    os.makedirs("assets", exist_ok=True)
    out = "assets/icon.ico"
    img = make_icon(ACCENT, 256)
    img.save(out, format="ICO",
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"Wrote {out}")
