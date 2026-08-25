"""Per-site reference-document library with safe upload and indexing controls."""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from nicegui import ui

from ...paths import data_dir
from .. import theme

log = logging.getLogger(__name__)

_ALLOWED_SUFFIXES = {".txt", ".md", ".pdf", ".docx"}
_MAX_FILE_SIZE = 20 * 1024 * 1024
_view: dict[str, str] = {"site": ""}


def safe_upload_name(name: str) -> str:
    """Return a display/storage-safe basename; never preserve path components."""
    base = Path((name or "document").replace("\\", "/")).name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .")
    return stem[:180] or "document"


def _value(row: Any, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, default)


def _status_kind(status: str) -> str:
    return {
        "READY": "success",
        "INDEXING": "accent",
        "ERROR": "error",
        "UPLOADED": "warning",
    }.get(status.upper(), "")


def _size_label(value: Any) -> str:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "?"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


async def _index_document(store: object, bridge: object | None, document_id: int) -> None:
    if bridge is None or not bool(bridge.is_up()):  # type: ignore[attr-defined]
        store.update_reference_document_status(  # type: ignore[attr-defined]
            document_id,
            "ERROR",
            error_text="AI/embedding model is offline; start LM Studio and reindex.",
            chunk_count=0,
        )
        return
    store.update_reference_document_status(  # type: ignore[attr-defined]
        document_id, "INDEXING", error_text=None
    )
    try:
        from ...rag.embedder import ingest_reference_document

        count = int(await ingest_reference_document(store, bridge, document_id))
        if count <= 0:
            raise RuntimeError("No readable text or embeddings were produced")
        store.update_reference_document_status(  # type: ignore[attr-defined]
            document_id, "READY", error_text=None, chunk_count=count
        )
    except Exception as e:  # noqa: BLE001
        log.exception("reference indexing failed")
        store.update_reference_document_status(  # type: ignore[attr-defined]
            document_id, "ERROR", error_text=str(e)[:300], chunk_count=0
        )


