"""Bridge triage / read ops for agents: archive, trash, flag, mark-read, withdraw,
outbox, sent, trash-queue, attachments, config-summary, activity, list views."""

from __future__ import annotations

import json

import pytest

from mailforge import bridge_cli
from mailforge.bridge_cli import BridgeInputError, _daily_brief, _message_json
from mailforge.bridge_ops import triage_ops
from mailforge.config import Settings
from mailforge.db.store import open_store, register_sites

H = triage_ops.HANDLERS


@pytest.fixture(autouse=True)
def _default_registry():
    register_sites(["main", "shop"])
    yield
    register_sites(["main", "shop"])


def _settings(per_day: int = 50) -> Settings:
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


def _inbound(store, *, site="main", uid=1, status=None, quarantined=0) -> int:
    name, user = ("sales", "sales@example.test") if site == "main" else ("shop", "orders@shop.test")
    account = store.upsert_account(name, "imap", "imap.example", 993, user, site)
    mid = store.insert_message(
        account_id=account, folder="INBOX", uid=uid, message_id=f"<m{uid}@example.test>",
        thread_id=f"t{uid}", from_addr="customer@example.test", from_name="Customer",
        to_addrs=user, cc_addrs="", subject=f"Subject {uid}",
        received_at="2026-11-02T00:00:00+00:00", raw_html=b"",
        sanitized_text="Hello there", has_attachments=0, link_count=0,
        quarantined=quarantined,
    )
    if status:
        store.conn.execute("UPDATE messages SET screening_status=? WHERE id=?", (status, mid))
        store.conn.commit()
    return mid


def _audit_rows(store, tool):
    return store.conn.execute(
        "SELECT actor, event FROM audit_log WHERE json_extract(detail_json,'$.tool')=?", (tool,)
    ).fetchall()


# ----- mark-read / archive -----

