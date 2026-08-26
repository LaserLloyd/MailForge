"""SMTP send path — stdlib ``smtplib`` + ``email.message.EmailMessage`` (spec §1, §4).

SECURITY BOUNDARY — READ BEFORE IMPORTING.

    This module MUST NOT be importable by the LLM / agent layer as a tool.
    Per invariants §0.1 and §0.2, the LLM never gets ``send_email``. Sending is
    a privileged action invoked ONLY by the UI send path (``ui/``) AFTER an
    explicit human approval click, and only after the send path has asserted
    that ``security.autosend_allowed`` is False AND a human-approval record
    exists. There is NO auto-send anywhere; this module just transmits when its
    one function is called by trusted, human-gated code.

    Do not register :func:`send_email` in ``agent/tools.py`` or expose it to any
    planner/worker. Doing so is a defect.

Transport: STARTTLS on submission port; supports password and XOAUTH2 auth.
"""

from __future__ import annotations

import base64
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid

from pydantic import SecretStr

from ..config import SMTPAccount


@dataclass(frozen=True)
class SendResult:
    """What was actually put on the wire.

    ``message_id`` is the header the recipient's client will thread on, and
    ``raw`` is the exact RFC 5322 byte stream — the copy appended to the
    mailbox's IMAP Sent folder, so other clients show the same message this
    machine sent, byte for byte.
    """

    message_id: str
    raw: bytes


def _xoauth2_string(username: str, access_token: str) -> str:
    """Build the SASL XOAUTH2 initial-response token (base64)."""
    auth = f"user={username}\x01auth=Bearer {access_token}\x01\x01"
    return base64.b64encode(auth.encode("utf-8")).decode("ascii")


def _build_message(
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str | None,
    references: str | None,
    html_body: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    # Per RFC 5322, References should chain prior message-ids; include the parent.
    refs = references
    if in_reply_to and (not refs or in_reply_to not in refs):
        refs = f"{refs} {in_reply_to}".strip() if refs else in_reply_to
    if refs:
        msg["References"] = refs
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(
    smtp_cfg: SMTPAccount,
    secret: SecretStr,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    html_body: str | None = None,
) -> SendResult:
    """Send one email over SMTP submission with STARTTLS.

    ``secret`` is the account password (``auth_method`` password) or an OAuth2
    access token (XOAUTH2) — selection is by the SMTP account's username/auth
    convention. Standard password ``login`` is attempted first; on an auth
    error we fall back to SASL XOAUTH2 treating ``secret`` as an OAuth2 access
    token.

    Raises on any transport/auth failure — the UI surfaces the error. There is
    NO retry and NO queue here; this is a single human-approved transmission.

    Returns a :class:`SendResult` with the generated ``Message-ID`` and the raw
    bytes, so the caller can log what went out and append the same bytes to the
    account's IMAP Sent folder.
    """
    token = secret.get_secret_value()
    msg = _build_message(
        from_addr,
        to_addr,
        subject,
        body,
        in_reply_to,
        references,
        html_body,
    )

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=30) as server:
        server.ehlo()
        if smtp_cfg.starttls:
            server.starttls(context=context)
            server.ehlo()
        try:
            server.login(smtp_cfg.username, token)
        except smtplib.SMTPAuthenticationError:
            # Fall back to XOAUTH2 (token is an OAuth2 access token).
            server.docmd("AUTH", "XOAUTH2 " + _xoauth2_string(smtp_cfg.username, token))
        server.send_message(msg)
    return SendResult(message_id=str(msg["Message-ID"] or ""), raw=msg.as_bytes())
