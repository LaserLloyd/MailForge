"""Read/unread state is pushed to the IMAP server and adopted from it."""

from __future__ import annotations

from types import SimpleNamespace

from mailforge.db.store import Store, open_store
from mailforge.mail import seen_sync
from mailforge.mail.imap_listener import IMAPListener
from mailforge.mail.sync_state import SyncRegistry
from mailforge.config import IMAPAccount


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    def uid(self, *args):
        self.calls.append(args)
        return ("NO", [b"nope"]) if self.fail else ("OK", [b""])


class FakeMailbox:
    def __init__(self, fail: bool = False, seen_uids=()) -> None:
        self.client = FakeClient(fail)
        self._seen = list(seen_uids)

    def uids(self, criteria="ALL"):
        assert criteria == "SEEN"
        return [str(u) for u in self._seen]


def _msg(store: Store, uid: int, thread: str = "t", received: str = "2026-07-01T00:00:00+00:00"):
    acct = store.upsert_account("primary", "imap", "h", 993, "u@example.test", "main")
    mid = store.insert_message(
        account_id=acct, folder="INBOX", uid=uid, message_id=f"<{uid}@x>", thread_id=thread,
        from_addr="a@example.test", from_name="A", to_addrs="u@example.test", cc_addrs="",
        subject="s", received_at=received, raw_html=None, sanitized_text="b",
        has_attachments=0, link_count=0,
    )
    return acct, mid


def _row(store, mid):
    return store.conn.execute(
        "SELECT seen, seen_sync_pending, seen_sync_error FROM messages WHERE id=?", (mid,)
    ).fetchone()


def test_migration_adds_columns_idempotently(tmp_path):
    p = tmp_path / "m.db"
    s = open_store(p)
    s.conn.execute("DROP INDEX IF EXISTS messages_seen_pending")
    s.conn.execute("ALTER TABLE messages DROP COLUMN seen_sync_pending")
    s.conn.execute("ALTER TABLE messages DROP COLUMN seen_sync_error")
    s.conn.commit()
    s2 = open_store(p)
    s2 = open_store(p)
    cols = {r["name"] for r in s2.conn.execute("PRAGMA table_info(messages)")}
    assert {"seen_sync_pending", "seen_sync_error"} <= cols


def test_bulk_mark_pushes_set_and_clear(tmp_path):
    store = open_store(tmp_path / "a.db")
    acct, a = _msg(store, 11)
    _, b = _msg(store, 12, thread="t2")
    store.mark_messages_seen([a], True)
    store.mark_messages_seen([b], False)
    mb = FakeMailbox()
    pushed, err = seen_sync.push_pending(mb, store, acct, "INBOX")
    assert (pushed, err) == (2, None)
    ops = {c[2]: c[1] for c in mb.client.calls}
    assert ops == {"+FLAGS.SILENT": "11", "-FLAGS.SILENT": "12"}
    assert _row(store, a)["seen_sync_pending"] == 0 and _row(store, b)["seen_sync_pending"] == 0
    # nothing left to do
    assert seen_sync.push_pending(FakeMailbox(), store, acct, "INBOX") == (0, None)


def test_failed_push_is_kept_and_retried(tmp_path):
    store = open_store(tmp_path / "b.db")
    acct, a = _msg(store, 21)
    store.mark_messages_seen([a], True)
    pushed, err = seen_sync.push_pending(FakeMailbox(fail=True), store, acct, "INBOX")
    assert pushed == 0 and err
    r = _row(store, a)
    assert r["seen_sync_pending"] == 1 and r["seen_sync_error"]
    pushed, err = seen_sync.push_pending(FakeMailbox(), store, acct, "INBOX")
    assert (pushed, err) == (1, None)
    assert _row(store, a)["seen_sync_pending"] == 0


def test_change_during_push_stays_pending(tmp_path):
    store = open_store(tmp_path / "c.db")
    acct, a = _msg(store, 31)
    store.mark_messages_seen([a], True)
    store.mark_messages_seen([a], False)  # flipped again before the push result landed
    store.record_seen_pushed([(a, 1)])
    assert _row(store, a)["seen_sync_pending"] == 1


