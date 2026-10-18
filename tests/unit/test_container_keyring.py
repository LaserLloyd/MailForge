"""deploy/container_keyring.py: the container's headless credential-store unlock.

Covers the password source (entrypoint env, or the raw variables when a process is
started with `docker compose exec`, which skips the entrypoint) and the failure modes:
no password without a terminal, and a wrong password.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from keyring.errors import KeyringError, KeyringLocked

MODULE = Path(__file__).resolve().parents[2] / "deploy" / "container_keyring.py"
VARS = ("MAILFORGE_KEYRING_KEY", "MAILFORGE_KEYRING_PASSWORD", "MAILFORGE_KEYRING_PASSWORD_FILE")


def _module():
    spec = importlib.util.spec_from_file_location("container_keyring_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ck(monkeypatch):
    for name in VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    return _module()


def _store(ck, path: Path):
    kr = ck.EnvCryptFileKeyring()
    kr.file_path = str(path)
    kr.time_cost, kr.memory_cost = 1, 1024  # keep Argon2 cheap in tests
    return kr


def test_password_sources_follow_the_entrypoint_precedence(ck, monkeypatch, tmp_path):
    assert ck.password_from_env() == ""
    monkeypatch.setenv("MAILFORGE_KEYRING_PASSWORD", "plain-pw")
    assert ck.password_from_env() == "plain-pw"
    secret = tmp_path / "kp"
    secret.write_text("file-pw\n\n", encoding="utf-8")
    monkeypatch.setenv("MAILFORGE_KEYRING_PASSWORD_FILE", str(secret))
    assert ck.password_from_env() == "file-pw"  # a readable _FILE wins, like `$(cat)`
    monkeypatch.setenv("MAILFORGE_KEYRING_PASSWORD_FILE", str(tmp_path / "missing"))
    assert ck.password_from_env() == "plain-pw"  # unreadable: the plain variable
    monkeypatch.setenv("MAILFORGE_KEYRING_KEY", "entrypoint-pw")
    assert ck.password_from_env() == "entrypoint-pw"  # what the entrypoint exported


def test_an_exec_process_without_the_entrypoint_still_unlocks(ck, monkeypatch, tmp_path):
    store = tmp_path / "cryptfile_pass.cfg"
    monkeypatch.setenv("MAILFORGE_KEYRING_KEY", "s3cret-store-pw")
    _store(ck, store).set_password("svc", "user", "imap-secret")

    # `docker compose exec`: only the compose variables, no MAILFORGE_KEYRING_KEY.
    monkeypatch.delenv("MAILFORGE_KEYRING_KEY")
    pw_file = tmp_path / "kp"
    pw_file.write_text("s3cret-store-pw\n", encoding="utf-8")
    monkeypatch.setenv("MAILFORGE_KEYRING_PASSWORD_FILE", str(pw_file))
    assert _store(ck, store).get_password("svc", "user") == "imap-secret"

    monkeypatch.delenv("MAILFORGE_KEYRING_PASSWORD_FILE")
    monkeypatch.setenv("MAILFORGE_KEYRING_PASSWORD", "s3cret-store-pw")
    assert _store(ck, store).get_password("svc", "user") == "imap-secret"


def test_no_password_and_no_terminal_is_locked_not_a_hang(ck, tmp_path):
    with pytest.raises(KeyringLocked, match="MAILFORGE_KEYRING_PASSWORD"):
        _store(ck, tmp_path / "cryptfile_pass.cfg").set_password("svc", "user", "x")


def test_a_wrong_password_is_a_plain_keyring_error(ck, monkeypatch, tmp_path):
    store = tmp_path / "cryptfile_pass.cfg"
    monkeypatch.setenv("MAILFORGE_KEYRING_KEY", "right-password-1")
    _store(ck, store).set_password("svc", "user", "imap-secret")

    monkeypatch.setenv("MAILFORGE_KEYRING_KEY", "wrong-password-2")
    kr = _store(ck, store)
    with pytest.raises(KeyringLocked) as info:
        kr.get_password("svc", "user")
    assert isinstance(info.value, KeyringError)  # secrets.get_secret degrades on this
    msg = str(info.value)
    assert "wrong password" in msg and "MAILFORGE_KEYRING_PASSWORD" in msg
    assert "wrong-password-2" not in msg and "right-password-1" not in msg
    assert kr._keyring_key is None  # nothing half-unlocked is left behind
