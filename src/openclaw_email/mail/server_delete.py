"""Deleting a message at the IMAP provider.

Everything else in this app is read-only against the mailbox, so this module is
the ONLY place that removes provider mail, and it is deliberately narrow:

* it works from ``(folder, uid)`` pairs the ingest already recorded — never
  from a search, so it can only ever touch messages this app has stored;
* the default mode is ``trash``: a server-side MOVE into the account's
  Trash/Deleted folder, which the provider's own web UI can still undo. An
  EXPUNGE (unrecoverable) happens only in ``expunge`` mode, or as the fallback
  when the server exposes no trash folder at all;
* a per-account failure is returned, never raised — one unreachable mailbox
  must not stop the others, and the caller records the error against the
  messages so the UI can say "deleted locally, not yet at the provider".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from imap_tools import MailBox
from imap_tools.errors import ImapToolsError

from ..config import IMAPAccount
from .imap_listener import SOCKET_TIMEOUT_S, _login

log = logging.getLogger(__name__)

#: Checked in order; the first folder the server reports wins.
TRASH_FOLDER_CANDIDATES = (
    "Trash",
    "Deleted Items",
    "Deleted Messages",
    "[Gmail]/Trash",
    "INBOX.Trash",
    "Junk",
)


@dataclass
class DeleteOutcome:
    """What actually happened at the provider for one account."""

    account: str
    mode: str = "off"
    deleted_ids: list[int] = field(default_factory=list)
    failed_ids: list[int] = field(default_factory=list)
    error: str = ""
    destination: str = ""


def find_trash_folder(mailbox: MailBox) -> str | None:
    """The account's trash folder, by well-known name then by \\Trash flag."""
    try:
        folders = list(mailbox.folder.list())
    except ImapToolsError as e:
        log.debug("folder list failed: %s", e)
        return None
    names = {f.name for f in folders}
    for candidate in TRASH_FOLDER_CANDIDATES:
        if candidate in names:
            return candidate
    for folder in folders:
        flags = {str(f).lower() for f in (getattr(folder, "flags", None) or ())}
        if "\\trash" in flags:
            return folder.name
    for name in names:
        if "trash" in name.lower() or "deleted" in name.lower():
            return name
    return None


def delete_uids(
    account: IMAPAccount,
    targets: dict[str, list[tuple[int, int]]],
    *,
    mode: str = "trash",
) -> DeleteOutcome:
    """Remove ``{folder: [(message_id, uid), ...]}`` from one account's mailbox.

    Blocking network I/O — call it off the event loop. ``message_id`` is carried
    through untouched so the caller can record exactly which local rows the
    provider actually accepted.
    """
    outcome = DeleteOutcome(account=account.name, mode=mode)
    all_ids = [mid for pairs in targets.values() for mid, _uid in pairs]
    if mode == "off" or not all_ids:
        outcome.mode = "off"
        return outcome
    try:
        with MailBox(account.host, account.port, timeout=SOCKET_TIMEOUT_S) as mailbox:
            _login(mailbox, account)
            trash = find_trash_folder(mailbox) if mode == "trash" else None
            if mode == "trash" and trash is None:
                log.warning(
                    "No trash folder on '%s'; falling back to expunge.", account.name
                )
            for folder, pairs in targets.items():
                if not pairs:
                    continue
                uids = [str(uid) for _mid, uid in pairs]
                ids = [mid for mid, _uid in pairs]
                try:
                    mailbox.folder.set(folder)
                    # Moving a message into the folder it already lives in is a
                    # no-op on some servers and an error on others; expunging is
                    # the honest action there.
                    if trash is not None and folder != trash:
                        mailbox.move(uids, trash)
                        outcome.destination = trash
                        outcome.mode = "trash"
                    else:
                        mailbox.delete(uids)  # \Deleted + EXPUNGE
                        outcome.mode = "expunge"
                    outcome.deleted_ids.extend(ids)
                except (ImapToolsError, OSError) as e:
                    log.warning(
                        "Provider delete failed on '%s'/%s: %s", account.name, folder, e
                    )
                    outcome.failed_ids.extend(ids)
                    outcome.error = outcome.error or f"{type(e).__name__}: {e}"
    except (ImapToolsError, OSError, RuntimeError) as e:
        log.warning("Provider delete could not connect to '%s': %s", account.name, e)
        outcome.failed_ids = [mid for mid in all_ids if mid not in outcome.deleted_ids]
        outcome.deleted_ids = []
        outcome.error = f"{type(e).__name__}: {e}"
    return outcome
