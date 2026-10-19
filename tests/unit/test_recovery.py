"""Accounts and lockout recovery: users verbs, unlock reset, token, RESET_LOGIN sentinel.

Every test runs against a throwaway MAILFORGE_HOME and an in-memory / tmp-file keyring,
never the machine's real credential store.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from typer.testing import CliRunner

from mailforge import cli, recovery
from mailforge import secrets as secrets_mod
from mailforge.config import Settings

MAILBOX_PW = "hunter2-very-secret"


class MemoryKeyring(KeyringBackend):
    """OS-keyring stand-in (no file on disk)."""

    priority = 1

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def get_password(self, service, username):
        return self.items.get((service, username))

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.items:
            raise PasswordDeleteError("not found")
        del self.items[(service, username)]


class FileKeyring(MemoryKeyring):
    """Stand-in for the cryptfile backend: has a ``file_path`` that exists once written."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.file_path = str(path)
        self._keyring_key = "cached"

    def set_password(self, service, username, password):
        super().set_password(service, username, password)
        Path(self.file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.file_path).write_bytes(b"ENCRYPTED-STORE")


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("MAILFORGE_HOME", str(root))
    monkeypatch.delenv("MAILFORGE_UI_PORT", raising=False)
    cfg = root / "config" / "config.toml"
    monkeypatch.setitem(Settings.model_config, "toml_file", str(cfg))
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def kr(monkeypatch):
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    monkeypatch.setattr(secrets_mod, "_fallback_configured", True)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def file_kr(home, monkeypatch):
    previous = keyring.get_keyring()
    backend = FileKeyring(home / "xdg" / "python_keyring" / "cryptfile_pass.cfg")
    keyring.set_keyring(backend)
    monkeypatch.setattr(secrets_mod, "_fallback_configured", True)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def runner():
    return CliRunner()


def _add(runner, args, **kw):
    return runner.invoke(
        cli.app,
        [
            "users", "add",
            "--username", "sam@shop.example.com", "--site", "main",
            "--imap-host", "imap.shop.example.com", "--imap-port", "993",
            *args,
        ],
        **kw,
    )


# --------------------------------------------------------------------------- #
# users list | add | passwd | delete
# --------------------------------------------------------------------------- #
def test_users_verbs_end_to_end(home, kr, runner, monkeypatch):
    monkeypatch.setenv("MF_TEST_PW", MAILBOX_PW)
    r = _add(runner, ["support", "--password-env", "MF_TEST_PW", "--no-input"])
    assert r.exit_code == 0, r.output
    assert "Added sam@shop.example.com as 'support'" in r.output
    assert MAILBOX_PW not in r.output
    assert kr.items[("mailforge.imap.support", "support")] == MAILBOX_PW
    assert kr.items[("mailforge.smtp.sam@shop.example.com", "sam@shop.example.com")] == MAILBOX_PW

    r = runner.invoke(cli.app, ["users", "list"])
    assert r.exit_code == 0 and "support" in r.output and "[ok]" in r.output
    assert MAILBOX_PW not in r.output

    r = runner.invoke(cli.app, ["users", "passwd", "support", "--password-stdin"], input="new-pw\nignored\n")
    assert r.exit_code == 0, r.output
    assert kr.items[("mailforge.imap.support", "support")] == "new-pw"
    assert kr.items[("mailforge.smtp.sam@shop.example.com", "sam@shop.example.com")] == "new-pw"

    r = runner.invoke(cli.app, ["users", "delete", "support", "--yes"])
    assert r.exit_code == 0, r.output
    assert kr.items == {}
    r = runner.invoke(cli.app, ["users", "list"])
    assert "No mailboxes configured" in r.output


def test_add_accepts_name_option_and_stdin_two_lines(home, kr, runner):
    r = _add(runner, ["--name", "support", "--password-stdin"], input="imap-pw\nsmtp-pw\n")
    assert r.exit_code == 0, r.output
    assert kr.items[("mailforge.imap.support", "support")] == "imap-pw"
    assert kr.items[("mailforge.smtp.sam@shop.example.com", "sam@shop.example.com")] == "smtp-pw"


