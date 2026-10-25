"""A keyring read that blocks (locked Secret Service waiting on an unlock
prompt at boot) must not wedge a listener thread forever."""
import threading
import time

import keyring

from mailforge import secrets


def test_hung_keyring_read_times_out_then_recovers(monkeypatch):
    monkeypatch.setattr(secrets, "_ensure_backend", lambda: None)
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
    got = secrets.get_imap_secret("acct")
    assert got is not None and got.get_secret_value() == "pw"
    release.set()
