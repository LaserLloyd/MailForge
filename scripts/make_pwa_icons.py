"""Regenerate the PWA icons in ui/static/pwa/ (gold envelope on the dark ground).

    uv run python scripts/make_pwa_icons.py

Needs Pillow (dev only); the PNGs are committed, so nothing at runtime does.
"""

from pathlib import Path

from PIL import Image, ImageDraw

BG, GOLD, INK = (14, 14, 27), (251, 191, 36), (14, 14, 27)
OUT = Path(__file__).resolve().parents[1] / "src" / "mailforge" / "ui" / "static" / "pwa"


def draw(size: int, safe: float) -> Image.Image:
    """``safe`` = fraction of the canvas the envelope may span (maskable keeps it inside 80%)."""
    k = 4  # supersample
    n = size * k
    im = Image.new("RGB", (n, n), BG)
    d = ImageDraw.Draw(im)
    w = n * safe
    h = w * 0.68
    x0, y0 = (n - w) / 2, (n - h) / 2
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=w * 0.07, fill=GOLD)
    lw = max(2, int(w * 0.05))
    d.line((x0 + w * 0.04, y0 + h * 0.10, n / 2, y0 + h * 0.60, x0 + w * 0.96, y0 + h * 0.10),
           fill=INK, width=lw, joint="curve")
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192, 0.62).save(OUT / "icon-192.png")
    draw(512, 0.62).save(OUT / "icon-512.png")
    draw(512, 0.46).save(OUT / "icon-maskable-512.png")
    draw(180, 0.62).save(OUT / "apple-touch-icon.png")
