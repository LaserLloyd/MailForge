"""LM Studio bridge (build spec §6).

Wraps the local LM Studio runtime. The ``lmstudio`` SDK is an OPTIONAL
dependency (the ``llm`` extra) and may be absent; everything here is
import-guarded so the package imports cleanly without it.

Two transports are used:

* The OpenAI-compat ``/v1`` endpoints via ``httpx`` (a core dependency) for
  chat, structured output (``response_format`` JSON schema), and embeddings.
  This works even when the native SDK lacks deep structured-output support.
* The native ``lmstudio`` SDK where it adds value (JIT model load with TTL +
  GPU offload + flash-attention, downloaded-model enumeration for the picker).

When LM Studio is down or the SDK is missing, methods return safe sentinels
and :meth:`LMStudioBridge.is_up` returns ``False`` — this drives the
``DEFERRED_NO_LLM`` degradation path (spec §5: ingestion continues, drafts are
deferred, never lost).
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Settings

log = logging.getLogger(__name__)

# Import-guard the OPTIONAL lmstudio SDK (the ``llm`` extra). The package must
# import and the bridge must function (degraded) when it is not installed.
try:  # pragma: no cover - exercised by extra presence
    import lmstudio as lms

    _HAVE_LMS = True
except Exception:  # ImportError, or SDK import-time errors
    lms = None  # type: ignore[assignment]
    _HAVE_LMS = False

# Embedding dimension required to match the vec0 FLOAT[768] column (spec §6/§7).
EMBED_DIM = 768


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a self-contained copy of ``schema`` with ``$ref`` resolved against
    ``$defs`` and the ``$defs`` block removed.

    Pydantic emits ``$ref``/``$defs`` for enums and nested models, but LM
    Studio's ``json_schema`` response-format validator rejects them (HTTP 400).
    We dereference recursively (cyclic refs collapse to ``{}``).
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                key = ref.split("/")[-1]
                if key in seen or key not in defs:
                    return {}
                return resolve(defs[key], seen | {key})
            return {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v, seen) for v in node]
        return node

    return resolve(schema, frozenset())


def _normalize_host(host: str) -> str:
    """Return a bare ``host:port`` (no scheme) for the SDK, tolerating URLs."""
    h = host.strip()
    for prefix in ("http://", "https://"):
        if h.startswith(prefix):
            h = h[len(prefix) :]
    return h.rstrip("/")


class LMStudioBridge:
    """Thin async bridge to LM Studio (spec §6).

    Parameters
    ----------
    host:
        ``host:port`` of the LM Studio server (e.g. ``localhost:1234``).
    model_id:
        Chat model identifier (e.g. ``qwen/qwen3-4b-2507``).
    embed_id:
        Embedding model identifier (768-dim, e.g.
        ``text-embedding-nomic-embed-text-v1.5``).
    """

    def __init__(
        self,
        host: str,
        model_id: str,
        embed_id: str,
        *,
        ttl_seconds: int = 1800,
        context_length: int = 8192,
        timeout: float = 120.0,
        heavy_base_url: str = "",
        heavy_model: str = "",
        heavy_key: str = "",
    ) -> None:
        self.host = _normalize_host(host)
        self.model_id = model_id
        self.embed_id = embed_id
        self.ttl_seconds = ttl_seconds
        self.context_length = context_length
        self.timeout = timeout
        # Optional "OpenClaw link" heavy-lift route (OpenAI-compatible). Empty
        # base_url/model => disabled, app runs purely on the local LM Studio.
        self.heavy_base_url = heavy_base_url.rstrip("/")
        self.heavy_model = heavy_model
        self.heavy_key = heavy_key

    @property
    def has_heavy(self) -> bool:
        """True iff an OpenClaw heavy-lift endpoint is configured."""
        return bool(self.heavy_base_url and self.heavy_model)

    # ----- constructors -----
    @classmethod
    def from_settings(cls, settings: "Settings") -> "LMStudioBridge":
        """Build a bridge from :class:`~openclaw_email.config.LLMSettings`
        plus the optional OpenClaw heavy-lift link."""
        import os

        llm = settings.llm
        oc = getattr(settings, "openclaw", None)
        heavy_base = heavy_model = heavy_key = ""
        if oc is not None and getattr(oc, "enabled", False):
            heavy_base = oc.heavy_lift_base_url or ""
            heavy_model = oc.heavy_lift_model or ""
            if heavy_base and oc.heavy_lift_key_env:
                heavy_key = os.environ.get(oc.heavy_lift_key_env, "") or ""
        return cls(
            host=llm.lm_studio_host,
            model_id=llm.chat_model,
            embed_id=llm.embedding_model,
            ttl_seconds=llm.ttl_seconds,
            context_length=llm.context_length,
            heavy_base_url=heavy_base,
            heavy_model=heavy_model,
            heavy_key=heavy_key,
        )

    # ----- transport helpers -----
    @property
    def base_url(self) -> str:
        """OpenAI-compat ``/v1`` base URL for ``httpx`` calls."""
        return f"http://{self.host}/v1"

    # ----- health -----
    async def is_up_async(self) -> bool:
        """:meth:`is_up` off the event loop (it is a blocking 5 s HTTP probe)."""
        import asyncio

        return await asyncio.to_thread(self.is_up)

    def is_up(self) -> bool:
        """Return True iff the LM Studio server answers (spec §6).

        Prefers the SDK's host validator; otherwise pings the OpenAI-compat
        ``/v1/models`` endpoint. Any failure → ``False`` (DEFERRED_NO_LLM).
        """
        # Probe the API actually used for chat and embeddings first. The native
        # SDK validator is both stricter and dramatically slower for healthy
        # remote servers.
        try:
            r = httpx.get(f"{self.base_url}/models", timeout=5.0)
            return r.status_code == 200
        except Exception as e:
            log.debug("LM Studio /v1/models probe failed: %s", e)
            return False

    # ----- native SDK session (JIT load w/ TTL, spec §6) -----
    @asynccontextmanager
    async def session(self) -> AsyncIterator[tuple[Any, Any]]:
        """Yield ``(llm, emb)`` SDK model handles with JIT-load config.

        Config mirrors spec §6: ``ttl`` (idle auto-evict), ``gpu_offload=max``,
        ``context_length``, ``flash_attention=True``. Raises ``RuntimeError``
        if the SDK is unavailable — callers that must degrade gracefully should
        gate on :meth:`is_up` first or use the HTTP helpers below.
        """
        if not _HAVE_LMS:
            raise RuntimeError(
                "lmstudio SDK not installed; install the 'llm' extra "
                "or use the httpx-based chat/embed helpers"
            )
        async with lms.AsyncClient(self.host) as client:
            llm = await client.llm.model(
                self.model_id,
                config={
                    "ttl": self.ttl_seconds,
                    "gpu_offload": "max",
                    "context_length": self.context_length,
                    "flash_attention": True,
                },
            )
            emb = await client.embedding.model(self.embed_id)
            yield llm, emb

    async def list_models(self) -> list[dict[str, Any]]:
        """List local models for the model-picker page (spec §6/§9).

        Uses the SDK's ``list_downloaded_models`` when available, else the
        OpenAI-compat ``/v1/models`` endpoint. Returns ``[]`` when down.
        """
        # Prefer the fast API used by the runtime. Native SDK inventory can
        # take minutes to fail against an otherwise healthy remote host.
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(f"{self.base_url}/models")
                r.raise_for_status()
                return list(r.json().get("data", []))
        except Exception as e:
            log.info("/v1 model inventory unavailable, trying native SDK: %s", e)
        if _HAVE_LMS:
            try:
                async with lms.AsyncClient(self.host) as c:
                    models = await c.system.list_downloaded_models()
                    out: list[dict[str, Any]] = []
                    for m in models:
                        if isinstance(m, dict):
                            out.append(m)
                        else:
                            out.append(
                                {
                                    "model_key": getattr(m, "model_key", None)
                                    or getattr(m, "path", None)
                                    or str(m),
                                    "type": getattr(m, "type", None),
                                }
                            )
                    return out
            except Exception as e:
                log.info("list_models unavailable (LM Studio down?): %s", e)
        return []

    # ----- high-level chat (spec §6) -----
    async def chat(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        *,
        temperature: float = 0.2,
    ) -> str:
        """Plain chat completion via OpenAI-compat ``/v1/chat/completions``.

        ``response_format`` is passed through verbatim when given (e.g.
        ``{"type": "json_object"}`` for json_mode). Returns the assistant
        message content, or ``""`` when LM Studio is unreachable (sentinel for
        the DEFERRED_NO_LLM path).
        """
        return await self._post_chat(
            self.base_url,
            self.model_id,
            messages,
            response_format=response_format,
            temperature=temperature,
        )

    async def _post_chat(
        self,
        base_url: str,
        model: str,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.2,
        api_key: str = "",
    ) -> str:
        """POST one OpenAI-compat chat completion to ``base_url``.

        Shared by the local LM Studio path and the optional OpenClaw heavy-lift
        path (which differs only in URL, model, and a Bearer key). Returns the
        assistant content, or ``""`` on any failure (sentinel → caller defers
        or falls back).
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(
                    f"{base_url}/chat/completions", json=payload, headers=headers
                )
                r.raise_for_status()
                data = r.json()
            msg = data["choices"][0]["message"]
            # Reasoning models may leave ``content`` empty and place the answer
            # (including structured JSON) in ``reasoning_content``; fall back to
            # it so downstream JSON extraction still works.
            return msg.get("content") or msg.get("reasoning_content") or ""
        except Exception as e:
            log.warning("chat POST to %s failed: %s", base_url, e)
            return ""

    async def _chat_heavy(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        *,
        temperature: float = 0.2,
    ) -> str:
        """Chat via the OpenClaw heavy-lift endpoint (caller checks has_heavy)."""
        return await self._post_chat(
            self.heavy_base_url,
            self.heavy_model,
            messages,
            response_format=response_format,
            temperature=temperature,
            api_key=self.heavy_key,
        )

    async def chat_structured(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        *,
        schema_name: str = "structured_output",
        temperature: float = 0.1,
        heavy: bool = False,
    ) -> dict[str, Any]:
        """Strict-JSON chat via ``response_format`` JSON schema (spec §6).

        Tries the OpenAI-compat ``json_schema`` response format first. Weak
        local models that don't honour schemas fall back to ``json_object``
        (json_mode) with the schema injected into the prompt — pattern copied
        from ``microsoft/local-email-agent``. Returns ``{}`` when down or
        unparseable (safe sentinel → caller defers).

        ``heavy=True`` (used by the draft node) prefers the OpenClaw heavy-lift
        endpoint when one is configured; if it is absent, unreachable, or
        returns nothing usable, this transparently falls back to the local LM
        Studio model — so the heavy link is purely additive.
        """
        from .structured import coerce_dict

        # LM Studio's structured-output validator does not accept ``$ref`` /
        # ``$defs`` (Pydantic emits these for enums/nested models). Inline them
        # into a self-contained schema before sending, else we get HTTP 400.
        inlined = _inline_refs(json_schema)

        rf: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": inlined},
        }
        schema_hint = {
            "role": "system",
            "content": (
                "Respond with a SINGLE JSON object that conforms exactly to "
                "this JSON Schema. Output JSON only, no prose, no code fences:\n"
                + json.dumps(inlined)
            ),
        }

        # Optional heavy-lift attempt (OpenClaw, when online). Best-effort: any
        # empty/error result falls through to the local model below.
        if heavy and self.has_heavy:
            raw_h = await self._chat_heavy(messages, response_format=rf, temperature=temperature)
            parsed_h = coerce_dict(raw_h)
            if not parsed_h:
                raw_h = await self._chat_heavy(
                    [schema_hint, *messages],
                    response_format={"type": "json_object"},
                    temperature=temperature,
                )
                parsed_h = coerce_dict(raw_h)
            if parsed_h:
                return parsed_h
            log.info("heavy-lift endpoint gave nothing usable; falling back to local model")

        # Attempt 1: native json_schema response_format (local LM Studio).
        raw = await self.chat(messages, response_format=rf, temperature=temperature)
        parsed = coerce_dict(raw)
        if parsed:
            return parsed

        # Attempt 2: json_mode fallback for weak models. Inject the schema so
        # the model knows the shape, then constrain output to a JSON object.
        log.info("json_schema response empty/unparseable; falling back to json_mode")
        raw2 = await self.chat(
            [schema_hint, *messages],
            response_format={"type": "json_object"},
            temperature=temperature,
        )
        return coerce_dict(raw2)

    # ----- standalone robustness: adapt to whatever LM Studio is serving -----
    async def resolve_served_models(self) -> dict[str, Any]:
        """Best-effort: if the configured chat/embedding model is not served by
        this LM Studio instance, fall back to a served one so the app works on
        *any* machine with LM Studio running (spec goal: free-standing client).

        Mutates ``self.model_id`` / ``self.embed_id`` in place and returns a
        report ``{"chat": ..., "embed": ..., "changed": bool, "served": [...]}``.
        Never raises; a down server leaves the configured ids untouched.
        """
        report: dict[str, Any] = {
            "chat": self.model_id,
            "embed": self.embed_id,
            "changed": False,
            "served": [],
        }
        try:
            models = await self.list_models()
        except Exception:
            return report
        ids = [
            (m.get("id") or m.get("model_key") or m.get("path") or "")
            for m in models
            if isinstance(m, dict)
        ]
        ids = [i for i in ids if i]
        report["served"] = ids
        if not ids:
            return report  # server down / empty — keep configured ids

        def _served(name: str) -> bool:
            return any(name == i or name in i for i in ids)

        def _family(name: str) -> str:
            # First identifier token, e.g. "qwen/qwen3.6-35b" -> "qwen".
            base = name.split("/")[0] if "/" in name else name
            return base.split("-")[0].lower()

        # Chat model: prefer the configured one; else a served model of the same
        # family (e.g. another qwen) before any non-embedding model.
        if not _served(self.model_id):
            fam = _family(self.model_id)
            non_embed = [i for i in ids if "embed" not in i.lower()]
            cand = next(
                (i for i in non_embed if _family(i) == fam), None
            ) or next(iter(non_embed), None)
            if cand:
                log.warning(
                    "configured chat_model %r not served; using %r", self.model_id, cand
                )
                self.model_id = cand
                report["chat"] = cand
                report["changed"] = True
        # Embedding model: prefer configured; else first id mentioning 'embed'.
        if not _served(self.embed_id):
            cand = next((i for i in ids if "embed" in i.lower()), None)
            if cand:
                log.warning(
                    "configured embedding_model %r not served; using %r",
                    self.embed_id,
                    cand,
                )
                self.embed_id = cand
                report["embed"] = cand
                report["changed"] = True
        return report

    # ----- embeddings (spec §6: 768-dim) -----
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` via OpenAI-compat ``/v1/embeddings`` (768-dim).

        Returns one vector per input, in order. Returns ``[]`` when LM Studio
        is unreachable so RAG ingest/retrieve degrade gracefully (spec §4).
        """
        if not texts:
            return []
        # LM Studio rejects very large multi-input requests even when every
        # individual chunk is within the model context window. Keep batches
        # bounded, preserve order, and fail atomically so a partial document is
        # never committed as READY.
        batch_size = 32
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                async def _request_batch(batch: list[str]) -> list[list[float]]:
                    try:
                        payload = {"model": self.embed_id, "input": batch}
                        r = await c.post(f"{self.base_url}/embeddings", json=payload)
                        r.raise_for_status()
                        data = r.json()
                        items = sorted(data["data"], key=lambda d: d.get("index", 0))
                        current = [list(item["embedding"]) for item in items]
                        if len(current) != len(batch):
                            raise RuntimeError("embedding endpoint returned a partial batch")
                        return current
                    except Exception:
                        # LM Studio can reject a particular multi-input request
                        # while accepting both smaller halves. Split down to a
                        # singleton before declaring the whole document failed.
                        if len(batch) <= 1:
                            raise
                        midpoint = len(batch) // 2
                        log.info(
                            "embedding batch of %d failed; retrying as %d + %d",
                            len(batch), midpoint, len(batch) - midpoint,
                        )
                        return (
                            await _request_batch(batch[:midpoint])
                            + await _request_batch(batch[midpoint:])
                        )

                vectors: list[list[float]] = []
                for offset in range(0, len(texts), batch_size):
                    batch = texts[offset : offset + batch_size]
                    # Indices are local to each request; append batches in the
                    # original request order.
                    vectors.extend(await _request_batch(batch))
            for v in vectors:
                if len(v) != EMBED_DIM:
                    log.warning(
                        "embedding dim %d != expected %d (model %s)",
                        len(v),
                        EMBED_DIM,
                        self.embed_id,
                    )
            return vectors
        except Exception as e:
            log.warning("embed() failed (LM Studio down?): %s", e)
            return []
