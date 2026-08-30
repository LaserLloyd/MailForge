"""Focused coverage for dashboard, templates, and managed knowledge data."""

from __future__ import annotations

import sqlite3

import pytest

from mailforge.db.store import Store, open_store
from mailforge.response_templates import EXAMPLE_TEMPLATES


def _message(store: Store, *, site_id: str = "main", uid: int = 1) -> int:
    account = store.upsert_account(
        f"{site_id}-{uid}",
        "imap",
        "imap.example",
        993,
        f"inbox-{uid}@example.test",
        site_id,
    )
    message_id = store.insert_message(
        account_id=account,
        folder="INBOX",
        uid=uid,
        message_id=f"<{site_id}-{uid}@example.test>",
        thread_id=f"{site_id}-{uid}",
        from_addr="sender@example.test",
        from_name="Sender",
        to_addrs="inbox@example.test",
        cc_addrs="",
        subject=f"Question {uid}",
        received_at=f"2026-07-16T00:00:{uid:02d}+00:00",
        raw_html=b"",
        sanitized_text="Please respond.",
        has_attachments=0,
        link_count=0,
    )
    assert message_id is not None
    return message_id


def test_legacy_reference_columns_are_added_before_schema_indexes(tmp_path):
    path = tmp_path / "legacy-reference.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE reference_documents (
          id INTEGER PRIMARY KEY,
          site_id TEXT NOT NULL,
          filename TEXT NOT NULL,
          stored_path TEXT NOT NULL,
          mime_type TEXT,
          size_bytes INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'UPLOADED',
          error_text TEXT,
          chunk_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    conn.close()

    store = Store(path)
    store.init_schema()
    columns = {
        row["name"]
        for row in store.conn.execute("PRAGMA table_info(reference_documents)")
    }
    assert {"managed_key", "content_sha256"} <= columns


def test_system_templates_seed_idempotently_and_are_site_scoped(tmp_path):
    store = open_store(tmp_path / "templates.db")
    assert len(store.list_response_templates("main")) == 5
    assert len(store.list_response_templates("shop")) == 5
    assert all(row["is_system"] for row in store.list_response_templates("main"))

    system = store.list_response_templates("main")[0]
    assert store.update_response_template(
        system["id"],
        "main",
        system["name"],
        system["category"],
        system["subject"],
        "Locally customized system template.",
        system["shortcut"],
    )
    store.init_schema()
    total = store.conn.execute(
        "SELECT COUNT(*) AS n FROM response_templates"
    ).fetchone()["n"]
    assert total == len(EXAMPLE_TEMPLATES) * 2  # one set per configured site
    assert (
        store.get_response_template(system["id"], "main")["body"]
        == "Locally customized system template."
    )


def test_response_template_crud_validation_and_system_delete_guard(tmp_path):
    store = open_store(tmp_path / "templates.db")
    template_id = store.create_response_template(
        "main",
        "Personal follow-up",
        "Support",
        "Re: {{question}}",
        "Hi {{first_name}},\n\n{{next_step}}\n\n{{signature}}",
        "follow-up",
    )
    assert store.get_response_template(template_id, "main") is not None
    assert store.get_response_template(template_id, "shop") is None
    assert store.update_response_template(
        template_id,
        "main",
        "Personal follow-up",
        "General",
        "Following up",
        "Hello {{first_name}},\n\nFollowing up.",
    )
    assert not store.update_response_template(
        template_id,
        "shop",
        "Wrong site",
        "General",
        "",
        "No.",
    )
    with pytest.raises(ValueError, match="unsupported template placeholder"):
        store.create_response_template(
            "main", "Unsafe placeholder", "General", "", "{{password}}"
        )

    system_id = store.list_response_templates("main")[0]["id"]
    assert store.delete_response_template(system_id, "main") is False
    assert store.delete_response_template(template_id, "shop") is False
    assert store.delete_response_template(template_id, "main") is True


def test_received_rows_include_classification_and_latest_draft(tmp_path):
    store = open_store(tmp_path / "mail.db")
    message_id = _message(store)
    store.upsert_classification(
        message_id,
        category="RESPOND",
        priority=9,
        rationale="A reply is requested.",
        injection_risk=0.2,
        model_used="test",
    )
    store.create_draft(
        message_id,
        "main-1",
        "sender@example.test",
        "Re: Question 1",
        "First",
        "PENDING",
    )
    latest_id = store.create_draft(
        message_id,
        "main-1",
        "sender@example.test",
        "Re: Question 1",
        "Second",
        "BLOCKED",
    )

    row = store.list_received()[0]
    assert row["category"] == "RESPOND"
    assert row["priority"] == 9
    assert row["rationale"] == "A reply is requested."
    assert row["injection_risk"] == 0.2
    assert row["draft_id"] == latest_id
    assert row["draft_state"] == "BLOCKED"


def test_message_unseen_and_archive_controls(tmp_path):
    store = open_store(tmp_path / "mail.db")
    message_id = _message(store)
    store.mark_message_seen(message_id)
    assert store.received_counts()["unseen"] == 0
    store.mark_message_unseen(message_id)
    assert store.received_counts()["unseen"] == 1
    assert store.set_message_archived(message_id)
    assert store.received_counts() == {"total": 0, "unseen": 0}
    assert store.set_message_archived(message_id, False)
    assert store.received_counts() == {"total": 1, "unseen": 1}


