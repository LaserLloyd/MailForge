# Changelog

All notable changes to this project are documented here. Dates are ISO-8601.
Published by Laser Lloyd — https://www.laserlloyd.com

## Unreleased

### Changed
- **UI theme: ForgeUI 1.2.0.** `ui/static/ui-theme/` was a pre-release copy (`ui-theme
  2026-09-24 638060cc7dc6`); it is now the pristine ForgeUI 1.2.0 bundle (`ThemeForge 1.2.0
  82d997e2846d`), vendored by `python -m forgeui vendor` from a new repository-root manifest,
  `forgeui.json`. The picker gains **Slate**, **High Contrast** and **High Contrast Light**
  (opt-in themes stay out; Night Red stays in). The theme list, default (`midnight-gold`) and
  storage key (`mailforge.theme`) now live only in `forgeui.json`; `ui/theme.py` reads a
  packaged copy (`ui/forgeui.json`, kept identical by a test). The unused `clawchat-compat`
  and `dispatch-compat` adapters were dropped. Update procedure: `docs/STYLE-GUIDE.md` section 2.

### Added
- `tests/unit/test_forgeui_vendor.py` and a CI step run `forgeui vendor --check`, so the
  vendored bundle cannot drift or be hand-edited unnoticed.

### Fixed
- **Bridge `status` and `reference-search` say why the LLM link is down.** A process started
  before the API key existed (e.g. an agent gateway running the plugin) got `model.up: false`
  with empty `notes`, and `reference-search` returned an empty result that read as "the
  references say nothing". `status.model.notes` now names an unreachable server, a missing key
  (and that a long-running parent must be restarted to see it) or a refused key;
  `reference-search` adds `searched: false` and a `note` when the server or the embedding model
  did not answer (additive fields).
- LLM warnings name a timeout (`ReadTimeout (no answer within the client timeout)`) instead of
  ending in `failed: ` with no cause; the 401 hint also mentions `STUDIOFORGE_API_KEY`.
- Docs used a look-alike allow marker the scrub gate does not recognise; public repository and image
  URLs now carry the real `scrub-check: allow` pragma (a test rejects the look-alike).
- `scripts/scrub_check.py` no longer reports the bundle's `files.json` sha256 table as
  `secret-blob` (that one file, that one category, and only 64-hex digests; any other
  blob in it is still reported; covered by a test).
- **Container: an `https://` `LLM_BASE_URL` stays https.** The entrypoint reduced it to a
  bare `host:port`, so the bridge spoke plain HTTP and sent the API key in clear.
- **Container: `docker compose exec` commands unlock the credential store** (they skip the
  entrypoint; the keyring helper now reads `MAILFORGE_KEYRING_PASSWORD[_FILE]` itself). A wrong
  password is a plain "did not unlock ... wrong password?" error, not a `MAC check failed`
  traceback.
- **Container: the `compose.puid.yml` override works for a PUID other than 1000** (setpriv
  `--init-groups` aborted because the read-only image has no passwd entry for that UID).
- `MAILFORGE_UI_ALLOWED_HOSTS` / `UI_EXTRA_HOSTS`: an entry on `:443`/`:80` also accepts the bare
  name browsers send, so a TLS reverse proxy no longer gets 403 on every request.
- The PyInstaller spec bundles `ui/static/` and `ui/forgeui.json` (the UI could not import
  without the manifest); `[tool.uv] cache-keys` cover every package-data kind.
- `mailforge llm-check` no longer prints OK when `/v1/models` lists no models or is not a model
  list; a heavy-lift 401 names `openclaw.heavy_lift_key_env`, not the local key variable.
- Tests: the hermetic socket guard now also covers asyncio on Windows (the proactor loop
  bypassed it, so async HTTP clients could reach real ports).

## 0.6.0 — 2026-10-18

