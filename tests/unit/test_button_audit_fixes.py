"""Regressions found by the WP-5 button-by-button audit of the web UI."""

from __future__ import annotations

import pytest

from mailforge.db.store import open_store
from mailforge.ui import presend
from mailforge.ui.pages import assistant, detail


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "w.db") as s:
        yield s


def _inbound(store, uid=1) -> int:
    account = store.upsert_account("sales", "imap", "imap.example", 993,
                                   "sales@example.test", "main")
    return store.insert_message(
        account_id=account, folder="INBOX", uid=uid, message_id=f"<m{uid}@example.test>",
        thread_id=f"t{uid}", from_addr="customer@example.test", from_name="Customer",
        to_addrs="sales@example.test", cc_addrs="", subject="Question",
        received_at="2026-11-02T00:00:00+00:00", raw_html=b"",
        sanitized_text="Hello", has_attachments=0, link_count=0,
    )


def _draft(store, mid, state="PENDING") -> int:
    return store.create_draft(mid, f"t{mid}", "customer@example.test", "Re: Question",
                              "Hello,\n\nThanks.\n\nJake", state, {})


# --- 1. the re-type gate is gone ---------------------------------------------
def test_there_is_no_retype_gate_any_more():
    assert not hasattr(detail, "retype_matches")


# --- 2. a stale page must not overwrite a draft that moved on -----------------
def test_a_sent_draft_cannot_be_rejected_or_saved_from_a_stale_page(store):
    did = _draft(store, _inbound(store))
    store.update_draft_state(did, "SENT", sent_at="2026-11-02T00:00:00+00:00")
    assert "sent" in (detail.stale_reason(store, did, detail.REJECTABLE) or "")
    assert "sent" in (detail.stale_reason(store, did, detail.EDITABLE) or "")


def test_an_open_draft_may_be_rejected_and_saved(store):
    did = _draft(store, _inbound(store))
    assert detail.stale_reason(store, did, detail.REJECTABLE) is None
    assert detail.stale_reason(store, did, detail.EDITABLE) is None


def test_a_rejected_draft_cannot_be_rejected_twice(store):
    did = _draft(store, _inbound(store))
    store.update_draft_state(did, "REJECTED")
    assert detail.stale_reason(store, did, detail.REJECTABLE)


def test_a_vanished_draft_is_stale(store):
    assert detail.stale_reason(store, 999, detail.REJECTABLE)


# --- 3. one pre-send dialog per page -----------------------------------------
def test_second_dialog_on_the_same_page_is_refused_until_the_first_ends():
    assert presend.begin_dialog("page-1") is True
    assert presend.begin_dialog("page-1") is False  # the double click
    assert presend.begin_dialog("page-2") is True  # another tab is its own page
    presend.end_dialog("page-1")
    assert presend.begin_dialog("page-1") is True
    presend.end_dialog("page-1")
    presend.end_dialog("page-2")


def test_confirm_claims_and_releases_the_page():
    import inspect

    src = inspect.getsource(presend.confirm)
    assert "begin_dialog" in src and "end_dialog" in src


# --- 4. the "Sent folder copy failed" warning must outlive the send -----------
def test_page_stays_open_when_there_is_a_warning_to_read():
    ok = presend.SendResult("sent", record_id=1)
    assert presend.stay_on_page(ok) is False
    assert presend.stay_on_page(presend.SendResult("sent", copy_warning="Sent. No copy.")) is True
    assert presend.stay_on_page(presend.SendResult("sent", persist_error="db locked")) is True


# --- 5. no AI drafting for mail that is in Trash or gone -----------------------
def test_ai_drafting_is_refused_for_trashed_and_purged_mail(store):
    mid = _inbound(store)
    assert assistant.source_block_reason(store, mid) is None
    store.set_message_trashed(mid, True)
    assert "Trash" in (assistant.source_block_reason(store, mid) or "")
    store.conn.execute("UPDATE messages SET purged_at='2026-10-01T00:00:00+00:00' WHERE id=?", (mid,))
    store.conn.commit()
    assert "deleted" in (assistant.source_block_reason(store, mid) or "")
