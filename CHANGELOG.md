# Changelog

All notable changes to this project are documented here. Dates are ISO-8601.
Published by Laser Lloyd — https://www.laserlloyd.com

## 0.3.1 — 2026-08-19

### Added
- **Demo mode** — `openclaw-email demo` builds a throwaway application home,
  seeds ~25 fictional messages across two example sites, and opens the normal
  UI. No mailbox is configured, and your real config/database are never opened.
- **Live sync chip + Refresh** in the UI header: per-account listener state
  (idle / fetching / error, last sync time) with a manual refresh that wakes the
  IMAP listener instead of waiting for the next poll.
- **Dashboard bulk actions** — multi-select with Read / Unread / Archive /
  Delete / Restore across the triage lists; Delete is a reversible *local*
  trash and never changes the server mailbox.
- **Public edition tooling** — `scripts/scrub_check.py` (standalone denylist
  scanner with `--self-test`) and `scripts/publish_public.py` (allowlisted sync
  → scrub gate → tests → reproducible zip + `PUBLISH-REPORT.md`).
- `OPENCLAW_EMAIL_HOME` relocates config and data together; `run_serve()` and
  `run_ui()` accept an explicit UI port.
- `[compose] signature` configures the name used by templates and the composer.

### Changed
- **Generic by default.** Site ids, display names, drafting guidance, response
  templates, screening vocabulary and every example are configuration now, not
  code. A fresh install ships one `[sites.main]` entry; existing configurations
  keep working unchanged.
- **Response templates are seeded once**, on a brand-new database only, with
  neutral examples per configured site. Existing databases are never re-seeded,
  so edited or deleted templates stay that way across upgrades.
- Inbound screening takes its topical vocabulary and brand names from config
  (`content_terms`, the site's display name) instead of hard-coded terms.

### Security
- Ingest hardening: attachment size/count caps enforced before disk writes,
  duplicate-UID inserts roll back the write lock they took, and messages that
  never reached triage are re-queued instead of being silently dropped.
- Screening hardening: a forged own-domain sender outranks any learned
  "content" label; account/security/payment claims in a subject always require
  direct-site verification; brand mentions alone can never buy CONTENT.
- Test isolation: the suite runs against a throwaway application home, so it can
  never read or write a real configuration.

## 0.2.0
- Quarantine for injection-scored mail, site-scoped reference library, managed
  per-site knowledge handbooks, agent bridge with metadata-only withholding.

## 0.1.0
- First working agent: IMAP ingest, sanitize/normalize, classify, draft,
  guardrails, hash-chained audit log, localhost approval UI, no auto-send.
