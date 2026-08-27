"""Outbox / Sent: the durable send log, its states, and the page that shows them.

The property under test throughout is that no outgoing message can be in an
ambiguous state. Every row resolves to exactly one of STAGED / EXPIRED /
UNKNOWN / IN_FLIGHT / SENT / FAILED, and neither of the two "we do not know"
states is ever rendered as sent.
"""

from __future__ import annotations

import hashlib

import pytest

from mailforge.bridge_cli import BridgeInputError, _do_agent_send, _prepare_send
from mailforge.config import Settings
from mailforge.db.store import open_store, register_sites
from mailforge.ui.pages.outbox import collect_rows, status_of


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


def _seed_reply_draft(store) -> int:
    account = store.upsert_account(
        "sales", "imap", "imap.example", 993, "sales@example.test", "main"
    )
    mid = store.insert_message(
        account_id=account, folder="INBOX", uid=1, message_id="<m1@example.test>",
        thread_id="t1", from_addr="customer@example.test", from_name="Customer",
        to_addrs="sales@example.test", cc_addrs="", subject="Question",
        received_at="2026-07-16T00:00:00+00:00", raw_html=b"",
        sanitized_text="Hello", has_attachments=0, link_count=0,
    )
    return store.create_draft(
        mid, "t1", "customer@example.test", "Re: Question",
        "Thanks for reaching out — happy to help.", "PENDING", {},
    )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def test_send_record_lifecycle(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        rid = store.begin_send_record(
            "sales@example.test", "customer@example.test", "Hi", "Body",
            origin="ui_compose", site_id="main",
        )
        row = store.get_sent_message(rid)
        # An open record is UNKNOWN on purpose: if the process dies here the
        # UI must say "we do not know", not "sent".
        assert row["outcome"] == "UNKNOWN" and row["imap_append"] == "PENDING"
        assert status_of(row, "sent")[0] == "IN_FLIGHT"

        store.finish_send_record(rid, "SENT", smtp_message_id="<x@example.test>")
        store.record_send_append(rid, "OK", folder="Sent")
        row = store.get_sent_message(rid)
        assert row["outcome"] == "SENT"
        assert row["smtp_message_id"] == "<x@example.test>"
        assert row["completed_at"]
        state, label, kind = status_of(row, "sent")
        assert (state, kind) == ("SENT", "success") and label.startswith("SENT ")


def test_failed_send_is_red_and_keeps_the_reason(tmp_path):
    with open_store(tmp_path / "b.db") as store:
        rid = store.begin_send_record(
            "sales@example.test", "customer@example.test", "Hi", "Body", site_id="main"
        )
        store.finish_send_record(rid, "FAILED", error_text="relay refused: 550")
        row = store.get_sent_message(rid)
        state, label, kind = status_of(row, "sent")
        assert state == "FAILED" and kind == "error"
        assert "550" in label


def test_append_failure_does_not_change_the_send_outcome(tmp_path):
    with open_store(tmp_path / "c.db") as store:
        rid = store.begin_send_record(
            "sales@example.test", "customer@example.test", "Hi", "Body", site_id="main"
        )
        store.finish_send_record(rid, "SENT")
        store.record_send_append(rid, "FAILED", note="login failed")
        row = store.get_sent_message(rid)
        assert row["outcome"] == "SENT" and row["imap_append"] == "FAILED"
        assert status_of(row, "sent")[0] == "SENT"


def test_invalid_outcome_and_origin_are_rejected(tmp_path):
    with open_store(tmp_path / "d.db") as store:
        with pytest.raises(ValueError):
            store.begin_send_record("a@b.test", "c@d.test", "s", "b", origin="nonsense")
        rid = store.begin_send_record("a@b.test", "c@d.test", "s", "b", site_id="main")
        with pytest.raises(ValueError):
            store.finish_send_record(rid, "MAYBE")
        with pytest.raises(ValueError):
            store.record_send_append(rid, "PROBABLY")


def test_deleting_a_draft_keeps_the_sent_evidence(tmp_path):
    with open_store(tmp_path / "e.db") as store:
        did = _seed_reply_draft(store)
        rid = store.begin_send_record(
            "sales@example.test", "customer@example.test", "Re: Question", "Body",
            origin="ui_draft", site_id="main", draft_id=did,
        )
        store.finish_send_record(rid, "SENT")
        store.conn.execute("DELETE FROM drafts WHERE id=?", (did,))
        store.conn.commit()
        row = store.get_sent_message(rid)
        assert row is not None and row["outcome"] == "SENT" and row["draft_id"] is None


# --------------------------------------------------------------------------- #
# Staged authorizations
# --------------------------------------------------------------------------- #
def test_staged_authorization_is_amber_until_used(tmp_path):
    with open_store(tmp_path / "f.db") as store:
        did = _seed_reply_draft(store)
        _prepare_send({"draft_id": did, "author": "email-main"}, "main", store, _settings())
        rows = store.list_staged_sends()
        assert len(rows) == 1
        state, label, kind = status_of(rows[0], "staged")
        assert (state, kind) == ("STAGED", "warning")
        assert "NOT SENT" in label
        assert store.outbox_counts()["pending"] == 1


def test_expired_authorization_is_its_own_state(tmp_path):
    with open_store(tmp_path / "g.db") as store:
        did = _seed_reply_draft(store)
        store.create_send_authorization(
            did, "hash-expired", "digest", "customer@example.test",
            "sales@example.test", "email-main", ttl_seconds=60,
        )
        store.conn.execute(
            "UPDATE send_authorizations SET expires_at='2000-01-01T00:00:00+00:00'"
        )
        store.conn.commit()
        row = store.list_staged_sends()[0]
        assert status_of(row, "staged")[0] == "EXPIRED"
        # An expired authorization is not pending work; it can never send.
        assert store.outbox_counts()["pending"] == 0


def test_consumed_token_without_a_transmission_is_unknown_not_sent(tmp_path):
    """The state the whole page exists for: authorised, then silence."""
    with open_store(tmp_path / "h.db") as store:
        did = _seed_reply_draft(store)
        out = _prepare_send({"draft_id": did}, "main", store, _settings())
        token_hash = hashlib.sha256(out["send_token"].encode()).hexdigest()
        from mailforge.bridge_cli import content_digest

        store.consume_send_authorization(
            did, token_hash, content_digest(out["subject"], out["body"])
        )
        row = store.list_staged_sends()[0]
        state, label, kind = status_of(row, "staged")
        assert state == "UNKNOWN" and kind == "error"
        assert "sent" not in label.lower()
        counts = store.outbox_counts()
        assert counts["unknown"] == 1 and counts["sent"] == 0 and counts["pending"] == 1


def test_recorded_transmission_supersedes_its_authorization(tmp_path):
    with open_store(tmp_path / "i.db") as store:
        did = _seed_reply_draft(store)
        auth_id = store.create_send_authorization(
            did, "hash-used", "digest", "customer@example.test",
            "sales@example.test", "email-main",
        )
        rid = store.begin_send_record(
            "sales@example.test", "customer@example.test", "Re: Question", "Body",
            origin="agent_bridge", site_id="main", draft_id=did,
            authorization_id=auth_id,
        )
        store.finish_send_record(rid, "SENT")
        # The staged row is gone: the transmission row is now the truth.
        assert store.list_staged_sends() == []
        assert store.outbox_counts()["sent"] == 1


# --------------------------------------------------------------------------- #
# The bridge send path writes the log
# --------------------------------------------------------------------------- #
def test_agent_send_records_the_transmission(tmp_path, monkeypatch):
    from mailforge.mail.smtp_sender import SendResult

    def _fake_send_email(**kwargs):
        return SendResult(message_id="<sent-1@example.test>", raw=b"")

    import mailforge.mail.smtp_sender as smtp_sender
    import mailforge.secrets as secret_store

    monkeypatch.setattr(smtp_sender, "send_email", _fake_send_email)

    class _Secret:
        def get_secret_value(self):
            return "pw"

    monkeypatch.setattr(secret_store, "get_smtp_secret", lambda addr: _Secret())

    with open_store(tmp_path / "j.db") as store:
        did = _seed_reply_draft(store)
        settings = _settings()
        out = _prepare_send({"draft_id": did}, "main", store, settings)
        _do_agent_send(
            {"draft_id": did, "send_token": out["send_token"]}, "main", store, settings
        )
        rows = store.list_sent_messages()
        assert len(rows) == 1
        row = rows[0]
        assert row["outcome"] == "SENT"
        assert row["origin"] == "agent_bridge"
        assert row["smtp_message_id"] == "<sent-1@example.test>"
        assert row["to_addrs"] == "customer@example.test"
        assert row["body"].startswith("Thanks for reaching out")
        assert store.list_staged_sends() == []


def test_agent_send_failure_is_recorded_as_failed(tmp_path, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("relay refused")

    import mailforge.mail.smtp_sender as smtp_sender
    import mailforge.secrets as secret_store

    monkeypatch.setattr(smtp_sender, "send_email", _boom)

    class _Secret:
        def get_secret_value(self):
            return "pw"

    monkeypatch.setattr(secret_store, "get_smtp_secret", lambda addr: _Secret())

    with open_store(tmp_path / "k.db") as store:
        did = _seed_reply_draft(store)
        settings = _settings()
        out = _prepare_send({"draft_id": did}, "main", store, settings)
        with pytest.raises(BridgeInputError, match="SMTP send failed"):
            _do_agent_send(
                {"draft_id": did, "send_token": out["send_token"]},
                "main", store, settings,
            )
        row = store.list_sent_messages()[0]
        assert row["outcome"] == "FAILED"
        assert "relay refused" in (row["error_text"] or "")
        assert status_of(row, "sent")[0] == "FAILED"


# --------------------------------------------------------------------------- #
# Page data + rendering
# --------------------------------------------------------------------------- #
def _rendered_text(container) -> str:
    """Every piece of text the page actually puts on screen.

    ``str(container)`` truncates each element's props, which silently turns an
    assertion about page content into an assertion about the first 20
    characters of it.
    """
    parts: list[str] = []
    for element in container.descendants():
        text = getattr(element, "_text", None)
        if isinstance(text, str):
            parts.append(text)
        props = getattr(element, "_props", {}) or {}
        for key in ("innerHTML", "label", "value"):
            value = props.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)



def _seed_both_populations(store) -> None:
    did = _seed_reply_draft(store)
    _prepare_send({"draft_id": did}, "main", store, _settings())
    rid = store.begin_send_record(
        "sales@example.test", "customer@example.test", "Older reply", "Body",
        origin="ui_compose", site_id="main",
    )
    store.finish_send_record(rid, "SENT")
    bad = store.begin_send_record(
        "sales@example.test", "other@example.test", "Bounced", "Body",
        origin="ui_compose", site_id="main",
    )
    store.finish_send_record(bad, "FAILED", error_text="mailbox unavailable")


def test_collect_rows_merges_and_filters(tmp_path):
    with open_store(tmp_path / "l.db") as store:
        _seed_both_populations(store)
        everything = collect_rows(store, "all")
        assert {status_of(r, k)[0] for k, r in everything} == {"STAGED", "SENT", "FAILED"}
        assert [status_of(r, k)[0] for k, r in collect_rows(store, "sent")] == ["SENT"]
        assert [status_of(r, k)[0] for k, r in collect_rows(store, "pending")] == ["STAGED"]
        assert [status_of(r, k)[0] for k, r in collect_rows(store, "problems")] == ["FAILED"]


def test_outbox_page_renders_both_populations(tmp_path):
    from nicegui import ui

    from mailforge.ui.pages import outbox as outbox_page

    with open_store(tmp_path / "m.db") as store:
        _seed_both_populations(store)
        with ui.column() as container:
            outbox_page.render(store, _settings())
        text = _rendered_text(container)
        assert "NOT SENT" in text
        assert "SENT " in text
        assert "FAILED" in text


def test_outbox_page_empty_state(tmp_path):
    from nicegui import ui

    from mailforge.ui.pages import outbox as outbox_page

    with open_store(tmp_path / "n.db") as store:
        with ui.column() as container:
            outbox_page.render(store, _settings())
        assert "Nothing here yet" in _rendered_text(container)


def test_outbox_detail_shows_verbatim_body_and_no_send_control(tmp_path):
    from nicegui import ui

    from mailforge.ui.pages import outbox as outbox_page

    with open_store(tmp_path / "o.db") as store:
        did = _seed_reply_draft(store)
        _prepare_send({"draft_id": did}, "main", store, _settings())
        auth_id = int(store.list_staged_sends()[0]["id"])
        with ui.column() as container:
            outbox_page.render_detail(store, _settings(), "staged", auth_id)
        text = _rendered_text(container)
        assert "Thanks for reaching out" in text          # verbatim staged content
        assert "sales@example.test" in text               # which mailbox it leaves from
        assert f"/detail/{did}" in text or "Open the draft" in text
        # The page never grows its own send control: every button here either
        # navigates back or opens the existing approval screen.
        labels = {
            str((getattr(b, "_props", {}) or {}).get("label", ""))
            for b in container.descendants()
            if type(b).__name__ == "Button"
        }
        assert labels == {"Back", "Open the draft to review and approve"}


def test_demo_seeds_a_staged_and_a_sent_example(tmp_path):
    from mailforge.demo import seed_demo

    with open_store(tmp_path / "p.db") as store:
        seed_demo(store)
        assert any(r["outcome"] == "SENT" for r in store.list_sent_messages())
        staged = store.list_staged_sends()
        assert [status_of(r, "staged")[0] for r in staged] == ["STAGED"]
        # Re-seeding must not duplicate or crash on the UNIQUE token hash.
        seed_demo(store)
        assert len(store.list_staged_sends()) == 1
        assert len(store.list_sent_messages()) == 1


def test_outbox_is_reachable_from_the_main_nav():
    from mailforge.ui import theme

    keys = [entry[0] for entry in theme.NAV]
    assert "outbox" in keys
    route = next(entry[3] for entry in theme.NAV if entry[0] == "outbox")
    assert route == "/outbox"


def test_header_chip_counts_only_what_needs_a_human(tmp_path):
    with open_store(tmp_path / "q.db") as store:
        assert store.outbox_counts()["pending"] == 0
        did = _seed_reply_draft(store)
        _prepare_send({"draft_id": did}, "main", store, _settings())
        rid = store.begin_send_record(
            "sales@example.test", "customer@example.test", "Done", "Body", site_id="main"
        )
        store.finish_send_record(rid, "SENT")
        counts = store.outbox_counts()
        # a completed send is not outstanding work; the staged one is
        assert counts["pending"] == 1 and counts["sent"] == 1
