"""Bearer-key support on the LLM link, and the model-id mismatch message.

A fake OpenAI-compatible server (tests/_fakesf.py) runs in-process on an
ephemeral loopback port and records the headers that actually went on the wire.
The socket guard (tests/_netguard.py) forbids every other connection.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from _fakesf import FakeStudioForge

from mailforge.config import LLMSettings, OpenClawSettings, Settings
from mailforge.llm.bridge import LMStudioBridge, resolve_llm_api_key
from mailforge.llm.model_ids import describe_mismatch, is_studioforge_id

KEY = "test-bearer-key-0123456789"
HEAVY = "test-heavy-key-9876543210"

CHAT_OK = {"choices": [{"message": {"content": "hello"}}]}
EMBED_OK = {"data": [{"index": 0, "embedding": [0.5] * 768}]}


@pytest.fixture(autouse=True)
def _clean_key_env(monkeypatch):
    for name in ("MAILFORGE_LLM_API_KEY", "STUDIOFORGE_API_KEY", "MAILFORGE_HEAVY_KEY",
                 "MY_LLM_KEY"):
        monkeypatch.delenv(name, raising=False)
    # a developer machine may export a proxy; loopback must bypass it
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _server() -> FakeStudioForge:
    sf = FakeStudioForge()
    sf.route("GET", "/v1/models", 200, {"data": [{"id": "pub/repo/stem"}]})
    sf.route("POST", "/v1/chat/completions", 200, CHAT_OK)
    sf.route("POST", "/v1/embeddings", 200, EMBED_OK)
    return sf


def _bridge(sf: FakeStudioForge, **kw) -> LMStudioBridge:
    host = sf.url.removeprefix("http://")
    return LMStudioBridge(host=host, model_id="pub/repo/stem", embed_id="pub/repo/emb", **kw)


# ----------------------------------------------------------------- resolution
def test_key_comes_from_the_env_var_named_by_config(monkeypatch):
    llm = LLMSettings(api_key_env="MY_LLM_KEY")
    assert resolve_llm_api_key(llm) == ""
    monkeypatch.setenv("MY_LLM_KEY", "  " + KEY + "\n")
    assert resolve_llm_api_key(llm) == KEY


def test_default_env_name_and_shared_fallback(monkeypatch):
    assert LLMSettings().api_key_env == "MAILFORGE_LLM_API_KEY"
    monkeypatch.setenv("STUDIOFORGE_API_KEY", "shared")
    assert resolve_llm_api_key(LLMSettings()) == "shared"
    monkeypatch.setenv("MAILFORGE_LLM_API_KEY", KEY)
    assert resolve_llm_api_key(LLMSettings()) == KEY       # named var wins


def test_from_settings_picks_the_key_up(monkeypatch):
    monkeypatch.setenv("MAILFORGE_LLM_API_KEY", KEY)
    bridge = LMStudioBridge.from_settings(Settings())
    assert bridge.has_api_key is True


def test_the_key_is_not_a_config_value_and_not_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_LLM_API_KEY", KEY)
    target = tmp_path / "config.toml"
    Settings().save(target)
    text = target.read_text(encoding="utf-8")
    assert KEY not in text
    assert "api_key_env" in text            # only the variable NAME is persisted


def test_repr_never_contains_the_keys():
    bridge = LMStudioBridge("127.0.0.1:1", "m", "e", api_key=KEY, heavy_key=HEAVY)
    assert KEY not in repr(bridge) and HEAVY not in repr(bridge)
    assert "auth=key" in repr(bridge)


# ------------------------------------------------------------------ on the wire
def test_every_local_request_carries_the_bearer_header():
    with _server() as sf:
        bridge = _bridge(sf, api_key=KEY)
        assert bridge.is_up() is True
        assert asyncio.run(bridge.list_models()) == [{"id": "pub/repo/stem"}]
        assert asyncio.run(bridge.chat([{"role": "user", "content": "x"}])) == "hello"
        assert len(asyncio.run(bridge.embed(["a"]))) == 1
    assert len(sf.requests) == 4
    for r in sf.requests:
        assert r["headers"]["Authorization"] == f"Bearer {KEY}", r["path"]


def test_no_key_means_no_authorization_header():
    with _server() as sf:
        bridge = _bridge(sf)
        assert bridge.is_up() is True
        asyncio.run(bridge.chat([{"role": "user", "content": "x"}]))
        asyncio.run(bridge.embed(["a"]))
    for r in sf.requests:
        assert "Authorization" not in r["headers"]


def test_identity_and_priority_still_sent_alongside_the_key():
    with _server() as sf:
        bridge = _bridge(sf, api_key=KEY)
        asyncio.run(bridge.chat([{"role": "user", "content": "x"}]))
        asyncio.run(bridge.embed(["a"]))
    chat, emb = sf.requests
    assert chat["headers"]["X-SF-Client"] == "mailforge" and chat["body"]["priority"] == 2
    assert emb["headers"]["X-SF-Client"] == "mailforge" and emb["body"]["priority"] == 3


def test_heavy_lift_endpoint_gets_only_its_own_key():
    """The local key must never be forwarded to the separate heavy-lift host."""
    with _server() as local, _server() as heavy:
        bridge = _bridge(local, api_key=KEY, heavy_key=HEAVY,
                         heavy_base_url=heavy.url + "/v1", heavy_model="big")
        out = asyncio.run(bridge._chat_heavy([{"role": "user", "content": "x"}]))
        asyncio.run(bridge.chat([{"role": "user", "content": "x"}]))
    assert out == "hello"
    assert heavy.requests[0]["headers"]["Authorization"] == f"Bearer {HEAVY}"
    assert local.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in str(dict(heavy.requests[0]["headers"]))


def test_refused_key_is_reported_without_the_key(caplog):
    with FakeStudioForge() as sf:
        sf.route("GET", "/v1/models", 401, {"error": {"code": "unauthorized"}})
        sf.route("POST", "/v1/chat/completions", 401, {"error": {"code": "unauthorized"}})
        bridge = _bridge(sf, api_key=KEY)
        with caplog.at_level(logging.INFO):
            assert bridge.is_up() is False
            assert asyncio.run(bridge.chat([{"role": "user", "content": "x"}])) == ""
    text = caplog.text
    assert "requires an API key" in text
    assert "MAILFORGE_LLM_API_KEY" in text
    assert KEY not in text


# ---------------------------------------------------------------- model ids
def test_studioforge_id_shape():
    assert is_studioforge_id("unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S")
    assert not is_studioforge_id("qwen/qwen3.6-35b-a3b")
    assert not is_studioforge_id("nomic-embed")


def test_mismatch_message_names_the_id_format_and_the_closest_served_id():
    served = ["unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-Q4_K_M"]
    msg = describe_mismatch("qwen/qwen3.6-35b-a3b", served, "chat")
    assert msg is not None
    assert "publisher/repo/stem" in msg and "404" in msg
    assert "llm.chat_model" in msg
    assert "unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-Q4_K_M" in msg
    assert "llm.embedding_model" in describe_mismatch("text-embedding-x", served, "embedding")


def test_no_message_when_the_id_is_served_or_nothing_is_known():
    served = ["a/b/c"]
    assert describe_mismatch("a/b/c", served) is None
    assert describe_mismatch("A/B/C", served) is None          # case-insensitive
    assert describe_mismatch("x/y", []) is None                 # server down: claim nothing
    assert describe_mismatch("", served) is None


def test_model_id_notes_over_http_and_chat_404_logs_the_hint(caplog):
    with FakeStudioForge() as sf:
        sf.route("GET", "/v1/models", 200,
                 {"data": [{"id": "pub/repo/real-stem"}, {"id": "pub/repo/real-emb"}]})
        sf.route("POST", "/v1/chat/completions", 404,
                 {"error": {"code": "model_not_found", "message": "unknown model"}})
        host = sf.url.removeprefix("http://")
        bridge = LMStudioBridge(host, "qwen/qwen3.6-35b-a3b", "text-embedding-nomic")
        notes = asyncio.run(bridge.model_id_notes())
        with caplog.at_level(logging.WARNING):
            assert asyncio.run(bridge.chat([{"role": "user", "content": "x"}])) == ""
    assert len(notes) == 2 and "publisher/repo/stem" in notes[0]
    assert "HTTP 404" in caplog.text and "llm-check" in caplog.text


def test_llm_check_command_reports_key_and_mismatch(monkeypatch):
    from typer.testing import CliRunner

    from mailforge.cli import app

    with FakeStudioForge() as sf:
        sf.route("GET", "/v1/models", 200, {"data": [{"id": "pub/repo/stem"}]})
        host = sf.url.removeprefix("http://")
        monkeypatch.setenv("MAILFORGE_LLM_API_KEY", KEY)
        settings = Settings(llm=LLMSettings(lm_studio_host=host))
        monkeypatch.setattr("mailforge.config.load_settings", lambda *a, **k: settings)
        result = CliRunner().invoke(app, ["llm-check"])
    assert result.exit_code == 1                       # default ids are not served
    assert "set (sent as Authorization: Bearer)" in result.output
    assert KEY not in result.output
    assert "publisher/repo/stem" in result.output
    assert sf.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_llm_check_refused_key_exits_1(monkeypatch):
    from typer.testing import CliRunner

    from mailforge.cli import app

    with FakeStudioForge() as sf:
        sf.route("GET", "/v1/models", 401, {"error": {"code": "unauthorized"}})
        settings = Settings(llm=LLMSettings(lm_studio_host=sf.url.removeprefix("http://")))
        monkeypatch.setattr("mailforge.config.load_settings", lambda *a, **k: settings)
        result = CliRunner().invoke(app, ["llm-check"])
    assert result.exit_code == 1
    assert "requires an API key" in result.output


def test_openclaw_settings_unchanged_by_the_new_key():
    assert OpenClawSettings().heavy_lift_key_env == "MAILFORGE_HEAVY_KEY"