def test_mark_read_changes_state_audits_and_binds_site(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        mid = _inbound(store)
        other = _inbound(store, site="shop", uid=2)
        out = H["mark-read"]({"ids": [mid, other], "seen": True, "author": "ds_flash"},
                             "main", store, _settings())
        assert out["changed"] == [mid]
        assert [r["id"] for r in out["refused"]] == [other]
        assert "not found" in out["refused"][0]["reason"]
        assert store.get_message(mid)["seen"] == 1
        assert store.get_message(other)["seen"] == 0
        rows = _audit_rows(store, "mark_read")
        assert [tuple(r) for r in rows] == [("agent", "tool_call")]
        H["mark-read"]({"ids": [mid], "seen": False}, "main", store, _settings())
        assert store.get_message(mid)["seen"] == 0


def test_archive_and_unarchive_and_refuses_trashed(tmp_path):
    with open_store(tmp_path / "b.db") as store:
        mid = _inbound(store)
        t = _inbound(store, uid=2)
        store.set_messages_trashed([t], True)
        out = H["archive"]({"ids": [mid, t]}, "main", store, _settings())
        assert out["changed"] == [mid]
        assert "Trash" in out["refused"][0]["reason"]
        assert store.get_message(mid)["archived"] == 1
        assert store.get_message(t)["trashed"] == 1  # archive must not silently restore it
        H["archive"]({"ids": [mid], "archived": False}, "main", store, _settings())
        assert store.get_message(mid)["archived"] == 0


@pytest.mark.parametrize("op", ["mark-read", "archive", "trash", "flag"])
def test_gate_r_refuses_quarantined_and_withheld_mail(tmp_path, op):
    with open_store(tmp_path / f"c{op}.db") as store:
        q = _inbound(store, uid=1, quarantined=1)
        w = _inbound(store, uid=2, status="POTENTIAL_ISSUE")
        data = {"ids": [q, w], "label": "POTENTIAL_SPAM"}
        if op == "flag":
            data = {"id": q, "label": "POTENTIAL_SPAM"}
            with pytest.raises(BridgeInputError, match="quarantined"):
                H[op](data, "main", store, _settings())
            data = {"id": w, "label": "POTENTIAL_SPAM"}
            with pytest.raises(BridgeInputError, match="withheld"):
                H[op](data, "main", store, _settings())
            return
        out = H[op](data, "main", store, _settings())
        assert out["changed"] == []
        assert {r["id"] for r in out["refused"]} == {q, w}
        assert store.get_message(q)["trashed"] == 0 and store.get_message(w)["archived"] == 0


def test_bulk_limit_and_input_validation(tmp_path):
    with open_store(tmp_path / "d.db") as store:
        with pytest.raises(BridgeInputError, match="at most 50"):
            H["archive"]({"ids": list(range(1, 52))}, "main", store, _settings())
        with pytest.raises(BridgeInputError, match="ids"):
            H["archive"]({"ids": []}, "main", store, _settings())
        with pytest.raises(BridgeInputError, match="ids"):
            H["archive"]({"ids": ["x"]}, "main", store, _settings())


def test_triage_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(triage_ops, "TRIAGE_PER_DAY", 3)
    with open_store(tmp_path / "e.db") as store:
        ids = [_inbound(store, uid=i) for i in range(1, 6)]
        H["mark-read"]({"ids": ids[:2]}, "main", store, _settings())
        with pytest.raises(BridgeInputError, match="cap"):
            H["archive"]({"ids": ids[2:4]}, "main", store, _settings())  # 2 + 2 > 3
        H["archive"]({"ids": ids[2:3]}, "main", store, _settings())     # 2 + 1 == 3
        with pytest.raises(BridgeInputError, match="cap"):
            H["mark-read"]({"ids": ids[4:5]}, "main", store, _settings())


# ----- trash -----

def test_trash_and_restore_round_trip(tmp_path):
    with open_store(tmp_path / "f.db") as store:
        mid = _inbound(store)
        out = H["trash"]({"ids": [mid]}, "main", store, _settings())
        assert out["changed"] == [mid]
        assert store.get_message(mid)["trashed"] == 1
        out = H["trash"]({"ids": [mid], "trashed": False}, "main", store, _settings())
        assert out["changed"] == [mid]
        assert store.get_message(mid)["trashed"] == 0


@pytest.mark.parametrize("status", ["SPAM", "POTENTIAL_SPAM", "POTENTIAL_ISSUE"])
def test_trash_refused_for_purge_immediately_statuses(tmp_path, status):
    with open_store(tmp_path / f"g{status}.db") as store:
        mid = _inbound(store, status=status)
        out = H["trash"]({"ids": [mid]}, "main", store, _settings())
        assert out["changed"] == []
        assert "human" in out["refused"][0]["reason"]
        assert store.get_message(mid)["trashed"] == 0


def test_restore_is_allowed_for_withheld_mail_but_not_for_purged(tmp_path):
    with open_store(tmp_path / "h.db") as store:
        w = _inbound(store, status="POTENTIAL_SPAM")
        store.set_messages_trashed([w], True)  # a human trashed it
        out = H["trash"]({"ids": [w], "trashed": False}, "main", store, _settings())
        assert out["changed"] == [w]
        p = _inbound(store, uid=2)
        store.set_messages_trashed([p], True)
        store.purge_messages([p])
        out = H["trash"]({"ids": [p], "trashed": False}, "main", store, _settings())
        assert out["changed"] == [] and "purged" in out["refused"][0]["reason"]


# ----- flag -----

def test_flag_marks_for_review_without_learning_and_audits_agent(tmp_path):
    with open_store(tmp_path / "i.db") as store:
        mid = _inbound(store)
        out = H["flag"]({"id": mid, "label": "POTENTIAL_SPAM", "note": "looks like a lure",
                         "author": "ds_flash"}, "main", store, _settings())
        assert out["screening_status"] == "POTENTIAL_SPAM"
        assert store.get_message(mid)["screening_status"] == "POTENTIAL_SPAM"
        fb = store.conn.execute("SELECT * FROM message_screening_feedback").fetchall()
        assert len(fb) == 1
        assert fb[0]["actor"] == "agent:ds_flash" and fb[0]["learn_similar"] == 0
        assert _audit_rows(store, "flag")


def test_flag_rejects_bad_labels_trashed_and_foreign_mail(tmp_path):
    with open_store(tmp_path / "j.db") as store:
        mid = _inbound(store)
        for label in ("SPAM", "CONTENT", "nonsense", ""):
            with pytest.raises(BridgeInputError, match="label"):
                H["flag"]({"id": mid, "label": label}, "main", store, _settings())
        store.set_messages_trashed([mid], True)
        with pytest.raises(BridgeInputError, match="Trash"):
            H["flag"]({"id": mid, "label": "POTENTIAL_ISSUE"}, "main", store, _settings())
        other = _inbound(store, site="shop", uid=2)
        with pytest.raises(BridgeInputError, match="not found"):
            H["flag"]({"id": other, "label": "POTENTIAL_ISSUE"}, "main", store, _settings())


def test_flag_then_trash_combination_is_impossible(tmp_path):
    with open_store(tmp_path / "k.db") as store:
        mid = _inbound(store)
        H["flag"]({"id": mid, "label": "POTENTIAL_ISSUE"}, "main", store, _settings())
        out = H["trash"]({"ids": [mid]}, "main", store, _settings())
        assert out["changed"] == []


# ----- withdraw-draft -----

def test_withdraw_only_agent_written_open_drafts(tmp_path):
    with open_store(tmp_path / "l.db") as store:
        mid = _inbound(store)
        mine = store.create_draft(mid, "t1", "customer@example.test", "Re: S", "body", "PENDING",
                                  {"agent_written": True, "author": "ds_flash"})
        theirs = store.create_manual_draft("sales@example.test", "x@example.test", "Hi", "Body")
        store.create_send_authorization(mine, "tok", "sha", "customer@example.test",
                                        "sales@example.test", "agent")
        with pytest.raises(BridgeInputError, match="agent"):
            H["withdraw-draft"]({"draft_id": theirs}, "main", store, _settings())
        out = H["withdraw-draft"]({"draft_id": mine, "author": "ds_flash"}, "main", store, _settings())
        assert out["state"] == "REJECTED"
        d = store.get_draft(mine)
        flags = json.loads(d["guardrail_flags"])
        assert d["state"] == "REJECTED" and flags["withdrawn_by"] == "ds_flash"
        assert flags["agent_written"] is True
        assert store.consume_send_authorization(mine, "tok", "sha") is None
        assert store.get_draft(theirs)["state"] == "DRAFT"
        with pytest.raises(BridgeInputError, match="cannot be withdrawn"):
            H["withdraw-draft"]({"draft_id": mine}, "main", store, _settings())


def test_withdraw_is_site_bound(tmp_path):
    with open_store(tmp_path / "m.db") as store:
        mid = _inbound(store, site="shop")
        d = store.create_draft(mid, "t1", "c@example.test", "Re: S", "b", "PENDING",
                               {"agent_written": True})
        with pytest.raises(BridgeInputError, match="not found"):
            H["withdraw-draft"]({"draft_id": d}, "main", store, _settings())


# ----- reads -----

def test_attachments_lists_metadata_only_and_applies_gate_r(tmp_path):
    with open_store(tmp_path / "n.db") as store:
        mid = _inbound(store)
        store.add_attachment(mid, "invoice.pdf", "application/pdf", 1234, "/secret/path/x.pdf",
                             sha256="abc")
        out = H["attachments"]({"message_id": mid}, "main", store, _settings())
        assert out["attachments"] == [{"id": out["attachments"][0]["id"],
                                       "filename": "invoice.pdf", "mime_type": "application/pdf",
                                       "size_bytes": 1234, "skipped": ""}]
        assert "/secret" not in json.dumps(out)
        q = _inbound(store, uid=2, quarantined=1)
        with pytest.raises(BridgeInputError, match="quarantined"):
            H["attachments"]({"message_id": q}, "main", store, _settings())
        other = _inbound(store, site="shop", uid=3)
        with pytest.raises(BridgeInputError, match="not found"):
            H["attachments"]({"message_id": other}, "main", store, _settings())


def test_outbox_staged_and_sent_never_leak_tokens_and_are_site_bound(tmp_path):
    with open_store(tmp_path / "o.db") as store:
        mid = _inbound(store)
        did = store.create_draft(mid, "t1", "customer@example.test", "Re: S", "the body", "PENDING", {})
        store.create_send_authorization(did, "SECRETHASH", "sha", "customer@example.test",
                                        "sales@example.test", "agent")
        rid = store.begin_send_record("sales@example.test", "customer@example.test", "Hello",
                                      "sent body text", origin="ui_compose", site_id="main")
        store.finish_send_record(rid, "SENT")
        other = store.begin_send_record("orders@shop.test", "y@example.test", "Other", "b",
                                        origin="ui_compose", site_id="shop")
        staged = H["outbox"]({"kind": "staged"}, "main", store, _settings())
        assert len(staged["staged"]) == 1 and staged["staged"][0]["status"] == "STAGED"
        assert "SECRETHASH" not in json.dumps(staged) and "the body" not in json.dumps(staged)
        sent = H["outbox"]({"kind": "sent"}, "main", store, _settings())
        assert [r["id"] for r in sent["sent"]] == [rid]
        assert "sent body text" not in json.dumps(sent)  # metadata only
        one = H["sent"]({"id": rid}, "main", store, _settings())
        assert "sent body text" in one["body"] and "UNTRUSTED" in one["body"]
        with pytest.raises(BridgeInputError, match="not found"):
            H["sent"]({"id": other}, "main", store, _settings())
        with pytest.raises(BridgeInputError, match="kind"):
            H["outbox"]({"kind": "weird"}, "main", store, _settings())


def test_trash_queue_shows_due_dates_for_this_site_only(tmp_path):
    with open_store(tmp_path / "p.db") as store:
        mid = _inbound(store)
        other = _inbound(store, site="shop", uid=2)
        store.set_messages_trashed([mid, other], True)
        out = H["trash-queue"]({}, "main", store, _settings())
        assert [r["id"] for r in out["queue"]] == [mid]
        assert out["queue"][0]["due_at"]
        assert out["retention_days"] == 60


def test_config_summary_is_read_only_and_secret_free(tmp_path):
    with open_store(tmp_path / "q.db") as store:
        out = H["config-summary"]({}, "main", store, _settings())
        assert out["site_id"] == "main"
        assert out["mailboxes"] == ["sales"]
        assert out["retention"]["trash_retention_days"] == 60
        assert out["caps"]["triage_per_day"] == triage_ops.TRIAGE_PER_DAY
        assert any(r["category"] == "GITHUB" for r in out["category_rules"])
        text = json.dumps(out)
        assert "sales@example.test" not in text  # addresses are not needed here


def test_activity_is_site_bound_and_includes_notes(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        mine = _inbound(store)
        theirs = _inbound(store, site="shop", uid=2)
        H["mark-read"]({"ids": [mine]}, "main", store, _settings())
        store.add_agent_note("main", "ds_flash", "a note", "body")
        out = H["activity"]({"limit": 50}, "main", store, _settings())
        assert any(r["detail"].get("tool") == "mark_read" for r in out["audit"])
        assert [n["title"] for n in out["notes"]] == ["a note"]
        from mailforge.audit.log import AuditLog
        AuditLog(store).record(actor="agent", event="tool_call", subject_table="messages",
                               subject_id=theirs, detail={"tool": "archive"})
        out = H["activity"]({"limit": 50}, "main", store, _settings())
        assert all(r.get("subject_id") != theirs for r in out["audit"])
        with pytest.raises(BridgeInputError, match="between"):
            H["activity"]({"limit": 101}, "main", store, _settings())


# ----- extended existing ops -----

def test_list_views_and_state_flags(tmp_path):
    with open_store(tmp_path / "s.db") as store:
        inbox = _inbound(store, uid=1)
        arch = _inbound(store, uid=2)
        trash = _inbound(store, uid=3)
        flagged = _inbound(store, uid=4, status="POTENTIAL_SPAM")
        store.set_messages_archived([arch], True)
        store.set_messages_trashed([trash], True)

        def ids(view):
            out = bridge_cli._list_messages({"view": view} if view else {}, "main", store, _settings())
            return {m["id"] for m in out["messages"]}, out

        assert ids(None)[0] == {inbox, flagged}
        assert ids("archived")[0] == {arch}
        assert ids("trash")[0] == {trash}
        assert ids("questionable")[0] == {flagged}
        _, out = ids("trash")
        assert out["messages"][0]["trashed"] is True and out["messages"][0]["archived"] is False
        with pytest.raises(BridgeInputError, match="view"):
            bridge_cli._list_messages({"view": "bogus"}, "main", store, _settings())


def test_get_reports_purged(tmp_path):
    with open_store(tmp_path / "t.db") as store:
        mid = _inbound(store)
        store.set_messages_trashed([mid], True)
        store.purge_messages([mid])
        row = store.get_message_for_site(mid, "main")
        out = _message_json(row, full=True)
        assert out["purged"] is True
        assert "Hello there" not in json.dumps(out)


def test_daily_brief_has_guidance_overrides_and_requests(tmp_path):
    with open_store(tmp_path / "u.db") as store:
        store.add_agent_note("main", "ds_flash", "please unquarantine", "see UI", kind="request")
        from mailforge.audit.log import AuditLog
        log = AuditLog(store)
        log.record(actor="agent", event="tool_call", subject_table="guidance",
                   detail={"tool": "guidance_write", "kind": "policy", "site_id": "main",
                           "op": "save", "key": "policy", "author": "ds_flash"})
        log.record(actor="agent", event="tool_call", subject_table="guidance",
                   detail={"tool": "guidance_write", "kind": "policy", "site_id": "shop"})
        log.record(actor="guardrail", event="block",
                   detail={"tool": "guidance_write", "site_id": "main"})
        log.record(actor="user", event="approval", subject_table="drafts", subject_id=1,
                   detail={"action": "guardrail_override"})
        brief = _daily_brief(store, "main")
        assert brief["guidance_changes_24h"] == 1  # this site only, refusals apart
        assert brief["guidance_refused_24h"] == 1
        assert [c["key"] for c in brief["guidance_changes"]] == ["policy"]
        assert brief["overrides_24h"] == 1
        assert [n["title"] for n in brief["open_requests"]] == ["please unquarantine"]


def test_agent_notes_migration_adds_request_kind_idempotently(tmp_path):
    path = tmp_path / "v.db"
    with open_store(path) as store:
        store.add_agent_note("main", "a", "t", "b")
        # simulate an old database whose CHECK lacks 'request'
        store.conn.execute("ALTER TABLE agent_notes RENAME TO n_old")
        store.conn.execute(
            "CREATE TABLE agent_notes (id INTEGER PRIMARY KEY, site_id TEXT NOT NULL, "
            "author TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'observation' "
            "CHECK (kind IN ('observation','action','incident','context')), title TEXT NOT NULL, "
            "body TEXT NOT NULL, related_message_ids TEXT, created_at TEXT NOT NULL)")
        store.conn.execute("INSERT INTO agent_notes SELECT * FROM n_old")
        store.conn.execute("DROP TABLE n_old")
        store.conn.commit()
    for i in range(2):
        with open_store(path) as store:
            assert len(store.list_agent_notes("main")) == 1 + i
            store.add_agent_note("main", "a", "req", "b", kind="request")
    with open_store(path) as store:
        assert len(store.list_agent_notes("main")) == 3
