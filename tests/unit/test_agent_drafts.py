"""Agent draft tools: list/read drafts, agent-written replies, direct edits.

None of these can send: replies land PENDING for a human to approve in the UI.
"""

from __future__ import annotations

import pytest

from mailforge.bridge_cli import (
    BridgeInputError,
    _compose_draft,
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


def _settings(per_day: int = 20) -> Settings:
    return Settings(
        imap_accounts=[
            {"name": "sales", "site_id": "main", "host": "imap.example",
             "username": "sales@example.test"},
            {"name": "shop", "site_id": "shop", "host": "imap.example",
             "username": "orders@shop.test"},
        ],
        smtp={"host": "smtp.example", "port": 587, "username": "sales@example.test"},
        openclaw={"agent_compose_per_day": per_day},
    )


def _inbound(store, *, site="main", uid=1, quarantined=0, subject="Question",
             sender="customer@example.test") -> int:
    name, user = ("sales", "sales@example.test") if site == "main" else ("shop", "orders@shop.test")
    account = store.upsert_account(name, "imap", "imap.example", 993, user, site)
    return store.insert_message(
        account_id=account, folder="INBOX", uid=uid, message_id=f"<m{uid}@example.test>",
        thread_id=f"t{uid}", from_addr=sender, from_name="Customer",
        to_addrs=user, cc_addrs="", subject=subject,
        received_at="2026-11-02T00:00:00+00:00", raw_html=b"",
        sanitized_text="Hello", has_attachments=0, link_count=0,
        quarantined=quarantined,
    )


REPLY = "Hello Vicky,\n\nMy order number is 249-0000000-0000000. Thank you!\n\nJake"


# ----- reply -----

def test_reply_saves_pending_threaded_draft_and_sends_nothing(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        mid = _inbound(store)
        out = _reply_draft({"message_id": mid, "body": REPLY, "author": "ds_flash"},
                           "main", store, _settings())
        d = store.get_draft(out["draft_id"])
        assert d["state"] == "PENDING"
        assert d["message_id"] == mid
        assert d["thread_id"] == "t1"
        assert d["recipient"] == "customer@example.test"
        assert d["sender_addr"] == "sales@example.test"
        assert d["subject"] == "Re: Question"
        assert d["body"] == REPLY
        assert out["review_path"] == f"/mail/{mid}"
        assert "nothing was sent" in out["next_step"].lower()
        assert store.conn.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0] == 0
        row = store.conn.execute(
            "SELECT actor, json_extract(detail_json,'$.tool') FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(row) == ("agent", "reply_draft")


def test_reply_keeps_existing_re_prefix_and_takes_custom_subject(tmp_path):
    with open_store(tmp_path / "b.db") as store:
        mid = _inbound(store, subject="RE: Order 12")
        out = _reply_draft({"message_id": mid, "body": REPLY}, "main", store, _settings())
        assert out["subject"] == "RE: Order 12"
        mid2 = _inbound(store, uid=2)
        out2 = _reply_draft({"message_id": mid2, "body": REPLY, "subject": "Re: Question (order)"},
                            "main", store, _settings())
        assert out2["subject"] == "Re: Question (order)"


def test_reply_is_site_bound_and_respects_quarantine_and_screening(tmp_path):
    with open_store(tmp_path / "c.db") as store:
        other = _inbound(store, site="shop", uid=5)
        with pytest.raises(BridgeInputError, match="not found for site"):
            _reply_draft({"message_id": other, "body": REPLY}, "main", store, _settings())
        q = _inbound(store, uid=6, quarantined=1)
        with pytest.raises(BridgeInputError, match="quarantined"):
            _reply_draft({"message_id": q, "body": REPLY}, "main", store, _settings())
        w = _inbound(store, uid=7)
        store.conn.execute("UPDATE messages SET screening_status='POTENTIAL_SPAM' WHERE id=?", (w,))
        with pytest.raises(BridgeInputError, match="screening"):
            _reply_draft({"message_id": w, "body": REPLY}, "main", store, _settings())


def test_reply_refuses_second_draft_and_points_at_edit(tmp_path):
    with open_store(tmp_path / "d.db") as store:
        mid = _inbound(store)
        first = _reply_draft({"message_id": mid, "body": REPLY}, "main", store, _settings())
        with pytest.raises(BridgeInputError, match=f"draft {first['draft_id']}.*edit"):
            _reply_draft({"message_id": mid, "body": "again"}, "main", store, _settings())


def test_reply_replaces_a_deferred_placeholder(tmp_path):
    with open_store(tmp_path / "e.db") as store:
        mid = _inbound(store)
        placeholder = store.create_draft(mid, "t1", "customer@example.test", "Re: Question",
                                         "", "DEFERRED_NO_LLM", {})
        out = _reply_draft({"message_id": mid, "body": REPLY}, "main", store, _settings())
        assert out["draft_id"] == placeholder
        assert store.get_draft(placeholder)["state"] == "PENDING"


@pytest.mark.parametrize("over,match", [
    ({"body": "  "}, "body"),
    ({"body": "x" * 20001}, "body"),
    ({"subject": "two\nlines"}, "subject"),
])
def test_reply_validates_input(tmp_path, over, match):
    with open_store(tmp_path / "f.db") as store:
        mid = _inbound(store)
        with pytest.raises(BridgeInputError, match=match):
            _reply_draft({"message_id": mid, "body": REPLY, **over}, "main", store, _settings())


def test_reply_guardrails_block_and_save_nothing(tmp_path):
    with open_store(tmp_path / "g.db") as store:
        mid = _inbound(store)
        leaked = "key: AKIAIOSFODNN7EXAMPLE"  # scrub-check: allow
        with pytest.raises(BridgeInputError, match="guardrails blocked"):
            _reply_draft({"message_id": mid, "body": leaked}, "main", store, _settings())
        assert store.latest_draft_for_message(mid) is None


def test_reply_shares_the_daily_cap_with_compose(tmp_path):
    with open_store(tmp_path / "h.db") as store:
        s = _settings(per_day=2)
        _compose_draft({"to": "a@vendor.test", "subject": "Hi", "body": "Hello"}, "main", store, s)
        _reply_draft({"message_id": _inbound(store, uid=1), "body": REPLY}, "main", store, s)
        with pytest.raises(BridgeInputError, match="cap"):
            _reply_draft({"message_id": _inbound(store, uid=2), "body": REPLY}, "main", store, s)


# ----- drafts / draft -----

def test_drafts_lists_reply_and_new_drafts_for_this_site_only(tmp_path):
    with open_store(tmp_path / "i.db") as store:
        r = _reply_draft({"message_id": _inbound(store), "body": REPLY}, "main", store, _settings())
        n = _compose_draft({"to": "a@vendor.test", "subject": "Hi", "body": "Hello"},
                           "main", store, _settings())
        store.create_manual_draft("orders@shop.test", "b@x.test", "Other site", "x", site_id="shop")
        out = _drafts_list({}, "main", store, _settings())
        by_id = {row["draft_id"]: row for row in out["drafts"]}
        assert set(by_id) == {r["draft_id"], n["draft_id"]}
        assert by_id[r["draft_id"]]["kind"] == "reply"
        assert by_id[n["draft_id"]]["kind"] == "new"
        assert "Hello Vicky" in by_id[r["draft_id"]]["snippet"]
        only_pending = _drafts_list({"state": "pending"}, "main", store, _settings())
        assert [row["draft_id"] for row in only_pending["drafts"]] == [r["draft_id"]]
        with pytest.raises(BridgeInputError, match="state"):
            _drafts_list({"state": "nope"}, "main", store, _settings())


def test_draft_get_returns_body_and_review_path(tmp_path):
    with open_store(tmp_path / "j.db") as store:
        mid = _inbound(store)
        r = _reply_draft({"message_id": mid, "body": REPLY}, "main", store, _settings())
        out = _draft_get({"draft_id": r["draft_id"]}, "main", store, _settings())
        assert out["body"] == REPLY
        assert out["kind"] == "reply" and out["message_id"] == mid
        assert out["review_path"] == f"/mail/{mid}"
        with pytest.raises(BridgeInputError, match="not found for site"):
            _draft_get({"draft_id": r["draft_id"]}, "shop", store, _settings())


def test_blocked_and_withheld_draft_bodies_are_never_returned(tmp_path):
    with open_store(tmp_path / "k.db") as store:
        mid = _inbound(store)
        blocked = store.create_draft(mid, "t1", "customer@example.test", "Re: Question",
                                     "secret stuff", "BLOCKED", {})
        assert "body" not in _draft_get({"draft_id": blocked}, "main", store, _settings())
        listed = _drafts_list({}, "main", store, _settings())["drafts"]
        assert "snippet" not in [r for r in listed if r["draft_id"] == blocked][0]
        store.conn.execute("UPDATE drafts SET state='PENDING' WHERE id=?", (blocked,))
        store.conn.execute("UPDATE messages SET quarantined=1 WHERE id=?", (mid,))
        out = _draft_get({"draft_id": blocked}, "main", store, _settings())
        assert "body" not in out and "withheld" in out["body_withheld"]


# ----- edit-draft -----

def test_edit_reply_draft_replaces_text_and_keeps_history(tmp_path):
    with open_store(tmp_path / "l.db") as store:
        r = _reply_draft({"message_id": _inbound(store), "body": REPLY}, "main", store, _settings())
        out = _edit_draft({"draft_id": r["draft_id"], "body": "Shorter reply.", "author": "ds_flash"},
                          "main", store, _settings())
        d = store.get_draft(r["draft_id"])
        assert d["body"] == "Shorter reply." and d["state"] == "PENDING"
        assert out["body"] == "Shorter reply."
        revs = store.list_ai_revisions(r["draft_id"])
        assert [rv["body"] for rv in revs] == [REPLY, "Shorter reply."]
        row = store.conn.execute(
            "SELECT json_extract(detail_json,'$.tool') FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "edit_draft"


def test_edit_new_draft_keeps_draft_state_and_can_change_subject(tmp_path):
    with open_store(tmp_path / "m.db") as store:
        n = _compose_draft({"to": "a@vendor.test", "subject": "Hi", "body": "Hello"},
                           "main", store, _settings())
        _edit_draft({"draft_id": n["draft_id"], "body": "Hello again", "subject": "Hi there"},
                    "main", store, _settings())
        d = store.get_draft(n["draft_id"])
        assert (d["state"], d["subject"], d["body"]) == ("DRAFT", "Hi there", "Hello again")


def test_edit_refuses_sent_drafts_other_sites_and_guardrail_failures(tmp_path):
    with open_store(tmp_path / "n.db") as store:
        r = _reply_draft({"message_id": _inbound(store), "body": REPLY}, "main", store, _settings())
        leaked = "key: AKIAIOSFODNN7EXAMPLE"  # scrub-check: allow
        with pytest.raises(BridgeInputError, match="guardrails blocked"):
            _edit_draft({"draft_id": r["draft_id"], "body": leaked}, "main", store, _settings())
        assert store.get_draft(r["draft_id"])["body"] == REPLY  # untouched
        with pytest.raises(BridgeInputError, match="not found for site"):
            _edit_draft({"draft_id": r["draft_id"], "body": "x"}, "shop", store, _settings())
        store.conn.execute("UPDATE drafts SET state='SENT' WHERE id=?", (r["draft_id"],))
        with pytest.raises(BridgeInputError, match="cannot be edited"):
            _edit_draft({"draft_id": r["draft_id"], "body": "x"}, "main", store, _settings())
