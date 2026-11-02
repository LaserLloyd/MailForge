# Changelog

All notable changes to this project are documented here. Dates are ISO-8601.
Published by Laser Lloyd — https://www.laserlloyd.com

## Unreleased (dev)

- Read/unread now syncs with the mail server. Marking mail read or unread in
  MailForge (opening it, bulk Read/Unread, the agent `mark-read` action) sets
  or clears `\Seen` at the provider through the listener's session, in the
  background, retried every cycle until it lands; a failure shows once on the
  sync chip. Mail read in another app shows read here (checked every 5 min and
  at ingest); an unpushed local change always wins. Sending a reply marks the
  mail it answers, and the thread before it, as read. New columns
  `messages.seen_sync_pending` / `seen_sync_error` (idempotent migration).

### Changed
- **No more re-typing the recipient.** Approve & Send no longer waits for the address to be typed
  again. The pre-send popup shows the recipient large, and an address you have never written to (not
  allowlisted, not in the thread) adds an overridable "You haven't emailed this address before" row,
  covered by the usual "I've read these warnings" tick. Invalid or blocked recipients still stop the
  send.

### Added
- **Install MailForge as an app (PWA).** A web app manifest (`/manifest.webmanifest`: name, standalone,
  dark theme colours, 192/512/maskable gold-envelope icons), a pass-through service worker (`/sw.js`;
  it never caches or answers anything, so no mail is stored) and the head tags let Chrome offer
  "Install app". The routes hold no data, sit before the sign-in gate and stay behind the Host/Origin
  guard. Icons regenerate with `scripts/make_pwa_icons.py`.
- **PIN or password sign-in for the web UI**, so a phone can open it without a launcher link.
  `mailforge ui-pin set | clear | status` (the secret is typed twice at a hidden prompt or piped on
  stdin, never on the command line; a numeric PIN needs 6+ digits, a password 8+ characters). Only a
  salted PBKDF2-HMAC-SHA256 hash (200,000 rounds) is stored, in `ui_pin` beside `ui_launcher_key`
  (mode 0600). With a PIN set, a visitor who is not signed in sees a plain sign-in page (with
  "Remember this device": 30 days, otherwise until the browser closes) instead of "Access denied";
  a header "Sign out" button ends the session. Wrong tries are throttled per device (5, then a
  growing wait), per client address (15) and install-wide (60 in 10 minutes); the client address
  comes from `X-Forwarded-For` only when the request arrives from loopback (a same-machine proxy). Every
  sign-in, failure, lockout and sign-out is written to the audit log. Changing or clearing the PIN
  signs every browser out. The launcher key and one-shot token keep working, and with no PIN set
  nothing changes.
### Changed
- **The web UI now fits phones, tablets and foldables.** Below 1100 px the sidebar becomes a drawer
  (hamburger in the header) so pages get the full width. In the Inbox the list and the reading pane
  sit side by side only when the page is at least 780 px wide (the pane never drops under about
  420 px); narrower, the list and the open message take turns and a "Back to inbox" button returns
  to the list. The reading pane's subject, action buttons and notices wrap by the pane's own width
  (two-column button grid, status cards stack), the bulk-action bar drops its keyboard hint, and
  Outbox / Activity rows stack instead of squeezing text into a sliver. Buttons are at least 40 px
  tall under 1100 px. Layout and CSS only; desktop widths look the same. New `tests/ui/` check
  (`check_responsive.py`, see its README) measures horizontal scroll, one-letter-per-line text and
  overlapping buttons at five viewport sizes against a scratch instance.

### Removed
- **The bridge no longer has `prepare-send` and `send`.** Agents can read, draft, edit and triage;
  only a human sends, in the MailForge UI. The `openclaw.agent_send_*` config keys are still read so
  older configs load, but nothing uses them.

### Fixed
- **Mail accounts no longer stay offline after a boot with a locked keyring.** A credential read that
  started while the login keyring was locked could hang forever, even after the keyring was unlocked,
  and every later read for that account waited on the stuck one, so its IMAP listener never
  reconnected until MailForge was restarted. A read stuck for more than 2 minutes is now abandoned
  and tried again (the wait doubles each time, up to 30 minutes, so a keyring that stays locked can't
  pile up threads). Nothing to configure.
