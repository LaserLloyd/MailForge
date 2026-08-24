"""Conventional received-mail inbox with split-pane and full-window reading."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from nicegui import ui

from .. import markdown as markdown_ui
from .. import theme
from . import assistant

log = logging.getLogger(__name__)

_VIEW_LABELS: dict[str, str] = {
    "all": "All messages",
    "today": "Received today",
    "unread": "Unread",
    "urgent": "Urgent",
    "needs-reply": "Needs reply",
    "needs-action": "Needs action",
    "drafted": "AI drafted",
    "quarantined": "Quarantined",
    "review": "Questionable",
    "spam": "Filtered spam",
    "archived": "Archived",
    "trash": "Trash",
}

#: Views that already cover a live tag one-for-one — listing both would put two
#: entries that do the same thing in the picker.
_VIEW_COVERED_TAGS = frozenset({"quarantined", "spam"})


_TAG_LABELS = {
    "quarantined": "Quarantined",
    "spam": "Spam",
    "screening:POTENTIAL_SPAM": "Potential spam",
    "screening:POTENTIAL_ISSUE": "Potential issue",
    "screening:CONTENT": "Content confirmed",
}


def _tag_label(tag: str) -> str:
    if tag in _TAG_LABELS:
        return _TAG_LABELS[tag]
    _kind, _separator, value = str(tag).partition(":")
    return value.replace("_", " ").strip().title() or str(tag)


def filter_options(tag_counts: dict[str, int] | None = None) -> dict[str, str]:
    """Mailbox views plus every live tag which has at least one message."""
    options = {f"view:{key}": label for key, label in _VIEW_LABELS.items()}
    live_tags = [
        (tag, int(count))
        for tag, count in (tag_counts or {}).items()
        if int(count) > 0 and tag not in _VIEW_COVERED_TAGS
    ]
    for tag, count in sorted(live_tags, key=lambda item: _tag_label(item[0]).lower()):
        options[f"tag:{tag}"] = f"{_tag_label(tag)} ({count})"
    return options


def _short_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        today = datetime.now().astimezone().date()
        # Compare local DATES, not "within 24 h": 21:00 yesterday must not
        # read as a bare "21:00" next to this morning's mail.
        if local.date() == today:
            return local.strftime("%H:%M")
        if (today - local.date()).days < 7:
            return local.strftime("%a %H:%M")
        return local.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(iso)[:16].replace("T", " ")


def _snippet(text: str | None, n: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def _value(row: Any, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, default)


def _shift_held(event: Any) -> bool:
    """True when the shift key was down for a browser event.

    NiceGUI delivers a single filtered DOM argument as a dict, but hands back a
    list when more than one argument survives the filter — accept either rather
    than depending on that detail.
    """
    args = getattr(event, "args", None)
    if isinstance(args, list):
        args = args[0] if args else None
    if isinstance(args, dict):
        return bool(args.get("shiftKey"))
    return False


class RangeSelection:
    """Which messages are ticked, including shift-click range extension.

    Pure state, deliberately free of any widget: the page owns the checkboxes
    and pushes back only the ids :meth:`toggle` reports as changed. The anchor
    follows plain clicks only, so a run of shift-clicks all extend from the same
    origin (the behaviour every mail client has trained people to expect).
    """

    def __init__(self, visible_ids: list[int]) -> None:
        self.visible_ids = [int(i) for i in visible_ids]
        self.selected: set[int] = set()
        self.anchor: int | None = None
        self._pending: tuple[int, bool] | None = None

    def note_intent(self, message_id: int, shift: bool) -> None:
        """Record the modifier state seen on a checkbox mousedown."""
        self._pending = (int(message_id), bool(shift))

    def toggle(self, message_id: int, checked: bool) -> list[int]:
        """Apply one checkbox change; return the *other* ids that changed."""
        message_id = int(message_id)
        intent, self._pending = self._pending, None
        # Honour an intent only for the row it was recorded on: a keyboard
        # toggle emits no mousedown and must not inherit an earlier row's shift.
        extend = bool(
            intent
            and intent[0] == message_id
            and intent[1]
            and self.anchor is not None
            and self.anchor in self.visible_ids
            and message_id in self.visible_ids
        )
        if not extend:
            self._set(message_id, checked)
            self.anchor = message_id
            return []
        start, end = sorted(
            (self.visible_ids.index(self.anchor), self.visible_ids.index(message_id))  # type: ignore[arg-type]
        )
        span = self.visible_ids[start : end + 1]
        for mid in span:
            self._set(mid, checked)
        return [mid for mid in span if mid != message_id]

    def set_all(self, checked: bool) -> None:
        self.anchor = None
        self._pending = None
        self.selected = set(self.visible_ids) if checked else set()

    def _set(self, message_id: int, checked: bool) -> None:
        if checked:
            self.selected.add(message_id)
        else:
            self.selected.discard(message_id)

    @property
    def ids(self) -> list[int]:
        """Selected ids still on screen, in ascending order."""
        self.selected.intersection_update(self.visible_ids)
        return sorted(self.selected)

    @property
    def all_selected(self) -> bool:
        return bool(self.visible_ids) and len(self.ids) == len(self.visible_ids)


def _notify_then_navigate(notice: str, url: str, *, kind: str = "positive") -> None:
    """Show a result message, then reload the list a moment later.

    ``ui.navigate`` tears the page down, taking any toast with it — so a notify
    immediately followed by a navigate is a message nobody ever reads. Deleting
    now explains a retention policy the user has to know about, so the reload
    waits for the toast to land.
    """
    ui.notify(notice, type=kind, timeout=7000)
    ui.timer(2.6, lambda: ui.navigate.to(url), once=True)


def _delete_notice(settings: object, changed: int) -> str:
    """What a delete actually did, in the user's terms.

    Deleting is a two-stage policy (see :mod:`mail.retention`): the message goes
    to the local holding box now and is destroyed — here and at the provider —
    once its retention period is up. Saying only "moved to Trash" would understate
    it; saying "deleted" would overstate it.
    """
    from ...mail.retention import delete_mode, retention_days

    days = retention_days(settings)
    tail = {
        "trash": "moved to your provider's Trash folder",
        "expunge": "removed from the mail server too",
        "off": "left untouched on the mail server (provider deletion is off)",
    }[delete_mode(settings)]
    return (
        f"Deleted {changed} message(s). They are held for {days} days — restore them from "
        f"Trash before then — after which they are erased here and {tail}."
    )


def _account_options(settings: object) -> list[str]:
    return [a.name for a in (getattr(settings, "imap_accounts", []) or [])]


def reply_compose_url(
    to_addr: str,
    subject: str,
    *,
    template_id: int | None = None,
    account: str = "",
    reply_to: int | None = None,
) -> str:
    """Compose link for a reply. ``account`` = the mailbox the mail arrived on
    (so the reply goes out from it, not from the first configured mailbox);
    ``reply_to`` = local message id (threading headers + quoted text)."""
    values: dict[str, Any] = {"to": to_addr or "", "subject": subject or ""}
    if template_id is not None:
        values["template_id"] = int(template_id)
    if account:
        values["account"] = account
    if reply_to is not None:
        values["reply_to"] = int(reply_to)
    return "/compose?" + urlencode(values)


def _message_url(message_id: int, **filters: str) -> str:
    params = {k: v for k, v in filters.items() if v}
    params["message_id"] = int(message_id)
    return "/mail?" + urlencode(params)


def _render_row(
    row: Any,
    selected_id: int | None,
    filters: dict[str, str],
    site_labels: dict[str, str] | None = None,
    on_selection_changed: Callable[[int, bool], None] | None = None,
    on_click_intent: Callable[[int, bool], None] | None = None,
) -> Any | None:
    message_id = int(_value(row, "id"))
    unseen = not bool(_value(row, "seen", 0))
    quarantined = bool(_value(row, "quarantined", 0))
    screening_status = str(_value(row, "screening_status", "UNSCREENED") or "UNSCREENED")
    classes = "oce-row w-full"
    if selected_id == message_id:
        classes += " oce-row--selected"
    with (
        ui.element("div")
        .classes(classes)
        .on("click", lambda: ui.navigate.to(_message_url(message_id, **filters)))
    ):
        with ui.row().classes("w-full items-start no-wrap").style("gap: 9px"):
            checkbox = None
            if on_selection_changed is not None:
                checkbox = (
                    ui.checkbox(
                        value=False,
                        on_change=lambda e, mid=message_id: on_selection_changed(
                            mid, bool(e.value)
                        ),
                    )
                    .props("dense")
                    .style("margin: -5px -3px 0 -5px; flex-shrink: 0")
                )
                checkbox.on("click", js_handler="event => event.stopPropagation()")
                if on_click_intent is not None:
                    # mousedown strictly precedes both the click and the value
                    # change, so the modifier state always reaches the server
                    # before the selection handler that consumes it.
                    checkbox.on(
                        "mousedown",
                        lambda e, mid=message_id: on_click_intent(
                            mid, _shift_held(e)
                        ),
                        ["shiftKey"],
                    )
                checkbox.tooltip("Select message — shift-click to select a range")
            ui.icon("circle", size="8px").style(
                "color: var(--accent-hover); margin-top: 7px"
                if unseen
                else "color: transparent; margin-top: 7px"
            )
            with ui.column().classes("col").style("gap: 2px; min-width: 0"):
                with ui.row().classes("w-full items-center no-wrap").style("gap: 6px"):
                    ui.label(
                        str(
                            _value(row, "from_name")
                            or _value(row, "from_addr")
                            or "(unknown sender)"
                        )
                    ).classes("oce-clip-1").style(
                        "font-weight: 800" if unseen else "font-weight: 600"
                    )
                    ui.space()
                    ui.label(_short_time(_value(row, "received_at"))).style(
                        "font-size: 10.5px; color: var(--text-muted); flex-shrink: 0"
                    )
                theme.subject_label(
                    _value(row, "subject"),
                    style=("font-weight: 650; " if unseen else "")
                    + "font-size: 13px; color: var(--text-secondary)",
                )
                if not quarantined:
                    ui.label(_snippet(_value(row, "sanitized_text"), 96)).classes(
                        "oce-clip-1"
                    ).style("font-size: 11.5px; color: var(--text-muted)")
                with ui.row().classes("items-center").style("gap: 4px"):
                    if quarantined:
                        theme.quarantine_badge()
                    theme.screening_badge(screening_status)
                    site = str(_value(row, "site_id", ""))
                    if site:
                        labels = site_labels or {}
                        theme.badge(labels.get(site, site), "accent")
                    category = str(_value(row, "category", "") or "")
                    if category:
                        theme.badge(category)
                    if _value(row, "has_attachments", 0):
                        ui.icon("attach_file", size="14px").style("color: var(--text-muted)")
                    if _value(row, "draft_id"):
                        theme.badge(
                            str(_value(row, "draft_state", "DRAFT") or "DRAFT"),
                            "success" if _value(row, "draft_state") == "SENT" else "warning",
                        )
    return checkbox


def _size_label(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return "?"


def _attachments_card(store: object, message_id: int, has_flag: bool) -> None:
    """Attachment list with explicit human-triggered downloads.

    Files were saved at ingest (size-capped, sanitized names); bytes never
    reach any LLM. The download button is only rendered inside an
    authenticated page; ``ui.download.file`` then serves the bytes from a
    one-shot, unguessable (UUID) static route behind the Host-header guard.
    """
    try:
        rows = list(store.list_attachments(int(message_id)))  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        log.debug("list_attachments failed: %s", e)
        rows = []
    if not rows and not has_flag:
        return
    with ui.card().classes("w-full oce-card").style("padding: 12px; gap: 6px"):
        with ui.row().classes("items-center").style("gap: 7px"):
            ui.icon("attach_file", size="18px").style("color: var(--text-secondary)")
            ui.label(f"Attachments ({len(rows)})").style("font-weight: 700")
        if not rows:
            ui.label(
                "This message reported attachments, but it was received before "
                "attachment storage existed — files were not downloaded."
            ).style("font-size: 12px; color: var(--text-muted)")
            return
        for att in rows:
            path = _value(att, "stored_path")
            skipped = _value(att, "skipped_reason")
            with ui.row().classes("w-full items-center no-wrap").style("gap: 8px"):
                ui.icon(
                    "image"
                    if str(_value(att, "content_type", "") or "").startswith("image/")
                    else "description",
                    size="17px",
                ).style("color: var(--accent-hover)")
                ui.label(str(_value(att, "filename", "attachment"))).classes("oce-clip-1").style(
                    "font-weight: 600; font-size: 13px"
                )
                ui.label(
                    f"{_size_label(int(_value(att, 'size_bytes', 0) or 0))}"
                    + (f" · {_value(att, 'content_type')}" if _value(att, "content_type") else "")
                ).style("font-size: 11.5px; color: var(--text-muted); flex-shrink: 0")
                ui.space()
                if skipped or not path:
                    theme.badge(str(skipped or "not stored"), "warning")
                else:

                    def _download(
                        p: str = str(path), n: str = str(_value(att, "filename"))
                    ) -> None:
                        import os

                        if not os.path.isfile(p):
                            ui.notify("File is missing on disk.", type="negative")
                            return
                        ui.download.file(p, filename=n)

                    ui.button(icon="download", on_click=_download).props(
                        "flat round dense"
                    ).tooltip("Download")
        ui.label(
            "Attachments are stored locally and are never read by any AI. "
            "Open them only if you trust the sender."
        ).style("font-size: 11px; color: var(--text-muted)")


def _message_reader(
    store: object,
    settings: object,
    message_id: int,
    bridge: object | None,
    *,
    full_page: bool = False,
    filters: dict[str, str] | None = None,
    neighbors: tuple[int | None, int | None] = (None, None),
) -> None:
    """The reading pane. ``filters`` is the list view the user came from, so
    archive/trash/mark-unread return there instead of to the unfiltered
    inbox; ``neighbors`` = (previous id, next id) in that list for ⬆/⬇."""
    filters = {k: v for k, v in (filters or {}).items() if v}
    back_url = "/mail?" + urlencode(filters) if filters else "/mail"
    try:
        msg = store.get_message(int(message_id))  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        log.warning("get_message failed: %s", e)
        msg = None
    if msg is None:
        with ui.column().classes("w-full items-center q-pa-xl"):
            ui.icon("search_off", size="42px").style("color: var(--text-muted)")
            ui.label("Message not found.").style("color: var(--text-secondary)")
        return

    try:
        store.mark_message_seen(int(message_id))  # type: ignore[attr-defined]
    except Exception:
        pass

    sender = _value(msg, "from_name") or _value(msg, "from_addr") or "(unknown sender)"
    subject = theme.clean_subject(_value(msg, "subject"))
    quarantined = bool(_value(msg, "quarantined", 0))
    archived = bool(_value(msg, "archived", 0))
    trashed = bool(_value(msg, "trashed", 0))
    screening_status = str(_value(msg, "screening_status", "UNSCREENED") or "UNSCREENED").upper()
    screening_withheld = screening_status in {"POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"}
    reply_to = str(_value(msg, "from_addr") or "")
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    try:
        reply_account = store.account_username(_value(msg, "account_id")) or ""  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        reply_account = ""
    try:
        working = store.latest_draft_for_message(int(message_id))  # type: ignore[attr-defined]
    except Exception:
        working = None

    def _current_body() -> str:
        return str(_value(working, "body", "") or "")

    def _apply_body(_body: str, draft_id: int) -> None:
        ui.navigate.to(f"/detail/{draft_id}")

    ai_drawer = assistant.render_drawer(
        store,
        settings,
        bridge,
        message=msg,
        draft_id=int(_value(working, "id")) if working is not None else None,
        current_body=_current_body,
        apply_body=_apply_body,
    )

    def _mark_unread() -> None:
        try:
            store.mark_message_unseen(message_id)  # type: ignore[attr-defined]
            ui.notify("Marked unread.", type="positive")
            ui.navigate.to(back_url)
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Could not mark unread: {e}", type="negative")

    def _archive() -> None:
        try:
            store.set_message_archived(message_id, True)  # type: ignore[attr-defined]
            ui.notify("Message archived.", type="positive")
            ui.navigate.to(back_url)
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Could not archive: {e}", type="negative")

    def _trash() -> None:
        try:
            store.set_message_trashed(message_id, True)  # type: ignore[attr-defined]
            _notify_then_navigate(_delete_notice(settings, 1), back_url)
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Could not move message to Trash: {e}", type="negative")

    def _restore() -> None:
        try:
            if trashed:
                store.set_message_trashed(message_id, False)  # type: ignore[attr-defined]
            if archived:
                store.set_message_archived(message_id, False)  # type: ignore[attr-defined]
            ui.notify("Message restored to the inbox.", type="positive")
            ui.navigate.to(_message_url(message_id))
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Could not restore message: {e}", type="negative")

    def _record_screening(label: str, learn_similar: bool, note: str = "") -> None:
        try:
            store.record_screening_feedback(  # type: ignore[attr-defined]
                message_id,
                label,
                actor="user",
                note=note,
                learn_similar=learn_similar,
            )
            try:
                from ...audit.log import AuditLog

                AuditLog(store).record(
                    actor="user",
                    event="approval",
                    subject_table="messages",
                    subject_id=message_id,
                    detail={
                        "action": "screening_feedback",
                        "label": label,
                        "learn_similar": bool(learn_similar),
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("audit append failed for screening feedback")
            if label == "SPAM":
                ui.notify(
                    "Marked as spam. Similar sender/subject patterns will be filtered locally; "
                    "nothing was deleted on the mail server.",
                    type="positive",
                    timeout=7000,
                )
                ui.navigate.to("/mail?" + urlencode({"tag": "spam"}))
            elif label == "CONTENT":
                ui.notify(
                    "Marked content-related. Similar messages may proceed to normal triage; "
                    "account/security claims still require direct-site verification.",
                    type="positive",
                    timeout=7000,
                )
                ui.navigate.to(_message_url(message_id))
            else:
                ui.notify("Added to the questionable review queue.", type="positive")
                ui.navigate.to(_message_url(message_id, tag=f"screening:{label}"))
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Could not save screening feedback: {e}", type="negative")

    with (
        ui.dialog() as review_dialog,
        ui.card().classes("oce-card").style("width: min(560px, 94vw); padding: 18px"),
    ):
        ui.label("Flag for review").style("font-size: 17px; font-weight: 800")
        ui.label(
            "Potential spam is unsolicited or off-topic. Potential issue is a login, "
            "account, payment, renewal, or legal claim that you will verify by opening "
            "the known site directly. Neither choice teaches an automatic spam rule yet."
        ).style("font-size: 12.5px; color: var(--text-secondary)")
        review_note = (
            ui.textarea(
                "Optional note for future alignment",
                placeholder="Why this is questionable…",
            )
            .props("outlined autogrow")
            .classes("w-full")
        )
        with ui.row().classes("w-full justify-end oce-toolbar"):
            ui.button("Cancel", on_click=review_dialog.close).props("flat no-caps")
            ui.button(
                "Potential spam",
                icon="report",
                on_click=lambda: _record_screening(
                    "POTENTIAL_SPAM", False, str(review_note.value or "")
                ),
            ).props("outline no-caps color=warning")
            ui.button(
                "Potential issue",
                icon="gpp_maybe",
                on_click=lambda: _record_screening(
                    "POTENTIAL_ISSUE", False, str(review_note.value or "")
                ),
            ).props("outline no-caps color=negative")

    with ui.row().classes("w-full items-center oce-toolbar").style("gap: 6px"):
        if full_page:
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to(back_url)).props(
                "flat round dense"
            )
        with ui.column().classes("col").style("gap: 1px; min-width: 0"):
            theme.subject_label(subject, full=True, style="font-size: 18px; font-weight: 800")
            ui.label(f"{sender} · {_short_time(_value(msg, 'received_at'))}").style(
                "font-size: 12px; color: var(--text-secondary)"
            )
        prev_id, next_id = neighbors
        if prev_id is not None or next_id is not None:
            prev_btn = ui.button(
                icon="expand_less",
                on_click=lambda: ui.navigate.to(_message_url(int(prev_id), **filters)),  # type: ignore[arg-type]
            ).props("flat round dense").tooltip("Previous message (k)")
            next_btn = ui.button(
                icon="expand_more",
                on_click=lambda: ui.navigate.to(_message_url(int(next_id), **filters)),  # type: ignore[arg-type]
            ).props("flat round dense").tooltip("Next message (j)")
            if prev_id is None:
                prev_btn.props("disable")
            if next_id is None:
                next_btn.props("disable")
        ui.button(icon="mark_email_unread", on_click=_mark_unread).props(
            "flat round dense"
        ).tooltip("Mark unread")
        if archived or trashed:
            ui.button(icon="restore", on_click=_restore).props("flat round dense").tooltip(
                "Restore to inbox"
            )
        else:
            ui.button(icon="archive", on_click=_archive).props("flat round dense").tooltip(
                "Archive"
            )
        if not trashed:
            ui.button(icon="delete_outline", on_click=_trash).props(
                "flat round dense color=negative"
            ).tooltip("Move to local Trash")
        if not full_page:
            ui.button(
                icon="open_in_new",
                on_click=lambda: ui.navigate.to(f"/mail/{message_id}", new_tab=True),
            ).props("flat round dense").tooltip("Open in new window")

    if quarantined:
        with (
            ui.card().classes("w-full oce-card").style("padding: 12px; border-color: var(--error)")
        ):
            with ui.row().classes("w-full items-center").style("gap: 8px"):
                ui.icon("gpp_maybe", size="22px").style("color: var(--error)")
                with ui.column().classes("col").style("gap: 1px"):
                    ui.label("Quarantined — suspected prompt injection").style(
                        "font-weight: 800; color: var(--error)"
                    )
                    ui.label(
                        str(_value(msg, "quarantine_reason", "") or "")
                        + " · No AI (local model or OpenClaw) can read this "
                        "message until you release it. Reading it here is safe."
                    ).style("font-size: 12px; color: var(--text-secondary)")

                def _release() -> None:
                    try:
                        store.set_message_quarantined(  # type: ignore[attr-defined]
                            message_id, False, None
                        )
                        try:
                            from ...audit.log import AuditLog

                            AuditLog(store).record(
                                actor="user",
                                event="approval",
                                subject_table="messages",
                                subject_id=message_id,
                                detail={"action": "quarantine_release"},
                            )
                        except Exception:  # noqa: BLE001
                            log.exception("audit append failed for release")
                        ui.notify("Released from quarantine.", type="positive")
                        ui.navigate.to(_message_url(message_id))
                    except Exception as e:  # noqa: BLE001
                        ui.notify(f"Release failed: {e}", type="negative")

                ui.button("Release", icon="lock_open", on_click=_release).props(
                    "outline no-caps color=negative"
                )

    if screening_status in {"POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"}:
        severe = screening_status in {"POTENTIAL_ISSUE", "SPAM"}
        with (
            ui.card()
            .classes("w-full oce-card")
            .style(f"padding: 12px; border-color: var(--{'error' if severe else 'warning'})")
        ):
            with ui.row().classes("w-full items-center").style("gap: 8px"):
                ui.icon(
                    "gpp_maybe" if screening_status == "POTENTIAL_ISSUE" else "report",
                    size="22px",
                ).style(f"color: var(--{'error' if severe else 'warning'})")
                with ui.column().classes("col").style("gap: 2px"):
                    theme.screening_badge(screening_status)
                    ui.label(str(_value(msg, "screening_reason", "") or "")).style(
                        "font-size: 12px; color: var(--text-secondary)"
                    )
                    ui.label(
                        "No AI can read this message in its current state. Review the "
                        "sanitized copy below, then mark it content-related or spam."
                    ).style("font-size: 12px; color: var(--text-secondary)")
                if screening_status != "SPAM":
                    ui.button(
                        "Spam & learn",
                        icon="block",
                        on_click=lambda: _record_screening("SPAM", True),
                    ).props("outline no-caps color=negative")
                ui.button(
                    "Mark as content",
                    icon="verified",
                    on_click=lambda: _record_screening("CONTENT", True),
                ).props("outline no-caps color=positive")

    with ui.card().classes("w-full oce-card").style("padding: 10px; gap: 4px"):
        with ui.row().classes("items-center").style("gap: 7px"):
            ui.icon("link_off", size="19px").style("color: var(--warning)")
            ui.label("Embedded links are disabled").style("font-weight: 800")
        ui.label(
            "For logins, account alerts, payments, renewals, or security checks, open "
            "the provider's known site yourself. Do not copy or follow a link from email."
        ).style("font-size: 12px; color: var(--text-secondary)")

    with ui.card().classes("w-full oce-card").style("padding: 12px; gap: 3px"):
        ui.label(
            f"From: {sender}"
            + (
                f" <{_value(msg, 'from_addr')}>"
                if _value(msg, "from_name") and _value(msg, "from_addr")
                else ""
            )
        ).style("font-size: 13px; font-weight: 700; overflow-wrap: anywhere")
        ui.label(f"To: {_value(msg, 'to_addrs', '') or ''}").style(
            "font-size: 11.5px; color: var(--text-muted); overflow-wrap: anywhere"
        )

    markdown_ui.reader(
        str(_value(msg, "sanitized_text", "") or "(no text content)"),
        trusted_markdown=False,
        title="Message",
        note="Sanitized content. Remote images, scripts, and active content are never loaded.",
        markdown_default=True,
        allow_links=False,
    )

    _attachments_card(store, message_id, bool(_value(msg, "has_attachments", 0)))

    if working is not None:
        with ui.card().classes("w-full oce-card").style("padding: 11px"):
            with ui.row().classes("w-full items-center"):
                ui.icon("edit_note", size="20px").style("color: var(--accent-hover)")
                ui.label("A response draft already exists.").style("color: var(--text-secondary)")
                ui.space()
                theme.state_badge(str(_value(working, "state", "DRAFT")))
                ui.button(
                    "Review draft",
                    on_click=lambda: ui.navigate.to(f"/detail/{int(_value(working, 'id'))}"),
                ).props("flat dense no-caps color=primary")

    with ui.row().classes("w-full oce-sticky-actions oce-toolbar"):
        if quarantined or screening_withheld:
            ai_btn = ui.button("AI draft", icon="auto_awesome").props(
                "outline no-caps color=primary disable"
            )
            with ai_btn:
                ui.tooltip("Unavailable until quarantine/screening review is cleared.")
        else:
            ui.button("AI draft", icon="auto_awesome", on_click=ai_drawer.show).props(
                "outline no-caps color=primary"
            )
        reply_btn = ui.button(
            "Reply",
            icon="reply",
            on_click=lambda: ui.navigate.to(
                reply_compose_url(
                    reply_to, reply_subject, account=reply_account, reply_to=message_id
                )
            ),
        ).props("unelevated no-caps color=primary")
        template_btn = ui.button(
            "Reply with template",
            icon="content_copy",
            on_click=lambda: ui.navigate.to(
                "/templates?"
                + urlencode({"to": reply_to, "subject": reply_subject, "message_id": message_id})
            ),
        ).props("flat no-caps")
        if quarantined or screening_withheld:
            reply_btn.props("disable")
            template_btn.props("disable")
        if screening_status != "SPAM":
            ui.button(
                "Spam & learn",
                icon="block",
                on_click=lambda: _record_screening("SPAM", True),
            ).props("flat no-caps color=negative")
        if screening_status not in {"POTENTIAL_SPAM", "POTENTIAL_ISSUE"}:
            ui.button("Flag for review", icon="flag", on_click=review_dialog.open).props(
                "flat no-caps"
            )


def render(
    store: object,
    settings: object,
    bridge: object | None = None,
    *,
    message_id: int | None = None,
    account: str = "",
    q: str = "",
    filter: str = "all",
    tag: str = "",
    page: int = 0,
) -> None:
    """Render route-local filters and a responsive list/reading workspace."""
    accounts = _account_options(settings)
    filter = filter if filter in _VIEW_LABELS else "all"
    try:
        page = max(0, int(page))
    except (TypeError, ValueError):
        page = 0
    if message_id is not None:
        # Mark read BEFORE the list renders so the row you just opened loses
        # its unread dot on this paint, not the next one.
        try:
            store.mark_message_seen(int(message_id))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    try:
        tag_counts = store.message_tag_counts(  # type: ignore[attr-defined]
            account_name=account or None
        )
    except Exception:  # noqa: BLE001
        tag_counts = {}
    tag = tag if tag in tag_counts else ""
    filters = {"account": account, "q": q, "filter": filter, "tag": tag}
    if page:
        filters["page"] = str(page)
    site_labels = {sid: site.name for sid, site in (getattr(settings, "sites", None) or {}).items()}

    def _current_url(**changes: str) -> str:
        current = dict(filters)
        if any(k != "page" for k in changes):
            current.pop("page", None)  # a new view starts at its first page
        current.update(changes)
        return "/mail?" + urlencode({k: v for k, v in current.items() if v})

    def _go(**changes: str) -> None:
        ui.navigate.to(_current_url(**changes))

    def _choose_filter(value: str) -> None:
        if value.startswith("tag:"):
            _go(filter="all", tag=value.removeprefix("tag:"))
        else:
            _go(filter=value.removeprefix("view:"), tag="")

    with ui.row().classes("w-full items-center oce-toolbar"):
        ui.button("Compose", icon="edit", on_click=lambda: ui.navigate.to("/compose")).props(
            "unelevated no-caps color=primary"
        )
        ui.select(
            filter_options(tag_counts),
            label="View or tag",
            value=f"tag:{tag}" if tag else f"view:{filter}",
            on_change=lambda e: _choose_filter(str(e.value)),
        ).props("dense outlined options-dense").classes("w-52 oce-toolbar-grow")
        ui.space()
        if accounts:
            ui.select(
                {"": "All accounts", **{name: name for name in accounts}},
                value=account,
                on_change=lambda e: _go(account=str(e.value or "")),
            ).props("dense outlined").classes("w-52 oce-toolbar-grow")
        search = (
            ui.input(
                placeholder="Search sender or subject…",
                value=q,
            )
            .props("dense outlined clearable")
            .classes("w-64 oce-toolbar-grow")
        )
        search.on("keydown.enter", lambda: _go(q=str(search.value or "")))
        # The ✕ only empties the field client-side; apply it to the list too.
        search.on("clear", lambda: _go(q=""))
        ui.button(icon="search", on_click=lambda: _go(q=str(search.value or ""))).props(
            "flat round dense"
        ).tooltip("Search sender, subject and message text")

    page_size = 300
    try:
        rows = list(
            store.list_received(  # type: ignore[attr-defined]
                limit=page_size,
                offset=page * page_size,
                drafted_only=filter == "drafted",
                account_name=account or None,
                q=q or None,
                unseen_only=filter == "unread",
                today_only=filter == "today",
                urgent_only=filter == "urgent",
                needs_reply_only=filter == "needs-reply",
                needs_action_only=filter == "needs-action",
                quarantined_only=filter == "quarantined",
                questionable_only=filter == "review",
                spam_only=filter == "spam",
                include_spam=(filter in {"archived", "trash"} or bool(tag)),
                message_tag=tag or None,
                archived_only=filter == "archived",
                trashed_only=filter == "trash",
            )
        )
    except Exception as e:  # noqa: BLE001
        log.warning("list_received failed: %s", e)
        rows = []
    if tag == "spam":
        ui.label(
            "Filtered locally and reversible. These messages remain on the mail server."
        ).style("font-size: 12px; color: var(--text-secondary)")
    elif tag in {"screening:POTENTIAL_SPAM", "screening:POTENTIAL_ISSUE"}:
        ui.label(
            "Questionable bodies are withheld from every AI until you mark them "
            "content-related or spam."
        ).style("font-size: 12px; color: var(--text-secondary)")
    elif filter == "trash":
        from ...mail.retention import delete_mode, retention_days

        days = retention_days(settings)
        where = {
            "trash": "and moved to your provider's Trash folder",
            "expunge": "and removed from the mail server",
            "off": "(the mail server keeps its copy — provider deletion is off)",
        }[delete_mode(settings)]
        with ui.row().classes("w-full items-center").style("gap: 8px"):
            ui.label(
                f"Deleted mail waits here for {days} days, then is erased here "
                f"{where}. Spam and scam mail is not held — it goes on the next sweep. "
                "Restore anything you want to keep."
            ).style("font-size: 12px; color: var(--text-secondary)")
            ui.space()
            ui.button(
                "Deletion queue",
                icon="schedule",
                on_click=lambda: ui.navigate.to("/spam"),
            ).props("flat dense no-caps color=primary")

    visible_ids = [int(_value(row, "id")) for row in rows]
    selection = RangeSelection(visible_ids)
    neighbors: tuple[int | None, int | None] = (None, None)
    if message_id is not None and int(message_id) in visible_ids:
        i = visible_ids.index(int(message_id))
        neighbors = (
            visible_ids[i - 1] if i > 0 else None,
            visible_ids[i + 1] if i + 1 < len(visible_ids) else None,
        )

    def _open_relative(step: int) -> None:
        """j/k: open the next/previous message in this list."""
        if not visible_ids:
            return
        if message_id is None or int(message_id) not in visible_ids:
            target = visible_ids[0]
        else:
            i = visible_ids.index(int(message_id)) + step
            if i < 0 or i >= len(visible_ids):
                return
            target = visible_ids[i]
        ui.navigate.to(_message_url(target, **filters))

    def _on_key(e: Any) -> None:
        # ui.keyboard already ignores keys typed into inputs/textareas.
        if not e.action.keydown or e.modifiers.ctrl or e.modifiers.meta or e.modifiers.alt:
            return
        key = str(e.key)
        if key == "j":
            _open_relative(1)
        elif key == "k":
            _open_relative(-1)
        elif key == "/":
            search.run_method("focus")
        elif key == "c":
            ui.navigate.to("/compose")
        elif key == "r":
            ui.navigate.to("/mail?" + urlencode({k: v for k, v in filters.items() if v}))

    ui.keyboard(on_key=_on_key)
    checkboxes: dict[int, Any] = {}
    bulk_buttons: list[Any] = []
    syncing_checks = False

    def _sync_selection_controls() -> None:
        nonlocal syncing_checks
        n = len(selection.ids)
        selection_label.set_text(f"{n} selected" if n else "Select messages")
        for button in bulk_buttons:
            button.enable() if n else button.disable()
        if bool(select_all.value) != selection.all_selected:
            syncing_checks = True
            select_all.set_value(selection.all_selected)
            syncing_checks = False

    def _push(message_ids: list[int], checked: bool) -> None:
        """Mirror model state onto checkboxes without re-entering the handler."""
        nonlocal syncing_checks
        syncing_checks = True
        try:
            for mid in message_ids:
                box = checkboxes.get(mid)
                if box is not None and bool(box.value) != checked:
                    box.set_value(checked)
        finally:
            syncing_checks = False

    def _note_click_intent(message_id: int, shift: bool) -> None:
        selection.note_intent(message_id, shift)

    def _set_selected(message_id: int, checked: bool) -> None:
        if syncing_checks:
            return
        _push(selection.toggle(message_id, checked), checked)
        _sync_selection_controls()

    def _select_all(e: Any) -> None:
        if syncing_checks:
            return
        checked = bool(e.value)
        selection.set_all(checked)
        _push(visible_ids, checked)
        _sync_selection_controls()

    def _audit_bulk(action: str, message_ids: list[int], changed: int) -> None:
        try:
            from ...audit.log import AuditLog

            AuditLog(store).record(
                actor="user",
                event="tool_call",
                subject_table="messages",
                subject_id=message_ids[0] if len(message_ids) == 1 else None,
                detail={"action": action, "selected": len(message_ids), "changed": changed},
            )
        except Exception:  # noqa: BLE001
            log.exception("audit append failed for bulk message action")

    def _apply_bulk(action: str) -> None:
        message_ids = selection.ids
        if not message_ids:
            return
        try:
            if action == "read":
                changed = store.mark_messages_seen(message_ids, True)  # type: ignore[attr-defined]
                notice = f"Marked {changed} message(s) read."
            elif action == "unread":
                changed = store.mark_messages_seen(message_ids, False)  # type: ignore[attr-defined]
                notice = f"Marked {changed} message(s) unread."
            elif action == "archive":
                changed = store.set_messages_archived(message_ids, True)  # type: ignore[attr-defined]
                notice = f"Archived {changed} message(s)."
            elif action == "trash":
                changed = store.set_messages_trashed(message_ids, True)  # type: ignore[attr-defined]
                notice = _delete_notice(settings, changed)
            elif action == "restore":
                if filter == "trash":
                    changed = store.set_messages_trashed(message_ids, False)  # type: ignore[attr-defined]
                else:
                    changed = store.set_messages_archived(message_ids, False)  # type: ignore[attr-defined]
                notice = f"Restored {changed} message(s) to the inbox."
            else:
                raise ValueError(f"unsupported bulk action: {action}")
            _audit_bulk(action, message_ids, changed)
            _notify_then_navigate(notice, _current_url())
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Bulk action failed: {e}", type="negative")

    async def _purge_selected() -> None:
        """Skip the holding period for the selected Trash messages.

        Irreversible, and it reaches the provider, so it asks first — the rest of
        the bulk actions are all undoable and deliberately do not.
        """
        from ...mail.retention import purge_now

        message_ids = selection.ids
        if not message_ids:
            return
        with ui.dialog() as confirm, ui.card().classes("oce-card").style(
            "width: min(500px, 94vw); padding: 18px; gap: 8px"
        ):
            ui.label("Delete permanently?").style("font-size: 17px; font-weight: 800")
            ui.label(
                f"{len(message_ids)} message(s) will be deleted from this app and from "
                "the mail server, without waiting out the holding period. "
                "This cannot be undone."
            ).style("font-size: 12.5px; color: var(--text-secondary)")
            with ui.row().classes("w-full justify-end oce-toolbar"):
                ui.button("Cancel", on_click=lambda: confirm.submit(False)).props("flat no-caps")
                ui.button("Delete permanently", icon="delete_forever",
                          on_click=lambda: confirm.submit(True)).props(
                    "unelevated no-caps color=negative"
                )
        if not await confirm:
            return
        progress = ui.notification(
            f"Deleting {len(message_ids)} message(s) — contacting the mail server…",
            spinner=True,
            timeout=None,
        )
        try:
            report = await asyncio.to_thread(purge_now, store, settings, message_ids)
        except Exception as e:  # noqa: BLE001
            ui.notify(f"Delete failed: {e}", type="negative")
            return
        finally:
            try:
                progress.dismiss()
            except Exception:  # noqa: BLE001
                pass
        _audit_bulk("purge_now", message_ids, report.purged)
        for err in report.errors:
            ui.notify(f"The mail server refused the delete — {err}", type="negative", timeout=9000)
        _notify_then_navigate(
            report.summary(),
            _current_url(),
            kind="positive" if report.ok else "warning",
        )

    with (
        ui.row()
        .classes("w-full items-center oce-card oce-toolbar")
        .style("gap: 6px; padding: 7px 10px")
    ):
        select_all = ui.checkbox("Select all", on_change=_select_all).props("dense")
        selection_label = ui.label("Select messages").style(
            "font-size: 12px; color: var(--text-secondary)"
        )
        ui.label("shift-click for a range · j/k next/prev · / search").classes(
            "oce-header-note"
        ).style("font-size: 11px; color: var(--text-muted)")
        ui.space()
        bulk_buttons.extend(
            [
                ui.button("Read", icon="drafts", on_click=lambda: _apply_bulk("read")).props(
                    "flat dense no-caps"
                ),
                ui.button(
                    "Unread", icon="mark_email_unread", on_click=lambda: _apply_bulk("unread")
                ).props("flat dense no-caps"),
            ]
        )
        if filter in {"archived", "trash"}:
            bulk_buttons.append(
                ui.button("Restore", icon="restore", on_click=lambda: _apply_bulk("restore")).props(
                    "flat dense no-caps color=primary"
                )
            )
        if filter == "trash":
            bulk_buttons.append(
                ui.button(
                    "Delete permanently", icon="delete_forever", on_click=_purge_selected
                ).props("flat dense no-caps color=negative")
            )
        if filter != "trash":
            if filter != "archived":
                bulk_buttons.append(
                    ui.button(
                        "Archive", icon="archive", on_click=lambda: _apply_bulk("archive")
                    ).props("flat dense no-caps")
                )
            bulk_buttons.append(
                ui.button(
                    "Delete", icon="delete_outline", on_click=lambda: _apply_bulk("trash")
                ).props("flat dense no-caps color=negative")
            )
    _sync_selection_controls()

    workspace_classes = "oce-mail-workspace oce-card w-full"
    if message_id is not None:
        workspace_classes += " has-selection"
    with ui.element("div").classes(workspace_classes):
        with ui.column().classes("oce-mail-list").style("gap: 7px; padding: 10px"):
            if not rows:
                with ui.column().classes("items-center q-pa-xl"):
                    ui.icon("inbox", size="38px").style("color: var(--text-muted)")
                    ui.label("No matching messages.").style("color: var(--text-secondary)")
            for row in rows:
                row_id = int(_value(row, "id"))
                checkbox = _render_row(
                    row,
                    message_id,
                    filters,
                    site_labels,
                    _set_selected,
                    _note_click_intent,
                )
                if checkbox is not None:
                    checkboxes[row_id] = checkbox
            if len(rows) >= page_size or page:
                with ui.row().classes("w-full items-center justify-between").style(
                    "padding: 4px 2px"
                ):
                    newer = ui.button(
                        "Newer", icon="chevron_left", on_click=lambda: _go(page=str(page - 1))
                    ).props("flat dense no-caps")
                    if not page:
                        newer.props("disable")
                    ui.label(
                        f"Page {page + 1} · {len(rows)} shown"
                    ).style("font-size: 11.5px; color: var(--text-muted)")
                    older = ui.button(
                        "Older", icon="chevron_right", on_click=lambda: _go(page=str(page + 1))
                    ).props("flat dense no-caps")
                    if len(rows) < page_size:
                        older.props("disable")
        with ui.column().classes("oce-mail-pane").style("gap: 12px"):
            if message_id is None:
                with ui.column().classes("w-full items-center q-pa-xl"):
                    ui.icon("drafts", size="52px").style("color: var(--text-muted)")
                    ui.label("Select a message to read").style("font-size: 17px; font-weight: 700")
                    ui.label("On a phone, the message opens as a full reading pane.").style(
                        "color: var(--text-secondary)"
                    )
            else:
                with ui.row().classes("w-full oce-menu-btn"):
                    ui.button(
                        "Back to inbox",
                        icon="arrow_back",
                        on_click=lambda: _go(),
                    ).props("flat no-caps")
                _message_reader(
                    store, settings, message_id, bridge, filters=filters, neighbors=neighbors
                )


def render_detail(
    store: object,
    settings: object,
    message_id: int,
    bridge: object | None = None,
) -> None:
    _message_reader(store, settings, message_id, bridge, full_page=True)
