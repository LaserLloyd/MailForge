"""Inbox page — triage dashboard for drafts (build spec §9).

Chat-styled overview of every draft the agent has produced:

  * stat chips (Pending / Blocked / Deferred / Sent) from one GROUP BY query;
  * state tabs + a live search box (sender / subject / recipient / category);
  * chat-style card rows with category, injection-risk and state badges; the
    recipient is flagged RED when it is an external / first-time address —
    the §0.4 "recipients are bound, not generated" invariant cue.

The body is ``@ui.refreshable``; the runtime/listener coroutine pushes updates
by calling :func:`mailforge.ui.app.refresh_inbox` (NiceGUI WS). Clicking
a row navigates to ``/detail/{draft_id}``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nicegui import ui

from .. import theme

log = logging.getLogger(__name__)

# Tab key -> (label, store state filter). None = all states.
_TABS: list[tuple[str, str, str | None]] = [
    ("pending", "Pending", "PENDING"),
    ("blocked", "Blocked", "BLOCKED"),
    ("deferred", "Deferred", "DEFERRED_NO_LLM"),
    ("sent", "Sent", "SENT"),
    ("all", "All", None),
]

# Per-process view state (single-user localhost app; survives refreshable
# re-renders and WS pushes). Shared across clients by design.
_view: dict[str, Any] = {"tab": "pending", "q": ""}


def _short_time(iso: str | None) -> str:
    """Compact human timestamp: HH:MM today, 'Mon DD' otherwise."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:16]
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    now = datetime.now(timezone.utc).astimezone()
    return dt.strftime("%H:%M") if dt.date() == now.date() else dt.strftime("%b %d")


def _state_for_tab(tab: str) -> str | None:
    for key, _label, state in _TABS:
        if key == tab:
            return state
    return "PENDING"


def rows_for_view(store: object, tab: str, q: str) -> list[dict[str, Any]]:
    """Draft rows for the selected tab, filtered by the search query."""
    try:
        drafts = store.drafts_overview(_state_for_tab(tab))  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("could not load drafts: %s", e)
        return []

    out: list[dict[str, Any]] = []
    needle = (q or "").strip().lower()
    for d in drafts:
        sender = d["from_name"] or d["from_addr"] or "(unknown sender)"
        recipient = d["recipient"] or ""
        try:
            external = not store.is_allowlisted(recipient)  # type: ignore[attr-defined]
        except Exception:
            external = True  # fail safe: treat unknown as external (red)
        row = {
            "id": d["id"],
            "sender": sender,
            "from_addr": d["from_addr"] or "",
            "subject": theme.clean_subject(d["subject"]),
            "preview": " ".join((d["body"] or "").split())[:110],
            "category": d["category"] or "",
            "risk": d["injection_risk"],
            "recipient": recipient,
            "external": external,
            "state": d["state"] or "",
            "time": _short_time(d["created_at"]),
        }
        if needle:
            hay = " ".join(
                (row["sender"], row["from_addr"], row["subject"], row["recipient"],
                 row["category"], row["preview"])
            ).lower()
            if needle not in hay:
                continue
        out.append(row)
    return out


# Backwards-compatible helper (used by tests / external callers).
def pending_rows(store: object) -> list[dict[str, Any]]:
    """Rows for all PENDING drafts (spec §9)."""
    return rows_for_view(store, "pending", "")


def _stat_chip(label: str, n: int, color: str = "var(--text-primary)") -> None:
    with ui.element("div").classes("bb-stat"):
        ui.label(str(n)).classes("bb-stat-n").style(f"color: {color}")
        ui.label(label).classes("bb-stat-l")