- **Replying to someone who wrote to you is no longer blocked** just because their domain isn't in the
  automatically built allowlist yet (rule b). A cc-only address still needs the allowlist, and
  explicitly blocked domains still block.
- **A reply to mail you sent is no longer screened as possible spam.** On content-only sites a vendor's
  answer to your own email was marked POTENTIAL SPAM (and hidden from agents) because it doesn't talk
  about the site's topics. Mail in a thread you started is now trusted, unless a human marked the sender
  as spam or the sender's authentication failed. Run `mailforge rescreen` to re-check existing mail.
- **Order and reference numbers such as `249-5011686-7907863` are no longer flagged as phone numbers.**

### Added
- **Agents can work with drafts** through the OpenClaw bridge: `drafts` (list), `draft` (read one),
  `reply` (save the agent's own reply text under a message, PENDING for human approval; shares the
  compose daily cap) and `edit-draft` (replace a draft's text; refused unchanged if the guardrails
  fail; the old text stays in the revision history). None of them can send.

## 0.7.1 — 2026-11-01

### Changed
- **The GPL `html2text` dependency is gone.** HTML-to-text now uses `markdownify` (MIT, already in the
  lock file, with `beautifulsoup4`) behind the same lxml + bleach sanitising stages; links, lists,
  emphasis and headings keep the same Markdown shapes, images and in-page `#` links are still dropped.
  The installer and Docker image no longer bundle any GPL-licensed library. Run `uv sync` after
  updating; nothing else to do. Closes #8.
- Docs: Docker secret files (`MAILFORGE_KEYRING_PASSWORD_FILE`) must use LF line endings; a CRLF
  file derives a different password.

## 0.7.0 — 2026-11-01

### Changed
- **Licence: MIT No Attribution (MIT-0).** You can use, copy, change and ship MailForge with no
  conditions; no copyright notice has to be kept. You don't need to credit this project — but if
  you do, a link back is appreciated. `LICENSE`, `pyproject.toml`, the Docker image label, the
  README and `docs/LICENSING.md` say so; the scrub check accepts the MIT-0 notice form
  (`Copyright <year> <holder>`, no "(c)"). Releases up to 0.6.0 were MIT. (0.7.1 removed the GPL `html2text` dependency.)
- **UI theme: ForgeUI 1.2.0.** `ui/static/ui-theme/` was a pre-release copy (`ui-theme
  2026-09-24 638060cc7dc6`); it is now the pristine ForgeUI 1.2.0 bundle (`ThemeForge 1.2.0
  82d997e2846d`), vendored by `python -m forgeui vendor` from a new repository-root manifest,
  `forgeui.json`. The picker gains **Slate**, **High Contrast** and **High Contrast Light**
  (opt-in themes stay out; Night Red stays in). The theme list, default (`midnight-gold`) and
  storage key (`mailforge.theme`) now live only in `forgeui.json`; `ui/theme.py` reads a
  packaged copy (`ui/forgeui.json`, kept identical by a test). The unused `clawchat-compat`
  and `dispatch-compat` adapters were dropped. Update procedure: `docs/STYLE-GUIDE.md` section 2.

### Added
- **Agent compose: `mailforge bridge compose` (`to`, `subject`, `body`, optional `from`).** An agent can write a
  NEW email as the same manual draft Compose → Save draft writes (`/compose?draft_id=N`); it never sends —
  `prepare-send` still refuses first-contact recipients, so a human presses Send. Content guardrails scan the
  subject and the body; recipient addresses reject control characters and subjects must be one printable line.
  Explicitly blocked domains still refuse; `openclaw.agent_compose_per_day` (default 20, `0` = off) caps drafts
  per rolling 24 h, counted from the audit log, and guardrail-blocked attempts count toward the cap. Every
  compose and every block is audited. An `audit_log(ts)` index keeps the cap count off a full table scan.
