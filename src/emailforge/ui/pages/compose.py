"""Human-authored compose surface with explicit local draft persistence."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from typing import Any
from urllib.parse import urlencode

from nicegui import run, ui

from ...mail.markdown import markdown_to_safe_html
from ...response_templates import expand_template
from .. import theme
from ..markdown import safe_markdown

log = logging.getLogger(__name__)

_send_times: deque[float] = deque(maxlen=1000)
_UNRESOLVED_PLACEHOLDER = re.compile(r"\{\{\s*[a-zA-Z0-9_]+\s*\}\}")


def _value(row: Any, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, default)


def _rate_limit_ok(settings: object) -> tuple[bool, int]:
    limit = int(getattr(getattr(settings, "security", None), "approvals_per_hour", 60))
    cutoff = time.monotonic() - 3600
    while _send_times and _send_times[0] < cutoff:
        _send_times.popleft()
    return (len(_send_times) < limit, limit)


def _account_usernames(settings: object) -> list[str]:
    return [a.username for a in (getattr(settings, "imap_accounts", []) or [])]


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].strip().lower() if "@" in addr else ""


def split_recipients(value: str) -> list[str]:
    """Comma/semicolon-separated recipient field → individual addresses.

    Display names are tolerated (``Name <a@b>`` → ``a@b``); every returned
    entry is checked individually so a block-listed domain cannot hide behind
    an allowed one in the same field.
    """
    out: list[str] = []
    for part in re.split(r"[,;]", value or ""):
        part = part.strip()
        if not part:
            continue
        if "<" in part and part.endswith(">"):
            part = part[part.rfind("<") + 1 : -1].strip()
        out.append(part)
    return out


def quoted_reply_body(sender: str, received_at: str | None, text: str, width: int = 2000) -> str:
    """Prefill for a human reply: blank line for typing, then the quoted mail."""
    body = (text or "").replace("\r", "").strip()
    if len(body) > width:
        body = body[:width].rstrip() + "\n[…]"
    when = ""
    if received_at:
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(str(received_at))
            when = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            when = str(received_at)[:16]
    head = f"On {when}, {sender} wrote:" if when else f"{sender} wrote:"
    quoted = "\n".join("> " + line if line else ">" for line in body.splitlines())
    return f"\n\n{head}\n{quoted}\n"


def _site_for_sender(settings: object, addr: str) -> str:
    sites = getattr(settings, "sites", None) or {}
    for account in getattr(settings, "imap_accounts", []) or []:
        if account.username == addr and getattr(account, "site_id", None) in sites:
            return str(account.site_id)
    raise PermissionError("Selected From identity has no confirmed site assignment")


def _site_label(settings: object, site_id: str) -> str:
    """Display name for a site from config; the id itself as a last resort."""
    site = (getattr(settings, "sites", None) or {}).get(str(site_id or "").lower())
    return str(getattr(site, "name", "") or site_id or "")


def _signature(settings: object) -> str:
    """``[compose] signature`` from config (falls back to ``[style] signature``)."""
    for section in ("compose", "style"):
        value = str(getattr(getattr(settings, section, None), "signature", "") or "").strip()
        if value:
            return value
    return ""


def _draft_url(draft_id: int) -> str:
    return "/compose?" + urlencode({"draft_id": int(draft_id)})


def _template_subject(text: str, *, question: str, site_name: str, signature: str = "") -> str:
    value = expand_template(
        text,
        site_name=site_name,
        signature=signature,
        question=question or "{{question}}",
    ).replace("{{question}}", "")
    value = " ".join(value.split()).strip()
    return "" if value.lower() in {"re:", "re"} else value


def _do_send(
    store: object,
    settings: object,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
) -> None:
    """Privileged SMTP transmit called only after the confirmation dialog."""
    sec = getattr(settings, "security", None)
    if not bool(getattr(sec, "require_human_approval", False)):
        raise PermissionError("INVARIANT VIOLATION: human approval must be required")
    if bool(getattr(sec, "autosend_allowed", False)):
        raise PermissionError("INVARIANT VIOLATION: autosend must stay disabled")
    if from_addr not in _account_usernames(settings):
        raise PermissionError("INVARIANT VIOLATION: From is not a configured mailbox")
    if not body.strip():
        raise RuntimeError("Message body is empty.")
    if _UNRESOLVED_PLACEHOLDER.search(subject + "\n" + body):
        raise RuntimeError(
            "Replace all {{template_placeholders}} before sending."
        )
    block = {d.lower() for d in getattr(sec, "recipient_block_domains", set()) or set()}
    recipients = split_recipients(to_addr)
    if not recipients or any("@" not in r for r in recipients):
        raise RuntimeError("Recipient field must contain valid email addresses.")
    for r in recipients:
        if _domain(r) in block:
            raise RuntimeError(f"Recipient domain '{_domain(r)}' is on the block list.")

    from ... import secrets as secret_store
    from ...audit.log import AuditLog
    from ...mail.smtp_sender import send_email

    account = next(
        (a for a in getattr(settings, "imap_accounts", []) or [] if a.username == from_addr),
        None,
    )
    smtp_cfg = settings.smtp_for_account(account) if account is not None else None
    if smtp_cfg is None:
        raise RuntimeError("No SMTP server configured for this mailbox (Settings page).")
    secret = secret_store.get_smtp_secret(from_addr)
    if secret is None:
        raise RuntimeError(
            f"No SMTP secret in keyring for '{from_addr}'. Add that mailbox credential first."
        )
    # The approval record is the evidence a human authorised this transmit, so
    # it is written BEFORE the socket opens and its failure aborts the send
    # (fail closed) — a send with no approval row must be impossible.
    AuditLog(store).record(
        actor="user",
        event="approval",
        subject_table=None,
        subject_id=None,
        detail={
            "kind": "manual_compose",
            "from": from_addr,
            "to": to_addr,
            "subject": subject,
            "chars": len(body),
            "in_reply_to": in_reply_to,
        },
    )
    send_email(
        smtp_cfg=smtp_cfg,
        secret=secret,
        from_addr=from_addr,
        to_addr=to_addr,
        subject=subject,
        body=body,
        in_reply_to=in_reply_to,
        references=in_reply_to,
        html_body=markdown_to_safe_html(body),
    )

    try:
        AuditLog(store).record(
            actor="user",
            event="send",
            subject_table=None,
            subject_id=None,
            detail={
                "kind": "manual_compose",
                "from": from_addr,
                "to": to_addr,
                "subject": subject,
                "chars": len(body),
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("Audit append failed for manual send")


def render(
    store: object,
    settings: object,
    to: str = "",
    subject: str = "",
    draft_id: int | None = None,
    template_id: int | None = None,
    account: str = "",
    reply_to: int | None = None,
) -> None:
    """Render compose; Reply query values and saved drafts are safely reopened.

    ``account`` (a configured mailbox username) preselects From — a reply
    must go out from the mailbox the mail arrived on, not the first one in
    config. ``reply_to`` is the local id of the message being answered: its
    Message-ID becomes In-Reply-To/References so the recipient's client
    threads it, and its text is quoted under the cursor.
    """
    usernames = _account_usernames(settings)
    if not usernames:
        ui.label("No mailbox configured — finish setup first.").style("color: var(--error)")
        return

    replied: Any = None
    if reply_to is not None:
        try:
            replied = store.get_message(int(reply_to))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("could not load reply target %s: %s", reply_to, e)
        if replied is not None and not account:
            try:
                account = store.account_username(_value(replied, "account_id")) or ""  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                account = ""
    in_reply_to = str(_value(replied, "message_id", "") or "") or None

    saved = None
    if draft_id is not None:
        try:
            saved = store.get_manual_draft(int(draft_id))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("could not load manual draft %s: %s", draft_id, e)
        if saved is None:
            ui.notify("Saved draft not found.", type="warning")

    selected_template = None
    if saved is None and template_id is not None:
        try:
            selected_template = store.get_response_template(  # type: ignore[attr-defined]
                int(template_id)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not load response template %s: %s", template_id, e)

    from_addr = str(_value(saved, "from_addr", "") or account or usernames[0])
    if from_addr not in usernames:
        from_addr = usernames[0]
    template_subject = str(_value(selected_template, "subject", "") or "")
    template_body = str(_value(selected_template, "body", "") or "")
    if selected_template is not None:
        site_id = str(_value(selected_template, "site_id", "") or "")
        matching = [
            u
            for u in usernames
            if _site_for_sender(settings, u) == site_id
        ]
        if matching:
            from_addr = matching[0]
        site_name = _site_label(settings, site_id)
        template_subject = _template_subject(
            template_subject,
            question=subject,
            site_name=site_name,
            signature=_signature(settings),
        )
        template_body = expand_template(
            template_body,
            site_name=site_name,
            signature=_signature(settings),
        )
    prefill_body = template_body
    if saved is None and replied is not None and not template_body:
        withheld = bool(_value(replied, "quarantined", 0)) or str(
            _value(replied, "screening_status", "") or ""
        ).upper() in {"POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"}
        if not withheld:
            prefill_body = quoted_reply_body(
                str(_value(replied, "from_name") or _value(replied, "from_addr") or "they"),
                _value(replied, "received_at"),
                str(_value(replied, "sanitized_text", "") or ""),
            )
    state: dict[str, Any] = {
        "id": int(_value(saved, "id")) if saved is not None else None,
        "from": from_addr,
        "to": str(_value(saved, "recipient", to) or ""),
        "subject": str(_value(saved, "subject", subject or template_subject) or ""),
        "body": str(_value(saved, "body", prefill_body) or ""),
        "in_reply_to": in_reply_to,
    }

    with ui.row().classes("w-full items-center no-wrap").style("gap: 8px"):
        ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/mail")).props(
            "flat round dense"
        )
        ui.label("Compose" if state["id"] is None else "Edit saved draft").style(
            "font-weight: 800; font-size: 18px"
        )
        if state["id"] is not None:
            theme.badge(f"DRAFT #{state['id']}", "accent")

    with ui.card().classes("w-full bb-card").style(
        "gap: 12px; padding: 16px; max-width: 820px"
    ):
        from_sel = ui.select(
            {u: u for u in usernames},
            value=state["from"],
            label="From",
            on_change=lambda e: state.update({"from": e.value}),
        ).props("outlined dense").classes("w-full")
        to_in = ui.input(
            "To",
            value=state["to"],
            on_change=lambda e: state.update(to=e.value or ""),
        ).props("outlined dense").classes("w-full")
        subj_in = ui.input(
            "Subject",
            value=state["subject"],
            on_change=lambda e: state.update(subject=e.value or ""),
        ).props("outlined dense").classes("w-full")

        template_rows: list[Any] = []
        try:
            template_rows = list(
                store.list_response_templates(  # type: ignore[attr-defined]
                    _site_for_sender(settings, state["from"])
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not list response templates: %s", e)
        template_options = {
            int(_value(row, "id")): str(_value(row, "name", "Template"))
            for row in template_rows
        }
        with ui.row().classes("w-full items-center bb-toolbar"):
            template_sel = ui.select(
                template_options,
                value=int(_value(selected_template, "id"))
                if selected_template is not None
                else None,
                label="Response template",
            ).props("outlined dense clearable").classes("col bb-toolbar-grow")

            def _apply_template() -> None:
                if template_sel.value is None:
                    ui.notify("Choose a template first.", type="warning")
                    return
                try:
                    row = store.get_response_template(  # type: ignore[attr-defined]
                        int(template_sel.value)
                    )
                    if row is None:
                        raise ValueError("template not found")
                    sender_site = _site_for_sender(settings, str(from_sel.value or ""))
                    if str(_value(row, "site_id", "")) != sender_site:
                        raise PermissionError(
                            "Template belongs to a different site identity"
                        )
                    name = _site_label(settings, sender_site)
                    subject_value = _template_subject(
                        str(_value(row, "subject", "") or ""),
                        question=str(subj_in.value or ""),
                        site_name=name,
                        signature=_signature(settings),
                    )
                    body_value = expand_template(
                        str(_value(row, "body", "") or ""),
                        site_name=name,
                        signature=_signature(settings),
                    )
                    if subject_value and not str(subj_in.value or "").strip():
                        subj_in.value = subject_value
                    else:
                        subject_value = str(subj_in.value or "")
                    body_in.value = body_value
                    state.update(subject=subject_value, body=body_value)
                    _preview.refresh()
                    ui.notify("Template inserted. Review and personalize it.", type="positive")
                except Exception as e:  # noqa: BLE001
                    ui.notify(f"Template could not be applied: {e}", type="negative")

            ui.button("Insert", icon="add", on_click=_apply_template).props(
                "outline no-caps color=primary"
            )
            ui.button(
                "Manage", icon="settings", on_click=lambda: ui.navigate.to("/templates")
            ).props("flat no-caps")

        with ui.tabs(value="write").props("dense no-caps") as editor_tabs:
            ui.tab("write", label="Write")
            ui.tab("preview", label="Preview")
        with ui.tab_panels(editor_tabs, value="write").classes(
            "w-full bg-transparent"
        ):
            with ui.tab_panel("write").classes("q-pa-none"):
                body_in = ui.textarea(
                    "Message (Markdown supported)",
                    value=state["body"],
                    on_change=lambda e: (
                        state.update(body=e.value or ""),
                        _preview.refresh(),
                    ),
                ).props("outlined").classes("w-full").style("min-height: 270px")
                ui.label(
                    "Use headings, lists, links, emphasis, code blocks, and tables. "
                    "Recipients receive plain text plus a safe formatted HTML version."
                ).style("font-size: 11.5px; color: var(--text-muted)")
            with ui.tab_panel("preview").classes("q-pa-sm"):
                @ui.refreshable
                def _preview() -> None:
                    value = str(body_in.value or state["body"] or "")
                    if value.strip():
                        safe_markdown(value)
                    else:
                        ui.label("Nothing to preview yet.").style(
                            "color: var(--text-secondary)"
                        )

                _preview()
        warn = ui.label("").style("font-size: 12.5px; color: var(--warning)")

        def _values() -> tuple[str, str, str, str]:
            return (
                str(from_sel.value or state["from"]).strip(),
                str(to_in.value or "").strip(),
                str(subj_in.value or "").strip(),
                str(body_in.value or "").strip(),
            )

        def _refresh_warn() -> None:
            sec = getattr(settings, "security", None)
            allow = {
                d.lower()
                for d in getattr(sec, "recipient_allowlist_domains", set()) or set()
            }
            outside = [
                _domain(r) for r in split_recipients(str(to_in.value or ""))
                if allow and _domain(r) and _domain(r) not in allow
            ]
            warn.set_text(
                f"⚠ '{', '.join(sorted(set(outside)))}' is outside the configured recipient domains."
                if outside
                else ""
            )

        to_in.on("blur", lambda _e: _refresh_warn())

        def _save() -> None:
            frm, to_addr, subject_v, body_v = _values()
            if not any((to_addr, subject_v, body_v)):
                ui.notify("Add a recipient, subject, or message before saving.", type="warning")
                return
            try:
                if state["id"] is None:
                    state["id"] = int(
                        store.create_manual_draft(  # type: ignore[attr-defined]
                            frm,
                            to_addr,
                            subject_v,
                            body_v,
                            site_id=_site_for_sender(settings, frm),
                        )
                    )
                else:
                    store.update_manual_draft(  # type: ignore[attr-defined]
                        state["id"], frm, to_addr, subject_v, body_v
                    )
                state.update({"from": frm, "to": to_addr, "subject": subject_v, "body": body_v})
                ui.notify("Draft saved locally.", type="positive")
                _saved_drafts.refresh()
            except Exception as e:  # noqa: BLE001
                log.exception("manual draft save failed")
                ui.notify(f"Save draft failed: {e}", type="negative")

        async def _send() -> None:
            frm, to_addr, subject_v, body_v = _values()
            if "@" not in to_addr:
                ui.notify("Enter a valid recipient address.", type="negative")
                return
            if not subject_v:
                ui.notify("Subject is empty.", type="warning")
                return
            if not body_v:
                ui.notify("Message body is empty.", type="negative")
                return
            if _UNRESOLVED_PLACEHOLDER.search(subject_v + "\n" + body_v):
                ui.notify(
                    "Replace all {{template_placeholders}} before sending.",
                    type="negative",
                )
                return
            ok, limit = _rate_limit_ok(settings)
            if not ok:
                ui.notify(f"Send rate limit reached ({limit}/hour).", type="negative")
                return
            try:
                # Persist before transport so any SMTP failure leaves a retryable draft.
                if state["id"] is None:
                    state["id"] = int(
                        store.create_manual_draft(  # type: ignore[attr-defined]
                            frm,
                            to_addr,
                            subject_v,
                            body_v,
                            site_id=_site_for_sender(settings, frm),
                        )
                    )
                else:
                    store.update_manual_draft(  # type: ignore[attr-defined]
                        state["id"], frm, to_addr, subject_v, body_v
                    )
                await run.io_bound(
                    _do_send, store, settings, frm, to_addr, subject_v, body_v,
                    state.get("in_reply_to"),
                )
                store.mark_manual_draft_sent(state["id"])  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                ui.notify(f"Send FAILED: {e}", type="negative", timeout=8000)
                log.warning("Manual send failed: %s", e)
                return
            _send_times.append(time.monotonic())
            ui.notify(f"Sent to {to_addr}.", type="positive")
            state.update({"id": None, "to": "", "subject": "", "body": ""})
            to_in.value = subj_in.value = body_in.value = ""
            _saved_drafts.refresh()
            _sent_history.refresh()

        def _confirm() -> None:
            frm, to_addr, subject_v, body_v = _values()
            if "@" not in to_addr:
                ui.notify("Enter a valid recipient address.", type="negative")
                return
            if not subject_v or not body_v:
                ui.notify("Subject and message body are required.", type="negative")
                return
            if _UNRESOLVED_PLACEHOLDER.search(subject_v + "\n" + body_v):
                ui.notify(
                    "Replace all {{template_placeholders}} before sending.",
                    type="negative",
                )
                return
            with ui.dialog() as dialog, ui.card().style("gap: 10px; max-width: 460px"):
                ui.label("Send this email?").style("font-weight: 800; font-size: 15px")
                ui.label(f"From: {frm}").style(
                    "font-size: 13px; color: var(--text-secondary); overflow-wrap: anywhere"
                )
                ui.label(f"To: {to_addr}").style(
                    "font-size: 13px; color: var(--text-secondary); overflow-wrap: anywhere"
                )
                ui.label("This is the only action that transmits the message.").style(
                    "font-size: 12px; color: var(--warning)"
                )
                with ui.row().classes("w-full justify-end").style("gap: 8px"):
                    ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
                    async def _confirmed_send() -> None:
                        dialog.close()
                        await _send()

                    ui.button(
                        "Send",
                        icon="send",
                        on_click=_confirmed_send,
                    ).props("unelevated no-caps color=primary")
            dialog.open()

        with ui.row().classes("w-full justify-end bb-toolbar").style("gap: 8px"):
            ui.label("Saved drafts stay local. Nothing auto-sends.").style(
                "font-size: 11.5px; color: var(--text-muted); align-self: center"
            )
            ui.space()
            ui.button("Save Draft", icon="save", on_click=_save).props(
                "outline no-caps color=primary"
            )
            ui.button("Send", icon="send", on_click=_confirm).props(
                "unelevated no-caps color=primary"
            )

    @ui.refreshable
    def _saved_drafts() -> None:
        try:
            rows = list(store.list_manual_drafts())  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("could not list manual drafts: %s", e)
            rows = []
        with ui.card().classes("w-full bb-card").style(
            "padding: 14px; max-width: 820px; gap: 8px"
        ):
            ui.label("Saved drafts").style("font-weight: 800")
            if not rows:
                ui.label("No saved manual drafts.").style("color: var(--text-secondary)")
                return

            def _delete(did: int) -> None:
                try:
                    store.delete_manual_draft(did)  # type: ignore[attr-defined]
                    if state["id"] == did:
                        state["id"] = None
                    _saved_drafts.refresh()
                    ui.notify("Saved draft deleted.", type="positive")
                except Exception as e:  # noqa: BLE001
                    ui.notify(f"Delete failed: {e}", type="negative")

            for row in rows:
                did = int(_value(row, "id"))
                with ui.element("div").classes("bb-row w-full"):
                    with ui.row().classes("w-full items-center no-wrap").style("gap: 8px"):
                        with ui.column().classes("col").style("gap: 2px; min-width: 0"):
                            ui.link(
                                theme.clean_subject(_value(row, "subject", "")),
                                _draft_url(did),
                            ).classes("bb-subject").style("font-weight: 700")
                            ui.label(
                                f"To: {str(_value(row, 'recipient', '') or '(not set)')} · "
                                f"{str(_value(row, 'updated_at', '') or '')[:19].replace('T', ' ')}"
                            ).style("font-size: 11.5px; color: var(--text-muted)")
                        ui.button(
                            icon="delete", on_click=lambda _e, i=did: _delete(i)
                        ).props("flat round dense color=negative")

    _saved_drafts()

    @ui.refreshable
    def _sent_history() -> None:
        try:
            rows = list(store.list_manual_drafts(state="SENT"))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("could not list manual sent history: %s", e)
            rows = []
        with ui.expansion(
            f"Manually sent — {len(rows)} retained message(s)", icon="outbox"
        ).classes("w-full bb-card").style("max-width: 820px"):
            if not rows:
                ui.label("No manually sent messages recorded yet.").style(
                    "color: var(--text-secondary)"
                )
                return
            for row in rows[:50]:
                with ui.row().classes("w-full items-center no-wrap").style("gap: 8px"):
                    ui.label(theme.clean_subject(_value(row, "subject", ""))).classes(
                        "col bb-clip-1"
                    )
                    ui.label(str(_value(row, "recipient", "") or "")).style(
                        "font-size: 11.5px; color: var(--text-muted)"
                    )
                    theme.badge("SENT", "success")
                    ui.label(
                        str(_value(row, "sent_at", "") or _value(row, "updated_at", ""))[:19]
                        .replace("T", " ")
                    ).style("font-size: 11px; color: var(--text-muted)")

    _sent_history()
