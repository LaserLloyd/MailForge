"""Planner LLM (build spec §5, Plan-Then-Execute).

The planner sees ONLY the user's policy and SYMBOLIC references about the
message (sender domain, link count, attachment flag, whether the thread is
known). It NEVER sees raw email text. Its job is to FIX the plan — the choice
of actions — *before* any content is examined. Tool/worker outputs may later
affect draft *content*, never the *choice of actions* (the Plan-Then-Execute
invariant). This bounds prompt-injection: even a fully attacker-controlled
email cannot change what the agent decides to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm.prompts import load
from .tools import ALLOWED_TOOLS


@dataclass
class SymbolicRefs:
    """Non-content facts the planner is allowed to see."""

    sender_domain: str
    thread_known: bool
    link_count: int
    has_attachments: bool
    category_hint: str | None = None  # may be filled post-classify, never raw text


@dataclass
class Plan:
    allowed_actions: list[str]
    should_draft: bool
    notes: str = ""
    decided_by: str = "policy"
    raw: dict = field(default_factory=dict)


# Categories that, per default policy, warrant a drafted reply. The planner can
# tighten this with user policy but the action set is always a subset of
# ALLOWED_TOOLS — the planner can never expand capabilities.
_REPLYABLE = {"RESPOND", "MEETING"}


def _policy_summary(security, style) -> str:
    return (
        f"tone={getattr(style, 'tone', 'professional')}; "
        f"require_human_approval={getattr(security, 'require_human_approval', True)}; "
        f"allowlist_domains={sorted(getattr(security, 'recipient_allowlist_domains', set()))}; "
        f"block_domains={sorted(getattr(security, 'recipient_block_domains', set()))}"
    )


async def freeze_plan(bridge, refs: SymbolicRefs, settings) -> Plan:
    """Produce a fixed plan from policy + symbolic refs ONLY.

    The plan is deterministic by default; the LLM is consulted only to make a
    policy-level go/no-go on drafting, and its answer is clamped to the allowed
    action catalog. If the bridge is down we fall back to a pure-policy plan.
    """
    security = getattr(settings, "security", settings)
    style = getattr(settings, "style", None)

    sender_blocked = refs.sender_domain.lower() in {
        d.lower() for d in getattr(security, "recipient_block_domains", set())
    }
    # Default policy decision (content-independent).
    should_draft = (not sender_blocked) and (
        refs.category_hint is None or refs.category_hint in _REPLYABLE
    )

    plan = Plan(
        allowed_actions=sorted(ALLOWED_TOOLS),
        should_draft=should_draft,
        notes=(
            f"sender_domain={refs.sender_domain}, thread_known={refs.thread_known}, "
            f"links={refs.link_count}, attachments={refs.has_attachments}, "
            f"blocked={sender_blocked}"
        ),
        decided_by="policy",
    )

    if bridge is None or not bridge.is_up():
        return plan

    # Optional LLM refinement — policy + symbolic refs only, no raw content.
    try:
        system = load("system_planner")
        user = (
            f"USER POLICY:\n{_policy_summary(security, style)}\n\n"
            f"SYMBOLIC MESSAGE FACTS (no content):\n"
            f"- sender_domain: {refs.sender_domain}\n"
            f"- thread_known: {refs.thread_known}\n"
            f"- link_count: {refs.link_count}\n"
            f"- has_attachments: {refs.has_attachments}\n"
            f"- category_hint: {refs.category_hint}\n\n"
            "Decide ONLY whether a reply draft should be prepared. "
            'Respond with JSON: {"should_draft": true|false, "reason": "..."}'
        )
        schema = {
            "type": "object",
            "properties": {
                "should_draft": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["should_draft"],
            "additionalProperties": False,
        }
        out = await bridge.chat_structured(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            schema,
            schema_name="plan_decision",
        )
        if out and "should_draft" in out:
            # Policy block always wins over an LLM "yes" (defence in depth).
            plan.should_draft = bool(out["should_draft"]) and not sender_blocked
            plan.notes += f" | llm_reason={out.get('reason', '')[:120]}"
            plan.decided_by = "policy+llm"
            plan.raw = out
    except Exception:
        # Any planner failure → keep the conservative policy plan.
        pass

    return plan
