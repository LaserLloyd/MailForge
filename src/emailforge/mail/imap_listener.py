"""IMAP IDLE listener with reconnect/backoff (spec §3 Phase 2, §5 state machine).

One :class:`IMAPListener` per account. The thread-based ``run()`` loop:

  * connects (password or XOAUTH2 using the keyring secret),
  * enters IDLE with a 30 s timeout (spec §5 ``mailbox.idle.wait(timeout=30s)``),
  * issues a NOOP keep-alive each cycle and force-reconnects every 15 minutes
    (well inside the RFC 2177 29-minute IDLE ceiling),
  * on a new UID fetches the message, runs it through ``sanitize`` + ``normalize``,
    inserts a ``messages`` row plus its controller-side link mapping via the
    :class:`~emailforge.db.store.Store`, computes a ``thread_id``, and fires
    the ``on_new_message(message_id)`` callback.

No LLM here (Phase 2). Reconnect uses exponential backoff with a cap. The
listener never deletes/moves mail — read-only ingest only.

:func:`bootstrap_allowlist` seeds ``recipient_allowlist`` from the Sent folder
(last 12 months) on first ingest (spec §7).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from imap_tools import AND, MailBox
from imap_tools.errors import ImapToolsError

from ..config import IMAPAccount, SecuritySettings
from ..db.store import Store
from ..paths import data_dir
from ..secrets import get_imap_secret
from . import normalize, sanitize
from .sync_state import SyncRegistry
from .sync_state import registry as default_registry

log = logging.getLogger(__name__)

IDLE_TIMEOUT_S = 30          # spec §5: mailbox.idle.wait(timeout=30s)
IDLE_POLL_SLICE_S = 1.0      # IDLE is polled in short slices so a manual
#                              refresh (or stop) interrupts it within ~1 s
#                              instead of waiting out the full 30 s.
FORCE_RECONNECT_S = 15 * 60  # spec §3 Phase 2: forced 15-min reconnect
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 300.0
SOCKET_TIMEOUT_S = 30        # every blocking IMAP op (login/select/fetch/NOOP/
#                              DONE) gives up after this instead of hanging the
#                              listener forever on a half-open NAT/VPN socket.
#                              IDLE itself polls with select() and is unaffected.
FETCH_CHUNK = 200            # max UIDs per SEARCH/FETCH during catch-up — one
#                              giant command fails on most servers and would then
#                              fail identically on every retry.
BACKFILL_COUNT = 50          # most-recent existing messages pulled once on first
#                              connect so the Mail page isn't empty. Idempotent
#                              (UNIQUE account,folder,uid); these messages enter
#                              the normal autonomous draft workflow.
SENT_FOLDER_CANDIDATES = ("Sent", "Sent Items", "Sent Mail", "[Gmail]/Sent Mail", "INBOX.Sent")
_RE_SUBJECT_PREFIX = re.compile(r"^\s*(re|fwd|fw|aw|wg)\s*:\s*", re.IGNORECASE)


def _safe_attachment_name(name: str) -> str:
    """Path-traversal-safe display/storage basename for an attachment."""
    base = Path((name or "attachment").replace("\\", "/")).name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .")
    return (stem or "attachment")[:150]


def _received_at(date: datetime | None) -> str:
    """Normalised ISO-8601 UTC timestamp for ``messages.received_at``.

    imap_tools returns ``datetime(1900,1,1)`` (truthy!) for an unparseable
    ``Date:`` header and may return naive datetimes; a 1900 row sorts to the
    bottom of every list and is the first thing cut by the startup
    ``unprocessed`` sweep. Fall back to "now" and store everything in UTC so
    the TEXT column sorts as instants.
    """
    dt = date
    if dt is None or dt.year < 1990:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


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
    with MailBox(account.host, account.port, timeout=SOCKET_TIMEOUT_S) as mailbox:
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
        security: SecuritySettings | None = None,
        screening_mode: str = "standard",
        site_terms: "re.Pattern[str] | None" = None,
        brand_terms: "re.Pattern[str] | None" = None,
        sync: SyncRegistry | None = None,
        bootstrap_allowlist_on_start: bool = False,
    ) -> None:
        self.account = account
        self.store = store
        self.on_new_message = on_new_message
        self.security = security or SecuritySettings()
        self.screening_mode = str(screening_mode or "standard")
        # Site vocabulary for content_only screening: configured terms win,
        # None => the packaged generic defaults (see inbound_screening).
        self.site_terms = site_terms
        self.brand_terms = brand_terms
        # Sent-folder allowlist seeding runs inside this listener's own thread
        # (best-effort) so one slow mailbox cannot delay ingest for the others.
        self._bootstrap_allowlist = bool(bootstrap_allowlist_on_start)
        # Live status + manual-refresh hook shared with the UI.
        self.sync = sync if sync is not None else default_registry
        self._refresh_requested = threading.Event()
        self._wake = threading.Event()  # set by stop() and request_refresh()
        # This mailbox's own domain — inbound mail claiming it is forged.
        from ..security.inbound_screening import sender_domain

        own = sender_domain(account.username)
        self._own_domains: tuple[str, ...] = (own,) if own else ()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backfilled = False
        self._account_id = store.upsert_account(
            account.name,
            "imap",
            account.host,
            account.port,
            account.username,
            account.site_id,
        )
        self.sync.register(account.name, self.request_refresh)

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
        self._wake.set()
        self.sync.update(self.account.name, state="stopped")

    def request_refresh(self) -> None:
        """Ask the loop to reconcile the mailbox now (UI Refresh button).

        Interrupts the current IDLE slice, or a reconnect backoff sleep, so
        the check starts within about a second instead of up to 30 s later.
        """
        self._refresh_requested.set()
        self._wake.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ----- main loop -----
    def run(self) -> None:
        """Connect → IDLE → ingest, with reconnect/backoff until :meth:`stop`."""
        backoff = BACKOFF_BASE_S
        if self._bootstrap_allowlist and not self._stop.is_set():
            try:
                n = bootstrap_allowlist(self.account, self.store)
                log.info("Bootstrapped %s allowlist entries for %s.", n, self.account.name)
            except Exception as e:  # noqa: BLE001 — best-effort, never blocks ingest
                log.info("Allowlist bootstrap skipped for %s: %s", self.account.name, e)
        while not self._stop.is_set():
            try:
                self._connected_loop()
                backoff = BACKOFF_BASE_S  # clean exit (e.g. forced reconnect)
            except Exception as e:  # noqa: BLE001 — any failure → backoff + retry
                log.warning(
                    "IMAP '%s' connection error: %s; reconnecting in %.0fs",
                    self.account.name, e, backoff,
                )
                self.sync.note_check(self.account.name, error=str(e))
                self.sync.update(self.account.name, next_retry_at=time.time() + backoff)
                # A manual refresh wakes this sleep early so the user's click
                # retries the connection immediately instead of waiting out
                # the backoff.
                self._wake.clear()
                self._wake.wait(backoff)
                if self._stop.is_set():
                    break
                backoff = min(backoff * 2, BACKOFF_MAX_S)
        self.sync.update(self.account.name, state="stopped")

    def _connected_loop(self) -> None:
        """One connection session; returns on forced reconnect or stop."""
        folder = self.account.folders[0] if self.account.folders else "INBOX"
        self.sync.update(self.account.name, state="connecting")
        with MailBox(self.account.host, self.account.port, timeout=SOCKET_TIMEOUT_S) as mailbox:
            _login(mailbox, self.account)
            mailbox.folder.set(folder)
            log.info("IMAP '%s' connected, IDLE on %s", self.account.name, folder)
            self.sync.update(
                self.account.name, state="idle", connected_since=time.time(), last_error=None,
            )

            # Reconcile server UIDs against durable DB state. Unlike an in-memory
            # high-water mark, this catches mail delivered while the service was
            # stopped or reconnecting and retries individual failed UIDs.
            if not self._backfilled:
                try:
                    self._checked_ingest(mailbox, folder, initial=True)
                except Exception:  # noqa: BLE001 — backfill is best-effort
                    log.exception("Backfill failed for '%s'", self.account.name)
                self._backfilled = True
            elif self._refresh_requested.is_set():
                # Reconnected because the user asked for a refresh while we were
                # in backoff: reconcile right away.
                self._checked_ingest(mailbox, folder)

            session_start = time.monotonic()

            while not self._stop.is_set():
                # Forced 15-min reconnect (well under RFC 2177 29-min ceiling).
                if time.monotonic() - session_start >= FORCE_RECONNECT_S:
                    log.debug("IMAP '%s' forced 15-min reconnect", self.account.name)
                    return

                responses = self._idle_wait(mailbox)
                if self._stop.is_set():
                    return

                # Always reconcile, even after a timeout: some servers drop or
                # coalesce IDLE notifications, while UID membership is durable.
                self._checked_ingest(mailbox, folder)
                if not responses:
                    # NOOP keep-alive on idle timeout to keep the connection live.
                    try:
                        mailbox.client.noop()
                    except Exception:  # noqa: BLE001 — dead socket → reconnect
                        return

    def _idle_wait(self, mailbox: MailBox) -> list[bytes]:
        """One IDLE session of up to ``IDLE_TIMEOUT_S``, polled in short slices.

        Returns early with whatever untagged responses arrived, or as soon as
        :meth:`stop` / :meth:`request_refresh` fires — the caller reconciles
        after every return regardless, so an early return just means the
        check happens now.
        """
        deadline = time.monotonic() + IDLE_TIMEOUT_S
        responses: list[bytes] = []
        with mailbox.idle as idle:
            while not self._stop.is_set() and not self._refresh_requested.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                responses = idle.poll(timeout=min(IDLE_POLL_SLICE_S, remaining))
                if responses:
                    break
        return responses

    def _checked_ingest(self, mailbox: MailBox, folder: str, initial: bool = False) -> int:
        """:meth:`_ingest_missing` wrapped with sync-status bookkeeping.

        Clears a pending refresh request first (it is being honoured now) and
        records the outcome so the UI's "checked N s ago" and the Refresh
        button's wait both see it — on failure too, so a refresh never hangs.
        """
        self._refresh_requested.clear()
        self._wake.clear()
        self.sync.update(self.account.name, state="checking")
        try:
            inserted = self._ingest_missing(mailbox, folder, initial=initial)
        except Exception as e:
            self.sync.note_check(self.account.name, error=str(e))
            raise
        self.sync.note_check(self.account.name, new_messages=inserted)
        return inserted

    def _ingest_missing(self, mailbox: MailBox, folder: str, initial: bool = False) -> int:
        """Fetch UIDs after the durable SQLite mailbox cursor.

        A brand-new account starts with the most recent ``BACKFILL_COUNT`` to
        bound first-run work. Targets are fetched in ``FETCH_CHUNK`` slices and
        the cursor advances after each fully-handled slice, so a long offline
        gap is caught up incrementally and a per-message failure is retried
        after reconnect without re-fetching what already landed.
        """
        cursor = self.store.mailbox_cursor(self._account_id, folder)
        if cursor is None:
            # Existing installs start from their durable DB maximum. Brand-new
            # installs intentionally seed only the recent bounded backfill.
            stored_uids = self.store.message_uids(self._account_id, folder)
            cursor = max(stored_uids, default=0)
            server_uids = sorted(int(u) for u in mailbox.uids())
            targets = [u for u in server_uids if u > cursor]
            if initial and not stored_uids:
                targets = targets[-BACKFILL_COUNT:]
        else:
            # Only ask the server for UIDs past the cursor; "SEARCH ALL" on
            # every 30 s cycle scales with mailbox size for no reason.
            server_uids = sorted(int(u) for u in mailbox.uids(f"UID {int(cursor) + 1}:*"))
            targets = [u for u in server_uids if u > cursor]
        if not targets:
            if cursor is not None:
                self.store.set_mailbox_cursor(self._account_id, folder, cursor)
            return 0
        inserted = 0
        for start in range(0, len(targets), FETCH_CHUNK):
            chunk = targets[start : start + FETCH_CHUNK]
            criteria = AND(uid=[str(u) for u in chunk])
            complete = True
            for msg in mailbox.fetch(criteria, mark_seen=False, bulk=True):
                try:
                    if self._ingest_one(msg, folder, notify=True):
                        inserted += 1
                except Exception:  # noqa: BLE001 — one bad message must not kill the loop
                    complete = False
                    log.exception("Failed to ingest UID %s on '%s'", msg.uid, self.account.name)
            # Advance only after the whole chunk was handled. If one UID failed,
            # the old cursor makes the next reconciliation retry the range;
            # already-inserted rows are idempotent duplicates (and a duplicate
            # that never reached triage is re-queued by _ingest_one).
            if not complete:
                break
            self.store.set_mailbox_cursor(self._account_id, folder, max(chunk))
        if inserted:
            log.info(
                "Reconciled %d missing message(s) for '%s'", inserted, self.account.name
            )
        return inserted

    def _ingest_one(self, msg, folder: str, notify: bool = True) -> bool:
        """Sanitize+normalize one message, persist it, fire the callback.

        ``notify=False`` is available to callers which need persistence without
        drafting. Returns True when a new row was inserted, False on duplicate.
        """
        hdr = msg.headers  # lowercase keys, values are tuples

        def _h(name: str) -> str | None:
            vals = hdr.get(name)
            return vals[0] if vals else None

        message_id = _h("message-id")
        in_reply_to = _h("in-reply-to")
        references = _h("references")
        # Unfold the subject: folded headers arrive with embedded CRLF +
        # whitespace which breaks single-line UI rendering and search.
        subject = " ".join((msg.subject or "").split())
        thread_id = compute_thread_id(message_id, in_reply_to, references, subject)

        # §4.1 prefer text/plain, else sanitize HTML → text.
        body = sanitize.prefer_plain(msg.text, msg.html)
        is_html = not (msg.text and msg.text.strip())
        norm = normalize.normalize_email(body if not is_html else msg.html, is_html=is_html)

        received_at = _received_at(msg.date)

        # Quarantine gate (prompt-injection containment): score at ingest so a
        # hostile message is contained BEFORE any LLM or bridge caller can see
        # it. Scoring failure falls back to 0.0 — containment is an extra
        # layer, the architecture stays safe without it (Rule-of-Two).
        quarantined, quarantine_reason = 0, None
        if self.security.quarantine_enabled:
            try:
                from ..security import score_injection

                risk = float(score_injection(norm.text or ""))
                if risk >= float(self.security.quarantine_threshold):
                    quarantined = 1
                    quarantine_reason = f"ingest injection score {risk:.2f}"
            except Exception as e:  # noqa: BLE001 — never lose mail over scoring
                log.warning("Quarantine scoring failed: %s", e)

        # Site policy gate runs deterministically before persistence/LLM work.
        # It can use prior human examples but never calls a model or follows a
        # link. Questionable/spam messages are stored for the operator's safe local
        # review and withheld from every AI path downstream.
        from ..security.inbound_screening import assess_inbound

        screening = assess_inbound(
            mode=self.screening_mode,
            from_addr=msg.from_ or "",
            subject=subject,
            text=norm.text or "",
            link_count=len(norm.links),
            has_attachments=bool(msg.attachments),
            learned_examples=self.store.screening_examples(self.account.site_id),
            # Our own domain, so inbound mail forging it is caught. Genuine mail
            # from ourselves does not arrive over the public MX.
            own_domains=self._own_domains,
            site_terms=self.site_terms,
            brand_terms=self.brand_terms,
        )

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
            subject=subject,
            received_at=received_at,
            raw_html=(msg.html or None),
            sanitized_text=norm.text,
            has_attachments=1 if msg.attachments else 0,
            link_count=len(norm.links),
            quarantined=quarantined,
            quarantine_reason=quarantine_reason,
            screening_status=screening.status,
            screening_reason=screening.reason,
            screening_source=screening.source,
        )
        if message_pk is None:
            # Duplicate (account,folder,uid) — already ingested. If the first
            # attempt died after the insert (links/attachments), the row never
            # reached triage and would otherwise sit unclassified forever.
            if notify and msg.uid:
                try:
                    existing = self.store.message_pk_for_uid(self._account_id, folder, int(msg.uid))
                    if existing is not None and self.store.message_needs_processing(existing):
                        log.info("Re-queuing stored-but-untriaged message %d", existing)
                        self._finish_ingest(existing, True)
                except Exception:  # noqa: BLE001 — best-effort repair
                    log.exception("Re-queue check failed for uid %s", msg.uid)
            return False

        if quarantined:
            log.warning(
                "Message %d QUARANTINED at ingest (%s) — no LLM/bridge access "
                "until released in the UI.", message_pk, quarantine_reason,
            )
        if screening.status in {"POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"}:
            log.info(
                "Message %d screened %s (%s) — body withheld from AI.",
                message_pk, screening.status, screening.reason,
            )

        try:
            if norm.links:
                self.store.add_links(message_pk, norm.links)
        except Exception:  # noqa: BLE001 — link rows are secondary; the message must still triage
            log.exception("Link persistence failed for msg %d", message_pk)

        try:
            self._save_attachments(msg, message_pk)
        except Exception:  # noqa: BLE001 — attachment failure must not block mail
            log.exception("Attachment persistence failed for msg %d", message_pk)

        log.info(
            "Ingested '%s' uid=%s msg=%d thread=%s links=%d notify=%s",
            self.account.name, msg.uid, message_pk, thread_id, len(norm.links), notify,
        )
        return self._finish_ingest(message_pk, notify)

    def _save_attachments(self, msg, message_pk: int) -> None:
        """Persist attachment files to disk (size-capped, 0600, sanitized
        names). Bytes never enter the DB and are never shown to any LLM —
        they exist for the human to download from the local UI only."""
        attachments = list(msg.attachments or [])
        if not attachments:
            return
        max_bytes = int(self.security.attachment_max_bytes)
        max_count = int(self.security.attachment_max_count)
        root = data_dir() / "attachments" / str(int(message_pk))
        for index, att in enumerate(attachments):
            name = _safe_attachment_name(getattr(att, "filename", "") or f"part-{index + 1}")
            payload = getattr(att, "payload", b"") or b""
            content_type = getattr(att, "content_type", None)
            if index >= max_count:
                self.store.add_attachment(
                    message_pk, name, content_type, len(payload), None,
                    skipped_reason=f"over per-message cap ({max_count})",
                )
                continue
            if len(payload) > max_bytes:
                self.store.add_attachment(
                    message_pk, name, content_type, len(payload), None,
                    skipped_reason=f"over size cap ({max_bytes} bytes)",
                )
                continue
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, stat.S_IRWXU)
            target = root / f"{index + 1:02d}_{name}"
            with open(target, "wb") as fh:
                fh.write(payload)
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
            self.store.add_attachment(
                message_pk, name, content_type, len(payload), str(target),
                sha256=hashlib.sha256(payload).hexdigest(),
            )

    def _finish_ingest(self, message_pk: int, notify: bool) -> bool:
        if notify:
            try:
                self.on_new_message(message_pk)
            except Exception:  # noqa: BLE001 — callback must not break ingest
                log.exception("on_new_message callback failed for msg %d", message_pk)
        return True
