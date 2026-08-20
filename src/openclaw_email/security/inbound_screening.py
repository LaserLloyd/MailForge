"""Deterministic, site-scoped inbound screening before any LLM work.

This is deliberately a small rules engine, not a second AI classifier.  It
answers whether a message may enter the model workflow at all.  Sites in
``content_only`` mode default to review unless the message looks related to
the site's published subject matter.  Human feedback supplies exact-sender
and normalized-subject examples for later messages; it never whitelists a
message past account/security danger cues.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

SCREENING_STATUSES = frozenset(
    {"UNSCREENED", "CONTENT", "POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"}
)
QUESTIONABLE_STATUSES = frozenset({"POTENTIAL_SPAM", "POTENTIAL_ISSUE"})
AI_WITHHELD_STATUSES = frozenset({"POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"})

_SUBJECT_PREFIX = re.compile(r"^\s*(?:re|fwd?|aw|wg)\s*:\s*", re.IGNORECASE)
_VARIABLE = re.compile(r"\b(?:\d[\d.,:/_-]*|[a-f0-9]{12,})\b", re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9]+")

# How much of the body each rule may look at.  Bodies are mostly boilerplate:
# scanning 12k of a marketing footer for "sign in to your account" produced
# POTENTIAL_ISSUE for PayPal newsletters, which trained the human to ignore the
# one status that means "stop and check this".  The subject is what the sender
# actually chose to say, so it carries the weight; the body only corroborates.
_BODY_SCAN_CHARS = 2000

# TOPICAL content — things only someone actually engaging with the site's
# subject matter says. This packaged default is deliberately generic; a site
# supplies its own vocabulary via ``[sites.<id>].content_terms`` in config.toml
# (built into a pattern by :func:`terms_pattern` and passed as ``site_terms``).
# Deliberately does NOT include the site/brand name: see ``brand_pattern``.
# NOTE the trailing \b sits OUTSIDE the group: with it inside, only the last
# alternative would be boundary-terminated, so a topic word could match the
# *prefix* of a longer word (e.g. a brand name echoed in a phishing body).
# Every alternative must be bounded on both sides; use stems (``engrav\w*``)
# rather than optional suffix groups, which spell "engraveing" and never match.
_CONTENT_TOPICAL = re.compile(
    r"\b(?:question\s+about|read\s+your\s+(?:article|post|guide|newsletter)|"
    r"your\s+(?:article|post|guide|tutorial|video|documentation)|"
    r"order\s+(?:number|#|status)|my\s+order|quote\s+for|quotation|"
    r"product\s+(?:question|enquiry|inquiry)|"
    r"how\s+(?:do|can)\s+i\b|does\s+(?:it|this)\s+support|"
    r"availability|lead\s+time|specifications?|spec\s+sheet|datasheet)\b",
    re.IGNORECASE,
)


def terms_pattern(terms: Iterable[str]) -> "re.Pattern[str] | None":
    """Build a both-sides-bounded alternation from configured site terms.

    ``["laser", "engrav\\w*"]`` -> ``\b(?:laser|engrav\\w*)\b``. Terms are used
    as regex fragments so a site can supply stems; an unparsable term set
    returns ``None`` (the packaged default vocabulary is then used).
    """
    parts = [str(t).strip() for t in terms or () if str(t).strip()]
    if not parts:
        return None
    try:
        return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)
    except re.error:
        return None


def brand_pattern(names: Iterable[str]) -> "re.Pattern[str] | None":
    """Pattern matching a site's own name(s), spaced or concatenated.

    The site's own name is a SPAM MAGNET, not a content signal: cold outreach
    opens with it ("<yoursite>.com: I gave your website a fresh look") and the
    recipient address is echoed into subjects ("Account security update for
    you@<yoursite>.com"). A mention is therefore never sufficient on its own —
    it must be corroborated by a topical match — and the brand is erased from
    the text before topical matching, because a brand can *contain* a topic
    word ("Acme Laser" contains "laser").
    """
    variants: set[str] = set()
    for name in names or ():
        value = " ".join(str(name or "").split()).strip()
        if len(value) < 3:
            continue
        variants.add(re.escape(value).replace(r"\ ", r"\s*"))
        squashed = value.replace(" ", "")
        if len(squashed) >= 3:
            variants.add(re.escape(squashed))
    if not variants:
        return None
    return re.compile(r"\b(?:" + "|".join(sorted(variants)) + r")\b", re.IGNORECASE)


# "your website/site" + an offer is the signature of unsolicited outreach, so
# it belongs here rather than in the content vocabulary where it used to live.
# Issue cues come in two strengths, because "log in to your account" appears in
# the footer of every newsletter ever sent. Treating those as security events
# put marketing in POTENTIAL_ISSUE, which teaches the human to ignore the one
# status that means "stop and check this" — the failure that matters most.
#
#   STRONG: an actual claim/demand. Trusted anywhere, subject or body.
#   WEAK  : ordinary boilerplate vocabulary. Only meaningful in a SUBJECT,
#           where the sender chose it as the point of the message.
_ISSUE_STRONG = re.compile(
    r"\b(?:account\s+(?:locked|suspended|disabled|compromised|security)|"
    r"verify\s+your\s+(?:email|account|identity|device)|"
    r"(?:security|account)\s+(?:alert|update|notice)|"
    r"unusual\s+activity|suspicious\s+(?:activity|sign[ -]?in|login)|"
    r"breach|compromised|reset\s+your\s+password|"
    r"one[ -]?time\s+(?:code|password)|otp|two[ -]?factor|2fa|"
    r"update\s+your\s+payment|payment\s+(?:failed|declined)|billing\s+notice|"
    r"invoice\s+(?:due|overdue)|deactivat(?:e|ion)|"
    r"domain\s+(?:renewal|expiration|expires?)|"
    r"hosting\s+(?:renewal|expiration|expires?)|"
    r"copyright\s+(?:claim|notice|infringement)|"
    r"trademark\s+(?:claim|notice|infringement)|legal\s+notice)\b",
    re.IGNORECASE,
)
_ISSUE_WEAK = re.compile(
    r"\b(?:sign[ -]?in|log[ -]?in|password|verify|verification|"
    r"payment\s+(?:method|due)|renewal)\b",
    re.IGNORECASE,
)

# Unsolicited sales/services.  Expanded from the real corpus — the old pattern
# required phrases like "rank on google" and so missed "Ready to Show Up on
# Google?", "App Proposal", "Gave your website a fresh look".
_UNSOLICITED = re.compile(
    r"\b(?:seo\b|backlinks?|guest\s+post|"
    r"rank(?:ing)?s?\b|show\s+up\s+on\s+google|1st\s+page\s+of\s+google|"
    r"increase\s+(?:your\s+)?traffic|digital\s+marketing|lead\s+generation|"
    r"web(?:site)?\s+(?:design|revamp|upgrade|redesign)|fresh\s+look|"
    r"app\s+(?:proposal|project|development)|apps?\s+proposal|"
    r"mobile\s+app\b|designer\s*/\s*developers?|developers?\s*\?|"
    r"business\s+proposal|investment\s+opportunity|"
    r"crypto(?:currency)?\s+(?:offer|investment)|token\s+distribution|"
    r"casino|betting|forex|depositors?|"
    r"email\s+lists?\b|mx\s+tested|payday\s+loan|"
    r"dear\s+(?:website|site)\s+owner|donation\s+winner|"
    r"annual\s+report|corporate\s+filing|payment\s+advice)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    status: str
    reason: str
    source: str = "AUTO_POLICY"


def sender_domain(address: str | None) -> str:
    value = str(address or "").strip().lower()
    return value.rsplit("@", 1)[-1] if "@" in value else ""


def subject_signature(subject: str | None) -> str:
    """Stable, non-secret signature for matching repeated subject templates."""
    value = str(subject or "").strip().lower()
    while _SUBJECT_PREFIX.match(value):
        value = _SUBJECT_PREFIX.sub("", value, count=1)
    value = _VARIABLE.sub("#", value)
    return _NON_WORD.sub(" ", value).strip()[:240]


def _example_value(example: Any, name: str, default: str = "") -> str:
    try:
        return str(example[name] or default)
    except (KeyError, IndexError, TypeError):
        return str(getattr(example, name, default) or default)


def _learned_label(
    examples: Iterable[Any], from_addr: str, subject: str
) -> tuple[str, str] | None:
    """Return the newest applicable human example, if one matches.

    Exact addresses are safe enough for spam recurrence.  Subject examples
    need at least two meaningful tokens so generic subjects such as "Hello"
    never become broad rules.  Store methods return newest-first rows.
    """
    address = str(from_addr or "").strip().lower()
    signature = subject_signature(subject)
    usable_subject = len(signature) >= 12 and len(signature.split()) >= 2
    for example in examples:
        label = _example_value(example, "label").upper()
        learned_address = _example_value(example, "sender_addr").lower()
        learned_subject = _example_value(example, "subject_signature")
        if learned_address and address and learned_address == address:
            return label, "same sender as a human-reviewed message"
        if usable_subject and learned_subject == signature:
            return label, "same subject pattern as a human-reviewed message"
    return None


def impersonates_own_domain(from_addr: str, own_domains: Iterable[str]) -> bool:
    """True when inbound mail claims to come from one of our own domains.

    Genuine mail from ourselves does not arrive over the public MX, so a
    From: that claims our domain is a forged sender — the exact shape of the
    "Action Required: WebHosting Deactivation" messages in this mailbox.
    """
    d = sender_domain(from_addr)
    if not d:
        return False
    return any(d == o or d.endswith("." + o) for o in (x.lower() for x in own_domains if x))


def _topical(
    text: str,
    site_terms: "re.Pattern[str] | None",
    brand: "re.Pattern[str] | None" = None,
) -> bool:
    """Whether the text says something about the site's subject matter.

    The brand is erased first: a brand such as "Acme Laser" contains the topic
    word "laser", so without this, addressing the mail to the company ("Hi Acme
    Laser") would score as topical — the same leak as matching the bare brand
    name, just spelled with a space. After stripping, "a laser cutter" still
    matches.
    """
    scanned = brand.sub(" ", text) if brand is not None else text
    return bool((site_terms or _CONTENT_TOPICAL).search(scanned))


def assess_inbound(
    *,
    mode: str,
    from_addr: str,
    subject: str,
    text: str,
    link_count: int = 0,
    has_attachments: bool = False,
    learned_examples: Iterable[Any] = (),
    own_domains: Iterable[str] = (),
    site_terms: "re.Pattern[str] | None" = None,
    brand_terms: "re.Pattern[str] | None" = None,
) -> ScreeningResult:
    """Screen one message without network, tools, or model access.

    Only ``CONTENT`` lets a body reach an LLM (see ``AI_WITHHELD_STATUSES``), so
    the bar for CONTENT is deliberately the strictest thing here: a message must
    say something *topical*.  Naming the site is not enough — spam names the site
    constantly.  Everything unproven falls through to POTENTIAL_SPAM, which is
    withheld from AI but still visible to a human in the Inbox.
    """
    if str(mode or "standard").lower() != "content_only":
        return ScreeningResult("UNSCREENED", "standard site screening")

    subject = subject or ""
    body = (text or "")[:_BODY_SCAN_CHARS]
    both = f"{subject}\n{body}"

    # A strong cue counts wherever it appears; a weak one only in the subject.
    issue_subject = bool(_ISSUE_STRONG.search(subject) or _ISSUE_WEAK.search(subject))
    issue_body = bool(_ISSUE_STRONG.search(body))
    unsolicited = bool(_UNSOLICITED.search(both))
    topical = _topical(both, site_terms, brand_terms)
    brand_only = (
        brand_terms is not None and bool(brand_terms.search(both)) and not topical
    )
    learned = _learned_label(learned_examples, from_addr, subject)
    link_note = " Embedded links remain disabled." if link_count else ""

    # 1. Forged sender — outranks everything, including a human "legitimate"
    #    mark, because the address it was learned against is exactly what is
    #    being spoofed.
    if impersonates_own_domain(from_addr, own_domains):
        return ScreeningResult(
            "POTENTIAL_ISSUE",
            "sender claims one of your own domains but arrived from outside; "
            f"treat as forged.{link_note}",
        )

    # 2. Account/security/payment claims in the SUBJECT always require
    #    direct-site verification, even from a previously-trusted sender.
    if issue_subject:
        return ScreeningResult(
            "POTENTIAL_ISSUE",
            "account, security, payment, renewal, or legal claim; verify by opening "
            f"the provider site directly.{link_note}",
        )

    if learned and learned[0] == "SPAM":
        return ScreeningResult("SPAM", learned[1], "LEARNED")

    # 3. Sales/scam patterns beat a body-only "issue" cue: a marketing mail whose
    #    footer says "sign in to your account" is spam, not a security event.
    if unsolicited:
        return ScreeningResult("POTENTIAL_SPAM", "unsolicited sales or promotion pattern")

    # 4. An issue cue found only in the body, with nothing topical to support it,
    #    is still worth a human look but is not a confirmed claim.
    if issue_body and not topical:
        return ScreeningResult(
            "POTENTIAL_ISSUE",
            "account or payment wording in the message body; verify by opening the "
            f"provider site directly.{link_note}",
        )

    # 5. CONTENT requires a real topical signal — never the brand name alone.
    if topical:
        return ScreeningResult("CONTENT", "matches the site's expected content topics")
    if learned and learned[0] == "CONTENT":
        return ScreeningResult("CONTENT", learned[1], "LEARNED")

    details = []
    if brand_only:
        details.append("names the site but says nothing about its subject matter")
    if link_count:
        details.append(f"{int(link_count)} embedded link(s)")
    if has_attachments:
        details.append("an attachment")
    extra = f" ({'; '.join(details)})" if details else ""
    return ScreeningResult(
        "POTENTIAL_SPAM",
        "not clearly related to the site's published content" + extra,
    )
