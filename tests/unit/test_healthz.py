"""/healthz: JSON status for monitors, behind the Host guard, outside the login gate."""

from __future__ import annotations

import json

import pytest

from mailforge.db.store import open_store
from mailforge.health import health_report
from mailforge.mail.sync_state import registry


@pytest.fixture()
def clean_registry():
    names = [a.account for a in registry.snapshot()]
    for n in names:
        registry.unregister(n)
    yield registry
    for a in list(registry.snapshot()):
        registry.unregister(a.account)


def test_ok_when_every_mailbox_is_healthy(tmp_path, clean_registry):
    clean_registry.register("a")
    clean_registry.register("b")
    clean_registry.update("a", state="idle")
    clean_registry.note_check("a")
    clean_registry.update("b", state="idle")
    clean_registry.note_check("b")
    with open_store(tmp_path / "h.db") as store:
        body, code = health_report(store)
    assert code == 200 and body["status"] == "ok" and body["db"] == "ok"
    assert set(body["accounts"]) == {"a", "b"}
    assert all(v["healthy"] for v in body["accounts"].values())
    assert body["accounts"]["a"]["last_check_age_s"] is not None


def test_degraded_on_listener_error_and_error_is_truncated(tmp_path, clean_registry):
    clean_registry.register("a")
    clean_registry.note_check("a", error="x" * 500)
    with open_store(tmp_path / "h.db") as store:
        body, code = health_report(store)
    assert code == 503 and body["status"] == "degraded"
    assert body["accounts"]["a"]["healthy"] is False
    assert len(body["accounts"]["a"]["last_error"]) <= 160


def test_no_listeners_is_not_ok(tmp_path, clean_registry):
    with open_store(tmp_path / "h.db") as store:
        body, code = health_report(store)
    assert code == 503 and body["accounts"] == {}


def test_route_serves_json_without_login_but_obeys_host_guard(tmp_path, clean_registry):
    from starlette.testclient import TestClient

    from mailforge.config import Settings
    from mailforge.ui import app as ui_app

    clean_registry.register("a")
    clean_registry.update("a", state="idle")
    clean_registry.note_check("a")
    with open_store(tmp_path / "h.db") as store:
        ui_app.build_app(store, Settings(imap_accounts=[]), None, port=45871)
        client = TestClient(ui_app.nicegui_app, base_url="http://127.0.0.1:45871")
        r = client.get("/healthz")  # no cookie, no token
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert "no-store" in r.headers["cache-control"]
        assert set(r.json()) == {"status", "accounts", "db", "version"}
        evil = client.get("/healthz", headers={"host": "evil.example"})
        assert evil.status_code == 403
        assert json.dumps(r.json()).find("password") == -1


def test_cli_health_unreachable_and_no_port(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mailforge import cli

    monkeypatch.setattr("mailforge.paths.runtime_file", lambda name: tmp_path / name)
    r = CliRunner().invoke(cli.app, ["health"])
    assert r.exit_code == 2 and "ui_port" in r.output
    (tmp_path / "ui_port").write_text("1", encoding="utf-8")  # nothing listens on :1
    r = CliRunner().invoke(cli.app, ["health", "--timeout", "1"])
    assert r.exit_code == 2 and "unreachable" in r.output