def test_account_add_alias_takes_password_env(home, kr, runner, monkeypatch):
    monkeypatch.setenv("MF_TEST_PW", MAILBOX_PW)
    r = runner.invoke(
        cli.app,
        ["account-add", "--name", "sales", "--username", "a@shop.example.com", "--site", "main",
         "--imap-host", "imap.shop.example.com", "--password-env", "MF_TEST_PW"],
    )
    assert r.exit_code == 0, r.output
    assert ("mailforge.imap.sales", "sales") in kr.items


def test_password_flags_are_exclusive_and_env_must_be_set(home, kr, runner, monkeypatch):
    monkeypatch.delenv("MF_NOPE", raising=False)
    r = _add(runner, ["support", "--password-env", "MF_NOPE", "--no-input"])
    assert r.exit_code == 2 and "MF_NOPE" in r.output
    r = _add(runner, ["support", "--password-env", "MF_NOPE", "--password-stdin"], input="x\n")
    assert r.exit_code == 2 and "not both" in r.output
    assert kr.items == {}


def test_passwd_unknown_mailbox_changes_nothing(home, kr, runner):
    r = runner.invoke(cli.app, ["users", "passwd", "ghost", "--password-stdin"], input="pw\n")
    assert r.exit_code == 2 and "No mailbox named 'ghost'" in r.output
    assert kr.items == {}
    r = runner.invoke(cli.app, ["users", "passwd", "ghost", "--password-stdin"], input="\n")
    assert r.exit_code == 2  # empty stdin is refused before anything is looked up


def test_every_command_says_which_data_it_acts_on(home, kr, runner):
    for argv in (["users", "list"], ["unlock", "status"], ["token", "show"]):
        r = runner.invoke(cli.app, argv)
        assert r.exit_code == 0, (argv, r.output)
        assert f"data={home / 'data'}" in r.output and "MAILFORGE_HOME=" in r.output


# --------------------------------------------------------------------------- #
# unlock status | reset
# --------------------------------------------------------------------------- #
def _with_mailbox(runner, monkeypatch):
    monkeypatch.setenv("MF_TEST_PW", MAILBOX_PW)
    r = _add(runner, ["support", "--password-env", "MF_TEST_PW", "--no-input"])
    assert r.exit_code == 0, r.output


