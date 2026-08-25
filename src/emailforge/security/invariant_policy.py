"""Invariant LocalPolicy flow rules (build spec §5, §11).

Encodes the four LocalPolicy rules from §5:

  (a) recipient ∈ {thread participants ∪ contacts ∪ allowlist}
  (b) no draft to a domain outside the link/recipient allowlist
  (c) block send within N seconds of a tool that fetched untrusted content
      unless re-authenticated
  (d) block if ``secrets()`` / ``pii()`` fire on the draft args

``invariant`` (invariantlabs-ai) is OPTIONAL and not installed by default. When
present its ``LocalPolicy`` engine can be used; otherwise the four rules are
implemented directly in Python here. Either way the rules are enforced — this
is a hard gate (unlike Prompt Guard scoring).

Exposes ``PolicyEngine(store, settings)`` with
``evaluate(draft, context) -> PolicyResult``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Default cooldown (seconds) for rule (c) when settings has no override.
_DEFAULT_UNTRUSTED_FETCH_COOLDOWN_S = 30


@dataclass
class PolicyResult:
    """Outcome of the LocalPolicy evaluation (spec §5)."""

    passed: bool
    violations: list[str] = field(default_factory=list)
    rules_checked: list[str] = field(default_factory=list)


def _addr(value: object) -> str:
    return str(value or "").strip().lower()


def _domain(addr: str) -> str:
    return addr.split("@")[-1] if "@" in addr else ""


class PolicyEngine:
    """Enforces the four §5 LocalPolicy rules (hard gate)."""

    def __init__(self, store: object, settings: object):
        self.store = store
        self.settings = settings
        self.cooldown_s = int(
            getattr(settings, "untrusted_fetch_cooldown_s", _DEFAULT_UNTRUSTED_FETCH_COOLDOWN_S)
        )
        # Try the real Invariant engine; fall back to native rules.
        self._localpolicy = self._try_load_invariant()

    @staticmethod
    def _try_load_invariant() -> object | None:
        try:
            from invariant import LocalPolicy  # type: ignore

            return LocalPolicy
        except Exception as e:  # invariant optional + not installed
            log.info("invariant unavailable, using native LocalPolicy rules: %s", e)
            return None

    # ---- helpers backed by the data-access layer ----
    def _allowed_recipients(self, context: dict) -> set[str]:
        allowed: set[str] = set()
        for p in context.get("thread_participants", []) or []:
            allowed.add(_addr(p))
        for c in context.get("contacts", []) or []:
            allowed.add(_addr(c))
        # Auto-built allowlist from the store.
        try:
            is_allow = getattr(self.store, "is_allowlisted", None)
            # Whole-set fetch isn't available; membership tested per-addr below.
            _ = is_allow
        except Exception:
            pass
        return allowed

    def _allowed_domains(self) -> set[str]:
        sec = self.settings
        domains: set[str] = set()
        domains |= {d.lower() for d in getattr(sec, "recipient_allowlist_domains", set()) or set()}
        domains |= {d.lower() for d in getattr(sec, "link_allowlist_domains", set()) or set()}
        try:
            domains |= {d.lower() for d in self.store.allowlisted_domains()}  # type: ignore[attr-defined]
        except Exception as e:
            log.debug("allowlisted_domains lookup failed: %s", e)
        return domains

    def _block_domains(self) -> set[str]:
        return {d.lower() for d in getattr(self.settings, "recipient_block_domains", set()) or set()}

    # ---- main entry ----
    def evaluate(self, draft: object, context: dict | None = None) -> PolicyResult:
        """Evaluate a draft against the four LocalPolicy rules."""
        context = context or {}
        recipient = _addr(getattr(draft, "recipient", None) or (draft.get("recipient") if isinstance(draft, dict) else None))
        body = getattr(draft, "body", None)
        if body is None and isinstance(draft, dict):
            body = draft.get("body")
        body = body or ""

        res = PolicyResult(passed=True)
        res.rules_checked = ["a_recipient_bound", "b_domain_allowlist", "c_untrusted_cooldown", "d_secrets_pii"]

        dom = _domain(recipient)
        block_domains = self._block_domains()
        allowed_domains = self._allowed_domains()

        # --- rule (a): recipient bound to thread/contacts/allowlist ---
        allowed_recipients = self._allowed_recipients(context)
        store_allows = False
        try:
            store_allows = bool(self.store.is_allowlisted(recipient))  # type: ignore[attr-defined]
        except Exception:
            store_allows = False
        if recipient and recipient not in allowed_recipients and not store_allows:
            # A known-good domain still satisfies (a) only via rule (b);
            # (a) is specifically about the *address* being bound.
            res.passed = False
            res.violations.append(
                f"(a) recipient '{recipient}' is not a thread participant, contact, or allowlisted address"
            )
        if not recipient:
            res.passed = False
            res.violations.append("(a) draft has no recipient")

        # --- rule (b): no draft to a domain outside the allowlist ---
        if dom and dom in block_domains:
            res.passed = False
            res.violations.append(f"(b) recipient domain '{dom}' is explicitly blocked")
        elif dom and allowed_domains and dom not in allowed_domains and not store_allows:
            res.passed = False
            res.violations.append(f"(b) recipient domain '{dom}' is outside the link/recipient allowlist")

        # --- rule (c): cooldown after untrusted-content fetch unless re-auth ---
        last_fetch = context.get("last_untrusted_fetch_ts")
        if last_fetch is not None and not context.get("reauthenticated", False):
            now = context.get("now_ts", time.time())
            elapsed = now - float(last_fetch)
            if elapsed < self.cooldown_s:
                res.passed = False
                res.violations.append(
                    f"(c) send blocked: only {elapsed:.0f}s since untrusted-content fetch "
                    f"(< {self.cooldown_s}s) and not re-authenticated"
                )

        # --- rule (d): block if secrets()/pii() fired on the draft args ---
        secrets_found = context.get("secrets_found")
        pii_found = context.get("pii_found")
        # If the orchestrator didn't pre-scan, scan here as defence in depth.
        if secrets_found is None:
            from . import secrets_scan

            secrets_found = secrets_scan.scan(body)
        if pii_found is None:
            from . import presidio

            pii_found = presidio.detect_pii(body, list(getattr(self.settings, "pii_block_categories", []) or []) or None)
        if secrets_found:
            res.passed = False
            res.violations.append(f"(d) secrets present in draft: {', '.join(secrets_found)}")
        if pii_found:
            res.passed = False
            res.violations.append(f"(d) blocked PII present in draft: {', '.join(pii_found)}")

        return res
