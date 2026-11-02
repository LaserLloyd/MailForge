"""Guardrail false positives seen 2026-11-02 on a real reply to a vendor:
an Amazon order number read as a phone number, and the vendor's own domain
refused under rule (b) although the vendor wrote to us first."""

from __future__ import annotations

from mailforge.config import Settings
from mailforge.db.store import open_store, register_sites
from mailforge.security import run_output_guardrails
from mailforge.security.presidio import _heuristic_detect


def test_order_numbers_are_not_phone_numbers():
    for text in ("Order # 249-5011686-7907863", "ref 123-4567890-1234567", "id 555-123-4567-89"):
        assert "PHONE_NUMBER" not in _heuristic_detect(text), text


def test_real_phone_numbers_still_detected():
    for text in ("call 555-123-4567", "(555) 123-4567 today", "+1 555.123.4567", "5551234567"):
        assert "PHONE_NUMBER" in _heuristic_detect(text), text


def _settings() -> Settings:
    return Settings(
        imap_accounts=[{"name": "sales", "site_id": "main", "host": "imap.example",
                        "username": "sales@example.test"}],
        smtp={"host": "smtp.example", "port": 587, "username": "sales@example.test"},
    )


def _thread(store, sender: str, cc: str = "") -> str:
    register_sites(["main"])
    acc = store.upsert_account("sales", "imap", "imap.example", 993, "sales@example.test", "main")
    store.insert_message(
        account_id=acc, folder="INBOX", uid=1, message_id="<v1@vendor.test>", thread_id="tv",
        from_addr=sender, from_name="Vendor", to_addrs="sales@example.test", cc_addrs=cc,
        subject="Re: firmware", received_at="2026-11-02T00:00:00+00:00", raw_html=b"",
        sanitized_text="Please send your order number", has_attachments=0, link_count=0,
    )
    store.record_allowlist("friend@known.test", "test")  # auto-built allowlist is non-empty
    return "tv"


def _check(store, s, recipient):
    draft = {"recipient": recipient, "subject": "Re: firmware",
             "body": "Thanks, here it is.", "thread_id": "tv"}
    ctx = {"recipient": recipient, "site_id": "main",
           "thread_participants": {"support@vendor.test", "sales@example.test", "evil@other.test"}}
    return run_output_guardrails(draft, ctx, store, s.security, None)


def test_replying_to_someone_who_wrote_to_us_passes_domain_rule(tmp_path):
    s = _settings()
    with open_store(tmp_path / "a.db") as store:
        _thread(store, "support@vendor.test", cc="evil@other.test")
        rep = _check(store, s, "support@vendor.test")
        assert not any("(b)" in r for r in rep.reasons), rep.reasons


def test_cc_only_participant_still_needs_the_domain_allowlist(tmp_path):
    s = _settings()
    with open_store(tmp_path / "b.db") as store:
        _thread(store, "support@vendor.test", cc="evil@other.test")
        rep = _check(store, s, "evil@other.test")
        assert any("(b)" in r for r in rep.reasons), rep.reasons


def test_blocked_domain_still_blocks_even_if_they_wrote(tmp_path):
    s = _settings()
    s.security.recipient_block_domains = {"vendor.test"}
    with open_store(tmp_path / "c.db") as store:
        _thread(store, "support@vendor.test")
        rep = _check(store, s, "support@vendor.test")
        assert any("explicitly blocked" in r for r in rep.reasons), rep.reasons
