"""Quarantine containment, agent notes, attachments, triage, site registry."""

from __future__ import annotations

import asyncio
import json

import pytest

from siftforge.config import Settings
from siftforge.db import store as store_mod
from siftforge.db.store import open_store, register_sites, validate_site_id


@pytest.fixture(autouse=True)
def _default_registry():
    """Each test starts from the stock two-site registry."""
    register_sites(["main", "shop"])
    yield
    register_sites(["main", "shop"])


def _message(
    store,
    *,
    site_id="main",
    uid=1,
    quarantined=0,
    reason=None,
    from_addr="sender@example.test",
    subject=None,
    text="Please respond.",
) -> int:
    account = store.upsert_account(
        f"{site_id}-{uid}", "imap", "imap.example", 993,
        f"inbox-{uid}@example.test", site_id,
    )
    message_id = store.insert_message(
        account_id=account,
        folder="INBOX",
        uid=uid,
        message_id=f"<{site_id}-{uid}@example.test>",
        thread_id=f"{site_id}-{uid}",
        from_addr=from_addr,
        from_name="Sender",
        to_addrs="inbox@example.test",
        cc_addrs="",
        subject=subject or f"Question {uid}",
        received_at=f"2026-07-16T00:00:{uid:02d}+00:00",
        raw_html=b"",
        sanitized_text=text,
        has_attachments=0,
        link_count=0,
        quarantined=quarantined,
        quarantine_reason=reason,
    )
    assert message_id is not None
    return message_id


# --------------------------------------------------------------------------- #
# quarantine
# --------------------------------------------------------------------------- #
def test_quarantine_flag_roundtrip_and_counts(tmp_path):
    with open_store(tmp_path / "q.db") as store:
        mid = _message(store, uid=1, quarantined=1, reason="ingest injection score 0.99")
        assert store.is_quarantined(mid)
        assert store.quarantined_count() == 1
        assert store.quarantined_count("main") == 1
        assert store.quarantined_count("shop") == 0
        rows = store.list_received(quarantined_only=True)
        assert [r["id"] for r in rows] == [mid]
        assert rows[0]["quarantine_reason"] == "ingest injection score 0.99"
        assert store.set_message_quarantined(mid, False, None)
        assert not store.is_quarantined(mid)
        assert store.quarantined_count() == 0


def test_graph_refuses_quarantined_message(tmp_path):
    from siftforge.agent.graph import AgentGraph

    with open_store(tmp_path / "g.db") as store:
        mid = _message(store, uid=2, quarantined=1, reason="test")

        class _Bridge:
            def is_up(self):  # pragma: no cover - must never be reached
                raise AssertionError("quarantined message reached the LLM bridge")

        graph = AgentGraph(store, Settings(), _Bridge())
        result = asyncio.run(graph.process_message(mid))
        assert result is None
        # No classification and no draft may exist for a quarantined message.
        assert store.conn.execute(
            "SELECT COUNT(*) FROM classifications WHERE message_id=?", (mid,)
        ).fetchone()[0] == 0
        assert store.latest_draft_for_message(mid) is None
        # The refusal is audited.
        events = store.conn.execute(
            "SELECT event, detail_json FROM audit_log WHERE subject_id=?", (mid,)
        ).fetchall()
        assert any(e["event"] == "block" for e in events)


def test_bridge_json_withholds_quarantined_bodies(tmp_path):
    from siftforge.bridge_cli import _message_json

    with open_store(tmp_path / "b.db") as store:
        mid = _message(store, uid=3, quarantined=1, reason="test")
        row = store.get_message_for_site(mid, "main")
        payload = _message_json(row, full=True)
        assert payload["quarantined"] is True
        assert "sanitized_text" not in payload
        assert "snippet" not in payload
        assert "body_withheld" in payload
        listing = _message_json(row)
        assert "snippet" not in listing


