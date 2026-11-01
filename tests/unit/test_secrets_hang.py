"""A keyring read that blocks (locked Secret Service waiting on an unlock
prompt at boot) must not wedge a listener thread forever."""
import logging
import threading
import time

import pytest

import keyring

from keyring.errors import KeyringError

from mailforge import secrets


def test_hung_keyring_read_times_out_then_recovers(monkeypatch):
    monkeypatch.setattr(secrets, "_ensure_backend", lambda: None)
    secrets._inflight.clear()
    secrets._warned.clear()
    monkeypatch.setattr(secrets, "KEYRING_READ_TIMEOUT_S", 0.3)
    release = threading.Event()
    calls = []

    def fake_get(service, user):
        calls.append(service)
        if len(calls) == 1:
            release.wait(30)  # simulates exec_prompt never returning
            return None
        return "pw"

    monkeypatch.setattr(keyring, "get_password", fake_get)
    t0 = time.monotonic()
    assert secrets.get_imap_secret("acct") is None  # abandoned, not hung
    assert time.monotonic() - t0 < 5
    release.set()  # the stuck read finishes; a later call must read afresh
    for _ in range(50):
        if not secrets._inflight:
            break
        time.sleep(0.05)
    got = secrets.get_imap_secret("acct")
    assert got is not None and got.get_secret_value() == "pw"


def _reset(monkeypatch):
    monkeypatch.setattr(secrets, "_ensure_backend", lambda: None)
    monkeypatch.setattr(secrets, "KEYRING_READ_TIMEOUT_S", 0.2)
    monkeypatch.delenv("MAILFORGE_KEYRING_TIMEOUT_S", raising=False)
    secrets._warned.clear()
    secrets._inflight.clear()


def test_single_inflight_reader_per_key(monkeypatch):
    _reset(monkeypatch)
    release = threading.Event()
    calls = []

    def fake_get(service, user):
        calls.append(1)
        release.wait(30)
        return "pw"

    monkeypatch.setattr(keyring, "get_password", fake_get)
    before = threading.active_count()
    assert secrets.get_imap_secret("acct") is None
    assert secrets.get_imap_secret("acct") is None
    assert len(calls) == 1
    assert threading.active_count() == before + 1
    release.set()
    got = None
    for _ in range(50):
        got = secrets.get_imap_secret("acct")
        if got is not None:
            break
        time.sleep(0.05)
    assert got is not None and got.get_secret_value() == "pw"


def test_timeout_warns_once_then_reachable_info(monkeypatch, caplog):
    _reset(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(keyring, "get_password", lambda s, u: (release.wait(30), "pw")[1])
    with caplog.at_level(logging.INFO, logger="mailforge.secrets"):
        for _ in range(3):
            assert secrets.get_imap_secret("acct") is None
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1 and "locked or unresponsive" in warns[0].getMessage()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        release.set()
        for _ in range(50):
            if secrets.get_imap_secret("acct") is not None:
                break
            time.sleep(0.05)
    assert any("reachable again" in r.getMessage() for r in caplog.records)
    assert ("mailforge.imap.acct", "acct") not in secrets._warned


def test_env_timeout_override_and_invalid(monkeypatch, caplog):
    _reset(monkeypatch)
    monkeypatch.setattr(secrets, "_bad_env_warned", False)
    monkeypatch.setenv("MAILFORGE_KEYRING_TIMEOUT_S", "2.5")
    assert secrets._timeout_s() == 2.5
    monkeypatch.setenv("MAILFORGE_KEYRING_TIMEOUT_S", "nope")
    with caplog.at_level(logging.WARNING, logger="mailforge.secrets"):
        assert secrets._timeout_s() == 0.2
        assert secrets._timeout_s() == 0.2
    assert len([r for r in caplog.records if "invalid" in r.getMessage()]) == 1


def test_set_and_delete_time_out_with_keyring_error(monkeypatch):
    _reset(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(keyring, "set_password", lambda s, u, v: release.wait(30))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: release.wait(30))
    with pytest.raises(KeyringError, match="did not answer within"):
        secrets.set_secret("imap", "a", "pw")
    with pytest.raises(KeyringError, match="did not answer within"):
        secrets.delete_secret("imap", "a")
    release.set()
