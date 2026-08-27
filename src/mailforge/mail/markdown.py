"""Sanitized Markdown rendering for previews and multipart email."""

from __future__ import annotations

import bleach
import markdown2

_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_ATTRS = {"a": ["href", "title", "rel"]}


def markdown_to_safe_html(text: str, *, allow_links: bool = True) -> str:
    """Render Markdown without remote media, scripts, styles, or unsafe schemes.

    Inbound email passes ``allow_links=False``: anchors are stripped entirely,
    enforcing the direct-site verification rule in the rendered UI as well as
    in policy text. Trusted local drafts/knowledge may retain sanitized links.
    """
    rendered = markdown2.markdown(
        str(text or ""),
        extras=["break-on-newline", "fenced-code-blocks", "strike", "tables"],
    )
    tags = _TAGS if allow_links else (_TAGS - {"a"})
    attrs = _ATTRS if allow_links else {}
    cleaned = bleach.clean(
        rendered,
        tags=tags,
        attributes=attrs,
        protocols={"http", "https", "mailto"},
        strip=True,
    )
    if not allow_links:
        return cleaned
    return bleach.linkify(
        cleaned,
        callbacks=[bleach.callbacks.nofollow, bleach.callbacks.target_blank],
        skip_tags={"pre", "code"},
    )
