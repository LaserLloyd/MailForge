"""Security layer (build spec §0, §4, §5, §11).

This package implements the output guardrails and flow policy that make the
architecture safe (Meta "Agents Rule of Two" class [A]+[B]). The worst case
must stay bounded even if any single detection layer is bypassed, so every
module degrades gracefully (real regex/heuristic fallbacks) when its optional
ML dependency is absent.

Public surface:
  * ``run_output_guardrails(...)`` — the single orchestrator the agent graph
    calls before enqueue AND the UI calls after edit (spec §5).
  * ``score_injection(text)``      — Prompt Guard 2 telemetry score (§5/§11).
  * the individual guard modules    — for targeted use/testing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import (
    crossthread,
    guardrails,
    invariant_policy,
    logging_filter,
    presidio,
    prompt_guard,
    secrets_scan,
    url_check,
)

log = logging.getLogger(__name__)

__all__ = [
    "run_output_guardrails",
    "score_injection",
    "GuardrailReport",
    "install_redaction",
    "crossthread",
    "guardrails",
    "invariant_policy",
    "logging_filter",
    "presidio",
    "prompt_guard",
    "secrets_scan",
    "url_check",
]

# Local URL extractor (avoids importing another module's private regex).
_URL_EXTRACT_RE = re.compile(r"https?://[^\s)>\]}\"']+", re.I)


def _extract_urls(text: str) -> list[str]:
    return _URL_EXTRACT_RE.findall(text or "")


def score_injection(text: str) -> float:
    """Prompt Guard 2 injection-risk score in [0, 1] (telemetry/routing only)."""
    return prompt_guard.score(text)


@dataclass
class GuardrailReport:
    """Combined output-guardrail verdict (spec §5).

    ``passed`` is the AND of every hard gate. ``flags`` records which guards
    fired plus their scores/details, persisted to ``drafts.guardrail_flags``.
    """

    passed: bool
    flags: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def run_output_guardrails(
    draft: object,
    context: dict | None,
    store: object,
    settings: object,
    bridge: object | None = None,
) -> GuardrailReport:
    """Run ALL output guards on a draft and return a combined verdict.

    Runs: detect-secrets, Presidio PII, llm-guard
    (Secrets/Sensitive/MaliciousURLs/Toxicity), URL allowlist, cross-thread
    leak, and Invariant LocalPolicy. Called by the agent graph before enqueue
    AND by the UI after an edit (§5).

    ``draft`` may be a dataclass/row-like object or a dict with at least
    ``recipient``, ``body`` and ``thread_id``. ``context`` is forwarded to the
    LocalPolicy engine (may carry ``thread_participants``,
    ``last_untrusted_fetch_ts``, ``secrets_found``, ``pii_found``, ``urls``).
    """
    context = dict(context or {})

    def _get(name: str, default: object = None) -> object:
        if isinstance(draft, dict):
            return draft.get(name, default)
        return getattr(draft, name, default)

    body = str(_get("body", "") or "")
    thread_id = str(_get("thread_id", "") or "")

    report = GuardrailReport(passed=True, flags={}, reasons=[])

    # --- 1. detect-secrets (independent of llm-guard) ---
    secrets_found = secrets_scan.scan(body)
    report.flags["secrets"] = secrets_found
    if secrets_found:
        report.passed = False
        report.reasons.append(f"secrets in draft: {', '.join(secrets_found)}")

    # --- 2. Presidio PII (honour configured block categories) ---
    block_cats = list(getattr(settings, "pii_block_categories", []) or []) or None
    pii_found = presidio.detect_pii(body, block_cats)
    report.flags["pii"] = pii_found
    if pii_found:
        report.passed = False
        report.reasons.append(f"blocked PII in draft: {', '.join(pii_found)}")

    # --- 3. llm-guard output scanners ---
    gr = guardrails.scan_output(body)
    report.flags["llm_guard"] = gr.flags
    if not gr.passed:
        report.passed = False
        report.reasons.extend(gr.reasons)

    # --- 4. URL allowlist (reject IPs/punycode/non-http(s), strip trackers) ---
    targets = context.get("urls")
    if targets is None:
        targets = _extract_urls(body)
    allow_domains = {d.lower() for d in getattr(settings, "link_allowlist_domains", set()) or set()}
    try:
        allow_domains |= {d.lower() for d in store.allowlisted_domains()}  # type: ignore[attr-defined]
    except Exception:
        pass
    ur = url_check.check_urls(list(targets), allow_domains)
    report.flags["urls"] = {"blocked": ur.blocked, "stripped": ur.stripped}
    if not ur.passed:
        report.passed = False
        report.reasons.extend(f"url blocked: {b}" for b in ur.blocked)

    # --- 5. cross-thread leak guard ---
    try:
        ctg = crossthread.CrossThreadGuard(store, settings)
        # Embedding-sim needs a vector; the (async) graph pre-computes it and
        # passes it via context['draft_embedding']. bridge is forwarded only as
        # a sync-embed fallback (async embedders are ignored inside check()).
        ct = ctg.check(
            body, thread_id, bridge=bridge,
            draft_embedding=(context or {}).get("draft_embedding"),
            site_id=(context or {}).get("site_id"),
        )
        report.flags["crossthread"] = {
            "leaked_thread_ids": ct.leaked_thread_ids,
            "ngrams": ct.leaked_ngrams,
            **ct.flags,
        }
        if not ct.passed:
            report.passed = False
            report.reasons.extend(ct.reasons)
    except Exception as e:  # never let a guard crash the gate open silently
        log.warning("cross-thread guard error (treated as fail): %s", e)
        report.passed = False
        report.reasons.append(f"cross-thread guard error: {e}")

    # --- 6. Invariant LocalPolicy (hard flow rules a-d) ---
    # Feed the scans we already ran so rule (d) is consistent and cheap.
    context.setdefault("secrets_found", secrets_found)
    context.setdefault("pii_found", pii_found)
    pe = invariant_policy.PolicyEngine(store, settings)
    pr = pe.evaluate(draft, context)
    report.flags["policy"] = {"violations": pr.violations, "rules": pr.rules_checked}
    if not pr.passed:
        report.passed = False
        report.reasons.extend(pr.violations)

    # --- telemetry: injection score on the draft (routing only, not a gate) ---
    report.flags["injection_score"] = prompt_guard.score(body)

    return report


# Convenience re-export so callers can install log redaction up front.
install_redaction = logging_filter.install_redaction
