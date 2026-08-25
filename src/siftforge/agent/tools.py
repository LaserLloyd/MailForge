"""Capability-restricted tool catalog (build spec §0.2, §2, §5).

The LLM NEVER receives destructive capabilities. The only tools it can
influence are ``propose_draft`` / ``propose_label``, which merely ENQUEUE
work for human approval — they never touch the network or mutate external
state. ``send_email``/``delete``/``forward``/``archive`` are deliberately
absent from this catalog; the send path lives behind a human click in the UI.

This module is the single source of truth for "what the agent is allowed to
do". ``assert_tool_allowed`` is the runtime guard.
"""

from __future__ import annotations

from typing import Final

from ..db.store import Store

# The complete set of capabilities the agent layer may enact. Anything not
# here is forbidden by construction (§0.2).
ALLOWED_TOOLS: Final[frozenset[str]] = frozenset({"propose_draft", "propose_label"})

# Explicitly enumerated forbidden capabilities — used by tests/red-team to
# assert none ever leak into a prompt or tool list (§11 checklist item 1).
FORBIDDEN_TOOLS: Final[frozenset[str]] = frozenset(
    {"send_email", "send", "delete", "forward", "archive", "move", "reply_all"}
)


def assert_tool_allowed(name: str) -> None:
    if name in FORBIDDEN_TOOLS or name not in ALLOWED_TOOLS:
        raise PermissionError(
            f"Tool '{name}' is not in the agent capability catalog "
            f"(allowed: {sorted(ALLOWED_TOOLS)})"
        )


def propose_label(store: Store, message_id: int, **classification) -> None:
    """Enqueue a classification for a message. Non-destructive."""
    assert_tool_allowed("propose_label")
    store.upsert_classification(message_id, **classification)


def propose_draft(
    store: Store,
    message_id: int,
    thread_id: str,
    recipient: str,
    subject: str,
    body: str,
    state: str,
    guardrail_flags: dict | None = None,
) -> int:
    """Enqueue a draft reply for human approval. Never sends. Returns draft id.

    ``state`` is set by the graph (PENDING on guardrail pass, BLOCKED on fail,
    DEFERRED_NO_LLM when the model is down). The draft is inert until a human
    approves it in the UI.
    """
    assert_tool_allowed("propose_draft")
    return store.upsert_message_draft(
        message_id=message_id,
        thread_id=thread_id,
        recipient=recipient,
        subject=subject,
        body=body,
        state=state,
        guardrail_flags=guardrail_flags,
    )
