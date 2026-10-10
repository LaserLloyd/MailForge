"""Deleting a message at the IMAP provider.

Everything else in this app is read-only against the mailbox, so this module is
the ONLY place that removes provider mail, and it is deliberately narrow:

* it works from ``(folder, uid)`` pairs the ingest already recorded — never
  from a search, so it can only ever touch messages this app has stored;
* the default mode is ``trash``: a server-side ``UID MOVE`` into the account's
  Trash/Deleted folder, which the provider's own web UI can still undo. An
  account with no trash folder is refused rather than silently expunged;
  permanent removal happens only in ``expunge`` mode;
* it only ever expunges the exact UIDs it was given (``UID EXPUNGE``, which
  needs UIDPLUS). A bare ``EXPUNGE`` would also destroy every message another
  client had flagged ``\\Deleted`` in that folder, so a server without
  MOVE/UIDPLUS is refused instead;
* before touching a UID it re-reads that message's ``Message-ID`` and skips
  any UID that now holds a different message (a mailbox rebuild can reassign
  UIDs), so a stale locator can never delete someone else's mail;
* a per-account failure is returned, never raised — one unreachable mailbox
  must not stop the others, and the caller records the error against the
  messages so the UI can say "deleted locally, not yet at the provider".
"""

from __future__ import annotations

import imaplib
import logging
from dataclasses import dataclass, field

from imap_tools import AND, MailBox
from imap_tools.errors import ImapToolsError
from imap_tools.utils import encode_folder

from ..config import IMAPAccount
from .imap_listener import _login, open_mailbox

log = logging.getLogger(__name__)

#: Checked in order; the first folder the server reports wins. Junk is NOT a
#: trash folder: "deleted" mail must not reappear among spam.
TRASH_FOLDER_CANDIDATES = (
    "Trash",
    "Deleted Items",
    "Deleted Messages",
    "[Gmail]/Trash",
    "INBOX.Trash",
)

#: Everything imaplib/imap_tools/sockets can raise mid-session.
#: ``imaplib.IMAP4.error`` (and ``.abort``) subclass Exception, not OSError.
IMAP_ERRORS = (ImapToolsError, imaplib.IMAP4.error, OSError, RuntimeError)

NO_TRASH_ERROR = (
    "no trash folder found at the provider; set security.server_delete_mode = "
    '"expunge" to allow permanent deletion'
)


#: Must equal ``Store.UID_MISMATCH_ERROR``: rows recorded with it are not retried.
UID_MISMATCH_ERROR = "uid no longer matches the stored message"


class DeleteRefused(RuntimeError):
    """The server cannot do a narrowly targeted delete, so nothing was done."""


@dataclass
class DeleteOutcome:
    """What actually happened at the provider for one account."""

    account: str
    mode: str = "off"
    deleted_ids: list[int] = field(default_factory=list)
    failed_ids: list[int] = field(default_factory=list)
    error: str = ""
    destination: str = ""
    #: Per-message error for ``failed_ids`` (``error`` is only the first one).
    #: A UID-mismatch skip must not share its error with a transport failure
    #: in the same call: one is never retried, the other must be.
    errors: dict[int, str] = field(default_factory=dict)


def find_trash_folder(mailbox: MailBox) -> str | None:
    """The account's trash folder, by well-known name then by \\Trash flag."""
    try:
        folders = list(mailbox.folder.list())
    except IMAP_ERRORS as e:
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


def _norm_msgid(value: object) -> str:
    return "".join(str(value or "").split()).strip("<>").lower()


def _check(result: tuple, what: str) -> None:
    typ = result[0] if result else None
    if typ != "OK":
        detail = result[1] if result and len(result) > 1 else ""
        raise DeleteRefused(f"{what} failed: {typ} {detail!r}"[:200])


def _capabilities(mailbox: MailBox) -> set[str]:
    return {str(c).upper() for c in (getattr(mailbox.client, "capabilities", ()) or ())}


