"""Input + output guardrails (build spec §1, §5, §11).

Wraps ``protectai/llm-guard`` scanners {Secrets, Sensitive, MaliciousURLs,
Toxicity}. ``llm_guard`` is OPTIONAL and not installed by default; when absent
we provide real regex/heuristic fallbacks so the layer keeps working.

Exposes:
  * ``scan_output(text) -> GuardResult`` — run before enqueue AND after edit.
  * ``scan_input(text) -> GuardResult``  — run on untrusted inbound content.

``GuardResult`` is a dataclass: ``passed: bool``, ``flags: dict[str, float]``
(scanner -> risk score), ``reasons: list[str]``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache

from . import presidio, secrets_scan

log = logging.getLogger(__name__)


@dataclass
class GuardResult:
    """Result of an llm-guard-style scan (spec §5)."""

    passed: bool
    flags: dict[str, float] = field(default_factory=dict)  # scanner -> risk
    reasons: list[str] = field(default_factory=list)


# ----- toxicity heuristic (fallback) -----
_TOXIC_TERMS = re.compile(
    r"\b(idiot|moron|stupid|dumb|hate\s+you|kill\s+yourself|kys|"
    r"shut\s+up|loser|pathetic|worthless|scum|disgusting|"
    r"f[\*u]ck|sh[\*i]t|b[\*i]tch|asshole|bastard)\b",
    re.I,
)

# ----- malicious URL heuristic (fallback) -----
_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)
_SUSPICIOUS_URL = re.compile(
    r"(?:xn--|@|//\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"\.(?:zip|mov|tk|gq|ml|cf|ru)\b|bit\.ly|tinyurl|t\.co|"
    r"data:|javascript:|\bphish|\blogin-verify|\baccount-secure)",
    re.I,
)


def _toxicity_score(text: str) -> float:
    if not text:
        return 0.0
    hits = len(_TOXIC_TERMS.findall(text))
    return min(1.0, round(0.5 + 0.25 * hits, 4)) if hits else 0.0


def _malicious_url_score(text: str) -> float:
    urls = _URL_RE.findall(text or "")
    if not urls:
        return 0.0
    bad = sum(1 for u in urls if _SUSPICIOUS_URL.search(u))
    return min(1.0, round(bad / max(len(urls), 1), 4))


def _heuristic_scan(text: str) -> GuardResult:
    """Regex/heuristic stand-in for the four llm-guard scanners."""
    flags: dict[str, float] = {}
    reasons: list[str] = []

    sec = secrets_scan.scan(text)
    flags["Secrets"] = 1.0 if sec else 0.0
    if sec:
        reasons.append(f"secrets detected: {', '.join(sec)}")

    pii = presidio.detect_pii(text)
    flags["Sensitive"] = 1.0 if pii else 0.0
    if pii:
        reasons.append(f"sensitive PII detected: {', '.join(pii)}")

    mal = _malicious_url_score(text)
    flags["MaliciousURLs"] = mal
    if mal > 0:
        reasons.append("suspicious/malicious URL pattern")

    tox = _toxicity_score(text)
    flags["Toxicity"] = tox
    if tox >= 0.5:
        reasons.append("toxic language detected")

    passed = not (sec or pii or mal > 0 or tox >= 0.5)
    return GuardResult(passed=passed, flags=flags, reasons=reasons)


@lru_cache(maxsize=2)
def _load_scanners(kind: str) -> object | None:
    """Load llm-guard scanner list for 'input' or 'output', or None."""
    try:
        if kind == "output":
            from llm_guard.output_scanners import (  # type: ignore
                MaliciousURLs,
                Secrets,
                Sensitive,
                Toxicity,
            )

            return [Secrets(), Sensitive(), MaliciousURLs(), Toxicity()]
        else:
            from llm_guard.input_scanners import (  # type: ignore
                Secrets,
                Toxicity,
            )

            return [Secrets(), Toxicity()]
    except Exception as e:  # llm-guard optional + not installed
        log.info("llm-guard unavailable, using heuristic %s scan: %s", kind, e)
        return None


def _run_llm_guard(text: str, kind: str, prompt: str = "") -> GuardResult:
    scanners = _load_scanners(kind)
    if scanners is None:
        return _heuristic_scan(text)
    flags: dict[str, float] = {}
    reasons: list[str] = []
    ok = True
    try:
        for sc in scanners:  # type: ignore[union-attr]
            name = type(sc).__name__
            if kind == "output":
                _sanitized, valid, score = sc.scan(prompt, text)
            else:
                _sanitized, valid, score = sc.scan(text)
            flags[name] = round(float(score), 4)
            if not valid:
                ok = False
                reasons.append(f"{name} flagged (score={score:.2f})")
        return GuardResult(passed=ok, flags=flags, reasons=reasons)
    except Exception as e:
        log.warning("llm-guard scan failed, falling back: %s", e)
        return _heuristic_scan(text)


def scan_output(text: str, prompt: str = "") -> GuardResult:
    """Scan generated output (a draft body) before enqueue / after edit."""
    return _run_llm_guard(text, "output", prompt)


def scan_input(text: str) -> GuardResult:
    """Scan untrusted inbound content."""
    return _run_llm_guard(text, "input")
