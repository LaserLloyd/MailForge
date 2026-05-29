"""IMAP IDLE listener with reconnect/backoff (spec §3 Phase 2, §5 state machine).

One :class:`IMAPListener` per account. The thread-based ``run()`` loop:

  * connects (password or XOAUTH2 using the keyring secret),
  * enters IDLE with a 30 s timeout (spec §5 ``mailbox.idle.wait(timeout=30s)``),
  * issues a NOOP keep-alive each cycle and force-reconnects every 15 minutes
    (well inside the RFC 2177 29-minute IDLE ceiling),
  * on a new UID fetches the message, runs it through ``sanitize`` + ``normalize``,
    inserts a ``messages`` row plus its controller-side link mapping via the
    :class:`~openclaw_email.db.store.Store`, computes a ``thread_id``, and fires
    the ``on_new_message(message_id)`` callback.

No LLM here (Phase 2). Reconnect uses exponential backoff with a cap. The
listener never deletes/moves mail — read-only ingest only.

:func:`bootstrap_allowlist` seeds ``recipient_allowlist`` from the Sent folder
(last 12 months) on first ingest (spec §7).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from imap_tools import AND, MailBox
from imap_tools.errors import ImapToolsError

from ..config import IMAPAccount
from ..db.store import Store
from ..secrets import get_imap_secret
from . import normalize, sanitize

log = logging.getLogger(__name__)

IDLE_TIMEOUT_S = 30          # spec §5: mailbox.idle.wait(timeout=30s)
FORCE_RECONNECT_S = 15 * 60  # spec §3 Phase 2: forced 15-min reconnect
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 300.0
SENT_FOLDER_CANDIDATES = ("Sent", "Sent Items", "Sent Mail", "[Gmail]/Sent Mail", "INBOX.Sent")
_RE_SUBJECT_PREFIX = re.compile(r"^\s*(re|fwd|fw|aw|wg)\s*:\s*", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# thread_id computation.
# --------------------------------------------------------------------------- #
def _parse_msgid_list(value: str | None) -> list[str]:
    if not value:
        return []
    return re.findall(r"<[^>]+>", value)


def compute_thread_id(
    message_id: str | None,
    in_reply_to: str | None,
    references: str | None,
    subject: str | None,
) -> str:
    """Derive a stable thread id (spec §5).

    Preference order:
      1. The root of the References / In-Reply-To header chain (RFC 5322
         threading) — the earliest referenced Message-ID identifies the thread.
      2. This message's own Message-ID (it starts a new thread).
      3. A normalised-subject hash fallback (no usable headers at all).
    """
    chain = _parse_msgid_list(references) + _parse_msgid_list(in_reply_to)
    if chain:
        return chain[0]
    if message_id and message_id.strip():
        mid = message_id.strip()
        return mid if mid.startswith("<") else f"<{mid}>"
    norm = _RE_SUBJECT_PREFIX.sub("", (subject or "").strip()).lower()
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]
    return f"subj:{digest}"


# --------------------------------------------------------------------------- #
# Allowlist bootstrap.
# --------------------------------------------------------------------------- #
def _find_sent_folder(mailbox: MailBox) -> str | None:
    try:
        existing = {f.name for f in mailbox.folder.list()}
    except ImapToolsError:
        return None
    for cand in SENT_FOLDER_CANDIDATES:
        if cand in existing:
            return cand
    for name in existing:
        if "sent" in name.lower():
            return name
    return None


def _login(mailbox: MailBox, account: IMAPAccount) -> None:
    secret = get_imap_secret(account.name)
    if secret is None:
        raise RuntimeError(f"No keyring secret for IMAP account '{account.name}'")
    token = secret.get_secret_value()
    if account.auth_method == "xoauth2":
        mailbox.xoauth2(account.username, token)
    else:
        mailbox.login(account.username, token)


def bootstrap_allowlist(account: IMAPAccount, store: Store) -> int:
    """Seed ``recipient_allowlist`` from the Sent folder, last 12 months (spec §7).

    Returns the number of distinct recipient addresses recorded. Safe to call
    repeatedly — :meth:`Store.record_allowlist` upserts.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=365)).date()
    recorded = 0
    with MailBox(account.host, account.port) as mailbox:
        _login(mailbox, account)
        sent = _find_sent_folder(mailbox)
        if not sent:
            log.info("No Sent folder found for '%s'; skipping allowlist bootstrap.", account.name)
            return 0
        mailbox.folder.set(sent)
        seen: set[str] = set()
        for msg in mailbox.fetch(AND(date_gte=since), headers_only=True, mark_seen=False):
            for addr in (*msg.to, *msg.cc):
                a = addr.strip().lower()
                if a and a not in seen:
                    seen.add(a)
                    store.record_allowlist(a, source="sent_folder")
                    recorded += 1
    log.info("Bootstrapped %d allowlist recipients for '%s'.", recorded, account.name)
    return recorded


