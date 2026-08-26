-- SiftForge — SQLite schema (build spec §7).
-- One file, chmod 0600 at creation (enforced in store.py), sqlite-vec loaded
-- as an extension when available.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- site_id values are validated in Python against the configured site registry
-- (config.toml [sites]; default 'main') — no CHECK so new sites can
-- be added by config alone. Legacy DBs with the old CHECK are rebuilt lazily
-- by store._ensure_site_capacity() only when a non-legacy site is configured.
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,                 -- imap|smtp
  host TEXT, port INTEGER, username TEXT,
  site_id TEXT NOT NULL DEFAULT 'main'
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, folder TEXT NOT NULL,
  uid INTEGER NOT NULL, message_id TEXT, thread_id TEXT NOT NULL,
  from_addr TEXT, from_name TEXT, to_addrs TEXT, cc_addrs TEXT,
  subject TEXT, received_at TEXT, raw_html BLOB, sanitized_text TEXT,
  has_attachments INTEGER, link_count INTEGER,
  seen INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
  -- Local, reversible trash. Whether the provider copy is also removed is
  -- decided by security.server_delete_mode; the two columns below record what
  -- actually happened at the server so the UI never claims more than it did.
  trashed INTEGER NOT NULL DEFAULT 0,
  trashed_at TEXT,                    -- starts the retention clock
  purged_at TEXT,                     -- content shredded + removed at the provider
  server_deleted_at TEXT,
  server_delete_error TEXT,
  -- Quarantine (prompt-injection containment): 1 => no LLM may read this
  -- message and the OpenClaw bridge returns metadata only, until a human
  -- releases it in the UI.
  quarantined INTEGER NOT NULL DEFAULT 0,
  quarantine_reason TEXT,
  -- Site-scoped pre-AI screening. Questionable/spam bodies are human-only;
  -- CONTENT may enter the normal classify/draft workflow.
  screening_status TEXT NOT NULL DEFAULT 'UNSCREENED',
  screening_reason TEXT,
  screening_source TEXT NOT NULL DEFAULT 'AUTO_POLICY',
  UNIQUE(account_id, folder, uid));
