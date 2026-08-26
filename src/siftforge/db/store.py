"""Data-access layer — ALL SQL lives here (build spec §2).

Owns connection lifecycle, schema init (with ``chmod 0600`` at creation per
§0.6), optional sqlite-vec extension loading, and typed helpers for every
table. Higher layers never write SQL directly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import stat
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

# Embedding dimension must match config LLM embedding model + vec0 column.
EMBED_DIM = 768

DraftState = str  # DRAFT|PENDING|APPROVED|REJECTED|SENT|BLOCKED|DEFERRED_NO_LLM

# Site id used for rows/columns that predate an explicit assignment and for
# schema defaults in NEW tables. Real ids come from config [sites].
DEFAULT_SITE_ID = "main"

# Live registry — config.load_settings() replaces this with the configured
# [sites] table. Kept module-level so CLI paths that touch the store before
# settings load still validate against a sane default.
VALID_SITES: frozenset[str] = frozenset({DEFAULT_SITE_ID})

# Legacy DBs (v0.1/v0.2) carry a hard-coded site_id CHECK constraint; the
# allowed ids are read back out of the stored DDL rather than assumed.
_SITE_CHECK_RE = re.compile(r"CHECK\s*\(\s*site_id\s+IN\s*\(([^)]*)\)", re.IGNORECASE)


def register_sites(site_ids: Iterable[str]) -> None:
    """Install the configured site registry (called by config.load_settings)."""
    global VALID_SITES
    ids = frozenset(str(s).strip().lower() for s in site_ids if str(s).strip())
    if ids:
        VALID_SITES = ids


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema_sql() -> str:
    return resources.files("siftforge.db").joinpath("schema.sql").read_text("utf-8")


def validate_site_id(site_id: str) -> str:
    site = str(site_id or "").strip().lower()
    if site not in VALID_SITES:
        raise ValueError(f"site_id must be one of {sorted(VALID_SITES)}")
    return site


def default_site_id() -> str:
    """The site id to assume when a caller passes none."""
    if DEFAULT_SITE_ID in VALID_SITES:
        return DEFAULT_SITE_ID
    return sorted(VALID_SITES)[0] if VALID_SITES else DEFAULT_SITE_ID


#: Serialises audit-chain appends within this process (see append_audit_chained).
_AUDIT_APPEND_LOCK = threading.Lock()


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(os.path.expanduser(os.path.expandvars(str(db_path)))).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vec_enabled = False
        existed = self.db_path.exists()
        # timeout=30: allow writers to wait up to 30 s for the write lock when
        # two IMAP listener threads backfill concurrently (default 5 s is too
        # short and causes "database is locked" during startup backfill).
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # Explicit WAL + busy_timeout on EVERY connection (listener threads,
        # UI/graph, monitor, CLI): readers never block the writer, and a busy
        # writer waits instead of raising "database is locked" immediately.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # foreign_keys is per-connection; schema.sql only sets it on the
        # connection that runs init, so listener/monitor/CLI connections used
        # to run with ON DELETE CASCADE silently disabled.
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not existed:
            # Restrict to owner read/write BEFORE writing any data (§0.6).
            os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)
        self._load_vec()

    # ----- lifecycle -----
    def _load_vec(self) -> None:
        try:
            import sqlite_vec

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self.vec_enabled = True
        except Exception as e:  # extension or wheel missing → RAG degrades
            log.info("sqlite-vec not available, vector search disabled: %s", e)

    def init_schema(self) -> None:
        # ``schema.sql`` creates indexes after ``CREATE TABLE IF NOT EXISTS``.
        # An older reference_documents table therefore needs its newly indexed
        # columns before the script runs, otherwise SQLite rejects the index.
        self._prepare_legacy_schema()
        self.conn.executescript(_schema_sql())
        self._migrate_schema()
        self._ensure_site_capacity()
        self._seed_response_templates()
        if self.vec_enabled:
            self.conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_embeddings "
                f"USING vec0(chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIM}]);"
            )
        # Guarantee 0600 even if the file pre-existed with looser perms.
        try:
            os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self.conn.commit()

    def _prepare_legacy_schema(self) -> None:
        """Add columns needed by indexes declared in the current schema."""
        table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reference_documents'"
        ).fetchone()
        if table is None:
            return
        present = {r["name"] for r in self.conn.execute("PRAGMA table_info(reference_documents)")}
        if "managed_key" not in present:
            self.conn.execute("ALTER TABLE reference_documents ADD COLUMN managed_key TEXT")
        if "content_sha256" not in present:
            self.conn.execute("ALTER TABLE reference_documents ADD COLUMN content_sha256 TEXT")
        self.conn.commit()

    def _migrate_schema(self) -> None:
        """Forward-only additive migrations for databases created by v0.1/v0.2."""

        def columns(table: str) -> set[str]:
            return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

        additions = {
            "accounts": {
                "site_id": f"TEXT NOT NULL DEFAULT '{DEFAULT_SITE_ID}'",
            },
            "messages": {
                "quarantined": "INTEGER NOT NULL DEFAULT 0",
                "quarantine_reason": "TEXT",
                "screening_status": "TEXT NOT NULL DEFAULT 'UNSCREENED'",
                "screening_reason": "TEXT",
                "screening_source": "TEXT NOT NULL DEFAULT 'AUTO_POLICY'",
                "trashed": "INTEGER NOT NULL DEFAULT 0",
                "trashed_at": "TEXT",
                "purged_at": "TEXT",
                "server_deleted_at": "TEXT",
                "server_delete_error": "TEXT",
            },
            "drafts": {
                "sender_addr": "TEXT",
                "site_id": f"TEXT NOT NULL DEFAULT '{DEFAULT_SITE_ID}'",
            },
            "chunks": {
                "site_id": f"TEXT NOT NULL DEFAULT '{DEFAULT_SITE_ID}'",
                "reference_document_id": "INTEGER REFERENCES reference_documents(id) ON DELETE CASCADE",
            },
            "reference_documents": {
                "managed_key": "TEXT",
                "content_sha256": "TEXT",
            },
        }
        for table, wanted in additions.items():
            present = columns(table)
            for name, ddl in wanted.items():
                if name not in present:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS chunks_site ON chunks(site_id, source_kind)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS chunks_reference_document ON chunks(reference_document_id)"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS reference_documents_managed "
            "ON reference_documents(managed_key) WHERE managed_key IS NOT NULL"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS msg_screening "
            "ON messages(screening_status, received_at DESC)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS msg_trash_retention "
            "ON messages(trashed, purged_at, trashed_at)"
        )
        # Retention clock starts when this version first runs, never earlier.
        # Mail deleted before the holding period existed has no trashed_at, and
        # falling back to received_at would make anything old enough due for
        # PERMANENT, provider-side deletion on the very first sweep — deleting
        # it under a policy its owner never agreed to. Idempotent: after this,
        # every trashed row has a stamp.
        self.conn.execute(
            "UPDATE messages SET trashed_at=? WHERE trashed=1 AND trashed_at IS NULL",
            (_now(),),
        )

    # DDL used to rebuild legacy tables whose site_id CHECK constraint would
    # reject a newly configured site. Must stay column-compatible with
    # schema.sql (same names/order is not required; INSERT is by column list).
    _SITE_TABLE_REBUILD_DDL = {
        "accounts": (
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "kind TEXT NOT NULL, host TEXT, port INTEGER, username TEXT, "
            f"site_id TEXT NOT NULL DEFAULT '{DEFAULT_SITE_ID}')"
        ),
        "drafts": (
            "CREATE TABLE drafts (id INTEGER PRIMARY KEY, message_id INTEGER REFERENCES messages(id), "
            "thread_id TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT, body TEXT, "
            f"sender_addr TEXT, site_id TEXT NOT NULL DEFAULT '{DEFAULT_SITE_ID}', state TEXT NOT NULL, "
            "guardrail_flags TEXT, created_at TEXT, updated_at TEXT, approved_by TEXT, "
            "approved_at TEXT, sent_at TEXT)"
        ),
        "chunks": (
            "CREATE TABLE chunks (id INTEGER PRIMARY KEY, source_kind TEXT, source_id TEXT, "
            f"thread_id TEXT, text TEXT, token_count INTEGER, site_id TEXT NOT NULL DEFAULT '{DEFAULT_SITE_ID}', "
            "reference_document_id INTEGER REFERENCES reference_documents(id) ON DELETE CASCADE)"
        ),
        "reference_documents": (
            "CREATE TABLE reference_documents (id INTEGER PRIMARY KEY, site_id TEXT NOT NULL, "
            "filename TEXT NOT NULL, stored_path TEXT NOT NULL, mime_type TEXT, managed_key TEXT, "
            "content_sha256 TEXT, size_bytes INTEGER NOT NULL DEFAULT 0, "
            "status TEXT NOT NULL DEFAULT 'UPLOADED' "
            "CHECK (status IN ('UPLOADED','INDEXING','READY','ERROR')), "
            "error_text TEXT, chunk_count INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        ),
        "response_templates": (
            "CREATE TABLE response_templates (id INTEGER PRIMARY KEY, site_id TEXT NOT NULL, "
            "name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'General', "
            "subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, "
            "shortcut TEXT NOT NULL DEFAULT '', is_system INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(site_id, name))"
        ),
    }

    @staticmethod
    def _site_check_accepts(table_sql: str) -> bool:
        """True when a stored table DDL imposes no site CHECK, or its CHECK
        already lists every configured site."""
        match = _SITE_CHECK_RE.search(table_sql)
        if match is None:
            return True
        allowed = {
            item.strip().strip("'\"").lower()
            for item in match.group(1).split(",")
            if item.strip()
        }
        return VALID_SITES <= allowed

    def _ensure_site_capacity(self) -> None:
        """Rebuild legacy tables whose hard-coded site CHECK would reject a
        configured site. No-op (and zero risk) when no such CHECK exists or it
        already accepts every configured site."""
        needs_rebuild = [
            row["name"]
            for row in self.conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            )
            if row["name"] in self._SITE_TABLE_REBUILD_DDL
            and not self._site_check_accepts(row["sql"] or "")
        ]
        if not needs_rebuild:
            return
        log.warning(
            "Rebuilding %s to accept configured sites %s",
            needs_rebuild,
            sorted(VALID_SITES),
        )
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            # Canonical SQLite table-rebuild order (create tmp -> copy -> drop
            # old -> rename tmp) so foreign-key clauses in OTHER tables keep
            # pointing at the original table name.
            with self.conn:  # one transaction for the whole rebuild
                for table in needs_rebuild:
                    tmp = f"{table}__rebuild"
                    cols = [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]
                    col_list = ",".join(cols)
                    ddl = self._SITE_TABLE_REBUILD_DDL[table].replace(
                        f"CREATE TABLE {table} ", f"CREATE TABLE {tmp} ", 1
                    )
                    self.conn.execute(ddl)
                    self.conn.execute(
                        f"INSERT INTO {tmp}({col_list}) SELECT {col_list} FROM {table}"
                    )
                    self.conn.execute(f"DROP TABLE {table}")
                    self.conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
        # Recreate the indexes the renamed tables carried.
        self.conn.executescript(_schema_sql())
        self._migrate_schema()

    def _seed_response_templates(self) -> None:
        """Seed the packaged example templates — first run only.

        Existing databases are never touched: if the table already holds a row
        the seed is skipped entirely, so edited/deleted templates stay edited
        and deleted across upgrades. A fresh DB gets one copy of
        :data:`response_templates.EXAMPLE_TEMPLATES` per configured site.
        """
        from ..response_templates import default_templates_for

        existing = self.conn.execute(
            "SELECT 1 FROM response_templates LIMIT 1"
        ).fetchone()
        if existing is not None:
            return
        templates = [t for site in sorted(VALID_SITES) for t in default_templates_for(site)]
        now = _now()
        self.conn.executemany(
            "INSERT INTO response_templates("
            "site_id,name,category,subject,body,shortcut,is_system,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,1,?,?) "
            "ON CONFLICT(site_id,name) DO NOTHING",
            [
                (
                    template.site_id,
                    template.name,
                    template.category,
                    template.subject,
                    template.body,
                    template.shortcut,
                    now,
                    now,
                )
                for template in templates
            ],
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----- accounts -----
    def upsert_account(
        self,
        name: str,
        kind: str,
        host: str,
        port: int,
        username: str,
        site_id: str = DEFAULT_SITE_ID,
    ) -> int:
        site = validate_site_id(site_id or default_site_id())
        cur = self.conn.execute(
            "INSERT INTO accounts(name,kind,host,port,username,site_id) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, host=excluded.host, "
            "port=excluded.port, username=excluded.username, site_id=excluded.site_id",
            (name, kind, host, port, username, site),
        )
        self.conn.commit()
        # NEVER trust lastrowid after an upsert: when the ON CONFLICT branch
        # UPDATEs, SQLite leaves lastrowid at whatever the failed insert
        # attempted, which silently returns the id of a DIFFERENT account and
        # duplicates every message ingested under it.
        row = self.conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
        if row is not None:
            return int(row["id"])
        return int(cur.lastrowid or 0)

    def account_id(self, name: str) -> int | None:
        row = self.conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
        return row["id"] if row else None

    def list_accounts(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind is None:
            return self.conn.execute("SELECT * FROM accounts ORDER BY site_id,name").fetchall()
        return self.conn.execute(
            "SELECT * FROM accounts WHERE kind=? ORDER BY site_id,name", (kind,)
        ).fetchall()

    def get_account_for_message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT a.* FROM accounts a JOIN messages m ON m.account_id=a.id WHERE m.id=?",
            (int(message_id),),
        ).fetchone()

    # ----- messages -----
    def insert_message(self, **f: Any) -> int | None:
        """Insert a message; returns row id, or None if the (account,folder,uid)
        already exists (idempotent ingest)."""
        cols = (
            "account_id folder uid message_id thread_id from_addr from_name to_addrs "
            "cc_addrs subject received_at raw_html sanitized_text has_attachments link_count "
            "quarantined quarantine_reason screening_status screening_reason screening_source"
        ).split()
        f.setdefault("quarantined", 0)
        f.setdefault("screening_status", "UNSCREENED")
        f.setdefault("screening_source", "AUTO_POLICY")
        try:
            cur = self.conn.execute(
                f"INSERT INTO messages({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                tuple(f.get(c) for c in cols),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # CRITICAL: the failed INSERT already opened an implicit write
            # transaction and took SQLite's write lock. Without this rollback
            # the connection holds that lock forever, every other connection
            # gets "database is locked", and the WAL can never checkpoint.
            self.conn.rollback()
            return None  # duplicate UID — already ingested

    def get_message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    def get_message_for_site(self, message_id: int, site_id: str) -> sqlite3.Row | None:
        site = validate_site_id(site_id)
        return self.conn.execute(
            "SELECT m.*, a.name AS account_name, a.username AS account_username, "
            "a.site_id FROM messages m JOIN accounts a ON a.id=m.account_id "
            "WHERE m.id=? AND a.site_id=?",
            (int(message_id), site),
        ).fetchone()

    def message_uids(self, account_id: int, folder: str) -> set[int]:
        rows = self.conn.execute(
            "SELECT uid FROM messages WHERE account_id=? AND folder=?",
            (int(account_id), folder),
        ).fetchall()
        return {int(r["uid"]) for r in rows if r["uid"] is not None}

    def mailbox_cursor(self, account_id: int, folder: str) -> int | None:
        row = self.conn.execute(
            "SELECT last_uid FROM mailbox_cursors WHERE account_id=? AND folder=?",
            (int(account_id), folder),
        ).fetchone()
        return int(row["last_uid"]) if row is not None else None

    def set_mailbox_cursor(self, account_id: int, folder: str, last_uid: int) -> None:
        self.conn.execute(
            "INSERT INTO mailbox_cursors(account_id,folder,last_uid,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(account_id,folder) DO UPDATE SET "
            "last_uid=MAX(last_uid,excluded.last_uid),updated_at=excluded.updated_at",
            (int(account_id), folder, max(0, int(last_uid)), _now()),
        )
        self.conn.commit()

    def unprocessed_message_ids(self, limit: int = 200) -> list[int]:
        rows = self.conn.execute(
            "SELECT m.id FROM messages m "
            "LEFT JOIN classifications c ON c.message_id=m.id "
            "LEFT JOIN drafts d ON d.message_id=m.id "
            "WHERE c.message_id IS NULL AND d.id IS NULL "
            "AND m.archived=0 AND m.trashed=0 "
            "AND COALESCE(m.screening_status,'UNSCREENED') "
            "NOT IN ('POTENTIAL_SPAM','POTENTIAL_ISSUE','SPAM') "
            "ORDER BY datetime(m.received_at) DESC, m.id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def account_username(self, account_id: int | None) -> str | None:
        """Mailbox address for an ``accounts`` row (None if unknown)."""
        if account_id is None:
            return None
        row = self.conn.execute(
            "SELECT username FROM accounts WHERE id=?", (int(account_id),)
        ).fetchone()
        return str(row["username"]) if row and row["username"] else None

    def message_pk_for_uid(self, account_id: int, folder: str, uid: int) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM messages WHERE account_id=? AND folder=? AND uid=?",
            (int(account_id), folder, int(uid)),
        ).fetchone()
        return int(row["id"]) if row else None

    def message_needs_processing(self, message_pk: int) -> bool:
        """True when a stored message never reached triage (no classification,
        no draft) and is eligible for it — the per-row twin of
        :meth:`unprocessed_message_ids`."""
        row = self.conn.execute(
            "SELECT 1 FROM messages m "
            "LEFT JOIN classifications c ON c.message_id=m.id "
            "LEFT JOIN drafts d ON d.message_id=m.id "
            "WHERE m.id=? AND c.message_id IS NULL AND d.id IS NULL "
            "AND m.archived=0 AND m.trashed=0 "
            "AND COALESCE(m.screening_status,'UNSCREENED') "
            "NOT IN ('POTENTIAL_SPAM','POTENTIAL_ISSUE','SPAM') LIMIT 1",
            (int(message_pk),),
        ).fetchone()
        return row is not None

    def thread_messages(self, thread_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY received_at", (thread_id,)
        ).fetchall()

    def list_received(
        self,
        limit: int = 200,
        account_name: str | None = None,
        q: str | None = None,
        site_id: str | None = None,
        unseen_only: bool = False,
        today_only: bool = False,
        urgent_only: bool = False,
        needs_reply_only: bool = False,
        needs_action_only: bool = False,
        quarantined_only: bool = False,
        questionable_only: bool = False,
        spam_only: bool = False,
        include_spam: bool = False,
        message_tag: str | None = None,
        archived_only: bool = False,
        trashed_only: bool = False,
        drafted_only: bool = False,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Received messages for the Mail page, newest first, joined with the
        account label. Optional account filter + substring search over
        sender/subject/body. Read-only — the LLM never reaches this path.

        Triage filters:
          * ``today_only`` — received today, local time.
          * ``urgent_only`` — classified priority at or above ``URGENT_PRIORITY``.
          * ``needs_reply_only`` — classified RESPOND/MEETING and no SENT reply.
          * ``needs_action_only`` — needs reply, OR has a PENDING/BLOCKED/
            DEFERRED draft, OR is quarantined.
          * ``quarantined_only`` — quarantined messages awaiting human review.
          * ``questionable_only`` — potential spam/issues awaiting alignment.
          * ``spam_only`` — locally filtered spam (never deletes/moves server mail).
          * ``message_tag`` — exact live category/screening/quarantine tag.
          * ``archived_only`` — locally archived messages.
          * ``trashed_only`` — messages in reversible local Trash.
        """
        sql = (
            "SELECT m.id, m.from_addr, m.from_name, m.to_addrs, m.subject, "
            "m.received_at, m.sanitized_text, m.has_attachments, m.seen, "
            "m.quarantined, m.quarantine_reason, "
            "m.screening_status, m.screening_reason, m.screening_source, "
            "m.thread_id, a.name AS account_name, a.username AS account_username, "
            "a.site_id AS site_id, c.category, c.priority, c.rationale, "
            "c.injection_risk, d.id AS draft_id, d.state AS draft_state "
            "FROM messages m LEFT JOIN accounts a ON a.id = m.account_id "
            "LEFT JOIN classifications c ON c.message_id = m.id "
            "LEFT JOIN drafts d ON d.id = ("
            "SELECT latest.id FROM drafts latest WHERE latest.message_id=m.id "
            "ORDER BY latest.id DESC LIMIT 1"
            ") "
            "WHERE 1=1"
        )
        params: list[Any] = []
        # A purged message keeps a one-line tombstone (the spam filter learns from
        # it) but has no content left — it must never surface in a list again.
        sql += " AND m.purged_at IS NULL"
        if trashed_only:
            sql += " AND m.trashed=1"
        elif archived_only:
            sql += " AND m.archived=1 AND m.trashed=0"
        else:
            sql += " AND m.archived=0 AND m.trashed=0"
        if site_id is not None:
            sql += " AND a.site_id = ?"
            params.append(validate_site_id(site_id))
        if account_name:
            sql += " AND a.name = ?"
            params.append(account_name)
        if q:
            # Escape LIKE metacharacters so a literal '%' or '_' in the query
            # matches itself; bodies are searched too (people remember words
            # from the mail, not the sender's display name).
            needle = re.sub(r"([\\%_])", r"\\\1", q.strip())
            like = f"%{needle}%"
            sql += (
                " AND (m.from_addr LIKE ? ESCAPE '\\' OR m.from_name LIKE ? ESCAPE '\\'"
                " OR m.subject LIKE ? ESCAPE '\\' OR m.sanitized_text LIKE ? ESCAPE '\\')"
            )
            params += [like, like, like, like]
        if unseen_only:
            sql += " AND m.seen=0"
        if today_only:
            sql += " AND date(m.received_at,'localtime')=date('now','localtime')"
        if urgent_only:
            sql += " AND COALESCE(c.priority,0)>=?"
            params.append(int(self.URGENT_PRIORITY))
        needs_reply_sql = (
            "(c.category IN ('RESPOND','MEETING') "
            "AND COALESCE(d.state,'') NOT IN ('SENT','REJECTED'))"
        )
        needs_action_sql = (
            f"({needs_reply_sql} OR m.quarantined=1 "
            "OR m.screening_status IN ('POTENTIAL_SPAM','POTENTIAL_ISSUE') "
            "OR COALESCE(d.state,'') IN ('PENDING','BLOCKED','DEFERRED_NO_LLM'))"
        )
        if needs_reply_only:
            sql += f" AND {needs_reply_sql}"
        if needs_action_only:
            sql += f" AND {needs_action_sql}"
        if quarantined_only:
            sql += " AND m.quarantined=1"
        if questionable_only:
            sql += " AND m.screening_status IN ('POTENTIAL_SPAM','POTENTIAL_ISSUE')"
        if spam_only:
            sql += " AND m.screening_status='SPAM'"
        elif not include_spam:
            sql += " AND COALESCE(m.screening_status,'UNSCREENED')!='SPAM'"
        if message_tag:
            tag_kind, separator, tag_value = str(message_tag).partition(":")
            normalized = tag_value.strip().upper()
            if message_tag == "spam":
                sql += (
                    " AND (UPPER(TRIM(COALESCE(c.category,'')))='SPAM' "
                    "OR UPPER(TRIM(COALESCE(m.screening_status,'')))='SPAM')"
                )
            elif message_tag == "quarantined":
                sql += " AND m.quarantined=1"
            elif separator and tag_kind == "category" and normalized:
                sql += " AND UPPER(TRIM(COALESCE(c.category,'')))=?"
                params.append(normalized)
            elif separator and tag_kind == "screening" and normalized:
                sql += " AND UPPER(TRIM(COALESCE(m.screening_status,'')))=?"
                params.append(normalized)
            else:
                sql += " AND 0=1"
        if drafted_only:
            sql += " AND d.id IS NOT NULL"
        # received_at keeps each sender's own UTC offset, so a plain string
        # sort puts "09:00+09:00" after "01:00+00:00" (the same instant).
        # datetime() normalises to UTC before comparing.
        sql += " ORDER BY datetime(m.received_at) DESC, m.id DESC LIMIT ? OFFSET ?"
        params += [int(limit), max(0, int(offset))]
        return self.conn.execute(sql, tuple(params)).fetchall()

    def message_tag_counts(self, account_name: str | None = None) -> dict[str, int]:
        """Live tags on active messages, omitting tags with zero matches."""
        account_sql = " AND a.name=?" if account_name else ""
        params: tuple[Any, ...] = (account_name,) if account_name else ()
        base = (
            " FROM messages m JOIN accounts a ON a.id=m.account_id "
            "WHERE m.archived=0 AND m.trashed=0" + account_sql
        )
        counts: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT UPPER(TRIM(c.category)) AS tag,COUNT(*) AS n "
            "FROM messages m JOIN accounts a ON a.id=m.account_id "
            "JOIN classifications c ON c.message_id=m.id "
            "WHERE m.archived=0 AND m.trashed=0 "
            "AND TRIM(COALESCE(c.category,''))!=''"
            " AND UPPER(TRIM(c.category))!='SPAM'"
            + account_sql
            + " GROUP BY UPPER(TRIM(c.category))",
            params,
        ).fetchall():
            counts[f"category:{row['tag']}"] = int(row["n"] or 0)
        for row in self.conn.execute(
            "SELECT UPPER(TRIM(m.screening_status)) AS tag,COUNT(*) AS n"
            + base
            + " AND TRIM(COALESCE(m.screening_status,''))!='' "
            "AND UPPER(TRIM(m.screening_status))!='UNSCREENED' "
            "AND UPPER(TRIM(m.screening_status))!='SPAM' "
            "GROUP BY UPPER(TRIM(m.screening_status))",
            params,
        ).fetchall():
            counts[f"screening:{row['tag']}"] = int(row["n"] or 0)
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT m.id) AS n FROM messages m "
            "JOIN accounts a ON a.id=m.account_id "
            "LEFT JOIN classifications c ON c.message_id=m.id "
            "WHERE m.archived=0 AND m.trashed=0 "
            "AND (UPPER(TRIM(COALESCE(c.category,'')))='SPAM' "
            "OR UPPER(TRIM(COALESCE(m.screening_status,'')))='SPAM')" + account_sql,
            params,
        ).fetchone()
        spam = int(row["n"] or 0)
        if spam:
            counts["spam"] = spam
        row = self.conn.execute(
            "SELECT COUNT(*) AS n" + base + " AND m.quarantined=1",
            params,
        ).fetchone()
        quarantined = int(row["n"] or 0)
        if quarantined:
            counts["quarantined"] = quarantined
        return {tag: count for tag, count in counts.items() if count > 0}

    def triage_counts(self, site_id: str | None = None) -> dict[str, int]:
        """Dashboard triage numbers: received today / unread / needs reply /
        needs action / quarantined — one aggregate query."""
        sql = (
            "SELECT "
            # 'localtime' on both sides: received_at carries a UTC offset, so a
            # bare date() would compare its UTC date against the local date and
            # drop this morning's mail whenever the two differ.
            "COALESCE(SUM(CASE WHEN date(m.received_at,'localtime')=date('now','localtime') "
            "AND COALESCE(m.screening_status,'UNSCREENED')!='SPAM' "
            "THEN 1 ELSE 0 END),0) AS today, "
            "COALESCE(SUM(CASE WHEN m.seen=0 "
            "AND COALESCE(m.screening_status,'UNSCREENED')!='SPAM' "
            "THEN 1 ELSE 0 END),0) AS unread, "
            "COALESCE(SUM(CASE WHEN c.category IN ('RESPOND','MEETING') "
            "AND COALESCE(d.state,'') NOT IN ('SENT','REJECTED') THEN 1 ELSE 0 END),0) "
            "AS needs_reply, "
            "COALESCE(SUM(CASE WHEN (c.category IN ('RESPOND','MEETING') "
            "AND COALESCE(d.state,'') NOT IN ('SENT','REJECTED')) OR m.quarantined=1 "
            "OR m.screening_status IN ('POTENTIAL_SPAM','POTENTIAL_ISSUE') "
            "OR COALESCE(d.state,'') IN ('PENDING','BLOCKED','DEFERRED_NO_LLM') "
            "THEN 1 ELSE 0 END),0) AS needs_action, "
            "COALESCE(SUM(m.quarantined),0) AS quarantined, "
            "COALESCE(SUM(CASE WHEN m.screening_status "
            "IN ('POTENTIAL_SPAM','POTENTIAL_ISSUE') THEN 1 ELSE 0 END),0) "
            "AS questionable, "
            "COALESCE(SUM(CASE WHEN m.screening_status='SPAM' THEN 1 ELSE 0 END),0) "
            "AS spam "
            "FROM messages m LEFT JOIN accounts a ON a.id=m.account_id "
            "LEFT JOIN classifications c ON c.message_id=m.id "
            "LEFT JOIN drafts d ON d.id=(SELECT latest.id FROM drafts latest "
            "WHERE latest.message_id=m.id ORDER BY latest.id DESC LIMIT 1) "
            "WHERE m.archived=0 AND m.trashed=0"
        )
        params: tuple[Any, ...] = ()
        if site_id is not None:
            sql += " AND a.site_id=?"
            params = (validate_site_id(site_id),)
        row = self.conn.execute(sql, params).fetchone()
        return {k: int(row[k] or 0) for k in row.keys()}

    def mailbox_coverage(self, site_id: str | None = None) -> list[dict[str, Any]]:
        """Per-IMAP-mailbox poll liveness for the daily brief: when each
        account's cursor was last touched by its listener and the newest
        stored message. A cursor that stopped advancing means the listener
        is down — the brief must say so instead of rendering stale counts."""
        sql = (
            "SELECT a.name, a.username, "
            "MAX(c.updated_at) AS last_poll, "
            "(SELECT MAX(m.received_at) FROM messages m "
            "WHERE m.account_id=a.id) AS newest_message "
            "FROM accounts a LEFT JOIN mailbox_cursors c ON c.account_id=a.id "
            "WHERE a.kind='imap'"
        )
        params: tuple[Any, ...] = ()
        if site_id is not None:
            sql += " AND a.site_id=?"
            params = (validate_site_id(site_id),)
        sql += " GROUP BY a.id ORDER BY a.id"
        return [
            {
                "account": r["name"],
                "username": r["username"],
                "last_poll": r["last_poll"],
                "newest_message": r["newest_message"],
            }
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def received_counts(
        self, site_id: str | None = None, *, include_spam: bool = False
    ) -> dict[str, int]:
        """Total + unseen received-message counts (Mail page stat chips)."""
        sql = (
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN seen=0 THEN 1 ELSE 0 END), 0) AS unseen "
            "FROM messages m JOIN accounts a ON a.id=m.account_id "
            "WHERE m.archived=0 AND m.trashed=0"
        )
        params: tuple[Any, ...] = ()
        if site_id is not None:
            sql += " AND a.site_id=?"
            params = (validate_site_id(site_id),)
        if not include_spam:
            sql += " AND COALESCE(m.screening_status,'UNSCREENED')!='SPAM'"
        row = self.conn.execute(sql, params).fetchone()
        return {"total": row["total"] or 0, "unseen": row["unseen"] or 0}

    #: Inclusive floor on ``classifications.priority`` (0..5) that reads as urgent.
    URGENT_PRIORITY = 4

    def inbox_metrics(
        self,
        site_id: str | None = None,
        *,
        urgent_priority: int | None = None,
    ) -> list[dict[str, Any]]:
        """Per-inbox tile counts for the dashboard — one row per configured
        account, including accounts with no mail yet.

        Counts cover active mail only (not archived, not trashed) and mirror the
        definitions in :meth:`triage_counts`, so a tile and the stat chip above
        it can never disagree. Locally filtered spam is excluded from every
        count except ``spam`` itself. Read-only; no bodies or credentials.
        """
        floor = self.URGENT_PRIORITY if urgent_priority is None else int(urgent_priority)
        not_spam = "COALESCE(m.screening_status,'UNSCREENED')!='SPAM'"
        # A message counts once per metric; m.id IS NULL means the account has no
        # active mail, and every SUM below then collapses to 0 via COALESCE.
        needs_reply_sql = (
            "c.category IN ('RESPOND','MEETING') "
            "AND COALESCE(d.state,'') NOT IN ('SENT','REJECTED')"
        )
        sql = (
            "SELECT a.id AS account_id, a.name AS account_name, a.username, a.site_id, "
            f"COALESCE(SUM(CASE WHEN m.id IS NOT NULL AND {not_spam} "
            "THEN 1 ELSE 0 END),0) AS total, "
            "COALESCE(SUM(CASE WHEN date(m.received_at,'localtime')=date('now','localtime') "
            f"AND {not_spam} THEN 1 ELSE 0 END),0) AS today, "
            f"COALESCE(SUM(CASE WHEN m.seen=0 AND {not_spam} THEN 1 ELSE 0 END),0) AS unread, "
            f"COALESCE(SUM(CASE WHEN COALESCE(c.priority,0)>=? AND {not_spam} "
            "THEN 1 ELSE 0 END),0) AS urgent, "
            f"COALESCE(SUM(CASE WHEN {needs_reply_sql} THEN 1 ELSE 0 END),0) AS needs_reply, "
            f"COALESCE(SUM(CASE WHEN ({needs_reply_sql}) OR m.quarantined=1 "
            "OR m.screening_status IN ('POTENTIAL_SPAM','POTENTIAL_ISSUE') "
            "OR COALESCE(d.state,'') IN ('PENDING','BLOCKED','DEFERRED_NO_LLM') "
            "THEN 1 ELSE 0 END),0) AS needs_action, "
            "COALESCE(SUM(CASE WHEN m.quarantined=1 THEN 1 ELSE 0 END),0) AS quarantined, "
            "COALESCE(SUM(CASE WHEN m.screening_status "
            "IN ('POTENTIAL_SPAM','POTENTIAL_ISSUE') THEN 1 ELSE 0 END),0) AS questionable, "
            "COALESCE(SUM(CASE WHEN m.screening_status='SPAM' THEN 1 ELSE 0 END),0) AS spam "
            "FROM accounts a "
            "LEFT JOIN messages m ON m.account_id=a.id AND m.archived=0 AND m.trashed=0 "
            "LEFT JOIN classifications c ON c.message_id=m.id "
            "LEFT JOIN drafts d ON d.id=(SELECT latest.id FROM drafts latest "
            "WHERE latest.message_id=m.id ORDER BY latest.id DESC LIMIT 1) "
            "WHERE a.kind='imap'"
        )
        params: list[Any] = [floor]
        if site_id is not None:
            sql += " AND a.site_id=?"
            params.append(validate_site_id(site_id))
        sql += " GROUP BY a.id,a.name,a.username,a.site_id ORDER BY a.site_id,a.name"
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    # ----- quarantine (prompt-injection containment) -----
    def set_message_quarantined(
        self, message_id: int, quarantined: bool, reason: str | None = None
    ) -> bool:
        cur = self.conn.execute(
            "UPDATE messages SET quarantined=?, quarantine_reason=? WHERE id=?",
            (1 if quarantined else 0, reason, int(message_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def is_quarantined(self, message_id: int) -> bool:
        row = self.conn.execute(
            "SELECT quarantined FROM messages WHERE id=?", (int(message_id),)
        ).fetchone()
        return bool(row and row["quarantined"])

    def quarantined_count(self, site_id: str | None = None) -> int:
        sql = (
            "SELECT COUNT(*) AS n FROM messages m JOIN accounts a ON a.id=m.account_id "
            "WHERE m.quarantined=1 AND m.archived=0 AND m.trashed=0"
        )
        params: tuple[Any, ...] = ()
        if site_id is not None:
            sql += " AND a.site_id=?"
            params = (validate_site_id(site_id),)
        return int(self.conn.execute(sql, params).fetchone()["n"] or 0)

    # ----- inbound screening + human alignment -----
    def set_message_screening(
        self,
        message_id: int,
        status: str,
        reason: str,
        source: str = "USER",
    ) -> bool:
        from ..security.inbound_screening import SCREENING_STATUSES

        normalized = str(status or "").strip().upper()
        if normalized not in SCREENING_STATUSES:
            raise ValueError(f"invalid screening status: {normalized}")
        cur = self.conn.execute(
            "UPDATE messages SET screening_status=?,screening_reason=?,screening_source=? "
            "WHERE id=?",
            (normalized, str(reason or "")[:500], str(source or "USER")[:40], int(message_id)),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def screening_examples(self, site_id: str, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT label,sender_addr,sender_domain,subject_signature,created_at "
            "FROM message_screening_feedback WHERE site_id=? AND learn_similar=1 "
            "AND label IN ('CONTENT','SPAM') ORDER BY id DESC LIMIT ?",
            (validate_site_id(site_id), max(1, min(int(limit), 2000))),
        ).fetchall()

    def list_screening_feedback(
        self, site_id: str | None = None, limit: int = 100
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM message_screening_feedback"
        params: list[Any] = []
        if site_id is not None:
            sql += " WHERE site_id=?"
            params.append(validate_site_id(site_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return self.conn.execute(sql, tuple(params)).fetchall()

    def record_screening_feedback(
        self,
        message_id: int,
        label: str,
        *,
        actor: str = "user",
        note: str = "",
        learn_similar: bool = False,
    ) -> int:
        from ..security.inbound_screening import sender_domain, subject_signature

        normalized = str(label or "").strip().upper()
        if normalized not in {"CONTENT", "POTENTIAL_SPAM", "POTENTIAL_ISSUE", "SPAM"}:
            raise ValueError("invalid screening feedback label")
        row = self.conn.execute(
            "SELECT m.id,m.from_addr,m.subject,a.site_id FROM messages m "
            "JOIN accounts a ON a.id=m.account_id WHERE m.id=?",
            (int(message_id),),
        ).fetchone()
        if row is None:
            raise ValueError("message does not exist")
        reason = {
            "CONTENT": "confirmed content-related by a human",
            "SPAM": "confirmed spam by a human",
            "POTENTIAL_SPAM": "flagged for potential-spam review by a human",
            "POTENTIAL_ISSUE": "flagged for potential-issue review by a human",
        }[normalized]
        now = _now()
        try:
            cur = self.conn.execute(
                "INSERT INTO message_screening_feedback("
                "message_id,site_id,actor,label,learn_similar,sender_addr,sender_domain,"
                "subject_signature,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    int(message_id),
                    row["site_id"],
                    str(actor or "user")[:80],
                    normalized,
                    1 if learn_similar and normalized in {"CONTENT", "SPAM"} else 0,
                    str(row["from_addr"] or "").strip().lower(),
                    sender_domain(row["from_addr"]),
                    subject_signature(row["subject"]),
                    str(note or "").strip()[:2000],
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE messages SET screening_status=?,screening_reason=?,"
                "screening_source='USER' WHERE id=?",
                (normalized, reason, int(message_id)),
            )
            if normalized != "CONTENT":
                self.conn.execute(
                    "UPDATE drafts SET state='REJECTED',updated_at=?,"
                    "guardrail_flags=? "
                    "WHERE message_id=? AND state IN ('PENDING','BLOCKED','DEFERRED_NO_LLM')",
                    (
                        now,
                        json.dumps({"reason": "human_screening", "status": normalized}),
                        int(message_id),
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return int(cur.lastrowid or 0)

    # ----- attachments -----
    def add_attachment(
        self,
        message_id: int,
        filename: str,
        content_type: str | None,
        size_bytes: int,
        stored_path: str | None,
        sha256: str | None = None,
        skipped_reason: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO attachments(message_id,filename,content_type,size_bytes,"
            "stored_path,sha256,skipped_reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(message_id),
                filename,
                content_type,
                int(size_bytes),
                stored_path,
                sha256,
                skipped_reason,
                _now(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def list_attachments(self, message_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM attachments WHERE message_id=? ORDER BY id",
            (int(message_id),),
        ).fetchall()

    def get_attachment(self, attachment_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM attachments WHERE id=?", (int(attachment_id),)
        ).fetchone()

    # ----- agent-relayed send authorizations (bridge prepare-send/send) -----
    def create_send_authorization(
        self,
        draft_id: int,
        token_hash: str,
        body_sha256: str,
        recipient: str,
        from_addr: str,
        author: str,
        ttl_seconds: int = 900,
    ) -> int:
        now = datetime.now(timezone.utc)
        expires = now.timestamp() + max(60, int(ttl_seconds))
        expires_iso = datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO send_authorizations(draft_id,token_hash,body_sha256,"
            "recipient,from_addr,author,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(draft_id),
                token_hash,
                body_sha256,
                recipient,
                from_addr,
                str(author or "bridge")[:80],
                now.isoformat(),
                expires_iso,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def consume_send_authorization(
        self, draft_id: int, token_hash: str, body_sha256: str
    ) -> sqlite3.Row | None:
        """Atomically consume a matching, unexpired, unused authorization.

        Returns the row on success, None otherwise. Single-use: the UPDATE
        marks it used in the same statement that validates it."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "UPDATE send_authorizations SET used_at=? "
            "WHERE draft_id=? AND token_hash=? AND body_sha256=? "
            "AND used_at IS NULL AND expires_at > ?",
            (now, int(draft_id), token_hash, body_sha256, now),
        )
        self.conn.commit()
        if cur.rowcount != 1:
            return None
        return self.conn.execute(
            "SELECT * FROM send_authorizations WHERE draft_id=? AND token_hash=?",
            (int(draft_id), token_hash),
        ).fetchone()

    def agent_sends_last_hour(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM send_authorizations "
            "WHERE used_at IS NOT NULL AND datetime(used_at)>=datetime('now','-1 hour')"
        ).fetchone()
        return int(row["n"] or 0)

    # ----- outbox: staged authorizations + the durable sent log -------------
    def list_staged_sends(
        self, site_id: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[sqlite3.Row]:
        """Send authorizations that have NOT produced a transmission record.

        Newest first. Each row carries the draft it authorises plus a derived
        ``status``:

          * ``STAGED``   — token unused and unexpired: awaiting a human yes.
          * ``EXPIRED``  — token unused and past ``expires_at``: it can never send.
          * ``UNKNOWN``  — token CONSUMED but no ``sent_messages`` row exists.
            Never render this as sent; the transmission result is genuinely
            unknown (the process died between consuming and recording).
        """
        sql = (
            "SELECT a.*, d.subject AS subject, d.body AS body, d.site_id AS site_id, "
            "d.state AS draft_state, d.thread_id AS thread_id, d.message_id AS message_id, "
            "CASE WHEN a.used_at IS NOT NULL THEN 'UNKNOWN' "
            "     WHEN datetime(a.expires_at) <= datetime('now') THEN 'EXPIRED' "
            "     ELSE 'STAGED' END AS status "
            "FROM send_authorizations a "
            "JOIN drafts d ON d.id = a.draft_id "
            "WHERE NOT EXISTS (SELECT 1 FROM sent_messages s WHERE s.authorization_id = a.id)"
        )
        values: list[Any] = []
        if site_id is not None:
            sql += " AND d.site_id=?"
            values.append(validate_site_id(site_id))
        sql += " ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?"
        values.extend([max(1, int(limit)), max(0, int(offset))])
        return self.conn.execute(sql, values).fetchall()

    def get_staged_send(self, auth_id: int) -> sqlite3.Row | None:
        rows = [
            r for r in self.list_staged_sends(limit=10_000) if int(r["id"]) == int(auth_id)
        ]
        return rows[0] if rows else None

    def begin_send_record(
        self,
        from_addr: str,
        to_addrs: str,
        subject: str,
        body: str,
        *,
        origin: str = "ui_compose",
        site_id: str | None = None,
        draft_id: int | None = None,
        authorization_id: int | None = None,
    ) -> int:
        """Open a send record BEFORE transmitting. Outcome starts 'UNKNOWN'.

        Writing the row first is what makes a crash mid-send visible: an
        orphaned 'UNKNOWN' row is an honest "we do not know", where no row at
        all would look like nothing ever happened.
        """
        if str(origin) not in {"ui_draft", "ui_compose", "agent_bridge"}:
            raise ValueError("origin must be ui_draft|ui_compose|agent_bridge")
        site = validate_site_id(site_id or default_site_id())
        cur = self.conn.execute(
            "INSERT INTO sent_messages(draft_id,authorization_id,site_id,origin,from_addr,"
            "to_addrs,subject,body,outcome,imap_append,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'UNKNOWN','PENDING',?)",
            (
                int(draft_id) if draft_id is not None else None,
                int(authorization_id) if authorization_id is not None else None,
                site,
                str(origin),
                str(from_addr or ""),
                str(to_addrs or ""),
                str(subject or ""),
                str(body or ""),
                _now(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def finish_send_record(
        self,
        record_id: int,
        outcome: str,
        *,
        smtp_message_id: str | None = None,
        error_text: str | None = None,
    ) -> None:
        """Stamp the transmission result on an open send record."""
        value = str(outcome or "").upper()
        if value not in {"SENT", "FAILED", "UNKNOWN"}:
            raise ValueError("outcome must be SENT|FAILED|UNKNOWN")
        self.conn.execute(
            "UPDATE sent_messages SET outcome=?,smtp_message_id=?,error_text=?,"
            "completed_at=? WHERE id=?",
            (
                value,
                smtp_message_id,
                (str(error_text)[:500] if error_text else None),
                _now(),
                int(record_id),
            ),
        )
        self.conn.commit()

    def record_send_append(
        self,
        record_id: int,
        status: str,
        *,
        folder: str | None = None,
        note: str | None = None,
    ) -> None:
        """Record what happened to the IMAP Sent-folder copy of a sent message.

        Deliberately separate from ``outcome``: a mail that left the SMTP relay
        is SENT even if the provider copy could not be appended.
        """
        value = str(status or "").upper()
        if value not in {"PENDING", "OK", "SKIPPED", "FAILED"}:
            raise ValueError("imap append status must be PENDING|OK|SKIPPED|FAILED")
        self.conn.execute(
            "UPDATE sent_messages SET imap_append=?,imap_folder=?,imap_note=? WHERE id=?",
            (value, folder, (str(note)[:300] if note else None), int(record_id)),
        )
        self.conn.commit()

    def list_sent_messages(
        self, site_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sent_messages"
        values: list[Any] = []
        if site_id is not None:
            sql += " WHERE site_id=?"
            values.append(validate_site_id(site_id))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        values.extend([max(1, int(limit)), max(0, int(offset))])
        return self.conn.execute(sql, values).fetchall()

    def get_sent_message(self, record_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sent_messages WHERE id=?", (int(record_id),)
        ).fetchone()

    def outbox_counts(self, site_id: str | None = None) -> dict[str, int]:
        """Counts the header chip and page tabs read.

        ``pending`` is what must never sit unnoticed: staged-and-live
        authorizations plus any transmission whose result was never recorded.
        """
        staged = self.list_staged_sends(site_id=site_id, limit=10_000)
        counts = {
            "staged": sum(1 for r in staged if r["status"] == "STAGED"),
            "expired": sum(1 for r in staged if r["status"] == "EXPIRED"),
            "unknown": sum(1 for r in staged if r["status"] == "UNKNOWN"),
            "sent": 0,
            "failed": 0,
            "in_flight": 0,
        }
        sql = "SELECT outcome, COUNT(*) AS n FROM sent_messages"
        values: list[Any] = []
        if site_id is not None:
            sql += " WHERE site_id=?"
            values.append(validate_site_id(site_id))
        sql += " GROUP BY outcome"
        for row in self.conn.execute(sql, values):
            key = {"SENT": "sent", "FAILED": "failed", "UNKNOWN": "in_flight"}[row["outcome"]]
            counts[key] = int(row["n"] or 0)
        counts["pending"] = counts["staged"] + counts["unknown"] + counts["in_flight"]
        return counts

    # ----- agent notes (durable cross-session memory for bridge agents) -----
    def add_agent_note(
        self,
        site_id: str,
        author: str,
        title: str,
        body: str,
        kind: str = "observation",
        related_message_ids: list[int] | None = None,
    ) -> int:
        site = validate_site_id(site_id or default_site_id())
        kind = str(kind or "observation").strip().lower()
        if kind not in {"observation", "action", "incident", "context"}:
            raise ValueError("kind must be observation|action|incident|context")
        title = str(title or "").strip()[:200]
        body = str(body or "").strip()[:8000]
        if not title or not body:
            raise ValueError("title and body are required")
        related = json.dumps([int(i) for i in (related_message_ids or [])][:100])
        cur = self.conn.execute(
            "INSERT INTO agent_notes(site_id,author,kind,title,body,"
            "related_message_ids,created_at) VALUES(?,?,?,?,?,?,?)",
            (site, str(author or "unknown")[:80], kind, title, body, related, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def list_agent_notes(
        self,
        site_id: str | None = None,
        limit: int = 50,
        since_days: int | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM agent_notes WHERE 1=1"
        params: list[Any] = []
        if site_id is not None:
            sql += " AND site_id=?"
            params.append(validate_site_id(site_id))
        if since_days is not None:
            sql += " AND datetime(created_at)>=datetime('now',?)"
            params.append(f"-{max(1, int(since_days))} days")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        return self.conn.execute(sql, tuple(params)).fetchall()

    @staticmethod
    def _message_ids(message_ids: Iterable[int]) -> list[int]:
        ids = list(dict.fromkeys(int(message_id) for message_id in message_ids))
        if len(ids) > 1000:
            raise ValueError("at most 1000 messages can be changed at once")
        return ids

    def mark_messages_seen(self, message_ids: Iterable[int], seen: bool = True) -> int:
        ids = self._message_ids(message_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cur = self.conn.execute(
            f"UPDATE messages SET seen=? WHERE id IN ({placeholders})",
            (1 if seen else 0, *ids),
        )
        self.conn.commit()
        return int(cur.rowcount)

    def mark_message_seen(self, message_id: int) -> None:
        self.mark_messages_seen([message_id])

    def mark_message_unseen(self, message_id: int) -> None:
        self.mark_messages_seen([message_id], False)

    def set_messages_archived(self, message_ids: Iterable[int], archived: bool = True) -> int:
        ids = self._message_ids(message_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        if archived:
            sql = f"UPDATE messages SET archived=1,trashed=0 WHERE id IN ({placeholders})"
            params: tuple[Any, ...] = tuple(ids)
        else:
            sql = f"UPDATE messages SET archived=0 WHERE id IN ({placeholders})"
            params = tuple(ids)
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.rowcount)

    def set_message_archived(self, message_id: int, archived: bool = True) -> bool:
        return self.set_messages_archived([message_id], archived) == 1

    def set_messages_trashed(self, message_ids: Iterable[int], trashed: bool = True) -> int:
        ids = self._message_ids(message_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        if trashed:
            # trashed_at starts the retention clock (security.trash_retention_days);
            # COALESCE so re-deleting an already-deleted message does not restart it.
            sql = (
                "UPDATE messages SET trashed=1,archived=0,trashed_at=COALESCE(trashed_at,?) "
                f"WHERE id IN ({placeholders})"
            )
            params: tuple[Any, ...] = (_now(), *ids)
        else:
            sql = (
                "UPDATE messages SET trashed=0,trashed_at=NULL "
                f"WHERE id IN ({placeholders})"
            )
            params = tuple(ids)
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.rowcount)

    def set_message_trashed(self, message_id: int, trashed: bool = True) -> bool:
        return self.set_messages_trashed([message_id], trashed) == 1

    # ----- retention queue (local Trash -> permanent deletion) -----
    #: Screening statuses that skip the holding period entirely — spam and scam
    #: mail is deleted the moment the user says so, here and at the provider.
    PURGE_IMMEDIATELY = ("SPAM", "POTENTIAL_SPAM", "POTENTIAL_ISSUE")

    def trash_queue(self, *, retention_days: int = 60) -> list[sqlite3.Row]:
        """Everything waiting in local Trash, with its scheduled deletion date.

        ``due_at`` is NULL for spam/scam mail (deleted on the next sweep, no
        holding period) and ``trashed_at + retention_days`` for everything else.
        Rows already purged are excluded — there is nothing left to delete.
        """
        days = max(0, int(retention_days))
        placeholders = ",".join("?" for _ in self.PURGE_IMMEDIATELY)
        return self.conn.execute(
            "SELECT m.id, m.from_addr, m.subject, m.received_at, m.trashed_at, "
            "m.screening_status, m.server_deleted_at, m.server_delete_error, "
            "a.name AS account_name, "
            f"CASE WHEN m.screening_status IN ({placeholders}) THEN NULL "
            "ELSE datetime(COALESCE(m.trashed_at, m.received_at), ?) END AS due_at "
            "FROM messages m LEFT JOIN accounts a ON a.id=m.account_id "
            "WHERE m.trashed=1 AND m.purged_at IS NULL "
            "ORDER BY datetime(COALESCE(m.trashed_at, m.received_at)) ASC",
            (*self.PURGE_IMMEDIATELY, f"+{days} days"),
        ).fetchall()

    def trash_due_ids(self, *, retention_days: int = 60) -> list[int]:
        """Ids the retention sweep should permanently delete right now."""
        days = max(0, int(retention_days))
        placeholders = ",".join("?" for _ in self.PURGE_IMMEDIATELY)
        rows = self.conn.execute(
            "SELECT id FROM messages WHERE trashed=1 AND purged_at IS NULL AND ("
            f"screening_status IN ({placeholders}) "
            "OR datetime(COALESCE(trashed_at, received_at), ?) <= datetime('now')"
            ") ORDER BY id",
            (*self.PURGE_IMMEDIATELY, f"+{days} days"),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def attachment_paths(self, message_ids: Iterable[int]) -> list[str]:
        ids = self._message_ids(message_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT stored_path FROM attachments WHERE message_id IN ({placeholders}) "
            "AND stored_path IS NOT NULL",
            tuple(ids),
        ).fetchall()
        return [str(r["stored_path"]) for r in rows if r["stored_path"]]

    def purge_messages(self, message_ids: Iterable[int]) -> int:
        """Shred message content locally, keeping a one-line tombstone.

        The row itself survives on purpose. ``message_screening_feedback``
        cascades on delete, so dropping the row would erase the very rule the
        user taught by marking the message as spam — and ``drafts`` /
        ``classifications`` reference it without a cascade, so a hard DELETE
        would fail outright once a draft existed. What is destroyed is
        everything that carries content: body, HTML, recipients, links, and the
        attachment rows (their files are removed by the caller, which knows the
        paths from :meth:`attachment_paths`).
        """
        ids = self._message_ids(message_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        try:
            self.conn.execute(
                f"DELETE FROM message_links WHERE message_id IN ({placeholders})", tuple(ids)
            )
            self.conn.execute(
                f"DELETE FROM attachments WHERE message_id IN ({placeholders})", tuple(ids)
            )
            cur = self.conn.execute(
                "UPDATE messages SET purged_at=?,trashed=1,raw_html=NULL,sanitized_text=NULL,"
                "to_addrs=NULL,cc_addrs=NULL,has_attachments=0,link_count=0 "
                f"WHERE id IN ({placeholders}) AND purged_at IS NULL",
                (_now(), *ids),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return int(cur.rowcount)

    # ----- provider-side delete bookkeeping -----
    def server_locators(self, message_ids: Iterable[int]) -> list[sqlite3.Row]:
        """(message id, account name, folder, uid) for messages still on a server.

        Rows already expunged (``server_deleted_at`` set) are omitted so a
        repeated delete is a no-op rather than a second round-trip, and rows
        with no UID (hand-inserted/demo mail) can never be targeted.
        """
        ids = self._message_ids(message_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return self.conn.execute(
            "SELECT m.id AS message_id, m.folder, m.uid, a.name AS account_name "
            f"FROM messages m JOIN accounts a ON a.id=m.account_id WHERE m.id IN ({placeholders}) "
            "AND m.uid IS NOT NULL AND m.server_deleted_at IS NULL",
            tuple(ids),
        ).fetchall()

    def record_server_delete(
        self,
        message_ids: Iterable[int],
        *,
        error: str | None = None,
    ) -> int:
        """Record the outcome of a provider-side delete.

        Success stamps ``server_deleted_at`` and clears any earlier error;
        failure records the error and leaves ``server_deleted_at`` NULL, so the
        message is still listed as present at the provider and a later retry
        picks it up again.
        """
        ids = self._message_ids(message_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        if error is None:
            sql = (
                "UPDATE messages SET server_deleted_at=?,server_delete_error=NULL "
                f"WHERE id IN ({placeholders})"
            )
            params: tuple[Any, ...] = (_now(), *ids)
        else:
            sql = (
                "UPDATE messages SET server_delete_error=? "
                f"WHERE id IN ({placeholders})"
            )
            params = (str(error)[:500], *ids)
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.rowcount)

    # ----- message links (controller-side, LLM never sees) -----
    def add_links(self, message_id: int, links: Iterable[tuple[str, str, str]]) -> None:
        try:
            self.conn.executemany(
                "INSERT INTO message_links(message_id,symbol,target,display_text) VALUES(?,?,?,?)",
                [(message_id, sym, tgt, disp) for sym, tgt, disp in links],
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()  # never leave a write transaction open
            raise

    def resolve_links(self, message_id: int) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT symbol,target FROM message_links WHERE message_id=?", (message_id,)
        ).fetchall()
        return {r["symbol"]: r["target"] for r in rows}

    # ----- classifications -----
    def upsert_classification(self, message_id: int, **f: Any) -> None:
        self.conn.execute(
            "INSERT INTO classifications(message_id,category,priority,rationale,injection_risk,"
            "classified_at,model_used) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET category=excluded.category,"
            "priority=excluded.priority,rationale=excluded.rationale,"
            "injection_risk=excluded.injection_risk,classified_at=excluded.classified_at,"
            "model_used=excluded.model_used",
            (
                message_id,
                f.get("category"),
                f.get("priority"),
                f.get("rationale"),
                f.get("injection_risk"),
                _now(),
                f.get("model_used"),
            ),
        )
        self.conn.commit()

    # ----- drafts -----
    def create_draft(
        self,
        message_id: int | None,
        thread_id: str,
        recipient: str,
        subject: str,
        body: str,
        state: DraftState,
        guardrail_flags: dict | None = None,
        sender_addr: str | None = None,
        site_id: str | None = None,
    ) -> int:
        if site_id is None and message_id is not None:
            account = self.get_account_for_message(message_id)
            site_id = account["site_id"] if account is not None else default_site_id()
            sender_addr = sender_addr or (account["username"] if account is not None else None)
        site = validate_site_id(site_id or default_site_id())
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO drafts(message_id,thread_id,recipient,subject,body,sender_addr,"
            "site_id,state,guardrail_flags,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                thread_id,
                recipient,
                subject,
                body,
                sender_addr,
                site,
                state,
                json.dumps(guardrail_flags or {}),
                now,
                now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def upsert_message_draft(
        self,
        message_id: int,
        thread_id: str,
        recipient: str,
        subject: str,
        body: str,
        state: DraftState,
        guardrail_flags: dict | None = None,
    ) -> int:
        """Create once per inbound message; only deferred rows may be regenerated."""
        existing = self.conn.execute(
            "SELECT * FROM drafts WHERE message_id=? ORDER BY id DESC LIMIT 1",
            (int(message_id),),
        ).fetchone()
        if existing is None:
            return self.create_draft(
                message_id,
                thread_id,
                recipient,
                subject,
                body,
                state,
                guardrail_flags,
            )
        if existing["state"] == "DEFERRED_NO_LLM":
            self.update_draft_state(
                existing["id"],
                state,
                recipient=recipient,
                subject=subject,
                body=body,
                guardrail_flags=guardrail_flags or {},
            )
        return int(existing["id"])

    def update_draft_state(self, draft_id: int, state: DraftState, **f: Any) -> None:
        sets = ["state=?", "updated_at=?"]
        vals: list[Any] = [state, _now()]
        for k in (
            "approved_by",
            "approved_at",
            "sent_at",
            "body",
            "recipient",
            "subject",
            "sender_addr",
        ):
            if k in f:
                sets.append(f"{k}=?")
                vals.append(f[k])
        if "guardrail_flags" in f:
            sets.append("guardrail_flags=?")
            vals.append(json.dumps(f["guardrail_flags"]))
        vals.append(draft_id)
        self.conn.execute(f"UPDATE drafts SET {','.join(sets)} WHERE id=?", vals)
        self.conn.commit()

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()

    def get_draft_for_site(self, draft_id: int, site_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM drafts WHERE id=? AND site_id=?",
            (int(draft_id), validate_site_id(site_id)),
        ).fetchone()

    def latest_draft_for_message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM drafts WHERE message_id=? ORDER BY id DESC LIMIT 1",
            (int(message_id),),
        ).fetchone()

    def drafts_by_state(self, state: DraftState) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM drafts WHERE state=? ORDER BY created_at DESC", (state,)
        ).fetchall()

    def deferred_drafts(self) -> list[sqlite3.Row]:
        return self.drafts_by_state("DEFERRED_NO_LLM")

    def draft_counts(self, site_id: str | None = None) -> dict[str, int]:
        """Counts per draft state (for the inbox stat chips / tab badges)."""
        sql = "SELECT state, COUNT(*) AS n FROM drafts"
        params: tuple[Any, ...] = ()
        if site_id is not None:
            sql += " WHERE site_id=?"
            params = (validate_site_id(site_id),)
        sql += " GROUP BY state"
        rows = self.conn.execute(sql, params).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def drafts_overview(self, state: DraftState | None = None) -> list[sqlite3.Row]:
        """Drafts joined with their source message + classification in ONE query
        (the inbox previously issued 2 extra queries per row). ``state=None``
        returns all states, newest first."""
        q = (
            "SELECT d.*, COALESCE(m.from_addr,d.sender_addr) AS from_addr, "
            "m.from_name, m.received_at, "
            "c.category, c.priority, c.injection_risk "
            "FROM drafts d "
            "LEFT JOIN messages m ON m.id = d.message_id "
            "LEFT JOIN classifications c ON c.message_id = d.message_id "
        )
        params: tuple[Any, ...] = ()
        if state:
            q += "WHERE d.state=? "
            params = (state,)
        q += "ORDER BY d.created_at DESC"
        return self.conn.execute(q, params).fetchall()

    # ----- human-authored local drafts -----
    def create_manual_draft(
        self,
        from_addr: str,
        to_addr: str,
        subject: str,
        body: str,
        site_id: str = DEFAULT_SITE_ID,
    ) -> int:
        import uuid

        return self.create_draft(
            message_id=None,
            thread_id=f"manual:{uuid.uuid4().hex}",
            recipient=to_addr.strip(),
            subject=subject,
            body=body,
            state="DRAFT",
            sender_addr=from_addr.strip(),
            site_id=validate_site_id(site_id),
        )

    def get_manual_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT d.*,d.sender_addr AS from_addr FROM drafts d "
            "WHERE id=? AND message_id IS NULL AND state IN ('DRAFT','SENT')",
            (int(draft_id),),
        ).fetchone()

    def list_manual_drafts(
        self, site_id: str | None = None, state: str = "DRAFT"
    ) -> list[sqlite3.Row]:
        state = state.upper()
        if state not in {"DRAFT", "SENT"}:
            raise ValueError("manual draft state must be DRAFT or SENT")
        sql = (
            "SELECT d.*,d.sender_addr AS from_addr FROM drafts d "
            "WHERE message_id IS NULL AND state=?"
        )
        values: list[Any] = [state]
        if site_id is not None:
            sql += " AND site_id=?"
            values.append(validate_site_id(site_id))
        sql += " ORDER BY updated_at DESC, id DESC"
        return self.conn.execute(sql, values).fetchall()

    def update_manual_draft(
        self,
        draft_id: int,
        from_addr: str,
        to_addr: str,
        subject: str,
        body: str,
    ) -> bool:
        cur = self.conn.execute(
            "UPDATE drafts SET sender_addr=?,recipient=?,subject=?,body=?,updated_at=? "
            "WHERE id=? AND message_id IS NULL AND state='DRAFT'",
            (from_addr.strip(), to_addr.strip(), subject, body, _now(), int(draft_id)),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def delete_manual_draft(self, draft_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM drafts WHERE id=? AND message_id IS NULL AND state='DRAFT'",
            (int(draft_id),),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def mark_manual_draft_sent(self, draft_id: int, sent_at: str | None = None) -> bool:
        cur = self.conn.execute(
            "UPDATE drafts SET state='SENT',sent_at=?,updated_at=? "
            "WHERE id=? AND message_id IS NULL AND state='DRAFT'",
            (sent_at or _now(), _now(), int(draft_id)),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def record_manual_sent(
        self,
        from_addr: str,
        to_addr: str,
        subject: str,
        body: str,
        site_id: str = DEFAULT_SITE_ID,
    ) -> int:
        draft_id = self.create_manual_draft(from_addr, to_addr, subject, body, site_id=site_id)
        self.mark_manual_draft_sent(draft_id)
        return draft_id

    # ----- conversational AI revision transcript -----
    def create_ai_revision(
        self,
        draft_id: int,
        feedback: str,
        body: str,
        model_used: str,
        role: str = "assistant",
    ) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError("role must be 'user' or 'assistant'")
        if self.get_draft(int(draft_id)) is None:
            raise ValueError("draft does not exist")
        cur = self.conn.execute(
            "INSERT INTO ai_revisions(draft_id,role,feedback,body,model_used,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (int(draft_id), role, feedback, body, model_used, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_ai_revisions(self, draft_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM ai_revisions WHERE draft_id=? ORDER BY id",
            (int(draft_id),),
        ).fetchall()

    # ----- site-scoped response templates -----
    @staticmethod
    def _validate_response_template(
        name: str,
        category: str,
        subject: str,
        body: str,
        shortcut: str,
    ) -> tuple[str, str, str, str, str]:
        from ..response_templates import validate_template_text

        clean_name = str(name or "").strip()
        clean_category = str(category or "").strip() or "General"
        clean_subject = str(subject or "").strip()
        clean_body = str(body or "").strip()
        clean_shortcut = str(shortcut or "").strip()
        if not clean_name:
            raise ValueError("template name is required")
        if not clean_body:
            raise ValueError("template body is required")
        validate_template_text(clean_subject)
        validate_template_text(clean_body)
        return (
            clean_name,
            clean_category,
            clean_subject,
            clean_body,
            clean_shortcut,
        )

    def list_response_templates(self, site_id: str) -> list[sqlite3.Row]:
        site = validate_site_id(site_id)
        return self.conn.execute(
            "SELECT * FROM response_templates WHERE site_id=? "
            "ORDER BY category COLLATE NOCASE,name COLLATE NOCASE",
            (site,),
        ).fetchall()

    def get_response_template(
        self, template_id: int, site_id: str | None = None
    ) -> sqlite3.Row | None:
        if site_id is None:
            return self.conn.execute(
                "SELECT * FROM response_templates WHERE id=?", (int(template_id),)
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM response_templates WHERE id=? AND site_id=?",
            (int(template_id), validate_site_id(site_id)),
        ).fetchone()

    def create_response_template(
        self,
        site_id: str,
        name: str,
        category: str,
        subject: str,
        body: str,
        shortcut: str = "",
    ) -> int:
        site = validate_site_id(site_id or default_site_id())
        clean = self._validate_response_template(name, category, subject, body, shortcut)
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO response_templates("
            "site_id,name,category,subject,body,shortcut,is_system,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,0,?,?)",
            (site, *clean, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_response_template(
        self,
        template_id: int,
        site_id: str,
        name: str,
        category: str,
        subject: str,
        body: str,
        shortcut: str = "",
    ) -> bool:
        site = validate_site_id(site_id)
        clean = self._validate_response_template(name, category, subject, body, shortcut)
        cur = self.conn.execute(
            "UPDATE response_templates SET name=?,category=?,subject=?,body=?,"
            "shortcut=?,updated_at=? WHERE id=? AND site_id=?",
            (*clean, _now(), int(template_id), site),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def save_response_template(
        self,
        site_id: str,
        name: str,
        category: str,
        subject: str,
        body: str,
        shortcut: str = "",
        template_id: int | None = None,
    ) -> int:
        """Create a template, or update an existing template in the same site."""
        if template_id is None:
            return self.create_response_template(site_id, name, category, subject, body, shortcut)
        if not self.update_response_template(
            template_id, site_id, name, category, subject, body, shortcut
        ):
            raise ValueError("response template does not belong to site")
        return int(template_id)

    def delete_response_template(self, template_id: int, site_id: str | None = None) -> bool:
        template = self.get_response_template(int(template_id), site_id)
        if template is None or bool(template["is_system"]):
            return False
        cur = self.conn.execute("DELETE FROM response_templates WHERE id=?", (int(template_id),))
        self.conn.commit()
        return cur.rowcount == 1

    # ----- recipient allowlist -----
    def record_allowlist(self, addr: str, source: str) -> None:
        domain = addr.split("@")[-1].lower() if "@" in addr else ""
        now = _now()
        self.conn.execute(
            "INSERT INTO recipient_allowlist(addr,domain,first_seen,last_seen,msg_count,source) "
            "VALUES(?,?,?,?,1,?) ON CONFLICT(addr) DO UPDATE SET last_seen=excluded.last_seen,"
            "msg_count=msg_count+1",
            (addr.lower(), domain, now, now, source),
        )
        self.conn.commit()

    def is_allowlisted(self, addr: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM recipient_allowlist WHERE addr=?", (addr.lower(),)
        ).fetchone()
        return row is not None

    def allowlisted_domains(self) -> set[str]:
        rows = self.conn.execute("SELECT DISTINCT domain FROM recipient_allowlist").fetchall()
        return {r["domain"] for r in rows if r["domain"]}

    # ----- site-scoped reference documents -----
    def create_reference_document(
        self,
        site_id: str,
        filename: str,
        stored_path: str,
        mime_type: str,
        size_bytes: int,
    ) -> int:
        site = validate_site_id(site_id or default_site_id())
        name = Path(filename).name.strip()
        if not name:
            raise ValueError("filename is required")
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO reference_documents(site_id,filename,stored_path,mime_type,"
            "size_bytes,status,error_text,chunk_count,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'UPLOADED',NULL,0,?,?)",
            (
                site,
                name,
                str(stored_path),
                mime_type or "application/octet-stream",
                max(0, int(size_bytes)),
                now,
                now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def upsert_managed_reference_document(
        self,
        site_id: str,
        managed_key: str,
        filename: str,
        stored_path: str,
        mime_type: str,
        size_bytes: int,
        content_sha256: str,
    ) -> tuple[int, bool]:
        """Create or refresh an app-managed reference.

        The boolean reports whether the document needs indexing: new content,
        a changed digest, or an existing row which is not currently READY.
        """
        site = validate_site_id(site_id)
        key = str(managed_key or "").strip()
        digest = str(content_sha256 or "").strip().lower()
        name = Path(filename).name.strip()
        if not key:
            raise ValueError("managed_key is required")
        if not name:
            raise ValueError("filename is required")
        if not digest:
            raise ValueError("content_sha256 is required")
        existing = self.conn.execute(
            "SELECT * FROM reference_documents WHERE managed_key=?", (key,)
        ).fetchone()
        now = _now()
        if existing is None:
            cur = self.conn.execute(
                "INSERT INTO reference_documents("
                "site_id,filename,stored_path,mime_type,managed_key,content_sha256,"
                "size_bytes,status,error_text,chunk_count,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,'UPLOADED',NULL,0,?,?)",
                (
                    site,
                    name,
                    str(stored_path),
                    mime_type or "application/octet-stream",
                    key,
                    digest,
                    max(0, int(size_bytes)),
                    now,
                    now,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid), True
        if existing["site_id"] != site:
            raise ValueError("managed reference key belongs to another site")

        changed = str(existing["content_sha256"] or "").lower() != digest
        needs_index = changed or existing["status"] != "READY"
        status = "UPLOADED" if changed else existing["status"]
        error_text = None if changed else existing["error_text"]
        chunk_count = 0 if changed else int(existing["chunk_count"] or 0)
        self.conn.execute(
            "UPDATE reference_documents SET filename=?,stored_path=?,mime_type=?,"
            "content_sha256=?,size_bytes=?,status=?,error_text=?,chunk_count=?,"
            "updated_at=? WHERE id=?",
            (
                name,
                str(stored_path),
                mime_type or "application/octet-stream",
                digest,
                max(0, int(size_bytes)),
                status,
                error_text,
                chunk_count,
                now,
                int(existing["id"]),
            ),
        )
        self.conn.commit()
        return int(existing["id"]), needs_index

    def list_reference_documents(self, site_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM reference_documents"
        params: tuple[Any, ...] = ()
        if site_id is not None:
            sql += " WHERE site_id=?"
            params = (validate_site_id(site_id),)
        sql += " ORDER BY created_at DESC, id DESC"
        return self.conn.execute(sql, params).fetchall()

    def get_reference_document(self, document_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM reference_documents WHERE id=?", (int(document_id),)
        ).fetchone()

    def update_reference_document_status(
        self,
        document_id: int,
        status: str,
        error_text: str | None = None,
        chunk_count: int | None = None,
    ) -> bool:
        status = status.upper()
        if status not in {"UPLOADED", "INDEXING", "READY", "ERROR"}:
            raise ValueError("invalid reference document status")
        sets = ["status=?", "error_text=?", "updated_at=?"]
        vals: list[Any] = [status, error_text, _now()]
        if chunk_count is not None:
            sets.append("chunk_count=?")
            vals.append(max(0, int(chunk_count)))
        vals.append(int(document_id))
        cur = self.conn.execute(f"UPDATE reference_documents SET {','.join(sets)} WHERE id=?", vals)
        self.conn.commit()
        return cur.rowcount == 1

    def replace_document_chunks(
        self,
        document_id: int,
        site_id: str,
        chunks: list[dict[str, Any]],
    ) -> int:
        site = validate_site_id(site_id or default_site_id())
        doc = self.get_reference_document(int(document_id))
        if doc is None or doc["site_id"] != site:
            raise ValueError("reference document does not belong to site")
        old_ids = [
            int(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM chunks WHERE reference_document_id=?", (int(document_id),)
            ).fetchall()
        ]
        try:
            if self.vec_enabled and old_ids:
                placeholders = ",".join("?" * len(old_ids))
                self.conn.execute(
                    f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({placeholders})", old_ids
                )
            self.conn.execute(
                "DELETE FROM chunks WHERE reference_document_id=?", (int(document_id),)
            )
            count = 0
            for item in chunks:
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                cur = self.conn.execute(
                    "INSERT INTO chunks(source_kind,source_id,thread_id,text,token_count,"
                    "site_id,reference_document_id) VALUES('reference_document',?,?,?,? ,?,?)",
                    (
                        str(document_id),
                        None,
                        text,
                        int(item.get("token_count") or 0),
                        site,
                        int(document_id),
                    ),
                )
                embedding = item.get("embedding")
                if self.vec_enabled and embedding:
                    import struct

                    blob = struct.pack(f"{len(embedding)}f", *embedding)
                    self.conn.execute(
                        "INSERT INTO chunk_embeddings(chunk_id,embedding) VALUES(?,?)",
                        (int(cur.lastrowid), blob),
                    )
                count += 1
            self.conn.execute(
                "UPDATE reference_documents SET status='READY',error_text=NULL,"
                "chunk_count=?,updated_at=? WHERE id=?",
                (count, _now(), int(document_id)),
            )
            self.conn.commit()
            return count
        except Exception:
            self.conn.rollback()
            raise

    def search_reference_chunks(
        self, site_id: str, query: str, limit: int = 5
    ) -> list[sqlite3.Row]:
        """Dependency-free lexical fallback for site-scoped reference search."""
        site = validate_site_id(site_id)
        terms = [t for t in str(query or "").strip().split() if len(t) >= 2][:8]
        if not terms:
            return []
        clauses = " OR ".join("LOWER(c.text) LIKE ?" for _ in terms)
        params: list[Any] = [site, *[f"%{t.lower()}%" for t in terms], int(limit)]
        return self.conn.execute(
            "SELECT c.*,r.filename FROM chunks c "
            "JOIN reference_documents r ON r.id=c.reference_document_id "
            "WHERE c.site_id=? AND c.source_kind='reference_document' "
            f"AND ({clauses}) ORDER BY c.id DESC LIMIT ?",
            params,
        ).fetchall()

    def weekly_summary(self, site_id: str, days: int = 7) -> dict[str, Any]:
        site = validate_site_id(site_id)
        days = max(1, min(int(days), 31))
        modifier = f"-{days} days"
        received = self.conn.execute(
            "SELECT COUNT(*) AS n,COALESCE(SUM(CASE WHEN m.seen=0 THEN 1 ELSE 0 END),0) "
            "AS unread FROM messages m JOIN accounts a ON a.id=m.account_id "
            "WHERE a.site_id=? AND datetime(m.received_at)>=datetime('now',?)",
            (site, modifier),
        ).fetchone()
        categories = self.conn.execute(
            "SELECT c.category,COUNT(*) AS n FROM classifications c "
            "JOIN messages m ON m.id=c.message_id JOIN accounts a ON a.id=m.account_id "
            "WHERE a.site_id=? AND datetime(c.classified_at)>=datetime('now',?) "
            "GROUP BY c.category ORDER BY n DESC",
            (site, modifier),
        ).fetchall()
        important = self.conn.execute(
            "SELECT m.id,m.from_addr,m.from_name,m.subject,m.received_at,c.category,c.priority "
            "FROM messages m JOIN accounts a ON a.id=m.account_id "
            "LEFT JOIN classifications c ON c.message_id=m.id "
            "WHERE a.site_id=? AND datetime(m.received_at)>=datetime('now',?) "
            "ORDER BY COALESCE(c.priority,0) DESC,m.received_at DESC LIMIT 10",
            (site, modifier),
        ).fetchall()
        refs = self.conn.execute(
            "SELECT status,COUNT(*) AS n FROM reference_documents WHERE site_id=? GROUP BY status",
            (site,),
        ).fetchall()
        return {
            "site_id": site,
            "days": days,
            "received": int(received["n"] or 0),
            "unread": int(received["unread"] or 0),
            "quarantined": self.quarantined_count(site),
            "screening": {
                key: value
                for key, value in self.triage_counts(site).items()
                if key in {"questionable", "spam"}
            },
            "drafts": self.draft_counts(site),
            "categories": {r["category"]: int(r["n"]) for r in categories},
            "references": {r["status"]: int(r["n"]) for r in refs},
            "important": [dict(r) for r in important],
        }

    def dashboard_snapshot(
        self,
        site_id: str | None = None,
        recent_limit: int = 6,
        draft_limit: int = 6,
        include_sites: bool = False,
    ) -> dict[str, Any]:
        """Return one read-only aggregate for the dashboard.

        No message bodies or credentials are included in the account summary.
        """
        site = validate_site_id(site_id) if site_id is not None else None
        recent_limit = max(1, min(int(recent_limit), 50))
        draft_limit = max(1, min(int(draft_limit), 50))

        received = self.received_counts(site)
        draft_counts = self.draft_counts(site)
        manual_sql = "SELECT COUNT(*) AS n FROM drafts WHERE message_id IS NULL AND state='DRAFT'"
        manual_params: tuple[Any, ...] = ()
        if site is not None:
            manual_sql += " AND site_id=?"
            manual_params = (site,)
        manual_row = self.conn.execute(manual_sql, manual_params).fetchone()

        refs_sql = (
            "SELECT status,COUNT(*) AS n FROM reference_documents"
            + (" WHERE site_id=?" if site is not None else "")
            + " GROUP BY status"
        )
        refs_rows = self.conn.execute(refs_sql, (site,) if site is not None else ()).fetchall()
        references = {r["status"]: int(r["n"]) for r in refs_rows}
        references["total"] = sum(references.values())

        accounts_sql = "SELECT id,name,kind,host,port,username,site_id FROM accounts"
        account_params: tuple[Any, ...] = ()
        if site is not None:
            accounts_sql += " WHERE site_id=?"
            account_params = (site,)
        accounts_sql += " ORDER BY site_id,name"
        accounts = [dict(row) for row in self.conn.execute(accounts_sql, account_params).fetchall()]

        actionable_sql = (
            "SELECT d.*,COALESCE(m.from_addr,d.sender_addr) AS from_addr,"
            "m.from_name,m.received_at,c.category,c.priority,c.injection_risk "
            "FROM drafts d "
            "LEFT JOIN messages m ON m.id=d.message_id "
            "LEFT JOIN classifications c ON c.message_id=d.message_id "
            "WHERE d.state IN ('PENDING','BLOCKED','DEFERRED_NO_LLM')"
        )
        actionable_params: list[Any] = []
        if site is not None:
            actionable_sql += " AND d.site_id=?"
            actionable_params.append(site)
        actionable_sql += (
            " ORDER BY CASE d.state WHEN 'BLOCKED' THEN 0 WHEN 'PENDING' THEN 1 "
            "ELSE 2 END,COALESCE(c.priority,0) DESC,d.updated_at DESC,d.id DESC LIMIT ?"
        )
        actionable_params.append(draft_limit)
        actionable = [
            dict(row)
            for row in self.conn.execute(actionable_sql, tuple(actionable_params)).fetchall()
        ]

        snapshot: dict[str, Any] = {
            "site_id": site,
            "received": received,
            "drafts": draft_counts,
            "draft_total": sum(draft_counts.values()),
            "manual_drafts": int(manual_row["n"] or 0),
            "references": references,
            "accounts": accounts,
            "triage": self.triage_counts(site),
            "recent_received": [
                dict(row) for row in self.list_received(recent_limit, site_id=site)
            ],
            "needs_reply": [
                dict(row)
                for row in self.list_received(recent_limit, site_id=site, needs_reply_only=True)
            ],
            "quarantined_messages": [
                dict(row)
                for row in self.list_received(recent_limit, site_id=site, quarantined_only=True)
            ],
            "questionable_messages": [
                dict(row)
                for row in self.list_received(recent_limit, site_id=site, questionable_only=True)
            ],
            "actionable_drafts": actionable,
        }
        if site is None and include_sites:
            # Opt-in: each per-site snapshot is another ~15 queries and the
            # dashboard page itself never reads this key.
            snapshot["sites"] = {
                candidate: self.dashboard_snapshot(
                    candidate, recent_limit=recent_limit, draft_limit=draft_limit
                )
                for candidate in sorted(VALID_SITES)
            }
        return snapshot

    def delete_reference_document(self, document_id: int) -> bool:
        doc = self.get_reference_document(int(document_id))
        if doc is None:
            return False
        if doc["managed_key"] is not None:
            return False
        chunk_ids = [
            int(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM chunks WHERE reference_document_id=?", (int(document_id),)
            ).fetchall()
        ]
        try:
            if self.vec_enabled and chunk_ids:
                placeholders = ",".join("?" * len(chunk_ids))
                self.conn.execute(
                    f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({placeholders})", chunk_ids
                )
            self.conn.execute(
                "DELETE FROM chunks WHERE reference_document_id=?", (int(document_id),)
            )
            self.conn.execute("DELETE FROM reference_documents WHERE id=?", (int(document_id),))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        # Delete only files inside the app-owned reference library.
        try:
            from ..paths import data_dir

            root = (data_dir() / "references").resolve()
            path = Path(doc["stored_path"]).expanduser().resolve()
            if path.is_relative_to(root):
                path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("reference file cleanup failed for document %s: %s", document_id, e)
        return True

    # ----- chunks / embeddings (RAG) -----
    def add_chunk(
        self,
        source_kind: str,
        source_id: str,
        thread_id: str | None,
        text: str,
        token_count: int,
        site_id: str = DEFAULT_SITE_ID,
        reference_document_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO chunks(source_kind,source_id,thread_id,text,token_count,site_id,"
            "reference_document_id) VALUES(?,?,?,?,?,?,?)",
            (
                source_kind,
                source_id,
                thread_id,
                text,
                token_count,
                validate_site_id(site_id),
                reference_document_id,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_embedding(self, chunk_id: int, embedding: list[float]) -> None:
        if not self.vec_enabled:
            return
        import struct

        blob = struct.pack(f"{len(embedding)}f", *embedding)
        self.conn.execute(
            "INSERT OR REPLACE INTO chunk_embeddings(chunk_id, embedding) VALUES(?, ?)",
            (chunk_id, blob),
        )
        self.conn.commit()

    def knn_chunks(
        self,
        embedding: list[float],
        k: int,
        thread_id: str | None = None,
        site_id: str | None = None,
        reference_only: bool = False,
    ) -> list[dict[str, Any]]:
        """KNN over chunk_embeddings, optionally scoped to a single thread
        (prevents cross-thread history bleed, spec §4 / §5)."""
        if not self.vec_enabled:
            return []
        import struct

        blob = struct.pack(f"{len(embedding)}f", *embedding)
        # vec0 applies LIMIT before our ordinary chunks-table filters. When a
        # site/thread/reference scope is requested, scan all stored vectors so
        # globally closer chunks from another site cannot crowd out valid hits.
        scan_limit = k
        if thread_id is not None or site_id is not None or reference_only:
            count_row = self.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
            scan_limit = max(k, int(count_row["n"] or 0))
        rows = self.conn.execute(
            "SELECT chunk_id, distance FROM chunk_embeddings "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, scan_limit),
        ).fetchall()
        ids = [int(r["chunk_id"]) for r in rows]
        if not ids:
            return []
        distances = {int(r["chunk_id"]): float(r["distance"]) for r in rows}
        placeholders = ",".join("?" * len(ids))
        q = f"SELECT * FROM chunks WHERE id IN ({placeholders})"
        params: list[Any] = list(ids)
        if site_id is not None:
            q += " AND site_id=?"
            params.append(validate_site_id(site_id))
        if thread_id:
            q += " AND (thread_id=? OR thread_id IS NULL)"
            params.append(thread_id)
        if reference_only:
            q += " AND source_kind='reference_document'"
        fetched = self.conn.execute(q, params).fetchall()
        by_id = {int(r["id"]): dict(r) for r in fetched}
        out: list[dict[str, Any]] = []
        for chunk_id in ids:
            if chunk_id in by_id:
                item = by_id[chunk_id]
                item["distance"] = distances[chunk_id]
                out.append(item)
                if len(out) >= k:
                    break
        return out

    # ----- audit log (chained hash; full impl in audit/log.py) -----
    def last_audit_hash(self) -> str | None:
        row = self.conn.execute(
            "SELECT this_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["this_hash"] if row else None

    def append_audit_chained(
        self,
        ts: str,
        actor: str,
        event: str,
        subject_table: str | None,
        subject_id: int | None,
        detail_json: str,
        hasher: Callable[[str | None], str],
    ) -> tuple[int, str]:
        """Read the chain tail and append the next link atomically.

        ``hasher(prev_hash)`` computes ``this_hash``. The read+insert runs
        under a process-wide lock AND a ``BEGIN IMMEDIATE`` transaction (when
        this connection has none open), so two appenders — UI worker thread
        and the agent loop, or a second process — can never both chain onto
        the same predecessor and break ``verify_chain``.
        """
        with _AUDIT_APPEND_LOCK:
            began = False
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
                began = True
            try:
                prev_hash = self.last_audit_hash()
                this_hash = hasher(prev_hash)
                cur = self.conn.execute(
                    "INSERT INTO audit_log(ts,actor,event,subject_table,subject_id,"
                    "detail_json,prev_hash,this_hash) VALUES(?,?,?,?,?,?,?,?)",
                    (ts, actor, event, subject_table, subject_id, detail_json,
                     prev_hash, this_hash),
                )
                self.conn.commit()
            except Exception:
                if began:
                    self.conn.rollback()
                raise
            return int(cur.lastrowid), this_hash

    def append_audit(
        self,
        ts: str,
        actor: str,
        event: str,
        subject_table: str | None,
        subject_id: int | None,
        detail_json: str,
        prev_hash: str | None,
        this_hash: str,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO audit_log(ts,actor,event,subject_table,subject_id,detail_json,"
            "prev_hash,this_hash) VALUES(?,?,?,?,?,?,?,?)",
            (ts, actor, event, subject_table, subject_id, detail_json, prev_hash, this_hash),
        )
        self.conn.commit()
        return cur.lastrowid

    def iter_audit(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()

    def recent_audit(self, limit: int = 200) -> list[sqlite3.Row]:
        """Most recent audit events, newest first (Activity page)."""
        return self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()


def open_store(db_path: str | Path, init: bool = True) -> Store:
    s = Store(db_path)
    if init:
        s.init_schema()
    return s