- **Accounts and lockout recovery** (README section of the same name; Docker: `docs/deploy/docker.md`).
  - `mailforge users list | add | passwd | delete`: the mailbox logins under the verbs every forge uses
    (MailForge has no separate web-UI user). `account-passwd <name>` replaces one mailbox's stored IMAP password
    without touching `config.toml` or the database; a stored SMTP password is kept unless `--also-smtp`, a second stdin line
    or `--smtp-password-env` says otherwise (a missing one is filled in). `users add` / `account-add` take the label as an argument.
  - Passwords come from `--password-stdin` or the new `--password-env VAR` (`account-add`, `account-passwd`), never argv.
  - `mailforge unlock status | reset`: where the credential store is, whether it opens, and a wipe for a lost password
    (store file renamed to `*.reset-<time>.bak`, newest 3 kept; `--no-backup` only deletes MailForge's own file, never the shared
    native `python_keyring` one; an OS keyring loses only the configured mailboxes'
    entries). Config, mail database and audit log are kept.
  - `mailforge token show | rotate`: the web-UI launcher URL, and a new launcher key + cookie-signing secret.
  - **`RESET_LOGIN` sentinel** in the data folder (or `MAILFORGE_HOME`, i.e. the Docker volume root): read once at
    `mailforge serve` start, deleted before anything is reset, never replays; if it cannot be deleted nothing is done.
    Empty/`all`, `unlock` or `ui`. No web route reads or creates it (tested).
  - Every `users`, `account-*`, `unlock` and `token` command prints the config and data folders it is acting on (stderr).
- `tests/unit/test_recovery.py`: every verb, both keyring kinds, a real encrypted store with a forgotten
  password, sentinel consumption/replay/unknown content/read-only, and HTTP probes that cannot trigger the sentinel.
- `tests/unit/test_forgeui_vendor.py` and a CI step run `forgeui vendor --check`, so the
  vendored bundle cannot drift or be hand-edited unnoticed.

### Fixed
- **A locked login keyring no longer wedges the IMAP listeners.** At boot a locked Secret Service made
  `keyring.get_password` block forever in an unlock prompt, leaving a listener in state `connecting` with no retry.
  Reads, writes and deletes now run with a timeout (`MAILFORGE_KEYRING_TIMEOUT_S`, default 30 s); one in-flight reader
  per credential, so retries do not pile up threads; one warning per credential until the store answers again
  ("reachable again"); a write or delete that times out raises `credential store did not answer within N s`.
- A wrong `MAILFORGE_KEYRING_PASSWORD` reports `wrong password for the credential store` instead of a raw
  `MAC check failed` traceback (#2).
- Pages requested without a session answer HTTP 403 instead of 200 with an "Access denied" body, so monitors can
  tell denied from served (#4).
- `account-add` / `users add` no longer write environment-derived settings (for example the LLM host) into
  `config.toml`; only the new mailbox is appended. `account-remove` rewrites only the mailbox list the same way (#5).
- The HTTP 401/403 API-key hint from the LLM probe is logged once per process instead of on every `is_up()` call (#6).
- `llm-check` reads served models listed by `path` as well as `id` and `model_key`, matching the model resolver, so a
  server that lists by `path` no longer shows "NO MODELS" (#7).
- A malformed or out-of-range `MAILFORGE_UI_PORT` logs a warning instead of silently falling back (#15).
- A served model id ending in `/` no longer matches every configured id in the model-mismatch hint (#17).
- The UI URL printed for an IPv6 `MAILFORGE_UI_HOST` brackets the address (`http://[::1]:PORT/`) (#18).
- Docs recommend `MAILFORGE_KEYRING_PASSWORD_FILE` (a Docker secret, with a `compose.secret.yml` override recipe)
  over the plain variable, which is visible in `docker inspect` and `docker compose config` (#3).
- Accounts and lockout recovery hardening: `token rotate` reports each key's outcome and exits non-zero when a key
  file could not be rewritten (it used to print "Rotated." regardless); the help now says browsers are signed out at
  the next restart. `RESET_LOGIN` is read without following symlinks, only when owned by you, a UTF-8 BOM is
  accepted, and several sentinel files in one start are all consumed but acted on once. `unlock reset --no-backup`
  treats a store as dedicated only under `MAILFORGE_HOME` or, in a container, `/data`, so a shared native keyring
  file is never hard-deleted. `account-add --password-stdin` strips CRLF. `account-remove` finishes with a clear
  message when the credential purge fails, and credential-save errors name only the exception class.
  `account-list` never prompts for a keyring password (rows read "credentials not checked" when the store is not
  open).
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
- Docker docs told a user who lost the credential password to re-run `setup-wizard`, which rewrites `config.toml`;
  they now point at `unlock reset` + `users passwd`.
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
