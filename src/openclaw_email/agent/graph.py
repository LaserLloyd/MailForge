"""Deterministic agent state machine (build spec §5).

LLM calls happen only at named nodes. The planner sees policy + symbolic refs
only; workers are quarantined (typed outputs, no tools). The graph never sends
mail — it only produces ``drafts`` rows in PENDING / BLOCKED / DEFERRED_NO_LLM.
The send path lives behind a human click in the UI.

Flow (per §5):
    fetch → (sanitize/normalize done at ingest) → spotlight_wrap
          → detect(PromptGuard score, telemetry only)
          → plan_freeze(planner; policy + symbolic refs only)
          → classify(worker → Category)
          → retrieve_context(thread-scoped)
          → draft(worker → body; recipient BOUND to thread participants)
          → output_guardrails → PASS:enqueue PENDING | FAIL:BLOCKED
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from ..audit.log import AuditLog
from ..config import Settings
from ..db.store import Store
from ..mail.normalize import spotlight_wrap
from ..security import run_output_guardrails, score_injection
from . import tools, worker
from .planner import SymbolicRefs, freeze_plan

log = logging.getLogger(__name__)

# Categories that warrant a drafted reply.
_REPLYABLE = {"RESPOND", "MEETING"}


async def _is_up(bridge) -> bool:
    """Non-blocking LM Studio probe: the sync ``is_up()`` is a 5 s HTTP call
    and these run inside the UI's event loop."""
    fn = getattr(bridge, "is_up_async", None)
    if fn is not None:
        return bool(await fn())
    import asyncio

    return bool(await asyncio.to_thread(bridge.is_up))


