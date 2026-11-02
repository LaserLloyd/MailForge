"""Fable review fixes H1-H4 and M1 (2026-11-02), each with its repro."""

from __future__ import annotations

import asyncio
import types

import pytest

from mailforge.agent import graph as graph_mod
from mailforge.agent.graph import AgentGraph
from mailforge.agent.revisions import revise_draft
from mailforge.bridge_cli import BridgeInputError, _reply_draft
from mailforge.bridge_ops import triage_ops
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


def _inbound(store, uid=1, sender="customer@example.test") -> int:
    account = store.upsert_account("sales", "imap", "imap.example", 993,
                                   "sales@example.test", "main")
    return store.insert_message(
        account_id=account, folder="INBOX", uid=uid, message_id=f"<m{uid}@example.test>",
        thread_id=f"t{uid}", from_addr=sender, from_name="Customer",
        to_addrs="sales@example.test", cc_addrs="", subject="Question",
        received_at="2026-11-02T00:00:00+00:00", raw_html=b"",
        sanitized_text="Hello", has_attachments=0, link_count=0,
    )


BODY = "Hello,\n\nThanks for writing; we will look into it.\n\nJake"


class _DownBridge:
    def is_up(self):
        return False


class _UpBridge:
    model_id = "fake"

    def is_up(self):
        return True

    async def embed(self, texts):
        return []


# ----- H1 -----

def test_h1_deferred_drafts_skip_trashed_mail(tmp_path):
    with open_store(tmp_path / "h1a.db") as store:
        mid = _inbound(store)
        store.create_draft(mid, "t1", "customer@example.test", "Re: Q", "",
                           "DEFERRED_NO_LLM", {})
        assert len(store.deferred_drafts()) == 1
        store.set_messages_trashed([mid], True)
        assert store.deferred_drafts() == []
        store.set_messages_trashed([mid], False)
        assert len(store.deferred_drafts()) == 1


def test_h1_process_message_refuses_trashed_mail(tmp_path):
    with open_store(tmp_path / "h1b.db") as store:
        mid = _inbound(store)
        store.set_messages_trashed([mid], True)

        class Bridge:
            def is_up(self):  # pragma: no cover - must not be reached
                raise AssertionError("trashed mail reached the LLM")

        assert asyncio.run(AgentGraph(store, _settings(), Bridge()).process_message(mid)) is None
        assert store.latest_draft_for_message(mid) is None


def test_h1_trashed_mid_run_gets_no_draft(tmp_path, monkeypatch):
    with open_store(tmp_path / "h1c.db") as store:
        mid = _inbound(store)

        async def plan(*_a, **_k):
            return types.SimpleNamespace(should_draft=True)

        async def classify(*_a, **_k):
            return types.SimpleNamespace(category="RESPOND", priority=1, rationale="r")

        async def draft_reply(*_a, **_k):
            store.set_messages_trashed([mid], True)  # owner trashes during the LLM await
            return types.SimpleNamespace(body=BODY, subject="Re: Question")

        monkeypatch.setattr(graph_mod, "freeze_plan", plan)
        monkeypatch.setattr(graph_mod.worker, "classify", classify)
        monkeypatch.setattr(graph_mod.worker, "draft_reply", draft_reply)
        monkeypatch.setattr(graph_mod, "asdict", lambda _p: {})
        out = asyncio.run(AgentGraph(store, _settings(), _UpBridge()).process_message(mid))
        assert out is None
        assert store.latest_draft_for_message(mid) is None


# ----- H2 -----

def test_h2_revise_never_resurrects_a_sent_draft(tmp_path, monkeypatch):
    from mailforge.agent import revisions

    with open_store(tmp_path / "h2.db") as store:
        mid = _inbound(store)
        did = store.create_draft(mid, "t1", "customer@example.test", "Re: Question",
                                 BODY, "PENDING", {})

        async def revise_reply(*_a, **_k):
            # the owner sends while the model is thinking
            store.update_draft_state(did, "SENT", expect_states=None, sent_at="2026-11-02T01:00:00+00:00")
            return types.SimpleNamespace(body=BODY + "\n\nP.S. shorter.", subject="Re: Question")

        async def retrieve(*_a, **_k):
            return []

        monkeypatch.setattr(revisions.worker, "revise_reply", revise_reply)
        import mailforge.rag.retriever as retr
        monkeypatch.setattr(retr, "retrieve", retrieve)
        with pytest.raises(ValueError, match="changed"):
            asyncio.run(revise_draft(store, _settings(), _UpBridge(), did, "shorter",
                                     site_id="main"))
        assert store.get_draft(did)["state"] == "SENT"
        assert store.get_draft(did)["body"] == BODY


