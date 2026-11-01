"""Regression tests for the 2026-11 issue batch (#17, #6, #7, #5)."""

from __future__ import annotations

import logging
import tomllib

import httpx

from mailforge.config import IMAPAccount, Settings
from mailforge.llm import bridge as bridge_mod
from mailforge.llm.model_ids import describe_mismatch, nearest_served


def test_trailing_slash_served_id_does_not_match_everything():
    served = ["org/", "  ", "vendor/other-model"]
    assert nearest_served("vendor/qwen-27b", served) == []
    msg = describe_mismatch("vendor/qwen-27b", served)
    assert msg and "Closest served" not in msg


def test_real_stem_still_matches_beside_slash_id():
    assert nearest_served("qwen-27b", ["org/", "pub/repo/qwen-27b-q5"]) == ["pub/repo/qwen-27b-q5"]


def test_is_up_401_warns_once_per_process(monkeypatch, caplog):
    bridge_mod.reset_auth_warning()
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: httpx.Response(401, request=httpx.Request("GET", "http://x"))
    )
    b = bridge_mod.LMStudioBridge.from_settings(Settings())
    with caplog.at_level(logging.WARNING, logger=bridge_mod.log.name):
        assert b.is_up() is False
        assert b.is_up() is False
    hits = [r for r in caplog.records if "HTTP 401" in r.getMessage()]
    assert len(hits) == 1
    assert "STUDIOFORGE_API_KEY" in hits[0].getMessage()
    bridge_mod.reset_auth_warning()


def test_llm_check_reads_path_ids_like_the_resolver(monkeypatch):
    from typer.testing import CliRunner

    from mailforge.cli import app

    body = {"data": [{"path": "pub/repo/stem-a"}, {"model_key": "pub/repo/stem-b"}]}
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: httpx.Response(200, json=body, request=httpx.Request("GET", "http://x"))
    )
    r = CliRunner().invoke(app, ["llm-check"])
    assert "Served:     2 model(s)" in r.output


def test_add_account_does_not_persist_env_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILFORGE_LLM__LM_STUDIO_HOST", "llm.example.com:1234")
    s = Settings()
    assert s.llm.lm_studio_host == "llm.example.com:1234"
    cfg = tmp_path / "config.toml"
    cfg.write_text('db_path = "/tmp/example.db"\n', encoding="utf-8")
    acct = IMAPAccount(
        name="alex", site_id=next(iter(s.sites)), host="imap.example.com",
        port=993, username="alex@example.com",
    )
    s.save_added_account(acct, path=cfg)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert "llm" not in data
    assert data["db_path"] == "/tmp/example.db"
    assert [a["name"] for a in data["imap_accounts"]] == ["alex"]
    # appending a second account keeps the first
    s.save_added_account(acct.model_copy(update={"name": "alex2"}), path=cfg)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert [a["name"] for a in data["imap_accounts"]] == ["alex", "alex2"]
