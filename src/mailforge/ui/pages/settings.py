"""Settings page (build spec §9, §8).

Forms for the user-editable parts of :class:`~mailforge.config.Settings`:
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
from .. import presend, theme

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


def render(store: object, settings: object, bridge: object | None = None) -> None:
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

    with ui.card().classes("bb-card w-full"):
        ui.label("Inbox Coverage").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--text-link)"
        )
        ui.label(
            "Mailboxes the agent will start a listener for, from config.toml. These labels show "
            "configuration, not independent proof that every business alias exists."
        ).style("font-size: 12px; color: var(--text-secondary)")
        with ui.row().classes("w-full bb-toolbar").style("gap: 12px"):
            for site_id, label in site_options.items():
                with ui.element("div").classes("bb-card q-pa-sm").style(
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
                            "font-size: 12px; color: var(--danger-text); font-weight: 700"
                        )
        # Any configured site with no listener is a real gap: mail to that brand
        # is being silently ignored. Reported per-site rather than hardcoded, so
        # a new [sites.<id>] entry is covered the moment it exists.
        uncovered = [label for sid, label in site_options.items() if not by_site.get(sid)]
        if uncovered:
            with ui.row().classes("w-full items-center q-pa-sm").style(
                "gap: 8px; border: 1px solid var(--danger-text); border-radius: var(--radius-md)"
            ):
                ui.icon("warning", size="21px").style("color: var(--danger-text)")
                ui.label(
                    f"NO INBOX CONFIGURED FOR: {', '.join(uncovered)} — mail to "
                    f"{'these brands is' if len(uncovered) > 1 else 'this brand is'} not being "
                    "collected. Use 'Add mailbox' below to connect one."
                ).style("font-size: 12.5px; color: var(--danger-text); font-weight: 800")

    # --- IMAP accounts (display + editable host/port/username) ---------------
    imap_inputs: list[dict[str, Any]] = []
    style_inputs: dict[str, Any] = {}

    def _shared_signature() -> str:
        box = style_inputs.get("signature")
        if box is not None:
            return str(box.value or "").strip()
        from ...signatures import global_signature

        return global_signature(settings)

    with ui.card().classes("bb-card w-full"):
        ui.label("IMAP accounts").classes("text-subtitle1 text-weight-bold").style("color: var(--text-link)")
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
            with ui.column().classes("w-full").style("gap: 2px; margin: -4px 0 8px 0"):
                with ui.row().classes("items-center").style("gap: 6px"):
                    ui.label("Address:").style("font-size: 12px; color: var(--text-secondary)")
                    theme.copyable_address(acct.username, style="font-size: 12px; font-weight: 700")
                sig = ui.textarea(
                    "Signature for this mailbox (optional)",
                    value=getattr(acct, "signature", "") or "",
                    placeholder="Leave empty to use the shared signature below.",
                ).classes("w-full").props("outlined dense autogrow")
                sig_preview = ui.label("").style(
                    "white-space: pre-wrap; font-size: 12px; color: var(--text-secondary); "
                    "border-left: 3px solid var(--border); padding-left: 8px"
                )

                def _paint(_e=None, s=sig, pv=sig_preview) -> None:
                    from ...signatures import block

                    own = str(s.value or "").strip()
                    shared = _shared_signature()
                    text = own or shared
                    if not text:
                        pv.set_text("Preview: no signature will be added for this mailbox.")
                    else:
                        who = "its own" if own else "the shared"
                        pv.set_text(f"Preview ({who} signature):" + block(text))

                sig.on_value_change(_paint)
                _paint()
            imap_inputs.append(
                {"acct": acct, "host": host, "port": port, "user": user, "site": site, "sig": sig}
            )

    # --- Add mailbox ---------------------------------------------------------
    # The one place a new inbox can be created from the UI. Writes all three
    # stores that must agree (config.toml + keyring + DB row) via accounts.py,
    # which is the same path `mailforge account-add` and any external
    # credential tooling uses.
    with ui.card().classes("bb-card w-full q-mt-md"):
        ui.label("Add mailbox").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--text-link)"
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
        client = ui.context.client  # the page may be gone when the login test returns
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
            presend.safe_notify(client, str(e), type="negative", timeout=12000)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("mailbox test failed")
            presend.safe_notify(client, f"Test failed: {e}", type="negative", timeout=12000)
            return
        presend.safe_notify(
            client, f"IMAP login OK for {fields['username']}.", type="positive", timeout=6000
        )

    async def _add_new() -> None:
        from ...accounts import AccountError, add_account

        secret = add_secret.value or ""
        client = ui.context.client  # the page may be gone when the add finishes
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
            presend.safe_notify(client, str(e), type="negative", timeout=10000)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("add mailbox failed")
            presend.safe_notify(
                client, f"Could not add mailbox: {e}", type="negative", timeout=10000
            )
            return

        def _clear_secret() -> None:
            add_secret.value = ""  # never leave a credential sitting in the DOM

        presend.safe_ui(client, _clear_secret)
        presend.safe_notify(
            client,
            f"Added {account.username}. Restart MailForge to start collecting its mail.",
            type="positive",
            timeout=10000,
        )
        presend.safe_ui(client, ui.navigate.reload)

    def _confirm_remove(account: Any) -> None:
        with ui.dialog() as dialog, ui.card().classes("bb-card"):
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
            f"Removed {account.username}. Restart MailForge to stop its listener.",
            type="positive",
            timeout=8000,
        )
        ui.navigate.reload()

    # --- SMTP account --------------------------------------------------------
    smtp_inputs: dict[str, Any] = {}
    smtp = getattr(settings, "smtp", None)
    with ui.card().classes("bb-card w-full q-mt-md"):
        ui.label("SMTP account").classes("text-subtitle1 text-weight-bold").style("color: var(--text-link)")
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
    with ui.card().classes("bb-card w-full q-mt-md"):
        ui.label("Style & context").classes("text-subtitle1 text-weight-bold").style("color: var(--text-link)")
        style_inputs["style_paths"] = ui.input(
            "Style-guide paths (comma-separated)",
            value=_csv(getattr(style, "style_guide_paths", []) if style else []),
        ).classes("w-full")
        style_inputs["context_paths"] = ui.input(
            "Context-doc paths (comma-separated)",
            value=_csv(getattr(style, "context_doc_paths", []) if style else []),
        ).classes("w-full")
        style_inputs["signature"] = ui.textarea(
            "Shared signature (used by any mailbox without its own)",
            value=getattr(style, "signature", "") if style else "",
        ).classes("w-full").props("outlined")
        style_inputs["tone"] = ui.input(
            "Tone", value=getattr(style, "tone", "professional") if style else "professional"
        ).classes("w-full")

    # --- Allowlists / block domains ------------------------------------------
    allow_inputs: dict[str, Any] = {}
    with ui.card().classes("bb-card w-full q-mt-md"):
        ui.label("Allowlists & block domains").classes("text-subtitle1 text-weight-bold").style("color: var(--text-link)")
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

    # --- AI model (local server or an API provider) ----------------------------
    from ...llm.providers import PRESETS, resolve_api_key

    llm_cfg = getattr(settings, "llm", None)
    ai_inputs: dict[str, Any] = {}
    if llm_cfg is not None:
        key_stored = bool(resolve_api_key(llm_cfg))
        with ui.card().classes("bb-card w-full q-mt-md"):
            with ui.row().classes("items-center").style("gap: 8px"):
                ui.icon("smart_toy", size="20px").style("color: var(--text-link)")
                ui.label("AI model").classes("text-subtitle1 text-weight-bold").style(
                    "color: var(--text-link)"
                )
            ui.label(
                "Choose what reads and drafts your mail: a model server on your own network "
                "(private, no per-use cost) or a hosted API provider (no hardware needed). "
                "With an API provider, the text of the messages it works on is sent to that "
                "provider. Quarantined mail never is. Knowledge search stays on the local "
                "server either way."
            ).style("font-size: 12px; color: var(--text-secondary)")
            ai_inputs["primary"] = ui.toggle(
                {"local": "Local server", "api": "API provider"},
                value=str(getattr(llm_cfg, "primary", "local") or "local"),
            ).props("no-caps")
            with ui.column().classes("w-full").style("gap: 6px") as api_box:
                with ui.row().classes("w-full items-center").style("gap: 10px"):
                    preset = ui.select(
                        {k: v.label for k, v in PRESETS.items()},
                        value=str(getattr(llm_cfg, "api_preset", "") or "custom")
                        if str(getattr(llm_cfg, "api_preset", "") or "custom") in PRESETS
                        else "custom",
                        label="Provider",
                    ).props("dense outlined").classes("w-56")
                    base = ui.input(
                        "API base URL", value=str(getattr(llm_cfg, "api_base_url", "") or ""),
                        placeholder="https://api.example.com/v1",
                    ).props("dense outlined").classes("bb-toolbar-grow").style("min-width: 260px")
                    model = ui.input(
                        "Model", value=str(getattr(llm_cfg, "api_model", "") or ""),
                    ).props("dense outlined").classes("w-64")
                key = ui.input(
                    "API key",
                    password=True,
                    password_toggle_button=True,
                    placeholder=(
                        "A key is stored — leave empty to keep it" if key_stored
                        else "Paste the provider's API key"
                    ),
                ).props("dense outlined autocomplete=off").classes("w-full")
                ui.label(
                    "The key is kept in your system's credential store (like your mailbox "
                    "passwords), never in the settings file, and is never shown again."
                    + (" A key is stored now." if key_stored else "")
                ).style("font-size: 11.5px; color: var(--text-tertiary)")
                ai_inputs["fallback"] = ui.switch(
                    "If the provider fails (limit reached, offline), use the local server",
                    value=bool(getattr(llm_cfg, "fallback_to_local", True)),
                )
                with ui.row().classes("items-center").style("gap: 8px"):
                    test_btn = ui.button("Test connection", icon="network_check").props(
                        "outline dense no-caps"
                    )
                    test_out = ui.label("").style("font-size: 12px")

            def _preset_changed(e: Any) -> None:
                chosen = PRESETS.get(str(e.value or ""))
                if chosen is None or str(e.value) == "custom":
                    return
                base.set_value(chosen.base_url)
                if chosen.model:
                    model.set_value(chosen.model)

            preset.on_value_change(_preset_changed)
            api_box.bind_visibility_from(ai_inputs["primary"], "value", value="api")
            ai_inputs.update(preset=preset, base=base, model=model, key=key)

            async def _test() -> None:
                from ...llm.bridge import LMStudioBridge
                from ...llm.providers import validate_base_url

                try:
                    url = validate_base_url(str(base.value or ""))
                except ValueError as e:
                    test_out.set_text(f"✗ {e}")
                    return
                k = str(key.value or "").strip() or resolve_api_key(llm_cfg)
                if not k:
                    test_out.set_text("✗ Enter an API key first.")
                    return
                chosen = PRESETS.get(str(preset.value or ""))
                probe = LMStudioBridge(
                    host="127.0.0.1:9", model_id="-", embed_id="-", timeout=40.0,
                    cloud_base_url=url, cloud_model=str(model.value or "").strip(),
                    cloud_key=k, cloud_extra_body=dict(chosen.extra_body) if chosen else {},
                    primary="api", fallback_to_local=False,
                )
                test_out.set_text("Testing…")
                test_btn.disable()
                try:
                    answer = await probe.chat(
                        [{"role": "user", "content": "Reply with the single word OK."}],
                        temperature=0.0,
                    )
                finally:
                    test_btn.enable()
                if answer:
                    test_out.set_text(f"✓ {probe.cloud_model} answered.")
                else:
                    test_out.set_text(
                        "✗ No answer. Check the URL, model name and key (details in the log)."
                    )

            test_btn.on_click(_test)

    # --- Translation -----------------------------------------------------------
    tr_cfg = getattr(settings, "translation", None)
    translation_inputs: dict[str, Any] = {}
    if tr_cfg is not None:
        with ui.card().classes("bb-card w-full q-mt-md"):
            with ui.row().classes("items-center").style("gap: 8px"):
                ui.icon("translate", size="20px").style("color: var(--text-link)")
                ui.label("Translation").classes("text-subtitle1 text-weight-bold").style(
                    "color: var(--text-link)"
                )
            ui.label(
                "Incoming mail that is not in English is translated by your AI model (above) "
                "and the English text is shown under the original. The translation is for "
                "you only: it is never given to "
                "triage, drafting, knowledge search or OpenClaw agents, it is shown as plain "
                "text with links disabled, and it is deleted with the message. Quarantined "
                "mail is never translated."
            ).style("font-size: 12px; color: var(--text-secondary)")
            translation_inputs["enabled"] = ui.switch(
                "Automatically translate incoming mail that is not in English",
                value=bool(getattr(tr_cfg, "enabled", True)),
            )
            translation_inputs["include_held"] = ui.switch(
                "Also translate mail held for review (potential spam / potential issue)",
                value=bool(getattr(tr_cfg, "include_held", True)),
            )
            translation_inputs["local_only"] = ui.switch(
                "Translate on the local server only (never send mail text to an API provider "
                "for translation)",
                value=bool(getattr(tr_cfg, "local_only", False)),
            )
            ui.label(
                "Held mail stays withheld from every AI that can act. The translator cannot "
                "act — no tools, and only you read its output — but it is still a model "
                "reading suspicious text; turn this off to keep held mail away from all AI."
            ).style("font-size: 11.5px; color: var(--text-tertiary)")
            with ui.row().classes("items-center").style("gap: 12px"):
                translation_inputs["backfill_days"] = ui.number(
                    "Translate mail from the last … days", value=int(tr_cfg.backfill_days),
                    min=0, max=365, format="%d",
                ).props("dense outlined").classes("w-56")
                translation_inputs["max_chars"] = ui.number(
                    "Longest text translated (characters)", value=int(tr_cfg.max_chars),
                    min=500, max=20000, format="%d",
                ).props("dense outlined").classes("w-56")
            ui.label(
                "Changes apply at once after Save. Older mail can be translated from the "
                "message itself (Translate button)."
            ).style("font-size: 11.5px; color: var(--text-tertiary)")

    # --- Security & quarantine (read-only) ------------------------------------
    with ui.card().classes("bb-card w-full q-mt-md"):
        ui.label("Security & quarantine").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--text-link)"
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
        with ui.row().classes("w-full bb-toolbar").style("gap: 12px"):
            with ui.element("div").classes("bb-card q-pa-sm").style("min-width: 200px; flex: 1"):
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
            with ui.element("div").classes("bb-card q-pa-sm").style("min-width: 200px; flex: 1"):
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
            with ui.element("div").classes("bb-card q-pa-sm").style("min-width: 200px; flex: 1"):
                ui.label("Currently quarantined").style("font-weight: 800")
                ui.label(
                    str(quarantined_now) if quarantined_now is not None else "unavailable"
                ).style("font-size: 20px; font-weight: 800; color: var(--text-link)")

    # --- Deleting mail --------------------------------------------------------
    delete_inputs: dict[str, Any] = {}
    with ui.card().classes("bb-card w-full q-mt-md"):
        ui.label("Deleting mail").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--text-link)"
        )
        ui.label(
            "Deleting a message always removes it from this app. These settings decide "
            "what happens to the copy on your mail server, and how long deleted mail is "
            "kept first. Spam and scam mail is never held — it is deleted on the next "
            "sweep."
        ).style("font-size: 12px; color: var(--text-secondary)")
        with ui.row().classes("w-full bb-toolbar").style("gap: 12px"):
            delete_inputs["mode"] = ui.select(
                {
                    "trash": "Move to the provider's Trash folder (recoverable there)",
                    "expunge": "Delete from the mail server permanently",
                    "off": "Never touch the mail server",
                },
                label="On the mail server",
                value=str(getattr(sec, "server_delete_mode", "trash") if sec else "trash"),
            ).props("dense outlined options-dense").classes("w-96 bb-toolbar-grow")
            delete_inputs["days"] = ui.number(
                "Hold deleted mail for (days)",
                value=int(getattr(sec, "trash_retention_days", 60) if sec else 60),
                min=0,
                max=3650,
                format="%d",
            ).props("dense outlined").classes("w-56")
        try:
            from ...mail.retention import retention_days as _days

            held = len(store.trash_queue(retention_days=_days(settings)))  # type: ignore[attr-defined]
            ui.label(
                f"{held} message(s) are currently waiting to be deleted — see Spam & Deletion."
            ).style("font-size: 12px; color: var(--text-tertiary)")
        except Exception as e:  # noqa: BLE001
            log.debug("trash queue count unavailable: %s", e)

    # --- Sites (read-only) -----------------------------------------------------
    with ui.card().classes("bb-card w-full q-mt-md"):
        ui.label("Sites").classes("text-subtitle1 text-weight-bold").style(
            "color: var(--text-link)"
        )
        ui.label(
            "New sites are added via a [sites.<id>] entry in config.toml, then appear in the "
            "'Add mailbox' site list above. Mailboxes can also be added with "
            "'mailforge account-add'."
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
                acct.signature = str(row["sig"].value or "").strip()
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
                mode = str(delete_inputs["mode"].value or "trash")
                if mode not in {"off", "trash", "expunge"}:
                    raise ValueError(f"unknown provider delete mode: {mode}")
                sec.server_delete_mode = mode  # type: ignore[assignment]
                sec.trash_retention_days = max(0, int(delete_inputs["days"].value or 0))

            if llm_cfg is not None and ai_inputs:
                from ...llm.providers import (
                    PRESETS as _P,
                    resolve_api_key as _key,
                    store_api_key,
                    validate_base_url,
                )

                primary = str(ai_inputs["primary"].value or "local")
                chosen_id = str(ai_inputs["preset"].value or "custom")
                url = str(ai_inputs["base"].value or "").strip()
                name = str(ai_inputs["model"].value or "").strip()
                new_key = str(ai_inputs["key"].value or "").strip()
                if primary == "api" or url:
                    url = validate_base_url(url)
                if primary == "api" and not name:
                    raise ValueError("enter the API model name")
                if new_key:
                    store_api_key(new_key)
                    ai_inputs["key"].set_value("")
                if primary == "api" and not (new_key or _key(llm_cfg)):
                    raise ValueError("enter the provider's API key")
                if chosen_id != str(getattr(llm_cfg, "api_preset", "") or ""):
                    preset_obj = _P.get(chosen_id)
                    llm_cfg.api_extra_body = dict(preset_obj.extra_body) if preset_obj else {}
                llm_cfg.primary = primary  # type: ignore[assignment]
                llm_cfg.api_preset = chosen_id
                llm_cfg.api_base_url = url
                llm_cfg.api_model = name
                llm_cfg.fallback_to_local = bool(ai_inputs["fallback"].value)
                configure = getattr(bridge, "configure_cloud", None)
                if callable(configure):
                    # Applied live: the next mail uses the new model, no restart.
                    configure(
                        base_url=url, model=name,
                        key=_key(llm_cfg) if primary == "api" else "",
                        extra_body=llm_cfg.api_extra_body, primary=primary,
                        fallback_to_local=llm_cfg.fallback_to_local,
                    )

            if tr_cfg is not None and translation_inputs:
                tr_cfg.enabled = bool(translation_inputs["enabled"].value)
                tr_cfg.include_held = bool(translation_inputs["include_held"].value)
                tr_cfg.local_only = bool(translation_inputs["local_only"].value)
                tr_cfg.backfill_days = min(
                    365, max(0, int(translation_inputs["backfill_days"].value or 0))
                )
                tr_cfg.max_chars = min(
                    20000, max(500, int(translation_inputs["max_chars"].value or 4000))
                )

            # Re-assert invariants before persisting (spec §0).
            if hasattr(settings, "assert_invariants"):
                settings.assert_invariants()  # type: ignore[attr-defined]
            path = settings.save()  # type: ignore[attr-defined]
            ui.notify(
                f"Saved to {path}. Restart MailForge to apply mailbox, site, SMTP, "
                "or model changes to active listeners.",
                type="positive",
                timeout=10000,
            )
        except Exception as e:
            log.exception("settings save failed")
            ui.notify(f"Save failed: {e}", type="negative")

    ui.button("Save settings", icon="save", on_click=_save).props("color=primary").classes("q-mt-md")
