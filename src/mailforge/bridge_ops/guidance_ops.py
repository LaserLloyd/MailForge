"""Bridge ops that read and change guidance: policy, references, templates.

Every handler has the bridge signature ``fn(data, site, store, settings) ->
dict`` and is synchronous (handlers that index run their own event loop).
``HANDLERS`` maps the bridge action name to its handler; ``CLASS`` classifies
each as ``"read"`` or ``"guidance_write"`` for the op manifest.

Writes are site-bound, audited (``tool=guidance_write``), versioned (see
:mod:`mailforge.knowledge.guidance`), checked against ``expected_sha256`` and
capped at ``GUIDANCE_WRITES_PER_DAY`` per rolling 24 h. No path, site or
database id other than the caller's own site ever comes from the request.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..audit.log import AuditLog
from ..knowledge import guidance as gd
from ..knowledge.guidance import GuidanceError
from ..response_templates import validate_template_text

GUIDANCE_WRITES_PER_DAY = 30
READ_CHARS_DEFAULT = 100_000
READ_CHARS_MAX = 200_000
OFFLINE_REASON = "the embedding model is offline; indexing is deferred until it is reachable"


# ------------------------------------------------------------------ helpers
def _make_bridge(settings: Any) -> Any:
    from ..llm.bridge import LMStudioBridge

    return LMStudioBridge.from_settings(settings)


def _run(coro: Any) -> Any:
    """Run a coroutine to completion, even if a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=target)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _author(data: dict[str, Any]) -> str:
    return str(data.get("author") or "bridge")[:80]


def _bounded_chars(data: dict[str, Any]) -> int:
    try:
        n = int(data.get("max_chars", READ_CHARS_DEFAULT))
    except (TypeError, ValueError):
        raise GuidanceError("max_chars must be an integer") from None
    if not 1 <= n <= READ_CHARS_MAX:
        raise GuidanceError(f"max_chars must be between 1 and {READ_CHARS_MAX}")
    return n


def _id(data: dict[str, Any], name: str = "id") -> int:
    try:
        value = int(data[name])
    except (KeyError, TypeError, ValueError):
        raise GuidanceError(f"{name} is required and must be an integer") from None
    if value < 1:
        raise GuidanceError(f"{name} must be a positive integer")
    return value


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


def _site_cfg(settings: Any, site: str) -> Any:
    cfg = dict(getattr(settings, "sites", {}) or {}).get(site)
    if cfg is None:
        raise GuidanceError(f"site {site!r} is not configured")
    return cfg


def _check_cap(store: Any) -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    if store.count_audit_tool_calls("guidance_write", since) >= GUIDANCE_WRITES_PER_DAY:
        raise GuidanceError(
            f"daily guidance write cap reached ({GUIDANCE_WRITES_PER_DAY} in 24 h); "
            "leave a note for the owner with note-add instead"
        )


def _audit(
    store: Any, site: str, op: str, kind: str, key: str, old: str | None, new: str | None,
    author: str, subject_table: str | None = None, subject_id: int | None = None,
) -> None:
    body = new if new is not None else (old or "")
    AuditLog(store).record(
        actor="agent", event="tool_call", subject_table=subject_table, subject_id=subject_id,
        detail={
            "tool": "guidance_write", "op": op, "kind": kind, "key": str(key), "site_id": site,
            # 16-hex prefixes: long enough to compare, short enough that the audit
            # redactor never mistakes a digest for a credential.
            "old_sha": gd.sha256_text(old)[:16] if old is not None else "",
            "new_sha": gd.sha256_text(new)[:16] if new is not None else "",
            "bytes": len(body.encode("utf-8")), "author": author,
        },
    )


def _screen(store: Any, text: str, label: str, op: str, kind: str, key: str, author: str) -> None:
    """Secret scan; a hit is refused, counted against the cap and audited."""
    try:
        gd.check_no_secrets(text, label)
    except GuidanceError as e:
        AuditLog(store).record(
            actor="guardrail", event="block",
            detail={"tool": "guidance_write", "op": op, "kind": kind, "key": str(key),
                    "author": author, "reasons": [str(e)[:200]]},
        )
        raise


