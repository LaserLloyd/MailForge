"""Agent <-> UI interrupt contract (build spec §9, §5).

Verbatim shape of the ``HumanInterrupt`` / ``HumanResponse`` typed dicts from
``langchain-ai/agent-inbox``. These are the wire contract between the agent
graph (which *enqueues* a draft for approval) and the NiceGUI human-approval
layer (which returns the human's decision). Keeping the shape identical means
the same UI conventions and any agent-inbox-compatible tooling interoperate.

Pure ``TypedDict`` / dataclass definitions — no heavy dependencies. The agent
emits a :class:`HumanInterrupt`; the UI resolves it to a :class:`HumanResponse`
whose ``type`` is one of ``accept | ignore | response | edit`` (spec §9).

Mapping to the draft state machine (spec §5):
    accept   -> APPROVED -> SENT          (human clicked Approve)
    edit     -> re-run output guardrails  -> SENT on pass / blocked on fail
    response -> human supplied free-text instructions back to the agent
    ignore   -> REJECTED (audit only)     (human clicked Reject)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict


class HumanInterruptConfig(TypedDict):
    """Which response actions the UI should offer for an interrupt.

    Mirrors agent-inbox ``HumanInterruptConfig``. Each flag gates one of the
    four buttons/affordances the human-approval card renders.
    """

    allow_ignore: bool
    allow_respond: bool
    allow_edit: bool
    allow_accept: bool


class ActionRequest(TypedDict):
    """The concrete action the agent is requesting approval for.

    Mirrors agent-inbox ``ActionRequest``. For this app ``action`` is typically
    ``"send_email"`` and ``args`` carries ``recipient`` / ``subject`` / ``body``
    (plus thread metadata) — the bound, human-reviewable payload.
    """

    action: str
    args: dict[str, Any]


class HumanInterrupt(TypedDict):
    """An interrupt raised by the agent, surfaced as a NiceGUI approval card.

    Mirrors agent-inbox ``HumanInterrupt`` verbatim:
      * ``action_request`` — the action + bound args awaiting a human decision.
      * ``config``         — which responses are permitted.
      * ``description``    — human-readable context (rationale, provenance).
    """

    action_request: ActionRequest
    config: HumanInterruptConfig
    description: str | None


class HumanResponse(TypedDict):
    """The human's decision returned to the agent (spec §9).

    Mirrors agent-inbox ``HumanResponse`` verbatim. ``type`` selects the path:
      * ``accept``   — approve the action as-is.
      * ``edit``     — approve a modified ``ActionRequest`` (re-guardrailed).
      * ``response`` — free-text reply / instructions back to the agent.
      * ``ignore``   — reject; take no action (audit only).

    ``args`` is the edited ``ActionRequest`` for ``edit``, a ``str`` for
    ``response``, and ``None`` for ``accept`` / ``ignore``.
    """

    type: Literal["accept", "ignore", "response", "edit"]
    args: None | str | ActionRequest


# Convenience constructors / typed default config ---------------------------

#: Default config for an email-send approval: every affordance enabled.
DEFAULT_EMAIL_CONFIG: HumanInterruptConfig = {
    "allow_ignore": True,
    "allow_respond": True,
    "allow_edit": True,
    "allow_accept": True,
}


@dataclass(slots=True)
class ApprovalRecord:
    """In-process record proving a human approved a send (spec §0.1, §11).

    The send path asserts an instance of this exists (with ``approved_by`` set)
    BEFORE calling :func:`openclaw_email.mail.smtp_sender.send_email`. It is the
    runtime witness of the "human-approval record" invariant.
    """

    draft_id: int
    approved_by: str
    approved_at: str
    response_type: Literal["accept", "edit"]
    recipient: str
    flags: dict[str, Any] = field(default_factory=dict)


def make_email_interrupt(
    *,
    recipient: str,
    subject: str,
    body: str,
    description: str | None = None,
    extra_args: dict[str, Any] | None = None,
    config: HumanInterruptConfig | None = None,
) -> HumanInterrupt:
    """Build a ``HumanInterrupt`` for an email-send approval (helper, spec §9)."""
    args: dict[str, Any] = {"recipient": recipient, "subject": subject, "body": body}
    if extra_args:
        args.update(extra_args)
    return HumanInterrupt(
        action_request=ActionRequest(action="send_email", args=args),
        config=config or DEFAULT_EMAIL_CONFIG,
        description=description,
    )
