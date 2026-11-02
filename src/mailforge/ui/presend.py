"""The pre-send check popup, shared by the draft page and Compose.

Nothing leaves MailForge without passing through :func:`confirm`. It shows the
owner a short list of checks (ticks and warnings) before the Send button, and
lets them override the *soft* ones with an explicit tick-box. *Hard* checks
(an empty message, a blocked domain, a deleted source email, ...) can never be
overridden: they hide the Send button.

The module is split so the important parts need no browser to test:

* :func:`collect_checks` / :func:`evaluate` build the list (pure, no UI).
* :func:`run_send` does the transmit and the bookkeeping around it. The
  ``try`` covers the transmit ONLY. Once the mail has gone out, nothing that
  fails afterwards (a closed page, a dead notification slot, a store hiccup)
  can turn the send back into "failed" or put the draft back in the queue.
* :func:`confirm` is the dialog. It stays open while sending and runs the
  send as a shielded task, so closing the tab mid-send cannot interrupt the
  bookkeeping.
* :func:`safe_ui` / :func:`safe_notify` make UI feedback after an ``await``
  harmless when the page is gone.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from nicegui import run, ui

from ..security import run_output_guardrails
from ..security.inbound_screening import ai_may_read

log = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{\{\s*[a-zA-Z0-9_]+\s*\}\}")
_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+$")

_PII_NAMES = {
    "PHONE_NUMBER": "a phone number",
    "EMAIL_ADDRESS": "an email address",
    "CREDIT_CARD": "a card number",
    "IBAN_CODE": "a bank account number",
    "US_SSN": "a social security number",
    "IP_ADDRESS": "an IP address",
    "LOCATION": "a street address or place",
    "PERSON": "a person's name",
    "URL": "a web address",
    "DATE_TIME": "a date",
}


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Check:
    """One row of the popup. ``overridable`` only matters when it failed."""

    id: str
    label: str
    passed: bool
    detail: str = ""
    overridable: bool = True

    @property
    def blocking(self) -> bool:
        return (not self.passed) and (not self.overridable)

    @property
    def warning(self) -> bool:
        return (not self.passed) and self.overridable


@dataclass
class Evaluation:
    checks: list[Check]
    flags: dict[str, Any] = field(default_factory=dict)


def hard_failures(checks: Iterable[Check]) -> list[Check]:
    return [c for c in checks if c.blocking]


def warnings(checks: Iterable[Check]) -> list[Check]:
    return [c for c in checks if c.warning]


def signature(checks: Iterable[Check]) -> tuple[tuple[str, bool, str], ...]:
    """What the owner actually saw; used to notice a change before sending."""
    return tuple((c.id, c.passed, c.detail) for c in checks)


def split_recipients(value: str) -> list[str]:
    """Comma/semicolon-separated recipient field -> individual addresses.

    Display names are tolerated (``Name <a@b>`` -> ``a@b``); every returned
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


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].strip().lower() if "@" in addr else ""


def _value(row: Any, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, default)


def _pii_text(types: Iterable[str]) -> str:
    return ", ".join(_PII_NAMES.get(str(t), str(t).replace("_", " ").lower()) for t in types)


def _security_with_domains(security: Any, domains: set[str]) -> Any:
    merged = set(getattr(security, "recipient_allowlist_domains", set()) or set()) | domains
    try:
        return security.model_copy(update={"recipient_allowlist_domains": merged})
    except AttributeError:
        clone = copy.copy(security)
        clone.recipient_allowlist_domains = merged
        return clone


