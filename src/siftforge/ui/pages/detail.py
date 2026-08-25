"""Draft detail + human-approval page (build spec §5, §9, §11, §0.1, §0.4).

Chat-style review screen. Shows everything a human needs to decide on a draft:
  * the ORIGINAL email as an inbound bubble, with symbolic ``[link_N]``
    references left inert. Account/security actions must be verified by opening
    the provider's known site directly;
  * the DRAFTED reply as an editable outbound bubble;
  * the GUARDRAIL flags persisted on the draft (``drafts.guardrail_flags``);
  * PROVENANCE — "agent saw these emails" — the same-thread messages.

Actions (spec §5):
  * Approve — write an approval record (state APPROVED, approved_by/at), then
    send via SMTP, then mark SENT. Explicit runtime checks require a human
    approval record even when Python optimizations are enabled.
  * Edit    — edit the body in a textarea; on save RE-RUN ``run_output_guardrails``
    (spec §5 "edit: re-run output_guardrails -> send"). Pass -> send; fail ->
    show the flags and BLOCK.
  * Reject  — audit only; state REJECTED. No send.

Recipient binding (spec §0.4 / §11): if the recipient is a NEW external address
(``not store.is_allowlisted(recipient)``), the human must RE-TYPE the exact
address before Approve/Send is enabled.

Approvals are rate-limited per hour via ``settings.security.approvals_per_hour``.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from nicegui import run, ui

from ...mail.markdown import markdown_to_safe_html
from ...security import run_output_guardrails
from .. import theme
from .. import markdown as markdown_ui
from ..interrupt import ApprovalRecord
from . import assistant

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- approvals/hour rate limiter (spec §9) ---------------------------------
# Sliding window of approval timestamps (monotonic seconds), process-wide.
_approval_times: list[float] = []


def _rate_limit_ok(settings: object) -> tuple[bool, int]:
    """Return ``(allowed, limit)`` for the approvals/hour window (spec §9)."""
    limit = int(getattr(getattr(settings, "security", None), "approvals_per_hour", 60))
    now = time.monotonic()
    cutoff = now - 3600.0
    _approval_times[:] = [t for t in _approval_times if t >= cutoff]
    return (len(_approval_times) < limit, limit)


def _record_approval_time() -> None:
    _approval_times.append(time.monotonic())


def _audit(store: object, actor: str, event: str, draft_id: int, detail: dict[str, Any]) -> None:
    """Best-effort audit append via AuditLog (chained hash + PII redaction, §7)."""
    try:
        from ...audit.log import AuditLog

        AuditLog(store).record(
            actor=actor,
            event=event,
            subject_table="drafts",
            subject_id=draft_id,
            detail=detail,
        )
    except Exception as e:
        log.warning("audit append failed: %s", e)


def _resolve_body_links(store: object, message_id: int | None, text: str) -> str:
    """Replace symbolic ``[link_N]`` refs with real targets for the human (§4)."""
    if message_id is None or not text:
        return text or ""
    try:
        mapping = store.resolve_links(message_id)  # type: ignore[attr-defined]
    except Exception as e:
        log.debug("resolve_links failed: %s", e)
        return text
    out = text
    for symbol, target in mapping.items():
        token = str(symbol)
        if not (token.startswith("[") and token.endswith("]")):
            token = f"[{token}]"
        resolved = f"{token[:-1]} -> {target}]"
        out = out.replace(token, resolved)
    return out


def _value(row: Any, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, default)


def source_account_for_draft(store: object, draft: Any) -> Any:
    """Resolve the persisted receiving account; fail closed if it is ambiguous."""
    message_id = _value(draft, "message_id")
    if message_id is None:
        raise RuntimeError("This draft is not linked to a source mailbox.")
    method = getattr(store, "get_account_for_message", None)
    if not callable(method):
        raise RuntimeError("Source mailbox lookup is unavailable; send is blocked.")
    account = method(int(message_id))
    username = str(_value(account, "username", "") or "").strip()
    site_id = str(_value(account, "site_id", "") or "").strip().lower()
    from ...db.store import validate_site_id

    try:
        validate_site_id(site_id)
    except ValueError:
        site_id = ""
    if not username or not site_id:
        raise RuntimeError("Source mailbox has no confirmed sending identity/site assignment.")
    return account


def _do_send(store: object, settings: object, draft: Any, body: str, approval: ApprovalRecord) -> None:
    """Privileged SMTP transmit — gated by an explicit human-approval record.

    Mirrors invariant §0.1 / §11: verify a human-approval record exists and
    autosend is NOT enabled before calling ``send_email``. This function is only
    ever reached from an explicit Approve/Edit-save click handler.
    """
    # --- INVARIANT §0.1 / §11: human-approval record must exist; no autosend ---
    if not isinstance(approval, ApprovalRecord) or not approval.approved_by:
        raise PermissionError("Send blocked: no explicit human-approval record")
    sec = getattr(settings, "security", None)
    if not bool(getattr(sec, "require_human_approval", False)):
        raise PermissionError("Send blocked: human approval is not required by configuration")
    if bool(getattr(sec, "autosend_allowed", False)):
        raise PermissionError("Send blocked: autosend must stay disabled")
    if draft["message_id"] is not None:
        is_quarantined = getattr(store, "is_quarantined", None)
        if callable(is_quarantined) and is_quarantined(int(draft["message_id"])):
            raise PermissionError("Send blocked: source message is quarantined")
        get_message = getattr(store, "get_message", None)
        source = get_message(int(draft["message_id"])) if callable(get_message) else None
        if source is not None and str(
            _value(source, "screening_status", "UNSCREENED") or "UNSCREENED"
        ).upper() in {
            "POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"
        }:
            raise PermissionError(
                "Send blocked: source message is withheld by inbound screening"
            )
    if not body.strip():
        raise RuntimeError("Send blocked: reply body is empty")
    if approval.recipient != str(_value(draft, "recipient", "") or ""):
        raise PermissionError("Send blocked: approved recipient changed")

    from ... import secrets as secret_store
    from ...mail.smtp_sender import send_email

    account = source_account_for_draft(store, draft)
    from_addr = str(_value(account, "username", "") or "")
    configured_account = next(
        (a for a in getattr(settings, "imap_accounts", []) or [] if a.username == from_addr),
        None,
    )
    if configured_account is None:
        raise PermissionError("Send blocked: source identity is not a configured mailbox")
    # Per-account SMTP override wins; otherwise the global relay is used with
    # this mailbox's identity (see Settings.smtp_for_account).
    smtp_cfg = settings.smtp_for_account(configured_account)
    if smtp_cfg is None:
        raise RuntimeError("No SMTP account configured (Settings page).")
    secret = secret_store.get_smtp_secret(from_addr)
    if secret is None:
        raise RuntimeError(
            f"No SMTP secret in keyring for source mailbox '{from_addr}'."
        )

    in_reply_to = None
    references = None
    try:
        msg = store.get_message(_value(draft, "message_id"))  # type: ignore[attr-defined]
        if msg is not None:
            in_reply_to = msg["message_id"]
    except Exception:
        pass

    send_email(
        smtp_cfg=smtp_cfg,
        secret=secret,
        from_addr=from_addr,
        to_addr=approval.recipient,
        subject=str(_value(draft, "subject", "") or ""),
        body=body,
        html_body=markdown_to_safe_html(body),
        in_reply_to=in_reply_to,
        references=references,
    )


def _bubble_meta(text: str) -> None:
    ui.label(text).style("font-size: 11.5px; color: var(--text-muted)")


def _restore_retryable_after_send_failure(
    store: object,
    draft_id: int,
    body: str,
    guardrail_flags: dict[str, Any],
    error: Exception,
) -> None:
    """Return an SMTP-failed approval to PENDING with an auditable failure flag."""
    flags = dict(guardrail_flags or {})
    flags["send_error"] = str(error)[:200]
    store.update_draft_state(  # type: ignore[attr-defined]
        draft_id,
        "PENDING",
        approved_by=None,
        approved_at=None,
        body=body,
        guardrail_flags=flags,
    )


def _any_guard_fired(flags: Any) -> bool:
    """True when the persisted guardrail flags show an actual firing (used to
    auto-expand the flags panel; telemetry like injection_score is ignored)."""
    if not isinstance(flags, dict):
        return bool(flags)
    if flags.get("secrets") or flags.get("pii"):
        return True
    urls = flags.get("urls")
    if isinstance(urls, dict) and urls.get("blocked"):
        return True
    policy = flags.get("policy")
    if isinstance(policy, dict) and policy.get("violations"):
        return True
    ct = flags.get("crossthread")
    if isinstance(ct, dict) and (ct.get("leaked_thread_ids") or ct.get("ngrams")):
        return True
    return False


def render(
    store: object,
    settings: object,
    draft_id: int,
    bridge: object | None = None,
) -> None:
    """Render the detail/approval page for one draft (spec §5, §9)."""
    draft = None
    try:
        draft = store.get_draft(int(draft_id))  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("get_draft failed: %s", e)

    if draft is None:
        with ui.column().classes("w-full items-center q-pa-xl").style("gap: 8px"):
            ui.icon("search_off", size="40px").style("color: var(--text-muted)")
            ui.label(f"Draft {draft_id} not found.").classes("text-h6")
            ui.button("Back to AI Review", on_click=lambda: ui.navigate.to("/drafts")).props(
                "flat"
            )
        return

    message_id = draft["message_id"]
    recipient = draft["recipient"] or ""
    try:
        is_external = not store.is_allowlisted(recipient)  # type: ignore[attr-defined]
    except Exception:
        is_external = True

    # Mutable per-render state.
    state: dict[str, Any] = {
        "body": draft["body"] or "",
        "draft_state": str(draft["state"] or "").upper(),
    }

    # --- header row -----------------------------------------------------------
    with ui.row().classes("w-full items-center bb-toolbar").style("gap: 10px"):
        ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/drafts")).props(
            "flat round dense"
        ).classes("bb-nav-btn")
        theme.subject_label(
            draft["subject"], full=True,
            style="font-size: 1.25rem; font-weight: 700; line-height: 1.4",
        )
        theme.state_badge(draft["state"] or "")
        ui.space()
        _bubble_meta(f"draft #{draft['id']}")

    # --- original email (inbound bubble) --------------------------------------
    orig = None
    try:
        orig = store.get_message(message_id)  # type: ignore[attr-defined]
    except Exception:
        pass
    source_withheld = bool(
        orig is not None
        and (
            bool(orig["quarantined"])
            or str(orig["screening_status"] or "").upper()
            in {"POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"}
        )
    )

    try:
        source_account = source_account_for_draft(store, draft)
        source_identity = str(_value(source_account, "username", "") or "")
        source_site = str(_value(source_account, "site_id", "") or "")
    except Exception as e:  # noqa: BLE001
        source_identity = "unresolved"
        source_site = "unassigned"
        log.warning("draft source identity unavailable: %s", e)

    with ui.row().classes("w-full no-wrap").style("gap: 10px"):
        ui.icon("mail", size="26px").style("color: var(--text-secondary); margin-top: 6px")
        with ui.column().classes("col").style("gap: 4px; min-width: 0"):
            if orig is not None:
                _bubble_meta(
                    f"{orig['from_name'] or ''} <{orig['from_addr'] or ''}>"
                    f"  ·  {orig['received_at'] or ''}"
                )
                with ui.element("div").classes("bb-bubble bb-bubble--bot w-full"):
                    ui.label(
                        "Embedded links are disabled. For account or security actions, "
                        "open the provider's known site directly."
                    ).style("font-size: 11px; color: var(--text-muted)")
                    markdown_ui.plain_text(orig["sanitized_text"] or "")
            else:
                with ui.element("div").classes("bb-bubble bb-bubble--bot w-full"):
                    ui.label("Original message unavailable.").style(
                        "color: var(--text-secondary)"
                    )

    # --- provenance (collapsed) ------------------------------------------------
    try:
        thread = store.thread_messages(draft["thread_id"])  # type: ignore[attr-defined]
    except Exception:
        thread = []
    with ui.expansion(
        f"Provenance — agent saw {len(thread)} email(s) in this thread", icon="visibility"
    ).classes("w-full bb-card"):
        if thread:
            for m in thread:
                marker = "  ← this email" if m["id"] == message_id else ""
                ui.label(
                    f"{m['received_at'] or '?'}  ·  {m['from_addr'] or '?'}  ·  "
                    f"{m['subject'] or ''}{marker}"
                ).classes("bb-mono").style("font-size: 12.5px")
        else:
            ui.label("No thread history recorded.").style("color: var(--text-secondary)")

    # --- guardrail flags ---------------------------------------------------------
    flags_raw = draft["guardrail_flags"]
    try:
        flags = json.loads(flags_raw) if flags_raw else {}
    except Exception:
        flags = {"_raw": flags_raw}
    fired = _any_guard_fired(flags)
    title = "Guardrail flags" + (" — ATTENTION" if fired else " — all clear")
    with ui.expansion(title, icon="shield", value=fired).classes("w-full bb-card"):
        if flags:
            ui.code(json.dumps(flags, indent=2, default=str)).classes("w-full")
        else:
            ui.label("No guardrail flags recorded.").style("color: var(--text-secondary)")

    # --- recipient binding (§0.4) ----------------------------------------------
    retype_ok = {"value": not is_external}  # allowlisted => no retype needed

    with ui.element("div").classes("bb-card w-full q-pa-md"):
        with ui.row().classes("items-center").style("gap: 8px; margin-bottom: 8px"):
            ui.icon("outbox", size="20px").style("color: var(--text-secondary)")
            ui.label("Send as:").style("color: var(--text-secondary)")
            ui.label(source_identity).style(
                "font-weight: 700; color: var(--error)"
                if source_identity == "unresolved"
                else "font-weight: 700"
            )
            theme.badge(
                source_site or "unassigned",
                "accent"
                if source_site in (getattr(settings, "sites", None) or {})
                else "error",
            )
        with ui.row().classes("items-center").style("gap: 8px"):
            ui.icon("person", size="20px").style("color: var(--text-secondary)")
            ui.label("Reply goes to:").style("color: var(--text-secondary)")
            ui.label(recipient).style(
                "color: var(--error); font-weight: 700" if is_external else "font-weight: 600"
            )
            if is_external:
                theme.badge("NEW EXTERNAL", "error")
            else:
                theme.badge("known recipient", "success")
        if is_external:
            ui.label(
                "This is a new / external recipient. Re-type the exact address "
                "to enable Approve & Send (invariant §0.4)."
            ).style("font-size: 12px; color: var(--error); margin-top: 4px")
            confirm = ui.input("Re-type recipient address").props("dense outlined").classes(
                "w-96"
            )

            def _check_retype() -> None:
                retype_ok["value"] = (confirm.value or "").strip().lower() == recipient.lower()
                _sync_buttons()

            confirm.on("update:model-value", lambda _e: _check_retype())

    # --- drafted reply (outbound bubble, editable) -------------------------------
    with ui.row().classes("w-full no-wrap justify-end").style("gap: 10px"):
        with ui.column().classes("col items-end").style("gap: 4px; min-width: 0"):
            _bubble_meta("drafted reply — edit freely; edits are re-guardrailed")
            with ui.element("div").classes("bb-bubble bb-bubble--user w-full"):
                with ui.tabs(value="write").props("dense no-caps") as draft_tabs:
                    ui.tab("write", label="Write")
                    ui.tab("preview", label="Preview")
                with ui.tab_panels(draft_tabs, value="write").classes(
                    "w-full bg-transparent"
                ):
                    with ui.tab_panel("write").classes("q-pa-none"):
                        body_area = (
                            ui.textarea(value=state["body"])
                            .classes("w-full")
                            .props("autogrow borderless")
                        )
                        body_area.on(
                            "update:model-value",
                            lambda e: (
                                state.update(body=e.args or ""),
                                _sync_buttons(),
                                _draft_preview.refresh(),
                            ),
                        )
                    with ui.tab_panel("preview").classes("q-pa-sm"):
                        @ui.refreshable
                        def _draft_preview() -> None:
                            markdown_ui.safe_markdown(
                                str(body_area.value or state["body"] or "")
                            )

                        _draft_preview()
            guard_box = ui.column().classes("w-full")
        ui.icon("edit_note", size="26px").style(
            "color: var(--accent-hover); margin-top: 6px"
        )

    def _apply_ai_body(body: str, _draft_id: int) -> None:
        state["body"] = body
        if state["draft_state"] != "PENDING":
            state["draft_state"] = "DRAFT"
        body_area.value = body
        _draft_preview.refresh()
        _sync_buttons()

    ai_drawer = None
    if orig is not None:
        ai_drawer = assistant.render_drawer(
            store,
            settings,
            bridge,
            message=orig,
            draft_id=int(draft["id"]),
            current_body=lambda: str(body_area.value or state["body"] or ""),
            apply_body=_apply_ai_body,
        )

    # --- action handlers ----------------------------------------------------------
    def _sendable() -> bool:
        return state["draft_state"] in {"DRAFT", "PENDING"} and not source_withheld

    def _editable() -> bool:
        return state["draft_state"] in {"DRAFT", "PENDING", "BLOCKED", "DEFERRED_NO_LLM"}

    def _sync_buttons() -> None:
        body_ok = bool(str(state["body"] or "").strip())
        source_ok = source_identity != "unresolved"
        approve_btn.set_enabled(retype_ok["value"] and _sendable() and body_ok and source_ok)
        save_btn.set_enabled(_editable())
        reject_btn.set_enabled(state["draft_state"] not in {"SENT", "REJECTED", "APPROVED"})

    def _guard_current() -> Any | None:
        body = str(state["body"] or "").strip()
        if not body:
            ui.notify("Draft body is empty. Draft or write a response first.", type="negative")
            return None
        participants: set[str] = set()
        for msg in thread:
            for key in ("from_addr", "to_addrs", "cc_addrs"):
                participants |= {
                    addr.strip().lower()
                    for addr in str(msg[key] or "").split(",")
                    if addr.strip()
                }
        edited = {
            "recipient": recipient,
            "body": body,
            "thread_id": draft["thread_id"],
            "subject": draft["subject"],
        }
        try:
            report = run_output_guardrails(
                edited,
                {
                    "recipient": recipient,
                    "thread_participants": participants,
                    "site_id": source_site,
                },
                store,
                getattr(settings, "security", settings),
                bridge,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("guardrail re-run failed")
            ui.notify(f"Guardrail error (send blocked): {e}", type="negative")
            return None

        guard_box.clear()
        store.update_draft_state(  # type: ignore[attr-defined]
            draft["id"], state["draft_state"], body=body, guardrail_flags=report.flags
        )
        if not report.passed:
            _audit(store, "guardrail", "block", draft["id"], {"reasons": report.reasons})
            with guard_box:
                ui.label("Guardrails blocked this version — not sent.").style(
                    "color: var(--error); font-weight: 700"
                )
                for reason in report.reasons:
                    ui.label(f"• {reason}").style("color: var(--error); font-size: 13px")
                ui.code(json.dumps(report.flags, indent=2, default=str)).classes("w-full")
            ui.notify("Current draft is blocked by guardrails.", type="negative")
            return None
        return report

    def _save_changes() -> None:
        if not _editable():
            ui.notify("This draft is immutable in its current state.", type="warning")
            return
        report = _guard_current()
        if report is None:
            return
        new_state = "PENDING" if state["draft_state"] == "PENDING" else "DRAFT"
        store.update_draft_state(  # type: ignore[attr-defined]
            draft["id"],
            new_state,
            body=str(state["body"] or "").strip(),
            guardrail_flags=report.flags,
        )
        state["draft_state"] = new_state
        ui.notify("Draft saved; guardrails passed. Nothing was sent.", type="positive")
        _sync_buttons()

    async def _approve_and_send() -> None:
        if not _sendable():
            ui.notify("This draft is not ready to send. Revise and save it first.", type="warning")
            return
        allowed, limit = _rate_limit_ok(settings)
        if not allowed:
            ui.notify(f"Rate limit reached ({limit}/hour). Try later.", type="negative")
            return
        if not retype_ok["value"]:
            ui.notify("Re-type the recipient address first.", type="warning")
            return
        report = _guard_current()  # always scan the exact current textarea body
        if report is None:
            return
        body = str(state["body"] or "").strip()
        approved_at = _now_iso()
        try:
            store.update_draft_state(  # type: ignore[attr-defined]
                draft["id"],
                "APPROVED",
                approved_by="ui-user",
                approved_at=approved_at,
                body=body,
                guardrail_flags=report.flags,
            )
            state["draft_state"] = "APPROVED"
            approval = ApprovalRecord(
                draft_id=draft["id"],
                approved_by="ui-user",
                approved_at=approved_at,
                response_type="edit",
                recipient=recipient,
                flags=report.flags,
            )
            _audit(store, "user", "approval", draft["id"], {"recipient": recipient})
            await run.io_bound(_do_send, store, settings, draft, body, approval)
            store.update_draft_state(  # type: ignore[attr-defined]
                draft["id"], "SENT", sent_at=_now_iso(), body=body
            )
            state["draft_state"] = "SENT"
            _record_approval_time()
            _audit(store, "user", "send", draft["id"], {"recipient": recipient})
            ui.notify("Approved and sent.", type="positive")
            ui.navigate.to("/drafts")
        except Exception as e:  # noqa: BLE001
            log.exception("send failed")
            _restore_retryable_after_send_failure(
                store, int(draft["id"]), body, report.flags, e
            )
            state["draft_state"] = "PENDING"
            _audit(store, "user", "error", draft["id"], {"error": str(e)})
            ui.notify(
                f"Send failed after approval: {e}. The draft is back in Pending for review/retry.",
                type="negative",
                timeout=10000,
            )
            _sync_buttons()

    def _reject() -> None:
        try:
            store.update_draft_state(draft["id"], "REJECTED")  # type: ignore[attr-defined]
            _audit(store, "user", "block", draft["id"], {"action": "reject"})
            ui.notify("Rejected (audit only).", type="info")
            ui.navigate.to("/drafts")
        except Exception as e:
            log.exception("reject failed")
            ui.notify(f"Reject failed: {e}", type="negative")

    # --- action bar -----------------------------------------------------------------
    if state["draft_state"] == "DEFERRED_NO_LLM":
        ui.label(
            "AI drafting was deferred and this body is empty. Use AI Revise or write and Save "
            "Changes before approval; an empty draft can never send."
        ).style("font-size: 12px; color: var(--warning)")

    with ui.row().classes(
        "w-full justify-end q-mt-sm bb-toolbar bb-sticky-actions"
    ).style("gap: 10px"):
        if ai_drawer is not None:
            ui.button("AI Revise", icon="auto_awesome", on_click=ai_drawer.show).props(
                "outline no-caps color=primary"
            )
        reject_btn = ui.button("Reject", icon="close", on_click=_reject).props(
            "outline color=negative"
        )
        save_btn = ui.button("Save Changes", icon="shield", on_click=_save_changes).props(
            "outline no-caps color=primary"
        )
        approve_btn = ui.button(
            "Approve & Send", icon="send", on_click=_approve_and_send
        ).props("unelevated no-caps color=positive")

    _sync_buttons()