CREATE INDEX IF NOT EXISTS msg_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS msg_from   ON messages(from_addr);
-- Human alignment examples. learn_similar=1 enables exact-sender or stable
-- subject-pattern matching for future messages; bodies are never copied here.
CREATE TABLE IF NOT EXISTS message_screening_feedback (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  site_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  label TEXT NOT NULL CHECK (
    label IN ('CONTENT','POTENTIAL_SPAM','POTENTIAL_ISSUE','SPAM')
  ),
  learn_similar INTEGER NOT NULL DEFAULT 0,
  sender_addr TEXT,
  sender_domain TEXT,
  subject_signature TEXT,
  note TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS screening_feedback_site
  ON message_screening_feedback(site_id, learn_similar, id DESC);

-- Attachment files saved at ingest (size-capped); bytes live on disk under
-- the app data dir, never in the DB and never fed to any LLM.
CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,             -- sanitized basename
  content_type TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  stored_path TEXT,                   -- NULL => skipped (over cap); see skipped_reason
  sha256 TEXT,
  skipped_reason TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS attachments_msg ON attachments(message_id);

-- Durable agent memory: what automated agents (OpenClaw bridge callers) and
-- humans observed/did, so a later session never re-diagnoses its own past
-- actions as an incident (e.g. self-caused GitHub verification bursts).
-- One-time authorizations for agent-relayed sends ("send it" flow): issued by
-- the bridge prepare-send op AFTER guardrails pass, consumed exactly once by
-- send. The token itself is never stored — only its sha256.
CREATE TABLE IF NOT EXISTS send_authorizations (
  id INTEGER PRIMARY KEY,
  draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  body_sha256 TEXT NOT NULL,          -- draft content must not change after prepare
  recipient TEXT NOT NULL,
  from_addr TEXT NOT NULL,
  author TEXT NOT NULL,               -- requesting agent id (e.g. email-main)
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT);
CREATE INDEX IF NOT EXISTS send_auth_draft ON send_authorizations(draft_id);

-- Durable record of every transmission ATTEMPT (Outbox / Sent page).
-- One row is written BEFORE the socket opens with outcome 'UNKNOWN'; the
-- result is stamped afterwards. A row left at 'UNKNOWN' therefore means the
-- process died (or was killed) between authorisation and result — it is shown
-- as its own explicit state and never as "sent".
--   outcome: UNKNOWN (in flight / result never recorded) | SENT | FAILED
--   origin:  ui_draft | ui_compose | agent_bridge
--   imap_append: PENDING | OK | SKIPPED | FAILED  (never affects `outcome`)
-- draft_id/authorization_id are ON DELETE SET NULL: deleting a draft must not
-- erase the evidence that mail went out.
CREATE TABLE IF NOT EXISTS sent_messages (
  id INTEGER PRIMARY KEY,
  draft_id INTEGER REFERENCES drafts(id) ON DELETE SET NULL,
  authorization_id INTEGER REFERENCES send_authorizations(id) ON DELETE SET NULL,
  site_id TEXT NOT NULL DEFAULT 'main',
  origin TEXT NOT NULL DEFAULT 'ui_compose',
  from_addr TEXT NOT NULL,
  to_addrs TEXT NOT NULL,
  subject TEXT,
  body TEXT,                          -- verbatim, exactly as transmitted
  outcome TEXT NOT NULL DEFAULT 'UNKNOWN'
    CHECK (outcome IN ('UNKNOWN','SENT','FAILED')),
  error_text TEXT,
  smtp_message_id TEXT,
  imap_append TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (imap_append IN ('PENDING','OK','SKIPPED','FAILED')),
  imap_folder TEXT,
  imap_note TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT);
CREATE INDEX IF NOT EXISTS sent_messages_created ON sent_messages(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS sent_messages_site ON sent_messages(site_id, created_at DESC);
CREATE INDEX IF NOT EXISTS sent_messages_auth ON sent_messages(authorization_id);

CREATE TABLE IF NOT EXISTS agent_notes (
  id INTEGER PRIMARY KEY,
  site_id TEXT NOT NULL,
  author TEXT NOT NULL,               -- e.g. email-main|user|claude-code
  kind TEXT NOT NULL DEFAULT 'observation'
    CHECK (kind IN ('observation','action','incident','context')),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  related_message_ids TEXT,           -- JSON int array, optional
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS agent_notes_site ON agent_notes(site_id, id DESC);

CREATE TABLE IF NOT EXISTS mailbox_cursors (
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  folder TEXT NOT NULL,
  last_uid INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_id, folder));

-- Controller-side link table: real URL targets the LLM never sees (spec §4.5).
CREATE TABLE IF NOT EXISTS message_links (
  id INTEGER PRIMARY KEY, message_id INTEGER REFERENCES messages(id),
  symbol TEXT NOT NULL,              -- e.g. link_3
  target TEXT NOT NULL,             -- real resolved URL
  display_text TEXT);
CREATE INDEX IF NOT EXISTS link_msg ON message_links(message_id);

CREATE TABLE IF NOT EXISTS classifications (
  message_id INTEGER PRIMARY KEY REFERENCES messages(id),
  category TEXT, priority INTEGER, rationale TEXT,
  injection_risk REAL, classified_at TEXT, model_used TEXT);

CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY, message_id INTEGER REFERENCES messages(id),
  thread_id TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT, body TEXT,
  sender_addr TEXT, site_id TEXT NOT NULL DEFAULT 'main',
  state TEXT NOT NULL,                       -- DRAFT|PENDING|APPROVED|REJECTED|SENT|BLOCKED|DEFERRED_NO_LLM
  guardrail_flags TEXT,                      -- JSON: which guards fired + scores
  created_at TEXT, updated_at TEXT, approved_by TEXT, approved_at TEXT, sent_at TEXT);
CREATE INDEX IF NOT EXISTS draft_state ON drafts(state);
CREATE INDEX IF NOT EXISTS draft_msg     ON drafts(message_id);
CREATE INDEX IF NOT EXISTS draft_created ON drafts(created_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,   -- agent|user|guardrail
  event TEXT NOT NULL,                                            -- llm_call|tool_call|approval|send|block|error
  subject_table TEXT, subject_id INTEGER, detail_json TEXT,        -- redacted before write
  prev_hash TEXT, this_hash TEXT);                                -- sha256 chain

CREATE TABLE IF NOT EXISTS reference_documents (
  id INTEGER PRIMARY KEY,
  site_id TEXT NOT NULL,
  filename TEXT NOT NULL, stored_path TEXT NOT NULL, mime_type TEXT,
  managed_key TEXT, content_sha256 TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'UPLOADED'
    CHECK (status IN ('UPLOADED','INDEXING','READY','ERROR')),
  error_text TEXT, chunk_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS reference_documents_site
  ON reference_documents(site_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS reference_documents_managed
  ON reference_documents(managed_key) WHERE managed_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, source_kind TEXT,                       -- context_doc|thread_history|style_guide
  source_id TEXT, thread_id TEXT, text TEXT, token_count INTEGER,
  site_id TEXT NOT NULL DEFAULT 'main',
  reference_document_id INTEGER REFERENCES reference_documents(id) ON DELETE CASCADE);

CREATE TABLE IF NOT EXISTS ai_revisions (
  id INTEGER PRIMARY KEY,
  draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('user','assistant')),
  feedback TEXT, body TEXT, model_used TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ai_revisions_draft ON ai_revisions(draft_id, id);

CREATE TABLE IF NOT EXISTS response_templates (
  id INTEGER PRIMARY KEY,
  site_id TEXT NOT NULL,
  name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'General',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  shortcut TEXT NOT NULL DEFAULT '',
  is_system INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(site_id, name));
CREATE INDEX IF NOT EXISTS response_templates_site
  ON response_templates(site_id, category, name);

CREATE TABLE IF NOT EXISTS recipient_allowlist (
  addr TEXT PRIMARY KEY, domain TEXT, first_seen TEXT, last_seen TEXT,
  msg_count INTEGER, source TEXT);                                -- sent_folder|contact|manual

CREATE TABLE IF NOT EXISTS app_config (id INTEGER PRIMARY KEY CHECK (id=1), config_json TEXT);
