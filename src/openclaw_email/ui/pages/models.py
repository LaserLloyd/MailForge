"""Models page (build spec §9, §6).

Populated from ``bridge.list_models()`` (async; the bridge is
:class:`~openclaw_email.llm.bridge.LMStudioBridge`). Shows downloaded /
on-disk models and which is the configured (loaded) chat/embedding model. When
``bridge.is_up()`` is False, a RED "LM Studio down" banner is shown with the
``lms server start`` / ``lms daemon up`` recovery guidance (spec §6). The
bridge may be ``None`` (LLM extra not wired) — handled gracefully.
"""

from __future__ import annotations

import logging
from typing import Any

from nicegui import ui

log = logging.getLogger(__name__)


def _model_key(m: Any) -> str:
    """Best-effort identifier from a list_models() entry (SDK dict or /v1 dict)."""
    if isinstance(m, dict):
        return str(m.get("model_key") or m.get("id") or m.get("path") or m)
    return str(m)


def _down_banner() -> None:
    """Red LM-Studio-down banner with recovery guidance (spec §6)."""
    with ui.card().classes("w-full bg-red-1 border border-red"):
        ui.label("LM Studio is DOWN").classes("text-red text-weight-bold text-h6")
        ui.label(
            "Drafts will be deferred (DEFERRED_NO_LLM) until it returns — no mail "
            "is lost. Start the runtime with one of:"
        ).classes("text-body2")
        ui.code("lms server start").classes("w-full")
        ui.code("lms daemon up").classes("w-full")


async def render(bridge: object | None) -> None:
    """Render the models page (spec §9). Async: probes the bridge live."""
    ui.label("Models").classes("text-h5 q-mb-md")

    if bridge is None:
        with ui.card().classes("w-full bg-orange-1"):
            ui.label("No LM Studio bridge configured.").classes("text-weight-bold")
            ui.label(
                "Install the 'llm' extra and configure llm.lm_studio_host to enable "
                "model management."
            ).classes("text-body2")
        return

    # Health check (sync) — drives the red banner.
    up = False
    try:
        up = bool(bridge.is_up())  # type: ignore[attr-defined]
    except Exception as e:
        log.debug("bridge.is_up() failed: %s", e)
        up = False

    if not up:
        _down_banner()
        return

    chat_id = getattr(bridge, "model_id", None)
    embed_id = getattr(bridge, "embed_id", None)

    with ui.card().classes("w-full"):
        ui.label("Configured (loaded) models").classes("text-subtitle1 text-weight-bold")
        ui.label(f"Chat: {chat_id or '?'}")
        ui.label(f"Embedding: {embed_id or '?'}")

    # On-disk / downloaded models (async).
    try:
        models = await bridge.list_models()  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("list_models failed: %s", e)
        models = []

    with ui.card().classes("w-full q-mt-md"):
        ui.label("Downloaded / on-disk models").classes("text-subtitle1 text-weight-bold")
        if not models:
            ui.label("No models reported by LM Studio.").classes("text-grey")
            return
        loaded_keys = {str(chat_id), str(embed_id)}
        rows: list[dict[str, Any]] = []
        for m in models:
            key = _model_key(m)
            rows.append({
                "model": key,
                "type": (m.get("type") if isinstance(m, dict) else "") or "",
                "loaded": "loaded" if key in loaded_keys else "on-disk",
            })
        ui.table(
            columns=[
                {"name": "model", "label": "Model", "field": "model", "align": "left"},
                {"name": "type", "label": "Type", "field": "type", "align": "left"},
                {"name": "loaded", "label": "Status", "field": "loaded", "align": "left"},
            ],
            rows=rows,
            row_key="model",
        ).classes("w-full")
