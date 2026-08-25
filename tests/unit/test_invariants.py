"""Security-invariant tests (build spec §0, §11).

These assert the non-negotiable invariants in code so a regression fails CI.
They run without any optional ML deps (fallbacks active).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from siftforge.agent.tools import ALLOWED_TOOLS, FORBIDDEN_TOOLS, assert_tool_allowed
from siftforge.audit.log import AuditLog, verify_chain
from siftforge.config import IMAPAccount, Settings, SMTPAccount
from siftforge.db.store import open_store
from siftforge.mail.normalize import normalize_email
from siftforge.security import run_output_guardrails


def _settings() -> Settings:
    return Settings(
        imap_accounts=[IMAPAccount(name="t", host="h", username="u")],
        smtp=SMTPAccount(host="h", username="u"),
    )


def test_no_destructive_tools_in_catalog():
    # §0.2 / §11: the LLM never gets send/delete/forward/archive.
    assert FORBIDDEN_TOOLS.isdisjoint(ALLOWED_TOOLS)
    assert ALLOWED_TOOLS == {"propose_draft", "propose_label"}
    for bad in ("send_email", "delete", "forward", "archive"):
        with pytest.raises(PermissionError):
            assert_tool_allowed(bad)


def test_autosend_invariant_enforced():
    # §0.1: autosend_allowed must be False; require_human_approval must be True.
    s = _settings()
    s.assert_invariants()  # default config passes
    s.security.autosend_allowed = True
    with pytest.raises(ValueError):
        s.assert_invariants()


def test_db_created_0600(tmp_path: Path):
    # §0.6: the DB file is chmod 0600 at creation.
    import os
    import stat

    db = tmp_path / "agent.db"
    st = open_store(db)
    mode = stat.S_IMODE(os.stat(db).st_mode)
    assert mode == 0o600
    st.close()


def test_normalize_strips_invisibles_and_symbolizes_links():
    # §4: invisibles stripped; links replaced with symbols; real URL hidden.
    raw = "Click ​here‮: visit https://evil.example.com/login now"
    ne = normalize_email(raw, is_html=False)
    assert "​" not in ne.text and "‮" not in ne.text
    assert "evil.example.com" not in ne.text  # real URL not exposed to the model
    assert ne.links, "link should be captured controller-side"


def test_guardrails_block_pii_and_secrets():
    # §5/§11: output guardrails block a draft leaking PII + secrets.
    s = _settings()
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        st = open_store(f.name)
        draft = {
            "recipient": "attacker@evil.example",
            "subject": "x",
            "body": "SSN 123-45-6789 and key AKIAIOSFODNN7EXAMPLE",  # scrub-check: allow
        }
        ctx = {"thread_participants": {"alice@example.com"}, "contacts": set()}
        report = run_output_guardrails(draft, ctx, st, s.security, None)
        assert report.passed is False
        joined = " ".join(report.reasons).lower()
        assert "ssn" in joined or "pii" in joined or "secret" in joined
        st.close()


def test_audit_chain_verifies_and_detects_tampering(tmp_path: Path):
    # §7/§11: SHA-256 hash chain verifies; any edit breaks it.
    db = tmp_path / "agent.db"
    st = open_store(db)
    audit = AuditLog(st)
    audit.record("agent", "llm_call", "messages", 1, {"node": "classify"})
    audit.record("agent", "tool_call", "drafts", 1, {"tool": "propose_draft"})
    audit.approval({"by": "user"}, subject_id=1)
    ok, n, bad = verify_chain(db)
    assert ok and n == 3 and bad is None

    # Tamper with a row directly → chain must break.
    st.conn.execute("UPDATE audit_log SET detail_json='HACKED' WHERE id=2")
    st.conn.commit()
    ok2, _, bad2 = verify_chain(db)
    assert ok2 is False and bad2 is not None
    st.close()


def test_audit_redacts_secrets_in_detail(tmp_path: Path):
    # §0.5: secrets/PII redacted before write.
    db = tmp_path / "agent.db"
    st = open_store(db)
    audit = AuditLog(st)
    audit.record("agent", "error", "messages", 1, {"oops": "SSN 123-45-6789 leaked"})
    rows = st.iter_audit()
    assert "123-45-6789" not in (rows[-1]["detail_json"] or "")
    st.close()