def test_bulk_message_actions_and_reversible_local_trash(tmp_path):
    store = open_store(tmp_path / "mail.db")
    first = _message(store, uid=1)
    second = _message(store, uid=2)

    assert store.mark_messages_seen([first, first, second]) == 2
    assert store.received_counts() == {"total": 2, "unseen": 0}
    assert store.mark_messages_seen([first, second], False) == 2
    assert store.received_counts() == {"total": 2, "unseen": 2}

    assert store.set_messages_archived([first, second]) == 2
    assert store.list_received() == []
    assert {row["id"] for row in store.list_received(archived_only=True)} == {
        first,
        second,
    }
    assert first not in store.unprocessed_message_ids()

    assert store.set_messages_trashed([first]) == 1
    assert [row["id"] for row in store.list_received(archived_only=True)] == [second]
    assert [row["id"] for row in store.list_received(trashed_only=True)] == [first]
    assert store.get_message(first)["archived"] == 0

    assert store.set_messages_trashed([first], False) == 1
    assert store.set_messages_archived([second], False) == 1
    assert {row["id"] for row in store.list_received()} == {first, second}


def test_existing_database_gains_reversible_trash_column(tmp_path):
    path = tmp_path / "legacy-mail.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,"
        "folder TEXT NOT NULL,uid INTEGER NOT NULL,message_id TEXT,thread_id TEXT NOT NULL,"
        "from_addr TEXT,from_name TEXT,to_addrs TEXT,cc_addrs TEXT,subject TEXT,"
        "received_at TEXT,raw_html BLOB,sanitized_text TEXT,has_attachments INTEGER,"
        "link_count INTEGER,seen INTEGER DEFAULT 0,archived INTEGER DEFAULT 0,"
        "UNIQUE(account_id,folder,uid))"
    )
    conn.commit()
    conn.close()

    migrated = Store(path)
    migrated.init_schema()
    message_id = _message(migrated)
    assert migrated.get_message(message_id)["trashed"] == 0
    assert migrated.set_message_trashed(message_id)
    assert [row["id"] for row in migrated.list_received(trashed_only=True)] == [message_id]


def test_managed_reference_upsert_detects_content_and_refuses_delete(tmp_path):
    store = open_store(tmp_path / "knowledge.db")
    document_id, needs_index = store.upsert_managed_reference_document(
        "main",
        "site-handbook:main:v1",
        "Northwind Studio handbook.md",
        str(tmp_path / "Northwind Studio handbook.md"),
        "text/markdown",
        100,
        "a" * 64,
    )
    assert needs_index is True
    store.replace_document_chunks(
        document_id,
        "main",
        [{"text": "Northwind Studio ships worldwide.", "token_count": 4, "embedding": None}],
    )

    same_id, needs_index = store.upsert_managed_reference_document(
        "main",
        "site-handbook:main:v1",
        "Northwind Studio handbook.md",
        str(tmp_path / "Northwind Studio handbook.md"),
        "text/markdown",
        100,
        "a" * 64,
    )
    assert same_id == document_id
    assert needs_index is False
    assert store.delete_reference_document(document_id) is False

    same_id, needs_index = store.upsert_managed_reference_document(
        "main",
        "site-handbook:main:v1",
        "Northwind Studio handbook.md",
        str(tmp_path / "Northwind Studio handbook.md"),
        "text/markdown",
        120,
        "b" * 64,
    )
    assert same_id == document_id
    assert needs_index is True
    assert store.get_reference_document(document_id)["status"] == "UPLOADED"
    with pytest.raises(ValueError, match="another site"):
        store.upsert_managed_reference_document(
            "shop",
            "site-handbook:main:v1",
            "wrong.md",
            str(tmp_path / "wrong.md"),
            "text/markdown",
            1,
            "c" * 64,
        )


def test_dashboard_snapshot_aggregates_site_scoped_actions(tmp_path):
    store = open_store(tmp_path / "dashboard.db")
    main_message = _message(store, site_id="main", uid=1)
    _message(store, site_id="shop", uid=2)
    store.upsert_classification(
        main_message,
        category="RESPOND",
        priority=10,
        rationale="Needs an answer.",
        injection_risk=0.1,
        model_used="test",
    )
    store.create_draft(
        main_message,
        "main-1",
        "sender@example.test",
        "Re: Question 1",
        "Draft reply",
        "PENDING",
    )
    store.create_manual_draft(
        "inbox-1@example.test",
        "new@example.test",
        "Manual",
        "Work in progress",
        "main",
    )
    store.create_reference_document(
        "main", "guide.txt", str(tmp_path / "guide.txt"), "text/plain", 10
    )

    snapshot = store.dashboard_snapshot(include_sites=True)
    assert snapshot["received"] == {"total": 2, "unseen": 2}
    assert len(snapshot["accounts"]) == 2
    assert len(snapshot["recent_received"]) == 2
    assert len(snapshot["actionable_drafts"]) == 1
    assert snapshot["sites"]["main"]["manual_drafts"] == 1
    assert snapshot["sites"]["main"]["references"]["total"] == 1
    assert snapshot["sites"]["shop"]["received"]["total"] == 1
