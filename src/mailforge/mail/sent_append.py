"""Append a transmitted message to the account's IMAP Sent folder.

SMTP hands a message to the relay; it does NOT put a copy in your mailbox.
Without this step a message sent from MailForge is invisible in every other
mail client — the phone, the webmail, the desktop app all show a thread with
no reply in it.

The append is deliberately BEST EFFORT. It runs after a successful SMTP
transmission and can never turn a delivered message into a failed one: a
failure here is recorded on the outbox row as a note ("the mail went out, the
provider copy did not"), which is the truth.

Folder discovery reuses the listener's ``_find_sent_folder`` (the RFC 6154
``\\Sent`` names most providers use, then any folder with "sent" in its name),
falling back to plain ``"Sent"`` when the folder list cannot be read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from imap_tools import MailBox

from ..config import IMAPAccount

log = logging.getLogger(__name__)

FALLBACK_SENT_FOLDER = "Sent"


def append_to_sent(account: IMAPAccount, raw: bytes) -> tuple[str, str, str]:
    """Append ``raw`` to ``account``'s Sent folder.

    Returns ``(status, folder, note)`` where status is ``OK``/``SKIPPED``/
    ``FAILED``. Never raises: every failure mode is a returned status, because
    the caller has already delivered the mail and must not treat this as a
    send failure.
    """
    from .imap_listener import SOCKET_TIMEOUT_S, _find_sent_folder, _login

    if not raw:
        return ("SKIPPED", "", "no raw message bytes to append")
    try:
        with MailBox(account.host, account.port, timeout=SOCKET_TIMEOUT_S) as mailbox:
            _login(mailbox, account)
            folder = _find_sent_folder(mailbox) or FALLBACK_SENT_FOLDER
            mailbox.append(
                raw,
                folder,
                dt=datetime.now(timezone.utc),
                flag_set=["\\Seen"],
            )
            return ("OK", folder, "")
    except Exception as e:  # noqa: BLE001 - best effort by design
        log.warning("Sent-folder append failed for '%s': %s", account.name, e)
        return ("FAILED", "", str(e)[:200])
