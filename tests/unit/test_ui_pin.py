"""PIN/password sign-in for the web UI: hashing, throttle, cookie, CLI, routes."""

from __future__ import annotations

import json
import stat

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from mailforge import cli
from mailforge.config import Settings
from mailforge.db.store import open_store
from mailforge.paths import runtime_file
from mailforge.ui import app as ui_app
from mailforge.ui import pin_auth


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILFORGE_HOME", str(tmp_path / "home"))
    pin_auth.reset_throttle()
    yield
    pin_auth.reset_throttle()


# --- hashing / storage ------------------------------------------------------ #
def test_default_iterations_not_weakened():
    assert pin_auth.PBKDF2_ITERATIONS >= 200_000


@pytest.mark.parametrize("bad", ["", "12345", "abcdefg", "1234567a ", " abcdefgh", "x" * 200, "12 456"])
def test_weak_secrets_refused(bad):
    assert pin_auth.check_strength(bad)


@pytest.mark.parametrize("good", ["123456", "0000000000", "correct horse", "abcdefgh"])
def test_acceptable_secrets(good):
    assert pin_auth.check_strength(good) is None


def test_set_stores_only_a_salted_hash_0600():
    assert not pin_auth.pin_is_set()
    assert pin_auth.set_secret("482913") == "pin"
    f = runtime_file("ui_pin")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    raw = f.read_text(encoding="utf-8")
    assert "482913" not in raw
    rec = json.loads(raw)
    assert rec["alg"] == "pbkdf2-sha256" and len(rec["salt"]) == 32 and rec["iterations"] >= 200_000
    assert pin_auth.verify("482913") and not pin_auth.verify("482914") and not pin_auth.verify("")


def test_salt_differs_per_set():
    pin_auth.set_secret("482913")
    a = json.loads(runtime_file("ui_pin").read_text(encoding="utf-8"))
    pin_auth.set_secret("482913")
    b = json.loads(runtime_file("ui_pin").read_text(encoding="utf-8"))
    assert a["salt"] != b["salt"] and a["hash"] != b["hash"]


def test_set_weak_raises_and_changes_nothing():
    with pytest.raises(ValueError):
        pin_auth.set_secret("123")
    assert not pin_auth.pin_is_set()


def test_corrupt_file_fails_closed():
    runtime_file("ui_pin").write_text("not json", encoding="utf-8")
    assert pin_auth.pin_is_set()
    assert not pin_auth.verify("anything1")


# --- throttle --------------------------------------------------------------- #
def test_device_lockout_after_five_then_refuses_before_verifying():
    pin_auth.set_secret("482913")
    for _ in range(5):
        assert pin_auth.attempt("000000", "dev1", "203.0.113.1")[0] is False
    ok, wait = pin_auth.attempt("482913", "dev1", "203.0.113.1")  # right PIN, but locked
    assert not ok and wait > 0


def test_lockout_grows():
    pin_auth.set_secret("482913")
    for _ in range(5):
        pin_auth.attempt("000000", "d", "203.0.113.2")
    w1 = pin_auth.wait_seconds("d", "203.0.113.2")
    # force expiry of the lock, fail again: the cooldown doubles
    for b in pin_auth._buckets.values():
        b[1] = 0.0
    pin_auth.attempt("000000", "d", "203.0.113.2")
    w2 = pin_auth.wait_seconds("d", "203.0.113.2")
    assert 4 < w1 <= 5.1 and 9 < w2 <= 10.1


def test_other_device_and_address_unaffected():
    pin_auth.set_secret("482913")
    for _ in range(5):
        pin_auth.attempt("000000", "bad", "203.0.113.3")
    assert pin_auth.attempt("482913", "good", "203.0.113.4")[0] is True


def test_ip_bucket_locks_rotating_devices():
    pin_auth.set_secret("482913")
    for i in range(15):
        pin_auth.attempt("000000", f"dev{i}", "203.0.113.5")
    assert pin_auth.attempt("482913", "fresh", "203.0.113.5")[0] is False


