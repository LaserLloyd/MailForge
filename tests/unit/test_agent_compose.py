"""Agent compose: a NEW email saved as a local draft — never sent by the bridge."""

from __future__ import annotations

import pytest

from mailforge.bridge_cli import BridgeInputError, _compose_draft, _prepare_send
from mailforge.config import Settings
from mailforge.db.store import open_store, register_sites


@pytest.fixture(autouse=True)
def _default_registry():
    register_sites(["main", "shop"])
    yield
    register_sites(["main", "shop"])


def _settings(per_day: int = 20, send_enabled: bool = True) -> Settings:
    return Settings(
        imap_accounts=[
            {"name": "sales", "site_id": "main", "host": "imap.example",
             "username": "sales@example.test"},
            {"name": "info", "site_id": "main", "host": "imap.example",
             "username": "info@example.test"},
            {"name": "shop", "site_id": "shop", "host": "imap.example",
             "username": "orders@shop.test"},
        ],
        smtp={"host": "smtp.example", "port": 587, "username": "sales@example.test"},
        openclaw={"agent_send_enabled": send_enabled, "agent_compose_per_day": per_day},
    )


def _payload(**over):
    data = {
        "to": "support@vendor.test",
        "subject": "Firmware for model M7",
        "body": "Hello, could you send the latest firmware package? Thank you.",
        "author": "ds_flash",
    }
    data.update(over)
    return data


def test_compose_saves_a_manual_draft_and_sends_nothing(tmp_path):
    with open_store(tmp_path / "a.db") as store:
        out = _compose_draft(_payload(), "main", store, _settings())
        draft = store.get_manual_draft(out["draft_id"])
        assert draft is not None
        assert draft["state"] == "DRAFT"
        assert draft["message_id"] is None
        assert draft["from_addr"] == "sales@example.test"  # site's first mailbox
        assert draft["recipient"] == "support@vendor.test"
        assert draft["site_id"] == "main"
        assert out["review_path"] == f"/compose?draft_id={out['draft_id']}"
        assert "nothing was sent" in out["next_step"].lower()
        # it shows up where the human looks for drafts
        assert out["draft_id"] in [int(r["id"]) for r in store.list_manual_drafts("main")]
        # and nothing reached the send ledger
        assert store.conn.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0] == 0


def test_compose_is_audited(tmp_path):
    with open_store(tmp_path / "b.db") as store:
        out = _compose_draft(_payload(), "main", store, _settings())
        row = store.conn.execute(
            "SELECT actor, event, subject_id, json_extract(detail_json,'$.tool'),"
            " json_extract(detail_json,'$.author') FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(row) == ("agent", "tool_call", out["draft_id"], "compose_draft", "ds_flash")


def test_compose_from_must_be_a_mailbox_of_this_site(tmp_path):
    with open_store(tmp_path / "c.db") as store:
        out = _compose_draft(_payload(**{"from": "INFO@example.test"}), "main", store, _settings())
        assert store.get_manual_draft(out["draft_id"])["from_addr"] == "info@example.test"
        # another site's mailbox is refused: the bridge is site-bound
        with pytest.raises(BridgeInputError, match="from must be"):
            _compose_draft(_payload(**{"from": "orders@shop.test"}), "main", store, _settings())
        with pytest.raises(BridgeInputError, match="from must be"):
            _compose_draft(_payload(**{"from": "ceo@elsewhere.test"}), "main", store, _settings())


@pytest.mark.parametrize("to", [
    "", "not-an-address", "a@b", "two@x.test, three@y.test", "x@y.test\nBcc: z@q.test",
    "Name <x@y.test>",
])
def test_compose_needs_exactly_one_plain_recipient(tmp_path, to):
    with open_store(tmp_path / "d.db") as store:
        with pytest.raises(BridgeInputError, match="to must be"):
            _compose_draft(_payload(to=to), "main", store, _settings())


@pytest.mark.parametrize("over,match", [
    ({"subject": ""}, "subject"),
    ({"subject": "line one\nBcc: x@y.test"}, "subject"),
    ({"subject": "x" * 201}, "subject"),
    ({"body": "   "}, "body"),
    ({"body": "x" * 20001}, "body"),
])
def test_compose_validates_subject_and_body(tmp_path, over, match):
    with open_store(tmp_path / "e.db") as store:
        with pytest.raises(BridgeInputError, match=match):
            _compose_draft(_payload(**over), "main", store, _settings())


def test_compose_guardrails_block_secrets_and_record_the_block(tmp_path):
    with open_store(tmp_path / "f.db") as store:
        leaked = _payload(body="Here is the key: AKIAIOSFODNN7EXAMPLE")  # scrub-check: allow
        with pytest.raises(BridgeInputError, match="guardrails blocked"):
            _compose_draft(leaked, "main", store, _settings())
        assert store.list_manual_drafts("main") == []
        row = store.conn.execute(
            "SELECT actor, event FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(row) == ("guardrail", "block")


def test_compose_reaches_new_domains_when_an_allowlist_exists(tmp_path):
    """Live installs auto-build a recipient allowlist from past contacts; a first
    email to a new company must still save as a draft (the human sends it)."""
    with open_store(tmp_path / "j.db") as store:
        store.record_allowlist("friend@known.test", "test")
        out = _compose_draft(_payload(to="support@newvendor.test"), "main", store, _settings())
        assert store.get_manual_draft(out["draft_id"])["recipient"] == "support@newvendor.test"


def test_compose_honours_explicitly_blocked_domains(tmp_path):
    with open_store(tmp_path / "k.db") as store:
        settings = _settings()
        settings.security.recipient_block_domains = {"blocked.test"}
        with pytest.raises(BridgeInputError, match="explicitly blocked"):
            _compose_draft(_payload(to="x@blocked.test"), "main", store, settings)


def test_compose_daily_cap(tmp_path):
    with open_store(tmp_path / "g.db") as store:
        settings = _settings(per_day=2)
        _compose_draft(_payload(), "main", store, settings)
        _compose_draft(_payload(subject="Second"), "main", store, settings)
        with pytest.raises(BridgeInputError, match="cap"):
            _compose_draft(_payload(subject="Third"), "main", store, settings)


def test_compose_cap_zero_turns_agent_compose_off(tmp_path):
    with open_store(tmp_path / "h.db") as store:
        with pytest.raises(BridgeInputError, match="cap|off"):
            _compose_draft(_payload(), "main", store, _settings(per_day=0))


def test_agent_cannot_send_its_own_first_contact_draft(tmp_path):
    """Even with agent send ON, a composed draft to a new address can only go out
    through the human Compose UI — prepare-send refuses the unbound recipient."""
    with open_store(tmp_path / "i.db") as store:
        out = _compose_draft(_payload(), "main", store, _settings(send_enabled=True))
        with pytest.raises(BridgeInputError, match="thread participant or allowlisted"):
            _prepare_send({"draft_id": out["draft_id"]}, "main", store, _settings())
