"""Render the arc-reactor app icon (assets/jarvis.png + assets/jarvis.ico) with Pillow.

    python assets/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
SIZE = 1024
CYAN = (42, 212, 255)


def ring(draw: ImageDraw.ImageDraw, r: float, width: int, color, c: float = SIZE / 2) -> None:
    draw.ellipse((c - r, c - r, c + r, c + r), outline=color, width=width)


def render() -> Image.Image:
    c = SIZE / 2
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # dark disc with a soft rim
    disc = ImageDraw.Draw(base)
    disc.ellipse((24, 24, SIZE - 24, SIZE - 24), fill=(3, 12, 22, 255), outline=(*CYAN, 160), width=10)

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    for r, w, a in ((430, 10, 200), (360, 6, 150), (250, 8, 230), (170, 5, 200)):
        ring(g, r, w, (*CYAN, a))
    # tick ring
    for i in range(60):
        ang = i / 60 * math.tau
        r0, r1 = 395, 395 - (34 if i % 5 == 0 else 16)
        g.line((c + math.cos(ang) * r0, c + math.sin(ang) * r0, c + math.cos(ang) * r1, c + math.sin(ang) * r1),
               fill=(*CYAN, 220 if i % 5 == 0 else 120), width=6 if i % 5 == 0 else 4)
    # coils
    for i in range(10):
        a = i / 10 * math.tau - math.pi / 2
        pts = []
        for rr, half in ((330, 0.17), (190, 0.2)):
            for sgn in ((-1, 1) if rr == 330 else (1, -1)):
                pts.append((c + math.cos(a + sgn * half) * rr, c + math.sin(a + sgn * half) * rr))
        g.polygon(pts, fill=(*CYAN, 70), outline=(*CYAN, 230))
    # triangle
    tri = [(c + math.cos(-math.pi / 2 + k * math.tau / 3) * 150, c + math.sin(-math.pi / 2 + k * math.tau / 3) * 150) for k in range(3)]
    g.polygon(tri, outline=(235, 252, 255, 255), width=12)

    blurred = glow.filter(ImageFilter.GaussianBlur(14))
    base = Image.alpha_composite(base, blurred)
    base = Image.alpha_composite(base, glow)

    core = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    cd = ImageDraw.Draw(core)
    for r, col in ((150, (*CYAN, 90)), (110, (120, 230, 255, 170)), (78, (220, 250, 255, 240)), (52, (255, 255, 255, 255))):
        cd.ellipse((c - r, c - r, c + r, c + r), fill=col)
    base = Image.alpha_composite(base, core.filter(ImageFilter.GaussianBlur(10)))
    return base


def main() -> None:
    img = render()
    img.resize((512, 512), Image.LANCZOS).save(HERE / "jarvis.png")
    img.save(HERE / "jarvis.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("wrote", HERE / "jarvis.png", "and", HERE / "jarvis.ico")


if __name__ == "__main__":
    main()
