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

APP_NAME = "openclaw-email"


def _expand(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(p)))).resolve()


def config_dir() -> Path:
    d = _expand(user_config_dir(APP_NAME, appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    d = _expand(user_data_dir(APP_NAME, appauthor=False))
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
    here; :func:`openclaw_email.llm.prompts.load` prefers them over the
    packaged defaults. Absent/empty => packaged defaults are used, so the app
    is fully standalone. Overridable via ``OPENCLAW_EMAIL_PROMPT_DIR``.
    """
    env = os.environ.get("OPENCLAW_EMAIL_PROMPT_DIR")
    if env:
        return _expand(env)
    return config_dir() / "prompts"


def desktop_entry_path() -> Path:
    """``~/.local/share/applications/openclaw-email.desktop`` (XDG app drawer)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "applications" / "openclaw-email.desktop"


def icon_path() -> Path:
    """Installed app icon location (hicolor scalable theme)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "icons" / "hicolor" / "scalable" / "apps" / "openclaw-email.svg"
