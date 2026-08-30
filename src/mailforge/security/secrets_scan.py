"""Secret scanning (build spec §1, §5, §11).

Wraps ``Yelp/detect-secrets`` independently of llm-guard so the two layers
provide defence in depth. ``detect_secrets`` is OPTIONAL and not installed by
default; when absent we fall back to a real regex/entropy detector covering
AWS keys, private keys, bearer tokens and high-entropy tokens.

Exposes ``scan(text) -> list[str]`` returning the secret *types* found.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

log = logging.getLogger(__name__)


# ----- regex fallback patterns: (type_name, regex) -----
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWSKeyDetector", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")),
    ("PrivateKeyDetector", re.compile(r"-----BEGIN\s+(?:RSA|EC|DSA|OPENSSH|PGP)?\s*PRIVATE KEY-----")),
    ("GitHubTokenDetector", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("SlackTokenDetector", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("StripeKeyDetector", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("GoogleApiKeyDetector", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("JwtTokenDetector", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
    ("BearerTokenDetector", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{16,}")),
    ("BasicAuthDetector", re.compile(r"https?://[^/\s:@]+:[^/\s:@]+@", re.I)),
    ("KeywordDetector", re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{8,}")),
]

# Tokens that look like long opaque strings get an entropy check.
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/=_\-]{20,}\b")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _heuristic_scan(text: str) -> list[str]:
    """Deterministic regex + entropy secret detector (fallback)."""
    found: set[str] = set()
    if not text:
        return []
    for name, pat in _PATTERNS:
        if pat.search(text):
            found.add(name)
    # High-entropy generic tokens (catch-all for opaque credentials).
    for tok in _TOKEN_RE.findall(text):
        if len(tok) >= 24 and _shannon_entropy(tok) >= 4.0:
            found.add("HighEntropyString")
    return sorted(found)


def scan(text: str) -> list[str]:
    """Return the list of secret *types* detected in ``text``.

    Uses ``detect-secrets`` when installed, otherwise the regex/entropy
    fallback. An empty list means no secrets were detected.
    """
    try:
        from detect_secrets import SecretsCollection  # type: ignore
        from detect_secrets.settings import default_settings  # type: ignore
    except Exception as e:  # detect-secrets optional + not installed
        log.debug("detect-secrets unavailable, using heuristic scan: %s", e)
        return _heuristic_scan(text)
    try:
        import os
        import tempfile

        types: set[str] = set()
        # delete=False + explicit close: Windows refuses a second open of a
        # still-open NamedTemporaryFile, and scan_file() reopens by name.
        # encoding is explicit because email text is not ASCII and Windows
        # would otherwise write it as cp1252.
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        )
        try:
            with handle as fh:
                fh.write(text)
            with default_settings():
                coll = SecretsCollection()
                coll.scan_file(handle.name)
                for _file, secret in coll:
                    types.add(secret.type)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        # Union with heuristic so defence-in-depth never regresses.
        return sorted(types | set(_heuristic_scan(text)))
    except Exception as e:
        log.warning("detect-secrets scan failed, falling back: %s", e)
        return _heuristic_scan(text)