def test_global_ceiling_locks_everyone():
    pin_auth.set_secret("482913")
    for i in range(60):
        pin_auth.attempt("000000", f"d{i}", f"203.0.114.{i // 250}.{i % 250}")
    ok, wait = pin_auth.attempt("482913", "innocent", "198.51.100.1")
    assert not ok and wait > 0


def test_success_clears_bucket():
    pin_auth.set_secret("482913")
    for _ in range(3):
        pin_auth.attempt("000000", "d", "203.0.113.6")
    assert pin_auth.attempt("482913", "d", "203.0.113.6")[0]
    assert pin_auth.wait_seconds("d", "203.0.113.6") == 0


# --- client address --------------------------------------------------------- #
def test_xff_trusted_only_from_loopback():
    assert pin_auth.client_ip("127.0.0.1", "198.51.100.27") == "198.51.100.27"
    assert pin_auth.client_ip("127.0.0.1", "6.6.6.6, 198.51.100.27") == "198.51.100.27"
    assert pin_auth.client_ip("198.51.100.9", "6.6.6.6") == "198.51.100.9"
    assert pin_auth.client_ip("::1", "198.51.100.27") == "198.51.100.27"
    assert pin_auth.client_ip("127.0.0.1", None) == "127.0.0.1"


def test_ipv6_counts_by_slash64():
    a = pin_auth.client_ip("2001:db8:1:2::5", None)
    b = pin_auth.client_ip("2001:db8:1:2:ffff::9", None)
    assert a == b == "2001:db8:1:2::/64"


# --- cookie ----------------------------------------------------------------- #
def test_cookie_roundtrip_expiry_and_pin_change():
    pin_auth.set_secret("482913")
    c = pin_auth.make_cookie("sek", remember=False, now=1000)
    assert pin_auth.cookie_valid("sek", c, now=1000 + 60)
    assert not pin_auth.cookie_valid("sek", c, now=1000 + pin_auth.SESSION_SECONDS + 1)
    r = pin_auth.make_cookie("sek", remember=True, now=1000)
    assert pin_auth.cookie_valid("sek", r, now=1000 + 29 * 86400)
    assert not pin_auth.cookie_valid("sek", r, now=1000 + 31 * 86400)
    assert not pin_auth.cookie_valid("other", c, now=1001)
    assert not pin_auth.cookie_valid("sek", c[:-2] + "00", now=1001)
    pin_auth.set_secret("999999")  # changing the PIN signs everyone out
    assert not pin_auth.cookie_valid("sek", r, now=1001)
    pin_auth.clear_secret()
    assert not pin_auth.cookie_valid("sek", pin_auth.make_cookie("sek", True), now=None)


@pytest.mark.parametrize("t", ["//evil.com", "https://evil.com", "/\\evil", "evil", "/signin?x=1", ""])
def test_next_is_same_site_only(t):
    assert pin_auth.safe_next(t) == "/"


def test_next_keeps_local_paths():
    assert pin_auth.safe_next("/mail?q=a") == "/mail?q=a"


# --- CLI -------------------------------------------------------------------- #
def test_cli_set_status_clear():
    r = CliRunner()
    assert "not set" in r.invoke(cli.app, ["ui-pin", "status"]).output
    out = r.invoke(cli.app, ["ui-pin", "set"], input="482913\n482913\n")
    assert out.exit_code == 0, out.output
    st = r.invoke(cli.app, ["ui-pin", "status"]).output
    assert "set (pin)" in st and "482913" not in st
    assert pin_auth.verify("482913")
    assert r.invoke(cli.app, ["ui-pin", "clear"]).exit_code == 0
    assert not pin_auth.pin_is_set()


def test_cli_set_mismatch_and_weak_change_nothing():
    r = CliRunner()
    assert r.invoke(cli.app, ["ui-pin", "set"], input="482913\n482914\n").exit_code == 2
    assert r.invoke(cli.app, ["ui-pin", "set"], input="123\n123\n").exit_code == 2
    assert not pin_auth.pin_is_set()


