"""A fixed allowlist of non-content header facts captured at ingest.

Screening rules need a few header signals (did the provider authenticate the
sender? is this bulk mail? where do replies go?), and the raw headers are not
stored. This module reduces a message's headers to a small, bounded dict of
facts — verdict tokens and domains only, never free text a sender could use
to smuggle instructions — which is passed to screening and persisted as JSON
in ``messages.header_meta`` so rules can be measured later.

Authentication-Results: only the FIRST (topmost) instance is trusted. It is
prepended by the receiving provider; anything below it arrived with the
message and may be forged by the sender.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+")
_ADDR_DOMAIN = re.compile(r"@([a-z0-9.-]+)")
_TOKEN = re.compile(r"[a-z0-9_.-]{1,40}")
_RESULT = re.compile(
    r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)\b([^;]*)", re.IGNORECASE
)
_PROP = re.compile(r"\b(header\.d|header\.i|header\.from|smtp\.mailfrom)\s*=\s*@?([^\s;()]+)", re.IGNORECASE)

#: Keys this module may emit; anything else is dropped on load.
FACT_KEYS = frozenset(
    {
        "auth_serv", "spf", "dkim", "dkim_pass_domains", "dmarc", "dmarc_from",
        "reply_to_domain", "return_path_domain", "list_unsubscribe", "list_id",
        "precedence", "github_reason",
    }
)


def _first(headers: Mapping[str, Any], name: str) -> str:
    value = headers.get(name) if headers else None
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return " ".join(str(value).split())


def _domain(value: str) -> str:
    value = str(value or "").strip().lower().strip("<>.")
    if "@" in value:
        value = value.rsplit("@", 1)[-1]
    match = _DOMAIN.fullmatch(value[:253])
    return match.group(0) if match else ""


def _address_domain(value: str) -> str:
    match = _ADDR_DOMAIN.search(str(value or "").lower())
    return _domain(match.group(1)) if match else ""


def _token(value: str) -> str:
    match = _TOKEN.match(str(value or "").strip().lower())
    return match.group(0) if match else ""


def parse_authentication_results(value: str) -> dict[str, Any]:
    """Verdicts from one Authentication-Results header value."""
    facts: dict[str, Any] = {}
    if not value:
        return facts
    serv = value.split(";", 1)[0].strip().split()[0] if value.strip() else ""
    serv_domain = _domain(serv)
    if serv_domain:
        facts["auth_serv"] = serv_domain
    dkim_pass: list[str] = []
    for method, result, rest in _RESULT.findall(value):
        method = method.lower()
        result = _token(result)
        props = {k.lower(): v for k, v in _PROP.findall(rest)}
        if method == "dkim":
            domain = _domain(props.get("header.d") or props.get("header.i") or "")
            if result == "pass" and domain and domain not in dkim_pass:
                dkim_pass.append(domain)
            # "pass" wins over any other dkim result (multiple signatures).
            if facts.get("dkim") != "pass":
                facts["dkim"] = result
        elif method == "dmarc" and "dmarc" not in facts:
            facts["dmarc"] = result
            dmarc_from = _domain(props.get("header.from") or "")
            if dmarc_from:
                facts["dmarc_from"] = dmarc_from
        elif method == "spf" and "spf" not in facts:
            facts["spf"] = result
    if dkim_pass:
        facts["dkim_pass_domains"] = dkim_pass[:4]
    return facts


def extract_header_facts(headers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Reduce lower-cased message headers to the allowlisted facts."""
    headers = headers or {}
    facts: dict[str, Any] = {}
    facts.update(parse_authentication_results(_first(headers, "authentication-results")))
    reply_to = _address_domain(_first(headers, "reply-to"))
    if reply_to:
        facts["reply_to_domain"] = reply_to
    return_path = _address_domain(_first(headers, "return-path"))
    if return_path:
        facts["return_path_domain"] = return_path
    if _first(headers, "list-unsubscribe"):
        facts["list_unsubscribe"] = True
    list_id = _first(headers, "list-id")
    if list_id:
        match = re.search(r"<([^<>\s]{1,200})>", list_id)
        ident = _domain(match.group(1) if match else list_id)
        if ident:
            facts["list_id"] = ident
    precedence = _token(_first(headers, "precedence"))
    if precedence:
        facts["precedence"] = precedence
    github_reason = _token(_first(headers, "x-github-reason"))
    if github_reason:
        facts["github_reason"] = github_reason
    return facts


def dumps(facts: Mapping[str, Any] | None) -> str | None:
    if not facts:
        return None
    return json.dumps({k: facts[k] for k in sorted(facts) if k in FACT_KEYS}, sort_keys=True)


def loads(value: Any) -> dict[str, Any]:
    """Parse stored ``header_meta``; anything malformed is simply no facts."""
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in FACT_KEYS}
