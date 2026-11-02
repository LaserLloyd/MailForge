"""Install-as-an-app routes: content types, a valid manifest, no auth, no caching of mail."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mailforge.ui import app as ui_app
from mailforge.ui import pwa

PORT = 45871  # shared with test_ui_pin/test_healthz: the Host guard installs once per process
HOST = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def client():
    # The shared NiceGUI app may already carry another test's guard (other port);
    # exercise the routes on the app directly and the guard via a dedicated test.
    pwa.register_routes(ui_app.nicegui_app)
    return TestClient(ui_app.nicegui_app, base_url=HOST, follow_redirects=False)


def test_manifest_is_valid_and_installable(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.text)
    assert (m["name"], m["short_name"]) == ("MailForge", "MailForge")
    assert (m["start_url"], m["scope"], m["display"]) == ("/", "/", "standalone")
    assert m["theme_color"] == m["background_color"] == "#0e0e1b"
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in m["icons"]:  # every declared icon is actually served
        got = client.get(icon["src"])
        assert got.status_code == 200 and got.headers["content-type"] == icon["type"]
        if icon["type"] == "image/png":
            assert got.content[:8] == b"\x89PNG\r\n\x1a\n"
        else:
            assert b"<svg" in got.content[:200] and b"MailForge" in got.content


def test_service_worker_is_javascript_and_never_caches(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers["service-worker-allowed"] == "/"
    for forbidden in ("caches.", "cache.put", "cache.add", "respondWith", "IndexedDB"):
        assert forbidden not in r.text


def test_apple_touch_icon_and_unknown_icon(client):
    assert client.get("/pwa/apple-touch-icon.png").headers["content-type"] == "image/png"
    assert client.get("/pwa/nope.png").status_code == 404


def test_routes_need_no_session_cookie(client):
    # No cookie, no launcher token: still served (they hold no data).
    for path in ("/manifest.webmanifest", "/sw.js", "/pwa/icon-192.png"):
        assert client.get(path).status_code == 200


def test_head_tags_link_the_manifest_and_register_the_worker():
    tags = pwa.head_tags()
    assert 'rel="manifest" href="/manifest.webmanifest"' in tags
    assert 'name="theme-color"' in tags and 'rel="apple-touch-icon"' in tags
    assert "serviceWorker.register('/sw.js'" in tags


def test_routes_stay_behind_the_host_guard(monkeypatch):
    # A fresh app (the shared one may already be serving, which freezes its middleware).
    from fastapi import FastAPI

    fresh = FastAPI()
    monkeypatch.setattr(ui_app, "nicegui_app", fresh)
    ui_app._install_host_guard(PORT)
    pwa.register_routes(fresh)
    good = TestClient(fresh, base_url=HOST)
    bad = TestClient(fresh, base_url="http://evil.example")
    assert good.get("/manifest.webmanifest").status_code == 200
    assert bad.get("/manifest.webmanifest").status_code == 403
    assert good.get("/sw.js", headers={"Origin": "http://evil.example"}).status_code == 403


def test_master_mark_is_the_favicon_and_launcher_icon():
    from mailforge.service import desktop
    from mailforge.ui import pwa

    assert 'rel="icon" type="image/svg+xml" href="/pwa/mailforge.svg"' in pwa.head_tags()
    assert desktop._ICON_SVG == (pwa.PWA_DIR / "mailforge.svg").read_text(encoding="utf-8")
