"""PIN / password sign-in for the web UI (the phone-friendly way in).

Modelled on DisPatch's sign-in so both apps give the same guarantees:

* The secret is stored only as a salted PBKDF2-HMAC-SHA256 hash (200,000 rounds, 16-byte
  random salt) in ``ui_pin`` next to ``ui_launcher_key`` (0600). Compared in constant time.
* Failed tries are throttled three ways: per device (5 failures, then 5 s doubling up to
  about 5 min), per client address (15 failures, same doubling) and install-wide (60
  failures in 10 min, then 30 s doubling). A locked bucket refuses *before* the secret is
  checked, so a guesser learns nothing while locked out.
* A successful sign-in sets a signed cookie: browser-session only, or 30 days when the
  person ticks "Remember this device". Changing or clearing the PIN invalidates every cookie.

The launcher-key and one-shot-token paths in ``app.py`` are unaffected.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets as _secrets
import threading
import time
from typing import Any

from ..paths import runtime_file

log = logging.getLogger(__name__)

PIN_FILE = "ui_pin"
PBKDF2_ITERATIONS = int(os.environ.get("MAILFORGE_PBKDF2_ITERATIONS", "200000"))
MIN_PIN_DIGITS = 6
MIN_PASSWORD_CHARS = 8
MAX_SECRET_CHARS = 128

REMEMBER_DAYS = 30
REMEMBER_SECONDS = REMEMBER_DAYS * 86400
SESSION_SECONDS = 12 * 3600  # a browser-session cookie is also refused server-side after this
COOKIE_NAME = "mf_auth"
DEVICE_COOKIE = "mf_dev"

DEVICE_LOCKOUT_AFTER = 5
IP_LOCKOUT_AFTER = 15
BASE_COOLDOWN = 5.0
GLOBAL_CEILING = 60
GLOBAL_WINDOW = 600.0
GLOBAL_COOLDOWN = 30.0


# --------------------------------------------------------------------------- #
# Strength + storage
# --------------------------------------------------------------------------- #
def check_strength(secret: str) -> str | None:
    """None when acceptable, else a plain-English reason. A numeric PIN needs 6+ digits;
    anything else is a password and needs 8+ characters."""
    if len(secret) > MAX_SECRET_CHARS:
        return f"That is too long (at most {MAX_SECRET_CHARS} characters)."
    if secret.isascii() and secret.isdigit():
        if len(secret) < MIN_PIN_DIGITS:
            return f"A PIN needs at least {MIN_PIN_DIGITS} digits (or use a password of {MIN_PASSWORD_CHARS}+ characters)."
        return None
    if len(secret.strip()) != len(secret) or not secret:
        return "A password cannot start or end with a space."
    if len(secret) < MIN_PASSWORD_CHARS:
        return f"A password needs at least {MIN_PASSWORD_CHARS} characters (or use a PIN of {MIN_PIN_DIGITS}+ digits)."
    return None


def _hash(secret: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), bytes.fromhex(salt_hex), iterations).hex()


def _path():
    return runtime_file(PIN_FILE)


def pin_is_set() -> bool:
    """True when a sign-in secret exists. An unreadable/corrupt file still counts as set
    (fail closed: the UI must not fall back to open)."""
    return _path().exists()


def _load() -> dict[str, Any] | None:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("hash") and d.get("salt"):
            return d
    except (OSError, ValueError):
        pass
    return None


def set_secret(secret: str) -> str:
    """Hash and store ``secret`` (0600). Returns ``"pin"`` or ``"password"``. Raises
    ``ValueError`` (plain message) when too weak."""
    from .app import _write_secret_file

    why = check_strength(secret)
    if why:
        raise ValueError(why)
    salt = _secrets.token_hex(16)
    kind = "pin" if secret.isascii() and secret.isdigit() else "password"
    rec = {
        "v": 1,
        "alg": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": salt,
        "hash": _hash(secret, salt, PBKDF2_ITERATIONS),
        "kind": kind,
    }
    _write_secret_file(_path(), json.dumps(rec))
    return kind


def clear_secret() -> bool:
    try:
        _path().unlink()
        return True
    except FileNotFoundError:
        return False


def kind() -> str | None:
    d = _load()
    return d.get("kind") if d else None


def verify(secret: str) -> bool:
    d = _load()
    if d is None or not isinstance(secret, str) or len(secret) > 1024:
        return False
    try:
        cand = _hash(secret, d["salt"], int(d.get("iterations") or PBKDF2_ITERATIONS))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(cand, str(d["hash"]))


def _fingerprint() -> str:
    d = _load()
    return hashlib.sha256(("fp:" + (d["hash"] if d else "")).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_buckets: dict[tuple, list[float]] = {}  # key -> [failures, locked_until (monotonic)]
_fail_count = 0
_fail_until = 0.0
_fail_window_start = 0.0


def reset_throttle() -> None:
    global _fail_count, _fail_until, _fail_window_start
    with _lock:
        _buckets.clear()
        _fail_count, _fail_until, _fail_window_start = 0, 0.0, 0.0


def _keys(device: str, ip: str) -> list[tuple]:
    return [("dev", device or f"ip:{ip}"), ("ip", ip or "-")]


def _threshold(key: tuple) -> int:
    return IP_LOCKOUT_AFTER if key[0] == "ip" else DEVICE_LOCKOUT_AFTER


def wait_seconds(device: str, ip: str) -> float:
    """Seconds before another try is allowed (0 = go ahead)."""
    now = time.monotonic()
    with _lock:
        wait = (_fail_until - now) if (_fail_count >= GLOBAL_CEILING and now < _fail_until) else 0.0
        for key in _keys(device, ip):
            b = _buckets.get(key)
            if b and b[0] >= _threshold(key) and now < b[1]:
                wait = max(wait, b[1] - now)
        return wait


def register_failure(device: str, ip: str) -> None:
    global _fail_count, _fail_until, _fail_window_start
    now = time.monotonic()
    with _lock:
        for key in _keys(device, ip):
            b = _buckets.setdefault(key, [0, 0.0])
            b[0] += 1
            thr = _threshold(key)
            if b[0] >= thr:
                b[1] = now + BASE_COOLDOWN * (2 ** min(b[0] - thr, 6))
        if now - _fail_window_start > GLOBAL_WINDOW:
            _fail_window_start, _fail_count = now, 0
        _fail_count += 1
        if _fail_count >= GLOBAL_CEILING:
            _fail_until = now + GLOBAL_COOLDOWN * (2 ** min(_fail_count - GLOBAL_CEILING, 6))
        if len(_buckets) > 4096:
            for k in [k for k, v in _buckets.items() if v[1] < now][:2048]:
                _buckets.pop(k, None)


def register_success(device: str, ip: str) -> None:
    with _lock:  # the global counter is deliberately NOT reset by a good sign-in
        for key in _keys(device, ip):
            _buckets.pop(key, None)


def attempt(secret: str, device: str, ip: str) -> tuple[bool, float]:
    """One sign-in try: ``(ok, wait)``. ``wait > 0`` means refused (locked) — when locked
    the secret is never looked at. A wrong secret that trips a lock reports the wait too."""
    w = wait_seconds(device, ip)
    if w > 0:
        return False, w
    if verify(secret):
        register_success(device, ip)
        return True, 0.0
    register_failure(device, ip)
    return False, wait_seconds(device, ip)


# --------------------------------------------------------------------------- #
# Client address
# --------------------------------------------------------------------------- #
def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address((host or "").split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _bucket_addr(addr: str) -> str:
    """IPv4 as is; IPv6 by its /64 (a home network hands out fresh addresses inside it)."""
    try:
        ip = ipaddress.ip_address(addr.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return "?"
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_ip(peer: str, forwarded_for: str | None) -> str:
    """The caller's address. ``X-Forwarded-For`` is believed only when the TCP peer is
    loopback (a same-machine reverse proxy); the right-most entry is the one the
    nearest proxy appended, so a client cannot forge it."""
    if _is_loopback(peer) and forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return _bucket_addr(last)
    return _bucket_addr(peer or "")


# --------------------------------------------------------------------------- #
# Session cookie
# --------------------------------------------------------------------------- #
def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def make_cookie(storage_secret: str, remember: bool, now: float | None = None) -> str:
    t = int(now if now is not None else time.time())
    exp = t + (REMEMBER_SECONDS if remember else SESSION_SECONDS)
    body = f"v1.{exp}.{_fingerprint()}"
    return f"{body}.{_sign(storage_secret, body)}"


def cookie_valid(storage_secret: str, value: str | None, now: float | None = None) -> bool:
    if not value or not pin_is_set():
        return False  # no PIN configured: only the launcher/storage flag counts
    try:
        ver, exp, fp, sig = value.split(".")
        if ver != "v1":
            return False
        body = f"{ver}.{exp}.{fp}"
        if not hmac.compare_digest(sig, _sign(storage_secret, body)):
            return False
        if not hmac.compare_digest(fp, _fingerprint()):
            return False
        return int(exp) > (now if now is not None else time.time())
    except ValueError:
        return False


def new_device_id() -> str:
    return _secrets.token_urlsafe(18)


def safe_next(target: str | None) -> str:
    """Only same-site paths: no scheme, no ``//host``, no backslash tricks."""
    t = (target or "").strip()
    if not t.startswith("/") or t.startswith("//") or "\\" in t or "\n" in t or "\r" in t:
        return "/"
    if t.startswith("/signin") or t.startswith("/signout"):
        return "/"
    return t


