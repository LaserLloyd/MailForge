"""Live mail-sync status shared between the IMAP listeners and the UI.

The listeners are background threads the UI never holds a reference to, so
this module is the meeting point: each :class:`IMAPListener` registers itself
here, reports state transitions (connecting → idle → checking → backoff), and
exposes a ``refresh`` hook. The UI reads :meth:`SyncRegistry.snapshot` for the
"checked N s ago" indicator and calls :meth:`SyncRegistry.request_refresh`
for the Refresh button, then waits on :meth:`SyncRegistry.wait_for_check`.

Thread-safe: every mutation holds ``_lock``; snapshots are copies.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace


@dataclass
class AccountSync:
    """Status of one mailbox listener. Timestamps are ``time.time()`` (wall)."""

    account: str
    state: str = "starting"  # starting | connecting | idle | checking | backoff | stopped
    connected_since: float | None = None
    last_check_at: float | None = None
    last_new_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    next_retry_at: float | None = None
    new_total: int = 0
    checks: int = 0
    #: Monotonic counter bumped after every completed (or failed) check; the
    #: UI waits for it to move past the value captured before a refresh.
    check_seq: int = 0
    refresh_pending: bool = False

    @property
    def healthy(self) -> bool:
        return self.state in {"idle", "checking"} and not self.last_error

    def age(self, now: float | None = None) -> float | None:
        """Seconds since the last completed check, or None if never."""
        if self.last_check_at is None:
            return None
        return max(0.0, (now if now is not None else time.time()) - self.last_check_at)


class SyncRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._accounts: dict[str, AccountSync] = {}
        self._refresh_hooks: dict[str, Callable[[], None]] = {}
        self._changed = threading.Condition(self._lock)

    # ----- listener side -----
    def register(self, account: str, refresh: Callable[[], None] | None = None) -> None:
        with self._lock:
            self._accounts.setdefault(account, AccountSync(account=account))
            if refresh is not None:
                self._refresh_hooks[account] = refresh
            self._changed.notify_all()

    def unregister(self, account: str) -> None:
        with self._lock:
            self._accounts.pop(account, None)
            self._refresh_hooks.pop(account, None)
            self._changed.notify_all()

    def update(self, account: str, **fields: object) -> None:
        with self._lock:
            cur = self._accounts.get(account) or AccountSync(account=account)
            self._accounts[account] = replace(cur, **fields)  # type: ignore[arg-type]
            self._changed.notify_all()

    def note_check(self, account: str, new_messages: int = 0, error: str | None = None) -> None:
        """Record one completed reconciliation (or its failure)."""
        now = time.time()
        with self._lock:
            cur = self._accounts.get(account) or AccountSync(account=account)
            fields: dict[str, object] = {
                "check_seq": cur.check_seq + 1,
                "refresh_pending": False,
            }
            if error is None:
                fields.update(
                    last_check_at=now,
                    checks=cur.checks + 1,
                    new_total=cur.new_total + max(0, int(new_messages)),
                    last_error=None,
                    last_error_at=None,
                    next_retry_at=None,
                    state="idle",
                )
                if new_messages:
                    fields["last_new_at"] = now
            else:
                fields.update(last_error=str(error), last_error_at=now, state="backoff")
            self._accounts[account] = replace(cur, **fields)  # type: ignore[arg-type]
            self._changed.notify_all()

    # ----- UI side -----
    def snapshot(self) -> list[AccountSync]:
        with self._lock:
            return [replace(a) for a in self._accounts.values()]

    def get(self, account: str) -> AccountSync | None:
        with self._lock:
            a = self._accounts.get(account)
            return replace(a) if a else None

    def request_refresh(self, account: str | None = None) -> int:
        """Poke the listener(s) to reconcile now. Returns how many were poked."""
        with self._lock:
            names = [account] if account else list(self._refresh_hooks)
            hooks = [(n, self._refresh_hooks[n]) for n in names if n in self._refresh_hooks]
            for name, _ in hooks:
                cur = self._accounts.get(name)
                if cur is not None:
                    self._accounts[name] = replace(cur, refresh_pending=True)
            self._changed.notify_all()
        for _, hook in hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001 — one bad hook must not block the rest
                pass
        return len(hooks)

    def seqs(self) -> dict[str, int]:
        with self._lock:
            return {n: a.check_seq for n, a in self._accounts.items() if n in self._refresh_hooks}

    def wait_for_check(self, since: dict[str, int], timeout: float) -> bool:
        """Block until every account in ``since`` has completed a newer check.

        Returns True if they all did within ``timeout`` seconds. Meant to run
        in a worker thread (the UI awaits it via ``asyncio.to_thread``).
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._changed:
            while True:
                pending = [
                    n for n, seq in since.items()
                    if n in self._accounts and self._accounts[n].check_seq <= seq
                ]
                if not pending:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)

    def summary(self, now: float | None = None) -> dict[str, object]:
        """Aggregate for the header chip: overall state, newest check age, errors."""
        now = now if now is not None else time.time()
        accounts = self.snapshot()
        if not accounts:
            return {"state": "none", "age": None, "errors": [], "accounts": [], "checking": False}
        ages = [a.age(now) for a in accounts if a.last_check_at is not None]
        errors = [a for a in accounts if a.last_error]
        checking = any(a.state == "checking" or a.refresh_pending for a in accounts)
        if errors:
            state = "error" if len(errors) == len(accounts) else "degraded"
        elif all(a.last_check_at is not None for a in accounts):
            state = "ok"
        else:
            state = "starting"
        return {
            "state": state,
            "age": max(ages) if ages else None,  # the STALEST account decides
            "errors": errors,
            "accounts": accounts,
            "checking": checking,
        }


#: Process-wide registry: listeners register here, the UI reads here.
registry = SyncRegistry()


def human_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 5:
        return "just now"
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"
