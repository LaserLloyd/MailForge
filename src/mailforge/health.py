"""Machine-readable health for the running service (``GET /healthz``).

Carries no credentials and no message content: per-mailbox listener state
from the process-wide sync registry, plus a database ping and the version.
"""

from __future__ import annotations

import time
from typing import Any

from . import __version__
from .mail.sync_state import registry

_ERROR_MAX = 160


def health_report(store: Any, now: float | None = None) -> tuple[dict[str, Any], int]:
    """Return ``(body, http_status)``. 200 only when every mailbox is healthy
    and the database answers; 503 otherwise (including "no listeners")."""
    now = now if now is not None else time.time()
    accounts: dict[str, Any] = {}
    for acct in registry.snapshot():
        age = acct.age(now)
        error = " ".join(str(acct.last_error).split())[:_ERROR_MAX] if acct.last_error else None
        accounts[acct.account] = {
            "state": acct.state,
            "last_check_age_s": None if age is None else round(age, 1),
            "healthy": bool(acct.healthy),
            "last_error": error,
        }
    try:
        store.conn.execute("SELECT 1").fetchone()
        db = "ok"
    except Exception:  # noqa: BLE001 — report, never raise from a probe
        db = "error"
    ok = db == "ok" and bool(accounts) and all(a["healthy"] for a in accounts.values())
    body = {
        "status": "ok" if ok else "degraded",
        "accounts": accounts,
        "db": db,
        "version": __version__,
    }
    return body, (200 if ok else 503)
