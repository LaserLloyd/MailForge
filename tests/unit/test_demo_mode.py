"""Demo mode: realistic fictional data, and never the real install."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bluebox import demo
from bluebox.db.store import open_store


@pytest.fixture()
def seeded(tmp_path):
    store = open_store(tmp_path / "demo.db")
    counts = store, demo.seed_demo(store)
    yield counts
    store.close()


def test_seed_creates_two_sites_and_a_full_mailbox(seeded):
    store, counts = seeded
    assert counts["accounts"] == 2
    assert counts["messages"] >= 25
    assert counts["drafts"] == 2
    sites = {
        row["site_id"]
        for row in store.conn.execute("SELECT DISTINCT site_id FROM accounts")
    }
    assert sites == {"main", "shop"}


def test_seed_is_idempotent(seeded):
    store, counts = seeded
    again = demo.seed_demo(store)
    assert again["messages"] == 0
    total = store.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert total == counts["messages"]


def test_dataset_covers_the_states_the_ui_renders(seeded):
    store, _ = seeded
    statuses = {
        row["screening_status"]
        for row in store.conn.execute("SELECT DISTINCT screening_status FROM messages")
    }
    assert {"CONTENT", "POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"} <= statuses

    states = {
        row["state"] for row in store.conn.execute("SELECT DISTINCT state FROM drafts")
    }
    assert {"PENDING", "SENT"} <= states

    quarantined = store.conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE quarantined=1"
    ).fetchone()["n"]
    assert quarantined == 1

    seen = store.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE seen=1").fetchone()["n"]
    assert 0 < seen < counts_of(store)

    threads = store.conn.execute(
        "SELECT thread_id, COUNT(*) AS n FROM messages GROUP BY thread_id HAVING n > 1"
    ).fetchall()
    assert threads, "the demo needs at least one multi-message thread"

    attachments = store.conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE has_attachments=1"
    ).fetchone()["n"]
    assert attachments >= 1


def counts_of(store) -> int:
    return store.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]


def test_every_sender_uses_a_documentation_domain(seeded):
    store, _ = seeded
    for row in store.conn.execute("SELECT from_addr FROM messages"):
        domain = str(row["from_addr"]).rsplit("@", 1)[-1]
        assert domain.endswith((".example", ".com", ".net", ".org"))
        assert "example" in domain, f"{domain} is not a documentation domain"


def test_dates_are_utc_iso_within_the_last_ten_days(seeded):
    store, _ = seeded
    now = datetime.now(timezone.utc)
    for row in store.conn.execute("SELECT received_at FROM messages"):
        stamp = datetime.fromisoformat(row["received_at"])
        assert stamp.tzinfo is not None
        age_days = (now - stamp).total_seconds() / 86400
        assert 0 <= age_days <= 10


def test_demo_config_uses_fictional_mailboxes_and_a_local_llm(tmp_path):
    import tomllib

    home = demo.build_demo_home(tmp_path / "home")
    text = (home / "config" / "config.toml").read_text(encoding="utf-8")
    cfg = tomllib.loads(text)
    # Mailboxes exist only so Compose has a From; every host is an example domain
    # and the CLI disables listeners (BLUEBOX_NO_LISTENERS=1).
    hosts = {a["host"] for a in cfg["imap_accounts"]} | {a["smtp_host"] for a in cfg["imap_accounts"]}
    assert hosts and all(h.endswith((".example.com", ".example.net")) for h in hosts)
    assert "127.0.0.1:1234" in text
    assert "password =" not in text  # no credential values anywhere


def test_demo_home_is_isolated_from_the_real_paths(tmp_path, monkeypatch):
    from bluebox import paths

    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "demo-home"))
    assert paths.data_dir() == (tmp_path / "demo-home" / "data").resolve()
    assert paths.config_file() == (tmp_path / "demo-home" / "config" / "config.toml").resolve()

    monkeypatch.delenv(paths.HOME_ENV, raising=False)
    assert paths.app_home() is None
    assert (tmp_path / "demo-home") not in paths.data_dir().parents


def test_simulated_mailbox_reports_and_honours_refresh():
    import time

    from bluebox.demo import SimulatedMailbox
    from bluebox.mail.sync_state import registry

    box = SimulatedMailbox(name="test-sim", period_s=60)
    try:
        box.start()
        deadline = time.monotonic() + 2
        while registry.get("test-sim").checks < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        before = registry.seqs()
        assert registry.request_refresh("test-sim") == 1
        assert registry.wait_for_check(before, timeout=3.0) is True
        st = registry.get("test-sim")
        assert st.state == "idle" and st.last_error is None and st.checks >= 2
    finally:
        box.stop()
        registry.unregister("test-sim")
