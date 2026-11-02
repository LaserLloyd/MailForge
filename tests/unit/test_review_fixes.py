"""Regression tests for the independent review (REVIEW-OPUS.md): S1-S4, S6-S8."""

from __future__ import annotations

import json

import pytest

from mailforge.bridge_cli import (
    BridgeInputError,
    _compose_draft,
    _edit_draft,
    _reply_draft,
)
from mailforge.bridge_ops import triage_ops
from mailforge.config import Settings
from mailforge.db.store import open_store, register_sites
from mailforge.ui import presend

H = triage_ops.HANDLERS
REPLY = "Hello Vicky,\n\nMy order number is 249-0000000-0000000. Thank you!\n\nJake"


@pytest.fixture(autouse=True)
def _registry():
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
    )


def _inbound(store, uid=1, sender="customer@example.test") -> int:
    account = store.upsert_account("sales", "imap", "imap.example", 993,
                                   "sales@example.test", "main")
    return store.insert_message(
        account_id=account, folder="INBOX", uid=uid, message_id=f"<m{uid}@example.test>",
        thread_id=f"t{uid}", from_addr=sender, from_name="C",
        to_addrs="sales@example.test", cc_addrs="", subject="Question",
        received_at="2026-11-02T00:00:00+00:00", raw_html=b"",
        sanitized_text="Hello", has_attachments=0, link_count=0, quarantined=0,
    )


def _sent(store, to, msgid="<s1@mailforge.example>", outcome="SENT"):
    store.conn.execute(
        "INSERT INTO sent_messages(site_id,origin,from_addr,to_addrs,subject,body,outcome,"
        "smtp_message_id,created_at) VALUES('main','ui_compose','a@x.test',?,'s','b',?,?,"
        "'2026-11-01T00:00:00+00:00')", (to, outcome, msgid))
    store.conn.commit()


