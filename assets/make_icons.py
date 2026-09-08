#!/usr/bin/env python3
"""Generate the PWA icons. Run only when the artwork changes."""
from PIL import Image, ImageDraw
import pathlib

def icon(size, maskable=False):
    S = size * 4                      # supersample for smooth edges
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = 0 if maskable else int(S * 0.22)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=(17, 19, 22, 255))

    pad = S * (0.28 if maskable else 0.22)   # maskable needs a safe zone
    w = S - 2 * pad
    green = (46, 190, 130, 255)
    # capsule
    cw, ch = w * 0.40, w * 0.58
    cx, cy = S / 2, pad + ch / 2
    d.rounded_rectangle([cx - cw / 2, pad, cx + cw / 2, pad + ch],
                        radius=cw / 2, fill=green)
    # pickup arc
    aw = w * 0.72
    lw = int(w * 0.085)
    top = cy + ch * 0.06
    d.arc([cx - aw / 2, top - aw / 2, cx + aw / 2, top + aw / 2],
          start=0, end=180, fill=green, width=lw)
    # stem
    sy = top + aw / 2
    d.line([cx, sy, cx, sy + w * 0.14], fill=green, width=lw)
    # base
    bw = w * 0.34
    d.line([cx - bw / 2, sy + w * 0.14, cx + bw / 2, sy + w * 0.14],
           fill=green, width=lw)
    return img.resize((size, size), Image.LANCZOS)

here = pathlib.Path(__file__).parent
for n in (192, 512):
    icon(n).save(here / f"icon-{n}.png")
icon(512, maskable=True).save(here / "icon-maskable-512.png")
icon(180).save(here / "apple-touch-icon.png")
print("icons written:", *(p.name for p in sorted(here.glob("*.png"))))
