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
from nicegui import ui

from ..paths import runtime_file
from . import theme
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
    """Return the persisted UI port, generating + storing one on first run."""
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
    app-drawer launcher (``siftforge open``) instead opens
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


def rotate_launcher_key() -> str:
    """Mint a fresh launcher key (the old one is retired).

    The key travels in a URL query string (browser history, any proxy log),
    so it is rotated after every successful ``/launch`` — the desktop entry
    reads the file on each click, so rotation costs the user nothing.
    """
    key = _pysecrets.token_urlsafe(32)
    try:
        _write_secret_file(runtime_file("ui_launcher_key"), key)
    except OSError as e:
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


# --------------------------------------------------------------------------- #
# Auth: launch token -> signed session cookie (spec §0.7)
# --------------------------------------------------------------------------- #
def _is_authenticated() -> bool:
    try:
        return bool(nicegui_app.storage.user.get("authenticated"))
    except Exception:
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


def _denied() -> None:
    theme.apply()
    with ui.column().classes("absolute-center items-center").style("gap: 10px"):
        ui.icon("lock", size="42px").style("color: var(--error)")
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
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def _install_host_guard(port: int) -> None:
    """Reject any HTTP request OR WebSocket upgrade whose Host header is not
    127.0.0.1:PORT / localhost:PORT (spec §0.7), and any WebSocket whose
    Origin (when sent) is not one of ours.

    Written as a raw ASGI middleware on purpose: Starlette's
    ``BaseHTTPMiddleware`` passes non-``http`` scopes straight through, so
    the previous version never looked at NiceGUI's socket.io connection —
    the channel every UI action travels on.
    """
    allowed = allowed_hosts(port)
    allowed_origins = {f"http://{h}" for h in allowed}

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
            if host_ok and (kind == "http" or origin_ok):
                await self.app(scope, receive, send)
                return
            if kind == "http":
                from starlette.responses import PlainTextResponse

                resp = PlainTextResponse("Forbidden: bad Host header", status_code=403)
                await resp(scope, receive, send)
            else:
                # Refuse the upgrade before the handshake completes.
                await send({"type": "websocket.close", "code": 1008})

    nicegui_app.add_middleware(_HostHeaderGuard)


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
        message_id: int | None = None,
        account: str = "",
        q: str = "",
        filter: str = "all",
        tag: str = "",
        page: int = 0,
        token: str | None = None,
    ) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("mail", "Inbox", bridge, store, max_width=1500):
            mail_page.render(
                store,
                settings,
                bridge,
                message_id=message_id,
                account=account,
                q=q,
                filter=filter,
                tag=tag,
                page=page,
            )

    @ui.page("/mail/{message_id}")
    def _mail_detail(message_id: int, token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("mail", "Message", bridge, store, max_width=1120):
            mail_page.render_detail(store, settings, message_id, bridge)

    @ui.page("/outbox")
    def _outbox(view: str = "all", page: int = 0, token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("outbox", "Outbox & Sent", bridge, store, max_width=1180):
            outbox_page.render(store, settings, view=view, page=page)

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
        draft_id: int | None = None,
        template_id: int | None = None,
        account: str = "",
        reply_to: int | None = None,
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
                draft_id=draft_id,
                template_id=template_id,
                account=account,
                reply_to=reply_to,
            )

    @ui.page("/templates")
    def _templates(
        site: str = "",
        to: str = "",
        subject: str = "",
        message_id: int | None = None,
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
                message_id=message_id,
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
def build_app(
    store: object,
    settings: object,
    bridge: object | None = None,
    host: str = "127.0.0.1",
    *,
    port: int | None = None,
) -> dict[str, Any]:
    """Wire pages, auth, and the Host-header guard WITHOUT starting a server.

    Returns a dict with the resolved ``host``, ``port``, ``token``, and
    ``storage_secret`` so :func:`run_ui` (or a test) can launch / inspect. Safe
    to call in tests; does not bind a socket beyond the one-shot port probe.
    """
    global _PORT, _LAUNCH_TOKEN, _TOKEN_CONSUMED, _LAUNCHER_KEY

    _PORT = int(port) if port is not None else persistent_port()
    _LAUNCH_TOKEN = _pysecrets.token_urlsafe(32)
    _TOKEN_CONSUMED = False
    _LAUNCHER_KEY = launcher_key()
    storage_secret = _load_or_create_storage_secret()

    _install_host_guard(_PORT)
    _register_pages(store, settings, bridge)

    return {
        "host": host,
        "port": _PORT,
        "token": _LAUNCH_TOKEN,
        "storage_secret": storage_secret,
        "url": f"http://{host}:{_PORT}/?token={_LAUNCH_TOKEN}",
    }


def run_ui(
    store: object,
    settings: object,
    bridge: object | None = None,
    host: str = "127.0.0.1",
    *,
    port: int | None = None,
) -> None:
    """Build the app and start the NiceGUI server (spec §0.7, §9).

    Binds ``127.0.0.1`` only, on the persisted random high port, prints the
    one-shot launch URL to stdout, and runs with ``show=False``.
    """
    cfg = build_app(store, settings, bridge, host=host, port=port)
    print(f"SiftForge UI: {cfg['url']}", flush=True)
    log.info("UI listening on %s:%s (token in URL above)", cfg["host"], cfg["port"])
    ui.run(
        host=host,
        port=cfg["port"],
        show=False,
        reload=False,
        storage_secret=cfg["storage_secret"],
        title="SiftForge",
    )