def guidance_changes_24h(store: Any, since_iso: str | None = None) -> list[dict[str, Any]]:
    """Guidance edits made through the bridge in the last 24 h (for the daily brief)."""
    since = since_iso or (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = store.conn.execute(
        "SELECT ts, detail_json FROM audit_log WHERE event='tool_call' AND ts>=? "
        "AND json_extract(detail_json,'$.tool')='guidance_write' ORDER BY id",
        (since,),
    ).fetchall()
    out = []
    for row in rows:
        d = json.loads(row["detail_json"] or "{}")
        out.append({"ts": row["ts"], **{k: d.get(k) for k in (
            "op", "kind", "key", "site_id", "author", "old_sha", "new_sha", "bytes")}})
    return out


# ---------------------------------------------------- regeneration + index
def _index_status(report: dict[str, Any]) -> tuple[bool, str]:
    if "error" in report:
        return False, str(report["error"])[:300]
    if "skipped" in report:
        return False, str(report["skipped"])
    docs = report.get("documents") or {}
    bad = [str(d.get("status")) for d in docs.values()
           if not (str(d.get("status")).startswith("indexed") or d.get("status") == "unchanged")]
    if bad:
        return False, OFFLINE_REASON if any("deferred" in b for b in bad) else "; ".join(bad)[:300]
    return True, ""


def _refresh(store: Any, settings: Any, site: str) -> dict[str, Any]:
    from ..knowledge.sync import refresh_managed_knowledge

    bridge = _make_bridge(settings)
    report = _run(refresh_managed_knowledge(store, bridge, sites=[site]))
    site_report = report.get(site, {})
    indexed, reason = _index_status(site_report)
    regenerated = "error" not in site_report and "skipped" not in site_report
    return {
        "regenerated": regenerated,
        "indexed": indexed,
        "reason": reason,
        "documents": site_report.get("documents", {}),
    }


def _index_reference(store: Any, settings: Any, doc_id: int) -> dict[str, Any]:
    from ..knowledge.sync import _is_up
    from ..rag.embedder import ingest_reference_document

    async def go() -> dict[str, Any]:
        bridge = _make_bridge(settings)
        if not await _is_up(bridge):
            store.update_reference_document_status(
                doc_id, "ERROR", error_text=OFFLINE_REASON, chunk_count=0)
            return {"indexed": False, "reason": OFFLINE_REASON}
        try:
            count = int(await ingest_reference_document(store, bridge, doc_id))
        except Exception as e:  # noqa: BLE001 - ingest already marked the row ERROR
            return {"indexed": False, "reason": str(e)[:300]}
        return {"indexed": True, "reason": "", "chunks": count}

    return _run(go())


# ------------------------------------------------------------------ policy
def _policy_file(settings: Any, site: str) -> Path:
    return gd.policy_path(_site_cfg(settings, site), site)


def _policy_write(
    data: dict[str, Any], site: str, store: Any, settings: Any, *, op: str,
    text: Any, expected: Any,
) -> dict[str, Any]:
    author = _author(data)
    _check_cap(store)
    path = _policy_file(settings, site)
    new = gd.normalise_text(text, cap_bytes=gd.POLICY_MAX_BYTES, label="policy")
    if not new.strip():
        raise GuidanceError("policy text is empty")
    current = gd.read_if_exists(path)
    gd.require_current(expected, current, "policy")
    if current is not None and gd.DIGEST_MARKER in current and gd.DIGEST_MARKER not in new:
        raise GuidanceError(
            f"the policy must keep the {gd.DIGEST_MARKER} token: the handbook generator "
            "fills it with the source digest"
        )
    _screen(store, new, "policy", op, "policy", "policy", author)
    version = None
    mode = 0o600
    if current is not None:
        version = gd.write_history(site, "policy", "policy", current)
        mode = os.stat(path).st_mode & 0o777
    gd.atomic_write(path, new, mode)
    _audit(store, site, op, "policy", "policy", current, new, author)
    result = {
        "site_id": site, "kind": "policy", "old_sha256": gd.sha256_text(current) if current is not None else "",
        "new_sha256": gd.sha256_text(new), "bytes": len(new.encode("utf-8")),
        "backup_version": version,
    }
    result.update(_refresh(store, settings, site))
    return result


def _policy_save(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    return _policy_write(data, site, store, settings, op="policy-save",
                         text=data.get("text"), expected=data.get("expected_sha256"))


# ------------------------------------------------------------------- reads
def _guidance(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    kind = str(data.get("kind") or "").strip().lower()
    limit = _bounded_chars(data)
    if kind == "policy":
        text = gd.read_if_exists(_policy_file(settings, site))
        if text is None:
            raise GuidanceError("the policy file does not exist yet")
    elif kind in ("handbook", "full-text"):
        from ..knowledge.handbooks import read_site_handbook

        try:
            text = read_site_handbook(site, full=(kind == "full-text"))
        except FileNotFoundError:
            raise GuidanceError(
                "the handbook has not been generated yet; run guidance-refresh") from None
    else:
        raise GuidanceError("kind must be policy, handbook or full-text")
    shown, truncated = _truncate(text, limit)
    return {"site_id": site, "kind": kind, "text": shown, "truncated": truncated,
            "sha256": gd.sha256_text(text), "bytes": len(text.encode("utf-8"))}


def _guidance_refresh(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    _check_cap(store)
    _site_cfg(settings, site)
    result = {"site_id": site, **_refresh(store, settings, site)}
    _audit(store, site, "guidance-refresh", "handbook", site, None, None, _author(data))
    return result


# --------------------------------------------------------------- templates
_TPL_FIELDS = ("name", "category", "subject", "body", "shortcut")


def _tpl_text(row: Any) -> str:
    return json.dumps({k: str(row[k] or "") for k in _TPL_FIELDS}, sort_keys=True,
                      ensure_ascii=False)


def _tpl_row(store: Any, site: str, template_id: int) -> Any:
    row = store.get_response_template(template_id, site)
    if row is None:
        raise GuidanceError("template not found for this site")
    return row


def _tpl_summary(row: Any) -> dict[str, Any]:
    return {"id": int(row["id"]), "name": row["name"], "category": row["category"],
            "shortcut": row["shortcut"] or "", "subject": row["subject"] or "",
            "is_system": bool(row["is_system"]), "updated_at": row["updated_at"],
            "sha256": gd.sha256_text(_tpl_text(row))}


def _templates(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    return {"site_id": site,
            "templates": [_tpl_summary(r) for r in store.list_response_templates(site)]}


def _template(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    row = _tpl_row(store, site, _id(data))
    return {**_tpl_summary(row), "body": row["body"] or "", "site_id": site}


def _tpl_fields(data: dict[str, Any]) -> dict[str, str]:
    def one(name: str, cap: int, single: bool, required: bool = False) -> str:
        value = gd.normalise_text(
            str(data.get(name) or "").strip(), cap_bytes=cap * 4, label=name, single_line=single)
        if len(value) > cap:
            raise GuidanceError(f"{name} is too long (limit {cap} characters)")
        if required and not value:
            raise GuidanceError(f"{name} is required")
        return value

    fields = {
        "name": one("name", 120, True, True),
        "category": one("category", 60, True) or "General",
        "subject": one("subject", gd.TEMPLATE_SUBJECT_MAX, True),
        "body": one("body", gd.TEMPLATE_BODY_MAX, False, True),
        "shortcut": one("shortcut", 40, True),
    }
    validate_template_text(fields["subject"])
    validate_template_text(fields["body"])
    return fields


def _template_write(
    data: dict[str, Any], site: str, store: Any, *, op: str, template_id: int | None,
    expected: Any,
) -> dict[str, Any]:
    author = _author(data)
    _check_cap(store)
    fields = _tpl_fields(data)
    key = str(template_id) if template_id is not None else "new"
    _screen(store, fields["subject"] + "\n" + fields["body"], "template", op, "template", key, author)
    new_text = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    old_text = None
    if template_id is None:
        new_id = store.create_response_template(
            site, fields["name"], fields["category"], fields["subject"], fields["body"],
            fields["shortcut"])
    else:
        row = _tpl_row(store, site, template_id)
        old_text = _tpl_text(row)
        gd.require_current(expected, old_text, "template")
        gd.write_history(site, "template", str(template_id), old_text)
        store.update_response_template(
            template_id, site, fields["name"], fields["category"], fields["subject"],
            fields["body"], fields["shortcut"])
        new_id = template_id
    _audit(store, site, op, "template", str(new_id), old_text, new_text, author,
           "response_templates", new_id)
    return {"id": new_id, "site_id": site, "created": template_id is None,
            "sha256": gd.sha256_text(_tpl_text(_tpl_row(store, site, new_id)))}


def _template_save(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    template_id = _id(data) if data.get("id") not in (None, "") else None
    return _template_write(data, site, store, op="template-save", template_id=template_id,
                           expected=data.get("expected_sha256"))


def _template_delete(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    template_id = _id(data)
    author = _author(data)
    _check_cap(store)
    row = _tpl_row(store, site, template_id)
    if row["is_system"]:
        raise GuidanceError("system templates cannot be deleted")
    old_text = _tpl_text(row)
    gd.write_history(site, "template", str(template_id), old_text)
    if not store.delete_response_template(template_id, site):
        raise GuidanceError("template could not be deleted")
    _audit(store, site, "template-delete", "template", str(template_id), old_text, None, author,
           "response_templates", template_id)
    return {"id": template_id, "site_id": site, "deleted": True}


# -------------------------------------------------------------- references
def _ref_row(store: Any, site: str, ref_id: int) -> Any:
    row = store.get_reference_document(ref_id)
    if row is None or row["site_id"] != site:
        raise GuidanceError("reference document not found for this site")
    return row


def _ref_text(row: Any) -> str | None:
    path = Path(row["stored_path"])
    if not gd.inside_data_dir(path):
        raise GuidanceError("the stored file is outside the application data directory")
    if not path.is_file():
        return None
    if path.suffix.lower() in gd.REFERENCE_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")
    from ..rag.embedder import _read_document

    return _read_document(path)


def _ref_summary(row: Any, text: str | None) -> dict[str, Any]:
    return {
        "id": int(row["id"]), "filename": row["filename"], "status": row["status"],
        "chunk_count": int(row["chunk_count"] or 0), "size_bytes": int(row["size_bytes"] or 0),
        "managed": row["managed_key"] is not None, "updated_at": row["updated_at"],
        "sha256": gd.sha256_text(text) if text is not None else "",
    }


def _references(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    out = []
    for row in store.list_reference_documents(site):
        try:
            text = _ref_text(row)
        except GuidanceError:
            text = None
        out.append(_ref_summary(row, text))
    return {"site_id": site, "references": out}


def _reference(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    row = _ref_row(store, site, _id(data))
    limit = _bounded_chars(data)
    text = _ref_text(row)
    if text is None:
        raise GuidanceError("the document file is missing")
    shown, truncated = _truncate(text, limit)
    return {**_ref_summary(row, text), "site_id": site, "text": shown, "truncated": truncated}


def _set_size(store: Any, ref_id: int, size: int) -> None:
    store.conn.execute(
        "UPDATE reference_documents SET size_bytes=?, updated_at=? WHERE id=?",
        (size, datetime.now(timezone.utc).isoformat(), ref_id))
    store.conn.commit()


def _reference_write(
    data: dict[str, Any], site: str, store: Any, settings: Any, *, op: str,
    ref_id: int | None, text: Any, expected: Any,
) -> dict[str, Any]:
    author = _author(data)
    _check_cap(store)
    new = gd.normalise_text(text, cap_bytes=gd.REFERENCE_MAX_BYTES, label="reference text")
    if not new.strip():
        raise GuidanceError("reference text is empty")
    key = str(ref_id) if ref_id is not None else "new"
    if ref_id is None:
        name = gd.safe_reference_name(data.get("filename"))
        _screen(store, new, "reference text", op, "reference", key, author)
        target = gd.references_root(site) / f"{uuid.uuid4().hex}-{name}"
        gd.atomic_write(target, new)
        mime = "text/markdown" if name.lower().endswith(".md") else "text/plain"
        try:
            ref_id = int(store.create_reference_document(
                site, name, str(target), mime, len(new.encode("utf-8"))))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        old = None
    else:
        row = _ref_row(store, site, ref_id)
        if row["managed_key"] is not None:
            raise GuidanceError(
                "managed documents are generated; edit the site policy (policy-save) instead")
        path = Path(row["stored_path"])
        if path.suffix.lower() not in gd.REFERENCE_SUFFIXES:
            raise GuidanceError("only .md and .txt references can be edited")
        old = _ref_text(row)
        if old is None:
            raise GuidanceError("the document file is missing")
        gd.require_current(expected, old, "reference")
        _screen(store, new, "reference text", op, "reference", key, author)
        gd.write_history(site, "reference", str(ref_id), old)
        gd.atomic_write(path, new)
        _set_size(store, ref_id, len(new.encode("utf-8")))
        store.update_reference_document_status(ref_id, "UPLOADED", chunk_count=0)
    _audit(store, site, op, "reference", str(ref_id), old, new, author,
           "reference_documents", ref_id)
    return {"id": ref_id, "site_id": site, "created": old is None,
            "sha256": gd.sha256_text(new), **_index_reference(store, settings, ref_id)}


def _reference_save(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    ref_id = _id(data) if data.get("id") not in (None, "") else None
    return _reference_write(data, site, store, settings, op="reference-save", ref_id=ref_id,
                            text=data.get("text"), expected=data.get("expected_sha256"))


def _reference_delete(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    ref_id = _id(data)
    author = _author(data)
    _check_cap(store)
    row = _ref_row(store, site, ref_id)
    if row["managed_key"] is not None:
        raise GuidanceError("managed documents cannot be deleted; they are regenerated")
    old = _ref_text(row)
    if old is not None:
        gd.write_history(site, "reference", str(ref_id), old)
    if not store.delete_reference_document(ref_id):
        raise GuidanceError("reference could not be deleted")
    _audit(store, site, "reference-delete", "reference", str(ref_id), old, None, author,
           "reference_documents", ref_id)
    return {"id": ref_id, "site_id": site, "deleted": True}


def _reference_reindex(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    ref_id = _id(data)
    _check_cap(store)
    row = _ref_row(store, site, ref_id)
    result = _index_reference(store, settings, int(row["id"]))
    _audit(store, site, "reference-reindex", "reference", str(ref_id), None, None, _author(data),
           "reference_documents", ref_id)
    return {"id": ref_id, "site_id": site, **result}


# ----------------------------------------------------------- history/revert
def _address(data: dict[str, Any]) -> tuple[str, str]:
    kind = str(data.get("kind") or "").strip().lower()
    if kind == "policy":
        return kind, "policy"
    if kind in ("reference", "template"):
        return kind, str(_id(data))
    raise GuidanceError("kind must be policy, reference or template")


def _guidance_history(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    kind, key = _address(data)
    if data.get("version"):
        text = gd.read_history(site, kind, key, str(data["version"]))
        shown, truncated = _truncate(text, _bounded_chars(data))
        return {"site_id": site, "kind": kind, "key": key, "version": data["version"],
                "text": shown, "truncated": truncated, "sha256": gd.sha256_text(text)}
    return {"site_id": site, "kind": kind, "key": key,
            "versions": gd.list_history(site, kind, key)}


def _guidance_revert(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    kind, key = _address(data)
    old = gd.read_history(site, kind, key, str(data.get("version") or ""))
    if kind == "policy":
        current = gd.read_if_exists(_policy_file(settings, site))
        return _policy_write(data, site, store, settings, op="guidance-revert", text=old,
                             expected=gd.sha256_text(current) if current is not None else None)
    if kind == "template":
        row = _tpl_row(store, site, int(key))  # a deleted template is recreated by template-save
        try:
            fields = json.loads(old)
        except ValueError:
            raise GuidanceError("that history version is not a template") from None
        return _template_write(
            {**fields, "author": data.get("author")}, site, store, op="guidance-revert",
            template_id=int(key), expected=gd.sha256_text(_tpl_text(row)))
    row = _ref_row(store, site, int(key))
    current = _ref_text(row)
    return _reference_write(data, site, store, settings, op="guidance-revert", ref_id=int(key),
                            text=old, expected=gd.sha256_text(current) if current is not None else None)


# ---------------------------------------------------------------- registry
HANDLERS: dict[str, Callable[[dict[str, Any], str, Any, Any], dict[str, Any]]] = {
    "guidance": _guidance,
    "policy-save": _policy_save,
    "guidance-refresh": _guidance_refresh,
    "guidance-history": _guidance_history,
    "guidance-revert": _guidance_revert,
    "templates": _templates,
    "template": _template,
    "template-save": _template_save,
    "template-delete": _template_delete,
    "references": _references,
    "reference": _reference,
    "reference-save": _reference_save,
    "reference-delete": _reference_delete,
    "reference-reindex": _reference_reindex,
}

CLASS: dict[str, str] = {
    "guidance": "read",
    "guidance-history": "read",
    "templates": "read",
    "template": "read",
    "references": "read",
    "reference": "read",
    "policy-save": "guidance_write",
    "guidance-refresh": "guidance_write",
    "guidance-revert": "guidance_write",
    "template-save": "guidance_write",
    "template-delete": "guidance_write",
    "reference-save": "guidance_write",
    "reference-delete": "guidance_write",
    "reference-reindex": "guidance_write",
}