def test_bridge_json_marks_untrusted_content(tmp_path):
    from siftforge.bridge_cli import UNTRUSTED_CLOSE, UNTRUSTED_OPEN, _message_json

    with open_store(tmp_path / "u.db") as store:
        mid = _message(store, uid=4)
        row = store.get_message_for_site(mid, "main")
        payload = _message_json(row, full=True)
        assert payload["sanitized_text"].startswith(UNTRUSTED_OPEN)
        assert payload["sanitized_text"].rstrip().endswith(UNTRUSTED_CLOSE)
        assert "content_policy" in payload


def test_revise_refuses_quarantined_source(tmp_path):
    from siftforge.agent.revisions import revise_draft

    with open_store(tmp_path / "r.db") as store:
        mid = _message(store, uid=5, quarantined=1)
        did = store.create_draft(
            mid, "main-5", "sender@example.test", "Re: Question 5",
            "draft body", "PENDING", {},
        )
        with pytest.raises(ValueError, match="quarantined"):
            asyncio.run(
                revise_draft(store, Settings(), object(), did, "shorter", site_id="main")
            )


# --------------------------------------------------------------------------- #
# agent notes
# --------------------------------------------------------------------------- #
def test_agent_notes_roundtrip_validation_and_scoping(tmp_path):
    with open_store(tmp_path / "n.db") as store:
        nid = store.add_agent_note(
            "main", "email-main", "GitHub burst self-caused",
            "Verification emails were our own OAuth logins.",
            kind="incident", related_message_ids=[1, 2, 3],
        )
        assert nid == 1
        notes = store.list_agent_notes("main")
        assert len(notes) == 1
        assert notes[0]["kind"] == "incident"
        assert json.loads(notes[0]["related_message_ids"]) == [1, 2, 3]
        assert store.list_agent_notes("shop") == []
        with pytest.raises(ValueError):
            store.add_agent_note("main", "x", "", "no title", kind="observation")
        with pytest.raises(ValueError):
            store.add_agent_note("main", "x", "t", "b", kind="bogus")
        with pytest.raises(ValueError):
            store.add_agent_note("not-a-site", "x", "t", "b")


def test_weekly_summary_includes_quarantine_count(tmp_path):
    with open_store(tmp_path / "w.db") as store:
        _message(store, uid=6, quarantined=1)
        summary = store.weekly_summary("main", 7)
        assert summary["quarantined"] == 1


# --------------------------------------------------------------------------- #
# cynical content-only screening + human alignment
# --------------------------------------------------------------------------- #
def test_content_only_site_screens_before_any_model_sees_the_body():
    from siftforge.config import SiteConfig
    from siftforge.security.inbound_screening import assess_inbound

    # content_only is per-site configuration, not a built-in for any brand.
    settings = Settings(
        sites={
            "shop": SiteConfig(
                name="Acme Workshop",
                screening_mode="content_only",
                triage_guidance=(
                    "Treat login, account, password, payment, or legal claims as "
                    "issues to verify by opening the provider's site directly."
                ),
            )
        }
    )
    assert settings.site_screening_mode("shop") == "content_only"
    assert "provider's site directly" in settings.site_triage_guidance("shop")

    content = assess_inbound(
        mode="content_only",
        from_addr="reader@example.com",
        subject="Question about your published guide",
        text="I read your guide and have a material question.",
    )
    assert content.status == "CONTENT"

    unknown = assess_inbound(
        mode="content_only",
        from_addr="stranger@example.net",
        subject="Hello",
        text="Can we talk?",
        link_count=1,
    )
    assert unknown.status == "POTENTIAL_SPAM"

    issue = assess_inbound(
        mode="content_only",
        from_addr="alerts@example.net",
        subject="Verify your account login",
        text="Open the link to reset your password.",
        link_count=1,
        learned_examples=[{
            "label": "CONTENT",
            "sender_addr": "alerts@example.net",
            "subject_signature": "",
        }],
    )
    assert issue.status == "POTENTIAL_ISSUE"
    assert "opening the provider site directly" in issue.reason


