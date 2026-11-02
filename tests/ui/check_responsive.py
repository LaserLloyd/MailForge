#!/usr/bin/env python3
"""Responsive layout check for the MailForge web UI (not part of the unit suite).

Drives a RUNNING scratch instance with headless Chromium at phone, tablet,
foldable and desktop sizes and asserts, per viewport and page:

* no horizontal page scroll (scrollWidth <= innerWidth);
* no visible text block narrower than ~80 px that holds more than 3
  characters and wraps onto several lines (the "one letter per line" bug);
* no button overlaps visible text outside itself or another button;
* below 1100 px wide, buttons are at least 40 px tall (touch targets).

Usage (see tests/ui/README.md)::

    python tests/ui/check_responsive.py --base http://127.0.0.1:61777 \
        --key-file <data>/ui_launcher_key --message-id 339 --draft-id 1 \
        [--shots DIR] [--only inbox-open,settings] [--viewports 390x844,...]

Exit code 0 = clean, 1 = violations (listed), 2 = could not run.
Never point it at the live service: use a scratch MAILFORGE_HOME.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

VIEWPORTS = ["390x844", "768x1024", "912x1000", "1180x820", "1920x1080"]

PAGES = {
    "dashboard": "/",
    "inbox": "/mail",
    "inbox-open": "/mail?message_id={message_id}",
    "message": "/mail/{message_id}",
    "drafts": "/drafts",
    "draft-detail": "/detail/{draft_id}",
    "spam": "/spam",
    "compose": "/compose",
    "outbox": "/outbox",
    "templates": "/templates",
    "knowledge": "/references",
    "activity": "/activity",
    "models": "/models",
    "settings": "/settings",
}

JS = r"""
() => {
  const out = [];
  // The action bar is sticky by design (it floats over scrolled content);
  // measure the flow layout so only real collisions are reported.
  document.querySelectorAll('.bb-sticky-actions').forEach(e => e.style.position = 'static');
  const vw = window.innerWidth;
  const se = document.scrollingElement;
  if (se.scrollWidth > vw) out.push({k: 'hscroll', d: `scrollWidth ${se.scrollWidth} > innerWidth ${vw}`});

  const visible = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const offscreenX = (el) => {   // e.g. the closed navigation drawer
    const r = el.getBoundingClientRect();
    return r.right <= 0 || r.left >= vw;
  };
  const clipped = (el) => {   // inside a scroll/overflow-hidden ancestor that cuts it off
    const r = el.getBoundingClientRect();
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (/(hidden|auto|scroll|clip)/.test(cs.overflowX + cs.overflowY)) {
        const pr = p.getBoundingClientRect();
        if (r.right <= pr.left || r.left >= pr.right || r.bottom <= pr.top || r.top >= pr.bottom) return true;
      }
    }
    return false;
  };
  const label = (el) => (el.tagName.toLowerCase() + (el.className && typeof el.className === 'string'
      ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : ''));

  // text nodes -> rects
  const texts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) {
    const s = n.textContent.replace(/\s+/g, ' ').trim();
    if (s.length < 1) continue;
    const el = n.parentElement;
    if (!el || ['SCRIPT', 'STYLE', 'OPTION', 'TEXTAREA'].includes(el.tagName)) continue;
    if (!visible(el) || clipped(el) || offscreenX(el)) continue;
    const range = document.createRange(); range.selectNodeContents(n);
    const rects = [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0);
    if (!rects.length) continue;
    texts.push({n, el, s, rects});
  }

  // 1. narrow multi-line text
  for (const t of texts) {
    const nonSpace = t.s.replace(/\s/g, '').length;
    if (nonSpace <= 3) continue;
    const lines = new Set(t.rects.map(r => Math.round(r.top / 4))).size;
    // width of the box that holds the text (an inline parent: its block ancestor)
    let box = t.el;
    while (box.parentElement && getComputedStyle(box).display.startsWith('inline')) box = box.parentElement;
    const w = box.getBoundingClientRect().width;
    const words = t.s.split(' ').length;   // 2 short words on 2 lines is fine; broken words are not
    if (lines >= 2 && w < 80 && (lines > words || lines >= 4)) out.push({k: 'narrow-text', d: `${label(t.el)} "${t.s.slice(0, 30)}" w=${Math.round(w)} lines=${lines}`});
    else if (lines >= 4 && nonSpace / lines < 3.5) out.push({k: 'sliver-text', d: `${label(t.el)} "${t.s.slice(0, 30)}" ${lines} lines`});
  }

  // 2. buttons
  const btns = [...document.querySelectorAll('.q-btn, button, [role=button]')]
    .filter(b => visible(b) && !clipped(b) && !offscreenX(b));
  const inside = (a, b) => a === b || a.contains(b) || b.contains(a);
  const hit = (r1, r2) => {
    const w = Math.min(r1.right, r2.right) - Math.max(r1.left, r2.left);
    const h = Math.min(r1.bottom, r2.bottom) - Math.max(r1.top, r2.top);
    return w > 3 && h > 3;
  };
  for (const b of btns) {
    const r = b.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight * 3) continue;
    if (vw < 1100 && r.height < 39.5 && !b.closest('.q-checkbox,.q-tab,.q-field,.q-toggle')) {
      out.push({k: 'tap-target', d: `${label(b)} "${(b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 24)}" ${Math.round(r.width)}x${Math.round(r.height)}`});
    }
    for (const t of texts) {
      if (inside(b, t.el)) continue;
      if (t.rects.some(tr => hit(tr, r))) { out.push({k: 'overlap-text', d: `${label(b)} "${(b.innerText || '').trim().slice(0, 20)}" over "${t.s.slice(0, 24)}"`}); break; }
    }
    for (const o of btns) {
      if (o === b || inside(b, o)) continue;
      if (hit(r, o.getBoundingClientRect()) && b.compareDocumentPosition(o) & 4)
        out.push({k: 'overlap-button', d: `${label(b)} "${(b.innerText || '').trim().slice(0, 16)}" x "${(o.innerText || '').trim().slice(0, 16)}"`});
    }
  }
  return out;
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="scratch instance, e.g. http://127.0.0.1:61777")
    ap.add_argument("--key-file", required=True, help="scratch <data>/ui_launcher_key")
    ap.add_argument("--message-id", default="1")
    ap.add_argument("--draft-id", default="1")
    ap.add_argument("--viewports", default=",".join(VIEWPORTS))
    ap.add_argument("--only", default="", help="comma list of page names")
    ap.add_argument("--shots", default="", help="save PNGs into this directory")
    ap.add_argument("--prefix", default="", help="screenshot filename prefix (before/after)")
    ap.add_argument("--chromium", default="", help="path to chrome-headless-shell")
    a = ap.parse_args()
    if "127.0.0.1" not in a.base and "localhost" not in a.base:
        print("refusing: non-loopback base URL", file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright for python is required", file=sys.stderr)
        return 2

    names = [n for n in (a.only.split(",") if a.only else PAGES) if n]
    shots = Path(a.shots) if a.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    key = Path(a.key_file).read_text(encoding="utf-8").strip()
    bad = 0
    with sync_playwright() as pw:
        kw = {"executable_path": a.chromium} if a.chromium else {}
        browser = pw.chromium.launch(**kw)
        auth = browser.new_context()
        pg = auth.new_page()
        pg.goto(f"{a.base}/launch?k={key}")
        pg.wait_for_url(f"{a.base}/", timeout=15000)
        state = auth.storage_state()
        auth.close()
        for vp in a.viewports.split(","):
            w, h = (int(x) for x in vp.split("x"))
            ctx = browser.new_context(viewport={"width": w, "height": h}, storage_state=state,
                                      has_touch=w < 1100, is_mobile=False)
            page = ctx.new_page()
            for name in names:
                url = a.base + PAGES[name].format(message_id=a.message_id, draft_id=a.draft_id)
                page.goto(url, wait_until="networkidle")
                page.wait_for_timeout(700)
                found = page.evaluate(JS)
                if shots:
                    page.screenshot(path=str(shots / f"{a.prefix}{name}-{vp}.png"))
                status = "ok" if not found else f"{len(found)} issue(s)"
                print(f"{vp:>10} {name:<13} {status}")
                seen = set()
                for f in found:
                    if (f["k"], f["d"]) in seen:
                        continue
                    seen.add((f["k"], f["d"]))
                    print(f"             - {f['k']}: {f['d']}")
                bad += len(seen)
            ctx.close()
        browser.close()
    print("RESULT:", "clean" if not bad else f"{bad} violation(s)")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
