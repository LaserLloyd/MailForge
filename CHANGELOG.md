# Changelog

All notable changes to this project are documented here. Dates are ISO-8601.
Published by Laser Lloyd — https://www.laserlloyd.com

## 0.4.0 — 2026-08-24

### Added
- **Spam & Deletion page** (sidebar) — everything the inbound screener held back
  from the AI, in three boxes ordered by how likely it is to be a scam
  (*Potential spam* → *Likely scam or phishing* → *Confirmed spam*), plus the
  **Scheduled for deletion** queue. Each box has select-all, shift-click range
  selection, an inline preview on row click, and bulk **Mark as spam & learn**
  (records human screening feedback so later mail from the same sender or
  subject template is filtered), **Not spam**, and **Delete**.
- **Provider-side delete.** Deleting a message can now remove the copy on the
  mail server as well — `[security] server_delete_mode`: `trash` (default;
  server-side MOVE into the account's Trash/Deleted folder, recoverable there),
  `expunge` (`\Deleted` + EXPUNGE), or `off` (pre-0.4 behaviour). Configurable
  in Settings → Deleting mail.
- **Retention policy.** Spam and scam mail is deleted immediately; everything
  else you delete is held in the local Trash for `[security]
  trash_retention_days` (60) and then permanently removed, locally and at the
  provider. A background sweep runs every six hours; `siftforge
  purge-trash` reports the queue and (with `--yes`) runs it by hand.
- New `messages` columns: `trashed_at`, `purged_at`, `server_deleted_at`,
  `server_delete_error`. Existing trash has its retention clock started at
  upgrade, never backdated to the received date.

### Changed
- Deleting explains what will happen and when, in the Inbox toast, the Trash
  banner, and the delete confirmation — and the toast is no longer destroyed by
  the reload that follows it.
- Trash view gained **Delete permanently**, which skips the holding period.

### Notes
- A purge keeps a one-line tombstone rather than dropping the row: message
  content, links, and attachment files are destroyed, but the learned spam rules
  taught from that message (`message_screening_feedback` cascades on delete)
  survive.
- A provider that refuses or cannot be reached is recorded on the message and
  reported in the UI; the local content is still shredded and the next sweep
  retries the server.

## 0.3.1 — 2026-08-19

### Added
- **Demo mode** — `siftforge demo` builds a throwaway application home,
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
- `SIFTFORGE_HOME` relocates config and data together; `run_serve()` and
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
