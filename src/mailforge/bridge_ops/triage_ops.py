"""Triage and read operations for bridge agents.

Everything here is site-bound, audited (``actor=agent``, ``tool=<op>``) and
limited to mail an agent may read ("Gate R": not quarantined, screening status
AI-readable), except *restoring* from Trash. Nothing here can send, delete
permanently, release quarantine, mark spam/content-related or change settings:
those stay with the human in the MailForge UI. ``test_bridge_ops_manifest.py``
enforces that with an AST scan.

``bridge_cli`` is imported lazily (it imports this module at load time).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..audit.log import AuditLog
from ..security.inbound_screening import ai_may_read

#: Message-level triage writes an agent may make per rolling 24 h.
TRIAGE_PER_DAY = 200
#: Ids per bulk call.
BULK_MAX = 50
_TRIAGE_TOOLS = ("mark_read", "archive", "trash", "flag")
_FLAG_LABELS = ("POTENTIAL_SPAM", "POTENTIAL_ISSUE")

Handler = Callable[[dict, str, Any, Any], dict]


def _bc() -> Any:
    from .. import bridge_cli

    return bridge_cli


def _err(message: str) -> Exception:
    return _bc().BridgeInputError(message)


def _author(data: dict[str, Any]) -> str:
    return str(data.get("author") or "bridge")[:80]


def _since() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def _ids(data: dict[str, Any]) -> list[int]:
    raw = data.get("ids")
    if raw is None and "id" in data:
        raw = [data["id"]]
    if not isinstance(raw, list) or not raw:
        raise _err("ids must be a non-empty list of message ids")
    if len(raw) > BULK_MAX:
        raise _err(f"at most {BULK_MAX} ids per call")
    out: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _err("ids must be a non-empty list of positive integers")
        if value not in out:
            out.append(value)
    return out


def _flag(data: dict[str, Any], name: str, default: bool) -> bool:
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise _err(f"{name} must be true or false")
    return value


def _gate_r(row: Any) -> str | None:
    bc = _bc()
    if bc._row_quarantined(row):
        return ("message is quarantined (suspected prompt injection); "
                "a human must release it in the local UI first")
    if not ai_may_read(bc._row_screening_status(row)):
        return ("message is withheld by inbound screening; a human must mark it "
                "content-related in the local UI first")
    return None


def _state_reason(row: Any) -> str | None:
    if row["purged_at"]:
        return "message was permanently deleted (purged)"
    return None


def _triage_used(store: Any) -> int:
    since = _since()
    return sum(store.count_audit_tool_calls(t, since) for t in _TRIAGE_TOOLS)


def _check_cap(store: Any, wanted: int) -> None:
    used = _triage_used(store)
    if used + wanted > TRIAGE_PER_DAY:
        raise _err(
            f"daily triage cap reached ({used} of {TRIAGE_PER_DAY} changes in 24 h; "
            f"this call would make {used + wanted}); ask the human to do the rest in MailForge"
        )


def _audit(store: Any, tool: str, message_id: int, author: str, **extra: Any) -> None:
    AuditLog(store).record(
        actor="agent", event="tool_call", subject_table="messages", subject_id=message_id,
        detail={"tool": tool, "author": author, **extra},
    )


def _bulk(
    data: dict[str, Any], site: str, store: Any, tool: str,
    check: Callable[[Any], str | None], apply: Callable[[list[int]], Any],
    extra: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ids = _ids(data)
    author = _author(data)
    _check_cap(store, len(ids))
    changed: list[int] = []
    refused: list[dict[str, Any]] = []
    for mid in ids:
        row = store.get_message_for_site(mid, site)
        if row is None:
            refused.append({"id": mid, "reason": "message_id not found for site"})
            continue
        reason = check(row)
        if reason:
            refused.append({"id": mid, "reason": reason})
        else:
            changed.append(mid)
    if changed:
        apply(changed)
        for mid in changed:
            _audit(store, tool, mid, author, **(extra(mid) if extra else {}))
    return {"changed": changed, "refused": refused}


# ----- writes: triage -----------------------------------------------------

def _mark_read(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    seen = _flag(data, "seen", True)
    return _bulk(
        data, site, store, "mark_read",
        lambda row: _state_reason(row) or _gate_r(row),
        lambda ids: store.mark_messages_seen(ids, seen),
        lambda mid: {"seen": seen},
    )


def _archive(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    archived = _flag(data, "archived", True)

    def check(row: Any) -> str | None:
        reason = _state_reason(row)
        if reason:
            return reason
        if archived and row["trashed"]:
            return "message is in Trash; restore it first (archiving would silently restore it)"
        return _gate_r(row)

    return _bulk(
        data, site, store, "archive", check,
        lambda ids: store.set_messages_archived(ids, archived),
        lambda mid: {"archived": archived},
    )


def _trash(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    trashed = _flag(data, "trashed", True)
    immediate = set(store.PURGE_IMMEDIATELY)

    def check(row: Any) -> str | None:
        reason = _state_reason(row)
        if reason:
            return reason
        if not trashed:  # restore is always allowed, even for withheld mail
            return None if row["trashed"] else "message is not in Trash"
        if row["trashed"]:
            return "message is already in Trash"
        status = _bc()._row_screening_status(row)
        if status in immediate:
            return (f"status {status} is deleted at the provider as soon as it is trashed; "
                    "a human decides that deletion in the local UI")
        return _gate_r(row)

    return _bulk(
        data, site, store, "trash", check,
        lambda ids: store.set_messages_trashed(ids, trashed),
        lambda mid: {"trashed": trashed},
    )


def _flag_message(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    mid = _bc()._required_int(data, "id")
    label = str(data.get("label") or "").strip().upper()
    if label not in _FLAG_LABELS:
        raise _err("label must be one of " + ", ".join(_FLAG_LABELS))
    note = str(data.get("note") or "").strip()[:2000]
    author = _author(data)
    row = store.get_message_for_site(mid, site)
    if row is None:
        raise _err("message_id not found for site")
    reason = _state_reason(row)
    if reason:
        raise _err(reason)
    if row["trashed"]:
        raise _err("message is in Trash; restore it before flagging")
    reason = _gate_r(row)
    if reason:
        raise _err(reason)
    if str(row["screening_source"] or "").upper() == "USER":
        raise _err("a human already decided this message's screening; "
                   "an agent cannot override it")
    _check_cap(store, 1)
    # Literal labels and learn_similar=False: an agent flag never trains a rule.
    if label == "POTENTIAL_SPAM":
        store.record_screening_feedback(
            mid, "POTENTIAL_SPAM", actor=f"agent:{author}", note=note, learn_similar=False
        )
    else:
        store.record_screening_feedback(
            mid, "POTENTIAL_ISSUE", actor=f"agent:{author}", note=note, learn_similar=False
        )
    _audit(store, "flag", mid, author, label=label)
    return {"id": mid, "screening_status": label,
            "next_step": "Flagged for human review; its body is now withheld from agents."}


def _withdraw_draft(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    bc = _bc()
    draft_id = bc._required_int(data, "draft_id")
    draft = store.get_draft_for_site(draft_id, site)
    if draft is None:
        raise _err("draft_id not found for site")
    try:
        flags = json.loads(draft["guardrail_flags"] or "{}")
    except ValueError:
        flags = {}
    if not isinstance(flags, dict) or not flags.get("agent_written"):
        raise _err("only a draft written by an agent can be withdrawn; "
                   "the owner's own drafts are never removed through the bridge")
    if str(draft["state"]).upper() not in bc._EDITABLE_STATES:
        raise _err(f"draft state {draft['state']} cannot be withdrawn")
    author = _author(data)
    flags["withdrawn_by"] = author
    if not store.update_draft_state(
        draft_id, "REJECTED", expect_states=bc._EDITABLE_STATES, guardrail_flags=flags
    ):
        raise _err("the draft changed state (it may have been sent); nothing was withdrawn")
    store.revoke_send_authorizations([draft_id])
    AuditLog(store).record(
        actor="agent", event="tool_call", subject_table="drafts", subject_id=draft_id,
        detail={"tool": "withdraw_draft", "author": author},
    )
    return {"draft_id": draft_id, "state": "REJECTED"}


# ----- reads --------------------------------------------------------------

def _attachments(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    from ..mail.normalize import untrusted_metadata

    mid = _bc()._required_int(data, "message_id")
    row = store.get_message_for_site(mid, site)
    if row is None:
        raise _err("message_id not found for site")
    reason = _gate_r(row)
    if reason:
        raise _err(reason)
    return {
        "message_id": mid,
        "purged": bool(row["purged_at"]),
        "attachments": [
            {"id": int(a["id"]), "filename": untrusted_metadata(a["filename"], 200),
             "mime_type": untrusted_metadata(a["content_type"], 100),
             "size_bytes": int(a["size_bytes"] or 0),
             "skipped": untrusted_metadata(a["skipped_reason"], 100)}
            for a in store.list_attachments(mid)
        ],
    }


def _outbox(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    from ..mail.normalize import untrusted_metadata

    kind = str(data.get("kind") or "staged").strip().lower()
    if kind not in {"staged", "sent"}:
        raise _err("kind must be staged or sent")
    limit = _bc()._bounded_int(data, "limit", 25, 1, 100)
    if kind == "staged":
        return {"staged": [
            {"id": int(r["id"]), "draft_id": int(r["draft_id"]), "status": r["status"],
             "recipient": untrusted_metadata(r["recipient"], 254),
             "subject": untrusted_metadata(r["subject"], 160),
             "created_at": r["created_at"], "expires_at": r["expires_at"]}
            for r in store.list_staged_sends(site_id=site, limit=limit)
        ]}
    return {"sent": [
        {"id": int(r["id"]), "draft_id": r["draft_id"], "outcome": r["outcome"],
         "to": untrusted_metadata(r["to_addrs"], 254),
         "subject": untrusted_metadata(r["subject"], 160), "origin": r["origin"],
         "imap_append": r["imap_append"], "imap_note": untrusted_metadata(r["imap_note"], 200),
         "error": untrusted_metadata(r["error_text"], 200),
         "created_at": r["created_at"], "completed_at": r["completed_at"]}
        for r in store.list_sent_messages(site_id=site, limit=limit)
    ]}


def _sent(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    from ..mail.normalize import neutralize_delimiters, untrusted_metadata

    bc = _bc()
    row = store.get_sent_message(bc._required_int(data, "id"))
    if row is None or row["site_id"] != site:
        raise _err("id not found for site")
    return {
        "id": int(row["id"]), "outcome": row["outcome"], "origin": row["origin"],
        "from": untrusted_metadata(row["from_addr"], 254),
        "to": untrusted_metadata(row["to_addrs"], 254),
        "subject": untrusted_metadata(row["subject"], 200),
        "imap_append": row["imap_append"], "created_at": row["created_at"],
        "body": f"{bc.UNTRUSTED_OPEN}\n{neutralize_delimiters(row['body'] or '')}\n"
                f"{bc.UNTRUSTED_CLOSE}",
    }


def _trash_queue(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    from ..mail.normalize import untrusted_metadata

    limit = _bc()._bounded_int(data, "limit", 50, 1, 200)
    days = int(settings.security.trash_retention_days)
    rows = store.trash_queue(retention_days=days, site_id=site)[:limit]
    return {
        "retention_days": days,
        "queue": [
            {"id": int(r["id"]), "from": untrusted_metadata(r["from_addr"], 80),
             "subject": untrusted_metadata(r["subject"], 80), "received_at": r["received_at"],
             "trashed_at": r["trashed_at"], "screening_status": r["screening_status"],
             "due_at": r["due_at"] or "next sweep (spam / scam: no holding period)"}
            for r in rows
        ],
    }


def _config_summary(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    from ..db import store as store_mod

    return {
        "site_id": site,
        "sites": sorted(store_mod.VALID_SITES),
        "mailboxes": [a.name for a in settings.imap_accounts if a.site_id == site],
        "retention": {"trash_retention_days": int(settings.security.trash_retention_days)},
        "caps": {
            "agent_compose_per_day": int(settings.openclaw.agent_compose_per_day),
            "triage_per_day": TRIAGE_PER_DAY,
            "bulk_ids_max": BULK_MAX,
        },
        "category_rules": [
            {"name": r.name, "label": r.label, "category": r.category,
             "enabled": r.enabled, "from_domains": r.from_domains,
             "applies_to_sites": r.sites or "all"}
            for r in settings.category_rules
            if not r.sites or site in r.sites
        ],
        "note": "Read-only. Settings, category rules and accounts are edited by the human in MailForge.",
    }


def _activity(data: dict[str, Any], site: str, store: Any, settings: Any) -> dict[str, Any]:
    limit = _bc()._bounded_int(data, "limit", 30, 1, 100)
    msg_ids = {int(r[0]) for r in store.conn.execute(
        "SELECT m.id FROM messages m JOIN accounts a ON a.id=m.account_id WHERE a.site_id=?",
        (site,))}
    draft_ids = {int(r[0]) for r in store.conn.execute(
        "SELECT id FROM drafts WHERE site_id=?", (site,))}
    audit: list[dict[str, Any]] = []
    for r in store.recent_audit(limit=2000):
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except ValueError:
            detail = {}
        sid, table = r["subject_id"], r["subject_table"]
        mine = (
            (table == "messages" and sid in msg_ids)
            or (table == "drafts" and sid in draft_ids)
            or (isinstance(detail, dict) and detail.get("site") == site)
        )
        if not mine:
            continue
        audit.append({"id": int(r["id"]), "ts": r["ts"], "actor": r["actor"], "event": r["event"],
                      "subject_table": table, "subject_id": sid, "detail": detail})
        if len(audit) >= limit:
            break
    return {
        "audit": audit,
        "notes": [
            {"id": int(n["id"]), "kind": n["kind"], "author": n["author"], "title": n["title"],
             "created_at": n["created_at"]}
            for n in store.list_agent_notes(site, limit=min(limit, 50))
        ],
    }


HANDLERS: dict[str, Handler] = {
    "mark-read": _mark_read,
    "archive": _archive,
    "trash": _trash,
    "flag": _flag_message,
    "withdraw-draft": _withdraw_draft,
    "attachments": _attachments,
    "outbox": _outbox,
    "sent": _sent,
    "trash-queue": _trash_queue,
    "config-summary": _config_summary,
    "activity": _activity,
}

CLASS: dict[str, str] = {
    "mark-read": "triage",
    "archive": "triage",
    "trash": "triage",
    "flag": "triage",
    "withdraw-draft": "draft_write",
    "attachments": "read",
    "outbox": "read",
    "sent": "read",
    "trash-queue": "read",
    "config-summary": "read",
    "activity": "read",
}
