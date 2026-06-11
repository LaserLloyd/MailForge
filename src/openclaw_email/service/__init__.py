"""OS-dispatching service controller (build spec §10).

A single ``service install`` / ``start`` / ``stop`` surface (spec §10: the
user-facing path is identical on both OSes). Linux delegates to ``systemd.py``
(systemd user unit + ``loginctl enable-linger``); Windows delegates to
``winsw.py`` (bundled WinSW-x64.exe wrapping ``uv tool run``, NOT NSSM).

Each entrypoint returns the path of the written unit/descriptor so the CLI can
report it. All modules import on any platform (Windows-only ops degrade to a
clear message), so this package is testable on Linux.
"""

from __future__ import annotations

import platform

from . import desktop, systemd, winsw


def _is_windows() -> bool:
    return platform.system() == "Windows"


def install_service() -> str:
    """Install the background service for the current OS; return its path.

    On Linux this also installs the app-drawer ``.desktop`` entry + icon so the
    user gets a clickable launcher (best-effort; failures don't block the
    service install).
    """
    if _is_windows():
        return winsw.install()
    path = systemd.install()
    try:
        desktop.install()
    except Exception:  # pragma: no cover - best-effort
        pass
    return path


def install_desktop_entry() -> str:
    """Install just the app-drawer launcher entry + icon."""
    return desktop.install()


def start_service() -> None:
    """Start the installed background service."""
    if _is_windows():
        winsw.start()
    else:
        systemd.start()


def stop_service() -> None:
    """Stop the installed background service."""
    if _is_windows():
        winsw.stop()
    else:
        systemd.stop()


__all__ = [
    "install_service",
    "install_desktop_entry",
    "start_service",
    "stop_service",
    "desktop",
    "systemd",
    "winsw",
]