def test_cli_set_takes_no_secret_argument():
    r = CliRunner().invoke(cli.app, ["ui-pin", "set", "482913"])
    assert r.exit_code != 0 and not pin_auth.pin_is_set()


# --- routes / gate ---------------------------------------------------------- #
PORT = 45871  # the port test_healthz also uses: the Host guard can be installed once per process
HOST = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def client(tmp_path):
    with open_store(tmp_path / "p.db") as store:
        try:
            ui_app.build_app(store, Settings(imap_accounts=[]), None, port=PORT)
        except RuntimeError:
            # Another test already started the shared NiceGUI app; its guard (same port)
            # stays in place. Wire just what we own.
            ui_app._STORAGE_SECRET = ui_app._load_or_create_storage_secret()
            ui_app._STORE = store
            ui_app._LAUNCHER_KEY = ui_app.launcher_key()
            ui_app._register_signin()
        yield TestClient(ui_app.nicegui_app, base_url=HOST, follow_redirects=False), store


class _Req:
    def __init__(self, cookies=None, path="/mail", query=""):
        from types import SimpleNamespace

        self.cookies = cookies or {}
        self.url = SimpleNamespace(path=path, query=query)


def _as_browser(monkeypatch, req):
    from types import SimpleNamespace

    monkeypatch.setattr(ui_app, "context", SimpleNamespace(client=SimpleNamespace(request=req, status_code=200)))


def _cookie_from(resp):
    return resp.headers["set-cookie"].split("mf_auth=")[1].split(";")[0]


def test_no_pin_means_old_behaviour(client, monkeypatch):
    c, _ = client
    assert c.get("/signin").status_code == 303
    _as_browser(monkeypatch, _Req())
    assert ui_app._is_authenticated() is False
    # a made-up cookie is worthless when no PIN is set
    pin_auth.set_secret("482913")
    good = _cookie_from(c.post("/signin", data={"secret": "482913"}))
    pin_auth.clear_secret()
    _as_browser(monkeypatch, _Req({"mf_auth": good}))
    assert ui_app._is_authenticated() is False


def test_denied_redirects_to_signin_when_pin_set(client, monkeypatch):
    from types import SimpleNamespace

    pin_auth.set_secret("482913")
    seen = []
    _as_browser(monkeypatch, _Req(path="/mail", query="q=a&token=SECRET&k=KEY"))
    monkeypatch.setattr(ui_app, "ui", SimpleNamespace(navigate=SimpleNamespace(to=seen.append)))
    ui_app._denied()
    assert seen == ["/signin?next=/mail%3Fq%3Da"]
    assert "SECRET" not in seen[0] and "KEY" not in seen[0]


def test_signin_page_has_the_controls(client):
    c, _ = client
    pin_auth.set_secret("482913")
    page = c.get("/signin?next=/mail")
    assert page.status_code == 200 and "Remember this device" in page.text and "Sign in" in page.text
    assert 'name="next" value="/mail"' in page.text and "no-store" in page.headers["cache-control"]


def test_wrong_then_right_pin(client, monkeypatch):
    c, _ = client
    pin_auth.set_secret("482913")
    bad = c.post("/signin", data={"secret": "000000", "next": "/mail"})
    assert bad.status_code == 401 and "right. Please" in bad.text or "isn&#39;t right" in bad.text
    ok = c.post("/signin", data={"secret": "482913", "next": "/mail"})
    assert ok.status_code == 303 and ok.headers["location"] == "/mail"
    sc = ok.headers["set-cookie"]
    assert "HttpOnly" in sc and "samesite=strict" in sc.lower() and "max-age" not in sc.lower()
    _as_browser(monkeypatch, _Req({"mf_auth": _cookie_from(ok)}))
    assert ui_app._is_authenticated() is True
    assert c.get("/signin?next=/mail").status_code == 303  # already signed in


