"""Read/unread state <-> the IMAP server's ``\\Seen`` flag.

MailForge records read state locally (``messages.seen``). This module keeps the
server in step, always from an already-open, authenticated listener session so
no extra connection or credential read is made:

* **push** — rows flagged ``seen_sync_pending`` (set by every local read/unread
  change) get ``\\Seen`` set or cleared with ``UID STORE``, for exactly the
  ``(folder, uid)`` the ingest recorded. Success clears the flag; a failure
  keeps it, records the error on the row, and the next listener cycle retries.
  Only rows a person (or a reply send) marked are ever pushed — ingest never
  marks anything read on the server.
* **adopt** — ``UID SEARCH SEEN`` marks mail read elsewhere as read here.
  Rows with an unpushed local change are skipped: the local change wins until
  it has landed. Server-unread never flips a local read (older local reads were
  never pushed, so that would silently un-read history).
"""

from __future__ import annotations

import imaplib
import logging

from imap_tools.errors import ImapToolsError

from ..db.store import Store

log = logging.getLogger(__name__)

PUSH_CHUNK = 200

#: Same set as ``server_delete.IMAP_ERRORS`` (not imported: it would be circular).
IMAP_ERRORS = (ImapToolsError, imaplib.IMAP4.error, OSError, RuntimeError)


def _store_flag(mailbox, uids: list[int], seen: bool) -> None:
    uidset = ",".join(str(u) for u in uids)
    op = "+FLAGS.SILENT" if seen else "-FLAGS.SILENT"
    typ, dat = mailbox.client.uid("STORE", uidset, op, r"(\Seen)")
    if typ != "OK":
        raise ImapToolsError(f"UID STORE {op} failed: {typ} {dat!r}"[:200])


def push_pending(mailbox, store: Store, account_id: int, folder: str) -> tuple[int, str | None]:
    """Push locally changed read state for ``folder`` (selected on ``mailbox``).

    Returns ``(rows pushed, first error or None)``. Never raises.
    """
    rows = store.pending_seen_rows(account_id, folder)
    if not rows:
        return 0, None
    pushed = 0
    first_error: str | None = None
    for want in (True, False):
        group = [r for r in rows if bool(r["seen"]) is want]
        for i in range(0, len(group), PUSH_CHUNK):
            chunk = group[i : i + PUSH_CHUNK]
            try:
                _store_flag(mailbox, [int(r["uid"]) for r in chunk], want)
            except IMAP_ERRORS as e:
                err = f"{type(e).__name__}: {e}"
                first_error = first_error or err
                store.record_seen_push_error([int(r["id"]) for r in chunk], err)
                log.warning("Read-state push failed on %s: %s", folder, err)
                continue
            store.record_seen_pushed((int(r["id"]), int(want)) for r in chunk)
            pushed += len(chunk)
    return pushed, first_error


def adopt_seen(mailbox, store: Store, account_id: int, folder: str) -> int:
    """Adopt the server's ``\\Seen`` for rows without a pending local change."""
    try:
        uids = [int(u) for u in mailbox.uids("SEEN")]
    except IMAP_ERRORS as e:
        log.debug("SEARCH SEEN failed on %s: %s", folder, e)
        return 0
    return store.adopt_server_seen(account_id, folder, uids) if uids else 0
