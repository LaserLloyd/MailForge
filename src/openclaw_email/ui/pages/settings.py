"""Settings page (build spec §9, §8).

Forms for the user-editable parts of :class:`~openclaw_email.config.Settings`:
IMAP/SMTP accounts (host/port/username — display + write-back to the config
TOML via ``settings.save()``), style-guide + context-doc paths, allowlists
(recipient allowlist / block domains, link allowlist), signature, and tone.

SECRETS ARE NEVER SHOWN (invariant §0.5) — passwords/tokens live only in the
keyring and are not part of the ``Settings`` object. "Add mailbox" is
write-only: it accepts a password, hands it to the keyring, and clears the
field. Nothing on this page ever reads a credential back for display; the most
it reports is whether one exists.
"""

from __future__ import annotations

import logging
from typing import Any

from nicegui import run, ui

from ...db.store import DEFAULT_SITE_ID
from .. import theme

log = logging.getLogger(__name__)


def _has_credential(account: Any) -> bool:
    """Whether both keyring entries this mailbox needs exist (never the value).

    IMAP is keyed by account label and SMTP by username — see ``secrets.py``.
    A mailbox missing either looks configured but fails at login or reply time,
    so it is worth surfacing distinctly from 'not configured'.
    """
    try:
        from ...secrets import get_imap_secret, get_smtp_secret

        return (
            get_imap_secret(account.name) is not None
            and get_smtp_secret(account.username) is not None
        )
    except Exception as e:  # noqa: BLE001 - keyring backend may be unavailable
        log.warning("could not check credential presence for %s: %s", account.name, e)
        return True  # Unknown => don't cry wolf with a false NO CREDENTIAL badge.


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
            "Secrets (passwords / OAuth tokens) are never SHOWN here — they live only in the "
            "OS keyring (invariant §0.5). 'Add mailbox' is write-only: what you type goes "
            "straight to the keyring and is never read back."
        ).style("font-size: 12px; color: var(--text-secondary)")

    sec = getattr(settings, "security", None)
    style = getattr(settings, "style", None)

    # Configured site registry (id -> display name), used throughout this page.
    site_options: dict[str, str] = {
        sid: getattr(cfg, "name", sid)
        for sid, cfg in (getattr(settings, "sites", None) or {}).items()
    }
    default_site = next(iter(site_options), DEFAULT_SITE_ID)

    # --- active inbox coverage ------------------------------------------------
    # Driven by config, NOT the accounts table: config.toml is what run_serve
    # iterates to start listeners. The DB row outlives a removed mailbox on
    # purpose (messages.account_id is ON DELETE CASCADE, so dropping the row
    # would destroy that mailbox's history), which would make a DB-driven panel
    # claim coverage for an inbox nothing is listening to.
    by_site: dict[str, list[Any]] = {sid: [] for sid in site_options}
    by_site["unassigned"] = []
    for acct in getattr(settings, "imap_accounts", []) or []:
        site_id = str(getattr(acct, "site_id", "") or "unassigned").lower()
        by_site.setdefault(site_id, []).append(acct)

    with ui.card().classes("oce-card w-full"):
        ui.label("Inbox Coverage").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--accent-hover)"
        )
        ui.label(
            "Mailboxes the agent will start a listener for, from config.toml. These labels show "
            "configuration, not independent proof that every business alias exists."
        ).style("font-size: 12px; color: var(--text-secondary)")
        with ui.row().classes("w-full oce-toolbar").style("gap: 12px"):
            for site_id, label in site_options.items():
                with ui.element("div").classes("oce-card q-pa-sm").style(
                    "min-width: 230px; flex: 1"
                ):
                    ui.label(label).style("font-weight: 800")
                    rows = by_site.get(site_id, [])
                    if rows:
                        for row in rows:
                            ui.label(str(getattr(row, "username", "") or getattr(row, "name", "") or "(unnamed)")) \
                                .style("font-size: 12px; overflow-wrap: anywhere")
                    else:
                        ui.label("No active inbox configured").style(
                            "font-size: 12px; color: var(--error); font-weight: 700"
                        )
        # Any configured site with no listener is a real gap: mail to that brand
        # is being silently ignored. Reported per-site rather than hardcoded, so
        # a new [sites.<id>] entry is covered the moment it exists.
        uncovered = [label for sid, label in site_options.items() if not by_site.get(sid)]
        if uncovered:
            with ui.row().classes("w-full items-center q-pa-sm").style(
                "gap: 8px; border: 1px solid var(--error); border-radius: var(--radius-md)"
            ):
                ui.icon("warning", size="21px").style("color: var(--error)")
                ui.label(
                    f"NO INBOX CONFIGURED FOR: {', '.join(uncovered)} — mail to "
                    f"{'these brands is' if len(uncovered) > 1 else 'this brand is'} not being "
                    "collected. Use 'Add mailbox' below to connect one."
                ).style("font-size: 12.5px; color: var(--error); font-weight: 800")

    # --- IMAP accounts (display + editable host/port/username) ---------------
    imap_inputs: list[dict[str, Any]] = []
    with ui.card().classes("oce-card w-full"):
        ui.label("IMAP accounts").classes("text-subtitle1 text-weight-bold").style("color: var(--accent-hover)")
        accounts = list(getattr(settings, "imap_accounts", []) or [])
        if not accounts:
            ui.label("No IMAP accounts configured — add one below.").classes("text-grey")
        for acct in accounts:
            with ui.row().classes("items-center q-gutter-sm"):
                ui.label(acct.name).classes("text-weight-bold w-32")
                host = ui.input("Host", value=acct.host).props("dense")
                port = ui.number("Port", value=acct.port, format="%d").props("dense").classes("w-24")
                user = ui.input("Username", value=acct.username).props("dense")
                site = ui.select(
                    site_options,
                    value=getattr(acct, "site_id", default_site),
                    label="Site assignment",
                ).props("dense outlined").classes("w-44")
                # Presence, never the value — a mailbox with no stored credential
                # looks configured but silently fails to log in.
                if not _has_credential(acct):
                    theme.badge("NO CREDENTIAL", "error")
                ui.button(
                    icon="delete_outline",
                    on_click=lambda _e=None, a=acct: _confirm_remove(a),
                ).props("flat dense round color=negative").tooltip(
                    f"Stop listening to {acct.username} (messages are kept)"
                )
            imap_inputs.append(
                {"acct": acct, "host": host, "port": port, "user": user, "site": site}
            )

    # --- Add mailbox ---------------------------------------------------------
    # The one place a new inbox can be created from the UI. Writes all three
    # stores that must agree (config.toml + keyring + DB row) via accounts.py,
    # which is the same path `openclaw-email account-add` and any external
    # credential tooling uses.
    with ui.card().classes("oce-card w-full q-mt-md"):
        ui.label("Add mailbox").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--accent-hover)"
        )
        ui.label(
            "The password goes straight to the OS keyring — never into config.toml, and it is "
            "not shown again after saving. 'Test connection' proves the credential logs in "
            "before anything is written."
        ).style("font-size: 12px; color: var(--text-secondary)")
        with ui.row().classes("items-center q-gutter-sm q-mt-sm"):
            add_name = ui.input("Account label", placeholder="support").props("dense").classes("w-40")
            add_user = ui.input("Email address", placeholder="you@example.com").props("dense").classes("w-64")
            add_site = ui.select(site_options, value=default_site, label="Site").props(
                "dense outlined"
            ).classes("w-44")
        with ui.row().classes("items-center q-gutter-sm"):
            add_host = ui.input("IMAP host", value="imap.example.com").props("dense").classes("w-56")
            add_port = ui.number("IMAP port", value=993, format="%d").props("dense").classes("w-24")
            add_auth = ui.select(
                {"password": "Password", "xoauth2": "OAuth2 token"},
                value="password",
                label="Auth",
            ).props("dense outlined").classes("w-40")
            add_secret = ui.input("Password / token", password=True).props("dense").classes("w-56")
        with ui.expansion("Advanced — per-account SMTP override").classes("w-full"):
            ui.label(
                "Leave the host blank unless this mailbox sends through a different provider "
                "than the global SMTP relay above."
            ).style("font-size: 12px; color: var(--text-secondary)")
            with ui.row().classes("items-center q-gutter-sm"):
                add_smtp_host = ui.input("SMTP host", value="").props("dense").classes("w-56")
                add_smtp_port = ui.number("SMTP port", value=587, format="%d").props("dense").classes("w-24")
        with ui.row().classes("items-center q-gutter-sm q-mt-sm"):
            ui.button("Test connection", icon="wifi_tethering", on_click=lambda: _test_new()).props(
                "outline color=primary"
            )
            ui.button("Add mailbox", icon="add", on_click=lambda: _add_new()).props("color=primary")

    def _collect_new() -> dict[str, Any]:
        return {
            "name": (add_name.value or "").strip(),
            "username": (add_user.value or "").strip(),
            "site_id": add_site.value or default_site,
            "imap_host": (add_host.value or "").strip(),
            "imap_port": int(add_port.value or 993),
            "auth_method": add_auth.value or "password",
        }

    async def _test_new() -> None:
        from ...accounts import AccountError, test_imap_login, validate_new_account

        try:
            fields = validate_new_account(settings, **_collect_new())  # type: ignore[arg-type]
            secret = add_secret.value or ""
            if not secret:
                raise AccountError("Enter the password/token to test.")
        except AccountError as e:
            ui.notify(str(e), type="negative", timeout=8000)
            return
        ui.notify(f"Testing IMAP login for {fields['username']}…", type="ongoing")
        try:
            # Blocking socket I/O — off the event loop or the whole UI stalls.
            await run.io_bound(
                test_imap_login,
                fields["imap_host"],
                fields["imap_port"],
                fields["username"],
                secret,
                fields["auth_method"],
            )
        except AccountError as e:
            ui.notify(str(e), type="negative", timeout=12000)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("mailbox test failed")
            ui.notify(f"Test failed: {e}", type="negative", timeout=12000)
            return
        ui.notify(f"IMAP login OK for {fields['username']}.", type="positive", timeout=6000)

    async def _add_new() -> None:
        from ...accounts import AccountError, add_account

        secret = add_secret.value or ""
        try:
            fields = _collect_new()
            account = await run.io_bound(
                lambda: add_account(
                    settings,
                    imap_secret=secret,
                    smtp_host=(add_smtp_host.value or "").strip(),
                    smtp_port=int(add_smtp_port.value or 587),
                    **fields,  # type: ignore[arg-type]
                )
            )
        except AccountError as e:
            ui.notify(str(e), type="negative", timeout=10000)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("add mailbox failed")
            ui.notify(f"Could not add mailbox: {e}", type="negative", timeout=10000)
            return
        add_secret.value = ""  # never leave a credential sitting in the DOM
        ui.notify(
            f"Added {account.username}. Restart OpenClaw Email to start collecting its mail.",
            type="positive",
            timeout=10000,
        )
        ui.navigate.reload()

    def _confirm_remove(account: Any) -> None:
        with ui.dialog() as dialog, ui.card().classes("oce-card"):
            ui.label(f"Remove {account.username}?").classes("text-subtitle1 text-weight-bold")
            ui.label(
                "The listener stops and the stored password is forgotten. Messages already "
                "collected are KEPT, so re-adding the mailbox restores its history."
            ).style("font-size: 12px; color: var(--text-secondary); max-width: 380px")
            with ui.row().classes("justify-end w-full q-gutter-sm"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button(
                    "Remove", on_click=lambda: _do_remove(account, dialog)
                ).props("color=negative")
        dialog.open()

    def _do_remove(account: Any, dialog: Any) -> None:
        from ...accounts import AccountError, remove_account

        try:
            remove_account(settings, account.name)
        except AccountError as e:
            ui.notify(str(e), type="negative", timeout=10000)
            return
        finally:
            dialog.close()
        ui.notify(
            f"Removed {account.username}. Restart OpenClaw Email to stop its listener.",
            type="positive",
            timeout=8000,
        )
        ui.navigate.reload()

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

    # --- Security & quarantine (read-only) ------------------------------------
    with ui.card().classes("oce-card w-full q-mt-md"):
        ui.label("Security & quarantine").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--accent-hover)"
        )
        ui.label(
            "Quarantined messages are never shown to any AI — the local LLM or the OpenClaw "
            "bridge — until a human releases them in the Inbox."
        ).style("font-size: 12px; color: var(--text-secondary)")
        quarantine_enabled = bool(getattr(sec, "quarantine_enabled", False)) if sec else False
        quarantine_threshold = getattr(sec, "quarantine_threshold", None) if sec else None
        attachment_max_bytes = getattr(sec, "attachment_max_bytes", None) if sec else None
        attachment_max_count = getattr(sec, "attachment_max_count", None) if sec else None
        try:
            quarantined_now = int(store.quarantined_count())  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("could not load quarantined message count: %s", e)
            quarantined_now = None
        with ui.row().classes("w-full oce-toolbar").style("gap: 12px"):
            with ui.element("div").classes("oce-card q-pa-sm").style("min-width: 200px; flex: 1"):
                ui.label("Quarantine gate").style("font-weight: 800")
                theme.badge(
                    "ENABLED" if quarantine_enabled else "DISABLED",
                    "success" if quarantine_enabled else "error",
                )
                threshold_label = (
                    f"Score threshold: {quarantine_threshold:.2f}"
                    if isinstance(quarantine_threshold, (int, float))
                    else "Score threshold: n/a"
                )
                ui.label(threshold_label).style("font-size: 12px; color: var(--text-secondary)")
            with ui.element("div").classes("oce-card q-pa-sm").style("min-width: 200px; flex: 1"):
                ui.label("Attachment caps").style("font-weight: 800")
                size_label = (
                    f"{attachment_max_bytes / (1024 * 1024):.0f} MB / file"
                    if isinstance(attachment_max_bytes, (int, float)) and attachment_max_bytes
                    else "n/a"
                )
                count_label = (
                    f"{attachment_max_count} file(s) / message"
                    if isinstance(attachment_max_count, (int, float)) and attachment_max_count
                    else "n/a"
                )
                ui.label(size_label).style("font-size: 12px; color: var(--text-secondary)")
                ui.label(count_label).style("font-size: 12px; color: var(--text-secondary)")
            with ui.element("div").classes("oce-card q-pa-sm").style("min-width: 200px; flex: 1"):
                ui.label("Currently quarantined").style("font-weight: 800")
                ui.label(
                    str(quarantined_now) if quarantined_now is not None else "unavailable"
                ).style("font-size: 20px; font-weight: 800; color: var(--accent-hover)")

    # --- Sites (read-only) -----------------------------------------------------
    with ui.card().classes("oce-card w-full q-mt-md"):
        ui.label("Sites").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--accent-hover)"
        )
        ui.label(
            "New sites are added via a [sites.<id>] entry in config.toml, then appear in the "
            "'Add mailbox' site list above. Mailboxes can also be added with "
            "'openclaw-email account-add'."
        ).style("font-size: 12px; color: var(--text-secondary)")
        sites_map = getattr(settings, "sites", None) or {}
        if not sites_map:
            ui.label("No sites configured.").classes("text-grey")
        for sid, cfg in sites_map.items():
            name = getattr(cfg, "name", sid)
            guidance = (getattr(cfg, "guidance", "") or "").strip()
            snippet = guidance if len(guidance) <= 140 else guidance[:137] + "..."
            with ui.row().classes("items-start no-wrap q-pa-xs").style("gap: 8px"):
                theme.badge(sid, "accent")
                with ui.column().style("gap: 2px; min-width: 0"):
                    ui.label(name).style("font-weight: 700")
                    if snippet:
                        ui.label(snippet).style(
                            "font-size: 12px; color: var(--text-secondary); "
                            "overflow-wrap: anywhere"
                        )

    # --- Save ----------------------------------------------------------------
    def _save() -> None:
        try:
            for row in imap_inputs:
                acct = row["acct"]
                acct.host = (row["host"].value or "").strip()
                acct.port = int(row["port"].value or acct.port)
                acct.username = (row["user"].value or "").strip()
                acct.site_id = row["site"].value or default_site
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
            ui.notify(
                f"Saved to {path}. Restart OpenClaw Email to apply mailbox, site, SMTP, "
                "or model changes to active listeners.",
                type="positive",
                timeout=10000,
            )
        except Exception as e:
            log.exception("settings save failed")
            ui.notify(f"Save failed: {e}", type="negative")

    ui.button("Save settings", icon="save", on_click=_save).props("color=primary").classes("q-mt-md")
