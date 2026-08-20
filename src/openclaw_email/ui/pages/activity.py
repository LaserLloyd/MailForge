"""Activity page — audit-trail viewer (build spec §7, §0.5).

Read-only view over the chained-hash ``audit_log``: hash-chain verification
status banner, actor/event filters, and the most recent events (newest first).
Details are shown as recorded — they were REDACTED before write (spec §0.5),
so nothing sensitive can appear here.
"""

from __future__ import annotations

import logging

from nicegui import ui

from .. import theme

log = logging.getLogger(__name__)

_LIMIT = 200

# Per-process filter state (single-user localhost app).
_view: dict[str, str] = {"actor": "all", "event": "all"}

_EVENT_KINDS = {
    "approval": "success",
    "send": "success",
    "block": "error",
    "error": "error",
    "llm_call": "accent",
    "tool_call": "accent",
}


def _verify_banner(store: object) -> None:
    """Verify the chained-hash audit log and show the verdict (spec §7/§11)."""
    try:
        from ...audit.log import verify_chain

        ok, checked, bad_index = verify_chain(store.db_path)  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("audit chain verify failed to run: %s", e)
        ui.label(f"Chain verification unavailable: {e}").style("color: var(--warning)")
        return

    with ui.row().classes("w-full items-center oce-card q-pa-md").style("gap: 10px"):
        if ok:
            ui.icon("verified", size="22px").style("color: var(--success)")
            ui.label(f"Hash chain verified — {checked} event(s), no tampering detected.").style(
                "color: var(--success); font-weight: 600"
            )
        else:
            ui.icon("gpp_bad", size="22px").style("color: var(--error)")
            ui.label(
                f"HASH CHAIN BROKEN at entry index {bad_index} — the audit log "
                "was modified outside the app."
            ).style("color: var(--error); font-weight: 700")


@ui.refreshable
def _event_list(store: object) -> None:
    try:
        rows = store.recent_audit(_LIMIT)  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("recent_audit failed: %s", e)
        rows = []

    actor_f, event_f = _view["actor"], _view["event"]
    shown = [
        r for r in rows
        if (actor_f == "all" or r["actor"] == actor_f)
        and (event_f == "all" or r["event"] == event_f)
    ]

    if not shown:
        with ui.column().classes("w-full items-center q-pa-xl").style("gap: 6px"):
            ui.icon("history", size="40px").style("color: var(--text-muted)")
            ui.label("No audit events match.").style("color: var(--text-secondary)")
        return

    with ui.column().classes("w-full").style("gap: 6px"):
        for r in shown:
            with ui.element("div").classes("oce-row w-full").style("cursor: default"):
                with ui.row().classes("w-full items-center no-wrap").style("gap: 10px"):
                    theme.badge(r["event"], _EVENT_KINDS.get(r["event"], ""))
                    ui.label(r["actor"]).style("font-weight: 600; font-size: 13px")
                    subject = (
                        f"{r['subject_table']}#{r['subject_id']}"
                        if r["subject_table"] else ""
                    )
                    if subject:
                        ui.label(subject).classes("oce-mono").style(
                            "font-size: 12px; color: var(--text-secondary)"
                        )
                    detail = (r["detail_json"] or "")[:140]
                    ui.label(detail).classes("oce-mono col").style(
                        "font-size: 12px; color: var(--text-muted); white-space: nowrap; "
                        "overflow: hidden; text-overflow: ellipsis"
                    )
                    ui.label((r["ts"] or "")[:19].replace("T", " ")).style(
                        "font-size: 11.5px; color: var(--text-muted); flex-shrink: 0"
                    )


_NOTE_KINDS = {
    "incident": "error",
    "action": "accent",
    "observation": "",
    "context": "warning",
}


def _agent_notes(store: object) -> None:
    """Durable agent-notes log: what automated agents observed/did and why.

    This is the memory that stops OpenClaw from re-diagnosing its own past
    actions (e.g. a self-caused GitHub verification burst) as an incident."""
    try:
        rows = list(store.list_agent_notes(limit=30))  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        log.debug("list_agent_notes failed: %s", e)
        rows = []
    with ui.expansion(
        f"Agent notes — {len(rows)} recorded", icon="sticky_note_2", value=bool(rows)
    ).classes("w-full oce-card"):
        ui.label(
            "Durable memory shared with the OpenClaw email agents: recorded "
            "actions and context that explain mail patterns (kept even after "
            "chat history is gone)."
        ).style("font-size: 12px; color: var(--text-secondary)")
        if not rows:
            ui.label("No notes yet — agents record them via the bridge.").style(
                "color: var(--text-secondary)"
            )
            return
        for r in rows:
            with ui.element("div").classes("oce-row w-full").style("cursor: default"):
                with ui.row().classes("w-full items-start no-wrap").style("gap: 10px"):
                    with ui.column().classes("col").style("gap: 3px; min-width: 0"):
                        with ui.row().classes("items-center").style("gap: 6px"):
                            theme.badge(r["kind"], _NOTE_KINDS.get(r["kind"], ""))
                            theme.badge(r["site_id"], "accent")
                            ui.label(r["title"]).style("font-weight: 700")
                        ui.label(r["body"]).style(
                            "font-size: 12.5px; color: var(--text-secondary); "
                            "white-space: pre-wrap; overflow-wrap: anywhere"
                        )
                        ui.label(
                            f"{r['author']} · {(r['created_at'] or '')[:19].replace('T', ' ')}"
                        ).style("font-size: 11px; color: var(--text-muted)")


def render(store: object) -> None:
    """Render the activity page body."""
    _verify_banner(store)
    _agent_notes(store)

    try:
        rows = store.recent_audit(_LIMIT)  # type: ignore[attr-defined]
    except Exception:
        rows = []
    actors = ["all"] + sorted({r["actor"] for r in rows if r["actor"]})
    events = ["all"] + sorted({r["event"] for r in rows if r["event"]})

    with ui.row().classes("w-full items-center").style("gap: 10px"):
        ui.select(
            actors, value=_view["actor"], label="Actor",
            on_change=lambda e: (_view.update(actor=e.value), _event_list.refresh()),
        ).props("dense outlined").classes("w-40")
        ui.select(
            events, value=_view["event"], label="Event",
            on_change=lambda e: (_view.update(event=e.value), _event_list.refresh()),
        ).props("dense outlined").classes("w-40")
        ui.space()
        ui.label(f"showing the last {_LIMIT} events").style(
            "font-size: 12px; color: var(--text-muted)"
        )

    _event_list(store)