# --------------------------------------------------------------------------- #
# Sign-in page
# --------------------------------------------------------------------------- #
def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def render_page(*, message: str = "", next_url: str = "/", kind_hint: str | None = None) -> str:
    label = {"pin": "PIN", "password": "password"}.get(kind_hint or "", "PIN or password")
    msg = f'<p class="msg" role="alert">{_esc(message)}</p>' if message else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0e0e1b">
<title>Sign in - MailForge</title>
<style>
:root{{color-scheme:light dark;--bg:#f5f6f8;--card:#fff;--fg:#1c2330;--muted:#5d6778;--accent:#2f5fd0;--bad:#b3261e;--line:#d5d9e2}}
@media(prefers-color-scheme:dark){{:root{{--bg:#12161d;--card:#1b212b;--fg:#e7ebf2;--muted:#9aa5b8;--accent:#7aa2ff;--bad:#ff8a80;--line:#333c4b}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif}}
main{{width:min(92vw,380px);background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px 24px}}
h1{{margin:0 0 4px;font-size:1.4rem}}p{{margin:6px 0 16px;color:var(--muted)}}
label{{display:block;font-weight:600;margin-bottom:6px}}
input[type=password]{{width:100%;box-sizing:border-box;padding:12px;font-size:1.1rem;border:1px solid var(--line);border-radius:10px;background:transparent;color:inherit}}
.row{{display:flex;gap:8px;align-items:center;margin:14px 0}}
button{{width:100%;padding:12px;font-size:1rem;font-weight:600;border:0;border-radius:10px;background:var(--accent);color:#fff;cursor:pointer}}
.msg{{color:var(--bad);font-weight:600}}
</style></head><body><main>
<h1>MailForge</h1>
<p>Enter your {label} to open your mail.</p>
{msg}
<form method="post" action="/signin">
<input type="hidden" name="next" value="{_esc(next_url)}">
<label for="secret">{label[0].upper() + label[1:]}</label>
<input id="secret" name="secret" type="password" autocomplete="current-password" autofocus required>
<div class="row"><input id="remember" name="remember" type="checkbox" value="1">
<label for="remember" style="margin:0;font-weight:400">Remember this device for {REMEMBER_DAYS} days</label></div>
<button type="submit">Sign in</button>
</form></main></body></html>"""