### Added
- **API key for the LLM server.** `Authorization: Bearer <key>` is now sent on
  every request to `llm.lm_studio_host` (probe, model list, chat, embeddings),
  so MailForge works with a StudioForge gateway that enforces keys. The key
  comes from the environment only: the variable named by the new
  `[llm] api_key_env` (default `MAILFORGE_LLM_API_KEY`), with
  `STUDIOFORGE_API_KEY` as a fallback. It is never written to `config.toml`,
  logged, or shown. The separate heavy-lift endpoint keeps its own key.
- **`mailforge llm-check`**: reports whether the server is reachable, whether
  an API key is set and accepted, and whether the configured chat and
  embedding model ids are ones the server serves (exit 0 ok, 1 fix needed, 2
  unreachable).
- Plain-language model-id mismatch message on the Models page, in
  `mailforge bridge status` (`model.notes`, additive) and in the chat warning
  log. StudioForge names models `publisher/repo/stem`; the shipped defaults
  are LM Studio ids, which it answers with HTTP 404. Behaviour is unchanged:
  the draft is still deferred, but now the reason is stated.
- StudioForge contract tests (`tests/contract_studioforge/`) pinning the
  OpenAI-compatible surface MailForge relies on, derived from the client code.
- `THIRD_PARTY_NOTICES.md` and `docs/LICENSING.md`: `html2text` is
  GPL-3.0-or-later and a core dependency. It stays (owner decision); the notes
  describe the obligations for a bundled installer and the MIT replacements
  (`markdownify`, `html-to-markdown`) with the files they would touch.

### Changed
- Tests are hermetic: a socket guard refuses every connection except to
  ephemeral loopback ports a test opens itself, and every test runs on the
  heuristic fallbacks even when the `security-ml` extra is installed (no Hugging
  Face or spaCy downloads). Tests that exercise the model tier are marked `ml`
  and skip unless the weights are already cached.
- Docs and comments that pointed the heavy-lift example at the StudioForge
  panel port (`:8080`) now point at the gateway (`http://127.0.0.1:1234/v1`).

### Fixed
- `[llm] lm_studio_host` written as an `https://` URL is no longer downgraded
  to plain HTTP (which would have sent the new bearer key in clear); the scheme
  is kept for every request. The LM Studio SDK path still receives a bare
  `host:port`.
- The container workflow now also runs on pushes to `master` (the default
  branch), not only `main`.

### Upgrading
- Nothing to do for existing installs. To use an authenticated server, export
  `MAILFORGE_LLM_API_KEY` and run `mailforge llm-check`. Against StudioForge,
  set `[llm] chat_model` and `embedding_model` to ids the server lists.

## 0.5.0 — 2026-10-10

### Added
- **Stronger spam and phishing screening.** Scored rules on subject, sender
  and provider authentication results file clear cold outreach and brand
  impersonation as spam instead of leaving it in the Inbox as "potential
  spam". Standard sites now get the forged-sender, phishing and learned-spam
  checks too. Add your own providers to `[security].impersonated_brands`.
- **Category rules** (`[[category_rules]]`, GitHub by default): notification
  mail is filed into its own View/Tag entry, kept out of the Inbox and away
  from every AI, with a "Delete all" action. Security and account notices are
  never filed.
- **`mailforge rescreen [--site ID] [--apply]`** re-applies screening and
  category rules to stored mail (dry run by default; never overrides a human
  decision or releases withheld mail to AI).

### Fixed
- **Mark as spam no longer lands on a 500 page.** The View/Tag picker now
  resolves `tag=spam` / `tag=quarantined` to their views, and any stale tag,
  unknown account (including a mailbox removed with `account-remove`) or
  non-numeric page falls back to a valid view instead of crashing. The same
  guard covers the Activity filters and Compose templates.
- Action toasts (spam, archive, restore, release, mark unread) survive the
  navigation that follows them.
- A message that repeatedly fails ingest is skipped after three attempts
  instead of blocking its mailbox; a locked database or full disk never
  counts towards that limit. UIDVALIDITY changes are detected and reported.
- Background tasks (retention sweep, anomaly monitor) use their own database
  connections and keep running after an error; the monitor no longer
  re-reports old sends after a restart.
