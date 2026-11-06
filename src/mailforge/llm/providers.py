"""API model providers (OpenAI-compatible chat endpoints).

MailForge runs on a local model server by default. ``[llm] primary = "api"``
switches chat work (triage, drafts, revisions, translation) to a hosted model
instead, with the local server as an optional fallback. Embeddings always stay
on the local server.

The key is entered once in Settings and kept in the OS keyring (the same store
as the mailbox passwords); an environment variable can override it for
headless installs. It is never written to config.toml, logged or shown.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

KEYRING_KIND = "llm"
KEYRING_ACCOUNT = "api"


@dataclass(frozen=True)
class Preset:
    label: str
    base_url: str
    model: str = ""
    extra_body: dict[str, Any] = field(default_factory=dict)
    docs: str = ""


#: One-click provider settings. ``model`` empty = the person picks one.
PRESETS: dict[str, Preset] = {
    "minimax": Preset(
        "MiniMax",
        "https://api.minimax.io/v1",
        "MiniMax-M3.1-Flash",
        # M3.1 refuses thinking "disabled"; split the reasoning out of the
        # answer so the reply text is clean.
        {"reasoning_split": True},
        "https://platform.minimax.io/",
    ),
    "deepseek": Preset(
        "DeepSeek",
        "https://api.deepseek.com/v1",
        "deepseek-v4-flash",
        {"thinking": {"type": "disabled"}},
        "https://platform.deepseek.com/",
    ),
    "openai": Preset("OpenAI", "https://api.openai.com/v1", "", {}, "https://platform.openai.com/"),
    "openrouter": Preset("OpenRouter", "https://openrouter.ai/api/v1", "", {}, "https://openrouter.ai/"),
    "custom": Preset("Other (OpenAI-compatible)", "", "", {}),
}


def validate_base_url(url: str) -> str:
    """The URL, normalised, or ``ValueError``.

    The key travels with every request, so a hosted endpoint must be HTTPS.
    Plain HTTP is accepted only for loopback / private-network servers.
    """
    url = str(url or "").strip().rstrip("/")
    if not url:
        raise ValueError("enter the provider's API base URL")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("the base URL must look like https://api.example.com/v1")
    if parts.username or parts.password:
        raise ValueError("put the key in the API key field, not in the URL")
    if parts.scheme == "http":
        host = parts.hostname
        private = host in {"localhost"} or host.endswith(".local")
        try:
            ip = ipaddress.ip_address(host)
            private = private or ip.is_private or ip.is_loopback
        except ValueError:
            pass
        if not private:
            raise ValueError("a hosted API must use https:// (the key would travel in clear)")
    return url


def resolve_api_key(llm: Any) -> str:
    """The API key: the env var named by ``llm.api_key_env`` first, then the
    keyring. ``""`` when none is set. Never logged."""
    name = str(getattr(llm, "cloud_key_env", "") or "")
    if name:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    try:
        from ..secrets import get_secret

        secret = get_secret(KEYRING_KIND, KEYRING_ACCOUNT)
    except Exception:  # noqa: BLE001 — no keyring backend: no key
        return ""
    return secret.get_secret_value().strip() if secret is not None else ""


def store_api_key(value: str) -> None:
    from ..secrets import set_secret

    set_secret(KEYRING_KIND, KEYRING_ACCOUNT, value.strip())


def delete_api_key() -> None:
    from ..secrets import delete_secret

    delete_secret(KEYRING_KIND, KEYRING_ACCOUNT)
