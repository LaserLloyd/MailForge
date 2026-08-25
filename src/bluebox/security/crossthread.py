"""Cross-thread leak guard (build spec §4, §5, §11).

Detects when a draft reply leaks content that belongs to a *different* email
thread — a confused-deputy / context-bleed risk. Two complementary signals:

  1. **n-gram overlap** — build word n-grams (``crossthread_ngram_n``) of the
     draft and test them against an in-process set built from OTHER threads'
     chunk texts. Distinctive overlapping spans => likely leak.
  2. **embedding similarity** — via ``store.knn_chunks`` over the optional LLM
     ``bridge`` embedding; chunks whose ``thread_id`` differs from the current
     thread and whose similarity ≥ ``crossthread_embedding_threshold`` are
     leak evidence.

``bridge`` may be ``None`` (no embedder) — then only the n-gram check runs.
Exposes ``CrossThreadGuard(store, settings)`` with ``check(...) ->
CrossThreadResult``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\w+")
# n-grams shorter than this many chars are too generic to be leak evidence.
_MIN_NGRAM_CHARS = 12


@dataclass
class CrossThreadResult:
    """Outcome of the cross-thread leak check (spec §5)."""

    passed: bool
    flags: dict[str, float] = field(default_factory=dict)
    leaked_ngrams: list[str] = field(default_factory=list)
    leaked_thread_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _ngrams(text: str, n: int) -> set[str]:
    words = _WORD_RE.findall((text or "").lower())
    grams: set[str] = set()
    for i in range(len(words) - n + 1):
        gram = " ".join(words[i : i + n])
        if len(gram) >= _MIN_NGRAM_CHARS:
            grams.add(gram)
    return grams


class CrossThreadGuard:
    """Guards against leaking other threads' content into a draft."""

    def __init__(self, store: object, settings: object):
        self.store = store
        self.settings = settings
        self.n = int(getattr(settings, "crossthread_ngram_n", 6))
        self.embed_threshold = float(
            getattr(settings, "crossthread_embedding_threshold", 0.85)
        )

    def _other_thread_ngrams(
        self, current_thread_id: str, site_id: str | None = None
    ) -> tuple[set[str], dict[str, str]]:
        """Build an n-gram set from chunks belonging to OTHER threads.

        Returns (ngram_set, gram->thread_id map). Uses only thread-history
        chunks so it never flags shared style-guide / FAQ boilerplate.
        """
        grams: set[str] = set()
        owner: dict[str, str] = {}
        try:
            sql = (
                "SELECT thread_id, text FROM chunks "
                "WHERE source_kind='thread_history' AND thread_id IS NOT NULL "
                "AND thread_id != ?"
            )
            params: list[object] = [current_thread_id]
            if site_id is not None:
                sql += " AND site_id=?"
                params.append(site_id)
            rows = self.store.conn.execute(sql, params).fetchall()  # type: ignore[attr-defined]
        except Exception as e:  # store without chunks table / no DB
            log.debug("cross-thread n-gram source unavailable: %s", e)
            return grams, owner
        for r in rows:
            tid = r["thread_id"]
            for g in _ngrams(r["text"] or "", self.n):
                grams.add(g)
                owner.setdefault(g, tid)
        return grams, owner

    def check(
        self,
        draft_body: str,
        current_thread_id: str,
        bridge: object | None = None,
        draft_embedding: list[float] | None = None,
        site_id: str | None = None,
    ) -> CrossThreadResult:
        """Check whether ``draft_body`` leaks content from another thread.

        The embedding-similarity leg needs a vector for ``draft_body``. Because
        this method is synchronous but the LLM bridge's ``embed`` is async, the
        caller should pre-compute the vector and pass ``draft_embedding`` (the
        agent graph does this). A sync ``bridge.embed`` is also accepted; an
        async one is detected and skipped (n-gram check still runs).
        """
        res = CrossThreadResult(passed=True)

        # ---- 1. n-gram overlap against other threads ----
        draft_grams = _ngrams(draft_body, self.n)
        other_grams, owner = self._other_thread_ngrams(current_thread_id, site_id)
        overlap = sorted(draft_grams & other_grams)
        if overlap:
            res.passed = False
            res.leaked_ngrams = overlap[:20]
            res.leaked_thread_ids = sorted(
                {owner[g] for g in overlap if g in owner}
            )
            res.flags["ngram_overlap"] = float(len(overlap))
            res.reasons.append(
                f"{len(overlap)} distinctive {self.n}-gram(s) match other thread(s): "
                f"{', '.join(res.leaked_thread_ids)}"
            )

        # ---- 2. embedding similarity (needs a pre-computed vector) ----
        if draft_embedding is None and bridge is not None:
            # Accept only a SYNC embedder here; an async coroutine is detected
            # and discarded (the graph supplies draft_embedding instead).
            import inspect

            embed = getattr(bridge, "embed", None)
            if callable(embed) and not inspect.iscoroutinefunction(embed):
                try:
                    out = embed([draft_body])
                    if not inspect.isawaitable(out):
                        draft_embedding = out[0] if out else None
                except Exception:
                    draft_embedding = None
        if draft_embedding is not None:
            try:
                vec = draft_embedding
                if vec:
                    rows = self.store.knn_chunks(  # type: ignore[attr-defined]
                        vec, k=8, thread_id=None, site_id=site_id
                    )
                    leaked_tids: set[str] = set()
                    max_sim = 0.0
                    for r in rows:
                        tid = r["thread_id"]
                        if not tid or tid == current_thread_id:
                            continue
                        # knn_chunks returns chunk rows; recompute via distance
                        # column when present, else treat presence as a hit.
                        dist = r["distance"] if "distance" in r.keys() else 0.0
                        sim = 1.0 - float(dist) if dist else 1.0
                        if sim >= self.embed_threshold:
                            leaked_tids.add(tid)
                            max_sim = max(max_sim, sim)
                    if leaked_tids:
                        res.passed = False
                        res.flags["embedding_similarity"] = round(max_sim, 4)
                        res.leaked_thread_ids = sorted(
                            set(res.leaked_thread_ids) | leaked_tids
                        )
                        res.reasons.append(
                            f"draft embedding ≥{self.embed_threshold} similar to "
                            f"other thread(s): {', '.join(sorted(leaked_tids))}"
                        )
            except Exception as e:  # embedder/vec disabled → n-gram only
                log.debug("cross-thread embedding check skipped: %s", e)

        return res
