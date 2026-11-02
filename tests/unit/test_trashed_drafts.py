"""F3: a draft must follow its source message through Trash, restore and purge."""

from __future__ import annotations

import asyncio
import json

import pytest

from mailforge.bridge_cli import (
    BridgeInputError,
    _async_action,
    _draft_get,
    _drafts_list,
    _edit_draft,
    _reply_draft,
)
from mailforge.config import Settings
from mailforge.db.store import open_store, register_sites


@pytest.fixture(autouse=True)
def _default_registry():
    register_sites(["main", "shop"])
    yield
    register_sites(["main", "shop"])


def _settings() -> Settings:
    return Settings(
        imap_accounts=[
            {"name": "sales", "site_id": "main", "host": "imap.example",
             "username": "sales@example.test"},
        ],
        smtp={"host": "smtp.example", "port": 587, "username": "sales@example.test"},
        openclaw={"agent_compose_per_day": 50},
    )


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


REPLY = "Hello,\n\nThanks for writing; we will look into it.\n\nJake"
PURGED_AT = "2026-10-01T00:00:00+00:00"


def _draft(store, mid, state="PENDING") -> int:
    return store.create_draft(mid, f"t{mid}", "customer@example.test", "Re: Question",
                              REPLY, state, {})


def _auth(store, draft_id, token="tok") -> int:
    return store.create_send_authorization(draft_id, token, "sha", "customer@example.test",
                                           "sales@example.test", "agent")


def _force_purged(store, mid) -> None:
    store.conn.execute("UPDATE messages SET purged_at=?,trashed=1 WHERE id=?", (PURGED_AT, mid))
    store.conn.commit()


