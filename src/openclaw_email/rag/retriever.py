"""RAG retrieval (build spec §4, §5).

Embeds a query and runs KNN over ``chunk_embeddings`` via the Store. When a
``thread_id`` is supplied, retrieval is SCOPED to that thread so thread-history
chunks from other threads cannot bleed in (spec §4 / §5 — a hard security
requirement, not an optimization).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..db.store import Store
    from ..llm.bridge import LMStudioBridge

log = logging.getLogger(__name__)


async def _is_up(bridge) -> bool:
    """Non-blocking LM Studio probe: the sync ``is_up()`` is a 5 s HTTP call
    and these run inside the UI's event loop."""
    fn = getattr(bridge, "is_up_async", None)
    if fn is not None:
        return bool(await fn())
    import asyncio

    return bool(await asyncio.to_thread(bridge.is_up))


async def retrieve(
    store: "Store",
    bridge: "LMStudioBridge",
    query: str,
    k: int = 5,
    thread_id: str | None = None,
    site_id: str | None = None,
    reference_only: bool = False,
) -> list[str]:
    """Return up to ``k`` chunk texts most relevant to ``query`` (spec §4/§5).

    Thread-history retrieval is scoped to ``thread_id`` (passed through to
    :meth:`Store.knn_chunks`) to prevent cross-thread bleed. Returns ``[]`` when
    vector search is disabled (no sqlite-vec) or the bridge is down — callers
    degrade to no-context drafting.
    """
    if not query or not query.strip():
        return []
    if not getattr(store, "vec_enabled", False):
        if reference_only and site_id is not None:
            return [
                row["text"]
                for row in store.search_reference_chunks(site_id, query, limit=k)
                if row["text"]
            ]
        log.info("retrieve: vector search disabled (sqlite-vec missing)")
        return []
    if not await _is_up(bridge):
        log.info("retrieve: LM Studio down; returning no context")
        return []

    vectors = await bridge.embed([query])
    if not vectors:
        log.info("retrieve: query embedding unavailable; returning no context")
        return []

    rows = store.knn_chunks(
        vectors[0],
        k=k,
        thread_id=thread_id,
        site_id=site_id,
        reference_only=reference_only,
    )
    return [row["text"] for row in rows if row["text"]]