def test_reply_marks_source_and_earlier_thread_read(tmp_path):
    store = open_store(tmp_path / "d.db")
    acct, first = _msg(store, 41, "th", "2026-07-01T00:00:00+00:00")
    _, src = _msg(store, 42, "th", "2026-07-02T00:00:00+00:00")
    _, later = _msg(store, 43, "th", "2026-07-03T00:00:00+00:00")
    _, other = _msg(store, 44, "other", "2026-07-02T00:00:00+00:00")
    store.mark_reply_source_seen(src)
    assert _row(store, first)["seen"] == 1 and _row(store, src)["seen"] == 1
    assert _row(store, later)["seen"] == 0 and _row(store, other)["seen"] == 0
    assert _row(store, src)["seen_sync_pending"] == 1


def test_mark_sent_marks_source_read(tmp_path):
    from mailforge.ui.pages import detail

    store = open_store(tmp_path / "e.db")
    acct, mid = _msg(store, 51)
    cols = [r["name"] for r in store.conn.execute("PRAGMA table_info(drafts)")]
    assert "message_id" in cols
    store.get_draft = lambda _id: {"message_id": mid}  # type: ignore[method-assign]
    store.update_draft_state = lambda *a, **k: None  # type: ignore[method-assign]
    detail.mark_sent(store, 1, "hi", "a@example.test")
    assert _row(store, mid)["seen"] == 1 and _row(store, mid)["seen_sync_pending"] == 1


def test_adopt_server_seen_and_local_pending_wins(tmp_path):
    store = open_store(tmp_path / "f.db")
    acct, a = _msg(store, 61)
    _, b = _msg(store, 62, "t2")
    _, c = _msg(store, 63, "t3")
    store.mark_messages_seen([b], False)  # local unread, push not landed yet
    store.conn.execute("UPDATE messages SET seen=1 WHERE id=?", (c,))
    store.conn.commit()
    n = seen_sync.adopt_seen(FakeMailbox(seen_uids=[61, 62, 63]), store, acct, "INBOX")
    assert n == 1
    assert _row(store, a)["seen"] == 1
    assert _row(store, b)["seen"] == 0  # pending local change beats the server
    assert _row(store, c)["seen"] == 1


def test_listener_ingest_adopts_flag_and_surfaces_push_error(tmp_path):
    store = open_store(tmp_path / "g.db")
    reg = SyncRegistry()
    acct = IMAPAccount(name="primary", host="h", port=993, username="u@example.test",
                       auth_method="password", site_id="main", folders=["INBOX"])
    lst = IMAPListener(acct, store, lambda _i: None, sync=reg)
    _, a = _msg(store, 71)
    store.mark_messages_seen([a], True)
    lst._sync_seen(FakeMailbox(fail=True), "INBOX")
    assert reg.get("primary").seen_sync_error
    lst._sync_seen(FakeMailbox(), "INBOX")
    assert reg.get("primary").seen_sync_error is None
    assert _row(store, a)["seen_sync_pending"] == 0


def test_ingest_one_adopts_server_seen(tmp_path):
    store = open_store(tmp_path / "h.db")
    acct = IMAPAccount(name="primary", host="h", port=993, username="u@example.test",
                       auth_method="password", site_id="main", folders=["INBOX"])
    lst = IMAPListener(acct, store, lambda _i: None, sync=SyncRegistry())
    hdr = {"message-id": ("<z@x>",)}
    msg = SimpleNamespace(
        uid="81", headers=hdr, subject="hello", text="body text", html="", date=None,
        from_="a@example.test", from_values=SimpleNamespace(name="A"), to=("u@example.test",),
        cc=(), attachments=[], flags=("\\Seen",),
    )
    assert lst._ingest_one(msg, "INBOX", notify=False)
    row = store.conn.execute("SELECT seen, seen_sync_pending FROM messages WHERE uid=81").fetchone()
    assert (row["seen"], row["seen_sync_pending"]) == (1, 0)