def _gate_checks(
    store: Any,
    settings: Any,
    *,
    recipients: list[str],
    subject: str,
    body: str,
    source_message_id: int | None,
    draft_state: str | None,
    sendable_states: Iterable[str],
    retype_ok: bool | None,
    rate: tuple[bool, int] | None,
    sender_ok: bool | None,
    require_subject: bool,
) -> list[Check]:
    sec = getattr(settings, "security", None)
    out: list[Check] = []

    approval_ok = bool(getattr(sec, "require_human_approval", False)) and not bool(
        getattr(sec, "autosend_allowed", False)
    )
    out.append(
        Check(
            "approval_rules",
            "MailForge is set to ask you before sending",
            approval_ok,
            "" if approval_ok else
            "A security setting would let mail go out without you. Fix it in Settings first.",
            overridable=False,
        )
    )
    if sender_ok is not None:
        out.append(
            Check(
                "sender_ok",
                "Sending from one of your mailboxes",
                sender_ok,
                "" if sender_ok else "That sender isn't one of your set-up mailboxes.",
                overridable=False,
            )
        )

    bad = [r for r in recipients if not _ADDRESS.match(r) or "." not in _domain(r)]
    valid = bool(recipients) and not bad
    out.append(
        Check(
            "recipient_valid",
            "The recipient address looks right",
            valid,
            "" if valid else (
                "Add who this is going to." if not recipients
                else "These don't look like email addresses: " + ", ".join(bad)
            ),
            overridable=False,
        )
    )
    block = {d.lower() for d in getattr(sec, "recipient_block_domains", set()) or set()}
    blocked = sorted({_domain(r) for r in recipients if _domain(r) in block})
    out.append(
        Check(
            "recipient_not_blocked",
            "The recipient isn't on your block list",
            not blocked,
            "" if not blocked else "You've blocked mail to: " + ", ".join(blocked),
            overridable=False,
        )
    )
    out.append(
        Check(
            "body_present",
            "The message isn't empty",
            bool(body.strip()),
            "" if body.strip() else "Write something in the message first.",
            overridable=False,
        )
    )
    left = sorted(set(PLACEHOLDER.findall(subject + "\n" + body)))
    out.append(
        Check(
            "no_placeholders",
            "No template blanks left to fill in",
            not left,
            "" if not left else "Still to fill in: " + ", ".join(left),
            overridable=False,
        )
    )
    if require_subject:
        out.append(
            Check(
                "subject_present",
                "The email has a subject",
                bool(subject.strip()),
                "" if subject.strip() else "The subject line is empty.",
            )
        )
    if rate is not None:
        ok, limit = rate
        out.append(
            Check(
                "rate_limit",
                "You're under the hourly sending limit",
                ok,
                "" if ok else f"You've reached {limit} sends this hour. Try again later.",
                overridable=False,
            )
        )
    if draft_state is not None:
        ok = str(draft_state).upper() in {s.upper() for s in sendable_states}
        out.append(
            Check(
                "draft_sendable",
                "This draft is ready to send",
                ok,
                "" if ok else f"This draft is {str(draft_state).lower()}, so it can't be sent.",
                overridable=False,
            )
        )
    if retype_ok is not None:
        out.append(
            Check(
                "retype",
                "You re-typed the new recipient's address",
                bool(retype_ok),
                "" if retype_ok else "Type the recipient's address again to confirm it's right.",
                overridable=False,
            )
        )
    if source_message_id is not None:
        source = None
        try:
            source = store.get_message(int(source_message_id))
        except Exception as e:  # noqa: BLE001
            log.warning("presend: source lookup failed: %s", e)
        if source is not None:
            withheld = bool(_value(source, "quarantined", 0)) or not ai_may_read(
                _value(source, "screening_status", "UNSCREENED")
            )
            out.append(
                Check(
                    "source_readable",
                    "The email you're replying to isn't held back",
                    not withheld,
                    "" if not withheld else
                    "That email is quarantined or held back by screening, so no reply can go out.",
                    overridable=False,
                )
            )
            gone = bool(_value(source, "trashed", 0)) or bool(_value(source, "purged_at", None))
            out.append(
                Check(
                    "source_not_deleted",
                    "The email you're replying to is still in your mailbox",
                    not gone,
                    "" if not gone else
                    "That email is in the Trash or deleted. Restore it first if you want to reply.",
                    overridable=False,
                )
            )
    return out


