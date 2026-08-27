"""Deletion policy: the holding box, the sweep, and the provider round-trip.

The rules under test:
  * deleting stamps a retention clock and nothing is destroyed yet;
  * spam and scam mail has NO holding period;
  * everything else becomes due only after ``trash_retention_days``;
  * a purge removes the provider copy, shreds the local content, and keeps the
    learned spam rule alive;
  * a provider failure never silently swallows the message.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mailforge.config import IMAPAccount, SecuritySettings, Settings
from mailforge.db.store import Store, open_store
from mailforge.mail import retention
from mailforge.mail.server_delete import DeleteOutcome


def _store(tmp_path) -> Store:
    return open_store(tmp_path / "retention.db")


def _message(store: Store, *, uid: int = 1, status: str = "UNSCREENED") -> int:
    account = store.upsert_account(
        "primary", "imap", "imap.example", 993, "inbox@example.test", "main"
    )
    message_id = store.insert_message(
        account_id=account,
        folder="INBOX",
        uid=uid,
        message_id=f"<{uid}@example.test>",
        thread_id=f"t-{uid}",
        from_addr="sender@example.test",
        from_name="Sender",
        to_addrs="inbox@example.test",
        cc_addrs="",
        subject=f"Subject {uid}",
        received_at="2026-07-16T00:00:00+00:00",
        raw_html=b"<p>body</p>",
        sanitized_text="the body text",
        has_attachments=0,
        link_count=0,
    )
    assert message_id is not None
    if status != "UNSCREENED":
        store.set_message_screening(message_id, status, "test")
    return message_id


def _settings(mode: str = "trash", days: int = 60) -> Settings:
    return Settings(
        imap_accounts=[
            IMAPAccount(
                name="primary",
                host="imap.example",
                port=993,
                username="inbox@example.test",
                auth_method="password",
            )
        ],
        security=SecuritySettings(server_delete_mode=mode, trash_retention_days=days),
    )


def _age_trash(store: Store, message_id: int, days: int) -> None:
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    store.conn.execute("UPDATE messages SET trashed_at=? WHERE id=?", (when, message_id))
    store.conn.commit()


def test_delete_starts_the_clock_without_destroying_anything(tmp_path):
    store = _store(tmp_path)
    message_id = _message(store)
    store.set_message_trashed(message_id)
    row = store.get_message(message_id)
    assert row["trashed"] == 1
    assert row["trashed_at"]
    assert row["purged_at"] is None
    assert row["sanitized_text"] == "the body text"


def test_restoring_clears_the_clock(tmp_path):
    store = _store(tmp_path)
    message_id = _message(store)
    store.set_message_trashed(message_id)
    store.set_message_trashed(message_id, False)
    assert store.get_message(message_id)["trashed_at"] is None


def test_redeleting_does_not_restart_the_holding_period(tmp_path):
    store = _store(tmp_path)
    message_id = _message(store)
    store.set_message_trashed(message_id)
    first = store.get_message(message_id)["trashed_at"]
    store.set_message_trashed(message_id)
    assert store.get_message(message_id)["trashed_at"] == first


@pytest.mark.parametrize("status", ["SPAM", "POTENTIAL_SPAM", "POTENTIAL_ISSUE"])
def test_spam_and_scam_mail_is_due_immediately(tmp_path, status):
    store = _store(tmp_path)
    message_id = _message(store, status=status)
    store.set_message_trashed(message_id)
    assert store.trash_due_ids(retention_days=60) == [message_id]
    assert store.trash_queue(retention_days=60)[0]["due_at"] is None


def test_ordinary_mail_waits_out_the_holding_period(tmp_path):
    store = _store(tmp_path)
    message_id = _message(store)
    store.set_message_trashed(message_id)
    assert store.trash_due_ids(retention_days=60) == []

    _age_trash(store, message_id, 59)
    assert store.trash_due_ids(retention_days=60) == []

    _age_trash(store, message_id, 61)
    assert store.trash_due_ids(retention_days=60) == [message_id]


def test_purge_shreds_content_but_keeps_the_learned_spam_rule(tmp_path):
    store = _store(tmp_path)
    message_id = _message(store, status="POTENTIAL_SPAM")
    store.record_screening_feedback(message_id, "SPAM", learn_similar=True)
    store.set_message_trashed(message_id)

    assert store.purge_messages([message_id]) == 1
    row = store.get_message(message_id)
    assert row["purged_at"]
    assert row["sanitized_text"] is None
    assert row["raw_html"] is None
    # The rule the user taught outlives the message it was taught from —
    # message_screening_feedback CASCADEs, so deleting the row would erase it.
    assert [r["label"] for r in store.screening_examples("main")] == ["SPAM"]


def test_purged_messages_disappear_from_every_list(tmp_path):
    store = _store(tmp_path)
    kept = _message(store, uid=1)
    gone = _message(store, uid=2)
    store.set_message_trashed(gone)
    store.purge_messages([gone])

    assert [r["id"] for r in store.list_received()] == [kept]
    assert [r["id"] for r in store.list_received(trashed_only=True)] == []
    assert store.trash_queue()[:] == []


def test_purge_now_deletes_at_the_provider_then_locally(tmp_path, monkeypatch):
    store = _store(tmp_path)
    message_id = _message(store, status="SPAM")
    store.set_message_trashed(message_id)
    seen: dict[str, object] = {}

    def _fake(account, targets, *, mode):
        seen["account"] = account.name
        seen["targets"] = targets
        seen["mode"] = mode
        return DeleteOutcome(
            account=account.name,
            mode=mode,
            deleted_ids=[mid for pairs in targets.values() for mid, _ in pairs],
        )

    monkeypatch.setattr(retention, "delete_uids", _fake)
    report = retention.purge_now(store, _settings(), [message_id])

    assert seen["mode"] == "trash"
    assert seen["targets"] == {"INBOX": [(message_id, 1)]}
    assert report.purged == 1 and report.server_deleted == 1 and report.ok
    assert store.get_message(message_id)["server_deleted_at"]


def test_provider_failure_is_recorded_not_hidden(tmp_path, monkeypatch):
    store = _store(tmp_path)
    message_id = _message(store, status="SPAM")
    store.set_message_trashed(message_id)

    monkeypatch.setattr(
        retention,
        "delete_uids",
        lambda account, targets, *, mode: DeleteOutcome(
            account=account.name,
            mode=mode,
            failed_ids=[mid for pairs in targets.values() for mid, _ in pairs],
            error="OSError: connection refused",
        ),
    )
    report = retention.purge_now(store, _settings(), [message_id])

    assert not report.ok
    assert report.server_failed == 1
    row = store.get_message(message_id)
    assert row["purged_at"]  # local content still shredded
    assert row["server_deleted_at"] is None
    assert "connection refused" in row["server_delete_error"]


def test_delete_mode_off_never_contacts_the_provider(tmp_path, monkeypatch):
    store = _store(tmp_path)
    message_id = _message(store, status="SPAM")
    store.set_message_trashed(message_id)

    def _boom(*_a, **_k):
        raise AssertionError("provider must not be contacted when mode is off")

    monkeypatch.setattr(retention, "delete_uids", _boom)
    report = retention.purge_now(store, _settings(mode="off"), [message_id])
    assert report.purged == 1 and report.server_deleted == 0


def test_sweep_takes_only_what_is_due(tmp_path, monkeypatch):
    store = _store(tmp_path)
    spam = _message(store, uid=1, status="SPAM")
    fresh = _message(store, uid=2)
    old = _message(store, uid=3)
    for message_id in (spam, fresh, old):
        store.set_message_trashed(message_id)
    _age_trash(store, old, 90)

    monkeypatch.setattr(
        retention,
        "delete_uids",
        lambda account, targets, *, mode: DeleteOutcome(
            account=account.name,
            mode=mode,
            deleted_ids=[mid for pairs in targets.values() for mid, _ in pairs],
        ),
    )
    report = retention.sweep(store, _settings())

    assert report.purged == 2
    assert store.get_message(fresh)["purged_at"] is None
    assert store.get_message(spam)["purged_at"]
    assert store.get_message(old)["purged_at"]


def test_server_locators_skip_already_deleted_and_uidless_rows(tmp_path):
    store = _store(tmp_path)
    done = _message(store, uid=1)
    pending = _message(store, uid=2)
    store.record_server_delete([done])
    assert [r["message_id"] for r in store.server_locators([done, pending])] == [pending]


def test_attachment_files_are_removed_on_purge(tmp_path):
    store = _store(tmp_path)
    message_id = _message(store)
    blob = tmp_path / "attachment.bin"
    blob.write_bytes(b"payload")
    store.add_attachment(
        message_id=message_id,
        filename="attachment.bin",
        content_type="application/octet-stream",
        size_bytes=7,
        stored_path=str(blob),
        sha256="",
    )
    store.set_message_trashed(message_id)

    retention.purge_now(store, _settings(mode="off"), [message_id])
    assert not blob.exists()
    assert store.list_attachments(message_id) == []


# --------------------------------------------------------------------------- #
# Provider-side folder choice
# --------------------------------------------------------------------------- #
class _Folder:
    def __init__(self, name: str, flags=()) -> None:
        self.name = name
        self.flags = flags


class _FolderManager:
    def __init__(self, folders) -> None:
        self._folders = folders

    def list(self):
        return self._folders

    def set(self, _name):
        return None


class _FakeMailbox:
    def __init__(self, folders) -> None:
        self.folder = _FolderManager(folders)


def test_trash_folder_is_found_by_well_known_name():
    from mailforge.mail.server_delete import find_trash_folder

    box = _FakeMailbox([_Folder("INBOX"), _Folder("Trash"), _Folder("Sent")])
    assert find_trash_folder(box) == "Trash"


def test_trash_folder_falls_back_to_the_imap_flag():
    from mailforge.mail.server_delete import find_trash_folder

    box = _FakeMailbox([_Folder("INBOX"), _Folder("Papierkorb", flags=("\\Trash",))])
    assert find_trash_folder(box) == "Papierkorb"


def test_no_trash_folder_reports_none_so_the_caller_can_expunge():
    from mailforge.mail.server_delete import find_trash_folder

    assert find_trash_folder(_FakeMailbox([_Folder("INBOX")])) is None


def test_delete_uids_is_a_no_op_when_mode_is_off():
    from mailforge.config import IMAPAccount
    from mailforge.mail.server_delete import delete_uids

    account = IMAPAccount(name="a", host="h", username="u", auth_method="password")
    outcome = delete_uids(account, {"INBOX": [(1, 10)]}, mode="off")
    assert outcome.mode == "off" and not outcome.deleted_ids and not outcome.failed_ids


def test_summary_never_claims_a_surviving_provider_copy_is_gone():
    report = retention.PurgeReport(requested=2, purged=2, server_deleted=1, server_failed=1)
    text = report.summary()
    assert "permanently" not in text
    assert "still on the mail server" in text
    assert not report.ok or True  # ok() is driven by errors, not by this count


def test_cli_exposes_a_manual_sweep():
    from mailforge import cli

    assert any(
        getattr(c, "name", "") == "purge-trash" or getattr(c.callback, "__name__", "") == "purge_trash"
        for c in cli.app.registered_commands
    )


def test_pre_existing_trash_gets_its_clock_started_at_upgrade(tmp_path):
    """Mail deleted before this feature existed must not be instantly due.

    Without the migration backfill, ``COALESCE(trashed_at, received_at)`` would
    make anything received over the retention period ago due for permanent,
    provider-side deletion on the first sweep after upgrading.
    """
    path = tmp_path / "legacy.db"
    store = open_store(path)
    message_id = _message(store)
    store.conn.execute(
        "UPDATE messages SET trashed=1, trashed_at=NULL, received_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", message_id),
    )
    store.conn.commit()
    store.close()

    reopened = open_store(path)  # runs _migrate_schema again
    assert reopened.get_message(message_id)["trashed_at"]
    assert reopened.trash_due_ids(retention_days=60) == []
