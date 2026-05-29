"""Structured-output schemas and JSON coercion (build spec §5/§6).

These typed outputs implement the Plan-Then-Execute rule (spec §5): sensitive
extractions (recipient address, sender-trust bool) come back as typed schemas
from quarantined worker calls, NEVER as free text fed back to the planner.

The :func:`coerce` / :func:`coerce_dict` helpers robustly parse the messy JSON
that weak local models emit (code fences, leading prose, trailing junk).
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)


class Category(str, Enum):
    """Triage categories (spec §5 — quarantined LLM returns this enum)."""

    RESPOND = "RESPOND"
    NOTIFY = "NOTIFY"
    ARCHIVE = "ARCHIVE"
    SPAM = "SPAM"
    MEETING = "MEETING"
    FYI = "FYI"


class Classification(BaseModel):
    """Output of the classify node (spec §5)."""

    category: Category
    priority: int = Field(ge=0, le=5, description="0=lowest .. 5=highest")
    rationale: str


class ProposedDraft(BaseModel):
    """Output of the draft node (spec §5).

    ``recipient`` is still bound to thread participants / allowlist downstream
    (spec §0.4 / §5); the model proposing it does not grant send capability.
    """

    recipient: str
    subject: str
    body: str
    rationale: str


class RecipientExtraction(BaseModel):
    """Sensitive extraction returned as a typed schema, not free text (spec §5)."""

    email_address: str
    sender_trusted: bool


# --------------------------------------------------------------------------- #
# JSON schema generation
# --------------------------------------------------------------------------- #
def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Return the JSON Schema for ``model`` for ``response_format`` (spec §6).

    Enables ``additionalProperties: false`` and inlines ``$defs`` references
    where possible so strict-schema-mode local backends accept it.
    """
    schema = model.model_json_schema()
    _harden_schema(schema)
    return schema


def _harden_schema(schema: dict[str, Any]) -> None:
    """Recursively set ``additionalProperties: false`` on object schemas."""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object" or "properties" in schema:
        schema.setdefault("additionalProperties", False)
    for key in ("properties", "$defs", "definitions"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            for v in sub.values():
                _harden_schema(v)
    for key in ("items", "additionalProperties"):
        v = schema.get(key)
        if isinstance(v, dict):
            _harden_schema(v)
    for key in ("anyOf", "oneOf", "allOf"):
        for v in schema.get(key, []) or []:
            _harden_schema(v)


# --------------------------------------------------------------------------- #
# Robust parsing of messy local-model JSON
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$", re.MULTILINE)


def _extract_json_object(text: str) -> str | None:
    """Find the first balanced ``{...}`` object in ``text``.

    Handles strings/escapes so braces inside string literals don't confuse the
    bracket matcher.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def coerce_dict(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    """Best-effort parse of ``raw`` into a dict.

    Accepts an already-parsed dict, or a string that may contain code fences,
    surrounding prose, or trailing junk. Returns ``{}`` on failure (safe
    sentinel — callers treat empty as "no structured output").
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}

    # 1) Direct parse.
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        pass

    # 2) Strip code fences, retry.
    stripped = _FENCE_RE.sub("", raw).strip()
    if stripped != raw:
        try:
            val = json.loads(stripped)
            return val if isinstance(val, dict) else {}
        except Exception:
            pass

    # 3) Extract the first balanced JSON object.
    candidate = _extract_json_object(stripped or raw)
    if candidate:
        try:
            val = json.loads(candidate)
            return val if isinstance(val, dict) else {}
        except Exception:
            pass
    return {}


def coerce(raw: str | dict[str, Any], model: type[T]) -> T:
    """Parse messy model output into a validated ``model`` instance (spec §5).

    Raises ``ValueError`` if the payload can't be coerced/validated, so the
    caller can defer rather than act on garbage (Plan-Then-Execute safety).
    """
    data = coerce_dict(raw)
    if not data:
        raise ValueError(f"could not extract JSON object for {model.__name__}")
    try:
        return model.model_validate(data)
    except Exception as e:  # pydantic ValidationError or otherwise
        raise ValueError(f"{model.__name__} validation failed: {e}") from e
