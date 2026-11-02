"""Re-render the PWA PNG icons in ui/static/pwa/ from the master mark mailforge.svg.

    uv run --with playwright python scripts/make_pwa_icons.py [path-to-chromium]

The SVG is the source of truth (DisPatch-family mark: ivory envelope outline on a
deep navy tile, "MF" in brass). The PNGs are committed, so nothing at runtime
needs Playwright. With no argument it uses Playwright's bundled Chromium.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "mailforge" / "ui" / "static" / "pwa"
MARK = (OUT / "mailforge.svg").read_text(encoding="utf-8")


def full_bleed(svg: str, scale: float) -> str:
    """Square, edge-to-edge tile (no rounded corners) with the artwork scaled to
    ``scale`` of the canvas: maskable icons keep content inside the 80% safe zone,
    and iOS rounds its own corners."""
    body = svg.split("</defs>", 1)[1].rsplit("</svg>", 1)[0]
    body = re.sub(r'<rect width="64" height="64" rx="14" fill="url\(#mf-tile\)"/>', "", body)
    off = 32 * (1 - scale)
    head = svg.split("</defs>", 1)[0] + "</defs>"
    return (f'{head}<rect width="64" height="64" fill="url(#mf-tile)"/>'
            f'<g transform="translate({off:.3f} {off:.3f}) scale({scale})">{body}</g></svg>')


TARGETS = [
    ("icon-192.png", 192, MARK),
    ("icon-512.png", 512, MARK),
    ("icon-maskable-512.png", 512, full_bleed(MARK, 0.78)),
    ("apple-touch-icon.png", 180, full_bleed(MARK, 0.9)),
]


async def main(executable: str | None) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable) if executable \
            else await p.chromium.launch()
        for name, size, svg in TARGETS:
            page = await browser.new_page(viewport={"width": size, "height": size})
            sized = svg.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
            await page.set_content(f"<html><body style='margin:0'>{sized}</body></html>")
            await page.screenshot(path=str(OUT / name), omit_background=True)
            await page.close()
            print("wrote", name)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
