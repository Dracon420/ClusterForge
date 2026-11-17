"""
Generate the PWA icon set.

Drawn rather than shipped as binaries so the icons can be regenerated after a
palette change, and so nothing here depends on Wildcard's artwork -- the app
icon is ours, the background picture is theirs.

Run:  python scripts\\make_icons.py
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFilter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "static", "icons")

BG_DARK = (13, 12, 10)
BG_STONE = (32, 29, 20)
AMBER = (233, 168, 60)
AMBER_DIM = (168, 114, 31)
TEK = (69, 214, 224)


def _hex_path(cx, cy, r):
    """Flat-topped hexagon -- ARK's tek motif."""
    import math
    pts = []
    for i in range(6):
        angle = math.radians(60 * i - 30)
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return pts


def draw_icon(size: int, maskable: bool = False) -> Image.Image:
    # Work at 4x then downsample: cheap, reliable antialiasing for the
    # diagonal strokes, which look ragged drawn directly at 192px.
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # A maskable icon gets cropped to a circle by Android, so its artwork has
    # to sit inside the middle 80%. A normal icon can use the full square.
    pad = int(s * 0.14) if maskable else int(s * 0.05)
    radius = int(s * 0.22)

    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=0 if maskable else radius,
                        fill=BG_DARK)
    d.rounded_rectangle([pad // 2, pad // 2, s - pad // 2, s - pad // 2],
                        radius=radius, fill=BG_STONE)

    # Faint hex lattice behind the glyph.
    cx, cy = s / 2, s / 2
    for r, alpha in ((s * 0.40, 40), (s * 0.30, 26)):
        d.line(_hex_path(cx, cy, r) + [_hex_path(cx, cy, r)[0]],
               fill=AMBER + (alpha,), width=max(1, s // 220))

    # The mark: a stylised ARK obelisk/chevron. Two strokes, wide stance, so it
    # stays readable at 48px in a task switcher.
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    w = max(2, int(s * 0.055))
    top = (cx, cy - s * 0.235)
    left = (cx - s * 0.215, cy + s * 0.215)
    right = (cx + s * 0.215, cy + s * 0.215)
    gd.line([left, top, right], fill=AMBER, width=w, joint="curve")
    # Crossbar, low, like the ARK logo's stance.
    gd.line([(cx - s * 0.125, cy + s * 0.075), (cx + s * 0.125, cy + s * 0.075)],
            fill=AMBER, width=w)
    # A tek-cyan spark at the apex: the one cool accent against all the amber.
    gd.ellipse([top[0] - w * 0.75, top[1] - w * 0.75,
                top[0] + w * 0.75, top[1] + w * 0.75], fill=TEK)

    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s * 0.018)))
    img.alpha_composite(glow)

    if not maskable:
        d.rounded_rectangle([pad // 2, pad // 2, s - pad // 2, s - pad // 2],
                            radius=radius, outline=AMBER_DIM,
                            width=max(1, int(s * 0.008)))

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    made = []
    for size in (48, 72, 96, 128, 144, 192, 256, 384, 512):
        path = os.path.join(OUT, f"icon-{size}.png")
        draw_icon(size).save(path, "PNG")
        made.append(os.path.basename(path))

    for size in (192, 512):
        path = os.path.join(OUT, f"maskable-{size}.png")
        draw_icon(size, maskable=True).save(path, "PNG")
        made.append(os.path.basename(path))

    # iOS uses this one and does NOT respect transparency or rounding, so it
    # is a plain square that iOS rounds itself.
    apple = os.path.join(OUT, "apple-touch-icon.png")
    draw_icon(180).convert("RGB").save(apple, "PNG")
    made.append(os.path.basename(apple))

    print(f"Wrote {len(made)} icons to {OUT}")
    for name in made:
        print("  " + name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
