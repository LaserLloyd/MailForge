"""Mailbox add/remove — the single code path that creates an inbox.

Adding a mailbox has to touch three stores that must agree, or the listener
silently does nothing:

  * ``config.toml`` — the ``[[imap_accounts]]`` entry :func:`runtime.run_serve`
    iterates over to start listeners.
  * the OS keyring — ``emailforge.imap.<name>`` for the listener AND
    ``emailforge.smtp.<username>`` for the send path. The two are keyed
    differently on purpose (see ``secrets.py``); writing only one produces a
    mailbox that receives but cannot reply.
  * the ``accounts`` DB row — what the Settings "Inbox Coverage" panel reads.
    :class:`~emailforge.mail.imap_listener.IMAPListener` upserts this itself
    at startup, so the write here only makes the new inbox visible in the UI
    before the service restarts; failing it is not fatal.

Every caller (CLI, Settings page, external credential tooling) funnels through
:func:`add_account` so that three-way write and its validation stay identical.

Secrets are written only after every validation passes, and are rolled back if
the config write fails — a failed add never leaves a credential behind
(invariant §0.5: credentials never land anywhere but the keyring).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from . import secrets as secrets_mod
from .config import IMAPAccount, SMTPAccount

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

# The account label becomes a keyring service name and a DB primary key, so it
# stays deliberately boring: no spaces, slashes, or leading punctuation.
_ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# Deliberately permissive — this catches typos ("sam@", "sam"), not RFC 5322
# exotica. The IMAP server is the real authority on whether the mailbox exists.
_USERNAME_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

AUTH_METHODS = ("password", "xoauth2")


def _smtp_guess(imap_host: str) -> str:
    """Best-effort SMTP host for the FIRST mailbox, from its IMAP host.

    Most providers mirror ``imap.<domain>`` / ``smtp.<domain>``. This only
    seeds the global relay when none exists yet; it is editable in Settings and
    can always be overridden per account.
    """
    host = (imap_host or "").strip().lower()
    if host.startswith("imap."):
        return "smtp." + host[len("imap."):]
    return host


class AccountError(ValueError):
    """A mailbox could not be added/removed. Message is safe to show a user."""


def _norm_name(name: str) -> str:
    value = (name or "").strip().lower()
    if not value:
        raise AccountError("Account label is required.")
    if not _ACCOUNT_NAME_RE.match(value):
        raise AccountError(
            "Account label must be lowercase letters/digits, optionally with "
            "'.', '-' or '_' (e.g. 'support')."
        )
    return value


def _norm_username(username: str) -> str:
    value = (username or "").strip()
    if not value:
        raise AccountError("Email address is required.")
    if not _USERNAME_RE.match(value):
        raise AccountError(f"'{value}' does not look like an email address.")
    return value


def _norm_port(port: object, label: str) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise AccountError(f"{label} must be a number.") from None
    if not 1 <= value <= 65535:
        raise AccountError(f"{label} must be between 1 and 65535.")
    return value


def validate_new_account(
    settings: "Settings",
    *,
    name: str,
    username: str,
    site_id: str,
    imap_host: str,
    imap_port: object = 993,
    auth_method: str = "password",
) -> dict:
    """Normalise and check a prospective mailbox against the live config.

    Pure — touches no keyring, no disk. Split out from :func:`add_account` so a
    UI can validate a form (or a "Test connection" button) without committing.
    Raises :class:`AccountError` with a user-facing message on the first problem.
    """
    clean_name = _norm_name(name)
    clean_username = _norm_username(username)

    for existing in settings.imap_accounts:
        if existing.name.lower() == clean_name:
            raise AccountError(f"Account label '{clean_name}' already exists.")
        if existing.username.lower() == clean_username.lower():
            raise AccountError(f"Mailbox '{clean_username}' is already configured.")

    site = str(site_id or "").strip().lower()
    if site not in settings.sites:
        known = ", ".join(settings.sites) or "(none configured)"
        raise AccountError(f"Unknown site '{site}'. Configured sites: {known}.")

    host = (imap_host or "").strip()
    if not host:
        raise AccountError("IMAP host is required.")

    method = (auth_method or "password").strip().lower()
    if method not in AUTH_METHODS:
        raise AccountError(f"Auth method must be one of: {', '.join(AUTH_METHODS)}.")

    return {
        "name": clean_name,
        "username": clean_username,
        "site_id": site,
        "imap_host": host,
        "imap_port": _norm_port(imap_port, "IMAP port"),
        "auth_method": method,
    }


def test_imap_login(
    host: str,
    port: object,
    username: str,
    secret: str,
    auth_method: str = "password",
) -> None:
    """Prove the credential actually logs in. Raises :class:`AccountError`.

    Mirrors ``imap_listener._login`` exactly (same client, same auth calls) so a
    pass here means the listener will connect too — not merely that the host
    resolves.
    """
    from imap_tools import MailBox

    if not secret:
        raise AccountError("No password/token to test.")
    checked_port = _norm_port(port, "IMAP port")
    try:
        with MailBox((host or "").strip(), checked_port, timeout=20) as mailbox:
            if (auth_method or "password").strip().lower() == "xoauth2":
                mailbox.xoauth2(username, secret)
            else:
                mailbox.login(username, secret)
    except Exception as e:  # noqa: BLE001 - imap_tools/imaplib/ssl/socket all land here
        # Server errors arrive as b'[AUTHENTICATIONFAILED] ...'; keep it short and
        # never echo the credential back.
        detail = str(e).strip().strip("[]") or e.__class__.__name__
        raise AccountError(f"IMAP login failed: {detail[:160]}") from None


def add_account(
    settings: "Settings",
    *,
    name: str,
    username: str,
    site_id: str,
    imap_host: str,
    imap_secret: str,
    imap_port: object = 993,
    auth_method: str = "password",
    smtp_secret: str | None = None,
    smtp_host: str = "",
    smtp_port: object = 587,
    smtp_starttls: bool = True,
) -> IMAPAccount:
    """Add one mailbox: keyring + config.toml + DB row, or nothing at all.

    ``smtp_secret`` defaults to ``imap_secret`` (the usual single-password
    mailbox). ``smtp_host`` empty => this mailbox sends through the global
    ``[smtp]`` relay under its own username (see ``Settings.smtp_for_account``).

    Mutates and saves ``settings``. Returns the created account.
    """
    from .db.store import open_store

    fields = validate_new_account(
        settings,
        name=name,
        username=username,
        site_id=site_id,
        imap_host=imap_host,
        imap_port=imap_port,
        auth_method=auth_method,
    )
    if not imap_secret:
        raise AccountError("IMAP password or OAuth2 token is required.")

    override_host = (smtp_host or "").strip()
    account = IMAPAccount(
        name=fields["name"],
        site_id=fields["site_id"],
        host=fields["imap_host"],
        port=fields["imap_port"],
        username=fields["username"],
        auth_method=fields["auth_method"],
        smtp_host=override_host,
        smtp_port=_norm_port(smtp_port, "SMTP port"),
        smtp_starttls=bool(smtp_starttls),
    )

    # First mailbox ever? Seed the global relay so the send path has an identity.
    created_relay = settings.smtp is None
    if created_relay:
        settings.smtp = SMTPAccount(
            host=override_host or _smtp_guess(fields["imap_host"]),
            port=_norm_port(smtp_port, "SMTP port"),
            username=fields["username"],
            starttls=bool(smtp_starttls),
        )

    settings.imap_accounts.append(account)
    try:
        settings.assert_invariants()
    except Exception as e:
        settings.imap_accounts.remove(account)
        if created_relay:
            settings.smtp = None
        raise AccountError(str(e)) from None

    # Snapshot before writing so a failed config save restores the exact prior
    # keyring state instead of leaving a live credential for a phantom account.
    prior_imap = secrets_mod.get_secret("imap", fields["name"])
    prior_smtp = secrets_mod.get_secret("smtp", fields["username"])
    try:
        secrets_mod.set_imap_secret(fields["name"], imap_secret)
        secrets_mod.set_smtp_secret(fields["username"], smtp_secret or imap_secret)
        path = settings.save()
    except Exception as e:
        settings.imap_accounts.remove(account)
        if created_relay:
            settings.smtp = None
        _restore_secret("imap", fields["name"], prior_imap)
        _restore_secret("smtp", fields["username"], prior_smtp)
        raise AccountError(f"Could not save mailbox: {e}") from None

    log.info("Added mailbox %s (%s) to %s", fields["username"], fields["name"], path)

    # Convenience only — IMAPListener upserts this row itself on startup, so a
    # failure here costs a UI label until restart, not the mailbox.
    try:
        with open_store(settings.resolved_db_path()) as store:
            store.upsert_account(
                fields["name"],
                "imap",
                fields["imap_host"],
                fields["imap_port"],
                fields["username"],
                fields["site_id"],
            )
    except Exception as e:  # noqa: BLE001
        log.warning("Mailbox saved, but the DB account row was not written: %s", e)

    return account


def remove_account(
    settings: "Settings", name: str, *, purge_secrets: bool = True
) -> IMAPAccount:
    """Remove a mailbox from config.toml and (by default) forget its secrets.

    Message history is deliberately KEPT: the ``accounts``/``messages`` rows stay
    so the audit trail and past threads survive re-adding the same mailbox.
    """
    target = (name or "").strip().lower()
    account = next(
        (a for a in settings.imap_accounts if a.name.lower() == target), None
    )
    if account is None:
        raise AccountError(f"No mailbox named '{name}' is configured.")

    settings.imap_accounts.remove(account)
    try:
        settings.assert_invariants()
        settings.save()
    except Exception as e:
        settings.imap_accounts.append(account)
        raise AccountError(f"Could not remove mailbox: {e}") from None

    if purge_secrets:
        secrets_mod.delete_secret("imap", account.name)
        # Only if no surviving mailbox still sends as this identity.
        if not any(
            a.username.lower() == account.username.lower() for a in settings.imap_accounts
        ):
            secrets_mod.delete_secret("smtp", account.username)
    log.info("Removed mailbox %s (%s); message history retained.", account.username, account.name)
    return account


def _restore_secret(kind: str, key: str, prior: object) -> None:
    """Put a keyring entry back the way it was (absent stays absent)."""
    try:
        if prior is None:
            secrets_mod.delete_secret(kind, key)
        else:
            secrets_mod.set_secret(kind, key, prior)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001 - rollback is best-effort
        log.warning("Could not roll back %s secret for %s: %s", kind, key, e)
