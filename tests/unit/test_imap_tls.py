"""Every IMAP connection must verify the server certificate and hostname."""

from __future__ import annotations

import ssl
import types

import pytest

from mailforge.mail import imap_listener


def test_tls_context_verifies_certificates_and_hostnames():
    ctx = imap_listener.tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_open_mailbox_passes_a_verifying_context(monkeypatch):
    seen = {}

    class FakeIMAP4SSL:
        def __init__(self, host, port, *args, **kwargs):
            seen["ssl_context"] = kwargs.get("ssl_context")
            raise ConnectionRefusedError("stop before any network I/O")

    import imaplib

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP4SSL)
    import imap_tools.mailbox as mbmod

    monkeypatch.setattr(mbmod.imaplib, "IMAP4_SSL", FakeIMAP4SSL)
    with pytest.raises(ConnectionRefusedError):
        imap_listener.open_mailbox("imap.example.net", 993)
    ctx = seen["ssl_context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_no_module_builds_an_unverified_mailbox():
    import pathlib
    import re

    src = pathlib.Path(imap_listener.__file__).resolve().parents[1]
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\bMailBox\(", text):
            line = text[: m.start()].count("\n") + 1
            context = text[m.start() : m.start() + 200]
            if "ssl_context" not in context:
                offenders.append(f"{path.name}:{line}")
    assert offenders == []


def test_capabilities_are_refreshed_after_login():
    class Client:
        capabilities = ("IMAP4REV1", "STARTTLS", "AUTH=PLAIN")

        def capability(self):
            return "OK", [b"IMAP4rev1 IDLE MOVE UIDPLUS"]

    mailbox = types.SimpleNamespace(client=Client())
    caps = imap_listener.refresh_capabilities(mailbox)
    assert "MOVE" in caps and "UIDPLUS" in caps
    assert "MOVE" in mailbox.client.capabilities


def test_capability_refresh_failure_keeps_the_greeting_list():
    class Client:
        capabilities = ("IMAP4REV1",)

        def capability(self):
            raise OSError("socket closed")

    mailbox = types.SimpleNamespace(client=Client())
    assert imap_listener.refresh_capabilities(mailbox) == ("IMAP4REV1",)
