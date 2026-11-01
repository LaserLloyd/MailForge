"""Layered secret storage (build spec §0.5, §8).

Credentials never hit disk in plaintext. Primary backend is the OS keyring
(``keyring``); on headless Linux without a Secret Service, we fall back to
``keyrings.cryptfile`` (an encrypted file keyring). In-memory secrets are
always wrapped in ``pydantic.SecretStr`` so they never leak via repr/logs.

Service-name convention (spec §8):
    mailforge.imap.{account_name}
    mailforge.smtp.{account_name}
"""

from __future__ import annotations

import logging
import os
import threading

import keyring
from keyring.errors import KeyringError
from pydantic import SecretStr

log = logging.getLogger(__name__)

_SERVICE_ROOT = "mailforge"
_fallback_configured = False

# A locked Secret Service (e.g. at boot before the login keyring is unlocked)
# makes keyring calls block forever on an unlock prompt. Bound the wait so
# callers (IMAP listener threads, CLI/UI) fail, back off and retry instead of
# wedging for good. Default 30 s; override with MAILFORGE_KEYRING_TIMEOUT_S.
KEYRING_READ_TIMEOUT_S = 30.0
_TIMEOUT_ENV = "MAILFORGE_KEYRING_TIMEOUT_S"
_bad_env_warned = False


def _timeout_s() -> float:
    """Effective timeout: env override if valid, else KEYRING_READ_TIMEOUT_S."""
    global _bad_env_warned
    raw = os.environ.get(_TIMEOUT_ENV)
    if raw is None or raw == "":
        return KEYRING_READ_TIMEOUT_S
    try:
        v = float(raw)
        if v > 0 and v == v and v != float("inf"):
            return v
    except ValueError:
        pass
    if not _bad_env_warned:
        _bad_env_warned = True
        log.warning("Ignoring invalid %s=%r; using %.0fs", _TIMEOUT_ENV, raw, KEYRING_READ_TIMEOUT_S)
    return KEYRING_READ_TIMEOUT_S


class KeyringTimeout(KeyringError):
    """The credential store did not answer within the timeout."""


class _Call:
    __slots__ = ("done", "value", "exc")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: object = None
        self.exc: BaseException | None = None


# One in-flight call per (op, service, account): a locked keyring leaves the
# worker blocked, and retries must not pile up more threads on the backend.
_inflight: dict[tuple[str, str, str], _Call] = {}
_inflight_lock = threading.Lock()
_warned: set[tuple[str, str]] = set()


def _run_bounded(op: str, service: str, account: str, fn, *, join: bool):
    """Run ``fn`` on a daemon thread, wait up to the timeout.

    If a call for the same key is already in flight: ``join`` waits on it
    (reads); otherwise (writes, whose value may differ) raise KeyringTimeout.
    """
    timeout = _timeout_s()
    key = (op, service, account)
    with _inflight_lock:
        call = _inflight.get(key)
        if call is None:
            call = _Call()
            _inflight[key] = call

            def _work(c: _Call = call) -> None:
                try:
                    c.value = fn()
                except BaseException as e:  # noqa: BLE001 — re-raised in the caller
                    c.exc = e
                finally:
                    with _inflight_lock:
                        if _inflight.get(key) is c:
                            del _inflight[key]
                    c.done.set()

            threading.Thread(target=_work, name="keyring-" + op, daemon=True).start()
        elif not join:
            raise KeyringTimeout(f"credential store did not answer within {timeout:g} s")
    if not call.done.wait(timeout):
        raise KeyringTimeout(f"credential store did not answer within {timeout:g} s")
    if call.exc is not None:
        raise call.exc
    return call.value


def _get_password_bounded(service: str, account: str) -> str | None:
    try:
        return _run_bounded(
            "get", service, account, lambda: keyring.get_password(service, account), join=True
        )  # type: ignore[return-value]
    except ValueError as e:
        if _is_mac_failure(e):
            raise WrongKeyringPassword(WRONG_PASSWORD_MSG) from None
        raise


class WrongKeyringPassword(KeyringError):
    """The encrypted credential file rejected the supplied password."""


WRONG_PASSWORD_MSG = "wrong password for the credential store"


def _is_mac_failure(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and "MAC check failed" in str(exc)


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
    service = _service(kind, account)
    try:
        _run_bounded(
            "set", service, account, lambda: keyring.set_password(service, account, value), join=False
        )
    except ValueError as e:
        if _is_mac_failure(e):
            raise WrongKeyringPassword(WRONG_PASSWORD_MSG) from None
        raise


def get_secret(kind: str, account: str) -> SecretStr | None:
    _ensure_backend()
    service = _service(kind, account)
    wkey = (service, account)
    try:
        v = _get_password_bounded(service, account)
    except KeyringTimeout as e:
        if wkey not in _warned:
            _warned.add(wkey)
            log.warning(
                "Credential store locked or unresponsive for %s/%s (%s); "
                "will keep retrying quietly",
                kind, account, e,
            )
        return None
    except KeyringError as e:  # pragma: no cover - environment dependent
        log.error("Keyring read failed for %s/%s: %s", kind, account, e)
        return None
    if wkey in _warned:
        _warned.discard(wkey)
        log.info("Credential store reachable again for %s/%s", kind, account)
    return SecretStr(v) if v is not None else None


def delete_secret(kind: str, account: str) -> None:
    _ensure_backend()
    service = _service(kind, account)
    try:
        _run_bounded(
            "delete", service, account, lambda: keyring.delete_password(service, account), join=False
        )
    except KeyringTimeout:
        raise
    except KeyringError:
        pass
    except ValueError as e:
        if not _is_mac_failure(e):
            raise
        raise WrongKeyringPassword(WRONG_PASSWORD_MSG) from None


# Convenience wrappers used by mail/* and cli setup-wizard.
def set_imap_secret(account: str, secret: str) -> None:
    set_secret("imap", account, secret)


def get_imap_secret(account: str) -> SecretStr | None:
    return get_secret("imap", account)


def set_smtp_secret(account: str, secret: str) -> None:
    set_secret("smtp", account, secret)


def get_smtp_secret(account: str) -> SecretStr | None:
    return get_secret("smtp", account)
