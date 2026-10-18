"""StudioForge contract tests: pin every StudioForge field MailForge reads.

See tests/contract_studioforge/README.md. Fixtures are derived from the client
code; a StudioForge change that breaks them must fail here, visibly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from _fakesf import FakeStudioForge

from mailforge.llm.bridge import EMBED_DIM, LMStudioBridge

FIX = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _loopback_no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _bridge(sf: FakeStudioForge) -> LMStudioBridge:
    return LMStudioBridge(sf.url.removeprefix("http://"), "pub/repo/chat-Q5_K_S",
                          "pub/repo/embed-Q8_0")


MSG = [{"role": "user", "content": "x"}]


def test_v1_models_additive_fields_are_ignored():
    fx = fixture("models")["response"]
    with FakeStudioForge() as sf:
        sf.route("GET", "/v1/models", 200, fx)
        bridge = _bridge(sf)
        assert bridge.is_up() is True
        models = asyncio.run(bridge.list_models())
        report = asyncio.run(bridge.resolve_served_models())
    assert [m["id"] for m in models] == ["pub/repo/chat-Q5_K_S", "pub/repo/embed-Q8_0"]
    assert report["served"] == ["pub/repo/chat-Q5_K_S", "pub/repo/embed-Q8_0"]
    assert report["changed"] is False          # both configured ids are served


def test_chat_request_keys_priority_and_client_header():
    fx = fixture("chat_completion")
    with FakeStudioForge() as sf:
        sf.route("POST", "/v1/chat/completions", 200, fx["response_content"])
        out = asyncio.run(_bridge(sf).chat(MSG))
        asyncio.run(_bridge(sf).chat(MSG, response_format={"type": "json_object"}))
    plain, with_schema = sf.requests
    assert out == "hello"
    assert sorted(plain["body"]) == sorted(fx["expect_request_keys"])
    assert sorted(with_schema["body"]) == sorted(fx["expect_request_keys_with_schema"])
    assert plain["body"]["priority"] == fx["expect_priority"]
    for k, v in fx["expect_request_headers"].items():
        assert plain["headers"][k] == v


def test_reasoning_only_reply_is_read_from_reasoning_content():
    fx = fixture("chat_completion")
    with FakeStudioForge() as sf:
        sf.route("POST", "/v1/chat/completions", 200, fx["response_reasoning_only"])
        out = asyncio.run(_bridge(sf).chat(MSG))
    assert out == '{"ok": true}'


def test_embeddings_request_keys_priority_and_response_shape():
    fx = fixture("embeddings")
    with FakeStudioForge() as sf:
        sf.route("POST", "/v1/embeddings", 200,
                 {"data": [{"index": 0, "embedding": [0.25] * fx["dim_required"]}]})
        vectors = asyncio.run(_bridge(sf).embed(["a"]))
    req = sf.requests[0]
    assert sorted(req["body"]) == sorted(fx["expect_request_keys"])
    assert req["body"]["priority"] == fx["expect_priority"]
    for k, v in fx["expect_request_headers"].items():
        assert req["headers"][k] == v
    assert len(vectors) == 1 and len(vectors[0]) == fx["dim_required"] == EMBED_DIM


def test_wider_embeddings_are_truncated_to_the_store_dimension():
    fx = fixture("embeddings")["response_wider_than_required"]["data"][0]
    wide = {"data": [{"index": 0, "embedding": [0.1] * fx["embedding_dim"]}]}
    with FakeStudioForge() as sf:
        sf.route("POST", "/v1/embeddings", 200, wide)
        vectors = asyncio.run(_bridge(sf).embed(["a"]))
    assert len(vectors[0]) == EMBED_DIM


@pytest.mark.parametrize("err", fixture("errors")["errors"],
                         ids=lambda e: e["body"]["error"]["code"])
def test_every_studioforge_refusal_degrades_to_the_sentinels(err):
    """gpu_leased / priority_hold / insufficient_vram / model_not_found /
    unauthorized: nothing raises, drafts defer, embeddings return []."""
    with FakeStudioForge() as sf:
        sf.route("POST", "/v1/chat/completions", err["http_status"], err["body"])
        sf.route("POST", "/v1/embeddings", err["http_status"], err["body"])
        sf.route("GET", "/v1/models", err["http_status"], err["body"])
        bridge = _bridge(sf)
        assert asyncio.run(bridge.chat(MSG)) == ""
        assert asyncio.run(bridge.embed(["a"])) == []
        assert asyncio.run(bridge.chat_structured(MSG, {"type": "object"})) == {}
        assert bridge.is_up() is False


def test_mailforge_does_not_use_the_management_api():
    """No /api/* call (leases, load-recommended, status). A new dependency on
    them must come with fixtures; this test is where it will first show."""
    fx = fixture("chat_completion")
    with FakeStudioForge() as sf:
        sf.route("GET", "/v1/models", 200, fixture("models")["response"])
        sf.route("POST", "/v1/chat/completions", 200, fx["response_content"])
        sf.route("POST", "/v1/embeddings", 200,
                 {"data": [{"index": 0, "embedding": [0.0] * EMBED_DIM}]})
        bridge = _bridge(sf)
        bridge.is_up()
        asyncio.run(bridge.list_models())
        asyncio.run(bridge.resolve_served_models())
        asyncio.run(bridge.chat(MSG))
        asyncio.run(bridge.embed(["a"]))
    assert sf.requests
    assert all(r["path"].startswith("/v1/") for r in sf.requests)
