"""Layered secret storage (build spec §0.5, §8).

Credentials never hit disk in plaintext. Primary backend is the OS keyring
(``keyring``); on headless Linux without a Secret Service, we fall back to
``keyrings.cryptfile`` (an encrypted file keyring). In-memory secrets are
always wrapped in ``pydantic.SecretStr`` so they never leak via repr/logs.

Service-name convention (spec §8):
    bluebox.imap.{account_name}
    bluebox.smtp.{account_name}
"""

from __future__ import annotations

import logging

import keyring
from keyring.errors import KeyringError
from pydantic import SecretStr

log = logging.getLogger(__name__)

_SERVICE_ROOT = "bluebox"
_fallback_configured = False


def _ensure_backend() -> None:
    """Probe the active keyring; install an encrypted-file fallback if the
    platform backend is unusable (common on headless Linux)."""
    global _fallback_configured
    if _fallback_configured:
        return
    backend = keyring.get_keyring()
    name = backend.__class__.__name__
    if "fail" in name.lower():
        try:
            from keyrings.cryptfile.cryptfile import CryptFileKeyring

            kr = CryptFileKeyring()
            keyring.set_keyring(kr)
            log.info("Using encrypted cryptfile keyring fallback.")
        except Exception as e:  # pragma: no cover - environment dependent
            log.warning("No usable keyring backend and cryptfile unavailable: %s", e)
    _fallback_configured = True


def _service(kind: str, account: str) -> str:
    return f"{_SERVICE_ROOT}.{kind}.{account}"


def set_secret(kind: str, account: str, secret: str | SecretStr) -> None:
    _ensure_backend()
    value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
    keyring.set_password(_service(kind, account), account, value)


def get_secret(kind: str, account: str) -> SecretStr | None:
    _ensure_backend()
    try:
        v = keyring.get_password(_service(kind, account), account)
    except KeyringError as e:  # pragma: no cover - environment dependent
        log.error("Keyring read failed for %s/%s: %s", kind, account, e)
        return None
    return SecretStr(v) if v is not None else None


def delete_secret(kind: str, account: str) -> None:
    _ensure_backend()
    try:
        keyring.delete_password(_service(kind, account), account)
    except KeyringError:
        pass


# Convenience wrappers used by mail/* and cli setup-wizard.
def set_imap_secret(account: str, secret: str) -> None:
    set_secret("imap", account, secret)


def get_imap_secret(account: str) -> SecretStr | None:
    return get_secret("imap", account)


def set_smtp_secret(account: str, secret: str) -> None:
    set_secret("smtp", account, secret)


def get_smtp_secret(account: str) -> SecretStr | None:
    return get_secret("smtp", account)
