"""Redaction logging filter (build spec §0.5, §11).

A ``logging.Filter`` that scrubs secrets and PII from log records **before the
formatter sees them** (spec §0.5: "Logs are redacted before the formatter").
Both the rendered message and the positional args are redacted, so neither a
pre-formatted string nor lazy ``%s`` args can leak.

Exposes ``RedactionFilter`` and ``install_redaction(logger=None)``.
"""

from __future__ import annotations

import logging
import re

# Self-contained patterns (filter must not import heavy deps and must work even
# if other security modules fail). Mirrors secrets_scan/presidio coverage.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # secrets
    (re.compile(r"-----BEGIN[\s\S]*?PRIVATE KEY-----"), "<PRIVATE_KEY>"),
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b"), "<AWS_KEY>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "<GITHUB_TOKEN>"),
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), "<SLACK_TOKEN>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"), "<JWT>"),
    (re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{16,}"), "Bearer <TOKEN>"),
    (re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token)(\s*[:=]\s*)['\"]?[A-Za-z0-9/+_\-]{6,}"), r"\1\2<REDACTED>"),
    # PII
    (re.compile(r"\b(?!000|666|9\d\d)\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b"), "<US_SSN>"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "<CARD_NUMBER>"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "<EMAIL>"),
    (re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"), "<PHONE>"),
]


def redact(text: str) -> str:
    """Apply all redaction patterns to ``text``."""
    out = text
    for pat, repl in _REDACTIONS:
        out = pat.sub(repl, out)
    return out


class RedactionFilter(logging.Filter):
    """Scrubs secrets/PII from a record before formatting (spec §0.5)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: redact(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:
            # Never let redaction failure suppress (or crash) logging; on error
            # drop args to a safe placeholder rather than risk a leak.
            record.args = None
            record.msg = "<REDACTION_ERROR: message suppressed>"
        return True


def install_redaction(logger: logging.Logger | None = None) -> RedactionFilter:
    """Attach a ``RedactionFilter`` to ``logger`` (root if None).

    Also attaches to any existing handlers so already-configured handlers get
    redacted records too.
    """
    target = logger or logging.getLogger()
    filt = RedactionFilter()
    target.addFilter(filt)
    for handler in target.handlers:
        handler.addFilter(filt)
    return filt
