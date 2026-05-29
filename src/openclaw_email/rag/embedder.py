"""Embedding + document ingestion for RAG (build spec §4, §7).

Reads style guides + context docs (and per-thread history), chunks them, embeds
via the LM Studio bridge (768-dim, spec §6), and stores chunks + embeddings in
the ``chunks`` / ``chunk_embeddings`` tables (spec §7).

``markitdown`` is an OPTIONAL dependency (the ``rag`` extra) used to parse rich
docs; it is import-guarded so ingestion still works (plain-text read) without
it. If the bridge is down, ingestion logs and returns 0 gracefully (spec §5:
ingestion never blocks on the LLM).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .chunker import approx_tokens, chunk_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Settings
    from ..db.store import Store
    from ..llm.bridge import LMStudioBridge

log = logging.getLogger(__name__)

# Import-guard the OPTIONAL markitdown parser (the ``rag`` extra).
try:  # pragma: no cover - exercised by extra presence
    from markitdown import MarkItDown

    _HAVE_MARKITDOWN = True
except Exception:
    MarkItDown = None  # type: ignore[assignment]
    _HAVE_MARKITDOWN = False


def _read_document(path: Path) -> str:
    """Read a document to text, using markitdown when available (spec §1)."""
    if _HAVE_MARKITDOWN:
        try:
            result = MarkItDown().convert(str(path))
            return result.text_content or ""
        except Exception as e:
            log.warning("markitdown failed for %s, falling back to plain read: %s", path, e)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("could not read %s: %s", path, e)
        return ""


async def embed_chunks(
    bridge: "LMStudioBridge", chunks: list[str]
) -> list[list[float]]:
    """Embed ``chunks`` via the bridge (768-dim, spec §6).

    Returns ``[]`` when the bridge is down or returns nothing — callers must
    skip storing embeddings in that case (graceful degradation, spec §5).
    """
    if not chunks:
        return []
    return await bridge.embed(chunks)


async def _ingest_paths(
    store: "Store",
    bridge: "LMStudioBridge",
    paths: list[str],
    source_kind: str,
) -> int:
    """Chunk + embed + store every file in ``paths``. Returns chunk count."""
    total = 0
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.exists():
            log.warning("ingest: path does not exist: %s", path)
            continue
        text = _read_document(path)
        if not text.strip():
            continue
        chunks = chunk_text(text)
        if not chunks:
            continue
        embeddings = await embed_chunks(bridge, chunks)
        have_vecs = len(embeddings) == len(chunks)
        if not have_vecs:
            log.info(
                "ingest: no/partial embeddings for %s (bridge down?); "
                "storing chunk text without vectors",
                path,
            )
        for i, chunk in enumerate(chunks):
            chunk_id = store.add_chunk(
                source_kind=source_kind,
                source_id=str(path),
                thread_id=None,
                text=chunk,
                token_count=approx_tokens(chunk),
            )
            if have_vecs:
                store.add_embedding(chunk_id, embeddings[i])
            total += 1
    return total


async def ingest_documents(
    settings: "Settings",
    include_style: bool = True,
    include_context: bool = True,
) -> int:
    """Ingest style guides + context docs into the vector store (spec §4 CLI).

    Reads ``settings.style.style_guide_paths`` / ``context_doc_paths``, chunks,
    embeds via a bridge built from settings, and stores chunks + embeddings.
    Returns the number of chunks ingested. If the bridge is down, logs and
    returns 0 (ingestion never crashes the loop, spec §5).
    """
    from ..db.store import open_store
    from ..llm.bridge import LMStudioBridge

    bridge = LMStudioBridge.from_settings(settings)
    if not bridge.is_up():
        log.warning("ingest_documents: LM Studio is down; skipping (returning 0)")
        return 0

    store = open_store(settings.resolved_db_path(), init=True)
    try:
        total = 0
        if include_style and settings.style.style_guide_paths:
            total += await _ingest_paths(
                store, bridge, settings.style.style_guide_paths, "style_guide"
            )
        if include_context and settings.style.context_doc_paths:
            total += await _ingest_paths(
                store, bridge, settings.style.context_doc_paths, "context_doc"
            )
        log.info("ingest_documents: ingested %d chunks", total)
        return total
    finally:
        store.close()


async def ingest_thread_history(
    store: "Store", bridge: "LMStudioBridge", thread_id: str
) -> int:
    """Ingest a thread's prior messages as ``thread_history`` chunks (spec §4).

    Stored with ``source_kind='thread_history'`` and the ``thread_id`` SET, so
    retrieval can be scoped to the same thread and prevent cross-thread bleed
    (spec §4 / §5). Returns the number of chunks stored; 0 if the bridge is down.
    """
    if not bridge.is_up():
        log.warning("ingest_thread_history: LM Studio down; skipping thread %s", thread_id)
        return 0

    messages = store.thread_messages(thread_id)
    total = 0
    for msg in messages:
        text = (msg["sanitized_text"] or "").strip()
        if not text:
            continue
        chunks = chunk_text(text)
        if not chunks:
            continue
        embeddings = await embed_chunks(bridge, chunks)
        have_vecs = len(embeddings) == len(chunks)
        for i, chunk in enumerate(chunks):
            chunk_id = store.add_chunk(
                source_kind="thread_history",
                source_id=str(msg["id"]),
                thread_id=thread_id,
                text=chunk,
                token_count=approx_tokens(chunk),
            )
            if have_vecs:
                store.add_embedding(chunk_id, embeddings[i])
            total += 1
    log.info("ingest_thread_history: %d chunks for thread %s", total, thread_id)
    return total
