"""The pre-send checks: what is a warning, what is a hard stop, and the audit."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mailforge.config import IMAPAccount, Settings, SMTPAccount
from mailforge.db.store import open_store
from mailforge.security import GuardrailReport
from mailforge.ui import presend

FAKE_KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"  # AWS's documented dummy key
IP = ".".join(["192", "168", "1", "5"])


def _settings(**security) -> Settings:
    return Settings(
        imap_accounts=[
            IMAPAccount(name="main", host="imap.example", username="inbox@main.example",
                        site_id="main")
        ],
        smtp=SMTPAccount(host="smtp.example", username="inbox@main.example"),
        security=security or {},
    )


def _by_id(checks):
    return {c.id: c for c in checks}


def _eval(store, settings=None, **over):
    kw = dict(
        recipients="bob@example.test",
        subject="Hello",
        body="Thanks for getting in touch. Here is the answer.",
        participants={"bob@example.test"},
        draft_state="DRAFT",
        site_id="main",
    )
    kw.update(over)
    return presend.evaluate(store, settings or _settings(), **kw)


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "p.db") as s:
        yield s


# --- the content guards map to one plain row each --------------------------
def test_clean_message_passes_every_check(store):
    checks = _by_id(_eval(store).checks)
    assert all(c.passed for c in checks.values()), [c for c in checks.values() if not c.passed]
    assert {"secrets", "pii", "content", "links", "other_threads", "rule_a", "rule_b",
            "rule_c"} <= set(checks)


def test_secret_is_a_warning_that_names_the_type_not_the_value(store):
    key = FAKE_KEY
    c = _by_id(_eval(store, body=f"Here you go: {key}").checks)["secrets"]
    assert not c.passed and c.overridable
    assert key not in c.detail and "AWS" in c.detail


def test_pii_is_a_warning_in_plain_words(store):
    c = _by_id(_eval(store, body="My card is 4111 1111 1111 1111 ok").checks)["pii"]
    assert not c.passed and c.overridable
    assert "card number" in c.detail
    assert "4111" not in c.detail


def test_blocked_link_is_a_warning(store):
    c = _by_id(_eval(store, body=f"See http://{IP}/login now").checks)["links"]
    assert not c.passed and c.overridable and IP in c.detail


def test_unknown_recipient_fails_rule_a_but_compose_relaxes_it(store):
    s = _settings(recipient_allowlist_domains={"main.example"})
    reply = _by_id(_eval(store, s, recipients="new@elsewhere.test", participants=set()).checks)
    assert not reply["rule_a"].passed and reply["rule_a"].overridable
    assert not reply["rule_b"].passed
    fresh = _by_id(
        _eval(store, s, recipients="new@elsewhere.test", participants=set(),
              relax_recipient_rules=True).checks
    )
    assert fresh["rule_a"].passed and fresh["rule_b"].passed


def test_guardrails_that_cannot_run_are_a_warning_not_a_silent_pass(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model file missing")

    monkeypatch.setattr(presend, "run_output_guardrails", boom)
    checks = _by_id(_eval(store).checks)
    row = checks["guardrails_ran"]
    assert not row.passed and row.overridable
    assert "couldn't run" in row.detail and "model file missing" in row.detail
    assert "secrets" not in checks  # no pretend ticks for checks that never ran


def test_unclaimed_reason_still_reaches_the_owner(store, monkeypatch):
    monkeypatch.setattr(
        presend, "run_output_guardrails",
        lambda *a, **k: GuardrailReport(False, {}, ["Toxicity flagged (score=0.91)"]),
    )
    c = _by_id(_eval(store).checks)["content"]
    assert not c.passed and "Toxicity" in c.detail


# --- hard gates are never overridable --------------------------------------
def _ok_report(monkeypatch):
    monkeypatch.setattr(
        presend, "run_output_guardrails", lambda *a, **k: GuardrailReport(True, {}, [])
    )


@pytest.mark.parametrize(
    ("over", "check_id"),
    [
        ({"body": "   "}, "body_present"),
        ({"body": "Dear {{name}}, hi"}, "no_placeholders"),
        ({"subject": "About {{topic}}"}, "no_placeholders"),
        ({"recipients": ""}, "recipient_valid"),
        ({"recipients": "not-an-address"}, "recipient_valid"),
        ({"recipients": "ok@example.test, broken"}, "recipient_valid"),
        ({"rate": (False, 5)}, "rate_limit"),
        ({"draft_state": "SENT"}, "draft_sendable"),
        ({"draft_state": "APPROVED"}, "draft_sendable"),
        ({"retype_ok": False}, "retype"),
        ({"sender_ok": False}, "sender_ok"),
    ],
)
def test_hard_gates_cannot_be_overridden(store, monkeypatch, over, check_id):
    _ok_report(monkeypatch)
    row = _by_id(_eval(store, **over).checks)[check_id]
    assert not row.passed and not row.overridable
    assert row.blocking and row in presend.hard_failures(_eval(store, **over).checks)


def test_block_listed_domain_in_a_second_recipient_is_a_hard_gate(store, monkeypatch):
    _ok_report(monkeypatch)
    s = _settings(recipient_block_domains={"spam.test"})
    row = _by_id(_eval(store, s, recipients="a@example.test; B <b@spam.test>").checks)
    assert row["recipient_not_blocked"].blocking
    assert "spam.test" in row["recipient_not_blocked"].detail


def test_invariant_violation_blocks(store, monkeypatch):
    _ok_report(monkeypatch)
    s = _settings()
    s.security.autosend_allowed = True
    assert _by_id(_eval(store, s).checks)["approval_rules"].blocking


def test_subject_missing_is_only_a_warning(store, monkeypatch):
    _ok_report(monkeypatch)
    row = _by_id(_eval(store, subject="", require_subject=True).checks)["subject_present"]
    assert row.warning and not row.blocking


def _source(store, **over):
    account = store.upsert_account("primary", "imap", "imap.example", 993, "inbox@main.example", "main")
    fields = dict(
        account_id=account, folder="INBOX", uid=1, message_id="<1@x>", thread_id="t1",
        from_addr="bob@example.test", from_name="Bob", to_addrs="inbox@main.example",
        cc_addrs="", subject="Q", received_at="2026-11-01T00:00:00+00:00", raw_html=b"",
        sanitized_text="hi", has_attachments=0, link_count=0, header_meta="{}",
        screening_status="CONTENT",
    )
    fields.update(over)
    return store.insert_message(**fields)


def test_trashed_or_purged_source_is_a_hard_gate(store, monkeypatch):
    _ok_report(monkeypatch)
    mid = _source(store)
    base = _by_id(_eval(store, source_message_id=mid).checks)
    assert base["source_not_deleted"].passed and base["source_readable"].passed
    store.set_messages_trashed([mid], True)
    gone = _by_id(_eval(store, source_message_id=mid).checks)["source_not_deleted"]
    assert gone.blocking and "Trash" in gone.detail
    store.set_messages_trashed([mid], False)
    store.conn.execute("UPDATE messages SET purged_at='2026-11-02T00:00:00+00:00' WHERE id=?", (mid,))
    store.conn.commit()
    assert _by_id(_eval(store, source_message_id=mid).checks)["source_not_deleted"].blocking


def test_quarantined_source_is_a_hard_gate(store, monkeypatch):
    _ok_report(monkeypatch)
    mid = _source(store, uid=2, message_id="<2@x>", quarantined=1)
    assert _by_id(_eval(store, source_message_id=mid).checks)["source_readable"].blocking


# --- the override leaves evidence ------------------------------------------
def test_override_writes_an_audit_row_listing_what_was_overridden(store):
    checks = _eval(store, body=f"Here you go: {FAKE_KEY}").checks
    soft = presend.warnings(checks)
    ids = presend.record_override(store, draft_id=7, recipient="bob@example.test", overridden=soft)
    assert "secrets" in ids
    row = store.conn.execute(
        "SELECT actor, event, subject_id, detail_json FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (row["actor"], row["event"], row["subject_id"]) == ("user", "approval", 7)
    import json

    detail = json.loads(row["detail_json"])
    assert detail["action"] == "guardrail_override"
    assert detail["draft_id"] == 7 and "secrets" in detail["overridden"]
    assert FAKE_KEY not in row["detail_json"]


def test_no_override_means_no_audit_row(store):
    assert presend.record_override(store, draft_id=1, recipient="x@y.test", overridden=[]) == []
    assert store.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_override_audit_failure_aborts(store):
    boom = SimpleNamespace(append_audit_chained=lambda **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        presend.record_override(
            boom, draft_id=1, recipient="x@y.test",
            overridden=[presend.Check("pii", "p", False, "d")],
        )


def test_the_agent_send_path_has_no_override_parameter():
    import inspect

    from mailforge import bridge_cli

    for name in ("_prepare_send", "_do_agent_send"):
        fn = getattr(bridge_cli, name, None)
        if fn is not None:
            assert "override" not in inspect.getsource(fn).lower()


def test_signature_changes_when_a_check_changes():
    a = [presend.Check("x", "X", True)]
    b = [presend.Check("x", "X", False, "now bad")]
    assert presend.signature(a) != presend.signature(b)
    assert presend.signature(a) == presend.signature(list(a))
