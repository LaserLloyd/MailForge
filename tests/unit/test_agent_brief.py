"""Daily brief shape (the agent send tests left with the removed send action)."""

from __future__ import annotations

import pytest

from mailforge.bridge_cli import _daily_brief
from mailforge.config import Settings
from mailforge.db.store import open_store, register_sites


@pytest.fixture(autouse=True)
def _default_registry():
    register_sites(["main", "shop"])
    yield
    register_sites(["main", "shop"])


def _settings(send_enabled: bool = True) -> Settings:
    return Settings(
        imap_accounts=[{
            "name": "sales", "site_id": "main", "host": "imap.example",
            "username": "sales@example.test",
        }],
        smtp={"host": "smtp.example", "port": 587, "username": "sales@example.test"},
        openclaw={"agent_send_enabled": send_enabled},
    )


def _seed_reply_draft(store, *, quarantined: int = 0) -> int:
    account = store.upsert_account(
        "sales", "imap", "imap.example", 993, "sales@example.test", "main"
    )
    mid = store.insert_message(
        account_id=account, folder="INBOX", uid=1, message_id="<m1@example.test>",
        thread_id="t1", from_addr="customer@example.test", from_name="Customer",
        to_addrs="sales@example.test", cc_addrs="", subject="Question",
        received_at="2026-07-16T00:00:00+00:00", raw_html=b"",
        sanitized_text="Hello", has_attachments=0, link_count=0,
        quarantined=quarantined,
    )
    return store.create_draft(
        mid, "t1", "customer@example.test", "Re: Question",
        "Thanks for reaching out — happy to help.", "PENDING", {},
    )


def test_daily_brief_shape(tmp_path):
    with open_store(tmp_path / "h.db") as store:
        did = _seed_reply_draft(store)
        mid = store.get_draft(did)["message_id"]
        store.upsert_classification(
            mid, category="RESPOND", priority=2, rationale="",
            injection_risk=0.0, model_used="test",
        )
        store.add_agent_note("main", "email-main", "fresh note", "body", kind="action")
        brief = _daily_brief(store, "main")
        assert brief["triage"]["needs_reply"] == 1
        assert brief["needs_reply"][0]["subject"] == "Question"
        assert brief["needs_reply"][0]["from"] == "Customer"
        assert brief["notes_last_day"][0]["title"] == "fresh note"
        assert brief["quarantined"] == []
