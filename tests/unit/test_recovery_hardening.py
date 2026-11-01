"""Review-pass hardening for lockout recovery, mailbox removal and stdin parsing."""

from __future__ import annotations

import io
import logging
import os
import sys

import keyring
import pytest
from keyring.backend import KeyringBackend
from typer.testing import CliRunner

from mailforge import accounts as accounts_mod
from mailforge import cli, recovery
from mailforge import secrets as secrets_mod
from mailforge.config import IMAPAccount, Settings

posix_only = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership/symlink semantics")


class MemKr(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def set_password(self, s, u, p):
        self.items[(s, u)] = p

    def get_password(self, s, u):
        return self.items.get((s, u))

    def delete_password(self, s, u):
        self.items.pop((s, u), None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("MAILFORGE_HOME", str(root))
    monkeypatch.delenv("MAILFORGE_UI_PORT", raising=False)
    monkeypatch.setitem(Settings.model_config, "toml_file", str(root / "config" / "config.toml"))
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def kr(monkeypatch):
    previous = keyring.get_keyring()
    backend = MemKr()
    keyring.set_keyring(backend)
    monkeypatch.setattr(secrets_mod, "_fallback_configured", True)
    yield backend
    keyring.set_keyring(previous)


def _no_store(monkeypatch):
    monkeypatch.setattr(recovery, "reset_credential_store", lambda s, **k: recovery.ResetReport("nothing"))


# 1 ------------------------------------------------------------------------ #
def test_token_rotate_reports_failure_and_exits_nonzero(home, monkeypatch):
    from mailforge.ui import app as ui_app

    ui_app.launcher_key()

    def boom(path, value):
        raise OSError("read-only")

    monkeypatch.setattr(ui_app, "_write_secret_file", boom)
    r = CliRunner().invoke(cli.app, ["token", "rotate", "--yes"])
    assert r.exit_code == 1
    assert "launcher key: NOT rotated" in r.output and "Rotated." not in r.output


def test_launch_route_rotation_stays_best_effort(home, monkeypatch):
    from mailforge.ui import app as ui_app

    monkeypatch.setattr(ui_app, "_write_secret_file", lambda *a: (_ for _ in ()).throw(OSError("x")))
    assert ui_app.rotate_launcher_key()  # logs, does not raise
    with pytest.raises(OSError):
        ui_app.rotate_launcher_key(strict=True)


# 2 ------------------------------------------------------------------------ #
def test_sentinel_with_bom_is_understood(home, monkeypatch):
    _no_store(monkeypatch)
    (home / "RESET_LOGIN").write_bytes(b"\xef\xbb\xbfunlock\r\n")
    res = recovery.consume_reset_sentinel(Settings(imap_accounts=[]))
    assert [r.action for r in res] == ["unlock"]


@posix_only
def test_symlinked_sentinel_is_left_alone(home, tmp_path, caplog):
    target = tmp_path / "elsewhere"
    target.write_text("all", encoding="utf-8")
    (home / "RESET_LOGIN").symlink_to(target)
    with caplog.at_level(logging.WARNING):
        assert recovery.consume_reset_sentinel(Settings(imap_accounts=[])) == []
    assert (home / "RESET_LOGIN").is_symlink() and target.exists()
    assert "leaving it alone" in caplog.text


@posix_only
def test_sentinel_owned_by_someone_else_is_ignored(home, monkeypatch, caplog):
    (home / "RESET_LOGIN").write_text("all", encoding="utf-8")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(home / "RESET_LOGIN").st_uid + 1)
    with caplog.at_level(logging.WARNING):
        assert recovery.consume_reset_sentinel(Settings(imap_accounts=[])) == []
    assert (home / "RESET_LOGIN").exists() and "not a regular file owned by this user" in caplog.text


# 3 ------------------------------------------------------------------------ #
def test_two_sentinels_act_once_on_the_union(home, monkeypatch):
    calls = {"store": 0, "ui": 0}

    def store(settings, **k):
        calls["store"] += 1
        return recovery.ResetReport("nothing")

    def ui():
        calls["ui"] += 1
        return {"launcher key": None, "cookie secret": None}

    monkeypatch.setattr(recovery, "reset_credential_store", store)
    monkeypatch.setattr(recovery, "rotate_ui_access", ui)
    (home / "RESET_LOGIN").write_text("", encoding="utf-8")
    (home / "data" / "RESET_LOGIN").write_text("unlock", encoding="utf-8")
    res = recovery.consume_reset_sentinel(Settings(imap_accounts=[]))
    assert len(res) == 2 and calls == {"store": 1, "ui": 1}
    assert not (home / "RESET_LOGIN").exists() and not (home / "data" / "RESET_LOGIN").exists()


# 4 ------------------------------------------------------------------------ #
def test_container_flag_alone_does_not_make_a_native_store_dedicated(home, monkeypatch, tmp_path):
    monkeypatch.setattr(recovery, "in_container", lambda: True)
    shared = tmp_path / "native" / "python_keyring" / "cryptfile_pass.cfg"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"x")
    assert recovery.store_is_dedicated(shared) is False
    assert recovery.store_is_dedicated(home / "xdg" / "cryptfile_pass.cfg") is True