- Deferred drafts that can never resume are retired instead of retrying
  forever.

### Security
- IMAP connections verify the server certificate and hostname everywhere.
- Provider deletes target single UIDs (UID MOVE, or UID COPY + STORE + UID
  EXPUNGE) after checking the stored Message-ID; a folder-wide EXPUNGE is
  never sent, and a server without MOVE/UIDPLUS is refused.
- SMTP refuses to authenticate without TLS (loopback relays excepted); port
  465 uses implicit TLS.
- The Origin check now covers plain HTTP requests as well as WebSockets.
- Mail can no longer imitate the untrusted-content markers to close its own
  block in a model prompt; subject and sender sit inside the block too.
- One fail-closed gate decides which screening statuses an AI may read.
- Dependencies upgraded (NiceGUI 3.18, FastAPI 0.143, Starlette 1.7, bleach
  6.4, python-multipart 0.0.32, cryptography 50); `pip-audit` is clean for
  the base, `llm` and `rag` installs.

### Upgrading
- Two additive columns (`messages.header_meta`,
  `mailbox_cursors.uidvalidity`) are created on first start.
- Run `mailforge rescreen` (dry run), then `mailforge rescreen --apply`, to
  apply the new rules and the GitHub category to mail already stored.

### Changed
- **Renamed SiftForge to MailForge** (2026-08-27). The project, CLI, package,
  systemd unit, and every path moved to the new name: command `mailforge`,
  env var `MAILFORGE_HOME`, config `~/.config/mailforge/`, data
  `~/.local/share/mailforge/`, keyring services `mailforge.imap.<label>` and
  `mailforge.smtp.<username>`. Existing installs need their config, data, and
  keyring entries migrated; nothing is moved automatically.

### Added
- **Outbox & Sent page** (sidebar) — every outgoing message in one list, newest
  first: those an agent staged for approval and those that were transmitted.
  Each row carries exactly one status chip and there are no ambiguous states —
  *NOT SENT — awaiting approval* (amber), *EXPIRED — never sent* (grey),
  *SENT &lt;local time&gt;* (green), *FAILED — reason* (red), plus two explicit red
  "we do not know" states: a transmission whose result was never recorded, and
  a one-time token consumed with no transmission behind it. Neither ever reads
  as sent. Row detail shows the content verbatim, the mailbox it leaves from,
  and (for staged rows) a link to the existing approval screen. The page has no
  send control of its own.
- **Durable send log** (`sent_messages`). Every send attempt from any path —
  the approval page, Compose, or the agent bridge — records timestamp, mailbox,
  recipients, subject, verbatim body, outcome, SMTP message-id, and the result
  of the Sent-folder copy. The row is written *before* the SMTP socket opens,
  so a crash mid-send is visible rather than silent.
- **Sent mail is appended to the account's IMAP Sent folder** after a
  successful send (folder discovered per account, `Sent` as fallback), so mail
  sent from MailForge shows up in every other mail client. Best effort by
  design: a failed append is noted on the outbox row and never turns a
  delivered message into a failed one.
- **Header chip** beside "Mail checked" counting messages awaiting approval and
  any unresolved send, so a staged message cannot sit unnoticed.

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
  provider. A background sweep runs every six hours; `mailforge
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
- **Demo mode** — `mailforge demo` builds a throwaway application home,
  seeds ~25 fictional messages across two example sites, and opens the normal
  UI. No mailbox is configured, and your real config/database are never opened.
- **Live sync chip + Refresh** in the UI header: per-account listener state
  (idle / fetching / error, last sync time) with a manual refresh that wakes the
  IMAP listener instead of waiting for the next poll.
- **Dashboard bulk actions** — multi-select with Read / Unread / Archive /
  Delete / Restore across the triage lists; Delete is a reversible *local*
  trash and never changes the server mailbox.
- **Publication gate** — `scripts/scrub_check.py` (standalone denylist
  scanner with `--self-test`).
- `MAILFORGE_HOME` relocates config and data together; `run_serve()` and
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