def _guard_checks(report: Any) -> list[Check]:
    reasons = [str(r) for r in (report.reasons or [])]
    flags = report.flags or {}
    buckets: dict[str, list[str]] = {
        k: [] for k in ("secrets", "pii", "urls", "cross", "a", "b", "c", "d", "content")
    }
    for r in reasons:
        low = r.lower()
        m = re.match(r"^\(([abcd])\)", low)
        if m:
            buckets[m.group(1)].append(r)
        elif low.startswith("secrets"):
            buckets["secrets"].append(r)
        elif "pii" in low:
            buckets["pii"].append(r)
        elif low.startswith("url blocked"):
            buckets["urls"].append(r)
        elif "thread" in low:
            buckets["cross"].append(r)
        else:
            buckets["content"].append(r)

    secrets_found = [str(s) for s in flags.get("secrets") or []]
    pii_found = [str(s) for s in flags.get("pii") or []]
    # Rule (d) is the same secrets/PII test seen from the policy engine, so it
    # shares the two rows above instead of repeating them.
    secrets_bad = bool(secrets_found or buckets["secrets"] or any("secrets" in r for r in buckets["d"]))
    pii_bad = bool(pii_found or buckets["pii"] or any("PII" in r for r in buckets["d"]))
    urls = flags.get("urls") if isinstance(flags.get("urls"), dict) else {}
    blocked_urls = [str(u) for u in (urls.get("blocked") or [])]
    cross = flags.get("crossthread") if isinstance(flags.get("crossthread"), dict) else {}
    cross_bad = bool(buckets["cross"] or cross.get("leaked_thread_ids"))
    # "explicitly blocked" is the block-list hard gate's job, not a soft rule.
    rule_b = [r for r in buckets["b"] if "explicitly blocked" not in r]

    def row(cid: str, label: str, bad: bool, detail: str) -> Check:
        return Check(cid, label, not bad, detail if bad else "")

    return [
        row("secrets", "No passwords or keys in the message", secrets_bad,
            "Looks like it contains: " + (", ".join(secrets_found) or "a secret") + "."),
        row("pii", "No personal details in the message", pii_bad,
            "Looks like it contains " + (_pii_text(pii_found) or "personal details") + "."),
        row("content", "The wording looks safe", bool(buckets["content"]),
            "; ".join(buckets["content"][:3])),
        row("links", "Links are on your allowed list", bool(buckets["urls"] or blocked_urls),
            "Not allowed: " + ", ".join(blocked_urls or buckets["urls"][:3])),
        row("other_threads", "Nothing copied from someone else's emails", cross_bad,
            "; ".join(buckets["cross"][:2]) or "Text matches another conversation."),
        row("rule_a", "You've dealt with this person before", bool(buckets["a"]),
            "This address isn't in the conversation, your contacts or your allowed list."),
        row("rule_b", "The recipient's domain is on your allowed list", bool(rule_b),
            "This domain isn't on your allowed list yet."),
        row("rule_c", "Not sent right after reading untrusted content", bool(buckets["c"]),
            "; ".join(buckets["c"][:1])),
    ]


