"""Capture the documentation screenshots from a running ``emailforge demo``.

Usage:
    emailforge demo --no-browser --port 8899      # in another terminal
    uv run --with playwright python scripts/demo_screenshots.py \
        "http://127.0.0.1:8899/?token=..." out_dir

Only ever point this at the DEMO (fictional data). Needs Playwright's Chromium
(``PLAYWRIGHT_BROWSERS_PATH`` if your browsers live outside the default cache).
Writes dashboard / inbox-refresh / message-reader / compose-reply / ai-review PNGs.
"""

from __future__ import annotations

import asyncio
import re
import sys
from urllib.parse import urlsplit


async def main(url: str, out: str) -> None:
    from playwright.async_api import async_playwright

    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}/"
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto(url)
        await page.wait_for_selector(".bb-sync", timeout=20000)
        await page.wait_for_timeout(2500)
        await page.screenshot(path=f"{out}/dashboard.png")

        await page.goto(base + "mail")
        await page.wait_for_selector(".bb-row", timeout=15000)
        await page.wait_for_timeout(1500)
        boxes = page.locator(".bb-row .q-checkbox")
        await boxes.nth(1).click()
        await boxes.nth(3).click(modifiers=["Shift"])
        await page.mouse.move(900, 600)
        for _ in range(40):
            if "checked" in (await page.inner_text(".bb-sync-text")):
                break
            await page.wait_for_timeout(250)
        await page.wait_for_timeout(400)
        await page.screenshot(path=f"{out}/inbox-refresh.png")

        rows = page.locator(".bb-row")
        target = 0
        for i in range(await rows.count()):
            if await rows.nth(i).locator("i:text('attach_file')").count():
                target = i
                break
        await rows.nth(target).locator(".bb-clip-1").first.click()
        await page.wait_for_url(re.compile(r"message_id=\d+"), timeout=10000)
        await page.wait_for_timeout(1800)
        await page.screenshot(path=f"{out}/message-reader.png")

        await page.click("button:has-text('Reply')")
        await page.wait_for_url(re.compile("/compose"), timeout=10000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path=f"{out}/compose-reply.png")

        await page.goto(base + "detail/1")
        await page.wait_for_timeout(2000)
        await page.screenshot(path=f"{out}/ai-review.png")
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
