"""Prompt-injection risk scorer (build spec §1, §5, §11).

Wraps ``meta-llama/Llama-Prompt-Guard-2-22M`` via ``transformers`` to produce
an injection-risk score in ``[0, 1]``.

IMPORTANT (spec §0/§5/§11): this score is **telemetry/routing only — NOT a hard
gate**. The architecture must stay safe even if this layer is bypassed, so the
score is logged and used for routing, never as the sole decision.

``transformers``/``torch`` are OPTIONAL and not installed by default. When they
are absent we fall back to a deterministic heuristic scorer built from regex
patterns for well-known injection phrases. The fallback is a *real* detector
(not a no-op) so graceful degradation keeps the system useful.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

log = logging.getLogger(__name__)

_MODEL_ID = "meta-llama/Llama-Prompt-Guard-2-22M"

# ----- heuristic fallback patterns -----
# Each (regex, weight) pair contributes to the heuristic score. Weights are
# tuned so a single strong phrase ("ignore previous instructions") already
# clears the 0.5 telemetry line, and stacking phrases approaches 1.0.
_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"ignore\s+(all\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|messages?|context)", re.I), 0.6),
    (re.compile(r"disregard\s+(all\s+|the\s+)?(previous|prior|above|earlier|your)\s+\w+", re.I), 0.55),
    (re.compile(r"forget\s+(everything|all|your|the)\s+\w+", re.I), 0.45),
    (re.compile(r"\b(system\s*prompt|system\s*message|developer\s*message)\b", re.I), 0.4),
    (re.compile(r"\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be|roleplay\s+as)\b", re.I), 0.35),
    (re.compile(r"\b(jailbreak|DAN\s+mode|do\s+anything\s+now)\b", re.I), 0.6),
    (re.compile(r"\b(reveal|print|show|leak|exfiltrate|disclose)\s+(your|the)\s+(system\s+)?(prompt|instructions?|rules?|api[_\s]?key|secret|password|token)", re.I), 0.6),
    (re.compile(r"new\s+(instructions?|rules?|task)\s*:", re.I), 0.4),
    (re.compile(r"\boverride\s+(your|the|all|previous)\s+\w+", re.I), 0.45),
    (re.compile(r"\b(send|email|forward|cc|bcc)\s+(this|the\s+\w+|everything|all)\s+to\b", re.I), 0.4),  # tool-call lure
    (re.compile(r"<\s*/?\s*(system|im_start|im_end|s)\s*>", re.I), 0.4),  # delimiter injection
    (re.compile(r"\bend\s+of\s+(prompt|instructions?|context)\b", re.I), 0.35),
    (re.compile(r"\b(execute|run|invoke|call)\s+(the\s+)?(tool|function|command|shell)\b", re.I), 0.35),
    (re.compile(r"```[\s\S]*?(system|assistant|user)\s*:", re.I), 0.3),
]


def _heuristic_score(text: str) -> float:
    """Deterministic regex/keyword injection-risk score in [0, 1]."""
    if not text:
        return 0.0
    # Combine weights probabilistically: each match reduces the "clean"
    # probability. score = 1 - product(1 - w) over matches. This saturates
    # toward 1.0 as more independent signals fire, without ever exceeding it.
    clean = 1.0
    for pat, weight in _PATTERNS:
        if pat.search(text):
            clean *= 1.0 - weight
    return round(1.0 - clean, 4)


@lru_cache(maxsize=1)
def _load_model() -> object | None:
    """Lazily load the Prompt Guard 2 pipeline, or None if unavailable."""
    try:
        import torch  # noqa: F401
        from transformers import (  # type: ignore
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )
    except Exception as e:  # transformers/torch optional + not installed
        log.info("Prompt Guard model unavailable, using heuristic fallback: %s", e)
        return None
    try:
        tok = AutoTokenizer.from_pretrained(_MODEL_ID)
        model = AutoModelForSequenceClassification.from_pretrained(_MODEL_ID)
        model.eval()
        return (tok, model)
    except Exception as e:  # weights not downloaded / offline
        log.info("Prompt Guard weights unavailable, using heuristic fallback: %s", e)
        return None


def score(text: str) -> float:
    """Return an injection-risk score in ``[0, 1]`` for ``text``.

    Telemetry/routing only (spec §5/§11) — never used as a sole hard gate.
    Uses Prompt Guard 2-22M when available, otherwise the deterministic
    heuristic fallback.
    """
    loaded = _load_model()
    if loaded is None:
        return _heuristic_score(text)
    try:
        import torch

        tok, model = loaded  # type: ignore[misc]
        inputs = tok(text, return_tensors="pt", truncation=True, max_length=512)
        with torch.no_grad():
            logits = model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)[0]
        # Label 1 = injection/jailbreak for Prompt Guard 2.
        return round(float(probs[-1].item()), 4)
    except Exception as e:  # any runtime failure → safe heuristic
        log.warning("Prompt Guard inference failed, falling back: %s", e)
        return _heuristic_score(text)
