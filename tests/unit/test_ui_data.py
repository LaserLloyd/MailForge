"""Tests for the store helpers + view logic behind the redesigned UI.

Cover the additions made for the chat-styled UI refresh:
  * ``Store.draft_counts``      — per-state counts for the stat chips
  * ``Store.drafts_overview``   — single-query join feeding the inbox rows
  * ``Store.recent_audit``      — newest-first slice for the Activity page
  * ``inbox.rows_for_view``     — tab filtering + search + external-recipient flag
All run on a temp SQLite DB with no network and no optional deps.
"""

from __future__ import annotations

from mailforge.db.store import open_store


def _seed(store):
    acct = store.upsert_account("t", "imap", "h", 993, "u")
    m1 = store.insert_message(
        account_id=acct,
        folder="INBOX",
        uid=1,
        message_id="<m1>",
        thread_id="t1",
        from_addr="alice@example.com",
        from_name="Alice",
        to_addrs="me@x",
        cc_addrs="",
        subject="Quarterly report",
        received_at="2026-06-10T10:00:00+00:00",
        raw_html=b"",
        sanitized_text="please review",
        has_attachments=0,
        link_count=0,
    )
    m2 = store.insert_message(
        account_id=acct,
        folder="INBOX",
        uid=2,
        message_id="<m2>",
        thread_id="t2",
        from_addr="bob@partner.example",
        from_name="Bob",
        to_addrs="me@x",
        cc_addrs="",
        subject="Lunch?",
        received_at="2026-06-10T11:00:00+00:00",
        raw_html=b"",
        sanitized_text="lunch friday",
        has_attachments=0,
        link_count=0,
    )
    store.upsert_classification(
        m1, category="RESPOND", priority=1, rationale="", injection_risk=0.12, model_used="test"
    )
    store.create_draft(m1, "t1", "alice@example.com", "Re: Quarterly report", "On it.", "PENDING")
    store.create_draft(m2, "t2", "bob@partner.example", "Re: Lunch?", "Sure!", "BLOCKED")
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
    store.append_audit("2026-01-01T00:00:00", "user", "approval", "drafts", 1, "{}", None, "h1")
    store.append_audit("2026-01-02T00:00:00", "user", "send", "drafts", 1, "{}", "h1", "h2")
    rows = store.recent_audit(10)
    assert [r["event"] for r in rows] == ["send", "approval"]
    assert len(store.recent_audit(1)) == 1


def test_inbox_rows_filter_and_search(tmp_path):
    from mailforge.ui.pages import inbox

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


def test_list_received_and_counts(tmp_path):
    """Received-mail view methods (Mail page): list, filter, search, counts, seen."""
    store = open_store(tmp_path / "t.db")
    _seed(store)

    rows = store.list_received()
    assert len(rows) == 2
    # newest-first ordering (m2 received later than m1)
    assert rows[0]["subject"] == "Lunch?"
    assert rows[0]["account_name"] == "t"

    # substring search over sender / subject
    assert len(store.list_received(q="quarterly")) == 1
    assert len(store.list_received(q="bob")) == 1
    assert len(store.list_received(q="zzz")) == 0

    # account filter
    assert len(store.list_received(account_name="t")) == 2
    assert len(store.list_received(account_name="nope")) == 0

    # counts: all unseen on ingest
    c = store.received_counts()
    assert c["total"] == 2 and c["unseen"] == 2

    # mark one seen -> unseen drops, total unchanged
    mid = rows[0]["id"]
    store.mark_message_seen(mid)
    c2 = store.received_counts()
    assert c2["total"] == 2 and c2["unseen"] == 1


def test_live_message_tags_have_counts_and_filter_exactly(tmp_path):
    store = open_store(tmp_path / "t.db")
    first, second = _seed(store)
    store.set_message_quarantined(first, True, "test")
    store.set_message_screening(second, "SPAM", "test")

    assert store.message_tag_counts() == {
        "category:RESPOND": 1,
        "quarantined": 1,
        "spam": 1,
    }
    assert [r["id"] for r in store.list_received(message_tag="category:RESPOND")] == [first]
    assert [r["id"] for r in store.list_received(message_tag="quarantined")] == [first]
    assert [r["id"] for r in store.list_received(message_tag="spam", include_spam=True)] == [second]
    assert store.list_received(message_tag="unknown:value") == []


# --- per-inbox dashboard tiles ------------------------------------------------


