"""Provider deletes touch exactly the stored UIDs and nothing else.

Regression for a live finding: imap_tools only saw the pre-login capability
list (no MOVE/UIDPLUS), so every "trash" was COPY + a bare EXPUNGE, which also
destroys whatever another client had flagged \\Deleted in the folder.
"""

from __future__ import annotations

import imaplib
import types

import pytest

from mailforge.config import IMAPAccount
from mailforge.db.store import open_store
from mailforge.mail import retention, server_delete
from mailforge.mail.server_delete import NO_TRASH_ERROR, TRASH_FOLDER_CANDIDATES, delete_uids


class _Folder:
    def __init__(self, name, flags=()):
        self.name = name
        self.flags = flags


class FakeClient:
    def __init__(self, caps, fail_on=None):
        self.capabilities = tuple(caps)
        self.calls: list[tuple] = []
        self.fail_on = fail_on

    def uid(self, command, *args):
        self.calls.append(("UID", command, *args))
        if self.fail_on == command:
            raise imaplib.IMAP4.abort("session state is inconsistent")
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("EXPUNGE",))
        return "OK", [b""]


class FakeMailbox:
    def __init__(self, caps=("IMAP4REV1", "MOVE", "UIDPLUS"), folders=("INBOX", "Trash"),
                 server=None, fail_on=None):
        self.client = FakeClient(caps, fail_on)
        self._folders = [_Folder(n) for n in folders]
        self.server = server if server is not None else {}  # uid -> Message-ID
        self.selected = None
        outer = self

        class _FM:
            def list(self_inner):
                return outer._folders

            def set(self_inner, name):
                outer.selected = name

        self.folder = _FM()

    def fetch(self, criteria, headers_only=False, mark_seen=True, bulk=False):
        assert headers_only and mark_seen is False
        for uid, mid in self.server.items():
            if f"{uid}" in str(criteria):
                yield types.SimpleNamespace(uid=str(uid), headers={"message-id": (mid,)})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


ACCOUNT = IMAPAccount(name="a", host="imap.example", username="u", auth_method="password")


@pytest.fixture
def box(monkeypatch):
    holder = {}

    def _use(mailbox):
        holder["box"] = mailbox
        monkeypatch.setattr(server_delete, "open_mailbox", lambda *a, **k: mailbox)
        monkeypatch.setattr(server_delete, "_login", lambda *a, **k: None)
        return mailbox

    return _use


def _commands(mailbox):
    return [c[1] if c[0] == "UID" else c[0] for c in mailbox.client.calls]


def test_trash_mode_uses_uid_move_and_never_a_bare_expunge(box):
    mb = box(FakeMailbox(server={10: "<a@x>", 11: "<b@x>"}))
    out = delete_uids(ACCOUNT, {"INBOX": [(1, 10), (2, 11)]}, mode="trash",
                      expected_ids={1: "<a@x>", 2: "<b@x>"})
    assert out.deleted_ids == [1, 2] and not out.failed_ids
    assert _commands(mb) == ["MOVE"]
    assert mb.client.calls[0][2] == "10,11"


def test_without_move_uses_copy_and_uid_expunge(box):
    mb = box(FakeMailbox(caps=("IMAP4REV1", "UIDPLUS"), server={10: "<a@x>"}))
    out = delete_uids(ACCOUNT, {"INBOX": [(1, 10)]}, mode="trash")
    assert out.deleted_ids == [1]
    assert _commands(mb) == ["COPY", "STORE", "EXPUNGE"]
    assert all(c[0] == "UID" for c in mb.client.calls)  # no folder-wide EXPUNGE


@pytest.mark.parametrize("mode", ["trash", "expunge"])
def test_without_uidplus_or_move_nothing_is_touched(box, mode):
    mb = box(FakeMailbox(caps=("IMAP4REV1",), server={10: "<a@x>"}))
    out = delete_uids(ACCOUNT, {"INBOX": [(1, 10)]}, mode=mode)
    assert out.failed_ids == [1] and not out.deleted_ids
    assert "UIDPLUS" in out.error
    assert mb.client.calls == []


def test_expunge_mode_expunges_only_the_given_uids(box):
    mb = box(FakeMailbox(server={10: "<a@x>"}))
    out = delete_uids(ACCOUNT, {"INBOX": [(1, 10)]}, mode="expunge")
    assert out.deleted_ids == [1] and out.mode == "expunge"
    assert ("UID", "EXPUNGE", "10") in mb.client.calls
    assert ("EXPUNGE",) not in mb.client.calls


def test_trash_mode_without_a_trash_folder_is_refused_not_expunged(box):
    mb = box(FakeMailbox(folders=("INBOX", "Junk"), server={10: "<a@x>"}))
    out = delete_uids(ACCOUNT, {"INBOX": [(1, 10)]}, mode="trash")
    assert out.failed_ids == [1] and out.error == NO_TRASH_ERROR
    assert mb.client.calls == []