def test_spam_feedback_filters_and_learns_without_deleting(tmp_path):
    from siftforge.security.inbound_screening import assess_inbound

    with open_store(tmp_path / "screening.db") as store:
        mid = _message(
            store,
            site_id="shop",
            uid=20,
            from_addr="pitch@rank-fast.example",
            subject="SEO audit offer 7291",
            text="We can improve your rankings.",
        )
        store.set_message_screening(
            mid, "POTENTIAL_SPAM", "not clearly content-related", "AUTO_POLICY"
        )
        did = store.create_draft(
            mid, "shop-20", "pitch@rank-fast.example", "Re: SEO audit",
            "No thanks", "PENDING", {},
        )
        feedback_id = store.record_screening_feedback(
            mid, "SPAM", learn_similar=True, note="Unsolicited SEO pitch"
        )

        assert feedback_id > 0
        assert store.get_message(mid)["screening_status"] == "SPAM"
        assert store.get_draft(did)["state"] == "REJECTED"
        assert store.received_counts("shop") == {"total": 0, "unseen": 0}
        assert store.received_counts("shop", include_spam=True)["total"] == 1
        assert [r["id"] for r in store.list_received(spam_only=True)] == [mid]
        assert store.list_received(questionable_only=True) == []
        assert store.triage_counts("shop")["spam"] == 1

        learned = assess_inbound(
            mode="content_only",
            from_addr="other@another.example",
            subject="SEO audit offer 9922",
            text="A different body.",
            learned_examples=store.screening_examples("shop"),
        )
        assert learned.status == "SPAM"
        assert learned.source == "LEARNED"


def test_questionable_screening_withholds_body_and_all_ai_work(tmp_path):
    from siftforge.agent.graph import AgentGraph
    from siftforge.bridge_cli import _message_json

    with open_store(tmp_path / "withheld.db") as store:
        mid = _message(store, site_id="shop", uid=21)
        store.set_message_screening(
            mid,
            "POTENTIAL_ISSUE",
            "login claim; verify by opening the provider site directly",
            "AUTO_POLICY",
        )

        class _Bridge:
            def is_up(self):  # pragma: no cover - must never be reached
                raise AssertionError("screened message reached the LLM bridge")

        result = asyncio.run(AgentGraph(store, Settings(), _Bridge()).process_message(mid))
        assert result is None
        assert mid not in store.unprocessed_message_ids()
        payload = _message_json(store.get_message_for_site(mid, "shop"), full=True)
        assert payload["screening_status"] == "POTENTIAL_ISSUE"
        assert "sanitized_text" not in payload
        assert "body_withheld" in payload
        assert [r["id"] for r in store.list_received(questionable_only=True)] == [mid]
        assert store.triage_counts("shop")["questionable"] == 1


def test_inbound_markdown_can_strip_every_clickable_link():
    from siftforge.mail.markdown import markdown_to_safe_html

    rendered = markdown_to_safe_html(
        "[Sign in](https://evil.example/login) or visit https://evil.example/reset",
        allow_links=False,
    )
    assert "<a" not in rendered
    assert "href=" not in rendered


