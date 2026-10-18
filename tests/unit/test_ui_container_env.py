"""The UI's bind host, port and accepted Host headers can be set from the
environment (container / reverse-proxy use); unset, nothing changes."""

from __future__ import annotations

import pytest

from mailforge import paths
import mailforge.ui.app as ui_app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (paths.UI_HOST_ENV, paths.UI_PORT_ENV, paths.UI_ALLOWED_HOSTS_ENV):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_loopback_only():
    assert paths.ui_host() == "127.0.0.1"
    assert paths.ui_port_override() is None
    assert paths.ui_extra_allowed_hosts() == set()
    assert ui_app.allowed_hosts(8765) == {"127.0.0.1:8765", "localhost:8765"}


def test_host_and_port_from_env(monkeypatch):
    monkeypatch.setenv(paths.UI_HOST_ENV, " 0.0.0.0 ")
    monkeypatch.setenv(paths.UI_PORT_ENV, "8090")
    assert paths.ui_host() == "0.0.0.0"
    assert paths.ui_port_override() == 8090
    assert ui_app.persistent_port() == 8090  # no ui_port file is written or needed


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "70000", " "])
def test_bad_port_is_ignored(monkeypatch, raw):
    monkeypatch.setenv(paths.UI_PORT_ENV, raw)
    assert paths.ui_port_override() is None


def test_extra_allowed_hosts_extend_not_replace(monkeypatch):
    monkeypatch.setenv(paths.UI_ALLOWED_HOSTS_ENV, "Localhost:18090, mail.lan:18090 ,")
    assert ui_app.allowed_hosts(8090) == {
        "127.0.0.1:8090",
        "localhost:8090",
        "localhost:18090",
        "mail.lan:18090",
    }


def test_guard_accepts_published_port_host_only_when_configured(monkeypatch):
    import asyncio

    def run(headers):
        calls = []

        class FakeApp:
            def add_middleware(self, cls):
                calls.append(cls)

        orig = ui_app.nicegui_app
        ui_app.nicegui_app = FakeApp()
        try:
            ui_app._install_host_guard(8090)
        finally:
            ui_app.nicegui_app = orig
        hit = []

        async def inner(scope, receive, send):
            hit.append(1)

        async def send(message):
            pass

        async def receive():
            return {"type": "http.request"}

        scope = {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
        asyncio.run(calls[0](inner)(scope, receive, send))
        return bool(hit)

    published = {"host": "localhost:18090", "origin": "http://localhost:18090"}
    assert run(published) is False
    monkeypatch.setenv(paths.UI_ALLOWED_HOSTS_ENV, "localhost:18090")
    assert run(published) is True
    assert run({"host": "evil.example"}) is False


def test_wildcard_bind_is_not_advertised_as_a_url_host():
    assert ui_app._display_host("0.0.0.0") == "127.0.0.1"
    assert ui_app._display_host("::") == "127.0.0.1"
    assert ui_app._display_host("192.0.2.7") == "192.0.2.7"


def test_default_ports_also_accept_the_bare_name_browsers_send(monkeypatch):
    """A browser at https://mail.example.lan sends Host: mail.example.lan and
    Origin: https://mail.example.lan (no :443); the documented name:443 form must
    not turn every proxied request into a 403."""
    monkeypatch.setenv(
        paths.UI_ALLOWED_HOSTS_ENV, "mail.example.lan:443,plain.example.lan:80,[::1]:443,x.lan:8443"
    )
    hosts = paths.ui_extra_allowed_hosts()
    assert {"mail.example.lan", "plain.example.lan", "[::1]"} <= hosts
    assert "x.lan" not in hosts  # a non-default port is never dropped
    assert ui_app.allowed_hosts(8090) >= {"mail.example.lan:443", "mail.example.lan"}