class AgentGraph:
    def __init__(self, store: Store, settings: Settings, bridge=None):
        self.store = store
        self.settings = settings
        self.bridge = bridge
        self.audit = AuditLog(store)

    # ----- recipient binding (§0.4) -----
    def _thread_participants(self, thread_id: str) -> set[str]:
        parts: set[str] = set()
        for m in self.store.thread_messages(thread_id):
            for col in ("from_addr", "to_addrs", "cc_addrs"):
                v = m[col]
                if v:
                    parts |= {a.strip().lower() for a in str(v).split(",") if a.strip()}
        return parts

    def _bind_recipient(self, msg, thread_id: str) -> str | None:
        """A draft recipient MUST come from thread participants / contacts /
        allowlist (§0.4). Default reply target is the inbound sender; we accept
        it only if it is a bound participant or allowlisted."""
        candidate = (msg["from_addr"] or "").strip().lower()
        if not candidate:
            return None
        participants = self._thread_participants(thread_id)
        if candidate in participants:
            # Persist the exact observed sender. This satisfies the downstream
            # domain policy without weakening recipient binding: unrelated
            # addresses at the same domain still fail rule (a), and the UI
            # still requires retyping a first-contact address before send.
            if not self.store.is_allowlisted(candidate):
                self.store.record_allowlist(candidate, source="inbound_thread")
            return candidate
        if self.store.is_allowlisted(candidate):
            return candidate
        return None

    async def process_message(self, message_id: int) -> int | None:
        """Run the full graph for one ingested message. Returns the draft id
        (or None if no draft was produced)."""
        msg = self.store.get_message(message_id)
        if msg is None:
            return None
        account = self.store.get_account_for_message(message_id)
        if account is None or account["site_id"] not in self.settings.sites:
            log.error("Message %s has no valid site-bound source account; refusing AI work.", message_id)
            return None

        # --- quarantine gate: contained mail never reaches any LLM ---
        try:
            quarantined = bool(msg["quarantined"])
        except (IndexError, KeyError):
            quarantined = False
        if quarantined:
            self.audit.block(
                {"reason": "quarantined", "detail": str(msg["quarantine_reason"] or "")},
                subject_table="messages", subject_id=message_id,
            )
            log.warning(
                "Message %s is QUARANTINED — refusing all AI work until a human "
                "releases it in the UI.", message_id,
            )
            return None

        # Cynical site screening is a policy gate, separate from prompt-
        # injection quarantine. Questionable/spam mail remains available only
        # to the operator's sanitized local reader until it is marked content-related.
        screening_status = str(msg["screening_status"] or "UNSCREENED").upper()
        if screening_status in {"POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"}:
            self.audit.block(
                {
                    "reason": "inbound_screening",
                    "status": screening_status,
                    "detail": str(msg["screening_reason"] or "")[:300],
                },
                subject_table="messages", subject_id=message_id,
            )
            log.info(
                "Message %s screened %s — refusing AI work until human review.",
                message_id, screening_status,
            )
            return None

        site_id = account["site_id"]
        thread_id = msg["thread_id"]
        text = msg["sanitized_text"] or ""

        # --- spotlight wrap (untrusted content is DATA, never instructions) ---
        spotlighted = spotlight_wrap(text, str(message_id))

        # --- detect: Prompt Guard 2 score (telemetry/routing, NOT a gate) ---
        injection_risk = score_injection(text)
        self.audit.llm_call(
            {"node": "detect", "injection_risk": injection_risk},
            subject_table="messages", subject_id=message_id,
        )

        # --- LLM down? defer, never lose mail (§5) ---
        if self.bridge is None or not await _is_up(self.bridge):
            recipient = self._bind_recipient(msg, thread_id) or (msg["from_addr"] or "")
            did = tools.propose_draft(
                self.store, message_id, thread_id, recipient,
                subject=f"Re: {msg['subject'] or ''}", body="",
                state="DEFERRED_NO_LLM",
                guardrail_flags={"injection_risk": injection_risk},
            )
            self.audit.record("agent", "block", "drafts", did, {"reason": "DEFERRED_NO_LLM"})
            log.info("LM Studio down — message %s deferred (draft %s).", message_id, did)
            return did

        # --- plan_freeze: planner sees policy + symbolic refs ONLY ---
        refs = SymbolicRefs(
            sender_domain=(msg["from_addr"] or "@").split("@")[-1].lower(),
            thread_known=len(self.store.thread_messages(thread_id)) > 1,
            link_count=msg["link_count"] or 0,
            has_attachments=bool(msg["has_attachments"]),
        )
        plan = await freeze_plan(self.bridge, refs, self.settings)
        self.audit.llm_call(
            {"node": "plan_freeze", **asdict(plan)},
            subject_table="messages", subject_id=message_id,
        )

        # --- classify (quarantined worker → typed Category) ---
        meta = (
            f"sender_domain={refs.sender_domain}; links={refs.link_count}; "
            f"attachments={refs.has_attachments}; injection_risk={injection_risk:.2f}"
        )
        try:
            classification = await worker.classify(
                self.bridge,
                spotlighted,
                meta,
                triage_rule=self.settings.site_triage_guidance(site_id),
            )
            category = (
                classification.category.value
                if hasattr(classification.category, "value")
                else str(classification.category)
            )
            priority, rationale = classification.priority, classification.rationale
        except Exception as e:
            # Weak/unavailable model returned unparseable output. Defer rather
            # than guess — no mail is lost and no draft is fabricated.
            log.warning("Classification failed for message %s: %s — deferring.", message_id, e)
            self.audit.error(
                {"node": "classify", "error": str(e)[:200]},
                subject_table="messages", subject_id=message_id,
            )
            recipient = self._bind_recipient(msg, thread_id) or (msg["from_addr"] or "")
            did = tools.propose_draft(
                self.store, message_id, thread_id, recipient,
                subject=f"Re: {msg['subject'] or ''}", body="",
                state="DEFERRED_NO_LLM",
                guardrail_flags={"injection_risk": injection_risk, "reason": "classify_failed"},
            )
            return did
        tools.propose_label(
            self.store, message_id,
            category=category, priority=priority,
            rationale=rationale, injection_risk=injection_risk,
            model_used=self.settings.llm.chat_model,
        )
        self.audit.tool_call(
            {"tool": "propose_label", "category": category},
            subject_table="messages", subject_id=message_id,
        )

        # --- decide whether to draft (action choice fixed by plan, not content) ---
        if not plan.should_draft or category not in _REPLYABLE:
            existing = self.store.latest_draft_for_message(message_id)
            if existing is not None and existing["state"] == "DEFERRED_NO_LLM":
                self.store.update_draft_state(
                    existing["id"],
                    "REJECTED",
                    guardrail_flags={"reason": "classification_no_reply", "category": category},
                )
            log.info("Message %s classified %s — no draft (plan=%s).", message_id, category, plan.should_draft)
            return None

        # --- recipient binding (§0.4) ---
        recipient = self._bind_recipient(msg, thread_id)
        if not recipient:
            log.warning("Message %s has no bindable recipient — skipping draft.", message_id)
            return None

        # --- retrieve thread-scoped context (§4/§5: same thread only) ---
        context_chunks: list[str] = []
        try:
            from ..rag.retriever import retrieve

            context_chunks = await retrieve(
                self.store, self.bridge, query=msg["subject"] or text[:200],
                k=5, thread_id=thread_id, site_id=site_id,
            )
        except Exception as e:
            log.info("Context retrieval skipped: %s", e)

        # --- draft (quarantined worker → body; recipient bound) ---
        try:
            proposed = await worker.draft_reply(
                self.bridge,
                spotlighted,
                context_chunks,
                recipient,
                self.settings.style,
                site_id=site_id,
                site_rule=self.settings.site_guidance(site_id),
            )
            body = (proposed.body or "").strip()
            subject = proposed.subject or f"Re: {msg['subject'] or ''}"
        except Exception as e:
            log.warning("Draft generation failed for message %s: %s — deferring.", message_id, e)
            self.audit.error(
                {"node": "draft", "error": str(e)[:200]},
                subject_table="messages", subject_id=message_id,
            )
            body, subject = "", f"Re: {msg['subject'] or ''}"
        if not body:
            # Model produced no usable body — defer for the human rather than
            # enqueue an empty PENDING draft.
            did = tools.propose_draft(
                self.store, message_id, thread_id, recipient, subject, "",
                state="DEFERRED_NO_LLM",
                guardrail_flags={"injection_risk": injection_risk, "reason": "empty_draft"},
            )
            log.info("Empty draft for message %s — deferred (draft %s).", message_id, did)
            return did
        self.audit.llm_call(
            {"node": "draft", "recipient": recipient},
            subject_table="messages", subject_id=message_id,
        )

        # --- output guardrails (run BEFORE enqueue; re-run after edit in UI) ---
        draft_dict = {
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "thread_id": thread_id,
        }
        # Pre-compute the draft embedding here (async) so the synchronous
        # cross-thread leak guard can use it without touching the event loop.
        draft_embedding = None
        try:
            vecs = await self.bridge.embed([body])
            draft_embedding = vecs[0] if vecs else None
        except Exception as e:
            log.debug("draft embedding skipped: %s", e)
        context = {
            "thread_participants": self._thread_participants(thread_id),
            "contacts": self.store.allowlisted_domains(),
            "last_untrusted_fetch_ts": None,
            "draft_embedding": draft_embedding,
            "site_id": site_id,
        }
        report = run_output_guardrails(
            draft_dict, context, self.store, self.settings.security, self.bridge
        )
        flags = {"injection_risk": injection_risk, **report.flags, "reasons": report.reasons}

        if report.passed:
            did = tools.propose_draft(
                self.store, message_id, thread_id, recipient, subject, body,
                state="PENDING", guardrail_flags=flags,
            )
            self.audit.tool_call(
                {"tool": "propose_draft", "state": "PENDING"},
                subject_table="drafts", subject_id=did,
            )
            log.info("Draft %s enqueued PENDING for message %s.", did, message_id)
            return did

        did = tools.propose_draft(
            self.store, message_id, thread_id, recipient, subject, body,
            state="BLOCKED", guardrail_flags=flags,
        )
        self.audit.block({"reasons": report.reasons}, subject_table="drafts", subject_id=did)
        log.warning("Draft %s BLOCKED for message %s: %s", did, message_id, report.reasons)
        return did
