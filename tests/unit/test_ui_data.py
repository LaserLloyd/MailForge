"""Tests for the store helpers + view logic behind the redesigned UI.

Cover the additions made for the Dispatch UI refresh:
  * ``Store.draft_counts``      — per-state counts for the stat chips
  * ``Store.drafts_overview``   — single-query join feeding the inbox rows
  * ``Store.recent_audit``      — newest-first slice for the Activity page
  * ``inbox.rows_for_view``     — tab filtering + search + external-recipient flag
All run on a temp SQLite DB with no network and no optional deps.
"""

from __future__ import annotations

from openclaw_email.db.store import open_store


def _seed(store):
    acct = store.upsert_account("t", "imap", "h", 993, "u")
    m1 = store.insert_message(
        account_id=acct, folder="INBOX", uid=1, message_id="<m1>", thread_id="t1",
        from_addr="alice@example.com", from_name="Alice", to_addrs="me@x", cc_addrs="",
        subject="Quarterly report", received_at="2026-06-10T10:00:00+00:00",
        raw_html=b"", sanitized_text="please review", has_attachments=0, link_count=0,
    )
    m2 = store.insert_message(
        account_id=acct, folder="INBOX", uid=2, message_id="<m2>", thread_id="t2",
        from_addr="bob@example.com", from_name="Bob", to_addrs="me@x", cc_addrs="",
        subject="Lunch?", received_at="2026-06-10T11:00:00+00:00",
        raw_html=b"", sanitized_text="lunch friday", has_attachments=0, link_count=0,
    )
    store.upsert_classification(m1, category="RESPOND", priority=1, rationale="",
                                injection_risk=0.12, model_used="test")
    store.create_draft(m1, "t1", "alice@example.com", "Re: Quarterly report",
                       "On it.", "PENDING")
    store.create_draft(m2, "t2", "bob@example.com", "Re: Lunch?", "Sure!", "BLOCKED")
    store.record_allowlist("alice@example.com", "sent_folder")
    return m1, m2


def test_draft_counts(tmp_path):
    store = open_store(tmp_path / "t.db")
    _seed(store)
    counts = store.draft_counts()
    assert counts == {"PENDING": 1, "BLOCKED": 1}


def test_drafts_overview_joins_message_and_classification(tmp_path):
    store = open_store(tmp_path / "t.db")
    _seed(store)
    rows = store.drafts_overview("PENDING")
    assert len(rows) == 1
    r = rows[0]
    assert r["from_name"] == "Alice"
    assert r["category"] == "RESPOND"
    assert abs(r["injection_risk"] - 0.12) < 1e-9
    # state=None returns every draft
    assert len(store.drafts_overview(None)) == 2


def test_recent_audit_newest_first(tmp_path):
    store = open_store(tmp_path / "t.db")
    store.append_audit("2026-01-01T00:00:00", "user", "approval", "drafts", 1,
                       "{}", None, "h1")
    store.append_audit("2026-01-02T00:00:00", "user", "send", "drafts", 1,
                       "{}", "h1", "h2")
    rows = store.recent_audit(10)
    assert [r["event"] for r in rows] == ["send", "approval"]
    assert len(store.recent_audit(1)) == 1


def test_inbox_rows_filter_and_search(tmp_path):
    from openclaw_email.ui.pages import inbox

    store = open_store(tmp_path / "t.db")
    _seed(store)

    pending = inbox.rows_for_view(store, "pending", "")
    assert len(pending) == 1
    assert pending[0]["sender"] == "Alice"
    assert pending[0]["external"] is False  # allowlisted recipient

    blocked = inbox.rows_for_view(store, "blocked", "")
    assert len(blocked) == 1
    assert blocked[0]["external"] is True  # bob is not allowlisted

    everything = inbox.rows_for_view(store, "all", "")
    assert len(everything) == 2

    # search hits sender / subject / recipient, case-insensitive
    assert len(inbox.rows_for_view(store, "all", "quarterly")) == 1
    assert len(inbox.rows_for_view(store, "all", "BOB")) == 1
    assert len(inbox.rows_for_view(store, "all", "zzz-no-match")) == 0

    # back-compat helper still returns pending rows
    assert len(inbox.pending_rows(store)) == 1
