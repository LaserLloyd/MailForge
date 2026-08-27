"""Ingest hardening: chunked catch-up, date normalisation, untriaged re-queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mailforge.mail import imap_listener
from mailforge.mail.imap_listener import IMAPListener, _received_at


class _CursorStore:
    def __init__(self, cursor=None, stored=()):
        self.cursor = cursor
        self.stored = set(stored)
        self.cursor_history = []

    def message_uids(self, _a, _f):
        return set(self.stored)

    def mailbox_cursor(self, _a, _f):
        return self.cursor

    def set_mailbox_cursor(self, _a, _f, uid):
        self.cursor = uid
        self.cursor_history.append(uid)


class _Mailbox:
    def __init__(self, uids):
        self._uids = list(uids)
        self.fetch_calls = []

    def uids(self, criteria="ALL"):
        if isinstance(criteria, str) and criteria.startswith("UID "):
            lo = int(criteria[4:].split(":")[0])
            return [str(u) for u in self._uids if u >= lo]
        return [str(u) for u in self._uids]

    def fetch(self, criteria, **_kw):
        body = str(criteria).strip("() ")
        wanted = [int(u) for u in body[4:].split(",")] if body.startswith("UID ") else []
        self.fetch_calls.append(wanted)
        return iter(SimpleNamespace(uid=str(u)) for u in wanted)


def _listener(store):
    lst = object.__new__(IMAPListener)
    lst.store = store
    lst._account_id = 1
    lst.account = SimpleNamespace(name="t", site_id="main")
    # Screening vocabulary is normally injected by runtime from config.
    lst.site_terms = None
    lst.brand_terms = None
    return lst


def test_catch_up_is_chunked_and_cursor_advances_per_chunk(monkeypatch):
    monkeypatch.setattr(imap_listener, "FETCH_CHUNK", 100)
    store = _CursorStore(cursor=0)
    lst = _listener(store)
    lst._ingest_one = lambda msg, folder, notify=True: True
    mailbox = _Mailbox(range(1, 251))
    assert lst._ingest_missing(mailbox, "INBOX") == 250
    assert [len(c) for c in mailbox.fetch_calls] == [100, 100, 50]
    assert store.cursor_history == [100, 200, 250]


def test_failure_in_a_later_chunk_keeps_earlier_progress(monkeypatch):
    monkeypatch.setattr(imap_listener, "FETCH_CHUNK", 100)
    store = _CursorStore(cursor=0)
    lst = _listener(store)

    def ingest(msg, folder, notify=True):
        if int(msg.uid) == 150:
            raise ValueError("bad")
        return True

    lst._ingest_one = ingest
    mailbox = _Mailbox(range(1, 251))
    assert lst._ingest_missing(mailbox, "INBOX") == 199
    # chunk 1 committed, chunk 2 failed → stop there; chunk 3 not attempted
    assert store.cursor == 100
    assert len(mailbox.fetch_calls) == 2


def test_server_is_only_asked_for_uids_past_cursor():
    store = _CursorStore(cursor=1000)
    lst = _listener(store)
    lst._ingest_one = lambda *a, **k: True
    mailbox = _Mailbox([999, 1000, 1001])
    seen = []
    orig = mailbox.uids
    mailbox.uids = lambda criteria="ALL": seen.append(criteria) or orig(criteria)
    assert lst._ingest_missing(mailbox, "INBOX") == 1
    assert seen == ["UID 1001:*"]


def test_received_at_normalises_to_utc_and_rejects_1900():
    assert _received_at(datetime(1900, 1, 1)).startswith(str(datetime.now(timezone.utc).year))
    assert _received_at(None).endswith("+00:00")
    aware = datetime(2026, 8, 19, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    assert _received_at(aware) == "2026-08-19T00:00:00+00:00"
    naive = datetime(2026, 8, 19, 9, 0)
    assert _received_at(naive) == "2026-08-19T09:00:00+00:00"


def test_duplicate_that_never_reached_triage_is_requeued(tmp_path):
    from mailforge.db.store import open_store

    store = open_store(tmp_path / "d.db")
    acct = store.upsert_account("t", "imap", "h", 993, "u@x", "main")
    pk = store.insert_message(
        account_id=acct, folder="INBOX", uid=7, message_id="<7>", thread_id="<7>",
        from_addr="s@x", from_name="S", to_addrs="u@x", cc_addrs="", subject="hi",
        received_at="2026-08-19T00:00:00+00:00", raw_html=None, sanitized_text="x",
        has_attachments=0, link_count=0,
    )
    assert store.message_needs_processing(pk) is True
    notified = []
    lst = _listener(store)
    lst.on_new_message = notified.append
    lst.security = SimpleNamespace(quarantine_enabled=False, attachment_max_bytes=1, attachment_max_count=1)
    lst.screening_mode = "standard"
    lst._own_domains = ()
    msg = SimpleNamespace(
        uid="7", headers={"message-id": ("<7>",)}, subject="hi", text="x", html=None,
        date=datetime.now(timezone.utc), from_="s@x", from_values=None, to=("u@x",),
        cc=(), attachments=[],
    )
    assert lst._ingest_one(msg, "INBOX") is False  # duplicate row
    assert notified == [pk]  # …but it was re-queued for triage
    # once classified it is not re-queued again
    store.conn.execute(
        "INSERT INTO classifications(message_id,category,priority,rationale,injection_risk) "
        "VALUES(?,?,?,?,?)", (pk, "NOTIFY", 1, "", 0.0),
    )
    store.conn.commit()
    notified.clear()
    assert lst._ingest_one(msg, "INBOX") is False
    assert notified == []
