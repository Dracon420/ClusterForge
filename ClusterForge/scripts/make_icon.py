"""Generate static/clusterforge.ico.

Deliberately simple: a dark tile with a green pulse line and a status dot. It
has to stay legible at 16px in the taskbar, which rules out anything detailed.
"""

import os
import sys

from PIL import Image, ImageDraw

OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "clusterforge.ico"
)

BG = (27, 27, 31, 255)
EDGE = (58, 58, 68, 255)
GREEN = (78, 201, 78, 255)
CYAN = (60, 200, 255, 255)


def draw(size: int) -> Image.Image:
    s = size * 4  # supersample, then downscale for clean edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    r = int(s * 0.18)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=BG, outline=EDGE,
                        width=max(1, s // 64))

    # Pulse / uptime line across the middle.
    pad = s * 0.17
    mid = s * 0.56
    amp = s * 0.17
    pts = [
        (pad,             mid),
        (pad + s * 0.14,  mid),
        (pad + s * 0.22,  mid - amp),
        (pad + s * 0.30,  mid + amp * 0.55),
        (pad + s * 0.40,  mid - amp * 1.25),
        (pad + s * 0.50,  mid),
        (s - pad,         mid),
    ]
    d.line(pts, fill=GREEN, width=max(2, int(s * 0.055)), joint="curve")

    # Status dot, top-right, matching the online indicator in the UI.
    dr = s * 0.10
    cx, cy = s - pad * 0.85, pad * 0.85
    d.ellipse([cx - dr, cy - dr, cx + dr, cy + dr], fill=CYAN)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [draw(n) for n in sizes]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    images[-1].save(OUT, format="ICO", sizes=[(n, n) for n in sizes])
    print(f"wrote {OUT} ({', '.join(str(n) for n in sizes)})")

    png = OUT.replace(".ico", ".png")
    images[-1].save(png)
    print(f"wrote {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
