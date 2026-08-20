"""Action-oriented home dashboard: received / needs action / needs reply.

One glance answers three questions (STYLE-GUIDE.md §Dashboard):
  * what came in (recent inbox, received today, unread),
  * what probably needs my action (pending/blocked AI drafts, quarantine),
  * what needs a reply (RESPOND/MEETING mail without a sent response).
Every row is clickable and lands on the message or draft it names.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from nicegui import ui

from .. import theme
from .mail import RangeSelection, _shift_held, _short_time, _snippet

log = logging.getLogger(__name__)

#: Per-inbox tile metrics: snapshot key, label, Mail view, accent colour.
#: The first five always render; the rest only when they are non-zero, so a
#: healthy inbox stays a clean five-cell grid.
_TILE_METRICS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("today", "New today", "today", "", True),
    ("unread", "Unread", "unread", "accent-hover", True),
    ("urgent", "Urgent", "urgent", "warning", True),
    ("needs_reply", "Needs reply", "needs-reply", "warning", True),
    ("needs_action", "Needs action", "needs-action", "warning", True),
    ("quarantined", "Quarantined", "quarantined", "error", False),
    ("questionable", "Review", "review", "warning", False),
    ("spam", "Spam", "spam", "error", False),
)


def _value(row: Any, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, default)


def _stat(value: int, label: str, *, color: str = "", href: str = "") -> None:
    chip = ui.element("div").classes("oce-stat")
    if href:
        chip.style("cursor: pointer").on("click", lambda h=href: ui.navigate.to(h))
    with chip:
        ui.label(str(value)).classes("oce-stat-n").style(
            f"color: var(--{color})" if color else ""
        )
        ui.label(label).classes("oce-stat-l")


def _inbox_url(account: str, view: str = "all") -> str:
    """Mail page, scoped to one inbox and one view."""
    return "/mail?" + urlencode({"account": account, "filter": view})


def _metric(value: int, label: str, href: str, *, color: str = "") -> None:
    cell = ui.element("div").classes("oce-metric" + ("" if value else " oce-metric--zero"))
    cell.on("click", lambda h=href: ui.navigate.to(h))
    with cell:
        ui.label(str(value)).classes("oce-metric-n").style(
            f"color: var(--{color})" if color and value else ""
        )
        ui.label(label).classes("oce-metric-l")


def _inbox_tile(metrics: dict[str, Any], site_labels: dict[str, str]) -> None:
    """One inbox: identity header plus a grid of click-through metrics."""
    account = str(metrics.get("account_name", "") or "")
    site_id = str(metrics.get("site_id", "") or "")
    with ui.card().classes("oce-card oce-inbox-tile"):
        with ui.row().classes("w-full items-center no-wrap").style("gap: 8px"):
            ui.icon("inbox", size="19px").style("color: var(--accent-hover)")
            with ui.column().classes("col").style("gap: 0; min-width: 0"):
                ui.label(account or "(unnamed inbox)").classes("oce-clip-1").style(
                    "font-weight: 800; font-size: 15px"
                )
                ui.label(str(metrics.get("username", "") or "")).classes("oce-clip-1").style(
                    "font-size: 11.5px; color: var(--text-muted)"
                )
            if site_id:
                theme.badge(site_labels.get(site_id, site_id), "accent")
        with ui.element("div").classes("oce-metric-grid"):
            for key, label, view, color, always in _TILE_METRICS:
                count = int(metrics.get(key, 0) or 0)
                if not always and not count:
                    continue
                _metric(count, label, _inbox_url(account, view), color=color)
        with ui.row().classes("w-full items-center no-wrap").style("gap: 6px"):
            ui.label(f"{int(metrics.get('total', 0) or 0)} in this inbox").style(
                "font-size: 11.5px; color: var(--text-muted)"
            )
            ui.space()
            ui.link("Open inbox", _inbox_url(account)).style("font-size: 12px")


class _BulkSelection:
    """Checkbox state shared by every message row on the dashboard.

    Rows live in three different cards, so the page owns one selection across
    all of them (in render order, so shift-click ranges still make sense) and
    one action bar that applies Read / Unread / Archive / Delete to whatever
    is ticked — the same store calls the Mail page uses.
    """

    def __init__(self, store: object) -> None:
        self.store = store
        self.visible_ids: list[int] = []
        # one message can appear in two lists (e.g. needs-reply AND recent);
        # every copy's box mirrors the same model state.
        self.checkboxes: dict[int, list[Any]] = {}
        self.model: RangeSelection | None = None
        self.label: Any = None
        self.bar: Any = None
        self.buttons: list[Any] = []
        self._syncing = False

    # rows register before the model exists (render order); finalize after.
    def add_row(self, message_id: int, checkbox: Any) -> None:
        if message_id not in self.checkboxes:
            self.visible_ids.append(message_id)
        self.checkboxes.setdefault(message_id, []).append(checkbox)

    def finalize(self) -> None:
        self.model = RangeSelection(self.visible_ids)
        self._sync()

    def note_intent(self, message_id: int, shift: bool) -> None:
        if self.model is not None:
            self.model.note_intent(message_id, shift)

    def toggle(self, message_id: int, checked: bool) -> None:
        if self._syncing or self.model is None:
            return
        others = self.model.toggle(message_id, checked)
        self._syncing = True
        try:
            for mid in [*others, message_id]:
                for box in self.checkboxes.get(mid, []):
                    if bool(box.value) != checked:
                        box.set_value(checked)
        finally:
            self._syncing = False
        self._sync()

    def clear(self) -> None:
        if self.model is None:
            return
        self.model.set_all(False)
        self._syncing = True
        try:
            for boxes in self.checkboxes.values():
                for box in boxes:
                    if box.value:
                        box.set_value(False)
        finally:
            self._syncing = False
        self._sync()

    def _sync(self) -> None:
        n = len(self.model.ids) if self.model else 0
        if self.label is not None:
            self.label.set_text(f"{n} selected" if n else "Tick messages for bulk actions")
        for b in self.buttons:
            b.enable() if n else b.disable()
        if self.bar is not None:
            self.bar.classes(remove="oce-bulkbar--active", add="oce-bulkbar--active" if n else "")

    def apply(self, action: str) -> None:
        ids = self.model.ids if self.model else []
        if not ids:
            return
        try:
            if action == "read":
                changed = self.store.mark_messages_seen(ids, True)  # type: ignore[attr-defined]
                notice = f"Marked {changed} message(s) read."
            elif action == "unread":
                changed = self.store.mark_messages_seen(ids, False)  # type: ignore[attr-defined]
                notice = f"Marked {changed} message(s) unread."
            elif action == "archive":
                changed = self.store.set_messages_archived(ids, True)  # type: ignore[attr-defined]
                notice = f"Archived {changed} message(s)."
            elif action == "trash":
                changed = self.store.set_messages_trashed(ids, True)  # type: ignore[attr-defined]
                notice = (
                    f"Moved {changed} message(s) to local Trash. "
                    "The provider mailbox was not changed."
                )
            else:
                raise ValueError(f"unsupported bulk action: {action}")
            try:
                from ...audit.log import AuditLog

                AuditLog(self.store).record(
                    actor="user",
                    event="tool_call",
                    subject_table="messages",
                    subject_id=ids[0] if len(ids) == 1 else None,
                    detail={"action": action, "selected": len(ids), "changed": changed,
                            "page": "dashboard"},
                )
            except Exception:  # noqa: BLE001
                log.exception("audit append failed for dashboard bulk action")
            ui.notify(notice, type="positive", timeout=6000)
            refresh.refresh()
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Bulk action failed: {e}", type="negative")

    def render_bar(self) -> None:
        """The action strip above the columns (enabled once something is ticked)."""
        with (
            ui.row()
            .classes("w-full items-center oce-card oce-toolbar oce-bulkbar")
            .style("gap: 6px; padding: 6px 10px") as bar
        ):
            self.bar = bar
            ui.icon("checklist", size="18px").style("color: var(--text-secondary)")
            self.label = ui.label("Tick messages for bulk actions").style(
                "font-size: 12px; color: var(--text-secondary)"
            )
            ui.label("shift-click for a range").classes("oce-header-note").style(
                "font-size: 11px; color: var(--text-muted)"
            )
            ui.space()
            self.buttons = [
                ui.button("Read", icon="drafts", on_click=lambda: self.apply("read")).props(
                    "flat dense no-caps"
                ),
                ui.button(
                    "Unread", icon="mark_email_unread", on_click=lambda: self.apply("unread")
                ).props("flat dense no-caps"),
                ui.button(
                    "Archive", icon="archive", on_click=lambda: self.apply("archive")
                ).props("flat dense no-caps"),
                ui.button(
                    "Delete", icon="delete_outline", on_click=lambda: self.apply("trash")
                ).props("flat dense no-caps color=negative"),
                ui.button("Clear", icon="close", on_click=self.clear).props(
                    "flat dense no-caps"
                ),
            ]


def _message_row(
    row: Any, *, show_draft_state: bool = True, selection: _BulkSelection | None = None
) -> None:
    """One clickable inbox row: sender, wrapped subject, snippet, badges.

    With ``selection`` a checkbox sits at the left edge (click-through to the
    message is suppressed on the box itself) so dashboard rows can be bulk
    read/archived/deleted without a trip to the Mail page.
    """
    message_id = int(_value(row, "id"))
    with ui.element("div").classes("oce-row w-full").on(
        "click", lambda _e, i=message_id: ui.navigate.to(f"/mail?message_id={i}")
    ):
        with ui.row().classes("w-full items-start no-wrap").style("gap: 9px"):
            if selection is not None:
                box = (
                    ui.checkbox(
                        value=False,
                        on_change=lambda e, mid=message_id: selection.toggle(mid, bool(e.value)),
                    )
                    .props("dense")
                    .style("margin: -5px -3px 0 -5px; flex-shrink: 0")
                )
                box.on("click", js_handler="event => event.stopPropagation()")
                box.on(
                    "mousedown",
                    lambda e, mid=message_id: selection.note_intent(mid, _shift_held(e)),
                    ["shiftKey"],
                )
                box.tooltip("Select message — shift-click to select a range")
                selection.add_row(message_id, box)
            ui.icon(
                "mark_email_unread" if not _value(row, "seen", 0) else "mail",
                size="18px",
            ).style("color: var(--accent-hover); margin-top: 3px")
            with ui.column().classes("col").style("gap: 2px; min-width: 0"):
                with ui.row().classes("w-full items-center no-wrap").style("gap: 8px"):
                    ui.label(
                        str(
                            _value(row, "from_name")
                            or _value(row, "from_addr")
                            or "Unknown"
                        )
                    ).classes("oce-clip-1").style("font-weight: 700")
                    ui.space()
                    ui.label(_short_time(_value(row, "received_at"))).style(
                        "font-size: 11px; color: var(--text-muted); flex-shrink: 0"
                    )
                theme.subject_label(
                    _value(row, "subject", ""),
                    style="color: var(--text-secondary); font-size: 13px",
                )
                snippet = _snippet(_value(row, "sanitized_text"), 90)
                withheld = str(
                    _value(row, "screening_status", "UNSCREENED") or "UNSCREENED"
                ).upper() in {"POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"}
                if snippet and not _value(row, "quarantined", 0) and not withheld:
                    ui.label(snippet).classes("oce-clip-1").style(
                        "font-size: 11.5px; color: var(--text-muted)"
                    )
                with ui.row().classes("items-center").style("gap: 5px"):
                    if _value(row, "quarantined", 0):
                        theme.quarantine_badge()
                    theme.screening_badge(_value(row, "screening_status"))
                    category = str(_value(row, "category", "") or "")
                    if category:
                        theme.badge(category, "accent")
                    if show_draft_state and _value(row, "draft_id"):
                        theme.state_badge(str(_value(row, "draft_state", "") or ""))


@ui.refreshable
def refresh(store: object, settings: object) -> None:
    try:
        snapshot = store.dashboard_snapshot(recent_limit=6, draft_limit=6)  # type: ignore[attr-defined]
    except Exception:
        log.exception("dashboard snapshot failed")
        snapshot = {}

    triage = snapshot.get("triage", {})
    drafts = snapshot.get("drafts", {})
    pending = int(drafts.get("PENDING", 0))
    blocked = int(drafts.get("BLOCKED", 0)) + int(drafts.get("DEFERRED_NO_LLM", 0))
    quarantined = int(triage.get("quarantined", 0))
    questionable = int(triage.get("questionable", 0))
    spam = int(triage.get("spam", 0))

    # --- stat row: the three questions, at a glance ---------------------------
    with ui.row().classes("w-full items-stretch").style("gap: 10px; flex-wrap: wrap"):
        _stat(int(triage.get("today", 0)), "Received today", href="/mail?filter=today")
        _stat(int(triage.get("unread", 0)), "Unread", color="accent-hover",
              href="/mail?filter=unread")
        _stat(int(triage.get("needs_reply", 0)), "Needs reply", color="warning",
              href="/mail?filter=needs-reply")
        _stat(int(triage.get("needs_action", 0)), "Needs action", color="warning",
              href="/mail?filter=needs-action")
        _stat(pending, "AI drafts to review", color="accent-hover", href="/drafts")
        if quarantined:
            _stat(quarantined, "Quarantined", color="error",
                  href="/mail?filter=quarantined")
        if questionable:
            _stat(questionable, "Questionable", color="warning",
                  href="/mail?filter=review")
        if spam:
            _stat(spam, "Filtered spam", color="error", href="/mail?filter=spam")

    with ui.row().classes("w-full oce-toolbar"):
        ui.button(
            "Compose email", icon="edit", on_click=lambda: ui.navigate.to("/compose")
        ).props("unelevated no-caps color=primary")
        ui.button(
            "Open inbox", icon="inbox", on_click=lambda: ui.navigate.to("/mail")
        ).props("outline no-caps color=primary")
        if pending:
            ui.button(
                f"Review {pending} AI draft{'s' if pending != 1 else ''}",
                icon="auto_awesome",
                on_click=lambda: ui.navigate.to("/drafts"),
            ).props("outline no-caps color=primary")

    # --- one tile per inbox: metrics that click through to a filtered view ----
    try:
        inboxes = list(store.inbox_metrics())  # type: ignore[attr-defined]
    except Exception:
        log.exception("inbox metrics failed")
        inboxes = []
    if inboxes:
        site_labels = {
            sid: site.name for sid, site in (getattr(settings, "sites", None) or {}).items()
        }
        with ui.row().classes("w-full items-center").style("gap: 8px; margin-top: 2px"):
            ui.icon("all_inbox", size="18px").style("color: var(--text-secondary)")
            ui.label("Inboxes").style("font-size: 16px; font-weight: 800")
        with ui.row().classes("w-full items-stretch").style("gap: 12px; flex-wrap: wrap"):
            for metrics in inboxes:
                _inbox_tile(metrics, site_labels)

    # --- bulk selection across the three message lists ------------------------
    selection = _BulkSelection(store)
    selection.render_bar()

    # --- three columns: needs reply / needs action / recent -------------------
    with ui.row().classes("w-full items-stretch").style("gap: 14px; flex-wrap: wrap"):
        with ui.card().classes("oce-card").style(
            "padding: 14px; flex: 1 1 400px; min-width: 0"
        ):
            with ui.row().classes("w-full items-center"):
                ui.icon("reply", size="18px").style("color: var(--warning)")
                ui.label("Needs a reply").style("font-size: 16px; font-weight: 800")
                ui.space()
                ui.link("View all", "/mail?filter=needs-reply").style("font-size: 12px")
            rows = list(snapshot.get("needs_reply", []))
            if not rows:
                ui.label("Nothing is waiting on a response.").style(
                    "color: var(--text-secondary)"
                )
            for row in rows:
                _message_row(row, selection=selection)

        with ui.card().classes("oce-card").style(
            "padding: 14px; flex: 1 1 400px; min-width: 0"
        ):
            with ui.row().classes("w-full items-center"):
                ui.icon("pending_actions", size="18px").style("color: var(--accent-hover)")
                ui.label("Needs your action").style("font-size: 16px; font-weight: 800")
                ui.space()
                ui.link("Open AI Review", "/drafts").style("font-size: 12px")
            action_rows = list(snapshot.get("actionable_drafts", []))
            q_rows = list(snapshot.get("quarantined_messages", []))
            review_rows = list(snapshot.get("questionable_messages", []))
            if not action_rows and not q_rows and not review_rows and not blocked:
                ui.label("All clear — nothing to approve or release.").style(
                    "color: var(--text-secondary)"
                )
            for row in q_rows:
                _message_row(row, show_draft_state=False, selection=selection)
            for row in review_rows:
                _message_row(row, show_draft_state=False, selection=selection)
            for row in action_rows:
                did = int(_value(row, "id"))
                with ui.element("div").classes("oce-row w-full").on(
                    "click", lambda _e, i=did: ui.navigate.to(f"/detail/{i}")
                ):
                    with ui.row().classes("w-full items-center no-wrap").style("gap: 8px"):
                        with ui.column().classes("col").style("gap: 2px; min-width: 0"):
                            theme.subject_label(
                                _value(row, "subject", ""), style="font-weight: 700"
                            )
                            ui.label(
                                f"→ {_value(row, 'recipient', '') or ''}"
                            ).classes("oce-clip-1").style(
                                "font-size: 11.5px; color: var(--text-muted)"
                            )
                        theme.state_badge(str(_value(row, "state", "")))

        with ui.card().classes("oce-card").style(
            "padding: 14px; flex: 1 1 400px; min-width: 0"
        ):
            with ui.row().classes("w-full items-center"):
                ui.icon("inbox", size="18px").style("color: var(--text-secondary)")
                ui.label("Recent inbox").style("font-size: 16px; font-weight: 800")
                ui.space()
                ui.link("View all", "/mail").style("font-size: 12px")
            messages = list(snapshot.get("recent_received", []))
            if not messages:
                ui.label("No received mail yet.").style("color: var(--text-secondary)")
            for row in messages:
                _message_row(row, selection=selection)
    selection.finalize()

    accounts = list(snapshot.get("accounts", []))
    with ui.expansion("How the email automation works", icon="account_tree").classes(
        "w-full oce-card"
    ):
        ui.markdown(
            "1. Mail is received, sanitized, and stored locally; suspicious "
            "messages are **quarantined** (no AI ever reads them until you "
            "release them).\n"
            "2. Content-only inboxes screen off-topic mail first. Login/account "
            "links stay disabled; open the provider site directly. Questionable "
            "mail remains human-only until you align it.\n"
            "3. The site-specific bot classifies eligible messages and retrieves only "
            "that site's knowledge.\n"
            "4. AI prepares a draft; it **cannot send**.\n"
            "5. You review, revise (chat with the AI), and explicitly approve.\n"
            "6. Email activity and agent notes feed the OpenClaw brief."
        )
        if accounts:
            ui.label(
                f"{len(accounts)} configured inbox{'es' if len(accounts) != 1 else ''}: "
                + ", ".join(str(_value(a, "username", "")) for a in accounts)
            ).style("font-size: 12px; color: var(--text-secondary)")


def render(store: object, settings: object) -> None:
    refresh(store, settings)
