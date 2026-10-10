"""SMTP never sends credentials in the clear."""

from __future__ import annotations

import smtplib

import pytest
from pydantic import SecretStr

from mailforge.config import SMTPAccount
from mailforge.mail import smtp_sender


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.calls: list[str] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, token):
        self.calls.append("login")

    def send_message(self, msg):
        self.calls.append("send")


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)


def _send(cfg):
    return smtp_sender.send_email(
        cfg, SecretStr("pw"), "me@example.test", "you@example.test", "s", "b"
    )


def test_starttls_on_submission_port():
    _send(SMTPAccount(host="smtp.example.test", port=587, username="u", starttls=True))
    (conn,) = FakeSMTP.instances
    assert type(conn) is FakeSMTP
    assert conn.calls.index("starttls") < conn.calls.index("login")


def test_port_465_uses_implicit_tls_with_a_verifying_context():
    import ssl

    _send(SMTPAccount(host="smtp.example.test", port=465, username="u", starttls=True))
    (conn,) = FakeSMTP.instances
    assert type(conn) is FakeSMTPSSL
    assert conn.context.verify_mode == ssl.CERT_REQUIRED
    assert "starttls" not in conn.calls and "login" in conn.calls


def test_plaintext_auth_to_a_remote_host_is_refused():
    with pytest.raises(RuntimeError, match="plaintext"):
        _send(SMTPAccount(host="smtp.example.test", port=587, username="u", starttls=False))
    assert FakeSMTP.instances == []


def test_plaintext_to_a_loopback_relay_is_allowed():
    _send(SMTPAccount(host="127.0.0.1", port=1025, username="u", starttls=False))
    assert "login" in FakeSMTP.instances[0].calls
