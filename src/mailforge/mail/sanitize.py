"""HTML → text sanitisation (build spec §4.1, input pipeline step 1).

Deterministic, pre-LLM. Untrusted email HTML is reduced to plain text via a
fixed three-stage pipeline so that no active content (scripts, remote images,
trackers, embedded objects) ever survives into anything an LLM might read:

    lxml_html_clean.Cleaner  (kill script/style/img/svg/iframe/object/link/meta/base)
        → bleach.clean(strip=True, strip_comments=True)
        → markdownify  (HTML → markdown-ish plain text)

The MIME parser should prefer ``text/plain`` when present (see
:func:`prefer_plain`); HTML conversion is the fallback path.
"""

from __future__ import annotations

import bleach
from lxml_html_clean import Cleaner
from markdownify import MarkdownConverter

# Tags whose entire subtree we drop (spec §4.1). ``kill_tags`` removes the
# element *and its content*, unlike ``remove_tags`` which would unwrap it.
_KILL_TAGS = ("script", "style", "img", "svg", "iframe", "object", "link", "meta", "base")

# A conservative cleaner: strip dangerous constructs, keep document text.
_CLEANER = Cleaner(
    scripts=True,
    javascript=True,
    comments=True,
    style=True,
    inline_style=True,
    links=True,
    meta=True,
    page_structure=False,
    embedded=True,
    frames=True,
    forms=True,
    annoying_tags=True,
    kill_tags=_KILL_TAGS,
    safe_attrs_only=True,
)

# bleach belt-and-suspenders pass: allow only benign structural/text tags so
# anything the lxml cleaner let through is stripped to text.
_BLEACH_ALLOWED_TAGS = [
    "a", "b", "i", "u", "em", "strong", "p", "br", "div", "span",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody",
    "tr", "td", "th", "hr",
]
_BLEACH_ALLOWED_ATTRS = {"a": ["href", "title"]}


class _Converter(MarkdownConverter):
    """markdownify with the options this pipeline needs (MIT; was html2text)."""

    def convert_img(self, el, text, parent_tags):  # images already killed; double-guard
        return ""

    def convert_a(self, el, text, parent_tags):
        href = el.get("href") or ""
        if href.startswith("#"):  # skip internal links, keep the text
            return text
        return super().convert_a(el, text, parent_tags)


def _to_markdown(html: str) -> str:
    return _Converter(
        heading_style="atx",
        bullets="*",
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
        autolinks=False,
    ).convert(html)


def sanitize_html(html: str) -> str:
    """Reduce untrusted HTML to plain text via the spec §4.1 pipeline.

    Returns an empty string for falsy/empty input. Never raises on malformed
    markup — degrades to best-effort text extraction.
    """
    if not html:
        return ""
    try:
        cleaned = _CLEANER.clean_html(html)
    except Exception:
        # Malformed markup: fall back to letting bleach do the stripping.
        cleaned = html
    cleaned = bleach.clean(
        cleaned,
        tags=_BLEACH_ALLOWED_TAGS,
        attributes=_BLEACH_ALLOWED_ATTRS,
        strip=True,
        strip_comments=True,
    )
    text = _to_markdown(cleaned)
    return text.strip()


def prefer_plain(text_plain: str | None, text_html: str | None) -> str:
    """Choose the body text for a MIME message (spec §4 step 1).

    Prefers a non-empty ``text/plain`` part; otherwise converts the HTML part
    through :func:`sanitize_html`. Returns ``""`` if neither is available.
    """
    if text_plain and text_plain.strip():
        return text_plain.strip()
    if text_html and text_html.strip():
        return sanitize_html(text_html)
    return ""
