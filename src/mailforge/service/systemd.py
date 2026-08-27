"""Linux systemd user-service installer (build spec §10).

Writes ``~/.config/systemd/user/mailforge.service`` from the vendored
template (``installer/systemd/mailforge.service.tmpl``) with the spec's
hardening flags, then enables linger + the unit so the agent survives logout
and reboot. All ``systemctl``/``loginctl`` calls tolerate the absence of
systemd: instead of crashing we print manual instructions (so the module works
under containers, WSL, and CI).
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

UNIT_NAME = "mailforge.service"


def _systemd_user_dir() -> Path:
    """``$XDG_CONFIG_HOME/systemd/user`` (defaulting to ``~/.config``)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "systemd" / "user"


def _resolve_exec_start() -> str:
    """Absolute ``ExecStart`` command for ``mailforge serve`` (spec §10).

    Prefer the installed ``mailforge`` console script; if it is not on PATH
    (e.g. a dev checkout), fall back to the current interpreter running the
    module entrypoint so the unit is always valid.
    """
    exe = shutil.which("mailforge")
    if exe:
        return f"{exe} serve"
    py = shutil.which("python3") or shutil.which("python") or "python3"
    return f"{py} -m mailforge.cli serve"


def _template() -> str | None:
    """Read the vendored unit template from the repo's ``installer/`` tree.

    Returns None when the tree is absent (e.g. a packaged wheel that did not
    ship installer assets), in which case the inline fallback is used.
    """
    # package is src/mailforge/service -> repo root is three parents up.
    repo_tmpl = (
        Path(__file__).resolve().parents[3]
        / "installer"
        / "systemd"
        / "mailforge.service.tmpl"
    )
    if repo_tmpl.is_file():
        return repo_tmpl.read_text("utf-8")
    return None


def render_unit(exec_start: str | None = None) -> str:
    """Render the unit text from the template (used by tests + install)."""
    exec_start = exec_start or _resolve_exec_start()
    # Inline fallback keeps the exact hardening contract from spec §10 even when
    # the installer/ tree was not packaged with the wheel.
    tmpl = _template() or _INLINE_TEMPLATE
    return tmpl.format(exec_start=exec_start)


def _run(cmd: list[str]) -> bool:
    """Run a command, returning True on success. Never raises."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except FileNotFoundError:
        log.info("Command not found (systemd absent?): %s", " ".join(cmd))
        return False
    except subprocess.CalledProcessError as e:
        log.warning("Command failed (%s): %s", " ".join(cmd), e.stderr.strip())
        return False


def install(unit_dir: Path | None = None) -> str:
    """Write the unit, enable linger, daemon-reload, and enable --now.

    Returns the path of the written unit file. If systemd is unavailable, the
    unit is still written and manual instructions are printed instead of
    crashing.
    """
    target_dir = unit_dir or _systemd_user_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    unit_path = target_dir / UNIT_NAME
    unit_path.write_text(render_unit(), encoding="utf-8")
    log.info("Wrote systemd unit: %s", unit_path)

    user = getpass.getuser()
    have_systemctl = shutil.which("systemctl") is not None
    if not have_systemctl:
        print(
            "systemd not detected. Unit written to:\n"
            f"  {unit_path}\n"
            "To enable it manually on a systemd host, run:\n"
            f"  loginctl enable-linger {user}\n"
            "  systemctl --user daemon-reload\n"
            "  systemctl --user enable --now mailforge"
        )
        return str(unit_path)

    _run(["loginctl", "enable-linger", user])
    _run(["systemctl", "--user", "daemon-reload"])
    if _run(["systemctl", "--user", "enable", "--now", "mailforge"]):
        log.info("mailforge user service enabled and started")
    else:
        print(
            "Unit written but could not enable it automatically. Run:\n"
            "  systemctl --user daemon-reload\n"
            "  systemctl --user enable --now mailforge"
        )
    return str(unit_path)


def start() -> None:
    """Start the user service (spec §10)."""
    if not _run(["systemctl", "--user", "start", "mailforge"]):
        print("Could not start service. Try: systemctl --user start mailforge")


def stop() -> None:
    """Stop the user service (spec §10)."""
    if not _run(["systemctl", "--user", "stop", "mailforge"]):
        print("Could not stop service. Try: systemctl --user stop mailforge")


# Fallback template if the installer/ tree was not packaged with the wheel.
_INLINE_TEMPLATE = """[Unit]
Description=MailForge (local-LLM email triage & auto-draft, no auto-send)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=default.target
"""
