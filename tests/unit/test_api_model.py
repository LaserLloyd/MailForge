"""API model as primary (llm/providers.py + LMStudioBridge cloud routing)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from mailforge.config import Settings
from mailforge.llm import bridge as bridge_mod
from mailforge.llm.bridge import LMStudioBridge
from mailforge.llm.providers import PRESETS, resolve_api_key, validate_base_url


def _bridge(**kw) -> LMStudioBridge:
    base = dict(
        host="127.0.0.1:1234", model_id="local-model", embed_id="embed",
        cloud_base_url="https://api.minimax.io/v1", cloud_model="MiniMax-M3.1-Flash",
        cloud_key="sk-test", cloud_extra_body={"reasoning_split": True},
        primary="api", fallback_to_local=True,
    )
    base.update(kw)
    return LMStudioBridge(**base)


@pytest.fixture()
def http(monkeypatch):
    """Route the bridge's httpx.AsyncClient through a recorder."""
    seen: list[dict] = []
    replies: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append({"url": str(request.url), "headers": dict(request.headers), "json": body})
        for prefix, reply in replies.items():
            if str(request.url).startswith(prefix):
                if isinstance(reply, int):
                    return httpx.Response(reply, json={"error": "x"})
                return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})
        return httpx.Response(503, json={"error": "nothing configured"})

    real = httpx.AsyncClient

    def fake_client(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(bridge_mod.httpx, "AsyncClient", fake_client)
    return seen, replies


def test_api_first_with_provider_extras_and_no_local_fields(http):
    seen, replies = http
    replies["https://api.minimax.io"] = "<think>hmm</think>OK"
    b = _bridge()
    out = asyncio.run(b.chat([{"role": "user", "content": "hi"}]))
    assert out == "OK"  # reasoning stripped
    assert b.last_model == "MiniMax-M3.1-Flash"
    call = seen[0]
    assert call["url"] == "https://api.minimax.io/v1/chat/completions"
    assert call["headers"]["authorization"] == "Bearer sk-test"
    assert call["json"]["reasoning_split"] is True
    # Fields only a local StudioForge understands are not sent to a hosted API.
    assert "priority" not in call["json"] and "x-sf-client" not in call["headers"]


def test_falls_back_to_local_on_plan_limit(http):
    seen, replies = http
    replies["https://api.minimax.io"] = 402
    replies["http://127.0.0.1:1234"] = "local answer"
    b = _bridge()
    assert asyncio.run(b.chat([{"role": "user", "content": "hi"}])) == "local answer"
    assert b.last_model == "local-model"
    assert seen[1]["json"]["priority"] == 2  # local call keeps its tier
    # The API key never goes to the local server.
    assert seen[1]["headers"].get("authorization") != "Bearer sk-test"


def test_no_fallback_when_turned_off(http):
    seen, replies = http
    replies["https://api.minimax.io"] = 402
    replies["http://127.0.0.1:1234"] = "local answer"
    b = _bridge(fallback_to_local=False)
    assert asyncio.run(b.chat([{"role": "user", "content": "hi"}])) == ""
    assert len(seen) == 1


def test_local_only_skips_the_api(http):
    seen, replies = http
    replies["http://127.0.0.1:1234"] = "local answer"
    b = _bridge()
    assert asyncio.run(b.chat([{"role": "user", "content": "hi"}], local_only=True)) \
        == "local answer"
    assert all(not c["url"].startswith("https://api.minimax.io") for c in seen)


def test_incomplete_api_config_stays_local():
    assert not _bridge(cloud_key="").use_cloud
    assert not _bridge(cloud_model="").use_cloud
    assert not _bridge(primary="local").use_cloud
    assert _bridge().use_cloud and _bridge().is_up()  # no local probe needed


def test_repr_never_contains_the_key():
    assert "sk-test" not in repr(_bridge())


def test_base_url_rules():
    assert validate_base_url("https://api.minimax.io/v1/") == "https://api.minimax.io/v1"
    assert validate_base_url("http://[fd00::5]:1234/v1")
    assert validate_base_url("http://localhost:8080/v1")
    for bad in ("http://api.example.com/v1", "ftp://x/v1", "", "https://user:pw@api.x/v1"):
        with pytest.raises(ValueError):
            validate_base_url(bad)


def test_minimax_preset_matches_its_api_rules():
    p = PRESETS["minimax"]
    assert p.base_url.startswith("https://")
    # M3.1 refuses thinking "disabled"; the preset must not send it.
    assert "thinking" not in p.extra_body and p.extra_body.get("reasoning_split") is True


def test_env_key_overrides_keyring(monkeypatch):
    monkeypatch.setenv("MAILFORGE_CLOUD_API_KEY", "from-env")
    assert resolve_api_key(Settings().llm) == "from-env"


def test_from_settings_and_toml_never_hold_the_key(monkeypatch):
    monkeypatch.setenv("MAILFORGE_CLOUD_API_KEY", "sk-secret-value")
    s = Settings(llm={
        "primary": "api", "api_preset": "minimax",
        "api_base_url": "https://api.minimax.io/v1", "api_model": "MiniMax-M3.1-Flash",
        "api_extra_body": {"reasoning_split": True},
    })
    b = LMStudioBridge.from_settings(s)
    assert b.use_cloud and b.active_model == "MiniMax-M3.1-Flash"
    toml = s.to_toml()
    assert 'primary = "api"' in toml and "sk-secret-value" not in toml


def test_live_reconfigure():
    b = _bridge()
    b.configure_cloud(base_url="", model="", key="", extra_body=None,
                      primary="local", fallback_to_local=True)
    assert not b.use_cloud and b.active_model == "local-model"
