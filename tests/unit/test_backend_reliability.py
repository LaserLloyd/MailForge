from __future__ import annotations

import asyncio
import io
import sqlite3
from types import SimpleNamespace

import pytest

from bluebox.llm import bridge as bridge_module
from bluebox.llm.bridge import LMStudioBridge

from bluebox.agent import worker
from bluebox.agent.graph import AgentGraph
from bluebox.config import Settings
from bluebox.db.store import Store, open_store
from bluebox.mail.imap_listener import IMAPListener


class _CaptureBridge:
    def __init__(self, result: dict):
        self.result = result
        self.user_prompt = ""

    async def chat_structured(self, messages, _schema, **_kwargs):
        self.user_prompt = messages[-1]["content"]
        return self.result


def test_packaged_prompts_receive_email_metadata_context_and_style():
    classify_bridge = _CaptureBridge(
        {"category": "RESPOND", "priority": 2, "rationale": "reply requested"}
    )
    asyncio.run(worker.classify(classify_bridge, "UNIQUE_EMAIL_BODY", "UNIQUE_META"))
    assert "UNIQUE_EMAIL_BODY" in classify_bridge.user_prompt
    assert "UNIQUE_META" in classify_bridge.user_prompt
    assert "{email_content}" not in classify_bridge.user_prompt

    draft_bridge = _CaptureBridge(
        {
            "recipient": "person@example.com",
            "subject": "Re: Hello",
            "body": "Draft",
            "rationale": "requested",
        }
    )
    style = SimpleNamespace(tone="warm", signature="Kind regards")
    asyncio.run(
        worker.draft_reply(
            draft_bridge,
            "UNIQUE_EMAIL_BODY",
            ["UNIQUE_SITE_REFERENCE"],
            "person@example.com",
            style,
            site_id="main",
            site_rule="SITE_RULE_FROM_CONFIG",
        )
    )
    for expected in (
        "UNIQUE_EMAIL_BODY",
        "UNIQUE_SITE_REFERENCE",
        "person@example.com",
        "warm",
        "Kind regards",
        # The configured [sites.<id>].guidance is what reaches the prompt.
        "SITE_RULE_FROM_CONFIG",
    ):
        assert expected in draft_bridge.user_prompt
    assert "<REFERENCE_DATA>" in draft_bridge.user_prompt
    assert "</REFERENCE_DATA>" in draft_bridge.user_prompt


def test_embedding_requests_are_batched_and_ordered(monkeypatch):
    requests: list[list[str]] = []

    class _Response:
        def __init__(self, batch):
            self.batch = batch

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [float(value)] * 768}
                    for index, value in reversed(list(enumerate(self.batch)))
                ]
            }

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, json):
            batch = list(json["input"])
            requests.append(batch)
            return _Response(batch)

    monkeypatch.setattr(bridge_module.httpx, "AsyncClient", _Client)
    bridge = LMStudioBridge(host="localhost:1234", model_id="chat", embed_id="embed")
    vectors = asyncio.run(bridge.embed(list(range(65))))
    assert [len(batch) for batch in requests] == [32, 32, 1]
    assert [vector[0] for vector in vectors] == [float(i) for i in range(65)]


def test_embedding_failed_batch_splits_before_failing_document(monkeypatch):
    request_sizes: list[int] = []

    class _Response:
        def __init__(self, batch):
            self.batch = batch

        def raise_for_status(self):
            if len(self.batch) > 8:
                raise RuntimeError("batch rejected")

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [float(value)] * 768}
                    for index, value in enumerate(self.batch)
                ]
            }

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, json):
            batch = list(json["input"])
            request_sizes.append(len(batch))
            return _Response(batch)

    monkeypatch.setattr(bridge_module.httpx, "AsyncClient", _Client)
    bridge = LMStudioBridge(host="localhost:1234", model_id="chat", embed_id="embed")
    vectors = asyncio.run(bridge.embed(list(range(20))))
    assert request_sizes == [20, 10, 5, 5, 10, 5, 5]
    assert [vector[0] for vector in vectors] == [float(i) for i in range(20)]


def test_cli_ingest_awaits_async_ingester(monkeypatch):
    from bluebox import cli
    from bluebox.rag import embedder

    called = []

    async def fake_ingest(_settings, include_style=True, include_context=True):
        called.append((include_style, include_context))
        return 7

    monkeypatch.setattr(embedder, "ingest_documents", fake_ingest)
    monkeypatch.setattr("bluebox.config.load_settings", lambda: object())
    cli.ingest(style=True, context=False)
    assert called == [(True, False)]