# --------------------------------------------------------------------------- #
# attachments
# --------------------------------------------------------------------------- #
def test_attachment_rows_roundtrip(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        mid = _message(store, uid=7)
        store.add_attachment(mid, "quote.pdf", "application/pdf", 1234, "/x/quote.pdf",
                             sha256="ff")
        store.add_attachment(mid, "huge.iso", "application/octet-stream", 10**9, None,
                             skipped_reason="over size cap")
        rows = store.list_attachments(mid)
        assert [r["filename"] for r in rows] == ["quote.pdf", "huge.iso"]
        assert rows[0]["stored_path"] == "/x/quote.pdf"
        assert rows[1]["stored_path"] is None and rows[1]["skipped_reason"]
        att = store.get_attachment(rows[0]["id"])
        assert att["sha256"] == "ff"


def test_safe_attachment_name_blocks_traversal():
    from siftforge.mail.imap_listener import _safe_attachment_name

    assert _safe_attachment_name("../../etc/passwd") == "passwd"
    assert _safe_attachment_name("..\\..\\win\\cmd.exe") == "cmd.exe"
    assert _safe_attachment_name("") == "attachment"
    assert "/" not in _safe_attachment_name("a/b/c<script>.pdf")


# --------------------------------------------------------------------------- #
# triage + subjects
# --------------------------------------------------------------------------- #
def test_triage_counts_and_filters(tmp_path):
    with open_store(tmp_path / "t.db") as store:
        respond = _message(store, uid=8)
        store.upsert_classification(
            respond, category="RESPOND", priority=2, rationale="",
            injection_risk=0.0, model_used="test",
        )
        fyi = _message(store, uid=9)
        store.upsert_classification(
            fyi, category="FYI", priority=0, rationale="",
            injection_risk=0.0, model_used="test",
        )
        _message(store, uid=10, quarantined=1)
        counts = store.triage_counts()
        assert counts["needs_reply"] == 1
        assert counts["needs_action"] == 2  # RESPOND + quarantined
        assert counts["quarantined"] == 1
        assert [r["id"] for r in store.list_received(needs_reply_only=True)] == [respond]
        action_ids = {r["id"] for r in store.list_received(needs_action_only=True)}
        assert respond in action_ids and fyi not in action_ids


def test_clean_subject_unfolds_header_linebreaks():
    from siftforge.ui.theme import clean_subject

    folded = "[GitHub] A third-party OAuth application has been added to your\r\n account"
    assert clean_subject(folded) == (
        "[GitHub] A third-party OAuth application has been added to your account"
    )
    assert clean_subject(None) == "(no subject)"
    assert clean_subject("  ") == "(no subject)"


# --------------------------------------------------------------------------- #
# site registry
# --------------------------------------------------------------------------- #
def test_site_registry_validation_and_rebuild(tmp_path):
    with open_store(tmp_path / "s.db") as store:
        with pytest.raises(ValueError):
            validate_site_id("newbrand")
        register_sites(["main", "shop", "newbrand"])
        assert validate_site_id("newbrand") == "newbrand"
        store.init_schema()  # triggers _ensure_site_capacity when needed
        account = store.upsert_account(
            "nb", "imap", "imap.example", 993, "x@newbrand.test", "newbrand"
        )
        assert account > 0
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        nid = store.add_agent_note("newbrand", "test", "t", "b")
        assert store.list_agent_notes("newbrand")[0]["id"] == nid


def test_settings_reject_account_with_unknown_site():
    settings = Settings(
        imap_accounts=[{
            "name": "x", "site_id": "ghost", "host": "h", "username": "u",
        }]
    )
    with pytest.raises(ValueError, match="unknown site"):
        settings.assert_invariants()


def test_smtp_for_account_prefers_override():
    settings = Settings(
        imap_accounts=[
            {"name": "a", "site_id": "main", "host": "h", "username": "a@x.test"},
            {"name": "b", "site_id": "main", "host": "h", "username": "b@y.test",
             "smtp_host": "smtp.y.test", "smtp_port": 465, "smtp_starttls": False},
        ],
        smtp={"host": "smtp.global.test", "port": 587, "username": "a@x.test"},
    )
    global_cfg = settings.smtp_for_account(settings.imap_accounts[0])
    assert global_cfg.host == "smtp.global.test"
    assert global_cfg.username == "a@x.test"
    override = settings.smtp_for_account(settings.imap_accounts[1])
    assert override.host == "smtp.y.test"
    assert override.port == 465
    assert override.starttls is False
    assert override.username == "b@y.test"


def test_store_module_registry_is_isolated(tmp_path):
    # register_sites replaces the module-level frozenset; ensure validate uses it.
    register_sites(["solo"])
    assert validate_site_id("solo") == "solo"
    with pytest.raises(ValueError):
        validate_site_id("main")
    assert store_mod.VALID_SITES == frozenset({"solo"})
