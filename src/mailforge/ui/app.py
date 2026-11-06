"""NiceGUI entrypoint + human-approval security shell (build spec §0.7, §9, §11).

Security properties (invariants §0.7 / §11 checklist):
  * Binds ``127.0.0.1`` only, on a RANDOM high port persisted on first run
    (``paths.runtime_file('ui_port')``) and reused thereafter.
  * Prints the launch URL with a ONE-SHOT ``?token=...`` to stdout; the first
    valid hit swaps it for a SIGNED session cookie (NiceGUI ``app.storage.user``
    backed by a generated ``storage_secret``). Later requests use the cookie.
  * Middleware REJECTS any request whose ``Host`` header != ``127.0.0.1:PORT``.
  * ``ui.run(..., show=False)``.

Public API:
  * :func:`build_app` — wires pages + middleware WITHOUT starting a server
    (importable/testable).
  * :func:`run_ui`    — :func:`build_app` then ``ui.run`` (blocking server).
  * :func:`refresh_inbox` — module-level hook the runtime/listener coroutine
    imports and calls to push pending-draft updates over the WS.
"""

from __future__ import annotations

import logging
import secrets as _pysecrets
import socket
from typing import Any

from nicegui import app as nicegui_app
from nicegui import context, ui

from ..paths import (
    runtime_file,
    ui_extra_allowed_hosts,
    ui_host,
    ui_port_override,
    url_host,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import pin_auth, pwa, theme
from .pages import activity as activity_page
from .pages import compose as compose_page
from .pages import dashboard as dashboard_page
from .pages import detail as detail_page
from .pages import inbox as inbox_page
from .pages import knowledge as knowledge_page
from .pages import mail as mail_page
from .pages import models as models_page
from .pages import outbox as outbox_page
from .pages import references as references_page
from .pages import settings as settings_page
from .pages import spam as spam_page
from .pages import templates as templates_page

log = logging.getLogger(__name__)

# Module-level handles set by build_app(), used by middleware + refresh hook.
_PORT: int | None = None
_LAUNCH_TOKEN: str | None = None
_TOKEN_CONSUMED = False
_LAUNCHER_KEY: str | None = None
# (retired key, expiry monotonic) — a just-rotated key stays valid briefly so a
# double-opened launcher tab does not land on "Access denied".
_PREV_LAUNCHER_KEY: tuple[str, float] | None = None
_PREV_KEY_GRACE_S = 60.0
# Set by build_app(): the cookie-signing secret and the store the PIN sign-in audits to.
_STORAGE_SECRET: str | None = None
_STORE: object | None = None


def _int_or_none(value: str | None) -> int | None:
    """Parse an optional integer query parameter; junk means "not given".

    Typing the route parameter ``int`` lets FastAPI answer ``?page=abc`` with a
    raw 422 JSON body instead of the page, so ids/pages arrive as text.
    """
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _write_secret_file(path: Any, value: str) -> None:
    """Create/replace a 0600 secret file with no world-readable window.

    ``write_text`` + ``chmod`` leaves the file at the umask default (0644)
    until the chmod runs; creating it O_EXCL at 0600 and renaming over the
    target avoids that, and re-asserts 0600 on files an older version left
    at 0644.
    """
    import os
    import stat

    mode = stat.S_IRUSR | stat.S_IWUSR
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.replace(tmp, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Port + token persistence (spec §0.7)
# --------------------------------------------------------------------------- #
def _random_high_port() -> int:
    """Pick a free, ephemeral high port by binding to :0 and reading it back."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def persistent_port() -> int:
    """Return the UI port: ``MAILFORGE_UI_PORT`` if set, else the persisted
    random one (generated + stored on first run)."""
    fixed = ui_port_override()
    if fixed is not None:
        return fixed
    f = runtime_file("ui_port")
    if f.exists():
        try:
            return int(f.read_text(encoding="utf-8").strip())
        except (ValueError, OSError) as e:
            log.warning("bad persisted ui_port (%s); regenerating", e)
    port = _random_high_port()
    f.write_text(str(port), encoding="utf-8")
    return port


def launcher_key() -> str:
    """Stable per-user launcher key for the desktop entry (0600 file).

    The console one-shot ``?token=`` URL is for the manual/headless path. The
    app-drawer launcher (``mailforge open``) instead opens
    ``/launch?k=<this>``; the route validates the key against this 0600 file and
    upgrades to the same signed session cookie. The file is readable only by the
    user, so possessing it == being the local user — the same trust boundary the
    127.0.0.1 bind + Host-header guard already assume. It is reusable (the
    launcher runs every time you click the icon), unlike the one-shot token.
    """
    f = runtime_file("ui_launcher_key")
    if f.exists():
        try:
            v = f.read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            pass
    key = _pysecrets.token_urlsafe(32)
    try:
        _write_secret_file(f, key)
    except OSError as e:
        log.warning("could not persist launcher key: %s", e)
    return key


def rotate_launcher_key(*, strict: bool = False) -> str:
    """Mint a fresh launcher key (the old one is retired).

    ``strict=True`` (the recovery commands) raises ``OSError`` when the key file could
    not be rewritten; the per-click rotation after ``/launch`` only logs.

    The key travels in a URL query string (browser history, any proxy log),
    so it is rotated after every successful ``/launch`` — the desktop entry
    reads the file on each click, so rotation costs the user nothing.
    """
    key = _pysecrets.token_urlsafe(32)
    try:
        _write_secret_file(runtime_file("ui_launcher_key"), key)
    except OSError as e:
        if strict:
            raise
        log.warning("could not rotate launcher key: %s", e)
    return key


def _load_or_create_storage_secret() -> str:
    """A stable signing secret for the signed session cookie (spec §0.7)."""
    f = runtime_file("ui_storage_secret")
    if f.exists():
        try:
            v = f.read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            pass
    secret = _pysecrets.token_urlsafe(48)
    try:
        _write_secret_file(f, secret)  # 0600 from the first byte — it signs the cookie
    except OSError as e:
        log.warning("could not persist storage secret: %s", e)
    return secret


def rotate_storage_secret() -> str:
    """Mint a fresh cookie-signing secret: every existing browser session dies.

    Takes effect when the server next starts (it reads the secret once). Used by
    ``mailforge token rotate`` and the lockout-recovery startup reset.
    """
    secret = _pysecrets.token_urlsafe(48)
    _write_secret_file(runtime_file("ui_storage_secret"), secret)
    return secret


# --------------------------------------------------------------------------- #
# Auth: launch token -> signed session cookie (spec §0.7)
# --------------------------------------------------------------------------- #
def _is_authenticated() -> bool:
    try:
        if nicegui_app.storage.user.get("authenticated"):
            return True
    except Exception:
        pass
    # PIN/password sign-in cookie (only honoured while a PIN is set).
    try:
        req = context.client.request
        if _STORAGE_SECRET and req is not None:
            return pin_auth.cookie_valid(_STORAGE_SECRET, req.cookies.get(pin_auth.COOKIE_NAME))
    except Exception:
        pass
    return False


def _try_consume_token(token: str | None) -> bool:
    """Consume the one-shot launch token, upgrading to a session cookie."""
    global _TOKEN_CONSUMED
    if not token or _LAUNCH_TOKEN is None:
        return False
    # Constant-time compare; one-shot (rejected once consumed).
    if _TOKEN_CONSUMED or not _pysecrets.compare_digest(token, _LAUNCH_TOKEN):
        return False
    _TOKEN_CONSUMED = True
    try:
        nicegui_app.storage.user["authenticated"] = True
    except Exception as e:
        log.warning("could not set session cookie: %s", e)
        return False
    return True


def _gate(token: str | None) -> bool:
    """Return True if the current request is authorised to view a page.

    Already-authenticated (cookie) sessions pass. Otherwise a valid one-shot
    launch token upgrades the session and passes. Everything else is denied and
    the caller renders an access-denied page.
    """
    if _is_authenticated():
        return True
    return _try_consume_token(token)


def _signin_target() -> str:
    """Where to send the person after signing in: the page they asked for, minus any
    launch credentials in its query string."""
    try:
        from urllib.parse import parse_qsl, urlencode

        req = context.client.request
        q = [(k, v) for k, v in parse_qsl(req.url.query) if k not in ("token", "k")]
        return pin_auth.safe_next(req.url.path + ("?" + urlencode(q) if q else ""))
    except Exception:
        return "/"


def _denied() -> None:
    if pin_auth.pin_is_set():
        from urllib.parse import quote

        ui.navigate.to("/signin?next=" + quote(_signin_target(), safe="/"))
        return
    # 403, not the default 200, so a monitor can tell "denied" from "served".
    context.client.status_code = 403
    theme.apply()
    with ui.column().classes("absolute-center items-center").style("gap: 10px"):
        ui.icon("lock", size="42px").style("color: var(--danger-text)")
        ui.label("Access denied").classes("text-h5").style("font-weight:700")
        ui.label(
            "Open the URL printed to the console (it carries a one-shot launch "
            "token), or use the app-drawer launcher. The token upgrades to a "
            "signed session cookie on first use."
        ).style("color: var(--text-secondary); max-width: 420px; text-align: center")


# --------------------------------------------------------------------------- #
# Host-header enforcement middleware (spec §0.7 / §11)
# --------------------------------------------------------------------------- #
def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"} | ui_extra_allowed_hosts()


def _install_host_guard(port: int) -> None:
    """Reject any HTTP request OR WebSocket upgrade whose Host header is not
    127.0.0.1:PORT / localhost:PORT (spec §0.7), and any request whose
    Origin (when sent) is not one of ours.

    Written as a raw ASGI middleware on purpose: Starlette's
    ``BaseHTTPMiddleware`` passes non-``http`` scopes straight through, so
    the previous version never looked at NiceGUI's socket.io connection —
    the channel every UI action travels on.
    """
    allowed = allowed_hosts(port)
    allowed_origins = {f"http://{h}" for h in allowed}
    # Behind a TLS-terminating proxy the browser's Origin is https://<host>.
    allowed_origins |= {f"https://{h}" for h in ui_extra_allowed_hosts()}

    class _HostHeaderGuard:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            kind = scope.get("type")
            if kind not in ("http", "websocket"):
                await self.app(scope, receive, send)
                return
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers") or []}
            host_ok = headers.get("host", "") in allowed
            origin = headers.get("origin")
            origin_ok = origin is None or origin in allowed_origins
            # Origin applies to plain HTTP too: socket.io's long-polling
            # fallback and uploads are HTTP POSTs, and NiceGUI's socket.io
            # server accepts any origin. Same-origin navigations send no
            # Origin (or ours), so only cross-site requests are refused.
            if host_ok and origin_ok:
                await self.app(scope, receive, send)
                return
            if kind == "http":
                from starlette.responses import PlainTextResponse

                what = "Host header" if not host_ok else "Origin"
                resp = PlainTextResponse(f"Forbidden: bad {what}", status_code=403)
                await resp(scope, receive, send)
            else:
                # Refuse the upgrade before the handshake completes.
                await send({"type": "websocket.close", "code": 1008})

    nicegui_app.add_middleware(_HostHeaderGuard)


# --------------------------------------------------------------------------- #
# Theme folder
# --------------------------------------------------------------------------- #
_THEME_INSTALLED = False


def _install_theme_files() -> None:
    """Serve the shared theme folder (ui/static/ui-theme/) at theme.THEME_URL.

    Its URLs carry no version, because replacing the folder is the whole theme
    update; so every response is sent `no-cache` (revalidate, a cheap 304) and
    a new copy is live on the next page load. Idempotent."""
    global _THEME_INSTALLED
    if _THEME_INSTALLED:
        return
    _THEME_INSTALLED = True
    if not theme.THEME_DIR.is_dir():
        log.warning("theme folder missing (%s); pages render unthemed", theme.THEME_DIR)
        return
    nicegui_app.add_static_files(theme.THEME_URL, str(theme.THEME_DIR))
    prefix = theme.THEME_URL + "/"

    class _ThemeNoCache:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope.get("type") != "http" or not scope.get("path", "").startswith(prefix):
                await self.app(scope, receive, send)
                return

            async def _send(message: Any) -> None:
                if message.get("type") == "http.response.start":
                    headers = [(k, v) for k, v in message.get("headers", [])
                               if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-cache"))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, _send)

    nicegui_app.add_middleware(_ThemeNoCache)


# --------------------------------------------------------------------------- #
# Page registration
# --------------------------------------------------------------------------- #
_PAGES_REGISTERED = False


def _register_pages(store: object, settings: object, bridge: object | None) -> None:
    """Register the four NiceGUI pages with auth gating (idempotent)."""
    global _PAGES_REGISTERED
    if _PAGES_REGISTERED:
        return
    _PAGES_REGISTERED = True

    @ui.page("/launch")
    def _launch(k: str | None = None) -> None:
        """Desktop-launcher entry: validate the 0600 launcher key, upgrade to a
        signed session cookie, then redirect to the inbox. See launcher_key()."""
        global _LAUNCHER_KEY, _PREV_LAUNCHER_KEY
        import time as _time

        ok = bool(k and _LAUNCHER_KEY is not None and _pysecrets.compare_digest(k, _LAUNCHER_KEY))
        if not ok and k and _PREV_LAUNCHER_KEY is not None:
            prev, until = _PREV_LAUNCHER_KEY
            ok = _time.monotonic() < until and _pysecrets.compare_digest(k, prev)
        if ok:
            try:
                nicegui_app.storage.user["authenticated"] = True
            except Exception as e:
                log.warning("launch: could not set session cookie: %s", e)
                ok = False
        if not ok:
            _denied()
            return
        # One use per key: rotate so the value left in browser history is dead.
        if _LAUNCHER_KEY is not None and k == _LAUNCHER_KEY:
            _PREV_LAUNCHER_KEY = (_LAUNCHER_KEY, _time.monotonic() + _PREV_KEY_GRACE_S)
            _LAUNCHER_KEY = rotate_launcher_key()
        ui.navigate.to("/")

    @ui.page("/")
    def _dashboard(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("dashboard", "Dashboard", bridge, store):
            dashboard_page.render(store, settings)

    @ui.page("/drafts")
    def _inbox(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("drafts", "AI Review", bridge, store):
            inbox_page.render(store)

    @ui.page("/detail/{draft_id}")
    def _detail(draft_id: int, token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("drafts", f"Draft #{draft_id}", bridge, store):
            detail_page.render(store, settings, draft_id, bridge)

    @ui.page("/mail")
    def _mail(
        message_id: str = "",
        account: str = "",
        q: str = "",
        filter: str = "all",
        tag: str = "",
        page: str = "",
        token: str | None = None,
    ) -> None:
        if not _gate(token):
            _denied()
            return
        saved = filter in {"archived", "saved"}
        with theme.shell(
            "saved" if saved else "mail", "Saved" if saved else "Inbox",
            bridge, store, max_width=1500,
        ):
            mail_page.render(
                store,
                settings,
                bridge,
                message_id=_int_or_none(message_id),
                account=account,
                q=q,
                filter=filter,
                tag=tag,
                page=max(0, _int_or_none(page) or 0),
            )

    @ui.page("/mail/{message_id}")
    def _mail_detail(message_id: int, token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("mail", "Message", bridge, store, max_width=1120):
            mail_page.render_detail(store, settings, message_id, bridge)

    @ui.page("/outbox")
    def _outbox(view: str = "all", page: str = "", token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("outbox", "Outbox & Sent", bridge, store, max_width=1180):
            outbox_page.render(store, settings, view=view, page=max(0, _int_or_none(page) or 0))

    @ui.page("/outbox/{kind}/{row_id}")
    def _outbox_detail(kind: str, row_id: int, token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("outbox", "Outbox entry", bridge, store, max_width=1020):
            outbox_page.render_detail(store, settings, kind, row_id)

    @ui.page("/spam")
    def _spam(account: str = "", token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("spam", "Spam & Deletion", bridge, store, max_width=1180):
            spam_page.render(store, settings, account=account)

    @ui.page("/compose")
    def _compose(
        to: str = "",
        subject: str = "",
        draft_id: str = "",
        template_id: str = "",
        account: str = "",
        reply_to: str = "",
        token: str | None = None,
    ) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("compose", "Compose", bridge, store):
            compose_page.render(
                store,
                settings,
                to=to,
                subject=subject,
                draft_id=_int_or_none(draft_id),
                template_id=_int_or_none(template_id),
                account=account,
                reply_to=_int_or_none(reply_to),
            )

    @ui.page("/templates")
    def _templates(
        site: str = "",
        to: str = "",
        subject: str = "",
        message_id: str = "",
        token: str | None = None,
    ) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("templates", "Response Templates", bridge, store):
            templates_page.render(
                store,
                settings,
                site=site,
                to=to,
                subject=subject,
                message_id=_int_or_none(message_id),
            )

    @ui.page("/references")
    def _references(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("references", "Knowledge", bridge, store):
            references_page.render(store, settings, bridge)

    @ui.page("/knowledge/{site_id}")
    def _knowledge(
        site_id: str,
        full: bool = False,
        token: str | None = None,
    ) -> None:
        if not _gate(token):
            _denied()
            return
        title = f"{settings.site_name(site_id)} Knowledge"
        with theme.shell("references", title, bridge, store, max_width=1180):
            knowledge_page.render(site_id, settings, full=full)

    @ui.page("/activity")
    def _activity(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("activity", "Activity", bridge, store):
            activity_page.render(store)

    @ui.page("/settings")
    def _settings(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("settings", "Settings", bridge, store):
            settings_page.render(store, settings)

    @ui.page("/models")
    def _models(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("models", "Models", bridge, store):
            models_page.render(bridge)


# --------------------------------------------------------------------------- #
# WS push hook for the listener coroutine (spec §9)
# --------------------------------------------------------------------------- #
def refresh_inbox() -> None:
    """Push pending-draft updates to connected clients (spec §9 WS push).

    The runtime/listener coroutine imports and calls this whenever a new draft
    lands so the inbox table re-renders without a manual reload.
    """
    inbox_page.refresh()
    try:
        dashboard_page.refresh.refresh()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Wiring + launch
# --------------------------------------------------------------------------- #
def _display_host(host: str) -> str:
    """0.0.0.0 / :: are bind addresses, not something a browser can open."""
    return url_host("127.0.0.1" if host in ("0.0.0.0", "::") else host)


def build_app(
    store: object,
    settings: object,
    bridge: object | None = None,
    host: str | None = None,
    *,
    port: int | None = None,
) -> dict[str, Any]:
    """Wire pages, auth, and the Host-header guard WITHOUT starting a server.

    Returns a dict with the resolved ``host``, ``port``, ``token``, and
    ``storage_secret`` so :func:`run_ui` (or a test) can launch / inspect. Safe
    to call in tests; does not bind a socket beyond the one-shot port probe.
    """
    global _PORT, _LAUNCH_TOKEN, _TOKEN_CONSUMED, _LAUNCHER_KEY, _STORAGE_SECRET, _STORE

    host = host or ui_host()
    _PORT = int(port) if port is not None else persistent_port()
    _LAUNCH_TOKEN = _pysecrets.token_urlsafe(32)
    _TOKEN_CONSUMED = False
    _LAUNCHER_KEY = launcher_key()
    storage_secret = _load_or_create_storage_secret()
    _STORAGE_SECRET = storage_secret
    _STORE = store

    _install_host_guard(_PORT)
    _install_theme_files()
    _register_health(store)
    pwa.register_routes(nicegui_app)
    _register_signin()
    _register_pages(store, settings, bridge)

    return {
        "host": host,
        "port": _PORT,
        "token": _LAUNCH_TOKEN,
        "storage_secret": storage_secret,
        "url": f"http://{_display_host(host)}:{_PORT}/?token={_LAUNCH_TOKEN}",
    }


def _audit(event: str, detail: dict[str, Any]) -> None:
    """Sign-in outcomes go to the hash-chained audit log (never a secret value)."""
    if _STORE is None:
        return
    try:
        from ..audit.log import AuditLog

        AuditLog(_STORE).record("user", event, detail=detail)
    except Exception as e:  # noqa: BLE001 - auditing must not break the page
        log.warning("signin audit failed: %s", e)


def _request_ip(request: Any) -> str:
    peer = request.client.host if request.client else ""
    return pin_auth.client_ip(peer, request.headers.get("x-forwarded-for"))


def _https_request(request: Any) -> bool:
    peer = request.client.host if request.client else ""
    if request.url.scheme == "https":
        return True
    return pin_auth._is_loopback(peer) and request.headers.get("x-forwarded-proto", "") == "https"


def _register_signin() -> None:
    """``GET/POST /signin`` and ``POST /signout``: plain routes (no socket needed), behind
    the Host/Origin guard like everything else."""
    from urllib.parse import parse_qs

    if any(getattr(r, "path", None) == "/signin" for r in nicegui_app.routes):
        return
    nocache = {"Cache-Control": "no-store"}

    def _page(request: Request, message: str = "", status: int = 200, next_url: str = "/",
              extra: dict[str, str] | None = None) -> HTMLResponse:
        resp = HTMLResponse(pin_auth.render_page(message=message, next_url=next_url,
                                                 kind_hint=pin_auth.kind()), status_code=status)
        for k, v in {**nocache, **(extra or {})}.items():
            resp.headers[k] = v
        if not request.cookies.get(pin_auth.DEVICE_COOKIE):
            resp.set_cookie(pin_auth.DEVICE_COOKIE, pin_auth.new_device_id(), max_age=365 * 86400,
                            httponly=True, samesite="strict", secure=_https_request(request))
        return resp

    def _signin_get(request: Request) -> Response:
        nxt = pin_auth.safe_next(request.query_params.get("next"))
        if not pin_auth.pin_is_set():
            return RedirectResponse("/", status_code=303)
        if _STORAGE_SECRET and pin_auth.cookie_valid(_STORAGE_SECRET, request.cookies.get(pin_auth.COOKIE_NAME)):
            return RedirectResponse(nxt, status_code=303)
        return _page(request, next_url=nxt)

    async def _signin_post(request: Request) -> Response:
        if not pin_auth.pin_is_set() or not _STORAGE_SECRET:
            return RedirectResponse("/", status_code=303)
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > 4096:
                return Response("Too large", status_code=413)
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        nxt = pin_auth.safe_next(form.get("next"))
        remember = form.get("remember") == "1"
        device = request.cookies.get(pin_auth.DEVICE_COOKIE, "")
        ip = _request_ip(request)
        import asyncio

        ok, wait = await asyncio.to_thread(pin_auth.attempt, form.get("secret", ""), device, ip)
        if ok:
            _audit("signin_ok", {"ip": ip, "remember": remember})
            resp = RedirectResponse(nxt, status_code=303)
            resp.headers["Cache-Control"] = "no-store"
            resp.set_cookie(
                pin_auth.COOKIE_NAME,
                pin_auth.make_cookie(_STORAGE_SECRET, remember),
                max_age=pin_auth.REMEMBER_SECONDS if remember else None,
                httponly=True, samesite="strict", secure=_https_request(request), path="/",
            )
            return resp
        if wait > 0:
            secs = max(1, int(wait) + 1)
            if pin_auth.audit_allowed("signin_locked", ip):
                _audit("signin_locked", {"ip": ip, "retry_after_s": secs})
            return _page(request, f"Too many tries. Please wait {secs} seconds, then try again.",
                         429, nxt, {"Retry-After": str(secs)})
        if pin_auth.audit_allowed("signin_failed", ip):
            _audit("signin_failed", {"ip": ip})
        return _page(request, "That PIN or password isn't right. Please try again.", 401, nxt)

    def _signout(request: Request) -> Response:
        _audit("signout", {"ip": _request_ip(request)})
        resp = Response(status_code=204)
        resp.delete_cookie(pin_auth.COOKIE_NAME, path="/")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    nicegui_app.get("/signin")(_signin_get)
    nicegui_app.post("/signin")(_signin_post)
    nicegui_app.post("/signout")(_signout)


def sign_out_current_browser() -> None:
    """The header's Sign out button: drop the launcher/storage flag, then the cookie."""
    try:
        nicegui_app.storage.user.pop("authenticated", None)
    except Exception:  # noqa: BLE001
        pass
    ui.run_javascript(
        "fetch('/signout',{method:'POST',credentials:'same-origin'})"
        ".finally(()=>{location.href='/signin'})"
    )


def _register_health(store: object) -> None:
    """``GET /healthz``: JSON status for monitors. Deliberately outside
    ``_gate`` (no session) but still behind the Host/Origin guard, so only
    127.0.0.1:<ui_port> reaches it; it exposes no credentials or content."""
    from starlette.responses import JSONResponse

    from ..health import health_report

    def _healthz() -> JSONResponse:
        body, code = health_report(store)
        return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})

    # NiceGUI's app is a FastAPI instance; skip a duplicate on re-entry (tests).
    if not any(getattr(r, "path", None) == "/healthz" for r in nicegui_app.routes):
        nicegui_app.get("/healthz")(_healthz)


def run_ui(
    store: object,
    settings: object,
    bridge: object | None = None,
    host: str | None = None,
    *,
    port: int | None = None,
) -> None:
    """Build the app and start the NiceGUI server (spec §0.7, §9).

    Binds ``127.0.0.1`` only (unless ``MAILFORGE_UI_HOST`` says otherwise, for
    containers), on the persisted random high port (or ``MAILFORGE_UI_PORT``),
    prints the one-shot launch URL to stdout, and runs with ``show=False``.
    """
    host = host or ui_host()
    cfg = build_app(store, settings, bridge, host=host, port=port)
    print(f"MailForge UI: {cfg['url']}", flush=True)
    log.info("UI listening on %s:%s (token in URL above)", cfg["host"], cfg["port"])
    ui.run(
        host=host,
        port=cfg["port"],
        show=False,
        reload=False,
        storage_secret=cfg["storage_secret"],
        title="MailForge",
    )