def render(store: object, settings: object, bridge: object | None) -> None:
    """Render upload, status, reindex, and deletion controls for each business site."""
    # Site isolation is stored with each document, not inferred from UI paths; we only
    # use `settings` here for the configured site list/labels shown in this page's UI.
    sites: dict[str, str] = {
        sid: getattr(cfg, "name", sid)
        for sid, cfg in (getattr(settings, "sites", None) or {}).items()
    } or {"main": "Main"}
    if _view["site"] not in sites:
        _view["site"] = next(iter(sites))

    with ui.card().classes("bb-card w-full").style("padding: 14px"):
        with ui.row().classes("items-center no-wrap").style("gap: 10px"):
            ui.icon("shield", size="22px").style("color: var(--accent-hover)")
            with ui.column().style("gap: 2px"):
                ui.label("Site-isolated AI knowledge").style("font-weight: 800")
                ui.label(
                    "Documents are retrieved only for their selected site. Email content never "
                    "changes that boundary."
                ).style("font-size: 12px; color: var(--text-secondary)")
        if not getattr(store, "vec_enabled", False):
            ui.label(
                "Semantic reference search is unavailable because sqlite-vec is not loaded. "
                "Uploads remain local and can be reindexed after the RAG component is installed."
            ).style("font-size: 12px; color: var(--warning); margin-top: 8px")

    async def _refresh_handbooks() -> None:
        from ...knowledge.sync import refresh_managed_knowledge

        ui.notify(
            "Refreshing canonical site text and bot knowledge…", type="info"
        )
        try:
            report = await refresh_managed_knowledge(store, bridge)
            indexed = sum(
                1
                for site_report in report.values()
                for doc in site_report.get("documents", {}).values()
                if str(doc["status"]).startswith("indexed")
            )
            ui.notify(
                f"Bot handbooks refreshed ({indexed} document(s) newly indexed).",
                type="positive",
            )
            _documents.refresh()
        except Exception as e:  # noqa: BLE001
            log.exception("managed handbook refresh failed")
            ui.notify(f"Handbook refresh failed: {e}", type="negative", timeout=10000)

    with ui.card().classes("bb-card w-full").style("padding: 14px"):
        with ui.row().classes("w-full items-center bb-toolbar"):
            with ui.column().classes("col").style("gap: 2px"):
                ui.label("Response-bot site handbooks").style("font-weight: 800")
                ui.label(
                    "Generated from each local canonical site, copied to its dedicated "
                    "OpenClaw email bot, and indexed into the matching site library."
                ).style("font-size: 12px; color: var(--text-secondary)")
            ui.button(
                "Refresh handbooks",
                icon="sync",
                on_click=_refresh_handbooks,
            ).props("outline no-caps color=primary")
        with ui.row().classes("w-full bb-toolbar"):
            # Not every configured site necessarily has a managed handbook generator;
            # /knowledge/<site> itself reports "Unknown/could not be generated" for
            # any that don't, so it's safe to link out for every configured site.
            for site_id, site_label in sites.items():
                ui.button(
                    f"Read {site_label} handbook",
                    icon="menu_book",
                    on_click=lambda _e, s=site_id: ui.navigate.to(f"/knowledge/{s}"),
                ).props("flat no-caps")
                ui.button(
                    f"{site_label} full site text",
                    icon="article",
                    on_click=lambda _e, s=site_id: ui.navigate.to(f"/knowledge/{s}?full=true"),
                ).props("flat no-caps")

    def _change_site(e: Any) -> None:
        site_id = str(e.value or "")
        if site_id not in sites:
            ui.notify("Unknown reference library.", type="negative")
            return
        _view["site"] = site_id
        _documents.refresh()

    ui.select(
        sites,
        value=_view["site"],
        label="Reference library",
        on_change=_change_site,
    ).props("outlined dense").classes("w-64")

    @ui.refreshable
    def _documents() -> None:
        site_id = _view["site"]
        try:
            rows = list(store.list_reference_documents(site_id))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("could not list reference documents: %s", e)
            rows = []

        if not rows:
            with ui.column().classes("w-full items-center q-pa-lg").style("gap: 6px"):
                ui.icon("library_books", size="38px").style("color: var(--text-muted)")
                ui.label(f"No {sites.get(site_id, site_id)} reference documents yet.").style(
                    "color: var(--text-secondary)"
                )
            return

        async def _reindex(document_id: int) -> None:
            ui.notify("Reindexing reference document…", type="info")
            await _index_document(store, bridge, document_id)
            _documents.refresh()

        def _delete(document_id: int, filename: str) -> None:
            with ui.dialog() as dialog, ui.card().style("gap: 10px; max-width: 440px"):
                ui.label("Delete reference document?").style("font-weight: 800")
                ui.label(filename).style("overflow-wrap: anywhere")
                ui.label(
                    "Its indexed chunks will also be removed. This does not affect the original "
                    "email archive."
                ).style("font-size: 12px; color: var(--text-secondary)")

                def _confirm() -> None:
                    try:
                        store.delete_reference_document(document_id)  # type: ignore[attr-defined]
                        ui.notify("Reference document deleted.", type="positive")
                        dialog.close()
                        _documents.refresh()
                    except Exception as e:  # noqa: BLE001
                        ui.notify(f"Delete failed: {e}", type="negative")

                with ui.row().classes("w-full justify-end"):
                    ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
                    ui.button("Delete", icon="delete", on_click=_confirm).props(
                        "unelevated no-caps color=negative"
                    )
            dialog.open()

        with ui.column().classes("w-full").style("gap: 8px"):
            for row in rows:
                document_id = int(_value(row, "id"))
                filename = str(_value(row, "filename", "document"))
                status = str(_value(row, "status", "UPLOADED") or "UPLOADED")
                managed = bool(_value(row, "managed_key", None))
                with ui.element("div").classes("bb-card w-full q-pa-md"):
                    with ui.row().classes("w-full items-center no-wrap").style("gap: 10px"):
                        ui.icon("description", size="22px").style(
                            "color: var(--text-secondary)"
                        )
                        with ui.column().classes("col").style("gap: 2px; min-width: 0"):
                            ui.label(filename).style(
                                "font-weight: 700; overflow-wrap: anywhere"
                            )
                            ui.label(
                                f"{_size_label(_value(row, 'size_bytes', 0))} · "
                                f"{int(_value(row, 'chunk_count', 0) or 0)} chunk(s) · "
                                f"{str(_value(row, 'updated_at', '') or '')[:19].replace('T', ' ')}"
                            ).style("font-size: 11.5px; color: var(--text-muted)")
                            error = str(_value(row, "error_text", "") or "")
                            if error:
                                ui.label(error).style(
                                    "font-size: 11.5px; color: var(--error); overflow-wrap: anywhere"
                                )
                        theme.badge(status, _status_kind(status))
                        if managed:
                            theme.badge("MANAGED", "accent")
                        reindex_btn = ui.button(
                            icon="refresh",
                            on_click=lambda _e, i=document_id: _reindex(i),
                        ).props("flat round dense")
                        with reindex_btn:
                            ui.tooltip("Reindex")
                        if not managed:
                            delete_btn = ui.button(
                                icon="delete",
                                on_click=lambda _e, i=document_id, f=filename: _delete(i, f),
                            ).props("flat round dense color=negative")
                            with delete_btn:
                                ui.tooltip("Delete")

    async def _upload(e: Any) -> None:
        site_id = _view["site"]
        name = safe_upload_name(e.file.name)
        suffix = Path(name).suffix.lower()
        size = int(e.file.size())
        if suffix not in _ALLOWED_SUFFIXES:
            ui.notify("Supported document types: TXT, Markdown, PDF, and DOCX.", type="negative")
            return
        if size <= 0 or size > _MAX_FILE_SIZE:
            ui.notify("Document must be between 1 byte and 20 MB.", type="negative")
            return
        target_dir = data_dir() / "references" / site_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{uuid.uuid4().hex}-{name}"
        try:
            await e.file.save(target)
            os.chmod(target, 0o600)
            document_id = int(
                store.create_reference_document(  # type: ignore[attr-defined]
                    site_id,
                    name,
                    str(target),
                    str(e.file.content_type or "application/octet-stream"),
                    size,
                )
            )
            _documents.refresh()
            await _index_document(store, bridge, document_id)
            _documents.refresh()
            row = store.get_reference_document(document_id)  # type: ignore[attr-defined]
            if str(_value(row, "status", "")) == "READY":
                ui.notify(f"{name} uploaded and indexed.", type="positive")
            else:
                ui.notify(f"{name} uploaded; indexing needs attention.", type="warning")
        except Exception as ex:  # noqa: BLE001
            target.unlink(missing_ok=True)
            log.exception("reference upload failed")
            ui.notify(f"Upload failed: {ex}", type="negative", timeout=8000)

    with ui.card().classes("bb-card w-full").style("padding: 14px; gap: 8px"):
        ui.label("Upload a reference document").style("font-weight: 800")
        ui.label(
            "The selected site is permanently attached to the document and enforced during retrieval."
        ).style("font-size: 12px; color: var(--text-secondary)")
        ui.upload(
            label="Choose TXT, Markdown, PDF, or DOCX",
            auto_upload=True,
            max_file_size=_MAX_FILE_SIZE,
            on_upload=_upload,
            on_rejected=lambda: ui.notify("Document rejected (type or size).", type="negative"),
        ).props('accept=".txt,.md,.pdf,.docx" flat bordered').classes("w-full")

    _documents()
