"""PII detection & redaction (build spec §1, §5, §8, §11).

Wraps ``microsoft/presidio`` (analyzer + anonymizer). Both packages are
OPTIONAL and not installed by default; when absent we fall back to a real regex
detector covering SSN, credit cards (Luhn-validated), IBAN, email and phone
numbers.

Exposes:
  * ``detect_pii(text, categories) -> list[str]`` — entity types found,
    optionally filtered to ``categories`` (honours ``pii_block_categories``).
  * ``redact(text) -> str`` — text with detected PII replaced by ``<TYPE>``.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

log = logging.getLogger(__name__)

# Presidio-compatible entity names so the rest of the system speaks one vocab.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_SSN_RE = re.compile(r"\b(?!000|666|9\d\d)\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"
)
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(nums)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(iban: str) -> bool:
    s = iban.replace(" ", "").upper()
    rearranged = s[4:] + s[:4]
    converted = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    try:
        return int(converted) % 97 == 1
    except ValueError:
        return False


def _heuristic_detect(text: str) -> dict[str, list[tuple[int, int]]]:
    """Return entity_type -> list of (start, end) spans (fallback)."""
    spans: dict[str, list[tuple[int, int]]] = {}
    if not text:
        return spans

    def add(name: str, m: re.Match[str]) -> None:
        spans.setdefault(name, []).append((m.start(), m.end()))

    for m in _SSN_RE.finditer(text):
        add("US_SSN", m)
    for m in _CC_RE.finditer(text):
        if _luhn_ok(m.group()):
            add("CREDIT_CARD", m)
    for m in _IBAN_RE.finditer(text):
        if _iban_ok(m.group()):
            add("IBAN_CODE", m)
    for m in _EMAIL_RE.finditer(text):
        add("EMAIL_ADDRESS", m)
    for m in _PHONE_RE.finditer(text):
        add("PHONE_NUMBER", m)
    return spans


@lru_cache(maxsize=1)
def _load_analyzer() -> object | None:
    try:
        from presidio_analyzer import AnalyzerEngine  # type: ignore
    except Exception as e:  # presidio optional + not installed
        log.info("Presidio unavailable, using regex PII fallback: %s", e)
        return None
    try:
        return AnalyzerEngine()
    except Exception as e:  # spaCy model missing
        log.info("Presidio analyzer init failed, using regex fallback: %s", e)
        return None


def detect_pii(text: str, categories: list[str] | None = None) -> list[str]:
    """Return PII entity types present in ``text``.

    If ``categories`` is given, only those types are returned (used with
    ``SecuritySettings.pii_block_categories``). Empty list => no PII.
    """
    analyzer = _load_analyzer()
    if analyzer is None:
        types = set(_heuristic_detect(text).keys())
    else:
        try:
            results = analyzer.analyze(text=text, language="en", entities=categories or None)
            types = {r.entity_type for r in results}
        except Exception as e:
            log.warning("Presidio analyze failed, falling back: %s", e)
            types = set(_heuristic_detect(text).keys())
    if categories:
        wanted = set(categories)
        types = {t for t in types if t in wanted}
    return sorted(types)


def redact(text: str, categories: list[str] | None = None) -> str:
    """Replace detected PII with ``<ENTITY_TYPE>`` placeholders."""
    if not text:
        return text
    analyzer = _load_analyzer()
    if analyzer is not None:
        try:
            from presidio_anonymizer import AnonymizerEngine  # type: ignore

            results = analyzer.analyze(text=text, language="en", entities=categories or None)
            if results:
                return AnonymizerEngine().anonymize(text=text, analyzer_results=results).text
            return text
        except Exception as e:
            log.warning("Presidio anonymize failed, falling back: %s", e)
    # Fallback: redact spans right-to-left so indices stay valid.
    spans = _heuristic_detect(text)
    flat: list[tuple[int, int, str]] = []
    for name, occ in spans.items():
        if categories and name not in set(categories):
            continue
        for start, end in occ:
            flat.append((start, end, name))
    out = text
    for start, end, name in sorted(flat, key=lambda x: x[0], reverse=True):
        out = out[:start] + f"<{name}>" + out[end:]
    return out
