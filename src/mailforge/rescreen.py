"""Re-run deterministic inbound screening over stored mail.

Screening normally runs once, at ingest. After the rules, a site's
vocabulary, the category rules or the learned human examples change, this
re-applies them to mail already in the store — dry run by default.

Safety rules (each one is tested):
  * a human decision is never overridden (``screening_source='USER'``), nor a
    model spam verdict (``'LLM'``);
  * mail already withheld from AI is never released to an AI-readable status
    here — that is a human's call in the UI (reported as "kept withheld");
  * trashed and purged mail is left alone unless ``include_trashed`` (purged
    rows never: they have no content left);
  * mail the AI may currently read (UNSCREENED/CONTENT) is only moved by a
    spam/phishing/category RULE verdict (SPAM, FILED, or a rule category), not
    by a site's catch-all default — so switching a site to ``content_only``
    does not retro-flag its old mail (``all_statuses`` overrides this);
  * a rule category is written as a ``rule:<name>`` classification; a stale
    rule classification is removed when its rule no longer matches.
Stored rows carry no headers from before header capture existed, so rules
that need header facts only see the facts recorded since.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .mail import header_facts
from .security.inbound_screening import (
    ScreeningResult,
    ai_may_read,
    assess_inbound,
    brand_pattern,
    normalize_status,
    sender_domain,
    terms_pattern,
)

log = logging.getLogger(__name__)

#: Screening sources this tool may rewrite. USER (human) and LLM (model spam
#: verdict) are never touched.
REWRITABLE_SOURCES = ("AUTO_POLICY", "AUTO_RULE", "LEARNED", "RULE")


@dataclass
class Change:
    message_id: int
    account: str
    before: str
    after: str
    reason: str
    category: str
    from_addr: str
    subject: str


@dataclass
class RescreenReport:
    examined: int = 0
    before: Counter = field(default_factory=Counter)
    after: Counter = field(default_factory=Counter)
    changes: list[Change] = field(default_factory=list)
    kept_withheld: list[Change] = field(default_factory=list)
    kept_readable: list[Change] = field(default_factory=list)
    categories: Counter = field(default_factory=Counter)
    applied: bool = False

    def transitions(self) -> Counter:
        return Counter((c.before, c.after) for c in self.changes)


def _site_patterns(settings: Any, site_id: str) -> tuple[Any, Any]:
    site = settings.sites.get(str(site_id or "").lower())
    if site is None:
        return None, None
    terms = terms_pattern(getattr(site, "content_terms", ()) or ())
    brand = brand_pattern([site.name, *(getattr(site, "brand_terms", ()) or ())])
    return terms, brand


def _candidates(store: Any, *, site: str | None, include_trashed: bool) -> list[Any]:
    sql = (
        "SELECT m.id, m.from_addr, m.from_name, m.subject, m.sanitized_text, "
        "m.link_count, m.has_attachments, m.screening_status, m.screening_source, "
        "m.header_meta, m.trashed, a.name AS account_name, a.username, a.site_id, "
        "c.category AS cur_category, c.model_used AS cur_model "
        "FROM messages m JOIN accounts a ON a.id=m.account_id "
        "LEFT JOIN classifications c ON c.message_id=m.id "
        "WHERE m.purged_at IS NULL AND m.archived=0"
    )
    params: list[Any] = []
    if not include_trashed:
        sql += " AND m.trashed=0"
    if site:
        sql += " AND a.site_id=?"
        params.append(site)
    sql += " ORDER BY m.id"
    return store.conn.execute(sql, tuple(params)).fetchall()


def rescreen(
    store: Any,
    settings: Any,
    *,
    site: str | None = None,
    dry_run: bool = True,
    include_trashed: bool = False,
    all_statuses: bool = False,
) -> RescreenReport:
    """Re-screen stored mail; apply the result unless ``dry_run``."""
    report = RescreenReport()
    examples_cache: dict[str, list[Any]] = {}
    learned_domains_cache: dict[str, set[str]] = {}
    patterns_cache: dict[str, tuple[Any, Any]] = {}
    rows = _candidates(store, site=site, include_trashed=include_trashed)
    planned: list[tuple[Any, ScreeningResult]] = []
    for row in rows:
        site_id = str(row["site_id"] or "")
        before = normalize_status(row["screening_status"])
        report.before[before] += 1
        source = str(row["screening_source"] or "AUTO_POLICY").upper()
        if source not in REWRITABLE_SOURCES:
            report.after[before] += 1
            continue
        report.examined += 1
        if site_id not in examples_cache:
            examples_cache[site_id] = list(store.screening_examples(site_id))
            learned_domains_cache[site_id] = store.learned_spam_domains(site_id)
            patterns_cache[site_id] = _site_patterns(settings, site_id)
        terms, brand = patterns_cache[site_id]
        own = sender_domain(row["username"])
        result = assess_inbound(
            mode=settings.site_screening_mode(site_id),
            from_addr=str(row["from_addr"] or ""),
            from_name=str(row["from_name"] or ""),
            subject=str(row["subject"] or ""),
            text=str(row["sanitized_text"] or ""),
            link_count=int(row["link_count"] or 0),
            has_attachments=bool(row["has_attachments"]),
            learned_examples=examples_cache[site_id],
            own_domains=(own,) if own else (),
            site_terms=terms,
            brand_terms=brand,
            headers=header_facts.loads(row["header_meta"]),
            category_rules=settings.category_rules,
            site_id=site_id,
            learned_spam_domains=learned_domains_cache[site_id],
            brand_domains=settings.security.impersonated_brands,
        )
        after = result.status
        change = Change(
            int(row["id"]), str(row["account_name"] or ""), before, after,
            result.reason, result.category, str(row["from_addr"] or ""),
            " ".join(str(row["subject"] or "").split())[:90],
        )
        if after != before and ai_may_read(after) and not ai_may_read(before):
            # Never release withheld mail to AI from a batch tool.
            report.kept_withheld.append(change)
            report.after[before] += 1
            continue
        if (
            not all_statuses
            and after != before
            and ai_may_read(before)
            and after not in {"SPAM", "FILED"}
            and not result.category
        ):
            report.kept_readable.append(change)
            report.after[before] += 1
            continue
        report.after[after] += 1
        if result.category:
            report.categories[result.category] += 1
        cur_model = str(row["cur_model"] or "")
        category_change = bool(result.category) and (
            str(row["cur_category"] or "").upper() != result.category
            or cur_model != f"rule:{result.rule}"
        )
        stale_rule = not result.category and cur_model.startswith("rule:")
        if after != before or category_change or stale_rule:
            if after != before:
                report.changes.append(change)
            planned.append((row, result))
    if dry_run or not planned:
        return report
    from .audit.log import AuditLog

    from .db.store import _now

    conn = store.conn
    now = _now()
    try:
        for row, result in planned:
            mid = int(row["id"])
            conn.execute(
                "UPDATE messages SET screening_status=?,screening_reason=?,screening_source=? "
                "WHERE id=? AND COALESCE(screening_source,'AUTO_POLICY') NOT IN ('USER','LLM')",
                (result.status, result.reason[:500], result.source[:40], mid),
            )
            if result.category:
                conn.execute(
                    "INSERT INTO classifications(message_id,category,priority,rationale,"
                    "injection_risk,classified_at,model_used) VALUES(?,?,?,?,NULL,?,?) "
                    "ON CONFLICT(message_id) DO UPDATE SET category=excluded.category,"
                    "priority=excluded.priority,rationale=excluded.rationale,"
                    "classified_at=excluded.classified_at,model_used=excluded.model_used",
                    (mid, result.category, int(result.priority),
                     f"sender rule {result.rule}", now, f"rule:{result.rule}"),
                )
            elif str(row["cur_model"] or "").startswith("rule:"):
                conn.execute(
                    "DELETE FROM classifications WHERE message_id=? AND model_used LIKE 'rule:%'",
                    (mid,),
                )
            if not ai_may_read(result.status):
                # Mirror ingest: pending AI drafts of now-withheld mail retire.
                conn.execute(
                    "UPDATE drafts SET state='REJECTED',updated_at=? "
                    "WHERE message_id=? AND state IN ('PENDING','BLOCKED','DEFERRED_NO_LLM')",
                    (now, mid),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    report.applied = True
    try:
        AuditLog(store).record(
            "user",
            "tool_call",
            detail={
                "action": "rescreen",
                "site": site or "all",
                "rows_changed": len(planned),
                "transitions": {f"{a}->{b}": n for (a, b), n in report.transitions().items()},
            },
        )
    except Exception:  # noqa: BLE001 — the rescreen itself succeeded
        log.exception("audit append failed for rescreen")
    return report
