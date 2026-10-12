"""Replay human-verified screening labels against the current screener.

Screening is deterministic, so "how good is it" is answerable offline: take
messages a person has judged, re-run ``assess_inbound`` over them with today's
rules, and compare. Two kinds of label count as human:

  * ``message_screening_feedback`` rows (the label the person chose);
  * messages whose ``screening_source`` is ``USER`` without a feedback row
    (a manual status change, e.g. moving mail to FILED); the truth is the
    status the person set.

Evaluation is leave-one-out: the row's own feedback example is removed before
screening, otherwise every learned label would trivially "predict" itself.
Everything runs on an in-memory copy of the database; the live file is only
read.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mail import header_facts
from .rescreen import _site_patterns
from .security.inbound_screening import assess_inbound, normalize_status, sender_domain


@dataclass
class Replay:
    message_id: int
    truth: str
    predicted: str
    source: str  # "feedback" | "manual"
    reason: str
    from_addr: str
    subject: str


@dataclass
class EvalReport:
    replays: list[Replay] = field(default_factory=list)
    skipped_purged: int = 0

    @property
    def matrix(self) -> Counter:
        return Counter((r.truth, r.predicted) for r in self.replays)

    @property
    def truth_classes(self) -> set[str]:
        return {r.truth for r in self.replays}

    @property
    def negatives_exist(self) -> bool:
        """Precision needs at least two truth classes in the sample."""
        return len(self.truth_classes) > 1

    def recall(self, status: str) -> float | None:
        total = sum(1 for r in self.replays if r.truth == status)
        if not total:
            return None
        return sum(1 for r in self.replays if r.truth == status == r.predicted) / total

    def precision(self, status: str) -> float | None:
        if not self.negatives_exist:
            return None
        predicted = sum(1 for r in self.replays if r.predicted == status)
        if not predicted:
            return None
        return sum(1 for r in self.replays if r.truth == status == r.predicted) / predicted


_SQL = (
    "SELECT m.id, m.from_addr, m.from_name, m.subject, m.sanitized_text, m.link_count, "
    "m.has_attachments, m.header_meta, m.screening_status, m.screening_source, "
    "m.received_at, m.purged_at, a.username, a.site_id, "
    "f.id AS fb_id, f.label AS fb_label "
    "FROM messages m JOIN accounts a ON a.id=m.account_id "
    "LEFT JOIN message_screening_feedback f ON f.message_id=m.id "
    "WHERE f.id IS NOT NULL OR COALESCE(m.screening_source,'')='USER' "
    "ORDER BY m.received_at DESC, m.id DESC"
)


def _copy_db(store: Any, dest: Path) -> None:
    out = sqlite3.connect(str(dest))
    try:
        store.conn.backup(out)
    finally:
        out.close()


def evaluate(store: Any, settings: Any, *, limit: int = 200) -> EvalReport:
    """Replay the ``limit`` most recent human-labelled messages."""
    from .db.store import open_store

    report = EvalReport()
    rows = store.conn.execute(_SQL).fetchall()
    # One truth per message: the newest feedback row wins; manual status otherwise.
    chosen: dict[int, Any] = {}
    for row in rows:
        mid = int(row["id"])
        if mid not in chosen or (
            row["fb_id"] is not None
            and (chosen[mid]["fb_id"] is None or row["fb_id"] > chosen[mid]["fb_id"])
        ):
            chosen[mid] = row
    picked = list(chosen.values())[: max(1, int(limit))]
    if not picked:
        return report

    with tempfile.TemporaryDirectory(prefix="mailforge-eval-") as tmp:
        copy_path = Path(tmp) / "eval.db"
        _copy_db(store, copy_path)
        with open_store(copy_path, init=False) as work:
            patterns: dict[str, tuple[Any, Any]] = {}
            for row in picked:
                if row["purged_at"]:
                    report.skipped_purged += 1
                    continue
                site_id = str(row["site_id"] or "")
                if site_id not in patterns:
                    patterns[site_id] = _site_patterns(settings, site_id)
                terms, brand = patterns[site_id]
                if row["fb_id"] is not None:
                    truth = normalize_status(row["fb_label"])
                    source = "feedback"
                    # Leave one out: drop this row's own example, then roll back.
                    work.conn.execute(
                        "DELETE FROM message_screening_feedback WHERE id=?", (row["fb_id"],)
                    )
                else:
                    truth = normalize_status(row["screening_status"])
                    source = "manual"
                try:
                    examples = list(work.screening_examples(site_id))
                    domains = work.learned_spam_domains(site_id)
                finally:
                    work.conn.rollback()
                own = sender_domain(row["username"])
                result = assess_inbound(
                    mode=settings.site_screening_mode(site_id),
                    from_addr=str(row["from_addr"] or ""),
                    from_name=str(row["from_name"] or ""),
                    subject=str(row["subject"] or ""),
                    text=str(row["sanitized_text"] or ""),
                    link_count=int(row["link_count"] or 0),
                    has_attachments=bool(row["has_attachments"]),
                    learned_examples=examples,
                    own_domains=(own,) if own else (),
                    site_terms=terms,
                    brand_terms=brand,
                    headers=header_facts.loads(row["header_meta"]),
                    category_rules=settings.category_rules,
                    site_id=site_id,
                    learned_spam_domains=domains,
                    brand_domains=settings.security.impersonated_brands,
                )
                report.replays.append(
                    Replay(
                        int(row["id"]), truth, result.status, source, result.reason,
                        str(row["from_addr"] or ""),
                        " ".join(str(row["subject"] or "").split())[:60],
                    )
                )
    return report


def format_report(report: EvalReport) -> list[str]:
    lines: list[str] = []
    n = len(report.replays)
    lines.append(f"Replayed {n} human-labelled message(s), leave-one-out.")
    if report.skipped_purged:
        lines.append(f"  ({report.skipped_purged} purged message(s) skipped: no content left.)")
    if not n:
        lines.append("No human labels to evaluate.")
        return lines
    classes = sorted(report.truth_classes | {r.predicted for r in report.replays})
    truths = sorted(report.truth_classes)
    width = max(14, *(len(c) for c in classes)) + 2
    lines.append("Confusion matrix (rows = human label, columns = screener now):")
    lines.append("  " + "".ljust(width) + "".join(c.rjust(width) for c in classes))
    matrix = report.matrix
    for t in truths:
        lines.append(
            "  " + t.ljust(width) + "".join(str(matrix.get((t, p), 0)).rjust(width) for p in classes)
        )
    lines.append("Per status:")
    for c in classes:
        rec, prec = report.recall(c), report.precision(c)
        rec_s = "n/a (no labels of this status)" if rec is None else f"{rec:.0%}"
        if prec is None:
            prec_s = "n/a"
        else:
            prec_s = f"{prec:.0%}"
        lines.append(f"  {c:<16} recall {rec_s:<32} precision {prec_s}")
    if not report.negatives_exist:
        lines.append(
            "No negative labels — recall only: every label is "
            f"{next(iter(report.truth_classes))}, so precision cannot be measured."
        )
    misses = [r for r in report.replays if r.truth != r.predicted]
    if misses:
        lines.append(f"Disagreements ({len(misses)}):")
        for r in misses[:20]:
            lines.append(
                f"  #{r.message_id:>6} human {r.truth} -> now {r.predicted}  "
                f"{r.from_addr[:36]}  {r.subject}"
            )
    return lines
