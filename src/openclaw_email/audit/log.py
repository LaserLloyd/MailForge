"""Chained-hash append-only audit writer (build spec §7, §8, §11).

Every consequential event (LLM call, tool call, approval, send, block, error)
is appended to the ``audit_log`` table with a SHA-256 hash chain:

    this_hash = sha256(prev_hash || ts || actor || event ||
                       subject_table || subject_id || detail_json)

``detail_json`` is REDACTED before write (spec §0.5, §7, §11): every string
value in the ``detail`` dict is passed through Presidio PII redaction and a
secret scrub so no secret or PII is ever persisted to the log. The chain lets
``verify_chain`` (and the ``openclaw-email audit-verify`` CLI command) detect
any tampering or row deletion after the fact.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..security import presidio, secrets_scan
from ..security.logging_filter import redact as _filter_redact

log = logging.getLogger(__name__)

# Field separator used when concatenating the hashed columns. A NUL byte cannot
# appear in the JSON/text fields, so it is an unambiguous delimiter that stops a
# crafted value from spoofing a field boundary.
_SEP = "\x00"


def _now_iso() -> str:
    """ISO-8601 UTC timestamp (spec §7 expects ISO-UTC ``ts``)."""
    return datetime.now(timezone.utc).isoformat()


def _redact_value(value: Any) -> Any:
    """Redact a single detail value, recursing into containers.

    Strings are scrubbed for PII (Presidio) and secrets (detect-secrets). The
    self-contained logging-filter redactor runs first as a fast, dependency-free
    backstop; Presidio then catches anything model-based detection adds.
    """
    if isinstance(value, str):
        scrubbed = _filter_redact(value)
        scrubbed = presidio.redact(scrubbed)
        # If detect-secrets still flags something the regex layers missed,
        # neutralise the whole value rather than risk leaking it.
        if secrets_scan.scan(scrubbed):
            return "<REDACTED_SECRET>"
        return scrubbed
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def _redact_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Return a redacted copy of ``detail`` safe to persist (spec §0.5/§7)."""
    if not detail:
        return {}
    return {k: _redact_value(v) for k, v in detail.items()}


def compute_hash(
    prev_hash: str | None,
    ts: str,
    actor: str,
    event: str,
    subject_table: str | None,
    subject_id: int | None,
    detail_json: str,
) -> str:
    """SHA-256 over the canonical concatenation of the chained fields."""
    parts = [
        prev_hash or "",
        ts,
        actor,
        event,
        subject_table or "",
        "" if subject_id is None else str(subject_id),
        detail_json,
    ]
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only, chained-hash audit writer over a ``Store`` (spec §7/§8)."""

    def __init__(self, store: Any):
        self.store = store

    def record(
        self,
        actor: str,
        event: str,
        subject_table: str | None = None,
        subject_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        """Append one audit entry and return its ``this_hash``.

        ``actor`` ∈ {agent, user, guardrail}; ``event`` ∈
        {llm_call, tool_call, approval, send, block, error}. ``detail`` is
        redacted (PII + secrets) before it is serialised and written.
        """
        ts = _now_iso()
        safe_detail = _redact_detail(detail)
        detail_json = json.dumps(safe_detail, sort_keys=True, ensure_ascii=False)
        prev_hash = self.store.last_audit_hash()
        this_hash = compute_hash(
            prev_hash, ts, actor, event, subject_table, subject_id, detail_json
        )
        self.store.append_audit(
            ts=ts,
            actor=actor,
            event=event,
            subject_table=subject_table,
            subject_id=subject_id,
            detail_json=detail_json,
            prev_hash=prev_hash,
            this_hash=this_hash,
        )
        return this_hash

    # ----- convenience wrappers (one per spec §7 event vocabulary) -----
    def llm_call(
        self,
        detail: dict[str, Any] | None = None,
        *,
        actor: str = "agent",
        subject_table: str | None = None,
        subject_id: int | None = None,
    ) -> str:
        """Record an ``llm_call`` event."""
        return self.record(actor, "llm_call", subject_table, subject_id, detail)

    def tool_call(
        self,
        detail: dict[str, Any] | None = None,
        *,
        actor: str = "agent",
        subject_table: str | None = None,
        subject_id: int | None = None,
    ) -> str:
        """Record a ``tool_call`` event."""
        return self.record(actor, "tool_call", subject_table, subject_id, detail)

    def approval(
        self,
        detail: dict[str, Any] | None = None,
        *,
        actor: str = "user",
        subject_table: str | None = "drafts",
        subject_id: int | None = None,
    ) -> str:
        """Record a human ``approval`` event (spec §0.1 send-gate evidence)."""
        return self.record(actor, "approval", subject_table, subject_id, detail)

    def send(
        self,
        detail: dict[str, Any] | None = None,
        *,
        actor: str = "agent",
        subject_table: str | None = "drafts",
        subject_id: int | None = None,
    ) -> str:
        """Record a ``send`` event (SMTP fired after human approval)."""
        return self.record(actor, "send", subject_table, subject_id, detail)

    def block(
        self,
        detail: dict[str, Any] | None = None,
        *,
        actor: str = "guardrail",
        subject_table: str | None = "drafts",
        subject_id: int | None = None,
    ) -> str:
        """Record a ``block`` event (a guardrail rejected a draft)."""
        return self.record(actor, "block", subject_table, subject_id, detail)

    def error(
        self,
        detail: dict[str, Any] | None = None,
        *,
        actor: str = "agent",
        subject_table: str | None = None,
        subject_id: int | None = None,
    ) -> str:
        """Record an ``error`` event."""
        return self.record(actor, "error", subject_table, subject_id, detail)


def verify_chain(db_path: str | Path) -> tuple[bool, int, int | None]:
    """Recompute the hash chain over the audit log and verify integrity.

    Backs ``openclaw-email audit-verify`` (spec §8/§11). Returns
    ``(ok, n, bad_index)`` where ``n`` is the number of entries examined and
    ``bad_index`` is the 0-based position of the first entry whose stored
    ``this_hash`` does not match the recomputation, or that fails to link to the
    previous entry's ``this_hash`` (``None`` when ``ok`` is True).
    """
    from ..db.store import Store

    store = Store(db_path)
    try:
        rows = store.iter_audit()
    finally:
        store.close()

    prev_hash: str | None = None
    for index, row in enumerate(rows):
        # Link check: this row's prev_hash must equal the prior row's this_hash.
        if (row["prev_hash"] or None) != (prev_hash or None):
            return (False, len(rows), index)
        recomputed = compute_hash(
            prev_hash,
            row["ts"],
            row["actor"],
            row["event"],
            row["subject_table"],
            row["subject_id"],
            row["detail_json"],
        )
        if recomputed != row["this_hash"]:
            return (False, len(rows), index)
        prev_hash = row["this_hash"]

    return (True, len(rows), None)
