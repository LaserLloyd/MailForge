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
