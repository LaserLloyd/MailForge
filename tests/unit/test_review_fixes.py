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


# ---- S2 (UI side): the approve/restore/sent transitions are compare-and-swap
def test_approve_refuses_when_the_draft_moved_on_after_the_check(tmp_path, monkeypatch):
    from mailforge.ui.pages import detail

    calls = []
    monkeypatch.setattr(detail, "_do_send", lambda *a, **k: calls.append(1) or 1)
    with open_store(tmp_path / "u.db") as store:
        did = store.create_draft(None, "t", "bob@example.test", "Re", "b", "PENDING",
                                 sender_addr="inbox@main.example")
        draft = store.get_draft(did)

        def flip(*a, **k):  # another process (the bridge) rejects it right now
            store.update_draft_state(did, "REJECTED")
            return []

        monkeypatch.setattr(presend, "record_override", flip)
        with pytest.raises(RuntimeError):
            detail.approve_and_transmit(store, _settings(), draft, "b", "bob@example.test", {}, [])
        assert calls == [] and store.get_draft(did)["state"] == "REJECTED"


def test_restore_after_failed_send_is_a_swap_from_approved(tmp_path):
    from mailforge.ui.pages import detail

    with open_store(tmp_path / "v.db") as store:
        did = store.create_draft(None, "t", "bob@example.test", "Re", "b", "SENT")
        assert detail._restore_retryable_after_send_failure(
            store, did, "b", {}, RuntimeError("x")) is False
        assert store.get_draft(did)["state"] == "SENT"


def test_ui_save_and_reject_refuse_a_moved_row(tmp_path):
    from mailforge.ui.pages import detail

    with open_store(tmp_path / "w.db") as store:
        did = store.create_draft(None, "t", "bob@example.test", "Re", "b", "SENT")
        assert detail.reject_draft(store, did) is False
        assert detail.save_draft_text(store, did, "PENDING", "new", {}) is False
        row = store.get_draft(did)
        assert (row["state"], row["body"]) == ("SENT", "b")
        pid = store.create_draft(None, "t", "bob@example.test", "Re", "b", "PENDING")
        assert detail.save_draft_text(store, pid, "PENDING", "new", {}) is True
        assert detail.reject_draft(store, pid) is True


# ---- B1: a double click inside the pre-send dialog sends once ---------------
def _drive_dialog(monkeypatch, clicks, *, flight_key=None, gap=0.02, recheck=0.15):
    import asyncio
    from unittest.mock import MagicMock

    fake_ui = MagicMock()
    buttons = {}

    def button(label="", **kw):
        b = MagicMock(name=f"btn:{label}")
        b.on_click = kw.get("on_click")
        b.enabled = True
        b.set_enabled.side_effect = lambda v: setattr(b, "enabled", bool(v))
        b.props.return_value = b
        buttons[label] = b
        return b

    fake_ui.button.side_effect = button
    fake_ui.checkbox.return_value.value = False
    fake_ui.context.client.id = "page-1"
    monkeypatch.setattr(presend, "ui", fake_ui)

    calls = {"collect": 0, "transmit": 0}

    async def io_bound(fn, *a, **k):
        if calls["collect"] >= 1:  # the re-check is slow, like Presidio + the LLM guard
            await asyncio.sleep(recheck)
        return fn(*a, **k)

    monkeypatch.setattr(presend, "run", MagicMock(io_bound=io_bound))

    def collect():
        calls["collect"] += 1
        return [presend.Check("ok", "Fine", True)]

    async def transmit(_over):
        calls["transmit"] += 1
        await asyncio.sleep(0.05)
        return None

    async def main():
        task = asyncio.ensure_future(presend.confirm(
            title="Send?", facts=[("To", "bob@example.test")], collect=collect,
            transmit=transmit, persist=lambda _r: None, on_failed=lambda e: None,
            store=None, **({"flight_key": flight_key} if flight_key else {}),
        ))
        await asyncio.sleep(0.05)
        send = buttons["Send"]
        click_tasks = []
        for _ in range(clicks):
            click_tasks.append(asyncio.ensure_future(send.on_click()))
            await asyncio.sleep(gap)
            if _ == 0:
                first_state = (send.enabled, [c.args for c in send.set_text.call_args_list])
        await asyncio.gather(*click_tasks)
        res = await asyncio.wait_for(task, 5)
        return res, first_state

    res, first_state = asyncio.run(main())
    return calls, res, first_state