# 5 ------------------------------------------------------------------------ #
def test_stdin_secrets_strip_carriage_returns(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("imap-pw\r\nsmtp-pw\r\n"))
    assert cli._read_stdin_secrets() == ("imap-pw", "smtp-pw")
    monkeypatch.setattr(sys, "stdin", io.StringIO("only\r\n"))
    assert cli._read_stdin_secrets() == ("only", None)


# 6 ------------------------------------------------------------------------ #
def _acct():
    return IMAPAccount(name="a", username="a@x.test", host="imap.x.test", site_id="main")


def test_remove_account_survives_a_failing_purge(home, kr, monkeypatch):
    settings = Settings(imap_accounts=[_acct()])
    monkeypatch.setattr(Settings, "assert_invariants", lambda self: None)
    monkeypatch.setattr(Settings, "save_removed_account", lambda self, n, path=None: None)

    def locked(kind, key):
        raise RuntimeError("cryptfile locked: SECRETVALUE")

    monkeypatch.setattr(secrets_mod, "delete_secret", locked)
    failures: list[str] = []
    accounts_mod.remove_account(settings, "a", purge_failures=failures)
    assert settings.imap_accounts == [] and failures == ["RuntimeError"]


def test_account_remove_cli_says_credentials_not_purged(home, kr, monkeypatch):
    s = Settings(imap_accounts=[_acct()])
    monkeypatch.setattr("mailforge.config.load_settings", lambda: s)

    def fake_remove(settings, name, *, purge_secrets=True, purge_failures=None):
        purge_failures.append("RuntimeError")
        return settings.imap_accounts[0]

    monkeypatch.setattr(accounts_mod, "remove_account", fake_remove)
    r = CliRunner().invoke(cli.app, ["account-remove", "a", "--yes"])
    assert r.exit_code == 0
    assert "mailbox removed; credentials could not be purged: run `mailforge unlock status`" in r.output


def test_set_account_password_error_names_only_the_class(home, kr, monkeypatch):
    settings = Settings(imap_accounts=[_acct()])

    def bad(name, secret):
        raise RuntimeError(f"backend echoed {secret}")

    monkeypatch.setattr(secrets_mod, "set_imap_secret", bad)
    with pytest.raises(accounts_mod.AccountError) as ei:
        accounts_mod.set_account_password(settings, "a", "TOPSECRET")
    assert str(ei.value) == "Could not save the credential: RuntimeError"


# 7 ------------------------------------------------------------------------ #
def test_account_list_never_reads_secrets_when_store_needs_a_typed_password(home, kr, monkeypatch):
    s = Settings(imap_accounts=[_acct()])
    monkeypatch.setattr("mailforge.config.load_settings", lambda: s)
    monkeypatch.setattr(recovery, "probe_unlock", lambda *a, **k: "not checked (typed password)")

    def no_read(*a, **k):
        raise AssertionError("account-list must not read secrets here")

    monkeypatch.setattr(secrets_mod, "get_imap_secret", no_read)
    monkeypatch.setattr(secrets_mod, "get_smtp_secret", no_read)
    r = CliRunner().invoke(cli.app, ["account-list"])
    assert r.exit_code == 0 and "credentials not checked" in r.output


# 8 ------------------------------------------------------------------------ #
def test_token_rotate_wording_mentions_restart():
    r = CliRunner().invoke(cli.app, ["token", "rotate", "--help"])
    assert "next restart" in " ".join(r.output.split())


# 9 ------------------------------------------------------------------------ #
def test_remove_account_does_not_freeze_env_values_into_config(home, kr, monkeypatch):
    cfg = home / "config" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        'keep_me = "yes"\n\n'
        '[[imap_accounts]]\nname = "a"\nusername = "a@x.test"\nhost = "imap.x.test"\nsite_id = "s"\n\n'
        '[[imap_accounts]]\nname = "b"\nusername = "b@x.test"\nhost = "imap.x.test"\nsite_id = "s"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MAILFORGE_LLM__LM_STUDIO_HOST", "http://env-host:1234")
    s = Settings(imap_accounts=[_acct(), IMAPAccount(name="b", username="b@x.test", host="imap.x.test", site_id="main")])
    monkeypatch.setattr(Settings, "assert_invariants", lambda self: None)
    accounts_mod.remove_account(s, "a")
    import tomllib

    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert "llm" not in data and "env-host" not in cfg.read_text(encoding="utf-8")
    assert data["keep_me"] == "yes" and [a["name"] for a in data["imap_accounts"]] == ["b"]
