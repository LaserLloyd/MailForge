"""The AI gate is an allowlist: only UNSCREENED/CONTENT bodies reach a model.

A status added later (FILED today, anything tomorrow) must be withheld by every
AI path without anyone having to remember the ~20 places that check it.
"""

from __future__ import annotations

import asyncio

import pytest

from mailforge.config import Settings
from mailforge.db.store import open_store, register_sites
from mailforge.security.inbound_screening import (
    AI_READABLE_STATUSES,
    AI_WITHHELD_STATUSES,
    QUESTIONABLE_STATUSES,
    SCREENING_STATUSES,
    ai_may_read,
    sql_status_list,
)


@pytest.fixture(autouse=True)
def _registry():
    register_sites(["main", "shop"])
    yield
    register_sites(["main", "shop"])


def _message(store, uid: int, *, text: str = "secret body words") -> int:
    account = store.upsert_account(
        "acct", "imap", "imap.example", 993, "inbox@example.test", "shop"
    )
    mid = store.insert_message(
        account_id=account, folder="INBOX", uid=uid,
        message_id=f"<m{uid}@example.test>", thread_id=f"t{uid}",
        from_addr="sender@example.test", from_name="Sender",
        to_addrs="inbox@example.test", cc_addrs="", subject=f"Hello {uid}",
        received_at=f"2026-07-16T00:00:{uid:02d}+00:00", raw_html=b"",
        sanitized_text=text, has_attachments=0, link_count=0,
    )
    assert mid is not None
    return mid


class _NoBridge:
    def is_up(self):  # pragma: no cover - must never be reached
        raise AssertionError("withheld message reached the LLM bridge")


def test_constants_are_consistent():
    assert AI_READABLE_STATUSES | AI_WITHHELD_STATUSES == SCREENING_STATUSES
    assert not AI_READABLE_STATUSES & AI_WITHHELD_STATUSES
    assert "FILED" in AI_WITHHELD_STATUSES
    assert "FILED" not in QUESTIONABLE_STATUSES
    assert ai_may_read(None) and ai_may_read("") and ai_may_read("content")
    assert not ai_may_read("FILED")
    assert not ai_may_read("SOMETHING_NEW")
    with pytest.raises(ValueError):
        sql_status_list(["x'); DROP TABLE messages; --"])


@pytest.mark.parametrize("status", ["FILED", "SOMETHING_NEW"])
def test_new_or_unknown_status_is_withheld_everywhere(tmp_path, status):
    from mailforge.agent.graph import AgentGraph
    from mailforge.bridge_cli import BridgeInputError, _async_action, _message_json

    with open_store(tmp_path / "gate.db") as store:
        mid = _message(store, 1)
        # Bypass set_message_screening's validation on purpose: an unknown
        # status must still fail closed.
        store.conn.execute(
            "UPDATE messages SET screening_status=? WHERE id=?", (status, mid)
        )
        store.conn.commit()

        assert asyncio.run(
            AgentGraph(store, Settings(), _NoBridge()).process_message(mid)
        ) is None
        assert mid not in store.unprocessed_message_ids()
        assert not store.message_needs_processing(mid)

        payload = _message_json(store.get_message_for_site(mid, "shop"), full=True)
        assert "sanitized_text" not in payload
        assert "body_withheld" in payload

        # An agent cannot probe a withheld body through search either.
        assert store.list_received(q="secret", include_spam=True, ai_caller=True) == []
        assert [
            r["id"] for r in store.list_received(q="secret", include_spam=True)
        ] == [mid]

        with pytest.raises(BridgeInputError):
            asyncio.run(
                _async_action("propose", "shop", {"message_id": mid}, store, Settings())
            )


def test_filed_mail_is_hidden_from_inbox_but_not_questionable(tmp_path):
    with open_store(tmp_path / "filed.db") as store:
        normal = _message(store, 1)
        filed = _message(store, 2)
        store.set_message_screening(filed, "FILED", "filed by category rule github", "RULE")
        store.upsert_classification(
            filed, category="GITHUB", priority=1, rationale="sender rule",
            injection_risk=None, model_used="rule:github",
        )
        inbox = [r["id"] for r in store.list_received()]
        assert inbox == [normal]
        tagged = [
            r["id"]
            for r in store.list_received(message_tag="category:GITHUB", include_spam=True)
        ]
        assert tagged == [filed]
        assert store.list_received(questionable_only=True) == []
        counts = store.triage_counts("shop")
        assert counts["filed"] == 1
        assert counts["unread"] == 1
        assert counts["questionable"] == 0
        assert counts["needs_action"] == 0
        assert store.received_counts()["total"] == 1
        tags = store.message_tag_counts()
        assert tags.get("category:GITHUB") == 1
        assert "screening:FILED" not in tags


def test_model_spam_verdict_moves_mail_to_the_spam_view(tmp_path):
    from mailforge.agent.graph import AgentGraph

    with open_store(tmp_path / "llm.db") as store:
        unknown = _message(store, 1)
        graph = AgentGraph(store, Settings(), None)
        graph._file_model_spam(unknown, store.get_message(unknown), "bulk sales pitch")
        row = store.get_message(unknown)
        assert (row["screening_status"], row["screening_source"]) == ("SPAM", "LLM")
        assert "bulk sales pitch" in row["screening_reason"]
        assert [r["id"] for r in store.list_received(spam_only=True)] == [unknown]
        assert store.list_received() == []

        # A correspondent you have written to stays visible.
        known = _message(store, 2)
        store.record_allowlist("sender@example.test", source="manual")
        graph._file_model_spam(known, store.get_message(known), "x")
        assert store.get_message(known)["screening_status"] == "POTENTIAL_SPAM"

        # A human decision always wins.
        human = _message(store, 3)
        store.record_screening_feedback(human, "CONTENT")
        graph._file_model_spam(human, store.get_message(human), "x")
        assert store.get_message(human)["screening_status"] == "CONTENT"


def test_spam_view_and_spam_tag_agree(tmp_path):
    with open_store(tmp_path / "agree.db") as store:
        mid = _message(store, 1)
        store.upsert_classification(mid, category="SPAM", priority=0, rationale="",
                                    injection_risk=0.0, model_used="m")
        assert [r["id"] for r in store.list_received(spam_only=True)] == [mid]
        assert [
            r["id"] for r in store.list_received(message_tag="spam", include_spam=True)
        ] == [mid]
