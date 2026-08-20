"""Managed handbook refresh, reference indexing, and OpenClaw bot synchronization."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Iterable

from .handbooks import (
    sync_site_handbook_to_openclaw,
    write_site_handbooks,
)

log = logging.getLogger(__name__)


def _sites() -> dict[str, object]:
    from ..config import load_settings

    return dict(load_settings().sites)


def managed_sites(sites: dict[str, object] | None = None) -> tuple[str, ...]:
    """Configured sites that declare a ``knowledge_root``, i.e. that have a
    generated handbook. Every other site relies on uploaded references only."""
    registry = _sites() if sites is None else sites
    return tuple(
        sorted(
            site_id
            for site_id, site in registry.items()
            if str(getattr(site, "knowledge_root", "") or "").strip()
        )
    )


async def _is_up(bridge) -> bool:
    """Non-blocking LM Studio probe: the sync ``is_up()`` is a 5 s HTTP call
    and these run inside the UI's event loop."""
    fn = getattr(bridge, "is_up_async", None)
    if fn is not None:
        return bool(await fn())
    import asyncio

    return bool(await asyncio.to_thread(bridge.is_up))


async def refresh_managed_knowledge(
    store: object,
    bridge: object | None,
    *,
    sites: Iterable[str] | None = None,
    sync_openclaw: bool = True,
) -> dict[str, Any]:
    """Regenerate changed handbooks and index them as site-scoped references."""
    registry = _sites()
    managed = managed_sites(registry)
    requested = list(registry) if sites is None else [str(s).strip().lower() for s in sites]
    report: dict[str, Any] = {}
    for site in requested:
        if site not in managed:
            report[site] = {"skipped": "no managed handbook for this site"}
            continue
        try:
            paths = write_site_handbooks(site, site=registry[site])
        except Exception as e:  # noqa: BLE001 — one site's missing source must not block the others
            log.warning("Handbook for site '%s' not regenerated: %s", site, e)
            report[site] = {"error": str(e)}
            continue
        site_report: dict[str, Any] = {"documents": {}, "openclaw": None}
        for kind, path in paths.items():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            document_id, needs_index = store.upsert_managed_reference_document(  # type: ignore[attr-defined]
                site,
                f"site-handbook:{site}:{kind}:v1",
                path.name,
                str(path),
                "text/markdown",
                path.stat().st_size,
                digest,
            )
            status = "unchanged"
            if needs_index:
                if bridge is None or not await _is_up(bridge):
                    store.update_reference_document_status(  # type: ignore[attr-defined]
                        document_id,
                        "ERROR",
                        error_text=(
                            "Managed handbook generated, but the embedding model is offline. "
                            "Refresh Knowledge when LM Studio is available."
                        ),
                        chunk_count=0,
                    )
                    status = "generated; indexing deferred"
                else:
                    from ..rag.embedder import ingest_reference_document

                    count = int(
                        await ingest_reference_document(store, bridge, document_id)
                    )
                    status = f"indexed {count} chunks"
            site_report["documents"][kind] = {
                "id": document_id,
                "path": str(path),
                "status": status,
            }
        if sync_openclaw:
            target = sync_site_handbook_to_openclaw(site, site=registry[site])
            site_report["openclaw"] = None if target is None else str(target)
        report[site] = site_report
    return report


def handbook_path(site_id: str, *, full: bool = False) -> Path:
    from ..paths import data_dir

    site = str(site_id).strip().lower()
    if site not in managed_sites():
        raise ValueError(f"unsupported site: {site_id}")
    suffix = "full-text" if full else "handbook"
    return data_dir() / "handbooks" / f"{site}-site-{suffix}.md"
