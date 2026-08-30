"""Out-of-band anomaly detector (build spec §2, §8).

Runs in a **separate daemon thread** from the agent graph so a compromised or
wedged agent loop cannot silently suppress detection. It polls the SQLite DB on
an interval (no heavy deps) and looks for anomalies:

  * spikes in ``BLOCKED`` drafts in a recent window,
  * repeated high ``injection_risk`` classifications,
  * sends to first-time / external recipients,
  * audit-chain breaks (re-runs ``verify_chain``).

Findings are emitted via the redaction-filtered logger (spec §0.5) so the
report itself never leaks secrets/PII. The monitor only *reports*; it never
mutates state or sends mail.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from ..security.logging_filter import install_redaction
from .log import verify_chain

log = logging.getLogger(__name__)
# Ensure findings are scrubbed even if the host app never installed the filter.
install_redaction(log)


class AnomalyMonitor:
    """Periodic, polling anomaly detector over the agent DB (spec §2/§8)."""

    def __init__(
        self,
        store: Any,
        *,
        poll_interval_s: float = 60.0,
        blocked_spike_threshold: int = 5,
        high_injection_threshold: float = 0.85,
        high_injection_count: int = 3,
        window_minutes: int = 10,
    ):
        self.store = store
        self.poll_interval_s = poll_interval_s
        self.blocked_spike_threshold = blocked_spike_threshold
        self.high_injection_threshold = high_injection_threshold
        self.high_injection_count = high_injection_count
        self.window_minutes = window_minutes

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Remember the last draft id we reported a send for, so we only flag
        # newly-seen first-time external recipients once.
        self._seen_send_ids: set[int] = set()

    # ----- lifecycle -----
    def start(self) -> None:
        """Launch the monitor in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="openclaw-anomaly-monitor", daemon=True
        )
        self._thread.start()
        log.info("Anomaly monitor started (poll=%.0fs)", self.poll_interval_s)

    def stop(self, timeout: float | None = 5.0) -> None:
        """Signal the loop to exit and join the thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        log.info("Anomaly monitor stopped")

    # ----- loop -----
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as e:  # never let one bad cycle kill the monitor
                log.warning("Anomaly monitor cycle failed: %s", e)
            # Interruptible sleep so stop() returns promptly.
            self._stop.wait(self.poll_interval_s)

    def check_once(self) -> list[str]:
        """Run all checks once; return the list of finding messages logged."""
        findings: list[str] = []
        findings += self._check_blocked_spike()
        findings += self._check_high_injection()
        findings += self._check_first_time_external_sends()
        findings += self._check_audit_chain()
        for f in findings:
            log.warning("ANOMALY: %s", f)
        return findings

    # ----- individual checks -----
    def _window_clause(self) -> str:
        return f"datetime('now', '-{int(self.window_minutes)} minutes')"

    def _check_blocked_spike(self) -> list[str]:
        row = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM drafts "
            f"WHERE state='BLOCKED' AND updated_at >= {self._window_clause()}"
        ).fetchone()
        n = row["n"] if row else 0
        if n >= self.blocked_spike_threshold:
            return [
                f"BLOCKED-draft spike: {n} drafts blocked in the last "
                f"{self.window_minutes} min (threshold {self.blocked_spike_threshold})"
            ]
        return []

    def _check_high_injection(self) -> list[str]:
        row = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM classifications "
            "WHERE injection_risk >= ? "
            f"AND classified_at >= {self._window_clause()}",
            (self.high_injection_threshold,),
        ).fetchone()
        n = row["n"] if row else 0
        if n >= self.high_injection_count:
            return [
                f"Repeated high injection_risk: {n} classifications "
                f">= {self.high_injection_threshold} in the last "
                f"{self.window_minutes} min"
            ]
        return []

    def _check_first_time_external_sends(self) -> list[str]:
        findings: list[str] = []
        rows = self.store.conn.execute(
            "SELECT id, recipient FROM drafts WHERE state='SENT' ORDER BY id DESC LIMIT 50"
        ).fetchall()
        for r in rows:
            did = r["id"]
            if did in self._seen_send_ids:
                continue
            self._seen_send_ids.add(did)
            recipient = (r["recipient"] or "").lower()
            if not recipient:
                continue
            # First-time => not previously recorded in the recipient allowlist.
            if not self.store.is_allowlisted(recipient):
                domain = recipient.split("@")[-1] if "@" in recipient else recipient
                findings.append(
                    f"Send to first-time/unlisted recipient on draft {did} "
                    f"(domain {domain})"
                )
        return findings

    def _check_audit_chain(self) -> list[str]:
        ok, n, bad = verify_chain(self.store.db_path)
        if not ok:
            return [f"AUDIT CHAIN BROKEN at entry {bad} of {n}"]
        return []


def run_monitor(db_path: str | Path, poll_interval_s: float = 60.0) -> AnomalyMonitor:
    """Convenience: open a Store and start a monitor (used by the serve loop)."""
    from ..db.store import Store

    monitor = AnomalyMonitor(Store(db_path), poll_interval_s=poll_interval_s)
    monitor.start()
    return monitor
