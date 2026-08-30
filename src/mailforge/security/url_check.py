"""URL allowlist & sanitisation (build spec §1, §5, §11).

Uses ``tldextract`` **offline** (``suffix_list_urls=()``) — a CORE dependency
that IS installed. Rejects raw IPs, punycode (``xn--``) and non-http(s)
schemes; strips tracking query parameters; flags domains outside the allowlist.

Exposes ``check_urls(targets, allowlist_domains) -> UrlResult``.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract

log = logging.getLogger(__name__)

# Common tracking params stripped from every URL (privacy + provenance).
_TRACKING_PARAMS = re.compile(
    r"^(?:utm_[a-z]+|fbclid|gclid|gclsrc|dclid|msclkid|mc_eid|mc_cid|"
    r"yclid|_hsenc|_hsmi|igshid|vero_id|wickedid|oly_enc_id|oly_anon_id|"
    r"ref|ref_src|spm|scid|si)$",
    re.I,
)

_ALLOWED_SCHEMES = {"http", "https"}


@dataclass
class UrlResult:
    """Outcome of a URL check (spec §5/§11)."""

    passed: bool
    blocked: list[str] = field(default_factory=list)  # rejected URLs + reason
    stripped: list[str] = field(default_factory=list)  # sanitised replacements


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    # Fully offline: never fetch the public-suffix list (spec §1).
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def _is_ip(host: str) -> bool:
    h = host.strip("[]")  # IPv6 literal brackets
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def _registered_domain(host: str) -> str:
    ext = _extractor()(host)
    if ext.registered_domain:
        return ext.registered_domain.lower()
    return host.lower()


def _strip_trackers(url: str, parts: tuple) -> str:
    scheme, netloc, path, query, fragment = parts
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
    new_query = urlencode(kept)
    # Drop tracking fragments too (e.g. #utm style is rare but harmless to keep).
    return urlunsplit((scheme, netloc, path, new_query, fragment))


def check_urls(targets: list[str], allowlist_domains: set[str]) -> UrlResult:
    """Validate, sanitise and allowlist-check a list of URL ``targets``.

    Blocks: raw IP hosts, punycode (``xn--``) hosts, non-http(s) schemes, and
    domains whose registered domain is outside ``allowlist_domains`` (when an
    allowlist is provided). Strips known tracking query params from survivors.
    """
    allow = {d.lower() for d in allowlist_domains}
    res = UrlResult(passed=True)

    for raw in targets:
        url = (raw or "").strip()
        if not url:
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            res.blocked.append(f"{url} (unparseable)")
            res.passed = False
            continue

        scheme = (parts.scheme or "").lower()
        if scheme not in _ALLOWED_SCHEMES:
            res.blocked.append(f"{url} (scheme '{scheme or 'none'}' not http/https)")
            res.passed = False
            continue

        host = (parts.hostname or "").lower()
        if not host:
            res.blocked.append(f"{url} (no host)")
            res.passed = False
            continue

        if _is_ip(host):
            res.blocked.append(f"{url} (raw IP address)")
            res.passed = False
            continue

        # Punycode anywhere in the host (homograph/IDN spoofing risk).
        if "xn--" in host:
            res.blocked.append(f"{url} (punycode host)")
            res.passed = False
            continue

        domain = _registered_domain(host)
        if allow and domain not in allow and host not in allow:
            res.blocked.append(f"{url} (domain '{domain}' not in allowlist)")
            res.passed = False
            continue

        cleaned = _strip_trackers(url, parts)
        if cleaned != url:
            res.stripped.append(f"{url} -> {cleaned}")

    return res