def test_trashed_source_hides_draft_everywhere_and_restore_brings_it_back(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        mid = _inbound(store)
        did = _draft(store, mid)
        assert [r["id"] for r in store.drafts_overview()] == [did]
        assert store.draft_counts("main") == {"PENDING": 1}
        assert store.dashboard_snapshot("main")["draft_total"] == 1

        store.set_messages_trashed([mid], True)
        assert store.drafts_overview() == []
        assert store.drafts_overview("PENDING") == []
        assert store.draft_counts("main") == {}
        assert store.draft_counts() == {}
        assert store.dashboard_snapshot("main")["draft_total"] == 0
        assert _drafts_list({}, "main", store, _settings())["drafts"] == []
        # trash hides, it does not mutate
        assert store.get_draft(did)["state"] == "PENDING"

        store.set_messages_trashed([mid], False)
        assert [r["id"] for r in store.drafts_overview()] == [did]
        assert store.draft_counts("main") == {"PENDING": 1}
        listed = _drafts_list({}, "main", store, _settings())["drafts"]
        assert [d["draft_id"] for d in listed] == [did]


def test_manual_drafts_are_never_hidden(tmp_path):
    with open_store(tmp_path / "m.db") as store:
        manual = store.create_manual_draft("sales@example.test", "x@example.test", "Hi", "Body")
        assert store.draft_counts("main") == {"DRAFT": 1}
        assert [r["id"] for r in store.drafts_overview()] == [manual]


def test_include_inactive_lists_hidden_drafts_with_their_source_state(tmp_path):
    with open_store(tmp_path / "b.db") as store:
        mid = _inbound(store)
        did = _draft(store, mid)
        store.set_messages_trashed([mid], True)
        out = _drafts_list({"include_inactive": True}, "main", store, _settings())
        assert [d["draft_id"] for d in out["drafts"]] == [did]
        assert out["drafts"][0]["active"] is False
        assert out["drafts"][0]["source_state"] == "trashed"
        got = _draft_get({"draft_id": did}, "main", store, _settings())
        assert got["active"] is False


def test_purge_retires_open_drafts_and_revokes_tokens_but_not_sent_ones(tmp_path):
    with open_store(tmp_path / "c.db") as store:
        mid = _inbound(store)
        open_id = _draft(store, mid, "PENDING")
        sent_mid = _inbound(store, uid=2)
        sent_id = _draft(store, sent_mid, "SENT")
        manual = store.create_manual_draft("sales@example.test", "x@example.test", "Hi", "Body")
        auth_id = _auth(store, open_id)
        store.set_messages_trashed([mid, sent_mid], True)
        store.purge_messages([mid, sent_mid])

        d = store.get_draft(open_id)
        assert d["state"] == "REJECTED"
        assert json.loads(d["guardrail_flags"])["reason"] == "source_purged"
        assert store.get_draft(sent_id)["state"] == "SENT"
        assert store.get_draft(manual)["state"] == "DRAFT"
        assert store.consume_send_authorization(open_id, "tok", "sha") is None
        row = store.conn.execute("SELECT * FROM send_authorizations WHERE id=?",
                                 (auth_id,)).fetchone()
        assert row["used_at"] is None  # revoked, not "used": the hourly cap stays honest
        assert store.agent_sends_last_hour() == 0


def test_migration_retires_open_drafts_of_already_purged_sources_idempotently(tmp_path):
    path = tmp_path / "d.db"
    with open_store(path) as store:
        mid = _inbound(store)
        did = _draft(store, mid, "DRAFT")
        _auth(store, did)
        _force_purged(store, mid)  # pre-fix data: purged without the retire step
        assert store.get_draft(did)["state"] == "DRAFT"
    with open_store(path) as store:
        assert store.get_draft(did)["state"] == "REJECTED"
        assert json.loads(store.get_draft(did)["guardrail_flags"])["reason"] == "source_purged"
        assert store.consume_send_authorization(did, "tok", "sha") is None
        updated = store.get_draft(did)["updated_at"]
    with open_store(path) as store:  # a second open changes nothing
        assert store.get_draft(did)["updated_at"] == updated


@pytest.mark.parametrize("label", ["SPAM", "POTENTIAL_SPAM", "POTENTIAL_ISSUE"])
def test_screening_feedback_rejects_a_draft_state_reply(tmp_path, label):
    with open_store(tmp_path / f"e{label}.db") as store:
        mid = _inbound(store)
        did = _draft(store, mid, "DRAFT")  # a reply the owner edited in the UI
        store.record_screening_feedback(mid, label)
        assert store.get_draft(did)["state"] == "REJECTED"


def test_content_feedback_leaves_the_draft_alone(tmp_path):
    with open_store(tmp_path / "f.db") as store:
        mid = _inbound(store)
        did = _draft(store, mid, "DRAFT")
        store.record_screening_feedback(mid, "CONTENT")
        assert store.get_draft(did)["state"] == "DRAFT"


@pytest.mark.parametrize("how", ["trashed", "purged"])
def test_reply_and_edit_refuse_trashed_or_purged_sources(tmp_path, how):
    with open_store(tmp_path / f"g{how}.db") as store:
        mid = _inbound(store)
        did = _draft(store, mid)
        mid2 = _inbound(store, uid=2)
        for m in (mid, mid2):
            store.set_messages_trashed([m], True)
            if how == "purged":
                _force_purged(store, m)
        with pytest.raises(BridgeInputError, match=how):
            _edit_draft({"draft_id": did, "body": REPLY + "!"}, "main", store, _settings())
        with pytest.raises(BridgeInputError, match=how):
            _reply_draft({"message_id": mid2, "body": REPLY}, "main", store, _settings())


def test_propose_and_revise_refuse_a_trashed_source(tmp_path):
    with open_store(tmp_path / "h.db") as store:
        mid = _inbound(store)
        did = _draft(store, mid)
        store.set_messages_trashed([mid], True)
        with pytest.raises(BridgeInputError, match="trashed"):
            asyncio.run(_async_action("propose", "main", {"message_id": mid}, store, _settings()))
        with pytest.raises(BridgeInputError, match="trashed"):
            asyncio.run(_async_action("revise", "main",
                                      {"draft_id": did, "feedback": "shorter"},
                                      store, _settings()))


def test_edit_draft_revokes_staged_authorizations(tmp_path):
    with open_store(tmp_path / "i.db") as store:
        mid = _inbound(store)
        did = _draft(store, mid)
        _auth(store, did)
        _edit_draft({"draft_id": did, "body": REPLY + " Thanks."}, "main", store, _settings())
        assert store.consume_send_authorization(did, "tok", "sha") is None
