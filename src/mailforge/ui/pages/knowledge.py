"""Readable handbook pages shared by users and the two OpenClaw response bots."""

from __future__ import annotations

import logging

from nicegui import ui

from ...knowledge.handbooks import read_site_handbook, write_site_handbooks
from ..markdown import safe_markdown

log = logging.getLogger(__name__)


def render(site_id: str, settings: object, *, full: bool = False) -> None:
    site = str(site_id).strip().lower()
    sites = getattr(settings, "sites", None) or {}
    if site not in sites:
        ui.label("Unknown knowledge site.").style("color: var(--error)")
        return
    site_name = getattr(sites[site], "name", site)
    if not str(getattr(sites[site], "knowledge_root", "") or "").strip():
        ui.label(
            "This site has no managed handbook. Set knowledge_root and "
            "policy_file in its [sites] configuration entry, or use uploaded "
            "references instead."
        ).style("color: var(--text-secondary)")
        return
    try:
        text = read_site_handbook(site, full=full)
    except (FileNotFoundError, OSError):
        try:
            write_site_handbooks(site)
            text = read_site_handbook(site, full=full)
        except Exception as e:  # noqa: BLE001
            log.exception("could not generate handbook for %s", site)
            ui.label(f"Handbook could not be generated: {e}").style(
                "color: var(--error)"
            )
            return

    with ui.row().classes("w-full items-center bb-toolbar"):
        ui.button(
            "Back to Knowledge",
            icon="arrow_back",
            on_click=lambda: ui.navigate.to("/references"),
        ).props("flat no-caps")
        ui.space()
        ui.button(
            "Response handbook",
            on_click=lambda: ui.navigate.to(f"/knowledge/{site}"),
        ).props("unelevated no-caps" if not full else "flat no-caps")
        ui.button(
            "Full site text",
            on_click=lambda: ui.navigate.to(f"/knowledge/{site}?full=true"),
        ).props("unelevated no-caps" if full else "flat no-caps")
    ui.label(
        f"{site_name} {'full site text' if full else 'email response handbook'}"
    ).style("font-size: 20px; font-weight: 800")
    ui.label(
        "This owner-only local page is generated from the canonical site repository. "
        "The response bot receives the same managed content."
    ).style("font-size: 12px; color: var(--text-secondary)")
    with ui.card().classes("w-full bb-card").style("padding: 18px"):
        safe_markdown(text)