def test_double_click_in_the_dialog_transmits_once(monkeypatch):
    calls, res, (enabled, texts) = _drive_dialog(monkeypatch, clicks=2)
    assert calls["transmit"] == 1
    assert res is not None and res.sent
    # the button was disabled and relabelled before the slow re-check finished
    assert enabled is False
    assert any("ending" in t[0] for t in texts)


def test_single_flight_key_blocks_a_second_dialog_for_the_same_draft(monkeypatch):
    presend.release_flight(["draft:7"])
    assert presend.claim_flight(["draft:7"]) is True
    try:
        assert presend.claim_flight(["draft:7", "page:x"]) is False
        assert presend.claim_flight(["page:x"]) is True  # all-or-nothing: x was not taken
        presend.release_flight(["page:x"])
    finally:
        presend.release_flight(["draft:7"])
    assert presend.claim_flight(["draft:7"]) is True
    presend.release_flight(["draft:7"])


# ---- S6: the agent_notes rebuild is atomic ----------------------------------
_OLD_NOTES = (
    "CREATE TABLE agent_notes (id INTEGER PRIMARY KEY, site_id TEXT NOT NULL, "
    "author TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'observation' "
    "CHECK (kind IN ('observation','action','incident','context')), "
    "title TEXT NOT NULL, body TEXT NOT NULL, related_message_ids TEXT, "
    "created_at TEXT NOT NULL)"
)


class _FailOnDrop:
    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, *a):
        if "DROP TABLE agent_notes_old" in sql:
            raise OSError("disk full")
        return self._c.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _old_notes_store(store):
    store.conn.execute("DROP TABLE agent_notes")
    store.conn.execute(_OLD_NOTES)
    store.conn.execute("INSERT INTO agent_notes VALUES(1,'main','bits','observation','t','b',NULL,'x')")
    store.conn.commit()


def test_notes_rebuild_failure_leaves_the_old_table_intact(tmp_path):
    with open_store(tmp_path / "n.db") as store:
        _old_notes_store(store)
        real = store.conn
        store.conn = _FailOnDrop(real)
        try:
            with pytest.raises(OSError):
                store._migrate_agent_notes_request_kind()
        finally:
            store.conn = real
        names = {r[0] for r in real.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "agent_notes_old" not in names
        assert real.execute("SELECT COUNT(*) FROM agent_notes").fetchone()[0] == 1
        store._migrate_agent_notes_request_kind()  # the retry completes
        assert real.execute("SELECT COUNT(*) FROM agent_notes").fetchone()[0] == 1
        real.execute("INSERT INTO agent_notes(site_id,author,kind,title,body,created_at) "
                     "VALUES('main','a','request','t','b','x')")


def test_stranded_agent_notes_old_is_recovered_at_start(tmp_path):
    with open_store(tmp_path / "o.db") as store:
        store.conn.execute("ALTER TABLE agent_notes RENAME TO agent_notes_old")
        store.conn.execute(_OLD_NOTES)
        store.conn.execute("DROP TABLE agent_notes")
        store.conn.execute(
            "CREATE TABLE agent_notes (id INTEGER PRIMARY KEY, site_id TEXT NOT NULL, "
            "author TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'observation' "
            "CHECK (kind IN ('observation','action','incident','context','request')), "
            "title TEXT NOT NULL, body TEXT NOT NULL, related_message_ids TEXT, "
            "created_at TEXT NOT NULL)")
        store.conn.execute("INSERT INTO agent_notes_old VALUES(1,'main','b','action','t','b',NULL,'x')")
        store.conn.commit()
        store._migrate_agent_notes_request_kind()
        names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "agent_notes_old" not in names
        assert store.conn.execute("SELECT COUNT(*) FROM agent_notes").fetchone()[0] == 1


# ---- S7: rule (b)'s reply exemption does not cover flagged senders -----------
def test_flagged_sender_does_not_get_the_domain_rule_exemption(tmp_path):
    from tests.unit.test_guardrail_false_positives import _check, _settings as gs, _thread

    s = gs()
    with open_store(tmp_path / "p.db") as store:
        _thread(store, "support@vendor.test")
        assert not any("(b)" in r for r in _check(store, s, "support@vendor.test").reasons)
        store.conn.execute("UPDATE messages SET screening_status='POTENTIAL_ISSUE'")
        store.conn.commit()
        assert any("(b)" in r for r in _check(store, s, "support@vendor.test").reasons)
