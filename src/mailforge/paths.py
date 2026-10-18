"""Cross-platform application paths.

Single source of truth for where config, data, and the DB live. Uses
``platformdirs`` so the same code resolves correct locations on Linux
(``~/.config`` / ``~/.local/share``) and Windows (``%APPDATA%`` /
``%LOCALAPPDATA%``). The build spec gives Linux-style defaults; we honour
those on Linux and use the OS-native equivalents elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "mailforge"

#: Single env var that relocates BOTH config and data (``<home>/config`` and
#: ``<home>/data``). Used by ``mailforge demo`` to run against a throwaway
#: tree, and handy for testing a second profile without touching the real one.
HOME_ENV = "MAILFORGE_HOME"


def _expand(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(p)))).resolve()


def app_home() -> Path | None:
    """Explicit application home from ``MAILFORGE_HOME``, if set."""
    value = os.environ.get(HOME_ENV, "").strip()
    return _expand(value) if value else None


def config_dir() -> Path:
    home = app_home()
    d = _expand(home / "config") if home else _expand(user_config_dir(APP_NAME, appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    home = app_home()
    d = _expand(home / "data") if home else _expand(user_data_dir(APP_NAME, appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file() -> Path:
    return config_dir() / "config.toml"


def default_db_path() -> Path:
    return data_dir() / "agent.db"


def runtime_file(name: str) -> Path:
    """Small runtime state files (UI port, launch token) live next to data."""
    return data_dir() / name


def prompt_override_dir() -> Path:
    """User-writable prompt override directory (the 'OpenClaw link' seam).

    OpenClaw (or the user) may drop updated ``<name>.txt`` prompt templates
    here; :func:`mailforge.llm.prompts.load` prefers them over the
    packaged defaults. Absent/empty => packaged defaults are used, so the app
    is fully standalone. Overridable via ``MAILFORGE_PROMPT_DIR``.
    """
    env = os.environ.get("MAILFORGE_PROMPT_DIR")
    if env:
        return _expand(env)
    return config_dir() / "prompts"


# --- Web UI binding (container / reverse-proxy deployments) -------------------
#: By default the UI binds 127.0.0.1 on a random persisted port and answers only
#: ``Host: 127.0.0.1:PORT`` / ``localhost:PORT``. A container cannot be reached
#: that way (the published port is not loopback inside it, and the browser's
#: Host header carries the *published* port), so these three variables relax
#: exactly those three things and nothing else; unset, behaviour is unchanged.
UI_HOST_ENV = "MAILFORGE_UI_HOST"
UI_PORT_ENV = "MAILFORGE_UI_PORT"
UI_ALLOWED_HOSTS_ENV = "MAILFORGE_UI_ALLOWED_HOSTS"


def ui_host() -> str:
    """Interface the UI binds: ``MAILFORGE_UI_HOST`` or ``127.0.0.1``."""
    return os.environ.get(UI_HOST_ENV, "").strip() or "127.0.0.1"


def ui_port_override() -> int | None:
    """Fixed UI port from ``MAILFORGE_UI_PORT`` (None: use the persisted random one)."""
    raw = os.environ.get(UI_PORT_ENV, "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


def ui_extra_allowed_hosts() -> set[str]:
    """Extra ``Host`` header values to accept (comma-separated ``host:port``)."""
    raw = os.environ.get(UI_ALLOWED_HOSTS_ENV, "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def desktop_entry_path() -> Path:
    """``~/.local/share/applications/mailforge.desktop`` (XDG app drawer)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "applications" / "mailforge.desktop"


def icon_path() -> Path:
    """Installed app icon location (hicolor scalable theme)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "icons" / "hicolor" / "scalable" / "apps" / "mailforge.svg"