def test_junk_is_not_a_trash_folder():
    assert "Junk" not in TRASH_FOLDER_CANDIDATES


def test_a_uid_now_holding_another_message_is_left_alone(box):
    mb = box(FakeMailbox(server={10: "<someone-else@x>", 11: "<b@x>"}))
    out = delete_uids(ACCOUNT, {"INBOX": [(1, 10), (2, 11)]}, mode="trash",
                      expected_ids={1: "<a@x>", 2: "<b@x>"})
    assert out.deleted_ids == [2]
    assert out.failed_ids == [1]
    assert out.error.startswith("uid no longer matches")
    assert mb.client.calls[0][2] == "11"


def test_a_uid_already_gone_counts_as_deleted_without_a_command(box):
    mb = box(FakeMailbox(server={}))
    out = delete_uids(ACCOUNT, {"INBOX": [(1, 10)]}, mode="trash", expected_ids={1: "<a@x>"})
    assert out.deleted_ids == [1] and mb.client.calls == []


def test_message_id_comparison_ignores_folding_and_brackets(box):
    box(FakeMailbox(server={10: " <A@x>\r\n"}))
    out = delete_uids(ACCOUNT, {"INBOX": [(1, 10)]}, mode="trash", expected_ids={1: "a@x"})
    assert out.deleted_ids == [1]


def test_imap_abort_is_caught_and_earlier_deletes_are_kept(box):
    class Abort(FakeMailbox):
        def __init__(self):
            super().__init__(server={10: "<a@x>", 20: "<b@x>"})
            self.n = 0

        def fetch(self, *a, **k):
            self.n += 1
            if self.n == 2:
                raise imaplib.IMAP4.abort("BYE session state is inconsistent")
            yield from super().fetch(*a, **k)

    box(Abort())
    out = delete_uids(
        ACCOUNT, {"INBOX": [(1, 10)], "Archive": [(2, 20)]}, mode="trash"
    )
    assert out.deleted_ids == [1]
    assert out.failed_ids == [2]
    assert "abort" in out.error


def test_connection_abort_keeps_what_the_provider_already_accepted(box, monkeypatch):
    class DiesOnSecondSet(FakeMailbox):
        def __init__(self):
            super().__init__(server={10: "<a@x>", 20: "<b@x>"})
            outer = self
            calls = {"n": 0}

            class _FM:
                def list(self_inner):
                    return outer._folders

                def set(self_inner, name):
                    calls["n"] += 1
                    if calls["n"] == 2:
                        raise imaplib.IMAP4.abort("socket error: EOF")

            self.folder = _FM()

        def __exit__(self, *exc):
            if exc[0] is not None:
                raise exc[1]
            return False

    box(DiesOnSecondSet())
    out = delete_uids(ACCOUNT, {"INBOX": [(1, 10)], "Other": [(2, 20)]}, mode="trash")
    assert out.deleted_ids == [1] and out.failed_ids == [2]


# --- retry of provider deletes that failed after the local purge ------------


def _store_with_failed_purge(tmp_path):
    store = open_store(tmp_path / "r.db")
    account = store.upsert_account("a", "imap", "imap.example", 993, "u", "main")
    mid = store.insert_message(
        account_id=account, folder="INBOX", uid=10, message_id="<a@x>", thread_id="t",
        from_addr="s@x", from_name="S", to_addrs="u", cc_addrs="", subject="s",
        received_at="2026-07-16T00:00:00+00:00", raw_html=None, sanitized_text="b",
        has_attachments=0, link_count=0,
    )
    store.set_message_trashed(mid)
    store.record_server_delete([mid], error="OSError: connection refused")
    store.purge_messages([mid])
    return store, mid


def _settings():
    from mailforge.config import SecuritySettings, Settings

    return Settings(imap_accounts=[ACCOUNT], security=SecuritySettings(server_delete_mode="trash"))


def test_failed_provider_delete_is_retried_by_the_next_sweep(tmp_path, monkeypatch):
    store, mid = _store_with_failed_purge(tmp_path)
    assert store.trash_due_ids(retention_days=60) == []  # gone from the trash queue
    assert store.server_retry_ids() == [mid]
    seen = {}

    def _fake(account, targets, *, mode, expected_ids=None):
        seen["targets"], seen["expected"] = targets, expected_ids
        return server_delete.DeleteOutcome(account=account.name, mode=mode, deleted_ids=[mid])

    monkeypatch.setattr(retention, "delete_uids", _fake)
    report = retention.sweep(store, _settings())
    assert seen["targets"] == {"INBOX": [(mid, 10)]}
    assert seen["expected"] == {mid: "<a@x>"}
    assert report.server_deleted == 1
    assert store.get_message(mid)["server_deleted_at"]
    assert store.server_retry_ids() == []


def test_uid_mismatch_is_not_retried_forever(tmp_path):
    store, mid = _store_with_failed_purge(tmp_path)
    store.record_server_delete([mid], error=store.UID_MISMATCH_ERROR)
    assert store.server_retry_ids() == []