def test_remember_sets_long_cookie(client):
    c, _ = client
    pin_auth.set_secret("482913")
    ok = c.post("/signin", data={"secret": "482913", "remember": "1"})
    assert f"Max-Age={30 * 86400}" in ok.headers["set-cookie"]


def test_lockout_returns_429_even_for_right_pin(client):
    c, _ = client
    pin_auth.set_secret("482913")
    for _ in range(6):  # the first try carries no device cookie yet, so it lands in the address bucket
        c.post("/signin", data={"secret": "000000"})
    r = c.post("/signin", data={"secret": "482913"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert "mf_auth" not in r.headers.get("set-cookie", "")


def test_forwarded_for_buckets_by_real_client(client):
    c, _ = client
    pin_auth.set_secret("482913")
    for _ in range(5):
        c.post("/signin", data={"secret": "000000"}, headers={"X-Forwarded-For": "198.51.100.21"})
    c2 = TestClient(ui_app.nicegui_app, base_url=HOST, follow_redirects=False)
    r = c2.post("/signin", data={"secret": "482913"}, headers={"X-Forwarded-For": "198.51.100.22"})
    assert r.status_code == 303


def test_signout_clears_cookie(client):
    c, _ = client
    pin_auth.set_secret("482913")
    c.post("/signin", data={"secret": "482913"})
    r = c.post("/signout")
    assert r.status_code == 204 and "mf_auth" in r.headers["set-cookie"]


def test_signin_post_obeys_host_and_origin_guard(client):
    c, _ = client
    pin_auth.set_secret("482913")
    assert c.post("/signin", data={"secret": "482913"}, headers={"Origin": "http://evil.example"}).status_code == 403
    bad_host = TestClient(ui_app.nicegui_app, base_url="http://evil.example", follow_redirects=False)
    assert bad_host.post("/signin", data={"secret": "482913"}).status_code == 403


def test_open_redirect_blocked(client):
    c, _ = client
    pin_auth.set_secret("482913")
    ok = c.post("/signin", data={"secret": "482913", "next": "//evil.example"})
    assert ok.headers["location"] == "/"


def test_launcher_key_path_unchanged_with_pin_set(client, monkeypatch):
    """/launch still validates the key and sets the storage flag; the PIN never gates it."""
    from types import SimpleNamespace

    pin_auth.set_secret("482913")
    flag: dict = {}
    seen = []
    monkeypatch.setattr(ui_app, "nicegui_app", SimpleNamespace(storage=SimpleNamespace(user=flag)))
    monkeypatch.setattr(ui_app, "ui", SimpleNamespace(navigate=SimpleNamespace(to=seen.append)))
    # the registered handler is the real one; call it through the module-level state it reads
    ui_app._PAGES_REGISTERED = False
    handlers = {}

    class _UI:
        navigate = SimpleNamespace(to=seen.append)

        @staticmethod
        def page(path):
            return lambda f: handlers.setdefault(path, f)

    monkeypatch.setattr(ui_app, "ui", _UI)
    ui_app._register_pages(None, None, None)
    ui_app._PAGES_REGISTERED = False
    handlers["/launch"](k=ui_app._LAUNCHER_KEY)
    assert flag.get("authenticated") is True and seen == ["/"]


def test_audit_records_outcomes_without_secrets(client):
    c, store = client
    pin_auth.set_secret("482913")
    c.post("/signin", data={"secret": "000000"})
    c.post("/signin", data={"secret": "482913"})
    c.post("/signout")
    rows = store.conn.execute("SELECT event, detail_json FROM audit_log ORDER BY id").fetchall() \
        if hasattr(store, "conn") else None
    assert rows is not None
    events = [r[0] for r in rows]
    assert events == ["signin_failed", "signin_ok", "signout"]
    blob = " ".join(str(r[1]) for r in rows)
    assert "482913" not in blob and "000000" not in blob