def _verify_targets(
    mailbox: MailBox,
    pairs: list[tuple[int, int]],
    expected: dict[int, str],
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Split ``pairs`` into (still ours, already gone, now someone else's).

    Reads only headers (``BODY.PEEK``), so nothing is marked seen.
    """
    uids = [str(uid) for _mid, uid in pairs]
    found: dict[int, str] = {}
    for msg in mailbox.fetch(
        AND(uid=uids), headers_only=True, mark_seen=False, bulk=True
    ):
        vals = (msg.headers or {}).get("message-id") or ("",)
        try:
            found[int(msg.uid)] = _norm_msgid(vals[0])
        except (TypeError, ValueError):
            continue
    keep: list[tuple[int, int]] = []
    gone: list[int] = []
    mismatched: list[int] = []
    for mid, uid in pairs:
        if int(uid) not in found:
            gone.append(mid)
            continue
        want = _norm_msgid(expected.get(mid))
        if want and found[int(uid)] != want:
            mismatched.append(mid)
            continue
        keep.append((mid, uid))
    return keep, gone, mismatched


def _uid_expunge(mailbox: MailBox, uidset: str, caps: set[str]) -> None:
    if "UIDPLUS" not in caps:
        raise DeleteRefused(
            "server lacks UIDPLUS; refusing a folder-wide EXPUNGE that would also "
            "remove messages other clients flagged as deleted"
        )
    _check(mailbox.client.uid("STORE", uidset, "+FLAGS.SILENT", r"(\Deleted)"), "UID STORE")
    _check(mailbox.client.uid("EXPUNGE", uidset), "UID EXPUNGE")


def _uid_move(mailbox: MailBox, uidset: str, destination: str, caps: set[str]) -> None:
    if "MOVE" in caps:
        _check(mailbox.client.uid("MOVE", uidset, encode_folder(destination)), "UID MOVE")
        return
    if "UIDPLUS" not in caps:
        raise DeleteRefused(
            "server supports neither MOVE nor UIDPLUS; refusing a COPY + folder-wide "
            "EXPUNGE"
        )
    _check(mailbox.client.uid("COPY", uidset, encode_folder(destination)), "UID COPY")
    _uid_expunge(mailbox, uidset, caps)


def delete_uids(
    account: IMAPAccount,
    targets: dict[str, list[tuple[int, int]]],
    *,
    mode: str = "trash",
    expected_ids: dict[int, str] | None = None,
) -> DeleteOutcome:
    """Remove ``{folder: [(message_id, uid), ...]}`` from one account's mailbox.

    Blocking network I/O — call it off the event loop. ``message_id`` is carried
    through untouched so the caller can record exactly which local rows the
    provider actually accepted. ``expected_ids`` maps a local message id to the
    ``Message-ID`` header stored at ingest; a UID whose current message has a
    different header is left alone and reported as failed.
    """
    outcome = DeleteOutcome(account=account.name, mode=mode)
    expected = dict(expected_ids or {})
    all_ids = [mid for pairs in targets.values() for mid, _uid in pairs]
    if mode == "off" or not all_ids:
        outcome.mode = "off"
        return outcome

    def _fail(ids: list[int], error: str) -> None:
        for i in ids:
            if i not in outcome.failed_ids:
                outcome.failed_ids.append(i)
                outcome.errors[i] = error
        outcome.error = outcome.error or error

    try:
        with open_mailbox(account.host, account.port) as mailbox:
            _login(mailbox, account)  # also refreshes post-login CAPABILITY
            caps = _capabilities(mailbox)
            trash = find_trash_folder(mailbox) if mode == "trash" else None
            if mode == "trash" and trash is None:
                log.warning("No trash folder on '%s'; provider delete refused.", account.name)
                _fail(all_ids, NO_TRASH_ERROR)
                return outcome
            for folder, pairs in targets.items():
                if not pairs:
                    continue
                ids = [mid for mid, _uid in pairs]
                try:
                    mailbox.folder.set(folder)
                    keep, gone, mismatched = _verify_targets(mailbox, list(pairs), expected)
                    # Nothing at that UID any more (another client deleted it):
                    # the provider copy is already gone.
                    outcome.deleted_ids.extend(gone)
                    if mismatched:
                        log.error(
                            "Provider delete on '%s'/%s skipped %d UID(s) that now hold a "
                            "different message.", account.name, folder, len(mismatched),
                        )
                        _fail(mismatched, UID_MISMATCH_ERROR)
                    if not keep:
                        continue
                    uidset = ",".join(str(uid) for _mid, uid in keep)
                    # Moving a message into the folder it already lives in is a
                    # no-op on some servers and an error on others; expunging is
                    # the honest action there.
                    if trash is not None and folder != trash:
                        _uid_move(mailbox, uidset, trash, caps)
                        outcome.destination = trash
                        outcome.mode = "trash"
                    else:
                        _uid_expunge(mailbox, uidset, caps)
                        outcome.mode = "expunge"
                    outcome.deleted_ids.extend(mid for mid, _uid in keep)
                except IMAP_ERRORS as e:
                    log.warning(
                        "Provider delete failed on '%s'/%s: %s", account.name, folder, e
                    )
                    _fail(
                        [i for i in ids if i not in outcome.deleted_ids],
                        f"{type(e).__name__}: {e}",
                    )
    except IMAP_ERRORS as e:
        log.warning("Provider delete could not complete for '%s': %s", account.name, e)
        # Keep what the provider already accepted; only the rest failed.
        _fail(
            [mid for mid in all_ids if mid not in outcome.deleted_ids],
            f"{type(e).__name__}: {e}",
        )
    return outcome
