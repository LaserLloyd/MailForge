"""The Spam & Deletion page: which box a message lands in, and what it says.

These tests do not drive NiceGUI widgets — they cover the data decisions the
page makes (which query feeds which box, how a due date is worded, the wording
of a delete confirmation), which is where a mistake would actually mislead
someone into deleting the wrong mail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mailforge.config import SecuritySettings, Settings
from mailforge.db.store import open_store
from mailforge.ui import theme
from mailforge.ui.pages import spam


def _store(tmp_path):
    return open_store(tmp_path / "spam-page.db")


def _message(store, *, uid: int, status: str, subject: str = "Hello") -> int:
    account = store.upsert_account(
        "primary", "imap", "imap.example", 993, "inbox@example.test", "main"
    )
    message_id = store.insert_message(
        account_id=account,
        folder="INBOX",
        uid=uid,
        message_id=f"<{uid}@example.test>",
        thread_id=f"t-{uid}",
        from_addr="sender@example.test",
        from_name="Sender",
        to_addrs="inbox@example.test",
        cc_addrs="",
        subject=subject,
        received_at="2026-07-16T00:00:00+00:00",
        raw_html=b"",
        sanitized_text="body",
        has_attachments=0,
        link_count=0,
    )
    if status != "UNSCREENED":
        store.set_message_screening(message_id, status, "because")
    return message_id


def test_the_three_boxes_are_ordered_least_certain_first():
    assert [section[3] for section in spam.SECTIONS] == [
        "POTENTIAL_SPAM",
        "POTENTIAL_ISSUE",
        "SPAM",
    ]


def test_each_box_shows_only_its_own_screening_status(tmp_path):
    store = _store(tmp_path)
    ids = {
        status: _message(store, uid=i + 1, status=status)
        for i, status in enumerate(("POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM", "CONTENT"))
    }
    for status in ("POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"):
        rows = spam._fetch(store, status, "")
        assert [r["id"] for r in rows] == [ids[status]], status
    # Legitimate mail appears in none of them.
    assert all(
        ids["CONTENT"] not in [r["id"] for r in spam._fetch(store, status, "")]
        for status in ("POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM")
    )


def test_deleted_mail_leaves_the_boxes_and_enters_the_queue(tmp_path):
    store = _store(tmp_path)
    settings = Settings(security=SecuritySettings(trash_retention_days=60))
    message_id = _message(store, uid=1, status="POTENTIAL_SPAM")
    assert [r["id"] for r in spam._fetch(store, "POTENTIAL_SPAM", "")] == [message_id]

    store.set_message_trashed(message_id)
    assert spam._fetch(store, "POTENTIAL_SPAM", "") == []
    assert [r["id"] for r in spam._fetch_queue(store, settings, "")] == [message_id]


def test_queue_filters_by_mailbox(tmp_path):
    store = _store(tmp_path)
    settings = Settings(security=SecuritySettings())
    message_id = _message(store, uid=1, status="CONTENT")
    store.set_message_trashed(message_id)
    assert len(spam._fetch_queue(store, settings, "primary")) == 1
    assert spam._fetch_queue(store, settings, "someone-else") == []


def test_due_text_calls_out_spam_as_unheld():
    text, tone = spam._due_text(None)
    assert "not held" in text and tone == "error"


def test_due_text_counts_down_and_escalates():
    soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    text, tone = spam._due_text(soon)
    assert "deletes in 3 days" in text and tone == "warning"

    later = (datetime.now(timezone.utc) + timedelta(days=40)).isoformat()
    text, tone = spam._due_text(later)
    assert "deletes in 40 days" in text and tone == "text-secondary"

    passed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    text, tone = spam._due_text(passed)
    assert text.startswith("due now") and tone == "error"


def test_bulk_labelling_records_feedback_for_every_message(tmp_path):
    store = _store(tmp_path)
    ids = [_message(store, uid=i, status="POTENTIAL_SPAM") for i in (1, 2, 3)]
    changed, problem = spam._apply_label(store, ids, "SPAM")
    assert (changed, problem) == (3, "")
    assert all(store.get_message(i)["screening_status"] == "SPAM" for i in ids)
    # learn_similar=1, so the sender pattern is now a rule.
    assert len(store.screening_examples("main")) == 3


def test_one_bad_message_does_not_discard_the_other_decisions(tmp_path):
    store = _store(tmp_path)
    good = _message(store, uid=1, status="POTENTIAL_SPAM")
    changed, problem = spam._apply_label(store, [good, 999_999], "SPAM")
    assert changed == 1 and problem
    assert store.get_message(good)["screening_status"] == "SPAM"


def test_page_is_registered_in_the_sidebar():
    keys = {entry[0]: entry for entry in theme.NAV}
    # Short on purpose: the drawer is 210px wide with an icon, so a longer
    # label wraps to two lines next to the single-line entries.
    assert keys["spam"][1] == "Spam"
    assert keys["spam"][3] == "/spam"
    assert all(len(entry[1]) <= 12 for entry in theme.NAV)


def test_screening_badge_covers_every_box_status():
    for _key, _title, _blurb, status, _tone in spam.SECTIONS:
        assert theme.screening_badge(status) is not None
