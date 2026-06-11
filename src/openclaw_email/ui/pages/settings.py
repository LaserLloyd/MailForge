"""Settings page (build spec §9, §8).

Forms for the user-editable parts of :class:`~openclaw_email.config.Settings`:
IMAP/SMTP accounts (host/port/username — display + write-back to the config
TOML via ``settings.save()``), style-guide + context-doc paths, allowlists
(recipient allowlist / block domains, link allowlist), signature, and tone.

SECRETS ARE NEVER SHOWN (invariant §0.5) — passwords/tokens live only in the
keyring and are not part of the ``Settings`` object. This page edits config
metadata only; credential entry is the setup-wizard's job.
"""

from __future__ import annotations

import logging
from typing import Any

from nicegui import ui

log = logging.getLogger(__name__)


def _csv(values: Any) -> str:
    """Render a set/list of strings as a comma-separated string for editing."""
    if not values:
        return ""
    return ", ".join(sorted(str(v) for v in values))


def _parse_csv(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def render(store: object, settings: object) -> None:
    """Render the settings page (spec §9). Edits write back via ``settings.save()``."""
    with ui.row().classes("items-center").style("gap: 8px"):
        ui.icon("key_off", size="18px").style("color: var(--text-secondary)")
        ui.label(
            "Secrets (passwords / OAuth tokens) are NOT shown or editable here — "
            "they live only in the OS keyring (invariant §0.5)."
        ).style("font-size: 12px; color: var(--text-secondary)")

    sec = getattr(settings, "security", None)
    style = getattr(settings, "style", None)

    # --- IMAP accounts (display + editable host/port/username) ---------------
    imap_inputs: list[dict[str, Any]] = []
    with ui.card().classes("oce-card w-full"):
        ui.label("IMAP accounts").classes("text-subtitle1 text-weight-bold").style("color: var(--accent-hover)")
        accounts = list(getattr(settings, "imap_accounts", []) or [])
        if not accounts:
            ui.label("No IMAP accounts configured (run setup-wizard).").classes("text-grey")
        for acct in accounts:
            with ui.row().classes("items-center q-gutter-sm"):
                ui.label(acct.name).classes("text-weight-bold w-32")
                host = ui.input("Host", value=acct.host).props("dense")
                port = ui.number("Port", value=acct.port, format="%d").props("dense").classes("w-24")
                user = ui.input("Username", value=acct.username).props("dense")
            imap_inputs.append({"acct": acct, "host": host, "port": port, "user": user})

    # --- SMTP account --------------------------------------------------------
    smtp_inputs: dict[str, Any] = {}
    smtp = getattr(settings, "smtp", None)
    with ui.card().classes("oce-card w-full q-mt-md"):
        ui.label("SMTP account").classes("text-subtitle1 text-weight-bold").style("color: var(--accent-hover)")
        if smtp is None:
            ui.label("No SMTP account configured (run setup-wizard).").classes("text-grey")
        else:
            with ui.row().classes("items-center q-gutter-sm"):
                smtp_inputs["host"] = ui.input("Host", value=smtp.host).props("dense")
                smtp_inputs["port"] = ui.number(
                    "Port", value=smtp.port, format="%d"
                ).props("dense").classes("w-24")
                smtp_inputs["user"] = ui.input("Username", value=smtp.username).props("dense")
                smtp_inputs["starttls"] = ui.switch("STARTTLS", value=smtp.starttls)

    # --- Style / context docs / signature / tone -----------------------------
    style_inputs: dict[str, Any] = {}
    with ui.card().classes("oce-card w-full q-mt-md"):
        ui.label("Style & context").classes("text-subtitle1 text-weight-bold").style("color: var(--accent-hover)")
        style_inputs["style_paths"] = ui.input(
            "Style-guide paths (comma-separated)",
            value=_csv(getattr(style, "style_guide_paths", []) if style else []),
        ).classes("w-full")
        style_inputs["context_paths"] = ui.input(
            "Context-doc paths (comma-separated)",
            value=_csv(getattr(style, "context_doc_paths", []) if style else []),
        ).classes("w-full")
        style_inputs["signature"] = ui.textarea(
            "Signature", value=getattr(style, "signature", "") if style else ""
        ).classes("w-full").props("outlined")
        style_inputs["tone"] = ui.input(
            "Tone", value=getattr(style, "tone", "professional") if style else "professional"
        ).classes("w-full")

    # --- Allowlists / block domains ------------------------------------------
    allow_inputs: dict[str, Any] = {}
    with ui.card().classes("oce-card w-full q-mt-md"):
        ui.label("Allowlists & block domains").classes("text-subtitle1 text-weight-bold").style("color: var(--accent-hover)")
        allow_inputs["recipient_allow"] = ui.input(
            "Recipient allowlist domains (comma-separated)",
            value=_csv(getattr(sec, "recipient_allowlist_domains", set()) if sec else set()),
        ).classes("w-full")
        allow_inputs["recipient_block"] = ui.input(
            "Recipient BLOCK domains (comma-separated)",
            value=_csv(getattr(sec, "recipient_block_domains", set()) if sec else set()),
        ).classes("w-full")
        allow_inputs["link_allow"] = ui.input(
            "Link allowlist domains (comma-separated)",
            value=_csv(getattr(sec, "link_allowlist_domains", set()) if sec else set()),
        ).classes("w-full")

    # --- Save ----------------------------------------------------------------
    def _save() -> None:
        try:
            for row in imap_inputs:
                acct = row["acct"]
                acct.host = (row["host"].value or "").strip()
                acct.port = int(row["port"].value or acct.port)
                acct.username = (row["user"].value or "").strip()
            if smtp is not None and smtp_inputs:
                smtp.host = (smtp_inputs["host"].value or "").strip()
                smtp.port = int(smtp_inputs["port"].value or smtp.port)
                smtp.username = (smtp_inputs["user"].value or "").strip()
                smtp.starttls = bool(smtp_inputs["starttls"].value)
            if style is not None:
                style.style_guide_paths = _parse_csv(style_inputs["style_paths"].value)
                style.context_doc_paths = _parse_csv(style_inputs["context_paths"].value)
                style.signature = style_inputs["signature"].value or ""
                style.tone = (style_inputs["tone"].value or "professional").strip()
            if sec is not None:
                sec.recipient_allowlist_domains = set(
                    _parse_csv(allow_inputs["recipient_allow"].value)
                )
                sec.recipient_block_domains = set(_parse_csv(allow_inputs["recipient_block"].value))
                sec.link_allowlist_domains = set(_parse_csv(allow_inputs["link_allow"].value))

            # Re-assert invariants before persisting (spec §0).
            if hasattr(settings, "assert_invariants"):
                settings.assert_invariants()  # type: ignore[attr-defined]
            path = settings.save()  # type: ignore[attr-defined]
            ui.notify(f"Saved to {path}", type="positive")
        except Exception as e:
            log.exception("settings save failed")
            ui.notify(f"Save failed: {e}", type="negative")

    ui.button("Save settings", icon="save", on_click=_save).props("color=primary").classes("q-mt-md")
