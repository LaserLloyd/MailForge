"""The one chokepoint every outgoing message passes through.

Three call sites transmit mail — the draft-approval page, the Compose page,
and the agent bridge's ``send`` op — and each of them used to call
:func:`mailforge.mail.smtp_sender.send_email` directly, leaving nothing behind
but audit-log lines. :func:`transmit_and_record` wraps that call so all three
produce the same durable evidence:

  1. a ``sent_messages`` row is opened BEFORE the socket, outcome ``UNKNOWN``;
  2. SMTP runs;
  3. the row is stamped ``SENT`` (with the Message-ID) or ``FAILED`` (with the
     error) — and the exception is re-raised unchanged, so every existing
     caller's failure handling still works;
  4. on success only, a copy is appended to the mailbox's IMAP Sent folder,
     best effort, recorded on the same row.

A row still reading ``UNKNOWN`` means the process died between (1) and (3).
That is a real state and the Outbox shows it as one — it is never rendered as
sent.

Security: this module transmits nothing on its own. It is called only from
code paths that have already asserted a human approval record and the
no-autosend invariant.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _account_for_address(settings: Any, from_addr: str) -> Any | None:
    addr = str(from_addr or "").strip().lower()
    for account in getattr(settings, "imap_accounts", []) or []:
        if str(account.username or "").strip().lower() == addr:
            return account
    return None


def transmit_and_record(
    store: Any,
    settings: Any,
    *,
    smtp_cfg: Any,
    secret: Any,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    html_body: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    origin: str = "ui_compose",
    site_id: str | None = None,
    draft_id: int | None = None,
    authorization_id: int | None = None,
    append_to_sent_folder: bool = True,
) -> int:
    """Send one message and record it. Returns the ``sent_messages`` row id.

    Re-raises whatever :func:`send_email` raises, after marking the row FAILED.
    """
    # Imported inside the call so tests (and the security invariant tests) can
    # monkeypatch the module attribute, as they do for the direct callers.
    from . import smtp_sender

    record_id = 0
    try:
        record_id = int(
            store.begin_send_record(
                from_addr,
                to_addr,
                subject,
                body,
                origin=origin,
                site_id=site_id,
                draft_id=draft_id,
                authorization_id=authorization_id,
            )
        )
    except Exception:  # noqa: BLE001 - logging must never block a send
        log.exception("could not open outbox record; sending anyway")

    try:
        result = smtp_sender.send_email(
            smtp_cfg=smtp_cfg,
            secret=secret,
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
            html_body=html_body,
        )
    except Exception as e:
        if record_id:
            try:
                store.finish_send_record(record_id, "FAILED", error_text=str(e))
            except Exception:  # noqa: BLE001
                log.exception("could not record send failure")
        raise

    message_id = str(getattr(result, "message_id", "") or "")
    raw = getattr(result, "raw", b"") or b""
    if record_id:
        try:
            store.finish_send_record(record_id, "SENT", smtp_message_id=message_id or None)
        except Exception:  # noqa: BLE001
            log.exception("could not record send success")

    if record_id and append_to_sent_folder:
        _append_copy(store, settings, record_id, from_addr, raw)
    return record_id


def _append_copy(
    store: Any, settings: Any, record_id: int, from_addr: str, raw: bytes
) -> None:
    """Best-effort IMAP Sent-folder copy; a failure is a note, not a send error."""
    account = _account_for_address(settings, from_addr)
    if account is None:
        _note(store, record_id, "SKIPPED", "", "no configured IMAP mailbox for this sender")
        return
    if not raw:
        _note(store, record_id, "SKIPPED", "", "no raw message bytes available")
        return
    try:
        from .sent_append import append_to_sent

        status, folder, note = append_to_sent(account, raw)
    except Exception as e:  # noqa: BLE001
        status, folder, note = "FAILED", "", str(e)[:200]
    _note(store, record_id, status, folder, note)


def _note(store: Any, record_id: int, status: str, folder: str, note: str) -> None:
    try:
        store.record_send_append(record_id, status, folder=folder or None, note=note or None)
    except Exception:  # noqa: BLE001
        log.exception("could not record Sent-folder append status")
