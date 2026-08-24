"""Deletion policy: what goes now, what waits, and what removes it.

A UI delete never destroys anything on its own — it puts the message in the
local Trash, which is a *holding box*, and the sweep in this module is what
eventually empties it:

* **spam and scam mail goes immediately.** Anything the screener or the user
  labelled SPAM / POTENTIAL_SPAM / POTENTIAL_ISSUE has no holding period; the
  next sweep (or the Spam page's own Delete button, which calls
  :func:`purge_now`) removes it locally and at the provider.
* **everything else waits** ``security.trash_retention_days`` (60 by default)
  from the moment it was deleted, then goes the same way.

"Removes it" means: the provider copy is deleted (see
:mod:`mail.server_delete`), attachment files are unlinked from disk, and the
local row is stripped of every piece of content. A one-line tombstone stays
behind — see :meth:`Store.purge_messages` for why deleting the row outright
would erase the spam rules the user taught.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from ..config import Settings
from ..db.store import Store
from .server_delete import delete_uids

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 60
#: How often the background sweep runs. The clock it enforces is measured in
#: days, so anything under an hour is just extra IMAP logins.
SWEEP_INTERVAL_S = 6 * 60 * 60


@dataclass
class PurgeReport:
    requested: int = 0
    purged: int = 0
    server_deleted: int = 0
    server_failed: int = 0
    files_removed: int = 0
    errors: list[str] = field(default_factory=list)
    modes: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        # Never say "permanently deleted" while a provider copy is still
        # standing — the second clause is what makes the first one true.
        parts = [f"{self.purged} message(s) deleted from this app"]
        if self.server_deleted:
            parts.append(f"{self.server_deleted} removed at the mail server")
        if self.server_failed:
            parts.append(
                f"{self.server_failed} still on the mail server — the next sweep retries"
            )
        return "; ".join(parts) + "."


def retention_days(settings: Settings) -> int:
    return max(0, int(getattr(settings.security, "trash_retention_days", DEFAULT_RETENTION_DAYS)))


def delete_mode(settings: Settings) -> str:
    mode = str(getattr(settings.security, "server_delete_mode", "trash") or "off")
    return mode if mode in {"off", "trash", "expunge"} else "off"


def _accounts(settings: Settings) -> dict[str, object]:
    return {a.name: a for a in (settings.imap_accounts or [])}


def _remove_files(paths: list[str]) -> int:
    removed = 0
    for path in paths:
        try:
            os.unlink(path)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("could not remove attachment file %s: %s", path, e)
    return removed


def purge_now(store: Store, settings: Settings, message_ids: list[int]) -> PurgeReport:
    """Permanently delete these messages — provider first, then locally.

    Blocking (IMAP): call it from a worker thread. The provider is contacted
    first on purpose: if the mailbox is unreachable, the local content is still
    shredded and the failure is recorded on the row, so the next sweep retries
    the server side rather than the message quietly surviving in both places.
    """
    report = PurgeReport(requested=len(message_ids))
    if not message_ids:
        return report

    mode = delete_mode(settings)
    accounts = _accounts(settings)
    if mode != "off":
        by_account: dict[str, dict[str, list[tuple[int, int]]]] = {}
        for row in store.server_locators(message_ids):
            name = str(row["account_name"] or "")
            if name not in accounts:
                continue  # mailbox removed from config — nothing to talk to
            by_account.setdefault(name, {}).setdefault(str(row["folder"]), []).append(
                (int(row["message_id"]), int(row["uid"]))
            )
        for name, targets in by_account.items():
            outcome = delete_uids(accounts[name], targets, mode=mode)  # type: ignore[arg-type]
            report.modes.add(outcome.mode)
            if outcome.deleted_ids:
                store.record_server_delete(outcome.deleted_ids)
                report.server_deleted += len(outcome.deleted_ids)
            if outcome.failed_ids:
                store.record_server_delete(
                    outcome.failed_ids, error=outcome.error or "provider delete failed"
                )
                report.server_failed += len(outcome.failed_ids)
            if outcome.error:
                report.errors.append(f"{name}: {outcome.error}")

    paths = store.attachment_paths(message_ids)
    report.purged = store.purge_messages(message_ids)
    report.files_removed = _remove_files(paths)
    log.info(
        "Purged %d/%d message(s); server deleted=%d failed=%d",
        report.purged, report.requested, report.server_deleted, report.server_failed,
    )
    return report


def sweep(store: Store, settings: Settings) -> PurgeReport:
    """Delete everything whose holding period is up (blocking)."""
    due = store.trash_due_ids(retention_days=retention_days(settings))
    if not due:
        return PurgeReport()
    log.info("Trash retention sweep: %d message(s) due for permanent deletion.", len(due))
    report = purge_now(store, settings, due)
    try:
        from ..audit.log import AuditLog

        AuditLog(store).record(
            actor="system",
            event="tool_call",
            subject_table="messages",
            subject_id=None,
            detail={
                "action": "retention_sweep",
                "due": len(due),
                "purged": report.purged,
                "server_deleted": report.server_deleted,
                "server_failed": report.server_failed,
                "retention_days": retention_days(settings),
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("audit append failed for retention sweep")
    return report