def test_h2_update_draft_state_requires_expect_states(tmp_path):
    with open_store(tmp_path / "h2b.db") as store:
        mid = _inbound(store)
        did = store.create_draft(mid, "t1", "customer@example.test", "Re: Q", BODY, "PENDING", {})
        with pytest.raises(TypeError):
            store.update_draft_state(did, "SENT")  # type: ignore[call-arg]
        assert store.update_draft_state(did, "SENT", expect_states=None) is True


# ----- H3 -----

def test_h3_flag_refuses_over_a_human_decision(tmp_path):
    with open_store(tmp_path / "h3.db") as store:
        mid = _inbound(store)
        store.record_screening_feedback(mid, "CONTENT", actor="user")
        assert store.get_message(mid)["screening_source"] == "USER"
        with pytest.raises(Exception, match="human"):
            triage_ops.HANDLERS["flag"](
                {"id": mid, "label": "POTENTIAL_SPAM", "author": "ds_flash"},
                "main", store, _settings())
        row = store.get_message(mid)
        assert row["screening_status"] == "CONTENT"
        assert row["screening_source"] == "USER"


# ----- H4 -----

def test_h4_ai_revise_passes_for_the_mails_own_sender(tmp_path, monkeypatch):
    from mailforge.agent import revisions

    with open_store(tmp_path / "h4.db") as store:
        mid = _inbound(store, sender="hello@vendor.example")
        # a working draft whose thread id is not the mail's thread (UI fallback id)
        did = store.create_draft(mid, f"message:{mid}", "hello@vendor.example", "Re: Question",
                                 BODY, "PENDING", {})

        async def revise_reply(*_a, **_k):
            return types.SimpleNamespace(body=BODY + "\n\nBest, the owner", subject="Re: Question")

        async def retrieve(*_a, **_k):
            return []

        monkeypatch.setattr(revisions.worker, "revise_reply", revise_reply)
        import mailforge.rag.retriever as retr
        monkeypatch.setattr(retr, "retrieve", retrieve)
        out = asyncio.run(revise_draft(store, _settings(), _UpBridge(), did, "warmer",
                                       site_id="main"))
        assert out["guardrail_passed"], out["guardrail_reasons"]


def test_h4_ui_helper_supplies_participants(tmp_path):
    from mailforge.ui.pages.assistant import _message_participants

    with open_store(tmp_path / "h4b.db") as store:
        mid = _inbound(store, sender="hello@vendor.example")
        people = _message_participants(store, store.get_message(mid))
        assert "hello@vendor.example" in people
        assert "sales@example.test" in people


# ----- M1 -----

def test_m1_bridge_reply_after_withdraw(tmp_path):
    with open_store(tmp_path / "m1.db") as store:
        mid = _inbound(store)
        first = _reply_draft({"message_id": mid, "body": BODY, "author": "ds_flash"},
                             "main", store, _settings())
        triage_ops.HANDLERS["withdraw-draft"](
            {"draft_id": first["draft_id"], "author": "ds_flash"}, "main", store, _settings())
        assert store.get_draft(first["draft_id"])["state"] == "REJECTED"
        second = _reply_draft({"message_id": mid, "body": BODY + " Again.", "author": "ds_flash"},
                              "main", store, _settings())
        assert second["draft_id"] != first["draft_id"]
        assert store.get_draft(second["draft_id"])["state"] == "PENDING"
        assert store.get_draft(first["draft_id"])["state"] == "REJECTED"
        # a live (non-rejected) draft still blocks a second one
        with pytest.raises(BridgeInputError, match="already exists"):
            _reply_draft({"message_id": mid, "body": BODY, "author": "ds_flash"},
                         "main", store, _settings())


def test_m1_ui_working_draft_supersedes_rejected(tmp_path):
    from mailforge.ui.pages.assistant import _create_working_draft

    with open_store(tmp_path / "m1b.db") as store:
        mid = _inbound(store)
        old = store.create_draft(mid, "t1", "customer@example.test", "Re: Question",
                                 BODY, "REJECTED", {})
        new = _create_working_draft(store, store.get_message(mid), "")
        assert new != old
        assert store.get_draft(new)["state"] == "DRAFT"
        # a SENT draft is still reused (immutable, handled by the caller)
        sent = store.create_draft(_inbound(store, uid=2), "t2", "customer@example.test",
                                  "Re: Question", BODY, "SENT", {})
        assert _create_working_draft(
            store, store.get_message(store.get_draft(sent)["message_id"]), "") == sent
