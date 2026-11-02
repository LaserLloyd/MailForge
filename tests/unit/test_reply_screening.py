"""2026-11-02: RUIZU's reply to an email we sent was screened POTENTIAL_SPAM on a
content_only site ("not clearly related to the site's published content"), which
also hid it from agents. A reply in a thread WE started is expected mail."""

from __future__ import annotations

from mailforge.db.store import open_store
from mailforge.security.inbound_screening import assess_inbound, terms_pattern

BASE = dict(mode="content_only", from_addr="support@vendor.test", from_name="Vendor",
            subject="Re: firmware package", text="Please provide your order number.",
            headers={"spf": "pass"},
            # the site's own topics; the vendor mail never mentions them
            site_terms=terms_pattern(["laser engraving", "maker projects"]))


def test_reply_to_our_mail_is_content():
    r = assess_inbound(**BASE, reply_to_our_mail=True)
    assert r.status == "CONTENT", r


def test_same_mail_without_our_thread_is_still_screened():
    r = assess_inbound(**BASE)
    assert r.status != "CONTENT"


def test_failed_authentication_is_not_trusted_even_in_our_thread():
    r = assess_inbound(**{**BASE, "headers": {"spf": "fail", "dmarc": "fail"}}, reply_to_our_mail=True)
    assert r.status != "CONTENT"


def test_learned_spam_label_still_wins():
    ex = [{"sender_addr": "support@vendor.test", "subject_signature": "", "label": "SPAM"}]
    r = assess_inbound(**BASE, reply_to_our_mail=True, learned_examples=ex)
    assert r.status != "CONTENT"


def test_store_knows_threads_we_started(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        assert store.thread_started_by_us("<abc@mailforge.example>") is False
        store.conn.execute(
            "INSERT INTO sent_messages(site_id,origin,from_addr,to_addrs,subject,body,outcome,"
            "smtp_message_id,created_at) VALUES('main','manual','a@x.test','b@y.test','s','b',"
            "'SENT','<abc@mailforge.example>','2026-11-01T00:00:00+00:00')")
        assert store.thread_started_by_us("<abc@mailforge.example>") is True
        assert store.thread_started_by_us("") is False
