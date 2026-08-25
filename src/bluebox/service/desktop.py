"""Desktop application entry (XDG ``.desktop`` + icon).

Puts BlueBox in the app drawer/launcher. Clicking it runs
``bluebox open``, which makes sure the background service is up and then
opens an *authenticated* browser session to the localhost approval UI (see
``ui.app.launcher_key`` / the ``/launch`` route).

Linux only in practice (freedesktop). On Windows/macOS this is a no-op that
prints guidance — the Start-menu/Dock shortcut is handled by the platform
installer (Inno Setup) there.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess

from ..paths import desktop_entry_path, icon_path

log = logging.getLogger(__name__)

# A small, dependency-free envelope-with-claw mark. Scalable SVG lives in the
# hicolor theme so the launcher renders it at any size.
_ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#1f2937"/>
  <rect x="22" y="36" width="84" height="60" rx="8" fill="#f9fafb" stroke="#9ca3af" stroke-width="2"/>
  <path d="M22 44 L64 74 L106 44" fill="none" stroke="#2563eb" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="64" cy="92" r="13" fill="#1f2937"/>
  <circle cx="59" cy="90" r="3" fill="#38bdf8"/>
  <circle cx="69" cy="90" r="3" fill="#38bdf8"/>
  <path d="M55 98 q9 6 18 0" fill="none" stroke="#38bdf8" stroke-width="2.5" stroke-linecap="round"/>
</svg>
"""


def _desktop_entry(exec_cmd: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=BlueBox\n"
        "GenericName=Email triage & draft assistant\n"
        "Comment=Local-LLM email triage and human-approved drafts (no auto-send)\n"
        f"Exec={exec_cmd}\n"
        "Icon=bluebox\n"
        "Terminal=false\n"
        "Categories=Network;Email;\n"
        "Keywords=email;mail;llm;openclaw;triage;\n"
        "StartupNotify=true\n"
    )


def _exec_cmd() -> str:
    """``bluebox open`` using the absolute console-script path if known."""
    exe = shutil.which("bluebox")
    return f"{exe} open" if exe else "bluebox open"


def install() -> str:
    """Write the icon + ``.desktop`` file and refresh the desktop database.

    Returns the path of the written ``.desktop`` file (or a message on
    unsupported platforms). Never raises on best-effort steps.
    """
    if platform.system() != "Linux":
        msg = (
            "Desktop entry is Linux-only here; on this OS the platform installer "
            "creates the Start-menu/Dock shortcut."
        )
        log.info(msg)
        return msg

    icon = icon_path()
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_text(_ICON_SVG, encoding="utf-8")

    entry = desktop_entry_path()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(_desktop_entry(_exec_cmd()), encoding="utf-8")
    os.chmod(entry, 0o644)

    # Best-effort cache refresh so the icon/entry show up immediately.
    apps_dir = str(entry.parent)
    icons_root = str(icon.parents[3])  # .../icons/hicolor/scalable/apps -> .../icons
    for cmd in (
        ["update-desktop-database", apps_dir],
        ["gtk-update-icon-cache", "-f", "-t", icons_root],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, check=False, capture_output=True, text=True)
            except Exception as e:  # pragma: no cover - environment dependent
                log.debug("%s failed: %s", cmd[0], e)

    log.info("Desktop entry installed: %s", entry)
    return str(entry)


def uninstall() -> None:
    """Remove the desktop entry + icon (best-effort)."""
    for p in (desktop_entry_path(), icon_path()):
        try:
            p.unlink(missing_ok=True)
        except OSError as e:  # pragma: no cover
            log.debug("could not remove %s: %s", p, e)
