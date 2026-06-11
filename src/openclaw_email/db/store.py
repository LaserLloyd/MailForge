"""Data-access layer — ALL SQL lives here (build spec §2).

Owns connection lifecycle, schema init (with ``chmod 0600`` at creation per
§0.6), optional sqlite-vec extension loading, and typed helpers for every
table. Higher layers never write SQL directly.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Embedding dimension must match config LLM embedding model + vec0 column.
EMBED_DIM = 768

DraftState = str  # DRAFT|PENDING|APPROVED|REJECTED|SENT|BLOCKED|DEFERRED_NO_LLM


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema_sql() -> str:
    return resources.files("openclaw_email.db").joinpath("schema.sql").read_text("utf-8")


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(os.path.expanduser(os.path.expandvars(str(db_path)))).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vec_enabled = False
        existed = self.db_path.exists()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
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
        self.conn.executescript(_schema_sql())
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

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----- accounts -----
    def upsert_account(self, name: str, kind: str, host: str, port: int, username: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO accounts(name,kind,host,port,username) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, host=excluded.host, "
            "port=excluded.port, username=excluded.username",
            (name, kind, host, port, username),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
        return row["id"]

    def account_id(self, name: str) -> int | None:
        row = self.conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
        return row["id"] if row else None

    # ----- messages -----
    def insert_message(self, **f: Any) -> int | None:
        """Insert a message; returns row id, or None if the (account,folder,uid)
        already exists (idempotent ingest)."""
        cols = (
            "account_id folder uid message_id thread_id from_addr from_name to_addrs "
            "cc_addrs subject received_at raw_html sanitized_text has_attachments link_count"
        ).split()
        try:
            cur = self.conn.execute(
                f"INSERT INTO messages({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                tuple(f.get(c) for c in cols),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # duplicate UID — already ingested

    def get_message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    def thread_messages(self, thread_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY received_at", (thread_id,)
        ).fetchall()

    # ----- message links (controller-side, LLM never sees) -----
    def add_links(self, message_id: int, links: Iterable[tuple[str, str, str]]) -> None:
        self.conn.executemany(
            "INSERT INTO message_links(message_id,symbol,target,display_text) VALUES(?,?,?,?)",
            [(message_id, sym, tgt, disp) for sym, tgt, disp in links],
        )
        self.conn.commit()

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
        message_id: int,
        thread_id: str,
        recipient: str,
        subject: str,
        body: str,
        state: DraftState,
        guardrail_flags: dict | None = None,
    ) -> int:
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO drafts(message_id,thread_id,recipient,subject,body,state,"
            "guardrail_flags,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                thread_id,
                recipient,
                subject,
                body,
                state,
                json.dumps(guardrail_flags or {}),
                now,
                now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_draft_state(
        self, draft_id: int, state: DraftState, **f: Any
    ) -> None:
        sets = ["state=?", "updated_at=?"]
        vals: list[Any] = [state, _now()]
        for k in ("approved_by", "approved_at", "sent_at", "body", "recipient", "subject"):
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

    def drafts_by_state(self, state: DraftState) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM drafts WHERE state=? ORDER BY created_at DESC", (state,)
        ).fetchall()

    def deferred_drafts(self) -> list[sqlite3.Row]:
        return self.drafts_by_state("DEFERRED_NO_LLM")

    def draft_counts(self) -> dict[str, int]:
        """Counts per draft state (for the inbox stat chips / tab badges)."""
        rows = self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM drafts GROUP BY state"
        ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def drafts_overview(self, state: DraftState | None = None) -> list[sqlite3.Row]:
        """Drafts joined with their source message + classification in ONE query
        (the inbox previously issued 2 extra queries per row). ``state=None``
        returns all states, newest first."""
        q = (
            "SELECT d.*, m.from_addr, m.from_name, m.received_at, "
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

    # ----- chunks / embeddings (RAG) -----
    def add_chunk(
        self, source_kind: str, source_id: str, thread_id: str | None, text: str, token_count: int
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO chunks(source_kind,source_id,thread_id,text,token_count) "
            "VALUES(?,?,?,?,?)",
            (source_kind, source_id, thread_id, text, token_count),
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
        self, embedding: list[float], k: int, thread_id: str | None = None
    ) -> list[sqlite3.Row]:
        """KNN over chunk_embeddings, optionally scoped to a single thread
        (prevents cross-thread history bleed, spec §4 / §5)."""
        if not self.vec_enabled:
            return []
        import struct

        blob = struct.pack(f"{len(embedding)}f", *embedding)
        rows = self.conn.execute(
            "SELECT chunk_id, distance FROM chunk_embeddings "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, k * 4 if thread_id else k),
        ).fetchall()
        ids = [r["chunk_id"] for r in rows]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        q = f"SELECT * FROM chunks WHERE id IN ({placeholders})"
        params: list[Any] = list(ids)
        if thread_id:
            q += " AND (thread_id=? OR thread_id IS NULL)"
            params.append(thread_id)
        q += f" LIMIT {k}"
        return self.conn.execute(q, params).fetchall()

    # ----- audit log (chained hash; full impl in audit/log.py) -----
    def last_audit_hash(self) -> str | None:
        row = self.conn.execute(
            "SELECT this_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["this_hash"] if row else None

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