# --------------------------------------------------------------------------- #
# Listener.
# --------------------------------------------------------------------------- #
class IMAPListener:
    """IDLE-driven ingest loop for one IMAP account (spec §5)."""

    def __init__(
        self,
        account: IMAPAccount,
        store: Store,
        on_new_message: Callable[[int], None],
    ) -> None:
        self.account = account
        self.store = store
        self.on_new_message = on_new_message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._account_id = store.upsert_account(
            account.name, "imap", account.host, account.port, account.username
        )

    # ----- lifecycle -----
    def start(self) -> None:
        """Run the loop in a daemon background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run, name=f"imap-{self.account.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ----- main loop -----
    def run(self) -> None:
        """Connect → IDLE → ingest, with reconnect/backoff until :meth:`stop`."""
        backoff = BACKOFF_BASE_S
        while not self._stop.is_set():
            try:
                self._connected_loop()
                backoff = BACKOFF_BASE_S  # clean exit (e.g. forced reconnect)
            except Exception as e:  # noqa: BLE001 — any failure → backoff + retry
                log.warning(
                    "IMAP '%s' connection error: %s; reconnecting in %.0fs",
                    self.account.name, e, backoff,
                )
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _connected_loop(self) -> None:
        """One connection session; returns on forced reconnect or stop."""
        folder = self.account.folders[0] if self.account.folders else "INBOX"
        with MailBox(self.account.host, self.account.port) as mailbox:
            _login(mailbox, self.account)
            mailbox.folder.set(folder)
            log.info("IMAP '%s' connected, IDLE on %s", self.account.name, folder)

            # Establish the high-water mark so we only ingest genuinely new mail.
            last_uid = self._max_uid(mailbox)
            session_start = time.monotonic()

            while not self._stop.is_set():
                # Forced 15-min reconnect (well under RFC 2177 29-min ceiling).
                if time.monotonic() - session_start >= FORCE_RECONNECT_S:
                    log.debug("IMAP '%s' forced 15-min reconnect", self.account.name)
                    return

                responses = mailbox.idle.wait(timeout=IDLE_TIMEOUT_S)
                if self._stop.is_set():
                    return

                if responses:
                    last_uid = self._ingest_new(mailbox, folder, last_uid)
                else:
                    # NOOP keep-alive on idle timeout to keep the connection live.
                    try:
                        mailbox.client.noop()
                    except Exception:  # noqa: BLE001 — dead socket → reconnect
                        return

    @staticmethod
    def _max_uid(mailbox: MailBox) -> int:
        uids = mailbox.uids()
        return max((int(u) for u in uids), default=0)

    def _ingest_new(self, mailbox: MailBox, folder: str, last_uid: int) -> int:
        """Fetch and ingest every UID greater than ``last_uid``. Returns the new
        high-water UID."""
        new_uids = [int(u) for u in mailbox.uids() if int(u) > last_uid]
        if not new_uids:
            return last_uid
        new_uids.sort()
        criteria = AND(uid=[str(u) for u in new_uids])
        for msg in mailbox.fetch(criteria, mark_seen=False, bulk=True):
            try:
                self._ingest_one(msg, folder)
            except Exception:  # noqa: BLE001 — one bad message must not kill the loop
                log.exception("Failed to ingest UID %s on '%s'", msg.uid, self.account.name)
        return max(new_uids[-1], last_uid)

    def _ingest_one(self, msg, folder: str) -> None:
        """Sanitize+normalize one message, persist it, fire the callback."""
        hdr = msg.headers  # lowercase keys, values are tuples

        def _h(name: str) -> str | None:
            vals = hdr.get(name)
            return vals[0] if vals else None

        message_id = _h("message-id")
        in_reply_to = _h("in-reply-to")
        references = _h("references")
        thread_id = compute_thread_id(message_id, in_reply_to, references, msg.subject)

        # §4.1 prefer text/plain, else sanitize HTML → text.
        body = sanitize.prefer_plain(msg.text, msg.html)
        is_html = not (msg.text and msg.text.strip())
        norm = normalize.normalize_email(body if not is_html else msg.html, is_html=is_html)

        received_at = (msg.date or datetime.now(timezone.utc)).isoformat()

        message_pk = self.store.insert_message(
            account_id=self._account_id,
            folder=folder,
            uid=int(msg.uid) if msg.uid else None,
            message_id=message_id,
            thread_id=thread_id,
            from_addr=msg.from_,
            from_name=(msg.from_values.name if msg.from_values else None),
            to_addrs=",".join(msg.to),
            cc_addrs=",".join(msg.cc),
            subject=msg.subject,
            received_at=received_at,
            raw_html=(msg.html or None),
            sanitized_text=norm.text,
            has_attachments=1 if msg.attachments else 0,
            link_count=len(norm.links),
        )
        if message_pk is None:
            return  # duplicate (account,folder,uid) — already ingested

        if norm.links:
            self.store.add_links(message_pk, norm.links)

        log.info(
            "Ingested '%s' uid=%s msg=%d thread=%s links=%d",
            self.account.name, msg.uid, message_pk, thread_id, len(norm.links),
        )
        try:
            self.on_new_message(message_pk)
        except Exception:  # noqa: BLE001 — callback must not break ingest
            log.exception("on_new_message callback failed for msg %d", message_pk)
