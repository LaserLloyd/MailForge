"""Screening regressions: can spam talk its way into CONTENT?

Only ``CONTENT`` lets a message body reach an LLM, so every case here asks that
one question. The shapes are the ones that actually occur in a public inbox —
brand-name spam, forged senders, marketing footers that read like a security
alert — expressed with a fictional site ("Acme Laser Workshop") and
documentation domains.
"""

from __future__ import annotations

import pytest

from siftforge.security.inbound_screening import (
    AI_WITHHELD_STATUSES,
    _CONTENT_TOPICAL,
    assess_inbound,
    brand_pattern,
    impersonates_own_domain,
    terms_pattern,
)

OWN = ("acmeworkshop.example",)
# A site's configured vocabulary ([sites.<id>].content_terms) and its own
# name(s). The brand deliberately CONTAINS a topic word ("laser") — that overlap
# is what the stripping rules below exist for.
SITE_TERMS = terms_pattern(
    ["lasers?", "engrav\\w*", "cut(?:ting)?\\s+file", "svgs?", "dxfs?", "kerf",
     "acrylics?", "plywood", "focal\\s+length"]
)
BRAND = brand_pattern(["Acme Laser Workshop", "acmeworkshop"])


def screen(subject: str, text: str = "", frm: str = "someone@example.com", **kw):
    kw.setdefault("site_terms", SITE_TERMS)
    kw.setdefault("brand_terms", BRAND)
    return assess_inbound(
        mode="content_only", from_addr=frm, subject=subject, text=text,
        own_domains=OWN, **kw,
    )


# --- the brand name must never buy CONTENT --------------------------------
@pytest.mark.parametrize(
    "frm, subject",
    [
        ("admin@example.net", "Account security update for you@acmeworkshop.example"),
        ("l.laurie@example.com", "acmeworkshop.example: Ready to Show Up on Google?"),
        ("s.lee@example.com", "acmeworkshop.example : Gave your website a fresh look (concept)"),
        ("r.kashyap@example.net", "acmeworkshop.example :Gave your website a fresh look"),
        ("hello@example.org", "Designer / Developer"),
        ("a.brown@example.net", "Re: rankings.?"),
    ],
)
def test_spam_shapes_never_reach_the_model(frm, subject):
    assert screen(subject, frm=frm).status in AI_WITHHELD_STATUSES


def test_brand_mention_alone_is_not_content():
    assert screen("acmeworkshop.example: I gave your website a fresh look").status != "CONTENT"


def test_brand_spelled_with_a_space_is_not_topical():
    # "Acme Laser Workshop" contains the topic word "laser"; brand is stripped first.
    assert screen("Hi Acme Laser Workshop, quick question",
                  "Hi Acme Laser Workshop").status != "CONTENT"


def test_recipient_address_echoed_in_body_is_not_topical():
    # A phish body quoting the recipient once scored topical because a topic
    # stem matched the PREFIX of the brand in the address.
    r = screen(
        "New Voice Message",
        "**Recipient:** you (you@acmeworkshop.example)",
    )
    assert r.status != "CONTENT"


# --- genuine reader mail must still get through ---------------------------
@pytest.mark.parametrize(
    "subject, body",
    [
        ("Question about your acrylic engraving guide", "kerf and focal length"),
        ("SVG cutting file wouldn't open", "the dxf you posted"),
        ("Re: plywood settings", "what speed on 3mm plywood?"),
        ("engraving question", "step 3 of your tutorial"),
    ],
)
def test_genuine_reader_mail_is_content(subject, body):
    assert screen(subject, body, frm="jane@example.com").status == "CONTENT"


def test_packaged_default_vocabulary_handles_a_site_with_no_terms():
    # No configured content_terms => the generic packaged vocabulary applies.
    r = assess_inbound(
        mode="content_only",
        from_addr="pat@example.com",
        subject="Question about your article",
        text="I read your guide and had one question.",
    )
    assert r.status == "CONTENT"


# --- vocabulary stems ------------------------------------------------------
@pytest.mark.parametrize(
    "word",
    ["engraving", "engraved", "engraver", "lasers", "acrylics", "kerf", "laser cutter"],
)
def test_configured_vocabulary_matches_stems(word):
    assert SITE_TERMS.search(word), f"{word!r} should be topical"


@pytest.mark.parametrize(
    "text", ["acmeworkshop.example", "you@acmeworkshop.example", "acmeworkshop"]
)
def test_brand_is_not_topical_vocabulary(text):
    assert not _CONTENT_TOPICAL.search(text), f"{text!r} must not be topical"
    # ...and not via the site's own vocabulary either, once the brand is erased.
    assert not SITE_TERMS.search(BRAND.sub(" ", text)), f"{text!r} must not be topical"


def test_brand_pattern_ignores_a_too_short_name():
    assert brand_pattern(["ab", ""]) is None
    assert terms_pattern([]) is None


# --- forged sender ---------------------------------------------------------
def test_inbound_claiming_our_own_domain_is_flagged():
    r = screen("Action Required: WebHosting Deactivation", frm="you@acmeworkshop.example")
    assert r.status == "POTENTIAL_ISSUE"
    assert "forged" in r.reason


def test_forged_sender_beats_a_learned_content_label():
    # The learned label keys on the sender address — which is what is spoofed.
    learned = [{"label": "CONTENT", "sender_addr": "you@acmeworkshop.example",
                "subject_signature": ""}]
    r = screen("hello", frm="you@acmeworkshop.example", learned_examples=learned)
    assert r.status == "POTENTIAL_ISSUE"


def test_impersonation_helper_matches_subdomains_not_lookalikes():
    assert impersonates_own_domain("x@mail.acmeworkshop.example", OWN)
    assert not impersonates_own_domain("x@acmeworkshop.example.evil.test", OWN)
    assert not impersonates_own_domain("noreply@mail.example.net", ("mailhost.example",))


# --- issue vs spam ---------------------------------------------------------
def test_marketing_footer_does_not_manufacture_an_issue():
    # A newsletter was POTENTIAL_ISSUE purely from a boilerplate footer.
    r = screen("Build AI Skills for Your Business.",
               "Log in to your account to learn more. Unsubscribe here.",
               frm="noreply@news.example.org")
    assert r.status == "POTENTIAL_SPAM"


def test_security_claim_in_subject_still_wins():
    r = screen("Verify your email to avoid suspension", frm="noreply@example.net")
    assert r.status == "POTENTIAL_ISSUE"


def test_payment_method_phish_is_caught():
    r = screen("Action Required: Update Your Payment Method", frm="support@example.org")
    assert r.status in AI_WITHHELD_STATUSES


def test_standard_mode_is_untouched():
    assert assess_inbound(mode="standard", from_addr="a@example.com", subject="x",
                          text="y").status == "UNSCREENED"