def _render_row(row: dict[str, Any], show_state: bool) -> None:
    with ui.element("div").classes("bb-row w-full").on(
        "click", lambda _e, i=row["id"]: ui.navigate.to(f"/detail/{i}")
    ):
        with ui.row().classes("w-full items-center no-wrap").style("gap: 10px"):
            with ui.column().classes("col").style("gap: 2px; min-width: 0"):
                with ui.row().classes("items-center no-wrap").style("gap: 8px"):
                    ui.label(row["sender"]).classes("bb-clip-1").style(
                        "font-weight: 700; font-size: 14px; flex-shrink: 0; max-width: 45%"
                    )
                    theme.subject_label(
                        row["subject"],
                        style="color: var(--text-secondary); font-size: 13.5px",
                    )
                with ui.row().classes("items-center no-wrap").style("gap: 6px"):
                    ui.label(f"→ {row['recipient']}").style(
                        "font-size: 12px; "
                        + ("color: var(--error); font-weight: 700"
                           if row["external"] else "color: var(--text-muted)")
                    )
                    if row["external"]:
                        theme.badge("external", "error")
                if row["preview"]:
                    ui.label(row["preview"]).classes("bb-clip-1").style(
                        "color: var(--text-muted); font-size: 12.5px; max-width: 640px"
                    )
            with ui.column().classes("items-end").style("gap: 5px; flex-shrink: 0"):
                ui.label(row["time"]).style("font-size: 11.5px; color: var(--text-muted)")
                with ui.row().classes("items-center").style("gap: 5px"):
                    if row["category"]:
                        theme.badge(row["category"], "accent")
                    theme.risk_badge(row["risk"])
                    if show_state:
                        theme.state_badge(row["state"])


@ui.refreshable
def _inbox_body(store: object) -> None:
    """Refreshable stats + draft list (re-pulled from the DB each refresh)."""
    try:
        counts = store.draft_counts()  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("draft_counts failed: %s", e)
        counts = {}

    deferred = int(counts.get("DEFERRED_NO_LLM", 0) or 0)
    with ui.element("div").classes("bb-card w-full q-pa-md"):
        with ui.row().classes("w-full items-center bb-toolbar").style("gap: 8px"):
            ui.icon("account_tree", size="21px").style("color: var(--accent-hover)")
            ui.label("Receive → sanitize/classify → site context → draft/revise → approve → send").style(
                "font-size: 12.5px; font-weight: 650"
            )
            ui.space()
            theme.badge("AUTO THROUGH DRAFT", "accent")
            theme.badge("NEVER AUTO-SEND", "success")
        ui.label(
            f"{deferred} deferred draft(s) are waiting for model retry or your revision."
            if deferred
            else "Deferred retry queue is clear. You remain the final send authority."
        ).style(
            "font-size: 11.5px; color: var(--warning)" if deferred
            else "font-size: 11.5px; color: var(--text-secondary)"
        )

    with ui.row().classes("w-full bb-toolbar").style("gap: 10px"):
        _stat_chip("Pending", counts.get("PENDING", 0), "var(--accent-hover)")
        _stat_chip("Blocked", counts.get("BLOCKED", 0), "var(--error)")
        _stat_chip("Deferred", counts.get("DEFERRED_NO_LLM", 0), "var(--warning)")
        _stat_chip("Sent", counts.get("SENT", 0), "var(--success)")

    rows = rows_for_view(store, _view["tab"], _view["q"])
    show_state = _view["tab"] == "all"

    if not rows:
        with ui.column().classes("w-full items-center q-pa-xl").style("gap: 6px"):
            ui.icon("mark_email_read", size="40px").style("color: var(--text-muted)")
            label = "No matching drafts." if _view["q"] else "Nothing here — all clear."
            ui.label(label).style("color: var(--text-secondary)")
        return

    with ui.column().classes("w-full").style("gap: 8px"):
        for row in rows:
            _render_row(row, show_state)


def render(store: object) -> None:
    """Render the inbox page body (spec §9).

    Tabs + search live outside the refreshable so WS pushes (listener calls
    :func:`mailforge.ui.app.refresh_inbox` -> :func:`refresh`) preserve
    the user's current view.
    """
    with ui.row().classes("w-full items-center bb-toolbar").style("gap: 12px"):
        with ui.tabs(
            value=_view["tab"],
            on_change=lambda e: (_view.update(tab=e.value), _inbox_body.refresh()),
        ).props("dense no-caps indicator-color=primary"):
            for key, label, _state in _TABS:
                ui.tab(key, label=label)
        ui.space()
        ui.input(
            placeholder="Search sender, subject, recipient…",
            value=_view["q"],
            on_change=lambda e: (_view.update(q=e.value or ""), _inbox_body.refresh()),
        ).props("dense outlined clearable debounce=300").classes(
            "w-72 bb-toolbar-grow"
        ).add_slot(
            "prepend", '<i class="material-icons" style="font-size:18px">search</i>'
        )

    _inbox_body(store)


def refresh() -> None:
    """Re-pull drafts and re-render (spec §9 WS push)."""
    try:
        _inbox_body.refresh()
    except Exception as e:  # no active client / not yet rendered
        log.debug("inbox refresh skipped: %s", e)
