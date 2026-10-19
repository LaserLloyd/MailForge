"""Lockout recovery: reset the encrypted credential store and the UI access keys.

MailForge has no user table. Two things guard it, and either can be lost:

* the **credential store** - the IMAP/SMTP passwords. On a desktop that is the OS
  keyring (unlocked by your OS login); on a headless Linux box or in the Docker image
  it is an encrypted file (``keyrings.cryptfile``) opened with a password you chose
  (``MAILFORGE_KEYRING_PASSWORD`` in Docker). Forget that password and the file can
  never be read again - the only way forward is to wipe it and enter the mailbox
  passwords again. The mail database and ``config.toml`` are NOT touched.
* the **UI access keys** - the launcher key (``/launch?k=``) and the secret that signs
  browser session cookies. Rotating them signs every browser out and retires every
  saved launcher URL; the next ``mailforge serve`` prints a fresh one-shot login URL.

Two ways in, both of which need access to the machine, never the network:

* the CLI: ``mailforge unlock reset`` and ``mailforge token rotate``;
* the ``RESET_LOGIN`` sentinel: a file dropped into the data directory, read ONCE
  when ``mailforge serve`` starts and deleted before anything is reset
  (:func:`consume_reset_sentinel`). Nothing in the web app reads it, and no route
  can create it.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from . import secrets as secrets_mod

log = logging.getLogger(__name__)

SENTINEL_NAME = "RESET_LOGIN"
#: Sentinel actions (the file's whole content, case-insensitive; empty == all).
ACTION_ALL = "all"
ACTION_UNLOCK = "unlock"
ACTION_UI = "ui"
_ACTIONS = {"": ACTION_ALL, ACTION_ALL: ACTION_ALL, ACTION_UNLOCK: ACTION_UNLOCK, ACTION_UI: ACTION_UI}
_BACKUPS_KEPT = 3


# --------------------------------------------------------------------------- #
# Where am I acting?
# --------------------------------------------------------------------------- #
def sentinel_paths() -> list[Path]:
    """Every place a ``RESET_LOGIN`` file is looked for, most specific first.

    Native install: ``<data dir>/RESET_LOGIN``. When ``MAILFORGE_HOME`` is set (the
    Docker image sets it to the volume root ``/data``) the home itself is also
    checked, so ``<volume>/RESET_LOGIN`` works without knowing the ``data/``
    sub-folder.
    """
    out: list[Path] = []
    home = paths.app_home()
    if home is not None:
        out.append(home / SENTINEL_NAME)
    out.append(paths.data_dir() / SENTINEL_NAME)
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def acting_on_line() -> str:
    """One line naming the config/data directories a command is acting on."""
    home = paths.app_home()
    source = f"MAILFORGE_HOME={home}" if home else "OS default (no MAILFORGE_HOME)"
    return f"MailForge data: config={paths.config_file()} data={paths.data_dir()} [{source}]"


@dataclass
class StoreInfo:
    backend: str
    path: Path | None  # None: an OS keyring, nothing on disk to wipe
    exists: bool = False
    size: int = 0
    note: str = ""

    @property
    def file_backed(self) -> bool:
        return self.path is not None


def _backend():
    import keyring

    secrets_mod._ensure_backend()
    return keyring.get_keyring()


def store_info(backend=None) -> StoreInfo:
    """Describe the active credential store WITHOUT unlocking or prompting."""
    be = backend if backend is not None else _backend()
    name = be.__class__.__name__
    raw = getattr(be, "file_path", None)
    if raw:
        p = Path(str(raw))
        exists = p.is_file()
        return StoreInfo(name, p, exists, p.stat().st_size if exists else 0)
    return StoreInfo(name, None, note="OS keyring: protected by your OS login, no separate unlock password")


def probe_unlock(backend=None) -> str:
    """Can the store be opened right now, without asking anyone for a password?"""
    from keyring.errors import KeyringError

    be = backend if backend is not None else _backend()
    info = store_info(be)
    if not info.file_backed:
        return "not applicable (OS keyring)"
    if not info.exists:
        return "no store yet (created on the first credential you save)"
    env_key = getattr(be, "_env_key", None)
    if env_key is None:
        return "not checked (the file is opened with a typed password; nothing is prompted here)"
    if not str(env_key).strip():
        return "LOCKED: no password in MAILFORGE_KEYRING_PASSWORD / MAILFORGE_KEYRING_PASSWORD_FILE"
    try:
        # A lookup of a missing entry never unlocks the file; _init_keyring is the
        # step that verifies the password against the store (and raises when wrong).
        init = getattr(be, "_init_keyring", None)
        if init is not None:
            init()
        else:
            be.get_password("mailforge.probe", "probe")
    except KeyringError as e:
        return f"LOCKED: {str(e)[:200]}"
    except Exception as e:  # noqa: BLE001 - never let a status check crash
        return f"LOCKED: {e.__class__.__name__}"
    return "unlocked"


# --------------------------------------------------------------------------- #
# Reset the credential store
# --------------------------------------------------------------------------- #
@dataclass
class ResetReport:
    kind: str  # "file" | "entries" | "nothing"
    path: Path | None = None
    backup: Path | None = None
    removed: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.kind == "file":
            tail = f" (kept as {self.backup.name})" if self.backup else " (no backup kept)"
            return f"credential store file removed: {self.path}{tail}"
        if self.kind == "entries":
            return f"{self.removed} mailbox credential(s) removed from the OS keyring"
        return "no credential store was present; nothing to remove"


def _prune_backups(path: Path) -> None:
    olds = sorted(path.parent.glob(f"{path.name}.reset-*.bak"), key=lambda p: p.name)
    for old in olds[:-_BACKUPS_KEPT]:
        try:
            old.unlink()
        except OSError:
            pass


def reset_credential_store(settings, *, backup: bool = True, backend=None) -> ResetReport:
    """Wipe the stored mailbox passwords. Config, mail database and audit log stay.

    File-backed store (cryptfile): the file is renamed to
    ``<name>.reset-<UTC timestamp>.bak`` (``backup=False`` deletes it), so the next
    credential saved creates a fresh store under whatever password is then supplied.
    OS keyring: the ``mailforge.imap.<name>`` / ``mailforge.smtp.<username>`` entries
    of the configured mailboxes are deleted (other applications' entries are never
    touched).
    """
    be = backend if backend is not None else _backend()
    info = store_info(be)
    if info.file_backed:
        assert info.path is not None
        if not info.exists:
            return ResetReport("nothing", info.path)
        bak: Path | None = None
        if backup:
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bak = info.path.with_name(f"{info.path.name}.reset-{stamp}.bak")
            os.replace(info.path, bak)
            _prune_backups(info.path)
        else:
            info.path.unlink()
        if hasattr(be, "_keyring_key"):
            be._keyring_key = None  # forget a password cached for the old file
        return ResetReport("file", info.path, bak)

    removed = 0
    for acct in getattr(settings, "imap_accounts", []):
        if secrets_mod.get_secret("imap", acct.name) is not None:
            secrets_mod.delete_secret("imap", acct.name)
            removed += 1
        still_used = any(
            o is not acct and o.username.lower() == acct.username.lower()
            for o in settings.imap_accounts
        )
        if not still_used and secrets_mod.get_secret("smtp", acct.username) is not None:
            secrets_mod.delete_secret("smtp", acct.username)
            removed += 1
    return ResetReport("entries" if removed else "nothing", None, None, removed)


# --------------------------------------------------------------------------- #
# UI access keys
# --------------------------------------------------------------------------- #
def current_launch_url(port: int | None = None) -> str:
    """The reusable launcher URL (``/launch?k=``) read from the 0600 key file."""
    from .ui.app import launcher_key, persistent_port

    p = port if port is not None else persistent_port()
    return f"http://127.0.0.1:{p}/launch?k={launcher_key()}"


def rotate_ui_access() -> None:
    """New launcher key + new cookie-signing secret (files in the data dir).

    A running server holds both in memory; the change applies at its next start.
    """
    from .ui.app import rotate_launcher_key, rotate_storage_secret

    rotate_launcher_key()
    rotate_storage_secret()


# --------------------------------------------------------------------------- #
# The RESET_LOGIN sentinel
# --------------------------------------------------------------------------- #
@dataclass
class SentinelResult:
    path: Path
    action: str | None  # None: unrecognised content, nothing was reset
    store: ResetReport | None = None
    ui_rotated: bool = False


def consume_reset_sentinel(settings) -> list[SentinelResult]:
    """Read-once handling of ``RESET_LOGIN``; called at the top of ``mailforge serve``.

    The file is deleted BEFORE anything is reset, so a crash half way can never replay
    it; if it cannot be deleted (read-only mount) NOTHING is done and an error is
    logged, because it would otherwise fire on every restart. Content (whitespace
    ignored, case-insensitive): empty or ``all`` = wipe the credential store and
    rotate the UI access keys; ``unlock`` = credential store only; ``ui`` = UI access
    keys only. Anything else is ignored (and the file still consumed) so a typo can
    never wipe credentials. The content is never logged.
    """
    results: list[SentinelResult] = []
    for path in sentinel_paths():
        try:
            if path.is_symlink() or not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log.error("RESET_LOGIN at %s could not be read (%s); ignoring it", path, e)
            continue
        try:
            path.unlink()
        except OSError as e:
            log.error(
                "RESET_LOGIN at %s could not be deleted (%s); NOT acting on it, because it "
                "would replay on every start. Make the data directory writable and retry.",
                path,
                e,
            )
            continue
        action = _ACTIONS.get(raw.strip().lower())
        if action is None:
            log.error(
                "RESET_LOGIN at %s consumed but its content (%d bytes) is not one of: "
                "empty, all, unlock, ui. Nothing was reset.",
                path,
                len(raw.encode("utf-8")),
            )
            results.append(SentinelResult(path, None))
            continue
        res = SentinelResult(path, action)
        try:
            if action in (ACTION_ALL, ACTION_UNLOCK):
                res.store = reset_credential_store(settings)
            if action in (ACTION_ALL, ACTION_UI):
                rotate_ui_access()
                res.ui_rotated = True
        except Exception as e:  # noqa: BLE001 - report, do not stop the service starting
            log.error("RESET_LOGIN (%s) failed part-way: %s", action, e)
        log.warning(
            "RESET_LOGIN consumed (%s): %s; UI access keys %s. Mail database and config.toml "
            "untouched. Re-enter mailbox passwords with 'mailforge account-passwd <name>'.",
            action,
            res.store.summary() if res.store else "credential store untouched",
            "rotated" if res.ui_rotated else "unchanged",
        )
        results.append(res)
    return results