def _insert_message(store, uid=1):
    account = store.upsert_account("mail", "imap", "host", 993, "me@example.com", "main")
    return store.insert_message(
        account_id=account,
        folder="INBOX",
        uid=uid,
        message_id=f"<m{uid}>",
        thread_id=f"t{uid}",
        from_addr="person@example.com",
        from_name="Person",
        to_addrs="me@example.com",
        cc_addrs="",
        subject="Hello",
        received_at="2026-07-16T00:00:00+00:00",
        raw_html=None,
        sanitized_text="Please reply",
        has_attachments=0,
        link_count=0,
    )


def test_deferred_draft_is_updated_not_duplicated(tmp_path):
    store = open_store(tmp_path / "mail.db")
    message_id = _insert_message(store)
    first = store.upsert_message_draft(
        message_id, "t1", "person@example.com", "Re: Hello", "", "DEFERRED_NO_LLM"
    )
    second = store.upsert_message_draft(
        message_id, "t1", "person@example.com", "Re: Hello", "Ready", "PENDING"
    )
    assert second == first
    assert store.get_draft(first)["state"] == "PENDING"
    assert store.get_draft(first)["body"] == "Ready"
    assert len(store.drafts_overview()) == 1


def test_inbound_thread_sender_is_persisted_as_exact_recipient(tmp_path):
    store = open_store(tmp_path / "recipient.db")
    message_id = _insert_message(store)
    message = store.get_message(message_id)
    graph = AgentGraph(store, Settings(), bridge=None)

    assert graph._bind_recipient(message, message["thread_id"]) == "person@example.com"
    assert store.is_allowlisted("person@example.com")
    store.close()


class _CursorStore:
    def __init__(self, cursor=None, stored=None):
        self.cursor = cursor
        self.stored = set(stored or [])

    def message_uids(self, _account_id, _folder):
        return set(self.stored)

    def mailbox_cursor(self, _account_id, _folder):
        return self.cursor

    def set_mailbox_cursor(self, _account_id, _folder, uid):
        self.cursor = uid


class _Mailbox:
    def __init__(self, uids, fetched):
        self._uids = uids
        self._fetched = fetched

    def uids(self, criteria="ALL"):
        # Honour "UID n:*" the way a server would (listener asks only past cursor).
        if isinstance(criteria, str) and criteria.startswith("UID "):
            lo = int(criteria[4:].split(":")[0])
            return [str(uid) for uid in self._uids if uid >= lo]
        return [str(uid) for uid in self._uids]

    def fetch(self, _criteria, **_kwargs):
        return iter(SimpleNamespace(uid=str(uid)) for uid in self._fetched)


def _listener(store):
    listener = object.__new__(IMAPListener)
    listener.store = store
    listener._account_id = 1
    listener.account = SimpleNamespace(name="test")
    return listener


def test_initial_imap_backfill_is_bounded_and_cursor_is_durable():
    store = _CursorStore()
    listener = _listener(store)
    handled = []
    notified = []

    def ingest(msg, _folder, notify=True):
        handled.append(int(msg.uid))
        notified.append(notify)
        return True

    listener._ingest_one = ingest
    mailbox = _Mailbox(range(1, 101), range(51, 101))
    assert listener._ingest_missing(mailbox, "INBOX", initial=True) == 50
    assert handled == list(range(51, 101))
    assert notified == [True] * 50  # first-run mail enters autonomous draft processing
    assert store.cursor == 100


def test_imap_cursor_does_not_advance_after_per_message_failure():
    store = _CursorStore(cursor=10, stored=range(1, 11))
    listener = _listener(store)

    def fail_one(msg, _folder, notify=True):
        if int(msg.uid) == 11:
            raise ValueError("bad message")
        return True

    listener._ingest_one = fail_one
    assert listener._ingest_missing(_Mailbox([11, 12], [11, 12]), "INBOX") == 1
    assert store.cursor == 10


