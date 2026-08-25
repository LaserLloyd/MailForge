"""Manual refresh + live sync status (mail/sync_state.py, listener wiring)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from siftforge.mail import imap_listener
from siftforge.mail.imap_listener import IMAPListener
from siftforge.mail.sync_state import SyncRegistry, human_age


def test_registry_tracks_checks_errors_and_summary():
    reg = SyncRegistry()
    reg.register("a", lambda: None)
    reg.register("b", lambda: None)
    assert reg.summary()["state"] == "starting"

    reg.note_check("a", new_messages=2)
    reg.note_check("b", error="boom")
    s = reg.summary()
    assert s["state"] == "degraded"
    assert [e.account for e in s["errors"]] == ["b"]
    assert reg.get("a").new_total == 2 and reg.get("a").last_new_at is not None
    assert reg.get("b").state == "backoff" and reg.get("b").last_error == "boom"

    reg.note_check("b", new_messages=0)  # recovery clears the error
    s = reg.summary()
    assert s["state"] == "ok" and s["errors"] == [] and reg.get("b").last_error is None
    assert reg.get("b").checks == 1  # the failed attempt is not a completed check


def test_request_refresh_pokes_hooks_and_wait_for_check_returns_on_new_seq():
    reg = SyncRegistry()
    poked = []
    reg.register("a", lambda: poked.append("a"))
    reg.register("b", lambda: poked.append("b"))
    before = reg.seqs()
    assert reg.request_refresh() == 2 and sorted(poked) == ["a", "b"]
    assert reg.get("a").refresh_pending is True
    assert reg.summary()["checking"] is True

    # Nothing completed yet → times out.
    assert reg.wait_for_check(before, timeout=0.05) is False

    def finish() -> None:
        time.sleep(0.05)
        reg.note_check("a", 1)
        reg.note_check("b", error="offline")  # a failure still unblocks the wait

    threading.Thread(target=finish).start()
    assert reg.wait_for_check(before, timeout=2.0) is True
    assert reg.get("a").refresh_pending is False


def test_request_refresh_for_unknown_account_is_noop():
    reg = SyncRegistry()
    assert reg.request_refresh("nope") == 0
    assert reg.summary()["state"] == "none"


def test_human_age():
    assert human_age(None) == "never"
    assert human_age(2) == "just now"
    assert human_age(42) == "42 s ago"
    assert human_age(130) == "2 min ago"
    assert human_age(7200) == "2 h ago"


# --------------------------------------------------------------------------- #
# Listener wiring (no network; fake mailbox + stubbed ingest).
# --------------------------------------------------------------------------- #
class _Idle:
    """Fake imap_tools IdleManager: poll() sleeps its timeout, returns nothing."""

    def __init__(self) -> None:
        self.polls = 0
        self.stopped = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stopped = True

    def poll(self, timeout):
        self.polls += 1
        time.sleep(timeout)
        return []


def _listener(reg: SyncRegistry) -> IMAPListener:
    lst = object.__new__(IMAPListener)
    lst.account = SimpleNamespace(name="acct")
    lst.sync = reg
    lst._stop = threading.Event()
    lst._refresh_requested = threading.Event()
    lst._wake = threading.Event()
    lst._account_id = 1
    reg.register("acct", lst.request_refresh)
    return lst


def test_idle_wait_is_interrupted_by_refresh_within_a_slice(monkeypatch):
    monkeypatch.setattr(imap_listener, "IDLE_TIMEOUT_S", 30)
    monkeypatch.setattr(imap_listener, "IDLE_POLL_SLICE_S", 0.05)
    reg = SyncRegistry()
    lst = _listener(reg)
    idle = _Idle()
    mailbox = SimpleNamespace(idle=idle)

    threading.Timer(0.12, lst.request_refresh).start()
    t0 = time.monotonic()
    assert lst._idle_wait(mailbox) == []
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, elapsed  # not the 30 s IDLE timeout
    assert idle.stopped is True  # DONE was sent (context manager exited)
    assert lst._refresh_requested.is_set()


def test_idle_wait_is_interrupted_by_stop(monkeypatch):
    monkeypatch.setattr(imap_listener, "IDLE_POLL_SLICE_S", 0.05)
    reg = SyncRegistry()
    lst = _listener(reg)
    mailbox = SimpleNamespace(idle=_Idle())
    threading.Timer(0.1, lst.stop).start()
    t0 = time.monotonic()
    lst._idle_wait(mailbox)
    assert time.monotonic() - t0 < 1.0


def test_checked_ingest_records_status_and_clears_refresh_flag():
    reg = SyncRegistry()
    lst = _listener(reg)
    lst._ingest_missing = lambda mailbox, folder, initial=False: 3
    lst.request_refresh()
    before = reg.seqs()
    assert lst._checked_ingest(object(), "INBOX") == 3
    st = reg.get("acct")
    assert st.state == "idle" and st.new_total == 3 and st.check_seq == before["acct"] + 1
    assert not lst._refresh_requested.is_set() and not lst._wake.is_set()
    assert st.refresh_pending is False


def test_checked_ingest_failure_is_visible_and_reraised():
    reg = SyncRegistry()
    lst = _listener(reg)

    def boom(mailbox, folder, initial=False):
        raise RuntimeError("socket closed")

    lst._ingest_missing = boom
    before = reg.seqs()
    try:
        lst._checked_ingest(object(), "INBOX")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected re-raise")
    st = reg.get("acct")
    assert st.state == "backoff" and "socket closed" in st.last_error
    assert st.check_seq == before["acct"] + 1  # a waiting Refresh click is released


def test_run_backoff_sleep_is_woken_by_refresh(monkeypatch):
    """A refresh during reconnect backoff retries immediately."""
    monkeypatch.setattr(imap_listener, "BACKOFF_BASE_S", 30.0)
    reg = SyncRegistry()
    lst = _listener(reg)
    lst._backfilled = True
    lst._bootstrap_allowlist = False
    attempts = []

    def connected_loop():
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            raise ConnectionError("offline")
        lst._stop.set()  # second attempt succeeds → stop the loop

    lst._connected_loop = connected_loop
    threading.Timer(0.1, lst.request_refresh).start()
    t0 = time.monotonic()
    lst.run()
    assert len(attempts) == 2
    assert time.monotonic() - t0 < 2.0  # did not sleep the 30 s backoff
    assert reg.get("acct").state == "stopped"


# --------------------------------------------------------------------------- #
# Store fixes that rode along with this change.
# --------------------------------------------------------------------------- #
def test_audit_chain_survives_concurrent_appenders(tmp_path):
    """Two threads appending at once must still produce one linear chain."""
    from siftforge.audit.log import AuditLog, verify_chain
    from siftforge.db.store import open_store

    db = tmp_path / "a.db"
    store = open_store(db)
    stores = [store, open_store(db, init=False)]
    logs = [AuditLog(s) for s in stores]
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for n in range(25):
                logs[i % 2].record(actor="user", event="tool_call", detail={"i": i, "n": n})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    rows = store.iter_audit()
    assert len(rows) == 100
    assert verify_chain(db)[0] is True
    # every prev_hash is unique (no two rows chained onto the same parent)
    prevs = [r["prev_hash"] for r in rows]
    assert len(set(prevs)) == len(prevs)


def test_list_received_orders_by_instant_and_searches_bodies(tmp_path):
    from siftforge.db.store import open_store

    store = open_store(tmp_path / "m.db")
    acct = store.upsert_account("a", "imap", "h", 993, "u@x", "main")
    base = dict(account_id=acct, folder="INBOX", thread_id="<t>", from_addr="s@x",
                from_name="S", to_addrs="u@x", cc_addrs="", raw_html=None,
                has_attachments=0, link_count=0)
    # Same instant expressed in two offsets + one clearly later: string order
    # would put the +09:00 row first.
    a = store.insert_message(uid=1, message_id="<1>", subject="alpha",
                             received_at="2026-08-19T09:00:00+09:00",
                             sanitized_text="the quick brown fox", **base)
    b = store.insert_message(uid=2, message_id="<2>", subject="beta",
                             received_at="2026-08-19T01:00:00+00:00",
                             sanitized_text="lazy dog 100% sure", **base)
    c = store.insert_message(uid=3, message_id="<3>", subject="gamma",
                             received_at="2026-08-19T02:00:00+00:00",
                             sanitized_text="later message", **base)
    ids = [r["id"] for r in store.list_received(limit=10)]
    assert ids[0] == c and set(ids[1:]) == {a, b}
    assert [r["id"] for r in store.list_received(limit=10, q="brown")] == [a]
    assert [r["id"] for r in store.list_received(limit=10, q="100%")] == [b]  # literal %
    assert [r["id"] for r in store.list_received(limit=10, offset=1)] == ids[1:]
    assert store.list_received(limit=10, drafted_only=True) == []