# ---- S2: compare-and-swap ------------------------------------------------
def test_update_draft_state_cas(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        d = store.create_manual_draft("a@x.test", "b@y.test", "s", "b")
        assert store.update_draft_state(d, "SENT", expect_states=["APPROVED"]) is False
        assert store.get_draft(d)["state"] == "DRAFT"
        assert store.update_draft_state(d, "PENDING", expect_states=["DRAFT"]) is True


def test_agent_edit_cannot_reopen_a_draft_sent_during_the_guardrails(tmp_path, monkeypatch):
    import mailforge.bridge_cli as bc

    with open_store(tmp_path / "b.db") as store:
        r = _reply_draft({"message_id": _inbound(store), "body": REPLY}, "main", store, _settings())
        did = r["draft_id"]
        real = bc._guard_draft_body

        def slow(*a, **k):  # the owner presses Send while the guardrails run
            store.update_draft_state(did, "SENT", body="what went out")
            return real(*a, **k)

        monkeypatch.setattr(bc, "_guard_draft_body", slow)
        with pytest.raises(BridgeInputError, match="changed|moved|cannot"):
            _edit_draft({"draft_id": did, "body": "Agent text."}, "main", store, _settings())
        row = store.get_draft(did)
        assert (row["state"], row["body"]) == ("SENT", "what went out")


def test_withdraw_cannot_turn_a_sent_draft_into_rejected(tmp_path):
    with open_store(tmp_path / "c.db") as store:
        mid = _inbound(store)
        did = store.create_draft(mid, "t1", "customer@example.test", "Re", "b", "PENDING",
                                 {"agent_written": True})
        stale = store.get_draft(did)
        store.update_draft_state(did, "SENT")
        real = store.get_draft_for_site
        store.get_draft_for_site = lambda *a, **k: stale  # row read before the send landed
        try:
            with pytest.raises(BridgeInputError):
                H["withdraw-draft"]({"draft_id": did}, "main", store, _settings())
        finally:
            store.get_draft_for_site = real
        assert store.get_draft(did)["state"] == "SENT"


# ---- S3: ownership ---------------------------------------------------------
def test_edit_does_not_make_an_owner_draft_agent_owned(tmp_path):
    with open_store(tmp_path / "d.db") as store:
        d = store.create_manual_draft("sales@example.test", "x@vendor.test", "Hi", "Mine")
        with pytest.raises(BridgeInputError, match="owner"):
            _edit_draft({"draft_id": d, "body": "Agent rewrite"}, "main", store, _settings())
        with pytest.raises(BridgeInputError):
            H["withdraw-draft"]({"draft_id": d}, "main", store, _settings())
        assert store.get_draft(d)["state"] == "DRAFT"


def test_edit_keeps_the_original_authorship(tmp_path):
    with open_store(tmp_path / "e.db") as store:
        mid = _inbound(store)
        # a drafter/owner reply (no agent_written flag), PENDING
        did = store.create_draft(mid, "t1", "customer@example.test", "Re", "Hi.", "PENDING", {})
        _edit_draft({"draft_id": did, "body": "Shorter."}, "main", store, _settings())
        flags = json.loads(store.get_draft(did)["guardrail_flags"])
        assert not flags.get("agent_written")
        with pytest.raises(BridgeInputError, match="agent"):
            H["withdraw-draft"]({"draft_id": did}, "main", store, _settings())


def test_agent_compose_is_agent_owned_and_withdrawable(tmp_path):
    with open_store(tmp_path / "f.db") as store:
        n = _compose_draft({"to": "a@vendor.test", "subject": "Hi", "body": "Hello"},
                           "main", store, _settings())
        out = H["withdraw-draft"]({"draft_id": n["draft_id"]}, "main", store, _settings())
        assert out["state"] == "REJECTED"


def test_agent_flag_leaves_an_owner_edited_draft_alone(tmp_path):
    with open_store(tmp_path / "g.db") as store:
        mid = _inbound(store)
        did = store.create_draft(mid, "t1", "customer@example.test", "Re", "b", "DRAFT", {})
        H["flag"]({"id": mid, "label": "POTENTIAL_ISSUE"}, "main", store, _settings())
        assert store.get_draft(did)["state"] == "DRAFT"
        store.record_screening_feedback(mid, "SPAM", actor="user")  # a human mark still rejects
        assert store.get_draft(did)["state"] == "REJECTED"


# ---- S4: reply trust -------------------------------------------------------
def test_thread_trust_needs_the_sender_to_be_a_recipient(tmp_path):
    with open_store(tmp_path / "h.db") as store:
        _sent(store, "Vicky <vicky@vendor.test>, other@vendor.test")
        tid = "<s1@mailforge.example>"
        assert store.thread_started_by_us(tid, "vicky@vendor.test") is True
        assert store.thread_started_by_us(tid, "VICKY@vendor.test") is True
        assert store.thread_started_by_us(tid, "forger@evil.test") is False
        assert store.thread_started_by_us(tid, "") is False


# ---- S1: first contact comes from what we sent ---------------------------
def _eval(store, **over):
    kw = dict(recipients="bob@example.test", subject="Hello",
              body="Thanks for getting in touch. Here is the answer.",
              participants=set(), draft_state="DRAFT", site_id="main")
    kw.update(over)
    return presend.evaluate(store, _settings(), **kw)


def test_allowlisting_does_not_hide_the_first_contact_warning(tmp_path, monkeypatch):
    with open_store(tmp_path / "i.db") as store:
        store.record_allowlist("cold@new.test", source="inbound_thread")  # what the drafter does
        ids = {c.id for c in _eval(store, recipients="cold@new.test").checks}
        assert "first_contact" in ids
        _sent(store, "cold@new.test")
        ids = {c.id for c in _eval(store, recipients="cold@new.test").checks}
        assert "first_contact" not in ids
        # a failed send is not "written to"
        _sent(store, "failed@new.test", msgid="<s2@x>", outcome="FAILED")
        ids = {c.id for c in _eval(store, recipients="failed@new.test").checks}
        assert "first_contact" in ids
        # a thread sender suppresses it
        ids = {c.id for c in _eval(store, recipients="cold2@new.test",
                                   participants={"cold2@new.test"}).checks}
        assert "first_contact" not in ids
