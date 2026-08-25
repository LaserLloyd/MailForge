"""Human-feedback draft revision service; proposes text and never sends mail."""

from __future__ import annotations

from typing import Any

from ..security import run_output_guardrails
from . import worker


async def _is_up(bridge) -> bool:
    """Non-blocking LM Studio probe: the sync ``is_up()`` is a 5 s HTTP call
    and these run inside the UI's event loop."""
    fn = getattr(bridge, "is_up_async", None)
    if fn is not None:
        return bool(await fn())
    import asyncio

    return bool(await asyncio.to_thread(bridge.is_up))


async def revise_draft(
    store: Any,
    settings: Any,
    bridge: Any,
    draft_id: int,
    feedback: str,
    *,
    site_id: str,
) -> dict[str, Any]:
    draft = store.get_draft_for_site(int(draft_id), site_id)
    if draft is None:
        raise ValueError("draft not found for site")
    if draft["message_id"] is None:
        raise ValueError("AI revision requires an inbound-message draft")
    if store.is_quarantined(int(draft["message_id"])):
        raise ValueError(
            "source message is quarantined — release it in the local UI first"
        )
    source = store.get_message(int(draft["message_id"]))
    if source is not None and str(source["screening_status"] or "").upper() in {
        "POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"
    }:
        raise ValueError(
            "source message is withheld by inbound screening — mark it content-related first"
        )
    if draft["state"] not in {"DRAFT", "PENDING", "BLOCKED", "DEFERRED_NO_LLM"}:
        raise ValueError(f"draft state {draft['state']} is immutable")
    feedback = str(feedback or "").strip()
    if not feedback:
        raise ValueError("feedback is required")
    if len(feedback) > 10_000:
        raise ValueError("feedback is too long")
    if bridge is None or not await _is_up(bridge):
        raise RuntimeError("model is unavailable")

    store.create_ai_revision(
        draft_id, feedback=feedback, body=draft["body"] or "", model_used="user", role="user"
    )
    from ..rag.retriever import retrieve

    context = await retrieve(
        store,
        bridge,
        query=f"{draft['subject'] or ''} {feedback}",
        k=5,
        site_id=site_id,
        reference_only=True,
    )
    proposed = await worker.revise_reply(
        bridge,
        draft["subject"] or "",
        draft["body"] or "",
        feedback,
        context,
        draft["recipient"],
        site_id=site_id,
        site_rule=getattr(settings, "site_guidance", lambda _s: None)(site_id),
    )
    body = (proposed.body or "").strip()
    if not body:
        raise RuntimeError("model returned an empty revision")
    subject = proposed.subject or draft["subject"] or ""
    thread = store.thread_messages(draft["thread_id"])
    participants: set[str] = set()
    for message in thread:
        for column in ("from_addr", "to_addrs", "cc_addrs"):
            participants.update(
                a.strip().lower()
                for a in str(message[column] or "").split(",")
                if a.strip()
            )
    report = run_output_guardrails(
        {
            "recipient": draft["recipient"],
            "subject": subject,
            "body": body,
            "thread_id": draft["thread_id"],
        },
        {"thread_participants": participants, "site_id": site_id},
        store,
        settings.security,
        bridge,
    )
    state = "PENDING" if report.passed else "BLOCKED"
    flags = {**report.flags, "reasons": report.reasons, "revision": True}
    store.update_draft_state(
        draft_id, state, body=body, subject=subject, guardrail_flags=flags
    )
    store.create_ai_revision(
        draft_id,
        feedback=feedback,
        body=body,
        model_used=getattr(bridge, "model_id", settings.llm.chat_model),
        role="assistant",
    )
    return {
        "draft_id": int(draft_id),
        "state": state,
        "subject": subject,
        # A guardrail-failing body is never echoed back to the caller — it may
        # contain the very secrets/PII the guard just flagged. Humans can still
        # inspect the BLOCKED draft in the local UI.
        "body": body if report.passed else "",
        "guardrail_passed": bool(report.passed),
        "guardrail_reasons": report.reasons,
    }