def evaluate(
    store: Any,
    settings: Any,
    *,
    recipients: str,
    subject: str,
    body: str,
    site_id: str | None = None,
    thread_id: str = "",
    participants: Iterable[str] = (),
    source_message_id: int | None = None,
    draft_state: str | None = None,
    sendable_states: Iterable[str] = ("DRAFT", "PENDING"),
    retype_ok: bool | None = None,
    rate: tuple[bool, int] | None = None,
    sender_ok: bool | None = None,
    relax_recipient_rules: bool = False,
    require_subject: bool = False,
    scan_subject: bool = False,
    bridge: Any = None,
) -> Evaluation:
    """Run every check once. Never raises: a crashing guard is itself a row."""
    addrs = split_recipients(recipients)
    checks = _gate_checks(
        store,
        settings,
        recipients=addrs,
        subject=subject,
        body=body,
        source_message_id=source_message_id,
        draft_state=draft_state,
        sendable_states=sendable_states,
        retype_ok=retype_ok,
        rate=rate,
        sender_ok=sender_ok,
        require_subject=require_subject,
    )
    sec = getattr(settings, "security", settings)
    people = {a.lower() for a in participants}
    if relax_recipient_rules and addrs:
        # A brand-new email from the owner: its own recipients count as known
        # (rules a/b bind *replies* to people already in the conversation).
        sec = _security_with_domains(sec, {_domain(a) for a in addrs if _domain(a)})
        people |= {a.lower() for a in addrs}
    text = (subject + "\n" + body) if scan_subject else body
    flags: dict[str, Any] = {}
    try:
        report = run_output_guardrails(
            {
                "recipient": addrs[0] if addrs else "",
                "subject": subject,
                "body": text,
                "thread_id": thread_id,
            },
            {"recipient": addrs[0] if addrs else "", "thread_participants": people,
             "site_id": site_id},
            store,
            sec,
            bridge,
        )
        checks.extend(_guard_checks(report))
        flags = dict(report.flags or {})
    except Exception as e:  # noqa: BLE001
        log.exception("pre-send guardrails could not run")
        checks.append(
            Check(
                "guardrails_ran",
                "The safety checks ran",
                False,
                f"They couldn't run ({str(e)[:120]}). Nothing in the message has been checked.",
            )
        )
    return Evaluation(checks, flags)


def collect_checks(*args: Any, **kwargs: Any) -> list[Check]:
    return evaluate(*args, **kwargs).checks


def record_override(
    store: Any,
    *,
    draft_id: int | None,
    recipient: str,
    overridden: Iterable[Check],
) -> list[str]:
    """Audit that the owner sent anyway. Raises on failure, so the send aborts
    (a send past a warning with no evidence of who allowed it must not happen).
    Returns the overridden check ids."""
    from ..audit.log import AuditLog

    items = list(overridden)
    ids = [c.id for c in items]
    if not ids:
        return ids
    AuditLog(store).record(
        actor="user",
        event="approval",
        subject_table="drafts" if draft_id is not None else None,
        subject_id=draft_id,
        detail={
            "action": "guardrail_override",
            "draft_id": draft_id,
            "recipient": recipient,
            "overridden": ids,
            "reasons": [c.detail or c.label for c in items],
        },
    )
    return ids


# --------------------------------------------------------------------------- #
# Sending, without ever letting UI trouble undo it
# --------------------------------------------------------------------------- #
@dataclass
class SendResult:
    status: str  # "sent" | "failed"
    error: str = ""
    record_id: int | None = None
    persist_error: str = ""
    copy_warning: str = ""

    @property
    def sent(self) -> bool:
        return self.status == "sent"


def append_warning(store: Any, record_id: int | None) -> str:
    """Plain-English note when the Sent-folder copy did not happen, else ''."""
    if not record_id:
        return ""
    try:
        row = store.get_sent_message(int(record_id))
    except Exception:  # noqa: BLE001
        log.exception("could not read the Sent-folder status")
        return ""
    if row is None:
        return ""
    status = str(_value(row, "imap_append", "") or "").upper()
    if status not in {"FAILED", "SKIPPED"}:
        return ""
    note = str(_value(row, "imap_note", "") or "").strip()
    return f"Sent. The copy did not reach your Sent folder{': ' + note if note else '.'}"


async def run_send(
    transmit: Callable[[], Awaitable[Any]],
    *,
    persist: Callable[[Any], None] | None = None,
    on_failed: Callable[[Exception], None] | None = None,
    store: Any = None,
) -> SendResult:
    """Transmit, then do the bookkeeping. Only ``transmit`` is allowed to fail.

    ``transmit`` returns the outbox record id (or None). Everything after it
    returns is best effort and reported, never raised.
    """
    try:
        record_id = await transmit()
    except Exception as e:  # noqa: BLE001
        log.warning("send failed: %s", e)
        if on_failed is not None:
            try:
                on_failed(e)
            except Exception:  # noqa: BLE001
                log.exception("could not record the send failure")
        return SendResult("failed", error=str(e))
    result = SendResult("sent", record_id=record_id if isinstance(record_id, int) else None)
    if persist is not None:
        try:
            persist(record_id)
        except Exception as e:  # noqa: BLE001
            log.exception("mail was sent but the draft could not be marked sent")
            result.persist_error = str(e)
    result.copy_warning = append_warning(store, result.record_id)
    return result


