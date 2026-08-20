"""Agent-relayed send: token gating, binding, guardrails, rate cap, daily brief."""

from __future__ import annotations

import hashlib

import pytest

from openclaw_email.bridge_cli import (
    BridgeInputError,
    _daily_brief,
    _do_agent_send,
    _prepare_send,
)
from openclaw_email.config import Settings
from openclaw_email.db.store import open_store, register_sites


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


def test_prepare_send_requires_enable_flag(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        did = _seed_reply_draft(store)
        with pytest.raises(BridgeInputError, match="disabled"):
            _prepare_send({"draft_id": did}, "main", store, _settings(False))


def test_prepare_send_issues_single_use_token(tmp_path):
    with open_store(tmp_path / "b.db") as store:
        did = _seed_reply_draft(store)
        out = _prepare_send(
            {"draft_id": did, "author": "email-main"}, "main", store, _settings()
        )
        assert out["recipient"] == "customer@example.test"
        assert out["from"] == "sales@example.test"
        assert out["send_token"]
        assert "explicit yes" in out["confirm_instructions"].lower()
        from openclaw_email.bridge_cli import content_digest

        subject = store.get_draft(did)["subject"]
        body_sha = content_digest(subject, out["body"])
        token_hash = hashlib.sha256(out["send_token"].encode()).hexdigest()
        # a body-only digest (the old contract) no longer matches: the subject
        # is bound into the authorization too
        assert store.consume_send_authorization(
            did, token_hash, hashlib.sha256(out["body"].encode()).hexdigest()
        ) is None
        auth = store.consume_send_authorization(did, token_hash, body_sha)
        assert auth is not None and auth["author"] == "email-main"
        # single-use: a second consume fails
        assert store.consume_send_authorization(did, token_hash, body_sha) is None


def test_prepare_send_refuses_unbound_recipient(tmp_path):
    with open_store(tmp_path / "c.db") as store:
        did = _seed_reply_draft(store)
        store.conn.execute(
            "UPDATE drafts SET recipient='stranger@evil.test' WHERE id=?", (did,)
        )
        store.conn.commit()
        with pytest.raises(BridgeInputError, match="thread participant or allowlisted"):
            _prepare_send({"draft_id": did}, "main", store, _settings())


def test_prepare_send_refuses_quarantined_source(tmp_path):
    with open_store(tmp_path / "d.db") as store:
        did = _seed_reply_draft(store, quarantined=1)
        with pytest.raises(BridgeInputError, match="quarantined"):
            _prepare_send({"draft_id": did}, "main", store, _settings())


def test_send_refuses_bad_or_stale_token(tmp_path):
    with open_store(tmp_path / "e.db") as store:
        did = _seed_reply_draft(store)
        settings = _settings()
        out = _prepare_send({"draft_id": did}, "main", store, settings)
        with pytest.raises(BridgeInputError, match="invalid|expired|changed"):
            _do_agent_send(
                {"draft_id": did, "send_token": "wrong"}, "main", store, settings
            )
        # editing the draft after prepare voids the token
        store.update_draft_state(did, "PENDING", body="edited body after prepare")
        with pytest.raises(BridgeInputError, match="invalid|expired|changed"):
            _do_agent_send(
                {"draft_id": did, "send_token": out["send_token"]},
                "main", store, settings,
            )


def test_send_happy_path_uses_token_once(tmp_path, monkeypatch):
    sent: dict = {}

    def _fake_send_email(**kwargs):
        sent.update(kwargs)

    import openclaw_email.mail.smtp_sender as smtp_sender

    monkeypatch.setattr(smtp_sender, "send_email", _fake_send_email)

    class _Secret:
        def get_secret_value(self):
            return "pw"

    import openclaw_email.secrets as secret_store

    monkeypatch.setattr(secret_store, "get_smtp_secret", lambda addr: _Secret())

    with open_store(tmp_path / "f.db") as store:
        did = _seed_reply_draft(store)
        settings = _settings()
        out = _prepare_send({"draft_id": did, "author": "email-main"}, "main", store, settings)
        result = _do_agent_send(
            {"draft_id": did, "send_token": out["send_token"]}, "main", store, settings
        )
        assert result["state"] == "SENT"
        assert sent["to_addr"] == "customer@example.test"
        assert sent["from_addr"] == "sales@example.test"
        draft = store.get_draft(did)
        assert draft["state"] == "SENT"
        assert str(draft["approved_by"]).startswith("openclaw:email-main")
        # audit trail: approval + send events exist
        events = [
            r["event"] for r in store.conn.execute(
                "SELECT event FROM audit_log WHERE subject_id=?", (did,)
            )
        ]
        assert "approval" in events and "send" in events
        # token is spent — a replay fails
        with pytest.raises(BridgeInputError):
            _do_agent_send(
                {"draft_id": did, "send_token": out["send_token"]},
                "main", store, settings,
            )


def test_send_rate_limit(tmp_path):
    with open_store(tmp_path / "g.db") as store:
        did = _seed_reply_draft(store)
        settings = _settings()
        settings.openclaw.agent_send_per_hour = 1
        # one consumed authorization in the last hour = at the cap
        store.create_send_authorization(did, "h1", "b1", "r@x", "f@x", "a")
        store.conn.execute("UPDATE send_authorizations SET used_at=datetime('now')")
        store.conn.commit()
        out = _prepare_send({"draft_id": did}, "main", store, settings)
        with pytest.raises(BridgeInputError, match="rate limit"):
            _do_agent_send(
                {"draft_id": did, "send_token": out["send_token"]},
                "main", store, settings,
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
