"""Site-isolated response-template manager."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from nicegui import ui

from ...db.store import DEFAULT_SITE_ID
from .. import markdown as markdown_ui
from .. import theme

log = logging.getLogger(__name__)

_FALLBACK_SITES = {DEFAULT_SITE_ID: "Main"}


def _value(row: Any, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, default)


def render(
    store: object,
    settings: object = None,
    *,
    site: str = "",
    to: str = "",
    subject: str = "",
    message_id: int | None = None,
) -> None:
    sites = {
        sid: getattr(cfg, "name", sid)
        for sid, cfg in (getattr(settings, "sites", None) or {}).items()
    } or dict(_FALLBACK_SITES)
    site = site if site in sites else next(iter(sites))
    state = {"site": site}

    ui.label(
        "Templates insert subject and body text only. They never change the recipient, "
        "approval state, or send an email."
    ).style("color: var(--text-secondary)")

    def _site_url(value: str) -> str:
        params: dict[str, Any] = {"site": value}
        if to:
            params["to"] = to
        if subject:
            params["subject"] = subject
        if message_id is not None:
            params["message_id"] = message_id
        return "/templates?" + urlencode(params)

    with ui.tabs(
        value=site,
        on_change=lambda e: ui.navigate.to(_site_url(str(e.value))),
    ).props("dense no-caps"):
        for sid, label in sites.items():
            ui.tab(sid, label=label)

    @ui.refreshable
    def _cards() -> None:
        try:
            rows = list(
                store.list_response_templates(state["site"])  # type: ignore[attr-defined]
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not list templates: %s", e)
            rows = []

        def _editor(row: Any | None = None) -> None:
            template_id = int(_value(row, "id")) if row is not None else None
            with ui.dialog() as dialog, ui.card().classes("oce-card").style(
                "width: min(760px, 94vw); max-width: 760px; gap: 10px"
            ):
                ui.label("Edit response template" if row else "New response template").style(
                    "font-size: 17px; font-weight: 800"
                )
                name = ui.input(
                    "Name", value=str(_value(row, "name", "") or "")
                ).props("outlined dense").classes("w-full")
                category = ui.input(
                    "Category", value=str(_value(row, "category", "General") or "General")
                ).props("outlined dense").classes("w-full")
                shortcut = ui.input(
                    "Shortcut", value=str(_value(row, "shortcut", "") or "")
                ).props("outlined dense").classes("w-full")
                subject_in = ui.input(
                    "Subject", value=str(_value(row, "subject", "") or "")
                ).props("outlined dense").classes("w-full")
                body = ui.textarea(
                    "Body (Markdown)",
                    value=str(_value(row, "body", "") or ""),
                ).props("outlined").classes("w-full").style("min-height: 270px")
                ui.label(
                    "Allowed placeholders: {{first_name}}, {{site_name}}, {{signature}}, "
                    "{{today}}, {{question}}, {{next_step}}"
                ).style("font-size: 11.5px; color: var(--text-muted)")

                def _save() -> None:
                    try:
                        store.save_response_template(  # type: ignore[attr-defined]
                            state["site"],
                            str(name.value or ""),
                            str(category.value or ""),
                            str(subject_in.value or ""),
                            str(body.value or ""),
                            str(shortcut.value or ""),
                            template_id=template_id,
                        )
                    except Exception as e:  # noqa: BLE001
                        ui.notify(f"Template was not saved: {e}", type="negative")
                        return
                    dialog.close()
                    _cards.refresh()
                    ui.notify("Template saved.", type="positive")

                with ui.row().classes("w-full justify-end"):
                    ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
                    ui.button("Save", icon="save", on_click=_save).props(
                        "unelevated no-caps color=primary"
                    )
            dialog.open()

        def _delete(template_id: int) -> None:
            try:
                if not store.delete_response_template(  # type: ignore[attr-defined]
                    template_id, state["site"]
                ):
                    raise ValueError("built-in templates cannot be deleted")
                _cards.refresh()
                ui.notify("Template deleted.", type="positive")
            except Exception as e:  # noqa: BLE001
                ui.notify(f"Delete failed: {e}", type="negative")

        with ui.row().classes("w-full items-center"):
            ui.label(f"{sites.get(state['site'], state['site'])} response templates").style(
                "font-size: 17px; font-weight: 800"
            )
            ui.space()
            ui.button("New template", icon="add", on_click=lambda: _editor()).props(
                "unelevated no-caps color=primary"
            )

        if not rows:
            ui.label("No templates for this site.").style("color: var(--text-secondary)")
            return
        with ui.row().classes("w-full items-stretch").style(
            "gap: 12px; flex-wrap: wrap"
        ):
            for row in rows:
                tid = int(_value(row, "id"))
                with ui.card().classes("oce-card").style(
                    "padding: 14px; flex: 1 1 360px; max-width: 620px; min-width: 0"
                ):
                    with ui.row().classes("w-full items-center no-wrap"):
                        with ui.column().classes("col").style("gap: 1px; min-width: 0"):
                            ui.label(str(_value(row, "name", "Template"))).style(
                                "font-weight: 800"
                            )
                            ui.label(str(_value(row, "category", "General"))).style(
                                "font-size: 11.5px; color: var(--text-muted)"
                            )
                        if _value(row, "is_system", 0):
                            theme.badge("BUILT IN", "accent")
                    template_subject = str(_value(row, "subject", "") or "")
                    if template_subject:
                        ui.label(template_subject).style(
                            "font-size: 12.5px; color: var(--text-secondary); "
                            "font-weight: 700"
                        )
                    with ui.expansion("Preview", icon="visibility").classes(
                        "w-full oce-card"
                    ):
                        markdown_ui.safe_markdown(str(_value(row, "body", "") or ""))
                    params: dict[str, Any] = {"template_id": tid}
                    if to:
                        params["to"] = to
                    if subject:
                        params["subject"] = subject
                    with ui.row().classes("w-full justify-end"):
                        ui.button(
                            "Use",
                            icon="edit",
                            on_click=lambda _e, p=dict(params): ui.navigate.to(
                                "/compose?" + urlencode(p)
                            ),
                        ).props("unelevated no-caps color=primary")
                        ui.button(
                            "Edit", icon="settings", on_click=lambda _e, r=row: _editor(r)
                        ).props("flat no-caps")
                        if not _value(row, "is_system", 0):
                            ui.button(
                                icon="delete",
                                on_click=lambda _e, i=tid: _delete(i),
                            ).props("flat round dense color=negative")

    _cards()
