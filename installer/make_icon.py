"""Generate assets/icon.ico from the same drawing used by the system tray.
Run from repo root before invoking Inno Setup."""
from PIL import Image, ImageDraw
import os, sys

ACCENT = "#3b82f6"

def make_icon(color=ACCENT, size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    
    # Temporary flag background
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    
    red = (217, 30, 24, 255)     # #d91e18
    blue = (0, 51, 160, 255)     # #0033a0
    orange = (242, 168, 21, 255) # #f2a815
    
    # Scale boundaries dynamically for top, middle, and bottom stripes
    y1 = int(22 * size / 64)
    y2 = int(42 * size / 64)
    
    bg_draw.rectangle([0, 0, size, y1], fill=red)
    bg_draw.rectangle([0, y1, size, y2], fill=blue)
    bg_draw.rectangle([0, y2, size, size], fill=orange)
    
    # Circular mask
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    s = size / 64
    mask_draw.ellipse([2*s, 2*s, 62*s, 62*s], fill=255)
    
    img.paste(bg, (0, 0), mask)
    
    # Draw white mic elements on top
    d = ImageDraw.Draw(img)
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
