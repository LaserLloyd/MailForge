"""`mailforge audit-verify` on a fresh home (no database / no schema yet)."""

from __future__ import annotations

import sqlite3

from typer.testing import CliRunner

from mailforge import cli
from mailforge.audit.log import verify_chain
from mailforge.config import Settings
from mailforge.db.store import open_store


def _invoke(monkeypatch, db):
    monkeypatch.setattr("mailforge.config.load_settings", lambda: Settings(db_path=str(db)))
    return CliRunner().invoke(cli.app, ["audit-verify"])


def test_no_database_prints_message_exit_0_and_creates_nothing(tmp_path, monkeypatch):
    db = tmp_path / "data" / "agent.db"
    r = _invoke(monkeypatch, db)
    assert r.exit_code == 0, r.output
    assert "No audit log yet: nothing to verify" in r.output
    assert r.exception is None
    assert not db.exists()


def test_database_without_schema_is_ok_with_zero_entries(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # what an earlier failed run used to leave behind
    r = _invoke(monkeypatch, db)
    assert r.exit_code == 0, r.output
    assert "Audit chain OK (0 entries)" in r.output
    assert verify_chain(db) == (True, 0, None)


def test_initialised_database_with_no_events_is_ok(tmp_path, monkeypatch):
    db = tmp_path / "fresh.db"
    open_store(db).close()
    r = _invoke(monkeypatch, db)
    assert r.exit_code == 0, r.output
    assert "Audit chain OK" in r.output
