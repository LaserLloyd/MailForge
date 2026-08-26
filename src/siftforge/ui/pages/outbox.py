"""Outbox / Sent — everything on its way out, and everything that went out.

Two populations, one list, newest first:

  * **Staged** — an agent called ``prepare_send`` and is holding a one-time
    token, waiting for a human to say yes. Nothing has been transmitted.
  * **Transmitted** — a row in ``sent_messages``, written before the SMTP
    socket opened and stamped with the result afterwards.

Every row carries exactly one status chip and there is no ambiguous state:

  ============================  =======  =====================================
  chip                          colour   meaning
  ============================  =======  =====================================
  NOT SENT — awaiting approval  amber    staged, token live, nothing sent
  EXPIRED — never sent          grey     staged, token lapsed; it cannot send
  SENDING — result not recorded red      transmission opened, never completed
  UNKNOWN — token used…         red      token consumed, no transmission row
  SENT <local time>             green    left the SMTP relay
  FAILED — <reason>             red      SMTP refused it
  ============================  =======  =====================================

The two red states are the point of this page: a message whose fate is unknown
must look like a problem, never like a success.

This page NEVER sends. Staged rows link to the existing approval screen; no
new send-triggering control exists here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from nicegui import ui

from .. import theme
from .mail import _value

log = logging.getLogger(__name__)

PAGE_SIZE = 100
_MAX_SCAN = 2000

#: (key, label) — the view selector, in order.
VIEWS: list[tuple[str, str]] = [
    ("all", "Everything"),
    ("pending", "Needs attention"),
    ("sent", "Sent"),
    ("problems", "Failed & unknown"),
]


def _local(ts: Any) -> str:
    """ISO-8601 UTC (how everything is stored) rendered in local time."""
    text = str(ts or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:19].replace("T", " ")


def status_of(row: Any, kind: str) -> tuple[str, str, str]:
    """``(state, chip label, badge kind)`` for one outbox row.

    ``state`` is the machine-readable name used by tests and filters:
    STAGED | EXPIRED | UNKNOWN | IN_FLIGHT | SENT | FAILED.
    """
    if kind == "staged":
        status = str(_value(row, "status", "") or "").upper()
        if status == "EXPIRED":
            return ("EXPIRED", "EXPIRED — never sent", "")
        if status == "UNKNOWN":
            return (
                "UNKNOWN",
                "UNKNOWN — token used, no transmission recorded",
                "error",
            )
        return ("STAGED", "NOT SENT — awaiting approval", "warning")

    outcome = str(_value(row, "outcome", "") or "").upper()
    if outcome == "SENT":
        return ("SENT", f"SENT {_local(_value(row, 'completed_at'))}".strip(), "success")
    if outcome == "FAILED":
        reason = " ".join(str(_value(row, "error_text", "") or "").split())[:60]
        return ("FAILED", f"FAILED — {reason or 'SMTP refused it'}", "error")
    return ("IN_FLIGHT", "SENDING — result not recorded", "error")


def _sort_key(entry: tuple[str, Any]) -> str:
    kind, row = entry
    if kind == "staged":
        return str(_value(row, "created_at", "") or "")
    return str(_value(row, "created_at", "") or "")


def collect_rows(store: object, view: str = "all", scan: int = _MAX_SCAN) -> list[tuple[str, Any]]:
    """Merge both populations into one newest-first list of ``(kind, row)``."""
    staged: list[Any] = []
    sent: list[Any] = []
    if view in {"all", "pending", "problems"}:
        try:
            staged = list(store.list_staged_sends(limit=scan))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("outbox: list_staged_sends failed: %s", e)
    # Sent rows are always fetched: "Needs attention" includes transmissions
    # whose result was never recorded, which live in that table.
    try:
        sent = list(store.list_sent_messages(limit=scan))  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        log.warning("outbox: list_sent_messages failed: %s", e)
    entries = [("staged", r) for r in staged] + [("sent", r) for r in sent]
    entries.sort(key=_sort_key, reverse=True)
    keep = {
        "all": {"STAGED", "EXPIRED", "UNKNOWN", "IN_FLIGHT", "SENT", "FAILED"},
        "pending": {"STAGED", "IN_FLIGHT", "UNKNOWN"},
        "sent": {"SENT"},
        "problems": {"FAILED", "UNKNOWN", "IN_FLIGHT", "EXPIRED"},
    }[view if view in {"all", "pending", "sent", "problems"} else "all"]
    return [e for e in entries if status_of(e[1], e[0])[0] in keep]


def _row_url(kind: str, row_id: int) -> str:
    return f"/outbox/{kind}/{int(row_id)}"


def _list_url(**params: str) -> str:
    query = {k: v for k, v in params.items() if v}
    return "/outbox" + ("?" + urlencode(query) if query else "")


def _recipients(kind: str, row: Any) -> str:
    return str(
        (_value(row, "recipient", "") if kind == "staged" else _value(row, "to_addrs", ""))
        or ""
    )


def _from_addr(kind: str, row: Any) -> str:
    return str(_value(row, "from_addr", "") or "")


def _body(kind: str, row: Any) -> str:
    return str(_value(row, "body", "") or "")


def _origin_label(kind: str, row: Any) -> str:
    if kind == "staged":
        return f"staged by {str(_value(row, 'author', '') or 'an agent')}"
    return {
        "ui_draft": "approved on the AI Review screen",
        "ui_compose": "written in Compose",
        "agent_bridge": "relayed for an agent",
    }.get(str(_value(row, "origin", "") or ""), "sent")


def _append_note(row: Any) -> tuple[str, str] | None:
    """(text, colour var) describing the IMAP Sent-folder copy, or None."""
    status = str(_value(row, "imap_append", "") or "").upper()
    folder = str(_value(row, "imap_folder", "") or "")
    note = str(_value(row, "imap_note", "") or "")
    if status == "OK":
        return (f"Copy saved to the “{folder or 'Sent'}” folder.", "var(--text-muted)")
    if status == "FAILED":
        return (
            f"Mail was sent, but the Sent-folder copy failed{f': {note}' if note else ''}.",
            "var(--warning)",
        )
    if status == "SKIPPED":
        return (
            f"No Sent-folder copy{f' ({note})' if note else ''}.",
            "var(--text-muted)",
        )
    return None


# --------------------------------------------------------------------------- #
# List page
# --------------------------------------------------------------------------- #
def render(store: object, settings: object, *, view: str = "all", page: int = 0) -> None:
    view = view if view in dict(VIEWS) else "all"
    try:
        page = max(0, int(page))
    except (TypeError, ValueError):
        page = 0

    try:
        counts = store.outbox_counts()  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        log.warning("outbox: counts failed: %s", e)
        counts = {}

    entries = collect_rows(store, view)
    window = entries[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    keys = [(kind, int(_value(row, "id"))) for kind, row in window]

    def _go(**changes: str) -> None:
        current = {"view": view, "page": str(page) if page else ""}
        if "view" in changes:
            current["page"] = ""
        current.update(changes)
        ui.navigate.to(_list_url(**current))

    with ui.row().classes("w-full items-center bb-toolbar"):
        ui.select(
            dict(VIEWS),
            label="View",
            value=view,
            on_change=lambda e: _go(view=str(e.value)),
        ).props("dense outlined options-dense").classes("w-52 bb-toolbar-grow")
        ui.space()
        for label, key, colour in (
            ("Awaiting approval", "staged", "var(--warning)"),
            ("Unresolved", "unknown", "var(--error)"),
            ("Sent", "sent", "var(--success)"),
            ("Failed", "failed", "var(--error)"),
        ):
            n = int(counts.get(key, 0) or 0)
            if key == "unknown":
                n += int(counts.get("in_flight", 0) or 0)
            with ui.element("div").classes("bb-stat"):
                ui.label(str(n)).classes("bb-stat-n").style(
                    f"color: {colour}" if n else "color: var(--text-muted)"
                )
                ui.label(label).classes("bb-stat-l")

    ui.label(
        "Nothing on this page can send a message. Staged messages are approved "
        "on their draft screen, exactly as they are today."
    ).style("font-size: 12px; color: var(--text-secondary)")

    def _open_relative(step: int) -> None:
        if keys:
            kind, row_id = keys[0 if step > 0 else -1]
            ui.navigate.to(_row_url(kind, row_id))

    def _on_key(e: Any) -> None:
        if not e.action.keydown or e.modifiers.ctrl or e.modifiers.meta or e.modifiers.alt:
            return
        key = str(e.key)
        if key == "j":
            _open_relative(1)
        elif key == "k":
            _open_relative(-1)
        elif key == "r":
            ui.navigate.to(_list_url(view=view, page=str(page) if page else ""))

    ui.keyboard(on_key=_on_key)

    if not window:
        with ui.column().classes("w-full items-center q-pa-xl"):
            ui.icon("outbox", size="52px").style("color: var(--text-muted)")
            ui.label("Nothing here yet").style("font-size: 17px; font-weight: 700")
            ui.label(
                "Messages appear the moment an agent stages one for approval, "
                "and stay after they are sent."
            ).style("color: var(--text-secondary)")
        return

    with ui.column().classes("w-full").style("gap: 8px"):
        for kind, row in window:
            _render_row(kind, row)
        if len(entries) > PAGE_SIZE or page:
            with ui.row().classes("w-full items-center justify-between").style(
                "padding: 4px 2px"
            ):
                newer = ui.button(
                    "Newer", icon="chevron_left", on_click=lambda: _go(page=str(page - 1))
                ).props("flat dense no-caps")
                if not page:
                    newer.props("disable")
                ui.label(f"Page {page + 1} · {len(window)} of {len(entries)}").style(
                    "font-size: 11.5px; color: var(--text-muted)"
                )
                older = ui.button(
                    "Older", icon="chevron_right", on_click=lambda: _go(page=str(page + 1))
                ).props("flat dense no-caps")
                if (page + 1) * PAGE_SIZE >= len(entries):
                    older.props("disable")


def _render_row(kind: str, row: Any) -> None:
    state, label, badge_kind = status_of(row, kind)
    row_id = int(_value(row, "id"))
    with ui.element("div").classes("bb-row w-full").on(
        "click", lambda: ui.navigate.to(_row_url(kind, row_id))
    ):
        with ui.row().classes("w-full items-start no-wrap").style("gap: 10px"):
            with ui.column().classes("col").style("gap: 3px; min-width: 0"):
                theme.subject_label(_value(row, "subject", ""), style="font-weight: 700")
                ui.label(
                    f"To: {_recipients(kind, row) or '(none)'} · "
                    f"From: {_from_addr(kind, row) or '(unknown)'}"
                ).classes("bb-clip-1").style("font-size: 11.5px; color: var(--text-muted)")
                ui.label(
                    f"{_origin_label(kind, row)} · {_local(_value(row, 'created_at'))}"
                ).style("font-size: 11px; color: var(--text-muted)")
            with ui.column().classes("items-end").style("gap: 4px"):
                theme.badge(label, badge_kind)
                if kind == "sent":
                    note = _append_note(row)
                    if note is not None and str(_value(row, "imap_append", "")) == "FAILED":
                        theme.badge("no Sent-folder copy", "warning")


# --------------------------------------------------------------------------- #
# Row detail
# --------------------------------------------------------------------------- #
def render_detail(store: object, settings: object, kind: str, row_id: int) -> None:
    """One outbox row in full: verbatim content, identity, and what happened."""
    if kind not in {"staged", "sent"}:
        ui.label("Unknown outbox row.").style("color: var(--error)")
        return
    getter = store.get_staged_send if kind == "staged" else store.get_sent_message  # type: ignore[attr-defined]
    try:
        row = getter(int(row_id))
    except Exception as e:  # noqa: BLE001
        log.warning("outbox detail lookup failed: %s", e)
        row = None
    if row is None:
        with ui.column().classes("w-full items-center q-pa-xl").style("gap: 8px"):
            ui.label("That outbox entry no longer exists.").style("font-weight: 700")
            ui.button("Back to Outbox", on_click=lambda: ui.navigate.to("/outbox")).props(
                "flat no-caps"
            )
        return

    state, label, badge_kind = status_of(row, kind)
    with ui.row().classes("w-full items-center").style("gap: 10px"):
        ui.button("Back", icon="arrow_back", on_click=lambda: ui.navigate.to("/outbox")).props(
            "flat no-caps"
        )
        ui.space()
        theme.badge(label, badge_kind)

    with ui.card().classes("w-full bb-card").style("gap: 8px"):
        theme.subject_label(_value(row, "subject", ""), full=True, style="font-weight: 800")
        for caption, value in (
            ("From (sending mailbox)", _from_addr(kind, row) or "(unknown)"),
            ("To", _recipients(kind, row) or "(none)"),
            ("Recorded", _local(_value(row, "created_at"))),
        ):
            with ui.row().classes("w-full no-wrap").style("gap: 8px"):
                ui.label(caption).style(
                    "font-size: 11.5px; color: var(--text-muted); min-width: 168px"
                )
                ui.label(str(value)).style("font-size: 12.5px; overflow-wrap: anywhere")

        if kind == "sent":
            for caption, value in (
                ("Completed", _local(_value(row, "completed_at")) or "—"),
                ("SMTP Message-ID", str(_value(row, "smtp_message_id", "") or "—")),
                ("Origin", _origin_label(kind, row)),
            ):
                with ui.row().classes("w-full no-wrap").style("gap: 8px"):
                    ui.label(caption).style(
                        "font-size: 11.5px; color: var(--text-muted); min-width: 168px"
                    )
                    ui.label(str(value)).classes("bb-mono").style(
                        "font-size: 12px; overflow-wrap: anywhere"
                    )
            note = _append_note(row)
            if note is not None:
                ui.label(note[0]).style(f"font-size: 12px; color: {note[1]}")
            if state == "FAILED":
                ui.label(
                    f"SMTP error: {str(_value(row, 'error_text', '') or '')}"
                ).style("font-size: 12.5px; color: var(--error); overflow-wrap: anywhere")
            if state == "IN_FLIGHT":
                ui.label(
                    "This transmission was started but its result was never written "
                    "back — SiftForge stopped between handing the message to SMTP and "
                    "recording the outcome. Check the mailbox's Sent folder at the "
                    "provider before resending."
                ).style("font-size: 12.5px; color: var(--error)")
        else:
            expires = _local(_value(row, "expires_at"))
            with ui.row().classes("w-full no-wrap").style("gap: 8px"):
                ui.label("Authorisation expires").style(
                    "font-size: 11.5px; color: var(--text-muted); min-width: 168px"
                )
                ui.label(expires or "—").style("font-size: 12.5px")
            if state == "STAGED":
                ui.label(
                    "Nothing has been transmitted. To send it, open the draft and use "
                    "Approve & Send there — the same approval you use today."
                ).style("font-size: 12.5px; color: var(--warning)")
                draft_id = _value(row, "draft_id")
                if draft_id is not None:
                    ui.button(
                        "Open the draft to review and approve",
                        icon="open_in_new",
                        on_click=lambda: ui.navigate.to(f"/detail/{int(draft_id)}"),
                    ).props("outline no-caps color=primary")
            elif state == "EXPIRED":
                ui.label(
                    "The one-time authorisation lapsed without being used. This message "
                    "cannot be sent from it; the agent must stage it again."
                ).style("font-size: 12.5px; color: var(--text-secondary)")
            else:
                ui.label(
                    "The one-time token was consumed but no transmission was recorded. "
                    "Treat the outcome as unknown and check the provider's Sent folder "
                    "before resending."
                ).style("font-size: 12.5px; color: var(--error)")

    with ui.card().classes("w-full bb-card").style("gap: 6px"):
        ui.label(
            "Content, verbatim" if kind == "sent" else "Content as staged, verbatim"
        ).style("font-weight: 700")
        ui.label(
            "Shown exactly as recorded — never re-rendered or re-generated."
        ).style("font-size: 11.5px; color: var(--text-muted)")
        ui.label(_body(kind, row) or "(empty)").style(
            "white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--font-mono); "
            "font-size: 12.5px; background: var(--bg-secondary); border: 1px solid var(--border); "
            "border-radius: var(--radius-sm); padding: 12px"
        )
