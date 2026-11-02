"""Per-mailbox signatures, click-to-copy addresses and the link reveal."""

from __future__ import annotations

import json

import pytest

from mailforge import signatures as sg
from mailforge.bridge_cli import OPS, _compose_draft, _message_json
from mailforge.config import Settings
from mailforge.db.store import open_store, register_sites
from mailforge.ui import links, theme


@pytest.fixture(autouse=True)
def _sites():
    register_sites(["main"])
    yield
    register_sites(["main"])


def _settings(**over) -> Settings:
    data = dict(
        imap_accounts=[
            {"name": "sales", "site_id": "main", "host": "h", "username": "sales@example.test",
             "signature": "Sam, Sales"},
            {"name": "info", "site_id": "main", "host": "h", "username": "info@example.test"},
        ],
        smtp={"host": "s", "port": 587, "username": "sales@example.test"},
        style={"signature": "The Team"},
        openclaw={"agent_compose_per_day": 20},
    )
    data.update(over)
    return Settings(**data)


# --- selection / fallback ---------------------------------------------------
def test_mailbox_signature_wins_and_global_is_fallback():
    s = _settings()
    assert sg.signature_for(s, "SALES@example.test") == "Sam, Sales"
    assert sg.signature_for(s, "sales") == "Sam, Sales"  # by account name
    assert sg.signature_for(s, "info@example.test") == "The Team"
    assert sg.signature_for(s, "unknown@x.test") == "The Team"
    assert sg.signature_for(_settings(style={}), "info@example.test") == ""


def test_signature_round_trips_through_config_toml():
    again = Settings.model_validate(__import__("tomllib").loads(_settings().to_toml()))
    assert again.imap_accounts[0].signature == "Sam, Sales"


# --- no double, replace only our block ---------------------------------------
def test_append_never_doubles():
    once = sg.append_signature("Hi there", "Sam")
    assert once == "Hi there\n\n-- \nSam"
    assert sg.append_signature(once, "Sam") == once
    assert sg.append_signature("Thanks,\nSam", "Sam") == "Thanks,\nSam"
    assert sg.append_signature("Hi", "") == "Hi"


def test_empty_body_gets_just_the_block():
    assert sg.append_signature("", "Sam") == "\n\n-- \nSam"


def test_swap_replaces_only_our_block():
    body = sg.append_signature("Hello", "Sam")
    assert sg.swap_signature(body, "Sam", "Pat") == "Hello\n\n-- \nPat"
    assert sg.swap_signature(body, "Sam", "") == "Hello"


def test_swap_leaves_user_text_alone():
    # the user typed their own sign-off, or edited our block: untouched
    assert sg.swap_signature("Hello\n\nCheers, Sam", "Sam", "Pat") == "Hello\n\nCheers, Sam"
    edited = "Hello\n\n-- \nSam (edited)"
    assert sg.swap_signature(edited, "Sam", "Pat") == edited
    # a signature-looking line in the middle is not ours to touch
    mid = "A\n\n-- \nSam\n\nmore words"
    assert sg.swap_signature(mid, "Sam", "Pat") == mid


# --- bridge ---------------------------------------------------------------------
def _payload(**over):
    d = {"to": "support@vendor.test", "subject": "Firmware", "author": "bot",
         "body": "Hello, could you send the latest firmware package? Thank you."}
    d.update(over)
    return d


