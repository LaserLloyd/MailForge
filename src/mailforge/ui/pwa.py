"""Install-as-an-app support (PWA): manifest, icons and a pass-through service worker.

These routes hold no mail and no secrets, so they sit outside the sign-in gate
(a browser fetches the manifest and icons without the session cookie when it
judges installability). They are still behind the Host/Origin guard.

The service worker never caches and never answers a fetch itself: it exists only
so Chrome offers "Install app". Mail data must never be stored by it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse, Response

PWA_DIR = Path(__file__).resolve().parent / "static" / "pwa"
THEME_COLOR = "#0e0e1b"  # the dark theme's ground (--surface-0)
ICONS = {
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}

MANIFEST: dict[str, Any] = {
    "name": "MailForge",
    "short_name": "MailForge",
    "description": "Local email triage and human-approved drafts.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "theme_color": THEME_COLOR,
    "background_color": THEME_COLOR,
    "icons": [
        {"src": "/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/pwa/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
}

SERVICE_WORKER = """\
// MailForge service worker: install support only. It never caches anything and
// never answers a request itself, so no mail data is ever stored here.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
// The handler answers nothing: the browser handles every request exactly as it would without a worker.
self.addEventListener('fetch', () => {});
"""

REGISTER_JS = (
    "if('serviceWorker' in navigator){window.addEventListener('load',function(){"
    "navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(function(){});});}"
)


def head_tags() -> str:
    """Tags for every page's <head>: manifest, theme colour, iOS icon, SW registration."""
    return (
        '<link rel="manifest" href="/manifest.webmanifest">\n'
        f'<meta name="theme-color" content="{THEME_COLOR}">\n'
        '<link rel="apple-touch-icon" href="/pwa/apple-touch-icon.png">\n'
        f"<script>{REGISTER_JS}</script>\n"
    )


def register_routes(app: Any) -> None:
    """Add the PWA routes to ``app`` (idempotent)."""
    if any(getattr(r, "path", None) == "/manifest.webmanifest" for r in app.routes):
        return

    def _manifest() -> Response:
        return Response(
            json.dumps(MANIFEST), media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    def _sw() -> Response:
        # Root scope comes from the URL path; the header makes it explicit.
        return Response(
            SERVICE_WORKER, media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    def _icon(name: str) -> Response:
        ctype = ICONS.get(name)
        if ctype is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return Response((PWA_DIR / name).read_bytes(), media_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})

    app.get("/manifest.webmanifest")(_manifest)
    app.get("/sw.js")(_sw)
    app.get("/pwa/{name}")(_icon)
