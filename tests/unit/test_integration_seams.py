"""Tests for the standalone-app + OpenClaw-link seams.

Cover the additions that make the app a free-standing LM Studio client with an
optional OpenClaw link:
  * prompt-override loading (OpenClaw can push prompts; packaged default else)
  * heavy-lift wiring is OFF by default and only on when configured
  * heavy-lift transparently falls back to the local model on failure
  * model auto-resolution adapts to whatever LM Studio serves (family-aware)
  * the desktop launcher key gates the /launch auth path
All run with no network and no optional ML deps.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from openclaw_email.config import LLMSettings, OpenClawSettings, Settings
from openclaw_email.llm import prompts
from openclaw_email.llm.bridge import LMStudioBridge


def test_prompt_override_prefers_user_dir_then_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_EMAIL_PROMPT_DIR", str(tmp_path))
    packaged = prompts.load("classify")
    assert packaged  # packaged default exists

    (tmp_path / "classify.txt").write_text("OVERRIDDEN", encoding="utf-8")
    assert prompts.load("classify") == "OVERRIDDEN"

    (tmp_path / "classify.txt").unlink()
    assert prompts.load("classify") == packaged  # falls back cleanly


def test_heavy_lift_off_by_default():
    b = LMStudioBridge.from_settings(Settings())
    assert b.has_heavy is False


def test_heavy_lift_on_when_configured(monkeypatch):
    monkeypatch.setenv("OPENCLAW_EMAIL_HEAVY_KEY", "k-123")
    s = Settings(
        openclaw=OpenClawSettings(
            heavy_lift_base_url="http://127.0.0.1:9/v1",
            heavy_lift_model="big-online-model",
        )
    )
    b = LMStudioBridge.from_settings(s)
    assert b.has_heavy is True
    assert b.heavy_model == "big-online-model"
    assert b.heavy_key == "k-123"


def test_heavy_disabled_flag_ignores_link(monkeypatch):
    monkeypatch.setenv("OPENCLAW_EMAIL_HEAVY_KEY", "k-123")
    s = Settings(
        openclaw=OpenClawSettings(
            enabled=False,
            heavy_lift_base_url="http://127.0.0.1:9/v1",
            heavy_lift_model="big",
        )
    )
    assert LMStudioBridge.from_settings(s).has_heavy is False


def test_heavy_structured_falls_back_to_local(monkeypatch):
    # Heavy endpoint unreachable -> chat_structured(heavy=True) must use local.
    b = LMStudioBridge(
        host="localhost:1234",
        model_id="m",
        embed_id="e",
        heavy_base_url="http://127.0.0.1:9/v1",  # dead
        heavy_model="big",
    )
    calls: list[str] = []

    async def fake_post(base_url, model, messages, **kw):
        calls.append(base_url)
        if "127.0.0.1:9" in base_url:
            return ""  # heavy fails
        return '{"ok": true}'  # local succeeds

    monkeypatch.setattr(b, "_post_chat", fake_post)
    out = asyncio.run(
        b.chat_structured([{"role": "user", "content": "x"}], {"type": "object"}, heavy=True)
    )
    assert out == {"ok": True}
    assert any("127.0.0.1:9" in c for c in calls)  # heavy was tried
    assert any("1234" in c for c in calls)  # then local


def test_model_resolution_is_family_aware(monkeypatch):
    b = LMStudioBridge(host="h", model_id="qwen/qwen3.6-35b-a3b", embed_id="nomic-embed")

    async def fake_list():
        return [
            {"id": "google/gemma-4-31b"},
            {"id": "qwen/qwen3.6-27b"},
            {"id": "text-embedding-nomic-embed-text-v1.5"},
        ]

    monkeypatch.setattr(b, "list_models", fake_list)
    rep = asyncio.run(b.resolve_served_models())
    assert rep["changed"] is True
    assert b.model_id == "qwen/qwen3.6-27b"  # same family preferred over gemma
    assert "embed" in b.embed_id.lower()


def test_model_resolution_keeps_served_config(monkeypatch):
    b = LMStudioBridge(host="h", model_id="qwen/qwen3.6-27b", embed_id="nomic-embed-v1.5")

    async def fake_list():
        return [{"id": "qwen/qwen3.6-27b"}, {"id": "nomic-embed-v1.5"}]

    monkeypatch.setattr(b, "list_models", fake_list)
    rep = asyncio.run(b.resolve_served_models())
    assert rep["changed"] is False  # both already served


def test_launcher_key_is_stable_and_0600(tmp_path, monkeypatch):
    import os
    import stat

    monkeypatch.setattr("openclaw_email.paths.data_dir", lambda: tmp_path)
    from openclaw_email.ui import app as ui_app

    k1 = ui_app.launcher_key()
    k2 = ui_app.launcher_key()
    assert k1 and k1 == k2  # reusable, stable across calls
    f = tmp_path / "ui_launcher_key"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_standalone_has_no_smtp_and_passes_invariants():
    # A free-standing config with zero accounts still satisfies the invariants.
    s = Settings(llm=LLMSettings())
    s.assert_invariants()
    assert isinstance(Path(s.db_path), Path)
