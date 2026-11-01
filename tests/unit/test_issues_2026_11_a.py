"""Regression tests for issues #18, #4, #15, #2."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from keyring.errors import KeyringError

from mailforge import paths, secrets
from mailforge.ui import app as ui_app


# --- #18: IPv6 literals are bracketed in URLs ------------------------------ #
@pytest.mark.parametrize(
    ("host", "want"),
    [("::1", "[::1]"), ("[::1]", "[::1]"), ("127.0.0.1", "127.0.0.1"), ("mail.example.com", "mail.example.com")],
)
def test_url_host(host, want):
    assert paths.url_host(host) == want


def test_display_host_brackets_ipv6_and_maps_wildcards():
    assert ui_app._display_host("::1") == "[::1]"
    assert ui_app._display_host("::") == "127.0.0.1"
    assert ui_app._display_host("0.0.0.0") == "127.0.0.1"


# --- #4: denied pages answer 403 ------------------------------------------- #
def test_denied_sets_403(monkeypatch):
    client = SimpleNamespace(status_code=200)
    monkeypatch.setattr(ui_app, "context", SimpleNamespace(client=client))
    monkeypatch.setattr(ui_app.theme, "apply", lambda: None)

    class _El:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def classes(self, *a, **k):
            return self

        def style(self, *a, **k):
            return self

    labels = []

    def _label(text):
        labels.append(text)
        return _El()

    monkeypatch.setattr(
        ui_app, "ui", SimpleNamespace(column=lambda: _El(), icon=lambda *a, **k: _El(), label=_label)
    )
    ui_app._denied()
    assert client.status_code == 403
    assert "Access denied" in labels


# --- #15: malformed MAILFORGE_UI_PORT warns once --------------------------- #
def test_bad_port_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(paths, "_bad_port_warned", None)
    monkeypatch.setenv(paths.UI_PORT_ENV, "80x80")
    with caplog.at_level(logging.WARNING, logger="mailforge.paths"):
        assert paths.ui_port_override() is None
        assert paths.ui_port_override() is None
    msgs = [r.getMessage() for r in caplog.records if "MAILFORGE_UI_PORT" in r.getMessage()]
    assert len(msgs) == 1 and "80x80" in msgs[0]


def test_out_of_range_port_warns(monkeypatch, caplog):
    monkeypatch.setattr(paths, "_bad_port_warned", None)
    monkeypatch.setenv(paths.UI_PORT_ENV, "70000")
    with caplog.at_level(logging.WARNING, logger="mailforge.paths"):
        assert paths.ui_port_override() is None
    assert any("70000" in r.getMessage() for r in caplog.records)


def test_good_port_is_silent(monkeypatch, caplog):
    monkeypatch.setenv(paths.UI_PORT_ENV, "8123")
    with caplog.at_level(logging.WARNING, logger="mailforge.paths"):
        assert paths.ui_port_override() == 8123
    assert not caplog.records


# --- #2: wrong keyring password is a plain error --------------------------- #
def test_mac_failure_becomes_plain_error(monkeypatch):
    def boom(*a, **k):
        raise ValueError("MAC check failed")

    monkeypatch.setattr(secrets, "_ensure_backend", lambda: None)
    monkeypatch.setattr(secrets.keyring, "set_password", boom)
    with pytest.raises(secrets.WrongKeyringPassword) as ei:
        secrets.set_secret("imap", "acct", "pw")
    assert str(ei.value) == "wrong password for the credential store"
    assert isinstance(ei.value, KeyringError)
    assert ei.value.__cause__ is None

    monkeypatch.setattr(secrets.keyring, "get_password", boom)
    assert secrets.get_secret("imap", "acct") is None  # logged, not raised


def test_other_value_errors_pass_through(monkeypatch):
    def boom(*a, **k):
        raise ValueError("something else")

    monkeypatch.setattr(secrets, "_ensure_backend", lambda: None)
    monkeypatch.setattr(secrets.keyring, "set_password", boom)
    with pytest.raises(ValueError, match="something else"):
        secrets.set_secret("imap", "acct", "pw")
