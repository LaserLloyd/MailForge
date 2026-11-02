"""Email signatures: per-mailbox with a global fallback.

A mailbox's own signature (``[[imap_accounts]] signature``) wins; otherwise the
global one (``[compose] signature``, then ``[style] signature``) is used. The
text MailForge inserts is always a ``-- `` delimiter block at the very end, so
a later change of mailbox can swap exactly that block and never touch the
user's own words.
"""

from __future__ import annotations

from typing import Any

DELIM = "-- "


def global_signature(settings: Any) -> str:
    for section in ("compose", "style"):
        value = str(getattr(getattr(settings, section, None), "signature", "") or "").strip()
        if value:
            return value
    return ""


def signature_for(settings: Any, mailbox: str | None) -> str:
    """Signature for ``mailbox`` (an account username or name), else the global."""
    wanted = str(mailbox or "").strip().lower()
    if wanted:
        for acct in getattr(settings, "imap_accounts", None) or []:
            names = {str(getattr(acct, "username", "") or "").lower(),
                     str(getattr(acct, "name", "") or "").lower()}
            if wanted in names:
                own = str(getattr(acct, "signature", "") or "").strip()
                if own:
                    return own
                break
    return global_signature(settings)


def block(signature: str) -> str:
    return f"\n\n{DELIM}\n{signature.strip()}"


def has_signature(body: str, signature: str) -> bool:
    sig = (signature or "").strip()
    return bool(sig) and (body or "").rstrip().endswith(sig)


def append_signature(body: str, signature: str) -> str:
    """Add the signature block at the bottom unless the body already ends with it."""
    sig = (signature or "").strip()
    if not sig or has_signature(body, sig):
        return body
    return (body or "").rstrip() + block(sig) if (body or "").strip() else block(sig)


def swap_signature(body: str, old: str, new: str) -> str:
    """Replace the block MailForge inserted (``old``) with ``new``.

    Only an untouched block at the end of the body is replaced; if the user
    edited it, or there never was one, the text is left exactly as it is. An
    empty body simply receives ``new``.
    """
    if not (body or "").strip():
        return append_signature("", new)
    old = (old or "").strip()
    text = body.rstrip()
    if old and text.endswith(block(old)):
        head = text[: -len(block(old))]
        return head + block(new) if (new or "").strip() else head
    return body