_TASKS: set[asyncio.Future] = set()


def start_send(coro: Awaitable[SendResult]) -> asyncio.Future:
    """Run the send as its own task, held by a strong reference so it survives
    the page (and the handler awaiting it) going away."""
    task = asyncio.ensure_future(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


# --------------------------------------------------------------------------- #
# Harmless UI feedback
# --------------------------------------------------------------------------- #
def safe_ui(client: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a UI call inside the page's client; log instead of raising when the
    page is gone (closed tab, deleted slot)."""
    try:
        with client:
            return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.info("UI update skipped (page is gone?): %s", e)
        return None


def safe_notify(client: Any, message: str, **kwargs: Any) -> None:
    safe_ui(client, ui.notify, message, **kwargs)


def notify_result(client: Any, result: SendResult, *, sent_text: str, failed_prefix: str) -> None:
    """Report the outcome the same way on both send paths."""
    if result.sent:
        safe_notify(client, sent_text, type="positive")
        if result.copy_warning:
            safe_notify(client, result.copy_warning, type="warning", timeout=0, close_button=True)
        if result.persist_error:
            safe_notify(
                client,
                "The email was sent, but MailForge couldn't update the draft record "
                f"({result.persist_error[:120]}). Don't send it again.",
                type="warning", timeout=0, close_button=True,
            )
    else:
        safe_notify(client, f"{failed_prefix}: {result.error}", type="negative",
                    timeout=0, close_button=True)


# --------------------------------------------------------------------------- #
# The dialog
# --------------------------------------------------------------------------- #
async def confirm(
    *,
    title: str,
    facts: list[tuple[str, str]],
    collect: Callable[[], list[Check]],
    transmit: Callable[[list[Check]], Awaitable[Any]],
    persist: Callable[[Any], None] | None = None,
    on_failed: Callable[[Exception], None] | None = None,
    store: Any = None,
) -> SendResult | None:
    """Show the checks, let the owner decide, and send. ``None`` = not sent
    (cancelled, or the checks changed and the owner must look again).

    ``collect`` is called off the event loop and must not touch the UI.
    ``transmit(overridden)`` is the only code whose failure means "not sent".
    """
    client = ui.context.client
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()
    st: dict[str, Any] = {"phase": "checking", "checks": [], "notice": ""}

    def _finish(value: SendResult | None) -> None:
        if not done.done():
            done.set_result(value)
        safe_ui(client, dialog.close)

    with ui.dialog().props("persistent") as dialog, ui.card().style(
        "gap: 10px; width: min(580px, 92vw); max-width: 92vw"
    ):
        ui.label(title).style("font-weight: 800; font-size: 16px")
        with ui.column().classes("w-full").style("gap: 2px"):
            for caption, text in facts:
                ui.label(f"{caption}: {text}").style(
                    "font-size: 13px; color: var(--text-secondary); overflow-wrap: anywhere"
                )
        box = ui.column().classes("w-full").style("gap: 6px").props("data-testid=presend-checks")
        ack = ui.checkbox("I've read these warnings").props("data-testid=presend-ack")
        with ui.row().classes("w-full justify-end").style("gap: 8px"):
            back_btn = ui.button("Cancel", on_click=lambda: _finish(None)).props(
                "flat no-caps data-testid=presend-back"
            )
            send_btn = ui.button("Send", icon="send", on_click=lambda: _on_send()).props(
                "unelevated no-caps color=primary data-testid=presend-send"
            )

    def _render() -> None:
        checks: list[Check] = st["checks"]
        hard = hard_failures(checks)
        soft = warnings(checks)
        box.clear()
        with box:
            if st["phase"] == "checking":
                with ui.row().classes("items-center").style("gap: 8px"):
                    ui.spinner(size="sm")
                    ui.label("Checking your message…")
            else:
                if st["notice"]:
                    ui.label(st["notice"]).style("color: var(--warning-text); font-size: 13px")
                def _row(c: Check) -> None:
                    if c.passed:
                        icon, colour = "check_circle", "var(--success-text, #2e7d32)"
                    elif c.overridable:
                        icon, colour = "warning", "var(--warning-text)"
                    else:
                        icon, colour = "block", "var(--danger-text)"
                    with ui.row().classes("w-full no-wrap items-start").style("gap: 8px").props(
                        f"data-check={c.id} data-passed={'1' if c.passed else '0'}"
                    ):
                        ui.icon(icon, size="20px").style(f"color: {colour}")
                        with ui.column().style("gap: 0; min-width: 0"):
                            ui.label(c.label).style(
                                "font-size: 13.5px; font-weight: 600" if not c.passed
                                else "font-size: 13px"
                            )
                            if c.detail:
                                ui.label(c.detail).style(
                                    f"font-size: 12.5px; color: {colour}; overflow-wrap: anywhere"
                                )

                for c in hard + soft:
                    _row(c)
                ok_rows = [c for c in checks if c.passed]
                if ok_rows:
                    title_ok = (
                        f"{len(ok_rows)} other checks passed" if (hard or soft)
                        else f"All {len(ok_rows)} checks passed"
                    )
                    with ui.expansion(title_ok, icon="check_circle").props("dense").classes(
                        "w-full"
                    ).style("font-size: 13px"):
                        for c in ok_rows:
                            _row(c)
                if hard:
                    ui.label("Fix the red items to send. They can't be skipped.").style(
                        "font-size: 12.5px; color: var(--danger-text); font-weight: 600"
                    )
                elif soft:
                    ui.label(
                        "These are warnings, not errors. You can still send if you're sure."
                    ).style("font-size: 12.5px; color: var(--text-secondary)")
            if st["phase"] == "sending":
                with ui.row().classes("items-center").style("gap: 8px"):
                    ui.spinner(size="sm")
                    ui.label("Sending…")
        sending = st["phase"] == "sending"
        checking = st["phase"] == "checking"
        ack.set_visibility(bool(soft) and not hard and not checking)
        send_btn.set_visibility(not hard and not checking)
        send_btn.set_text("Send anyway" if soft else "Send")
        send_btn.set_enabled(not sending and (not soft or bool(ack.value)))
        back_btn.set_text("Cancel" if (not soft and not hard) else "Go back and edit")
        back_btn.set_enabled(not sending)
        ack.set_enabled(not sending)

    ack.on_value_change(lambda _e: _render())

    async def _on_send() -> None:
        if st["phase"] != "review":
            return
        try:
            fresh = await run.io_bound(collect)
        except Exception as e:  # noqa: BLE001
            log.exception("pre-send re-check failed")
            st.update(notice=f"The checks couldn't run again ({e}). Nothing was sent.")
            _render()
            return
        if signature(fresh) != signature(st["checks"]):
            st.update(
                checks=fresh,
                notice="Something changed while you were looking. Please read the checks "
                "again. Nothing was sent.",
            )
            ack.value = False
            _render()
            return
        soft = warnings(fresh)
        if hard_failures(fresh) or (soft and not ack.value):
            _render()
            return
        st.update(phase="sending", notice="")
        _render()
        task = start_send(
            run_send(lambda: transmit(soft), persist=persist, on_failed=on_failed, store=store)
        )
        result = await asyncio.shield(task)
        if not done.done():
            done.set_result(result)
        safe_ui(client, dialog.close)

    dialog.open()
    try:
        st["checks"] = await run.io_bound(collect)
    except Exception as e:  # noqa: BLE001
        log.exception("pre-send checks failed")
        st["checks"] = [
            Check("checks_ran", "The pre-send checks ran", False,
                  f"They couldn't run ({str(e)[:120]}).", overridable=False)
        ]
    st["phase"] = "review"
    _render()
    result = await done
    safe_ui(client, dialog.delete)
    return result