def test_unlock_reset_file_store_keeps_config_and_db(home, file_kr, runner, monkeypatch):
    _with_mailbox(runner, monkeypatch)
    store = Path(file_kr.file_path)
    cfg = home / "config" / "config.toml"
    db = home / "data" / "agent.db"
    assert store.exists() and cfg.exists() and db.exists()
    cfg_before, db_before = cfg.read_bytes(), db.read_bytes()

    r = runner.invoke(cli.app, ["unlock", "status"])
    assert r.exit_code == 0 and "present" in r.output and "Backend:" in r.output

    declined = runner.invoke(cli.app, ["unlock", "reset"], input="n\n")
    assert declined.exit_code == 1 and store.exists()

    r = runner.invoke(cli.app, ["unlock", "reset", "--yes"])
    assert r.exit_code == 0, r.output
    assert not store.exists()
    backups = list(store.parent.glob("cryptfile_pass.cfg.reset-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"ENCRYPTED-STORE"
    assert cfg.read_bytes() == cfg_before and db.read_bytes() == db_before
    assert "account-passwd" in r.output


def test_unlock_reset_no_backup_deletes(home, file_kr, runner, monkeypatch):
    _with_mailbox(runner, monkeypatch)
    r = runner.invoke(cli.app, ["unlock", "reset", "--yes", "--no-backup"])
    assert r.exit_code == 0
    assert list(Path(file_kr.file_path).parent.iterdir()) == []


def test_unlock_reset_os_keyring_removes_only_mailforge_entries(home, kr, runner, monkeypatch):
    _with_mailbox(runner, monkeypatch)
    kr.set_password("some.other.app", "me", "keep-me")
    r = runner.invoke(cli.app, ["unlock", "reset", "--yes"])
    assert r.exit_code == 0, r.output
    assert kr.items == {("some.other.app", "me"): "keep-me"}
    r = runner.invoke(cli.app, ["unlock", "status"])
    assert "OS keyring" in r.output and "1 configured, 1 without a stored credential" in r.output
    # mailbox is still configured; account-passwd restores it
    r = runner.invoke(cli.app, ["users", "passwd", "support", "--password-stdin"], input="again\n")
    assert r.exit_code == 0
    assert kr.items[("mailforge.imap.support", "support")] == "again"


def test_forgotten_password_end_to_end_with_real_cryptfile(home, runner, monkeypatch, tmp_path):
    """Create a real encrypted store, lose its password, reset, store works under a new one."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ck_under_test", Path(__file__).resolve().parents[2] / "deploy" / "container_keyring.py"
    )
    ck = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ck)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    path = tmp_path / "cryptfile_pass.cfg"

    def backend(password: str):
        monkeypatch.setenv("MAILFORGE_KEYRING_KEY", password)
        kr = ck.EnvCryptFileKeyring()
        kr.file_path = str(path)
        kr.time_cost, kr.memory_cost = 1, 1024
        return kr

    backend("first-password").set_password("mailforge.imap.x", "x", "imap-secret")
    lost = backend("a-different-password")
    assert recovery.probe_unlock(lost).startswith("LOCKED")
    assert recovery.probe_unlock(backend("first-password")) == "unlocked"

    report = recovery.reset_credential_store(Settings(imap_accounts=[]), backend=lost)
    assert report.kind == "file" and report.backup is not None and not path.exists()

    fresh = backend("second-password")
    fresh.set_password("mailforge.imap.x", "x", "new-secret")
    assert fresh.get_password("mailforge.imap.x", "x") == "new-secret"
    assert recovery.probe_unlock(fresh) == "unlocked"


def test_old_reset_backups_are_pruned(home, file_kr):
    store = Path(file_kr.file_path)
    store.parent.mkdir(parents=True, exist_ok=True)
    for day in range(1, 6):
        (store.parent / f"{store.name}.reset-2020010{day}T000000Z.bak").write_bytes(b"old")
    store.write_bytes(b"current")
    recovery.reset_credential_store(Settings(imap_accounts=[]), backend=file_kr)
    names = sorted(p.name for p in store.parent.glob("*.bak"))
    assert len(names) == 3
    assert names[0].endswith("20200105T000000Z.bak") or "20200104" in names[0]
    assert not any("20200101" in n or "20200102" in n or "20200103" in n for n in names)
    assert "reset-2020" not in names[-1]  # the fresh backup is the newest and kept


# --------------------------------------------------------------------------- #
# token show | rotate
# --------------------------------------------------------------------------- #
def test_token_show_prints_only_the_url_on_stdout(home, runner):
    r = runner.invoke(cli.app, ["token", "show"])
    assert r.exit_code == 0, r.output
    from mailforge.ui.app import launcher_key, persistent_port

    assert r.stdout.strip() == f"http://127.0.0.1:{persistent_port()}/launch?k={launcher_key()}"


def test_token_rotate_changes_both_secrets_and_signs_browsers_out(home, runner):
    from mailforge.ui.app import _load_or_create_storage_secret, launcher_key

    key, cookie = launcher_key(), _load_or_create_storage_secret()
    assert runner.invoke(cli.app, ["token", "rotate"], input="n\n").exit_code == 1
    assert launcher_key() == key
    r = runner.invoke(cli.app, ["token", "rotate", "--yes"])
    assert r.exit_code == 0, r.output
    assert launcher_key() != key and _load_or_create_storage_secret() != cookie
    assert key not in r.output and cookie not in r.output


# --------------------------------------------------------------------------- #
# RESET_LOGIN sentinel
# --------------------------------------------------------------------------- #
def _ui_files(home):
    d = home / "data"
    return {n: (d / n).read_bytes() for n in ("ui_launcher_key", "ui_storage_secret")}


@pytest.fixture
def armed(home, file_kr):
    """A configured install with a credential store and UI keys, ready to reset."""
    from mailforge.ui.app import _load_or_create_storage_secret, launcher_key

    launcher_key()
    _load_or_create_storage_secret()
    file_kr.set_password("mailforge.imap.x", "x", "s")
    (home / "data" / "agent.db").write_bytes(b"MAIL")
    return home


def test_empty_sentinel_resets_store_and_ui_keys_then_is_consumed(armed, file_kr):
    sentinel = armed / "RESET_LOGIN"  # <MAILFORGE_HOME>/RESET_LOGIN == the Docker volume root
    sentinel.write_text("", encoding="utf-8")
    before = _ui_files(armed)
    res = recovery.consume_reset_sentinel(Settings(imap_accounts=[]))
    assert [r.action for r in res] == ["all"]
    assert not sentinel.exists()
    assert not Path(file_kr.file_path).exists()
    after = _ui_files(armed)
    assert after["ui_launcher_key"] != before["ui_launcher_key"]
    assert after["ui_storage_secret"] != before["ui_storage_secret"]
    assert (armed / "data" / "agent.db").read_bytes() == b"MAIL"  # mail database kept


def test_sentinel_in_the_data_dir_works_too(armed, file_kr):
    (armed / "data" / "RESET_LOGIN").write_text("unlock\n", encoding="utf-8")
    before = _ui_files(armed)
    res = recovery.consume_reset_sentinel(Settings(imap_accounts=[]))
    assert [r.action for r in res] == ["unlock"]
    assert not Path(file_kr.file_path).exists()
    assert _ui_files(armed) == before  # "unlock" leaves the UI keys alone


def test_ui_only_sentinel_leaves_credentials(armed, file_kr):
    (armed / "RESET_LOGIN").write_text(" UI ", encoding="utf-8")
    before = _ui_files(armed)
    recovery.consume_reset_sentinel(Settings(imap_accounts=[]))
    assert Path(file_kr.file_path).exists()
    assert _ui_files(armed)["ui_launcher_key"] != before["ui_launcher_key"]


def test_sentinel_is_consumed_and_cannot_replay(armed, file_kr):
    (armed / "RESET_LOGIN").write_text("", encoding="utf-8")
    recovery.consume_reset_sentinel(Settings(imap_accounts=[]))
    file_kr.set_password("mailforge.imap.y", "y", "fresh")  # user re-enters a credential
    keys = _ui_files(armed)
    assert recovery.consume_reset_sentinel(Settings(imap_accounts=[])) == []
    assert Path(file_kr.file_path).exists() and _ui_files(armed) == keys


def test_unknown_content_is_consumed_but_resets_nothing_and_is_not_logged(armed, file_kr, caplog):
    (armed / "RESET_LOGIN").write_text("admin:hunter2-typo", encoding="utf-8")
    keys = _ui_files(armed)
    with caplog.at_level(logging.INFO):
        res = recovery.consume_reset_sentinel(Settings(imap_accounts=[]))
    assert res[0].action is None
    assert not (armed / "RESET_LOGIN").exists()
    assert Path(file_kr.file_path).exists() and _ui_files(armed) == keys
    assert "hunter2" not in caplog.text and "admin" not in caplog.text


def test_undeletable_sentinel_is_not_acted_on(armed, file_kr, monkeypatch, caplog):
    sentinel = armed / "RESET_LOGIN"
    sentinel.write_text("", encoding="utf-8")
    real_unlink = Path.unlink

    def deny(self, *a, **k):
        if self.name == "RESET_LOGIN":
            raise PermissionError("read-only volume")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", deny)
    keys = _ui_files(armed)
    with caplog.at_level(logging.ERROR):
        assert recovery.consume_reset_sentinel(Settings(imap_accounts=[])) == []
    assert sentinel.exists() and Path(file_kr.file_path).exists() and _ui_files(armed) == keys
    assert "NOT acting" in caplog.text


def test_directory_or_symlink_named_reset_login_is_ignored(armed, file_kr):
    (armed / "RESET_LOGIN").mkdir()
    assert recovery.consume_reset_sentinel(Settings(imap_accounts=[])) == []
    assert Path(file_kr.file_path).exists()


def test_a_single_log_line_per_reset(armed, caplog):
    (armed / "RESET_LOGIN").write_text("", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="mailforge.recovery"):
        recovery.consume_reset_sentinel(Settings(imap_accounts=[]))
    lines = [r for r in caplog.records if "RESET_LOGIN consumed" in r.getMessage()]
    assert len(lines) == 1


def test_serve_consumes_the_sentinel_before_anything_else_starts(armed, file_kr, monkeypatch):
    from mailforge import runtime

    (armed / "RESET_LOGIN").write_text("", encoding="utf-8")

    class Stop(Exception):
        pass

    def boom(*a, **k):
        raise Stop

    monkeypatch.setattr(runtime, "open_store", boom)
    with pytest.raises(Stop):
        runtime.run_serve(start_ui=False)
    assert not (armed / "RESET_LOGIN").exists()
    assert not Path(file_kr.file_path).exists()


# --------------------------------------------------------------------------- #
# the network cannot trigger it
# --------------------------------------------------------------------------- #
def test_http_requests_cannot_trigger_or_create_the_sentinel(armed, file_kr, tmp_path):
    from starlette.testclient import TestClient

    from mailforge.db.store import open_store
    from mailforge.ui import app as ui_app

    sentinel = armed / "RESET_LOGIN"
    keys = _ui_files(armed)
    probes = [
        ("GET", "/RESET_LOGIN"), ("GET", "/?reset_login=1"), ("GET", "/launch?k=RESET_LOGIN"),
        ("GET", "/healthz?reset=1"), ("POST", "/RESET_LOGIN"), ("POST", "/"),
        ("PUT", "/RESET_LOGIN"), ("GET", "/settings?token=RESET_LOGIN"),
    ]
    with open_store(tmp_path / "h.db") as store:
        # build_app may run once per process (the host guard is middleware); another
        # test may already have built it, in which case its port is the one to use.
        if ui_app.nicegui_app.middleware_stack is None:
            ui_app.build_app(store, Settings(imap_accounts=[]), None, port=45872)
        client = TestClient(
            ui_app.nicegui_app, base_url=f"http://127.0.0.1:{ui_app._PORT}", raise_server_exceptions=False
        )
        for armed_now in (False, True):  # no sentinel: none appears; sentinel: none is consumed
            if armed_now:
                sentinel.write_text("", encoding="utf-8")
            for method, url in probes:
                client.request(method, url, headers={"X-Reset-Login": "1"}, content=b"RESET_LOGIN")
            assert sentinel.exists() is armed_now, "a request created or consumed the sentinel"
            assert not (armed / "data" / "RESET_LOGIN").exists()
            assert Path(file_kr.file_path).exists() and _ui_files(armed) == keys


def test_only_startup_and_cli_code_reference_the_sentinel():
    """No web code (ui/) can read or write it; the only reader is run_serve."""
    src = Path(__file__).resolve().parents[2] / "src" / "mailforge"
    users = {
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "RESET_LOGIN" in p.read_text(encoding="utf-8")
        or "consume_reset_sentinel" in p.read_text(encoding="utf-8")
    }
    assert users == {"recovery.py", "runtime.py"}
    assert not any(u.startswith("ui/") for u in users)