def test_bridge_compose_appends_mailbox_signature(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        out = _compose_draft(_payload(), "main", store, _settings())
        assert store.get_manual_draft(out["draft_id"])["body"].endswith("\n\n-- \nSam, Sales")
        out = _compose_draft(_payload(**{"from": "info@example.test"}), "main", store, _settings())
        assert store.get_manual_draft(out["draft_id"])["body"].endswith("\n\n-- \nThe Team")


def test_bridge_compose_no_double_and_opt_out(tmp_path):
    with open_store(tmp_path / "b.db") as store:
        signed = _payload(body="Hello, please send the firmware. Thanks.\nSam, Sales")
        out = _compose_draft(signed, "main", store, _settings())
        assert store.get_manual_draft(out["draft_id"])["body"].count("Sam, Sales") == 1
        out = _compose_draft(_payload(add_signature=False), "main", store, _settings())
        assert "Sam" not in store.get_manual_draft(out["draft_id"])["body"]


def test_bridge_guardrails_see_the_signed_text(tmp_path):
    from mailforge.bridge_cli import BridgeInputError

    s = _settings(imap_accounts=[
        {"name": "sales", "site_id": "main", "host": "h", "username": "sales@example.test",
         "signature": "Call me: password: hunter2hunter2"}])
    with open_store(tmp_path / "c.db") as store, pytest.raises(BridgeInputError):
        _compose_draft(_payload(body="Hello, here is the update you asked for. Regards."),
                       "main", store, s)


# --- copy handler ---------------------------------------------------------------
def test_bare_address_extraction():
    assert theme.bare_address("Ann Lee <ann@example.com>") == "ann@example.com"
    assert theme.bare_address("ann@example.com") == "ann@example.com"
    assert theme.bare_address("not an address") == ""


def test_copy_handler_carries_exact_address_outside_the_js():
    from nicegui import ui
    from nicegui import Client

    with Client(ui.page("/")):
        lab = theme.copyable_address("Ann Lee <ann@example.com>", "Ann")
    assert lab._props["data-copy-addr"] == "ann@example.com"
    assert lab._props["title"] == "Click to copy"
    assert "ann@example.com" not in theme.COPY_ADDRESS_JS
    assert "stopPropagation" in theme.COPY_ADDRESS_JS
    assert "click" in {x.type for x in lab._event_listeners.values()}


# --- link reveal ----------------------------------------------------------------
def test_only_http_https_mailto_are_clickable():
    for url, ok in [("https://a.test/x", True), ("http://a.test", True),
                    ("mailto:bob@a.test", True), ("javascript:alert(1)", False),
                    ("data:text/html,<script>", False), ("file:///etc/passwd", False),
                    ("ftp://a.test", False), ("//a.test", False)]:
        assert links.analyze("[link_0]", url).clickable is ok, url


def test_flags_mismatch_http_ip_and_lookalike():
    f = links.analyze("[link_0]", "https://evil.test/x", "www.mybank.com").flags
    assert any("mybank.com" in x and "evil.test" in x for x in f)
    assert not links.analyze("[link_0]", "https://www.mybank.com/a", "mybank.com").flags
    assert any("encrypted" in x for x in links.analyze("s", "http://a.test").flags)
    assert any("number" in x for x in links.analyze("s", "https://" + ".".join(["8"] * 4) + "/x").flags)
    assert any("Look-alike" in x for x in links.analyze("s", "https://xn--pple-43d.com/").flags)
    assert any("Look-alike" in x for x in links.analyze("s", "https://аpple.com/").flags)


def test_hostile_url_is_data_never_markup():
    url = 'https://a.test/"><script>alert(1)</script>\'x'
    info = links.analyze("[link_0]", url)
    body = "Click [link_0] now"
    parts = links.split_body(body, [info])
    assert parts == [("text", "Click "), ("link", info), ("text", " now")]
    from nicegui import Client, ui

    with Client(ui.page("/")):
        a = links.anchor(info)
    assert a._props["href"] == url  # carried as a serialised prop, not spliced into HTML
    assert a._props["rel"] == "noopener noreferrer nofollow"
    assert a._props["referrerpolicy"] == "no-referrer"
    assert a._props.get("target") == "_blank"
    with Client(ui.page("/")):
        dead = links.anchor(links.analyze("[link_1]", "javascript:alert(1)"))
    assert "href" not in dead._props


def test_risky_screening_flag():
    assert links.is_risky("SPAM", 0) and links.is_risky("POTENTIAL_ISSUE", 0)
    assert links.is_risky("POTENTIAL_SPAM", 0) and links.is_risky("CONTENT", 1)
    assert not links.is_risky("CONTENT", 0)


def test_reveal_is_audited_without_urls(tmp_path):
    with open_store(tmp_path / "d.db") as store:
        links.audit_revealed(store, 7, 3)
        row = store.conn.execute(
            "SELECT actor,event,subject_table,subject_id,detail_json FROM audit_log"
        ).fetchone()
        assert tuple(row[:4]) == ("user", "links_revealed", "messages", 7)
        assert json.loads(row[4]) == {"count": 3}


def test_store_lists_links_in_order(tmp_path):
    with open_store(tmp_path / "e.db") as store:
        store.conn.execute("PRAGMA foreign_keys=OFF")
        store.add_links(1, [("[link_0]", "https://a.test", "A"), ("[link_1]", "http://b.test", "")])
        assert [x.target for x in links.load_links(store, 1)] == ["https://a.test", "http://b.test"]


def test_bridge_get_still_shows_tokens_only():
    row = {"id": 1, "site_id": "main", "account_name": "a", "from_addr": "x@y.test",
           "from_name": "X", "subject": "s", "to_addrs": "", "received_at": "",
           "has_attachments": 0, "seen": 0, "archived": 0, "trashed": 0, "quarantined": 0,
           "screening_status": "CONTENT", "screening_reason": "", "purged_at": None,
           "sanitized_text": "Pay here [link_0]", "thread_id": "t", "link_count": 1}
    out = json.dumps(_message_json(row, full=True))
    assert "[link_0]" in out and "http" not in out
    assert not any("link" in name or "reveal" in name for name in OPS)
