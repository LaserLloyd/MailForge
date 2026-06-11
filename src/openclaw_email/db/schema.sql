-- OpenClaw Email Agent — SQLite schema (build spec §7).
-- One file, chmod 0600 at creation (enforced in store.py), sqlite-vec loaded
-- as an extension when available.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,                 -- imap|smtp
  host TEXT, port INTEGER, username TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, folder TEXT NOT NULL,
  uid INTEGER NOT NULL, message_id TEXT, thread_id TEXT NOT NULL,
  from_addr TEXT, from_name TEXT, to_addrs TEXT, cc_addrs TEXT,
  subject TEXT, received_at TEXT, raw_html BLOB, sanitized_text TEXT,
  has_attachments INTEGER, link_count INTEGER,
  seen INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
  UNIQUE(account_id, folder, uid));
CREATE INDEX IF NOT EXISTS msg_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS msg_from   ON messages(from_addr);

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

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, source_kind TEXT,                       -- context_doc|thread_history|style_guide
  source_id TEXT, thread_id TEXT, text TEXT, token_count INTEGER);

CREATE TABLE IF NOT EXISTS recipient_allowlist (
  addr TEXT PRIMARY KEY, domain TEXT, first_seen TEXT, last_seen TEXT,
  msg_count INTEGER, source TEXT);                                -- sent_folder|contact|manual

CREATE TABLE IF NOT EXISTS app_config (id INTEGER PRIMARY KEY CHECK (id=1), config_json TEXT);
