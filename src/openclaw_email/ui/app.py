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
from .pages import detail as detail_page
from .pages import inbox as inbox_page
from .pages import models as models_page
from .pages import settings as settings_page

log = logging.getLogger(__name__)

# Module-level handles set by build_app(), used by middleware + refresh hook.
_PORT: int | None = None
_LAUNCH_TOKEN: str | None = None
_TOKEN_CONSUMED = False
_LAUNCHER_KEY: str | None = None


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
    app-drawer launcher (``openclaw-email open``) instead opens
    ``/launch?k=<this>``; the route validates the key against this 0600 file and
    upgrades to the same signed session cookie. The file is readable only by the
    user, so possessing it == being the local user — the same trust boundary the
    127.0.0.1 bind + Host-header guard already assume. It is reusable (the
    launcher runs every time you click the icon), unlike the one-shot token.
    """
    import os
    import stat

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
        f.write_text(key, encoding="utf-8")
        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError as e:
        log.warning("could not persist launcher key: %s", e)
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
        f.write_text(secret, encoding="utf-8")
        import os
        import stat

        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — it signs the cookie
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
def _install_host_guard(port: int) -> None:
    """Reject any request whose Host header != 127.0.0.1:PORT (spec §0.7)."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse

    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class _HostHeaderGuard(BaseHTTPMiddleware):
        async def dispatch(self, request: Any, call_next: Any) -> Any:
            host = request.headers.get("host", "")
            if host not in allowed:
                return PlainTextResponse("Forbidden: bad Host header", status_code=403)
            return await call_next(request)

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
        ok = bool(
            k
            and _LAUNCHER_KEY is not None
            and _pysecrets.compare_digest(k, _LAUNCHER_KEY)
        )
        if ok:
            try:
                nicegui_app.storage.user["authenticated"] = True
            except Exception as e:
                log.warning("launch: could not set session cookie: %s", e)
                ok = False
        if not ok:
            _denied()
            return
        ui.navigate.to("/")

    @ui.page("/")
    def _inbox(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("inbox", "Inbox", bridge):
            inbox_page.render(store)

    @ui.page("/detail/{draft_id}")
    def _detail(draft_id: int, token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("inbox", f"Draft #{draft_id}", bridge):
            detail_page.render(store, settings, draft_id)

    @ui.page("/activity")
    def _activity(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("activity", "Activity", bridge):
            activity_page.render(store)

    @ui.page("/settings")
    def _settings(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("settings", "Settings", bridge):
            settings_page.render(store, settings)

    @ui.page("/models")
    async def _models(token: str | None = None) -> None:
        if not _gate(token):
            _denied()
            return
        with theme.shell("models", "Models", bridge):
            await models_page.render(bridge)


# --------------------------------------------------------------------------- #
# WS push hook for the listener coroutine (spec §9)
# --------------------------------------------------------------------------- #
def refresh_inbox() -> None:
    """Push pending-draft updates to connected clients (spec §9 WS push).

    The runtime/listener coroutine imports and calls this whenever a new draft
    lands so the inbox table re-renders without a manual reload.
    """
    inbox_page.refresh()


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
) -> None:
    """Build the app and start the NiceGUI server (spec §0.7, §9).

    Binds ``127.0.0.1`` only, on the persisted random high port, prints the
    one-shot launch URL to stdout, and runs with ``show=False``.
    """
    cfg = build_app(store, settings, bridge, host=host)
    print(f"OpenClaw Email UI: {cfg['url']}", flush=True)
    log.info("UI listening on %s:%s (token in URL above)", cfg["host"], cfg["port"])
    ui.run(
        host=host,
        port=cfg["port"],
        show=False,
        reload=False,
        storage_secret=cfg["storage_secret"],
        title="OpenClaw Email",
    )
