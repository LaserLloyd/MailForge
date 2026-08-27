"""Models page (build spec §9, §6).

Populated from ``bridge.list_models()`` (async; the bridge is
:class:`~mailforge.llm.bridge.LMStudioBridge`). Shows downloaded /
on-disk models and which is the configured (loaded) chat/embedding model. When
``bridge.is_up()`` is False, a RED "LM Studio down" banner is shown with the
``lms server start`` / ``lms daemon up`` recovery guidance (spec §6). The
bridge may be ``None`` (LLM extra not wired) — handled gracefully.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nicegui import background_tasks, ui

log = logging.getLogger(__name__)


def _model_key(m: Any) -> str:
    """Best-effort identifier from a list_models() entry (SDK dict or /v1 dict)."""
    if isinstance(m, dict):
        return str(m.get("model_key") or m.get("id") or m.get("path") or m)
    return str(m)


def _down_banner() -> None:
    """Red LM-Studio-down banner with recovery guidance (spec §6)."""
    with ui.card().classes("bb-card w-full").style("border-color: var(--error)"):
        with ui.row().classes("items-center").style("gap: 8px"):
            ui.icon("cloud_off", size="22px").style("color: var(--error)")
            ui.label("LM Studio is DOWN").classes("text-h6").style(
                "color: var(--error); font-weight: 700"
            )
        ui.label(
            "Drafts will be deferred (DEFERRED_NO_LLM) until it returns — no mail "
            "is lost. Start the runtime with one of:"
        ).classes("text-body2")
        ui.code("lms server start").classes("w-full")
        ui.code("lms daemon up").classes("w-full")


def render(bridge: object | None) -> None:
    """Render immediately, then populate the remote inventory in the background."""
    if bridge is None:
        with ui.card().classes("bb-card w-full").style("border-color: var(--warning)"):
            ui.label("No LM Studio bridge configured.").classes("text-weight-bold")
            ui.label(
                "Install the 'llm' extra and configure llm.lm_studio_host to enable "
                "model management."
            ).classes("text-body2")
        return

    chat_id = getattr(bridge, "model_id", None)
    embed_id = getattr(bridge, "embed_id", None)

    with ui.card().classes("bb-card w-full"):
        ui.label("Configured (loaded) models").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--accent-hover)"
        )
        ui.label(f"Chat: {chat_id or '?'}")
        ui.label(f"Embedding: {embed_id or '?'}")

    inventory = ui.card().classes("bb-card w-full q-mt-md")
    with inventory:
        ui.label("Downloaded / on-disk models").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--accent-hover)"
        )
        with ui.row().classes("items-center").style("gap: 10px"):
            ui.spinner(size="20px")
            ui.label("Loading the LM Studio inventory…").classes("text-grey")

    async def _load_inventory() -> None:
        try:
            up = await asyncio.to_thread(lambda: bool(bridge.is_up()))  # type: ignore[attr-defined]
        except Exception as e:
            log.debug("bridge.is_up() failed: %s", e)
            up = False

        if not up:
            try:
                inventory.clear()
                with inventory:
                    _down_banner()
            except Exception:
                pass  # client navigated away while the probe was running
            return

        try:
            models = await bridge.list_models()  # type: ignore[attr-defined]
        except Exception as e:
            log.warning("list_models failed: %s", e)
            models = []

        try:
            inventory.clear()
            with inventory:
                ui.label("Downloaded / on-disk models").classes(
                    "text-subtitle1 text-weight-bold"
                ).style("color: var(--accent-hover)")
                if not models:
                    ui.label("No models reported by LM Studio.").classes("text-grey")
                    return
                loaded_keys = {str(chat_id), str(embed_id)}
                rows: list[dict[str, Any]] = []
                for model in models:
                    key = _model_key(model)
                    rows.append(
                        {
                            "model": key,
                            "type": (model.get("type") if isinstance(model, dict) else "") or "",
                            "loaded": "loaded" if key in loaded_keys else "on-disk",
                        }
                    )
                ui.table(
                    columns=[
                        {
                            "name": "model",
                            "label": "Model",
                            "field": "model",
                            "align": "left",
                        },
                        {
                            "name": "type",
                            "label": "Type",
                            "field": "type",
                            "align": "left",
                        },
                        {
                            "name": "loaded",
                            "label": "Status",
                            "field": "loaded",
                            "align": "left",
                        },
                    ],
                    rows=rows,
                    row_key="model",
                ).classes("w-full")
        except Exception:
            pass  # client navigated away while the inventory was loading

    background_tasks.create(_load_inventory(), name=f"models-inventory-load-{id(inventory)}")
