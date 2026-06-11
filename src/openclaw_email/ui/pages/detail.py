"""Draft detail + human-approval page (build spec §5, §9, §11, §0.1, §0.4).

Chat-style review screen. Shows everything a human needs to decide on a draft:
  * the ORIGINAL email as an inbound bubble, with symbolic ``[link_N]``
    references resolved to their real targets for the human (via
    ``store.resolve_links``) — the LLM never saw these targets (spec §4);
  * the DRAFTED reply as an editable outbound bubble;
  * the GUARDRAIL flags persisted on the draft (``drafts.guardrail_flags``);
  * PROVENANCE — "agent saw these emails" — the same-thread messages.

Actions (spec §5):
  * Approve — write an approval record (state APPROVED, approved_by/at), then
    send via SMTP, then mark SENT. The send call is guarded by an inline assert
    that a human-approval record exists (invariant §0.1 / §11).
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

from nicegui import ui

from ...security import run_output_guardrails
from .. import theme
from ..interrupt import ApprovalRecord

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
    """Best-effort audit append (full chained-hash impl lives in audit/log.py)."""
    try:
        from ...audit import log as audit_log  # local import: optional module

        if hasattr(audit_log, "record"):
            audit_log.record(store, actor=actor, event=event, subject_table="drafts",
                             subject_id=draft_id, detail=detail)
            return
    except Exception as e:
        log.debug("audit module unavailable, writing raw audit row: %s", e)
    # Fallback: write an unchained row so the event is not lost.
    try:
        import hashlib

        prev = store.last_audit_hash()  # type: ignore[attr-defined]
        payload = json.dumps(detail, sort_keys=True, default=str)
        this = hashlib.sha256(((prev or "") + actor + event + payload).encode()).hexdigest()
        store.append_audit(  # type: ignore[attr-defined]
            _now_iso(), actor, event, "drafts", draft_id, payload, prev, this
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
        out = out.replace(f"[{symbol}]", f"[{symbol} -> {target}]")
    return out


def _smtp_settings(settings: object) -> Any:
    return getattr(settings, "smtp", None)


def _do_send(store: object, settings: object, draft: Any, body: str, approval: ApprovalRecord) -> None:
    """Privileged SMTP transmit — gated by an explicit human-approval record.

    Mirrors invariant §0.1 / §11: assert a human-approval record exists and
    autosend is NOT enabled before calling ``send_email``. This function is only
    ever reached from an explicit Approve/Edit-save click handler.
    """
    # --- INVARIANT §0.1 / §11: human-approval record must exist; no autosend ---
    assert isinstance(approval, ApprovalRecord) and approval.approved_by, (
        "INVARIANT VIOLATION: send attempted without a human-approval record"
    )
    sec = getattr(settings, "security", None)
    assert not bool(getattr(sec, "autosend_allowed", False)), (
        "INVARIANT VIOLATION: security.autosend_allowed must be False"
    )

    from ... import secrets as secret_store
    from ...mail.smtp_sender import send_email

    smtp_cfg = _smtp_settings(settings)
    if smtp_cfg is None:
        raise RuntimeError("No SMTP account configured (Settings page).")

    # Account name for the keyring lookup: SMTP secrets are keyed by account
    # name (spec §8). We use the SMTP username as the account key by convention.
    secret = secret_store.get_smtp_secret(smtp_cfg.username)
    if secret is None:
        raise RuntimeError(
            f"No SMTP secret in keyring for '{smtp_cfg.username}'. Run setup-wizard."
        )

    in_reply_to = None
    references = None
    try:
        msg = store.get_message(draft["message_id"])  # type: ignore[attr-defined]
        if msg is not None:
            in_reply_to = msg["message_id"]
    except Exception:
        pass

    send_email(
        smtp_cfg=smtp_cfg,
        secret=secret,
        from_addr=smtp_cfg.username,
        to_addr=approval.recipient,
        subject=draft["subject"] or "",
        body=body,
        in_reply_to=in_reply_to,
        references=references,
    )


def _bubble_meta(text: str) -> None:
    ui.label(text).style("font-size: 11.5px; color: var(--text-muted)")


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


def render(store: object, settings: object, draft_id: int) -> None:
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
            ui.button("Back to inbox", on_click=lambda: ui.navigate.to("/")).props("flat")
        return

    message_id = draft["message_id"]
    recipient = draft["recipient"] or ""
    try:
        is_external = not store.is_allowlisted(recipient)  # type: ignore[attr-defined]
    except Exception:
        is_external = True

    # Mutable per-render state.
    state: dict[str, Any] = {"body": draft["body"] or "", "approved": False}

    # --- header row -----------------------------------------------------------
    with ui.row().classes("w-full items-center").style("gap: 10px"):
        ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props(
            "flat round dense"
        ).classes("oce-nav-btn")
        ui.label(draft["subject"] or "(no subject)").classes("text-h6").style(
            "font-weight: 700"
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

    with ui.row().classes("w-full no-wrap").style("gap: 10px"):
        ui.icon("mail", size="26px").style("color: var(--text-secondary); margin-top: 6px")
        with ui.column().classes("col").style("gap: 4px; min-width: 0"):
            if orig is not None:
                _bubble_meta(
                    f"{orig['from_name'] or ''} <{orig['from_addr'] or ''}>"
                    f"  ·  {orig['received_at'] or ''}"
                )
                with ui.element("div").classes("oce-bubble oce-bubble--bot w-full"):
                    resolved = _resolve_body_links(
                        store, message_id, orig["sanitized_text"] or ""
                    )
                    ui.label(
                        "Links shown as RESOLVED targets (you see real URLs; "
                        "the LLM never did)."
                    ).style("font-size: 11px; color: var(--text-muted)")
                    ui.markdown(f"```\n{resolved}\n```")
            else:
                with ui.element("div").classes("oce-bubble oce-bubble--bot w-full"):
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
    ).classes("w-full oce-card"):
        if thread:
            for m in thread:
                marker = "  ← this email" if m["id"] == message_id else ""
                ui.label(
                    f"{m['received_at'] or '?'}  ·  {m['from_addr'] or '?'}  ·  "
                    f"{m['subject'] or ''}{marker}"
                ).classes("oce-mono").style("font-size: 12.5px")
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
    with ui.expansion(title, icon="shield", value=fired).classes("w-full oce-card"):
        if flags:
            ui.code(json.dumps(flags, indent=2, default=str)).classes("w-full")
        else:
            ui.label("No guardrail flags recorded.").style("color: var(--text-secondary)")

    # --- recipient binding (§0.4) ----------------------------------------------
    retype_ok = {"value": not is_external}  # allowlisted => no retype needed

    with ui.element("div").classes("oce-card w-full q-pa-md"):
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
            with ui.element("div").classes("oce-bubble oce-bubble--user w-full"):
                body_area = (
                    ui.textarea(value=state["body"])
                    .classes("w-full")
                    .props("autogrow borderless")
                )
                body_area.on("update:model-value", lambda e: state.update(body=e.args or ""))
            guard_box = ui.column().classes("w-full")
        ui.icon("edit_note", size="26px").style(
            "color: var(--accent-hover); margin-top: 6px"
        )

    # --- action handlers ----------------------------------------------------------
    def _terminal() -> bool:
        """Already in a terminal state -> no further actions."""
        return (draft["state"] or "").upper() in {"SENT", "REJECTED", "BLOCKED"}

    def _sync_buttons() -> None:
        can_send = retype_ok["value"] and not _terminal()
        approve_btn.set_enabled(can_send)
        edit_btn.set_enabled(not _terminal())
        reject_btn.set_enabled(not _terminal())

    def _approve_and_send() -> None:
        allowed, limit = _rate_limit_ok(settings)
        if not allowed:
            ui.notify(f"Rate limit reached ({limit}/hour). Try later.", type="negative")
            return
        if not retype_ok["value"]:
            ui.notify("Re-type the recipient address first.", type="warning")
            return
        body = state["body"]
        try:
            # Write the human-approval record FIRST (state APPROVED), then send.
            approved_at = _now_iso()
            store.update_draft_state(  # type: ignore[attr-defined]
                draft["id"], "APPROVED", approved_by="ui-user", approved_at=approved_at, body=body
            )
            approval = ApprovalRecord(
                draft_id=draft["id"],
                approved_by="ui-user",
                approved_at=approved_at,
                response_type="accept",
                recipient=recipient,
            )
            _audit(store, "user", "approval", draft["id"], {"recipient": recipient})
            _do_send(store, settings, draft, body, approval)
            store.update_draft_state(  # type: ignore[attr-defined]
                draft["id"], "SENT", sent_at=_now_iso(), body=body
            )
            _record_approval_time()
            _audit(store, "user", "send", draft["id"], {"recipient": recipient})
            ui.notify("Approved and sent.", type="positive")
            ui.navigate.to("/")
        except Exception as e:
            log.exception("send failed")
            _audit(store, "user", "error", draft["id"], {"error": str(e)})
            ui.notify(f"Send failed: {e}", type="negative")

    def _save_edit() -> None:
        """Re-run output guardrails on the edited body; pass -> send (spec §5)."""
        allowed, limit = _rate_limit_ok(settings)
        if not allowed:
            ui.notify(f"Rate limit reached ({limit}/hour). Try later.", type="negative")
            return
        if not retype_ok["value"]:
            ui.notify("Re-type the recipient address first.", type="warning")
            return
        body = state["body"]
        # Re-run guardrails on the edited content BEFORE any send (§5, §11).
        edited = {"recipient": recipient, "body": body, "thread_id": draft["thread_id"],
                  "subject": draft["subject"]}
        try:
            report = run_output_guardrails(
                edited, {"recipient": recipient}, store,
                getattr(settings, "security", settings),
            )
        except Exception as e:
            log.exception("guardrail re-run failed")
            ui.notify(f"Guardrail error (blocked): {e}", type="negative")
            return

        guard_box.clear()
        store.update_draft_state(  # type: ignore[attr-defined]
            draft["id"], draft["state"], body=body, guardrail_flags=report.flags
        )
        if not report.passed:
            # Block: show flags, do NOT send (§5 FAIL branch).
            _audit(store, "guardrail", "block", draft["id"],
                   {"reasons": report.reasons})
            with guard_box:
                ui.label("Edit BLOCKED by guardrails — not sent.").style(
                    "color: var(--error); font-weight: 700"
                )
                for r in report.reasons:
                    ui.label(f"• {r}").style("color: var(--error); font-size: 13px")
                ui.code(json.dumps(report.flags, indent=2, default=str)).classes("w-full")
            ui.notify("Edit blocked by guardrails.", type="negative")
            return

        # Pass -> approval record -> send.
        try:
            approved_at = _now_iso()
            store.update_draft_state(  # type: ignore[attr-defined]
                draft["id"], "APPROVED", approved_by="ui-user",
                approved_at=approved_at, body=body, guardrail_flags=report.flags,
            )
            approval = ApprovalRecord(
                draft_id=draft["id"], approved_by="ui-user", approved_at=approved_at,
                response_type="edit", recipient=recipient, flags=report.flags,
            )
            _audit(store, "user", "approval", draft["id"],
                   {"recipient": recipient, "edited": True})
            _do_send(store, settings, draft, body, approval)
            store.update_draft_state(  # type: ignore[attr-defined]
                draft["id"], "SENT", sent_at=_now_iso(), body=body
            )
            _record_approval_time()
            _audit(store, "user", "send", draft["id"], {"recipient": recipient, "edited": True})
            ui.notify("Guardrails passed — edited draft sent.", type="positive")
            ui.navigate.to("/")
        except Exception as e:
            log.exception("send after edit failed")
            _audit(store, "user", "error", draft["id"], {"error": str(e)})
            ui.notify(f"Send failed: {e}", type="negative")

    def _reject() -> None:
        try:
            store.update_draft_state(draft["id"], "REJECTED")  # type: ignore[attr-defined]
            _audit(store, "user", "block", draft["id"], {"action": "reject"})
            ui.notify("Rejected (audit only).", type="info")
            ui.navigate.to("/")
        except Exception as e:
            log.exception("reject failed")
            ui.notify(f"Reject failed: {e}", type="negative")

    # --- action bar -----------------------------------------------------------------
    with ui.row().classes("w-full justify-end q-mt-sm").style("gap: 10px"):
        reject_btn = ui.button("Reject", icon="close", on_click=_reject).props(
            "outline color=negative"
        )
        edit_btn = ui.button(
            "Save edit & re-guardrail", icon="shield", on_click=_save_edit
        ).props("color=primary")
        approve_btn = ui.button(
            "Approve & Send", icon="send", on_click=_approve_and_send
        ).props("color=positive")

    _sync_buttons()
