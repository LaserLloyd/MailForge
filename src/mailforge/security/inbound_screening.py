"""Deterministic, site-scoped inbound screening before any LLM work.

This is deliberately a small rules engine, not a second AI classifier.  It
answers whether a message may enter the model workflow at all.  Sites in
``content_only`` mode default to review unless the message looks related to
the site's published subject matter.  Human feedback supplies exact-sender
and normalized-subject examples for later messages; it never whitelists a
message past account/security danger cues.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: Every status a message may carry. ``FILED`` is mail a deterministic
#: category rule sorted into its own bucket (e.g. GitHub notifications): it is
#: withheld from AI like spam, but it is not "questionable" and is not spam.
SCREENING_STATUSES = frozenset(
    {"UNSCREENED", "CONTENT", "POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM", "FILED"}
)
QUESTIONABLE_STATUSES = frozenset({"POTENTIAL_SPAM", "POTENTIAL_ISSUE"})
#: The ONLY statuses whose bodies an LLM, agent or bridge caller may read.
#: Gates test membership in this allowlist (see :func:`ai_may_read`), so a
#: status added later is withheld until someone deliberately allows it.
AI_READABLE_STATUSES = frozenset({"UNSCREENED", "CONTENT"})
AI_WITHHELD_STATUSES = SCREENING_STATUSES - AI_READABLE_STATUSES
#: Statuses kept out of the default Inbox listing and its counts.
INBOX_HIDDEN_STATUSES = frozenset({"SPAM", "FILED"})


def normalize_status(status: Any) -> str:
    """Upper-cased status; NULL/empty is the schema default ``UNSCREENED``."""
    value = str(status if status is not None else "").strip().upper()
    return value or "UNSCREENED"


def ai_may_read(status: Any) -> bool:
    """Fail-closed AI gate: True only for an explicitly AI-readable status."""
    return normalize_status(status) in AI_READABLE_STATUSES


def sql_status_list(statuses: Iterable[str]) -> str:
    """``'A','B'`` for an SQL ``IN`` list built from the constants above.

    Only ever called with this module's fixed status sets (validated to be
    plain upper-case identifiers), never with user input.
    """
    values = sorted(statuses)
    if not all(re.fullmatch(r"[A-Z_]+", v) for v in values):
        raise ValueError("status lists must be fixed identifiers")
    return ",".join(f"'{v}'" for v in values)

_SUBJECT_PREFIX = re.compile(r"^\s*(?:re|fwd?|aw|wg)\s*:\s*", re.IGNORECASE)
_VARIABLE = re.compile(r"\b(?:\d[\d.,:/_-]*|[a-f0-9]{12,})\b", re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9]+")

# How much of the body each rule may look at.  Bodies are mostly boilerplate:
# scanning 12k of a marketing footer for "sign in to your account" produced
# POTENTIAL_ISSUE for ordinary payment-provider newsletters, which trained the human to ignore the
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
    r"trademark\s+(?:claim|notice|infringement)|legal\s+notice|"
    # Credential/key/app grants: a provider telling you something new can now
    # act as you. These are the alerts that signal a compromised account.
    r"(?:public|ssh|gpg|deploy)\s+key\s+(?:was\s+|has\s+been\s+)?added|"
    r"personal\s+access\s+token|"
    r"oauth\s+(?:application|app)\s+(?:was\s+|has\s+been\s+)?(?:added|authorized)|"
    r"review\s+this\s+sign[ -]?in|recovery\s+codes?)\b",
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
    #: Set when a category rule matched: the ingest writes a rule
    #: classification (``model_used='rule:<rule>'``) with this category.
    category: str = ""
    rule: str = ""
    priority: int = 0


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


def _domain_matches(domain: str, allowed: Iterable[str]) -> bool:
    """Exact domain or a subdomain of one of ``allowed``."""
    domain = str(domain or "").lower().strip(".")
    if not domain:
        return False
    return any(
        domain == a or domain.endswith("." + a)
        for a in (str(x or "").lower().strip(".") for x in allowed)
        if a
    )


def _own_domain_authenticated(
    domain: str, own_domains: Iterable[str], headers: Mapping[str, Any]
) -> bool:
    """A provider-verified DKIM signature by our own domain.

    Only a holder of the domain's signing key can produce one, so own-domain
    mail that carries it (a site's contact form, say) is genuinely ours. SPF
    and DMARC alone are not accepted: shared outbound relays can pass SPF for
    many customers.
    """
    own = [d for d in own_domains if d]
    if not own or not _domain_matches(domain, own):
        return False
    signed = headers.get("dkim_pass_domains") or ()
    return any(_domain_matches(str(d), own) for d in signed)


def impersonates_own_domain(
    from_addr: str,
    own_domains: Iterable[str],
    headers: Mapping[str, Any] | None = None,
) -> bool:
    """True when inbound mail claims to come from one of our own domains.

    Genuine mail from ourselves does not arrive over the public MX, so a
    From: that claims our domain is a forged sender — the exact shape of the
    "Action Required: WebHosting Deactivation" messages in this mailbox. The
    one exception is mail the provider verified as DKIM-signed by our own
    domain (e.g. the site's own contact form).
    """
    own = tuple(x.lower() for x in own_domains if x)
    d = sender_domain(from_addr)
    if not d or not _domain_matches(d, own):
        return False
    return not _own_domain_authenticated(d, own, headers or {})


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


# --------------------------------------------------------------------------- #
# Scored spam and phishing signals.
#
# Measured against a hand-labelled copy of a real public inbox (218 messages):
# the rules below moved 37 of 38 spam/phishing messages from "potential" to
# confirmed spam with no legitimate message flagged. They look only at what a
# sender chose to put in the subject, display name and domain, plus provider-
# added authentication verdicts — never at body text, which is mostly footer.
# --------------------------------------------------------------------------- #

#: Score at or above which a content_only site files a message as SPAM.
SPAM_SCORE = 4
#: Standard sites (mail goes to the model by default) need more evidence.
STANDARD_SPAM_SCORE = 6
#: Score at which a message is at least POTENTIAL_SPAM.
POTENTIAL_SCORE = 2

# Cold sales/outreach in the SUBJECT. Every alternative is specific enough to
# be spam on its own: never a bare "crypto\w*" (it matches "cryptography") or
# a bare "ranking" (benchmark and research mail).
_COLD_SUBJECT = re.compile(
    r"\b(?:seo|backlinks?|guest\s+posts?|link\s+building|"
    r"(?:google|seo)\s+rank\w*|1st\s+page|first\s+page\s+of\s+google|"
    r"ai\s+search\s+report|"
    r"mobile\s+app|app\s+(?:proposal|development|developer)|need\s+an\s+app|"
    r"your\s+idea,?\s+our|building\s+something\s+mobile|designer\s*/\s*developers?|"
    r"web(?:site)?\s+(?:design|redesign|revamp|development|upgrade)|"
    r"modernize\s+your\s+website|fresh\s+look|"
    r"more\s+leads|leads?\s+for\s+sale|database\s+for\s+sale|\w+\s+leads\s*,|"
    r"casino|forex|crypto(?:currency)?|betting|whatsapp\s+number|"
    r"limited[- ]time\s+offer|luxury\s+(?:bags|watches)|replica)\b",
    re.IGNORECASE,
)
# "<oursite>.com - quick question?" — cold outreach opens with our own URL.
_OWN_DOMAIN_HOOK = re.compile(
    r"quick\s+(?:question|check)|follow(?:ing)?\s*-?\s*up|report|proposal|\?|"
    r"\bneed\b|\bseo\b|\bapp\b",
    re.IGNORECASE,
)
OWN_URL_HOOK = "subject opens with your own web address"
# A bare "Re:" (or "Re: meeting") with nothing else: a fake reply to get opened.
_FAKE_REPLY = re.compile(r"^\s*(?:re\s*:\s*)+(?:meeting)?\s*$", re.IGNORECASE)
# Google Classroom invitations abused to deliver sales/price spam.
_CLASSROOM_ABUSE = re.compile(
    r"class\s+invitation:.*\b(?:interested\s+in\s+your\s+products|prices?|"
    r"quotation|whatsapp)\b",
    re.IGNORECASE,
)
# Classic phishing lures in the SUBJECT.
_LURE_SUBJECT = re.compile(
    r"\b(?:voice\s*mail|voice\s+message|new\s+fax|e-?fax|parcel|shipment|"
    r"package\s+(?:status|delivery|held)|"
    r"delivery\s+(?:failed|attempt|status|notification)|re-?auth\w*|"
    r"mailbox\s+(?:full|quota|storage|upgrade)|storage\s+(?:full|limit|quota)|"
    r"password\s+expir\w*|shared\s+(?:a\s+)?(?:document|file)s?\s+with\s+you|"
    r"invoice\s+attached|payment\s+remittance)\b",
    re.IGNORECASE,
)

FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
        "msn.com", "yahoo.com", "ymail.com", "icloud.com", "me.com", "aol.com",
        "proton.me", "protonmail.com", "gmx.com", "gmx.net", "mail.com",
        "yandex.com", "yandex.ru", "zoho.com",
    }
)

# Display-name brand -> the domains that brand really sends from. A display
# name claiming the brand from any other domain is a spoof. Role names with no
# domains ("Voicemail", "IT Support") are never a real sender's name.
# Deliberately absent: "mail delivery"/"postmaster" (real bounces come from
# relays) and short common words ("ups", "apple") that occur in ordinary names.
# Sites add their own providers (hosting, registrar, bank) through
# ``[security].impersonated_brands`` in config.toml.
_BRAND_DOMAINS: dict[str, tuple[str, ...]] = {
    "paypal": ("paypal.com", "paypal.co.uk", "paypal.de", "paypal.jp"),
    "dhl": ("dhl.com", "dhl.de"),
    "fedex": ("fedex.com",),
    "usps": ("usps.com", "usps.gov"),
    "docusign": ("docusign.com", "docusign.net"),
    "microsoft": ("microsoft.com", "microsoftonline.com", "office.com", "outlook.com"),
    "office 365": ("microsoft.com", "microsoftonline.com", "office.com"),
    "github": ("github.com",),
    "google": ("google.com", "googlemail.com", "youtube.com"),
    "icloud": ("apple.com", "icloud.com"),
    "amazon": ("amazon.com", "amazon.co.jp", "amazon.co.uk", "amazon.de", "amazonaws.com"),
    "stripe": ("stripe.com",),
    "supabase": ("supabase.com", "supabase.io"),
    "mozilla": ("mozilla.com", "mozilla.org", "firefox.com"),
    "voicemail": (),
    "voice mail": (),
    "it governance": (),
    "it support": (),
    "it helpdesk": (),
    "help desk": (),
    "helpdesk": (),
    "webmail": (),
    "mail administrator": (),
    "email administrator": (),
}


def _normalize_brands(
    extra: Mapping[str, Iterable[str]] | None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    merged = dict(_BRAND_DOMAINS)
    for name, domains in (extra or {}).items():
        key = " ".join(str(name or "").lower().split())
        if len(key) < 3:
            continue
        merged[key] = tuple(
            sorted({str(d).lower().strip(".") for d in domains or () if str(d).strip(".")})
        )
    return tuple(sorted(merged.items()))


@functools.lru_cache(maxsize=16)
def _brand_tables(
    brands: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple["re.Pattern[str]", dict[str, tuple[str, ...]], tuple[str, ...]]:
    """(display-name pattern, brand -> domains, lookalike targets)."""
    table = dict(brands)
    pattern = re.compile(
        r"\b(" + "|".join(
            re.escape(b).replace(r"\ ", r"\s+") for b in sorted(table, key=len, reverse=True)
        ) + r")\b",
        re.IGNORECASE,
    )
    # Lookalike targets: brand domains whose registrable label is long enough
    # that one edit is not just another ordinary word (no "dhl.com").
    targets = tuple(
        sorted({d for doms in table.values() for d in doms if len(d.split(".")[0]) >= 6})
    )
    return pattern, table, targets

_TWO_LEVEL_SUFFIXES = frozenset({"co", "com", "net", "org", "ac", "gov", "ne", "or", "go"})


def registrable_domain(domain: str) -> str:
    """Best-effort registrable domain (``a.b.example.co.uk`` -> ``example.co.uk``)."""
    labels = [p for p in str(domain or "").lower().strip(".").split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    if labels[-2] in _TWO_LEVEL_SUFFIXES and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _edit_distance_at_most_one(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return edits + (len(b) - j) + (len(a) - i) <= 1


def lookalike_brand_domain(
    domain: str, brands: Mapping[str, Iterable[str]] | None = None
) -> str:
    """The brand domain ``domain`` imitates by one edit (``docsign.com``), or ''."""
    reg = registrable_domain(domain)
    if not reg:
        return ""
    _, _, targets = _brand_tables(_normalize_brands(brands))
    for target in targets:
        if reg != target and _edit_distance_at_most_one(reg, target):
            return target
    return ""


def spoofed_brand(
    from_name: str, domain: str, brands: Mapping[str, Iterable[str]] | None = None
) -> str:
    """Brand a display name claims while sending from a foreign domain, or ''."""
    pattern, table, _ = _brand_tables(_normalize_brands(brands))
    match = pattern.search(str(from_name or ""))
    if not match:
        return ""
    brand = " ".join(match.group(1).lower().split())
    allowed = table.get(brand, ())
    if allowed and _domain_matches(domain, allowed):
        return ""
    return brand


@dataclass(frozen=True, slots=True)
class PhishingVerdict:
    brand: str
    corroborated: bool
    hits: tuple[str, ...]


def phishing_signals(
    *,
    from_name: str,
    domain: str,
    subject: str,
    headers: Mapping[str, Any],
    brands: Mapping[str, Iterable[str]] | None = None,
) -> PhishingVerdict | None:
    """Brand impersonation, judged on display name vs. sending domain.

    A spoofed brand alone is suspicious (POTENTIAL_ISSUE); with a lure, an
    account/payment claim, a lookalike or freemail domain, or a failed DMARC
    verdict it is phishing (SPAM). A lookalike domain plus a lure is phishing
    even without a brand in the display name.
    """
    brand = spoofed_brand(from_name, domain, brands)
    lookalike = lookalike_brand_domain(domain, brands)
    lure = bool(_LURE_SUBJECT.search(subject))
    claim = bool(_ISSUE_STRONG.search(subject) or _ISSUE_WEAK.search(subject))
    freemail = registrable_domain(domain) in FREEMAIL_DOMAINS
    dmarc_fail = str(headers.get("dmarc") or "") == "fail"
    hits = tuple(
        name
        for name, hit in (
            (f"display name claims {brand}", bool(brand)),
            (f"lookalike of {lookalike}", bool(lookalike)),
            ("lure subject", lure),
            ("account/payment claim", claim),
            ("freemail sender", freemail and bool(brand)),
            ("DMARC fail", dmarc_fail),
        )
        if hit
    )
    if brand:
        corroborated = lure or claim or bool(lookalike) or freemail or dmarc_fail
        return PhishingVerdict(brand, corroborated, hits)
    if lookalike and (lure or claim):
        return PhishingVerdict(lookalike, True, hits)
    return None


def spam_score(
    *,
    from_addr: str,
    from_name: str,
    subject: str,
    headers: Mapping[str, Any],
    own_domains: Iterable[str] = (),
    learned_spam_domains: Iterable[str] = (),
) -> tuple[int, tuple[str, ...]]:
    """Additive spam score from subject, sender and header facts."""
    subject = " ".join(str(subject or "").split())
    domain = sender_domain(from_addr)
    reg = registrable_domain(domain)
    score = 0
    hits: list[str] = []

    def hit(points: int, name: str) -> None:
        nonlocal score
        score += points
        hits.append(name)

    if _COLD_SUBJECT.search(subject):
        hit(4, "cold-outreach subject")
    own_bases = {registrable_domain(d).split(".")[0] for d in own_domains if d}
    own_bases.discard("")
    if own_bases:
        own_url = re.compile(
            r"\b(?:www\.)?(?:" + "|".join(re.escape(b) for b in sorted(own_bases))
            + r")\.[a-z]{2,10}\b",
            re.IGNORECASE,
        )
        if own_url.search(subject) and _OWN_DOMAIN_HOOK.search(subject):
            hit(4, OWN_URL_HOOK)
    if _FAKE_REPLY.match(subject):
        hit(3, "empty fake reply")
    if _CLASSROOM_ABUSE.search(subject):
        hit(4, "classroom invitation used for sales")
    learned = {str(d or "").lower() for d in learned_spam_domains}
    if domain and domain in learned and reg not in FREEMAIL_DOMAINS:
        hit(4, "domain you marked as spam")
    if score:
        # Corroboration only: none of these is spam on its own.
        if reg in FREEMAIL_DOMAINS:
            hit(1, "freemail sender")
        reply_to = str(headers.get("reply_to_domain") or "")
        if reply_to and registrable_domain(reply_to) != reg:
            hit(1, "replies go to a different domain")
    return score, tuple(hits)


# --------------------------------------------------------------------------- #
# Deterministic category rules (e.g. file GitHub notifications).
# --------------------------------------------------------------------------- #
def _rule_value(rule: Any, name: str, default: Any = None) -> Any:
    if isinstance(rule, Mapping):
        return rule.get(name, default)
    return getattr(rule, name, default)


def _auth_failed_for(domains: Iterable[str], headers: Mapping[str, Any]) -> bool:
    """Provider verdicts that positively contradict the claimed sender.

    Missing verdicts are not failure (stored mail has no header facts), but a
    DMARC fail, or DKIM results with no passing signature by the domain, mean
    the From: line cannot be trusted to sort mail out of view.
    """
    if not headers:
        return False
    if str(headers.get("dmarc") or "") in {"fail", "quarantine", "reject"}:
        return True
    dkim = str(headers.get("dkim") or "")
    if dkim:
        signed = headers.get("dkim_pass_domains") or ()
        if not any(_domain_matches(str(d), domains) for d in signed):
            return True
    return False


def match_category_rule(
    rules: Iterable[Any],
    *,
    site_id: str,
    from_addr: str,
    subject: str,
    headers: Mapping[str, Any] | None = None,
) -> tuple[Any, bool] | None:
    """First enabled rule matching the sender: ``(rule, exempt)`` or None.

    ``exempt`` is True for a security/account notice the rule must NOT file
    away (its ``exempt_subject_regex`` or ``exempt_reasons`` matched). Mail
    whose provider authentication contradicts the claimed domain never
    matches, so a forged From: cannot hide itself in a filed bucket.
    """
    headers = headers or {}
    address = str(from_addr or "").strip().lower()
    domain = sender_domain(address)
    site = str(site_id or "").lower()
    subject = str(subject or "")
    for rule in rules or ():
        if not _rule_value(rule, "enabled", True):
            continue
        sites = [str(s).lower() for s in (_rule_value(rule, "sites") or ())]
        if sites and site not in sites:
            continue
        domains = [str(d).lower() for d in (_rule_value(rule, "from_domains") or ())]
        addrs = [str(a).lower() for a in (_rule_value(rule, "from_addrs") or ())]
        list_suffixes = [str(x).lower() for x in (_rule_value(rule, "list_id_suffixes") or ())]
        list_id = str(headers.get("list_id") or "").lower()
        by_sender = (domain and _domain_matches(domain, domains)) or (address and address in addrs)
        by_list = bool(list_id) and any(
            list_id == sfx.lstrip(".") or list_id.endswith("." + sfx.lstrip("."))
            for sfx in list_suffixes
        )
        if not (by_sender or by_list):
            continue
        subject_regex = str(_rule_value(rule, "subject_regex", "") or "")
        if subject_regex and not re.search(subject_regex, subject):
            continue
        trusted = domains or [registrable_domain(domain)]
        if _rule_value(rule, "require_authenticated", True) and _auth_failed_for(
            trusted, headers
        ):
            continue
        exempt_regex = str(_rule_value(rule, "exempt_subject_regex", "") or "")
        reasons = {str(r).lower() for r in (_rule_value(rule, "exempt_reasons") or ())}
        exempt = bool(exempt_regex and re.search(exempt_regex, subject)) or (
            str(headers.get("github_reason") or "").lower() in reasons
        )
        return rule, exempt
    return None


def _category_result(rule: Any, exempt: bool, learned: tuple[str, str] | None) -> ScreeningResult:
    name = str(_rule_value(rule, "name", "rule") or "rule")
    label = str(_rule_value(rule, "label", name) or name)
    category = str(_rule_value(rule, "category", "") or "").upper()
    if exempt:
        return ScreeningResult(
            str(_rule_value(rule, "exempt_status", "POTENTIAL_ISSUE") or "POTENTIAL_ISSUE"),
            f"{label} security or account notice; verify it by signing in to {label} "
            "directly, not through a link in the mail.",
            "RULE",
            category=category,
            rule=name,
            priority=4,
        )
    if learned and learned[0] == "CONTENT":
        return ScreeningResult(
            "CONTENT", learned[1], "LEARNED", category=category, rule=name,
            priority=int(_rule_value(rule, "priority", 1) or 0),
        )
    if learned and learned[0] == "SPAM":
        return ScreeningResult("SPAM", learned[1], "LEARNED", category=category, rule=name)
    return ScreeningResult(
        "FILED",
        f"filed by category rule {name} ({label})",
        "RULE",
        category=category,
        rule=name,
        priority=int(_rule_value(rule, "priority", 1) or 0),
    )


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
    from_name: str = "",
    headers: Mapping[str, Any] | None = None,
    category_rules: Iterable[Any] = (),
    site_id: str = "",
    learned_spam_domains: Iterable[str] = (),
    brand_domains: Mapping[str, Iterable[str]] | None = None,
) -> ScreeningResult:
    """Screen one message without network, tools, or model access.

    Only ``UNSCREENED``/``CONTENT`` let a body reach an LLM (see
    :func:`ai_may_read`). Order of precedence, both modes:

    1. a forged own-domain sender (POTENTIAL_ISSUE);
    2. brand impersonation (SPAM when corroborated, else POTENTIAL_ISSUE);
    3. a category rule (FILED, or POTENTIAL_ISSUE for its security notices).

    ``content_only`` sites then require a topical signal for CONTENT, and
    everything unproven falls through to POTENTIAL_SPAM. ``standard`` sites
    only act on strong evidence (learned human spam labels, a high spam score)
    and otherwise leave the message UNSCREENED for normal triage.
    """
    standard = str(mode or "standard").lower() != "content_only"
    headers = dict(headers or {})
    own_domains = tuple(own_domains or ())
    subject = subject or ""
    domain = sender_domain(from_addr)
    link_note = " Embedded links remain disabled." if link_count else ""
    examples = list(learned_examples or ())
    learned = _learned_label(examples, from_addr, subject)

    # 1. Forged sender — outranks everything, including a human "legitimate"
    #    mark, because the address it was learned against is exactly what is
    #    being spoofed.
    if impersonates_own_domain(from_addr, own_domains, headers):
        return ScreeningResult(
            "POTENTIAL_ISSUE",
            "sender claims one of your own domains but arrived from outside; "
            f"treat as forged.{link_note}",
        )

    # 2. Brand impersonation: "PayPal" <billing@unrelated.example>.
    phish = phishing_signals(
        from_name=from_name, domain=domain, subject=subject, headers=headers,
        brands=brand_domains,
    )
    if phish is not None and phish.corroborated:
        return ScreeningResult(
            "SPAM",
            f"phishing: {'; '.join(phish.hits)} (sent from {domain or 'unknown'})",
            "AUTO_RULE",
        )

    # 3. Deterministic category rules (GitHub notifications by default).
    matched = match_category_rule(
        category_rules, site_id=site_id, from_addr=from_addr, subject=subject,
        headers=headers,
    )
    if matched is not None:
        return _category_result(matched[0], matched[1], learned)

    if phish is not None:
        return ScreeningResult(
            "POTENTIAL_ISSUE",
            f"display name claims {phish.brand} but the mail was sent from "
            f"{domain or 'an unknown domain'}; verify with {phish.brand} directly."
            f"{link_note}",
        )

    score, hits = spam_score(
        from_addr=from_addr, from_name=from_name, subject=subject, headers=headers,
        own_domains=own_domains, learned_spam_domains=learned_spam_domains,
    )
    score_reason = "; ".join(hits)

    if standard:
        # Standard sites send mail to the model by default; only strong,
        # deterministic evidence holds a message back.
        if learned and learned[0] == "SPAM":
            return ScreeningResult("SPAM", learned[1], "LEARNED")
        if score >= STANDARD_SPAM_SCORE:
            return ScreeningResult("SPAM", f"spam rules: {score_reason}", "AUTO_RULE")
        if score >= SPAM_SCORE:
            return ScreeningResult(
                "POTENTIAL_SPAM", f"spam rules: {score_reason}", "AUTO_RULE"
            )
        return ScreeningResult("UNSCREENED", "standard site screening")

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

    # 4. Account/security/payment claims in the SUBJECT always require
    #    direct-site verification, even from a previously-trusted sender.
    if issue_subject:
        return ScreeningResult(
            "POTENTIAL_ISSUE",
            "account, security, payment, renewal, or legal claim; verify by opening "
            f"the provider site directly.{link_note}",
        )

    if learned and learned[0] == "SPAM":
        return ScreeningResult("SPAM", learned[1], "LEARNED")

    # 5. Scored spam rules. A topical message (it talks about the site's own
    #    subject) needs the higher bar before it is filed away unseen — except
    #    the "www.<yoursite>.com – quick question?" opener, whose generic
    #    "question about" wording is exactly what makes it look topical.
    bar = STANDARD_SPAM_SCORE if topical and OWN_URL_HOOK not in hits else SPAM_SCORE
    if score >= bar:
        return ScreeningResult("SPAM", f"spam rules: {score_reason}", "AUTO_RULE")
    if score >= POTENTIAL_SCORE:
        return ScreeningResult(
            "POTENTIAL_SPAM", f"spam rules: {score_reason}", "AUTO_RULE"
        )

    # 6. Sales/scam patterns beat a body-only "issue" cue: a marketing mail whose
    #    footer says "sign in to your account" is spam, not a security event.
    if unsolicited:
        return ScreeningResult("POTENTIAL_SPAM", "unsolicited sales or promotion pattern")

    # 7. An issue cue found only in the body, with nothing topical to support it,
    #    is still worth a human look but is not a confirmed claim.
    if issue_body and not topical:
        return ScreeningResult(
            "POTENTIAL_ISSUE",
            "account or payment wording in the message body; verify by opening the "
            f"provider site directly.{link_note}",
        )

    # 8. CONTENT requires a real topical signal — never the brand name alone.
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
