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

from . import systemd, winsw


def _is_windows() -> bool:
    return platform.system() == "Windows"


def install_service() -> str:
    """Install the background service for the current OS; return its path."""
    if _is_windows():
        return winsw.install()
    return systemd.install()


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


__all__ = ["install_service", "start_service", "stop_service", "systemd", "winsw"]