def test_fresh_and_legacy_schema_migrate_site_reference_and_revision_columns(tmp_path):
    fresh = open_store(tmp_path / "fresh.db")
    for table in ("reference_documents", "ai_revisions", "mailbox_cursors"):
        assert fresh.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    fresh.close()

    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        CREATE TABLE accounts(id INTEGER PRIMARY KEY,name TEXT UNIQUE,kind TEXT,host TEXT,port INTEGER,username TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,folder TEXT NOT NULL,uid INTEGER NOT NULL,message_id TEXT,thread_id TEXT NOT NULL,from_addr TEXT,from_name TEXT,to_addrs TEXT,cc_addrs TEXT,subject TEXT,received_at TEXT,raw_html BLOB,sanitized_text TEXT,has_attachments INTEGER,link_count INTEGER,seen INTEGER DEFAULT 0,archived INTEGER DEFAULT 0,UNIQUE(account_id,folder,uid));
        CREATE TABLE drafts(id INTEGER PRIMARY KEY,message_id INTEGER,thread_id TEXT NOT NULL,recipient TEXT NOT NULL,subject TEXT,body TEXT,state TEXT NOT NULL,guardrail_flags TEXT,created_at TEXT,updated_at TEXT,approved_by TEXT,approved_at TEXT,sent_at TEXT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY,source_kind TEXT,source_id TEXT,thread_id TEXT,text TEXT,token_count INTEGER);
        """
    )
    conn.close()
    legacy = Store(legacy_path)
    legacy.init_schema()
    assert {r["name"] for r in legacy.conn.execute("PRAGMA table_info(accounts)")} >= {"site_id"}
    assert {r["name"] for r in legacy.conn.execute("PRAGMA table_info(drafts)")} >= {
        "site_id", "sender_addr"
    }
    assert {r["name"] for r in legacy.conn.execute("PRAGMA table_info(chunks)")} >= {
        "site_id", "reference_document_id"
    }


def test_site_scoped_reference_search_does_not_cross_contaminate(tmp_path):
    store = open_store(tmp_path / "refs.db")
    main = store.create_reference_document("main", "main.txt", "/tmp/main", "text/plain", 1)
    laser = store.create_reference_document(
        "shop", "laser.txt", "/tmp/laser", "text/plain", 1
    )
    store.replace_document_chunks(
        main, "main", [{"text": "laser pricing pricing pricing", "token_count": 4, "embedding": None}]
    )
    store.replace_document_chunks(
        laser,
        "shop",
        [{"text": "laser project reference", "token_count": 3, "embedding": None}],
    )
    rows = store.search_reference_chunks("shop", "laser", 5)
    assert [row["filename"] for row in rows] == ["laser.txt"]


def test_knn_site_scope_overfetches_past_globally_closer_other_site():
    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

    class Conn:
        def execute(self, sql, params=()):
            if "COUNT(*) AS n FROM chunks" in sql:
                return Result([{"n": 2}])
            if "FROM chunk_embeddings" in sql:
                assert params[1] == 2  # all vectors, not only global top k=1
                return Result(
                    [
                        {"chunk_id": 1, "distance": 0.01},  # closer, wrong site
                        {"chunk_id": 2, "distance": 0.50},  # farther, correct site
                    ]
                )
            if "FROM chunks WHERE id IN" in sql:
                assert "shop" in params
                return Result(
                    [{"id": 2, "site_id": "shop", "text": "laser answer",
                      "thread_id": None, "source_kind": "reference_document"}]
                )
            raise AssertionError(sql)

    store = object.__new__(Store)
    store.vec_enabled = True
    store.conn = Conn()
    rows = store.knn_chunks(
        [0.0], k=1, site_id="shop", reference_only=True
    )
    assert [row["id"] for row in rows] == [2]


def test_reference_delete_removes_chunks_and_only_app_owned_file(tmp_path, monkeypatch):
    from bluebox import paths

    data = tmp_path / "data"
    library = data / "references" / "main"
    library.mkdir(parents=True)
    uploaded = library / "guide.txt"
    uploaded.write_text("guide", encoding="utf-8")
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    store = open_store(tmp_path / "delete.db")
    doc_id = store.create_reference_document(
        "main", uploaded.name, str(uploaded), "text/plain", uploaded.stat().st_size
    )
    store.replace_document_chunks(
        doc_id, "main", [{"text": "guide text", "token_count": 2, "embedding": None}]
    )
    assert store.delete_reference_document(doc_id) is True
    assert store.get_reference_document(doc_id) is None
    assert store.conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE reference_document_id=?", (doc_id,)
    ).fetchone()[0] == 0
    assert not uploaded.exists()


def test_revision_refuses_immutable_sent_draft(tmp_path):
    from bluebox.agent.revisions import revise_draft
    from bluebox.config import Settings

    store = open_store(tmp_path / "immutable.db")
    message_id = _insert_message(store)
    draft_id = store.create_draft(
        message_id, "t1", "person@example.com", "Re: Hello", "Sent body", "SENT"
    )
    with pytest.raises(ValueError, match="immutable"):
        asyncio.run(
            revise_draft(
                store, Settings(), object(), draft_id, "change it", site_id="main"
            )
        )


def test_bridge_rejects_unknown_site_before_opening_database(monkeypatch, capsys):
    from bluebox.bridge_cli import run_bridge

    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert run_bridge("status", "not-a-site") == 2
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_request"


def test_lmstudio_health_falls_back_to_openai_api_when_sdk_rejects_remote_host(
    monkeypatch,
):
    monkeypatch.setattr(bridge_module, "_HAVE_LMS", True)
    monkeypatch.setattr(
        bridge_module,
        "lms",
        SimpleNamespace(
            Client=SimpleNamespace(is_valid_api_host=lambda _host: False)
        ),
    )
    monkeypatch.setattr(
        bridge_module.httpx,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200),
    )
    bridge = LMStudioBridge("203.0.113.2:1234", "chat", "embed")
    assert bridge.is_up() is True
