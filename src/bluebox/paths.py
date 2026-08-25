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

APP_NAME = "bluebox"

#: Single env var that relocates BOTH config and data (``<home>/config`` and
#: ``<home>/data``). Used by ``bluebox demo`` to run against a throwaway
#: tree, and handy for testing a second profile without touching the real one.
HOME_ENV = "BLUEBOX_HOME"


def _expand(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(p)))).resolve()


def app_home() -> Path | None:
    """Explicit application home from ``BLUEBOX_HOME``, if set."""
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
    here; :func:`bluebox.llm.prompts.load` prefers them over the
    packaged defaults. Absent/empty => packaged defaults are used, so the app
    is fully standalone. Overridable via ``BLUEBOX_PROMPT_DIR``.
    """
    env = os.environ.get("BLUEBOX_PROMPT_DIR")
    if env:
        return _expand(env)
    return config_dir() / "prompts"


def desktop_entry_path() -> Path:
    """``~/.local/share/applications/bluebox.desktop`` (XDG app drawer)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "applications" / "bluebox.desktop"


def icon_path() -> Path:
    """Installed app icon location (hicolor scalable theme)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "icons" / "hicolor" / "scalable" / "apps" / "bluebox.svg"
