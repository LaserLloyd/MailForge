"""Regression tests for select-value crashes (NiceGUI 500 pages).

``ui.select`` raises ``ValueError: Invalid value`` when its value is not one of
its options, and NiceGUI turns that into a 500 page. "Mark as spam" used to
redirect to ``/mail?tag=spam`` — a tag the picker deliberately hides because the
"Filtered spam" view covers it — so every spam click ended on a 500.
"""

from __future__ import annotations

import inspect

import pytest

from mailforge.ui import app as ui_app
from mailforge.ui.pages import activity, compose, dashboard, mail


COUNTS = {
    "spam": 3,
    "quarantined": 2,
    "category:RESPOND": 4,
    "screening:POTENTIAL_SPAM": 5,
}


@pytest.mark.parametrize("tag", sorted(COUNTS))
def test_every_live_tag_resolves_to_a_selectable_value(tag):
    _filter, _tag, value = mail.resolve_view("all", tag, COUNTS)
    assert value in mail.filter_options(COUNTS)


@pytest.mark.parametrize("tag", ["spam", "quarantined"])
def test_view_covered_tags_become_their_view(tag):
    assert mail.resolve_view("all", tag, COUNTS) == (tag, "", f"view:{tag}")


@pytest.mark.parametrize(
    "filter,tag",
    [
        ("all", "category:GONE"),  # stale tag from an old URL
        ("all", "nonsense"),
        ("bogus-view", ""),
        ("all", "category:RESPOND\x00"),
    ],
)
def test_unknown_values_fall_back_to_all(filter, tag):
    f, t, value = mail.resolve_view(filter, tag, COUNTS)
    assert value in mail.filter_options(COUNTS)
    assert (f, t) == ("all", "")


def test_zero_count_tag_is_dropped():
    counts = {"category:RESPOND": 0}
    assert mail.resolve_view("unread", "category:RESPOND", counts) == (
        "unread",
        "",
        "view:unread",
    )


def test_spam_tag_with_no_spam_left_still_resolves():
    # The spam count can be zero by the time the redirect lands.
    _f, _t, value = mail.resolve_view("all", "spam", {})
    assert value == "view:spam"
    assert value in mail.filter_options({})


def test_mark_as_spam_lands_on_the_spam_view_not_the_hidden_tag():
    assert mail.SPAM_VIEW_URL == "/mail?filter=spam"
    source = inspect.getsource(mail._message_reader)
    assert '"tag": "spam"' not in source
    assert "SPAM_VIEW_URL" in source


def test_reader_actions_do_not_navigate_straight_after_a_toast():
    # notify() immediately followed by navigate.to() loses the toast.
    source = inspect.getsource(mail._message_reader)
    lines = [ln.strip() for ln in source.splitlines()]
    for i, line in enumerate(lines[:-1]):
        if line.startswith("ui.notify(") and line.endswith(")"):
            assert not lines[i + 1].startswith("ui.navigate.to("), line


def test_unknown_account_falls_back_to_all():
    options, value = mail.account_select_options(["a", "b"], "bogus", ["a", "b"])
    assert value == ""
    assert value in options


def test_removed_account_stays_browsable_and_labelled():
    options, value = mail.account_select_options(["a"], "old", ["a", "old"])
    assert value == "old"
    assert options["old"] == "old (removed)"


def test_configured_account_is_kept():
    options, value = mail.account_select_options(["a"], "a", ["a"])
    assert value == "a" and value in options


def test_activity_filter_resets_values_missing_from_options():
    saved = dict(activity._view)
    try:
        activity._view.update(actor="agent", event="send")
        activity.reset_stale_filters(["all", "user"], ["all", "send"])
        assert activity._view == {"actor": "all", "event": "send"}
    finally:
        activity._view.clear()
        activity._view.update(saved)


def test_compose_keeps_a_cross_site_template_selectable():
    source = inspect.getsource(compose.render)
    assert "selected_tid not in template_options" in source


def test_dashboard_delete_notice_describes_retention():
    source = inspect.getsource(dashboard._BulkSelection.apply)
    assert "provider mailbox was not changed" not in source
    assert "_delete_notice" in source


@pytest.mark.parametrize(
    "raw,expected",
    [("", None), (None, None), ("abc", None), ("12", 12), (" 7 ", 7), ("-3", -3)],
)
def test_query_int_parsing_never_raises(raw, expected):
    assert ui_app._int_or_none(raw) == expected


def _guard(port=8765):
    captured = {}

    class FakeApp:
        def add_middleware(self, cls):
            captured["cls"] = cls

    import mailforge.ui.app as mod

    orig = mod.nicegui_app
    mod.nicegui_app = FakeApp()
    try:
        mod._install_host_guard(port)
    finally:
        mod.nicegui_app = orig
    return captured["cls"]


@pytest.mark.parametrize(
    "kind,headers,allowed",
    [
        ("http", {"host": "127.0.0.1:8765"}, True),
        ("http", {"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}, True),
        ("http", {"host": "127.0.0.1:8765", "origin": "https://evil.example"}, False),
        ("http", {"host": "evil.example"}, False),
        ("websocket", {"host": "localhost:8765", "origin": "http://localhost:8765"}, True),
        ("websocket", {"host": "localhost:8765", "origin": "https://evil.example"}, False),
    ],
)
def test_host_and_origin_guard(kind, headers, allowed):
    import asyncio

    calls = []

    async def inner(scope, receive, send):
        calls.append("app")

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    guard = _guard()(inner)
    scope = {
        "type": kind,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(guard(scope, receive, send))
    assert (calls == ["app"]) is allowed