def _seed_inboxes(store):
    """Two live inboxes plus one that has never received anything."""
    from datetime import datetime, timedelta

    now = datetime.now().astimezone()
    a = store.upsert_account("alpha", "imap", "h", 993, "a@main", site_id="main")
    b = store.upsert_account("beta", "imap", "h", 993, "b@ll", site_id="shop")
    store.upsert_account("empty", "imap", "h", 993, "e@main", site_id="main")

    def add(account, uid, when, *, subject="s"):
        return store.insert_message(
            account_id=account,
            folder="INBOX",
            uid=uid,
            message_id=f"<m{uid}>",
            thread_id=f"t{uid}",
            from_addr="x@example.com",
            from_name="X",
            to_addrs="me@x",
            cc_addrs="",
            subject=subject,
            received_at=when.isoformat(),
            raw_html=b"",
            sanitized_text="body",
            has_attachments=0,
            link_count=0,
        )

    today_unread = add(a, 1, now)
    today_read = add(a, 2, now - timedelta(minutes=5))
    store.mark_messages_seen([today_read], True)
    old_unread = add(a, 3, now - timedelta(days=3))
    spam = add(a, 4, now)
    other = add(b, 5, now - timedelta(days=1))

    store.upsert_classification(today_unread, category="RESPOND", priority=5, model_used="t")
    store.upsert_classification(old_unread, category="NOTIFY", priority=1, model_used="t")
    store.set_message_screening(spam, "SPAM", "test")
    store.set_message_quarantined(other, True, "test")
    return {
        "a": a,
        "b": b,
        "today_unread": today_unread,
        "today_read": today_read,
        "old_unread": old_unread,
        "spam": spam,
        "other": other,
    }


def _by_name(rows):
    return {row["account_name"]: row for row in rows}


def test_inbox_metrics_reports_every_configured_inbox_including_empty_ones(tmp_path):
    store = open_store(tmp_path / "t.db")
    _seed_inboxes(store)
    tiles = _by_name(store.inbox_metrics())

    assert set(tiles) == {"alpha", "beta", "empty"}
    assert tiles["empty"]["total"] == 0
    assert tiles["empty"]["unread"] == 0
    assert tiles["empty"]["username"] == "e@main"


def test_inbox_metrics_counts_are_scoped_to_their_own_inbox(tmp_path):
    store = open_store(tmp_path / "t.db")
    _seed_inboxes(store)
    tiles = _by_name(store.inbox_metrics())

    alpha = tiles["alpha"]
    # spam is excluded everywhere except its own count
    assert alpha["total"] == 3
    assert alpha["spam"] == 1
    assert alpha["today"] == 2  # today_unread + today_read, not the spam
    assert alpha["unread"] == 2  # today_unread + old_unread
    assert alpha["urgent"] == 1  # priority 5 only
    assert alpha["needs_reply"] == 1  # RESPOND with no sent draft
    assert alpha["quarantined"] == 0

    assert tiles["beta"]["quarantined"] == 1
    assert tiles["beta"]["today"] == 0


def test_inbox_metrics_sum_to_the_global_triage_numbers(tmp_path):
    store = open_store(tmp_path / "t.db")
    _seed_inboxes(store)
    tiles = store.inbox_metrics()
    triage = store.triage_counts()

    for key in ("today", "unread", "needs_reply", "needs_action", "quarantined", "spam"):
        assert sum(t[key] for t in tiles) == triage[key], key


def test_inbox_metrics_filters_by_site(tmp_path):
    store = open_store(tmp_path / "t.db")
    _seed_inboxes(store)
    assert set(_by_name(store.inbox_metrics("shop"))) == {"beta"}
    assert set(_by_name(store.inbox_metrics("main"))) == {"alpha", "empty"}


def test_inbox_metrics_urgent_floor_is_configurable(tmp_path):
    store = open_store(tmp_path / "t.db")
    _seed_inboxes(store)
    assert _by_name(store.inbox_metrics(urgent_priority=1))["alpha"]["urgent"] == 2
    assert _by_name(store.inbox_metrics(urgent_priority=5))["alpha"]["urgent"] == 1


def test_today_and_urgent_views_return_the_same_messages_the_tiles_count(tmp_path):
    store = open_store(tmp_path / "t.db")
    seeded = _seed_inboxes(store)

    today = store.list_received(account_name="alpha", today_only=True)
    assert sorted(r["id"] for r in today) == sorted(
        [seeded["today_unread"], seeded["today_read"]]
    )
    urgent = store.list_received(account_name="alpha", urgent_only=True)
    assert [r["id"] for r in urgent] == [seeded["today_unread"]]


def test_today_view_uses_local_dates_on_both_sides(tmp_path):
    """A message stored with a non-UTC offset must not fall out of 'today'
    just because its UTC date differs from the local one."""
    from datetime import datetime, timedelta, timezone

    store = open_store(tmp_path / "t.db")
    acct = store.upsert_account("tz", "imap", "h", 993, "tz@main", site_id="main")
    # Same instant as now, expressed in a zone 9 hours ahead of UTC.
    local_now = datetime.now(timezone(timedelta(hours=9)))
    store.insert_message(
        account_id=acct,
        folder="INBOX",
        uid=99,
        message_id="<tz>",
        thread_id="tz",
        from_addr="x@example.com",
        from_name="X",
        to_addrs="me@x",
        cc_addrs="",
        subject="tz",
        received_at=local_now.isoformat(),
        raw_html=b"",
        sanitized_text="body",
        has_attachments=0,
        link_count=0,
    )
    tile = _by_name(store.inbox_metrics())["tz"]
    assert tile["today"] == len(store.list_received(account_name="tz", today_only=True))
