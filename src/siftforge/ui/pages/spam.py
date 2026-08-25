"""Spam, scams, and scheduled deletions — one triage screen.

The Inbox can already show screened mail one tag at a time; this page exists
because triage is a batch job. It has four boxes, top to bottom:

  1. Potential spam       — flagged, least certain
  2. Likely scam/phishing — account, payment, and security claims
  3. Confirmed spam       — already filtered, by rule or by you
  4. Scheduled deletion   — the holding box everything else waits in

Deletion policy (implemented in :mod:`mail.retention`, not here): spam and scam
mail is deleted immediately, locally *and* at the mail server. Anything else
you delete goes to box 4 and is permanently removed once it is older than
``security.trash_retention_days`` (60 by default).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode

from nicegui import ui

from .. import theme
# Generic list helpers shared with the Inbox — one implementation of shift-click
# range selection and time/snippet formatting, not two that drift apart.
from .mail import RangeSelection, _shift_held, _short_time, _snippet, _value

log = logging.getLogger(__name__)

_LIMIT = 300

#: (key, title, blurb, screening status, tone) — page order is the order the
#: sections are listed in: least certain first, the holding box last.
SECTIONS: list[tuple[str, str, str, str, str]] = [
    (
        "potential",
        "Potential spam",
        "Not clearly related to your published content — unsolicited sales, cold "
        "outreach, or anything the screener could not place. Mostly junk, but a "
        "real enquiry can land here, so check before you empty it.",
        "POTENTIAL_SPAM",
        "warning",
    ),
    (
        "issue",
        "Likely scam or phishing",
        "Account, security, payment, renewal, and legal claims — plus senders "
        "forging one of your own domains. Never act on a link in these; open the "
        "provider's own site yourself.",
        "POTENTIAL_ISSUE",
        "error",
    ),
    (
        "confirmed",
        "Confirmed spam",
        "Already filtered — either you marked it, or it matched a sender or "
        "subject pattern you taught the screener.",
        "SPAM",
        "error",
    ),
]


def _fetch(store: object, status: str, account: str) -> list[Any]:
    """Active (not archived, not deleted) messages carrying one screening status."""
    try:
        return list(
            store.list_received(  # type: ignore[attr-defined]
                limit=_LIMIT,
                account_name=account or None,
                spam_only=(status == "SPAM"),
                message_tag=None if status == "SPAM" else f"screening:{status}",
                include_spam=True,
            )
        )
    except Exception as e:  # noqa: BLE001
        log.warning("spam page: list_received(%s) failed: %s", status, e)
        return []


def _fetch_queue(store: object, settings: object, account: str) -> list[Any]:
    from ...mail.retention import retention_days

    try:
        rows = list(
            store.trash_queue(retention_days=retention_days(settings))  # type: ignore[attr-defined]
        )
    except Exception as e:  # noqa: BLE001
        log.warning("spam page: trash_queue failed: %s", e)
        return []
    if account:
        rows = [r for r in rows if str(_value(r, "account_name", "") or "") == account]
    return rows[:_LIMIT]


def _audit(store: object, action: str, message_ids: list[int], changed: int) -> None:
    try:
        from ...audit.log import AuditLog

        AuditLog(store).record(
            actor="user",
            event="tool_call",
            subject_table="messages",
            subject_id=message_ids[0] if len(message_ids) == 1 else None,
            detail={
                "action": action,
                "page": "spam",
                "selected": len(message_ids),
                "changed": changed,
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("audit append failed for spam-page action")


def _apply_label(store: object, message_ids: list[int], label: str) -> tuple[int, str]:
    """Record human screening feedback per message; returns (changed, first error).

    ``record_screening_feedback`` is per-message on purpose — it derives the
    sender and subject signature that later mail is matched against — so this
    loops rather than issuing one UPDATE, and a failure on one message never
    discards the decisions already recorded for the others.
    """
    changed = 0
    problem = ""
    for message_id in message_ids:
        try:
            store.record_screening_feedback(  # type: ignore[attr-defined]
                int(message_id),
                label,
                actor="user",
                note="bulk decision from the Spam & Deletion page",
                learn_similar=True,
            )
            changed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("screening feedback failed for message %s: %s", message_id, e)
            problem = problem or str(e)
    return changed, problem


def _due_text(due_at: Any) -> tuple[str, str]:
    """(human due text, tone) for a scheduled-deletion row."""
    if not due_at:
        return ("deletes on the next sweep — spam is not held", "error")
    try:
        due = datetime.fromisoformat(str(due_at))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return (f"deletes {due_at}", "text-secondary")
    # Round UP: a deadline 2 days and 23 hours out is "in 3 days", not "in 2".
    # Truncating undercounts every deadline by a day, which on a delete countdown
    # is the wrong direction to be wrong in.
    remaining = due - datetime.now(timezone.utc)
    days = -((-remaining.total_seconds()) // 86400)
    days = int(days)
    stamp = due.astimezone().strftime("%d %b %Y")
    if days <= 0:
        return (f"due now ({stamp})", "error")
    if days <= 7:
        return (f"deletes in {days} day{'s' if days != 1 else ''} — {stamp}", "warning")
    return (f"deletes in {days} days — {stamp}", "text-secondary")


def _render_row(
    row: Any,
    tone: str,
    subtitle: tuple[str, str],
    on_selection_changed: Callable[[int, bool], None],
    on_click_intent: Callable[[int, bool], None],
) -> Any:
    """One message line: checkbox, sender/subject, and why it is in this box.

    Clicking the row toggles an inline sanitized preview instead of navigating —
    losing a half-built selection to read one message is what makes batch triage
    miserable. The ↗ button opens the full message in a new tab.
    """
    message_id = int(_value(row, "id"))
    container = ui.element("div").classes("bb-row w-full")
    with container:
        with ui.row().classes("w-full items-start no-wrap").style("gap: 9px"):
            checkbox = (
                ui.checkbox(
                    value=False,
                    on_change=lambda e, mid=message_id: on_selection_changed(mid, bool(e.value)),
                )
                .props("dense")
                .style("margin: -5px -3px 0 -5px; flex-shrink: 0")
            )
            checkbox.on("click", js_handler="event => event.stopPropagation()")
            # mousedown strictly precedes the value change, so the modifier state
            # always reaches the server before the handler that consumes it.
            checkbox.on(
                "mousedown",
                lambda e, mid=message_id: on_click_intent(mid, _shift_held(e)),
                ["shiftKey"],
            )
            checkbox.tooltip("Select message — shift-click to select a range")
            with ui.column().classes("col").style("gap: 2px; min-width: 0"):
                with ui.row().classes("w-full items-center no-wrap").style("gap: 6px"):
                    ui.label(
                        str(
                            _value(row, "from_addr")
                            or _value(row, "from_name")
                            or "(unknown sender)"
                        )
                    ).classes("bb-clip-1").style("font-weight: 700; font-size: 12.5px")
                    ui.space()
                    ui.label(_short_time(_value(row, "received_at"))).style(
                        "font-size: 10.5px; color: var(--text-muted); flex-shrink: 0"
                    )
                theme.subject_label(
                    _value(row, "subject"), style="font-size: 13px; font-weight: 650"
                )
                text, text_tone = subtitle
                if text:
                    with ui.row().classes("w-full items-start no-wrap").style("gap: 5px"):
                        ui.icon("schedule" if text_tone == "warning" else "info", size="13px").style(
                            f"color: var(--{text_tone}); margin-top: 2px; flex-shrink: 0"
                        )
                        ui.label(text).style(
                            f"font-size: 11.5px; color: var(--{text_tone}); "
                            "overflow-wrap: anywhere"
                        )
                with ui.row().classes("w-full items-center").style("gap: 4px"):
                    if bool(_value(row, "quarantined", 0)):
                        theme.quarantine_badge()
                    source = str(_value(row, "screening_source", "") or "")
                    if source:
                        theme.badge(
                            "you taught this" if source in {"USER", "LEARNED"} else "auto rule"
                        )
                    account_name = str(_value(row, "account_name", "") or "")
                    if account_name:
                        theme.badge(account_name, "accent")
                    if _value(row, "server_delete_error"):
                        theme.badge("server delete failed", "error")
                    if _value(row, "has_attachments", 0):
                        ui.icon("attach_file", size="14px").style("color: var(--text-muted)")
                    ui.space()
                    open_btn = ui.button(
                        icon="open_in_new",
                        on_click=lambda mid=message_id: ui.navigate.to(
                            f"/mail/{mid}", new_tab=True
                        ),
                    ).props("flat round dense size=sm")
                    open_btn.on("click", js_handler="event => event.stopPropagation()")
                    open_btn.tooltip("Open the full message in a new tab")
                body = str(_value(row, "sanitized_text", "") or "")
                preview = ui.label(_snippet(body, 700) if body else "(no preview here)").style(
                    "font-size: 11.5px; color: var(--text-muted); white-space: pre-wrap; "
                    "overflow-wrap: anywhere; border-left: 2px solid var(--border-light); "
                    "padding-left: 9px; margin-top: 4px"
                )
                preview.set_visibility(False)

    container.on("click", lambda: preview.set_visibility(not preview.visible))
    return checkbox


def _box(
    *,
    store: object,
    title: str,
    blurb: str,
    icon: str,
    tone: str,
    rows: list[Any],
    subtitle_for: Callable[[Any], tuple[str, str]],
    actions: Callable[[Callable[[], list[int]], list[Any]], list[Any]],
    empty_text: str = "Nothing here.",
) -> None:
    """One box: header, bulk bar, message list.

    ``actions`` builds this box's bulk buttons — it is handed a callable
    returning the currently selected ids, and returns the buttons it made so
    they can be enabled and disabled with the selection."""
    visible_ids = [int(_value(row, "id")) for row in rows]
    selection = RangeSelection(visible_ids)
    checkboxes: dict[int, Any] = {}
    bulk_buttons: list[Any] = []
    syncing = False

    with ui.card().classes("w-full bb-card").style(
        f"padding: 0; gap: 0; border-color: var(--{tone})"
    ):
        with ui.column().classes("w-full").style("padding: 13px 15px 10px; gap: 3px"):
            with ui.row().classes("w-full items-center no-wrap").style("gap: 9px"):
                ui.icon(icon, size="21px").style(f"color: var(--{tone})")
                ui.label(title).style("font-size: 16px; font-weight: 800")
                theme.badge(
                    f"{len(rows)}{'+' if len(rows) >= _LIMIT else ''}",
                    "warning" if tone == "warning" else "error",
                )
            ui.label(blurb).style(
                "font-size: 12px; color: var(--text-secondary); max-width: 920px"
            )

        if not rows:
            with ui.column().classes("w-full items-center").style("padding: 16px"):
                ui.icon("check_circle", size="24px").style("color: var(--success)")
                ui.label(empty_text).style("font-size: 12.5px; color: var(--text-secondary)")
            return

        def _sync() -> None:
            nonlocal syncing
            n = len(selection.ids)
            count_label.set_text(f"{n} selected" if n else "Select messages")
            for button in bulk_buttons:
                button.enable() if n else button.disable()
            if bool(select_all.value) != selection.all_selected:
                syncing = True
                select_all.set_value(selection.all_selected)
                syncing = False

        def _push(ids: list[int], checked: bool) -> None:
            nonlocal syncing
            syncing = True
            try:
                for mid in ids:
                    box = checkboxes.get(mid)
                    if box is not None and bool(box.value) != checked:
                        box.set_value(checked)
            finally:
                syncing = False

        def _set_selected(message_id: int, checked: bool) -> None:
            if syncing:
                return
            _push(selection.toggle(message_id, checked), checked)
            _sync()

        def _select_all(e: Any) -> None:
            if syncing:
                return
            checked = bool(e.value)
            selection.set_all(checked)
            _push(visible_ids, checked)
            _sync()

        with (
            ui.row()
            .classes("w-full items-center bb-toolbar")
            .style(
                "gap: 6px; padding: 7px 12px; background: var(--bg-secondary); "
                "border-top: 1px solid var(--border); border-bottom: 1px solid var(--border)"
            )
        ):
            select_all = ui.checkbox("Select all", on_change=_select_all).props("dense")
            count_label = ui.label("Select messages").style(
                "font-size: 12px; color: var(--text-secondary)"
            )
            ui.label("shift-click for a range · click a row to preview").classes(
                "bb-header-note"
            ).style("font-size: 11px; color: var(--text-muted)")
            ui.space()
            bulk_buttons.extend(actions(lambda: selection.ids, rows) or [])

        with ui.column().classes("w-full").style("gap: 7px; padding: 10px"):
            for row in rows:
                message_id = int(_value(row, "id"))
                checkboxes[message_id] = _render_row(
                    row,
                    tone,
                    subtitle_for(row),
                    _set_selected,
                    selection.note_intent,
                )
        _sync()


def render(store: object, settings: object, account: str = "") -> None:
    """Spam & Deletion: three screening boxes plus the scheduled-deletion queue."""
    from ...mail.retention import delete_mode, purge_now, retention_days

    accounts = [a.name for a in (getattr(settings, "imap_accounts", []) or [])]
    account = account if account in accounts else ""
    days = retention_days(settings)
    mode = delete_mode(settings)
    server_note = {
        "trash": "Deleting also moves the message to your provider's Trash folder.",
        "expunge": "Deleting also removes the message from the mail server permanently.",
        "off": "Provider-side deletion is switched off — the mail server keeps its copy.",
    }[mode]

    def _go(**changes: str) -> None:
        params = {"account": account, **changes}
        ui.navigate.to("/spam?" + urlencode({k: v for k, v in params.items() if v}))

    # Actions repaint the boxes in place instead of navigating. A page reload
    # would tear down the notification that just explained what the action did
    # — which, for a delete, is the part the user most needs to read.
    def _reload() -> None:
        _boxes.refresh()

    with ui.dialog() as confirm, ui.card().classes("bb-card").style(
        "width: min(520px, 94vw); padding: 18px; gap: 8px"
    ):
        ui.label("Delete permanently?").style("font-size: 17px; font-weight: 800")
        confirm_text = ui.label("").style("font-size: 12.5px; color: var(--text-secondary)")
        confirm_state: dict[str, Any] = {"ids": [], "run": None}
        with ui.row().classes("w-full justify-end bb-toolbar"):
            ui.button("Cancel", on_click=confirm.close).props("flat no-caps")
            confirm_go = ui.button("Delete permanently", icon="delete_forever").props(
                "unelevated no-caps color=negative"
            )

    async def _purge(ids: list[int]) -> None:
        confirm.close()
        if not ids:
            return
        # Talking to the mail server can take tens of seconds (a slow or dead
        # host runs into the socket timeout), and the dialog is already gone —
        # without a progress notice the page just looks frozen.
        progress = ui.notification(
            f"Deleting {len(ids)} message(s) — contacting the mail server…",
            spinner=True,
            timeout=None,
        )
        try:
            report = await asyncio.to_thread(purge_now, store, settings, ids)
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Delete failed: {e}", type="negative")
            return
        finally:
            try:
                progress.dismiss()
            except Exception:  # noqa: BLE001
                pass
        _audit(store, "purge_now", ids, report.purged)
        ui.notify(report.summary(), type="positive" if report.ok else "warning", timeout=8000)
        for err in report.errors:
            ui.notify(
                f"The mail server refused the delete — {err}. The local copy is gone; "
                "the next sweep retries the server.",
                type="negative",
                timeout=10000,
            )
        _reload()

    def _ask_purge(ids: list[int]) -> None:
        if not ids:
            return
        confirm_state["ids"] = ids
        confirm_text.set_text(
            f"{len(ids)} message(s) will be deleted from this app and "
            + (
                "moved to your provider's Trash folder."
                if mode == "trash"
                else "removed from the mail server permanently."
                if mode == "expunge"
                else "left untouched on the mail server (provider deletion is off)."
            )
            + " This cannot be undone from here."
        )
        confirm.open()

    confirm_go.on_click(lambda: _purge(list(confirm_state["ids"])))

    def _mark(ids: list[int], label: str) -> None:
        if not ids:
            return
        changed, problem = _apply_label(store, ids, label)
        _audit(store, f"label_{label.lower()}", ids, changed)
        if not changed:
            ui.notify(f"Could not save feedback: {problem}", type="negative")
            return
        if label == "SPAM":
            ui.notify(
                f"Marked {changed} message(s) as spam and learned the sender/subject "
                "patterns — similar mail is filtered from now on. Nothing was deleted; "
                "use Delete when you want them gone.",
                type="positive",
                timeout=8000,
            )
        else:
            ui.notify(
                f"Marked {changed} message(s) as legitimate — they return to normal "
                "triage and similar mail is no longer held back from the AI.",
                type="positive",
                timeout=8000,
            )
        if problem:
            ui.notify(f"Some messages failed: {problem}", type="warning", timeout=7000)
        _reload()

    def _restore(ids: list[int]) -> None:
        if not ids:
            return
        try:
            changed = store.set_messages_trashed(ids, False)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Restore failed: {e}", type="negative")
            return
        _audit(store, "restore", ids, changed)
        ui.notify(f"Restored {changed} message(s) to the inbox.", type="positive")
        _reload()

    # ---------------- header ----------------
    with ui.row().classes("w-full items-center bb-toolbar"):
        with ui.column().classes("col").style("gap: 2px; min-width: 0"):
            ui.label(
                "Everything the screener held back from the AI, grouped by how likely it "
                "is to be a scam."
            ).style("font-size: 12.5px; color: var(--text-secondary)")
            ui.label(
                f"Spam and scam mail is deleted immediately. Anything else you delete is "
                f"held for {days} days, then removed automatically. {server_note}"
            ).style("font-size: 12px; color: var(--text-muted)")
        ui.space()
        if accounts:
            ui.select(
                {"": "All mailboxes", **{name: name for name in accounts}},
                value=account,
                on_change=lambda e: _go(account=str(e.value or "")),
            ).props("dense outlined").classes("w-52 bb-toolbar-grow")
        ui.button(icon="refresh", on_click=_reload).props("flat round dense").tooltip("Reload")

    @ui.refreshable
    def _boxes() -> None:
        """Three screening boxes plus the deletion queue, repainted in place."""
        for _key, title, blurb, status, tone in SECTIONS:

            def _actions(
                get_ids: Callable[[], list[int]],
                _rows: list[Any],
                status: str = status,
            ) -> list[Any]:
                buttons = []
                if status != "SPAM":
                    buttons.append(
                        ui.button(
                            "Mark as spam & learn",
                            icon="block",
                            on_click=lambda: _mark(get_ids(), "SPAM"),
                        ).props("outline dense no-caps color=negative")
                    )
                buttons.append(
                    ui.button(
                        "Not spam",
                        icon="verified",
                        on_click=lambda: _mark(get_ids(), "CONTENT"),
                    ).props("flat dense no-caps color=positive")
                )
                buttons.append(
                    ui.button(
                        "Delete",
                        icon="delete_forever",
                        on_click=lambda: _ask_purge(get_ids()),
                    ).props("flat dense no-caps color=negative")
                )
                return buttons

            _box(
                store=store,
                title=title,
                blurb=blurb,
                icon=(
                    "report" if status == "POTENTIAL_SPAM"
                    else "gpp_maybe" if status == "POTENTIAL_ISSUE"
                    else "block"
                ),
                tone=tone,
                rows=_fetch(store, status, account),
                subtitle_for=lambda row: (
                    str(_value(row, "screening_reason", "") or ""),
                    "text-secondary",
                ),
                actions=_actions,
            )

        def _queue_actions(
            get_ids: Callable[[], list[int]], _rows: list[Any]
        ) -> list[Any]:
            return [
                ui.button(
                    "Restore", icon="restore", on_click=lambda: _restore(get_ids())
                ).props("outline dense no-caps color=primary"),
                ui.button(
                    "Delete now",
                    icon="delete_forever",
                    on_click=lambda: _ask_purge(get_ids()),
                ).props("flat dense no-caps color=negative"),
            ]

        _box(
            store=store,
            title="Scheduled for deletion",
            blurb=(
                f"Mail you deleted elsewhere in the app waits here for {days} days, then "
                "is removed for good — from this app and from the mail server. Restore "
                "anything you want back before its date."
            ),
            icon="schedule",
            tone="warning",
            rows=_fetch_queue(store, settings, account),
            subtitle_for=lambda row: _due_text(_value(row, "due_at")),
            actions=_queue_actions,
            empty_text="Nothing is waiting to be deleted.",
        )

    _boxes()

    ui.label(
        "Links in these messages are never loaded or clickable. For any account, "
        "payment, renewal, or security claim, open the provider's site yourself."
    ).style("font-size: 11.5px; color: var(--text-muted)")
