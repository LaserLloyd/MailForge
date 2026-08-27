"""Text chunking for RAG (build spec §4).

Chunks documents into 500–1000-token windows (~750 default) with ~100-token
overlap. Tokens are approximated at ~4 chars/token to avoid a heavy tokenizer
dependency — adequate for retrieval-window sizing.
"""

from __future__ import annotations

import re

# Approximate average characters per token for English prose (spec §4 note).
CHARS_PER_TOKEN = 4


def approx_tokens(text: str) -> int:
    """Approximate token count of ``text`` (~4 chars/token, spec §4)."""
    return max(1, len(text) // CHARS_PER_TOKEN)


# Split on paragraph/sentence-ish boundaries to keep chunks coherent, while
# still respecting the size budget.
_SPLIT_RE = re.compile(r"(\n\s*\n|(?<=[.!?])\s+)")


def _segments(text: str) -> list[str]:
    """Break text into small atomic segments (paragraphs / sentences)."""
    parts = _SPLIT_RE.split(text)
    segs: list[str] = []
    for p in parts:
        if p is None:
            continue
        p = p.strip()
        if p:
            segs.append(p)
    return segs


def chunk_text(text: str, target_tokens: int = 750, overlap: int = 100) -> list[str]:
    """Split ``text`` into overlapping chunks (spec §4: 500–1000 tok, ~100 overlap).

    Parameters
    ----------
    text:
        The source text.
    target_tokens:
        Desired chunk size in approximate tokens (kept within the 500–1000 band).
    overlap:
        Approximate token overlap carried from the end of one chunk into the
        start of the next, to preserve context across boundaries.

    Returns a list of chunk strings (possibly one). Empty/whitespace input → [].
    """
    text = (text or "").strip()
    if not text:
        return []

    target_chars = max(1, target_tokens * CHARS_PER_TOKEN)
    overlap_chars = max(0, overlap * CHARS_PER_TOKEN)

    segments = _segments(text)
    if not segments:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append(" ".join(current).strip())
            current = []
            current_len = 0

    for seg in segments:
        seg_len = len(seg)
        # A single oversized segment: hard-split it on character windows.
        if seg_len > target_chars:
            flush()
            start = 0
            while start < seg_len:
                end = min(start + target_chars, seg_len)
                chunks.append(seg[start:end].strip())
                if end >= seg_len:
                    break
                start = end - overlap_chars if overlap_chars < target_chars else end
            continue

        if current_len + seg_len + 1 > target_chars and current:
            flush()
            # Seed the new chunk with a trailing overlap from the previous one.
            if overlap_chars and chunks:
                tail = chunks[-1][-overlap_chars:]
                current.append(tail)
                current_len += len(tail)
        current.append(seg)
        current_len += seg_len + 1

    flush()
    return [c for c in chunks if c]
