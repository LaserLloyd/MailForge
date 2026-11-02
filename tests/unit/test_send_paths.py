"""The send paths: a send that went out stays sent, whatever the UI does next."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mailforge.config import IMAPAccount, Settings, SMTPAccount
from mailforge.db.store import open_store
from mailforge.ui import presend
from mailforge.ui.pages import compose, detail

FAKE_KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"  # AWS's documented dummy key


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "s.db") as s:
        yield s


def _settings() -> Settings:
    return Settings(
        imap_accounts=[
            IMAPAccount(name="main", host="imap.example", username="inbox@main.example",
                        site_id="main")
        ],
        smtp=SMTPAccount(host="smtp.example", username="inbox@main.example"),
    )


def _reply_draft(store, state="PENDING") -> int:
    return store.create_draft(
        message_id=None, thread_id="t-1", recipient="bob@example.test", subject="Re: Q",
        body="Here is the answer.", state=state, sender_addr="inbox@main.example",
        site_id="main",
    )


class _DeadClient:
    """What a deleted page looks like to code that kept the client around."""

    def __enter__(self):
        raise RuntimeError("The parent element this slot belongs to has been deleted.")

    def __exit__(self, *a):
        return False


# --- run_send --------------------------------------------------------------
def test_persist_failure_after_a_real_send_is_reported_not_raised():
    def persist(_):
        raise OSError("database is locked")

    async def go():
        async def transmit():
            return 5

        return await presend.run_send(transmit, persist=persist)

    res = asyncio.run(go())
    assert res.sent and res.record_id == 5 and "locked" in res.persist_error


def test_transmit_failure_is_a_failed_result_and_calls_on_failed():
    seen = []

    async def go():
        async def transmit():
            raise ConnectionError("SMTP refused")

        return await presend.run_send(transmit, on_failed=seen.append)

    res = asyncio.run(go())
    assert not res.sent and "SMTP refused" in res.error and len(seen) == 1


def test_closing_the_page_mid_send_does_not_stop_the_bookkeeping():
    marked = []

    async def go():
        gate = asyncio.Event()

        async def transmit():
            await gate.wait()
            return 9

        task = presend.start_send(presend.run_send(transmit, persist=marked.append))

        async def handler():  # the click handler awaiting the send
            await asyncio.shield(task)

        h = asyncio.ensure_future(handler())
        await asyncio.sleep(0)
        h.cancel()  # tab closed: NiceGUI drops the handler
        with pytest.raises(asyncio.CancelledError):
            await h
        gate.set()
        return await task

    res = asyncio.run(go())
    assert res.sent and marked == [9]


def test_safe_notify_survives_a_deleted_page():
    presend.safe_notify(_DeadClient(), "Sent.", type="positive")  # must not raise
    assert presend.safe_ui(_DeadClient(), lambda: 1) is None


# --- F1 / double-send: a UI failure after the send keeps the draft SENT ------
def test_ui_exception_after_a_successful_send_leaves_the_draft_sent(store, monkeypatch):
    calls = []
    monkeypatch.setattr(detail, "_do_send", lambda *a, **k: calls.append(1) or 11)
    draft_id = _reply_draft(store)
    draft = store.get_draft(draft_id)

    async def go():
        async def transmit():
            return detail.approve_and_transmit(
                store, _settings(), draft, "Here is the answer.", "bob@example.test", {}, []
            )

        res = await presend.run_send(
            transmit,
            persist=lambda _r: detail.mark_sent(
                store, draft_id, "Here is the answer.", "bob@example.test"
            ),
            on_failed=lambda e: detail.restore_after_failed_send(
                store, draft_id, "Here is the answer.", {}, e
            ),
            store=store,
        )
        # the UI feedback that used to explode and trigger the revert to PENDING
        presend.notify_result(_DeadClient(), res, sent_text="Approved and sent.", failed_prefix="x")
        presend.safe_ui(_DeadClient(), lambda: None)
        return res

    res = asyncio.run(go())
    assert res.sent
    assert store.get_draft(draft_id)["state"] == "SENT"
    assert len(calls) == 1


def test_a_sent_draft_cannot_be_sent_again(store, monkeypatch):
    calls = []
    monkeypatch.setattr(detail, "_do_send", lambda *a, **k: calls.append(1) or 1)
    draft_id = _reply_draft(store)
    draft = store.get_draft(draft_id)
    detail.approve_and_transmit(store, _settings(), draft, "Body", "bob@example.test", {}, [])
    detail.mark_sent(store, draft_id, "Body", "bob@example.test")
    with pytest.raises(RuntimeError, match="not sent again"):
        detail.approve_and_transmit(store, _settings(), draft, "Body", "bob@example.test", {}, [])
    assert len(calls) == 1


def test_a_failed_transmit_returns_the_draft_to_pending_but_never_a_sent_one(store, monkeypatch):
    def smtp_down(*a, **k):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(detail, "_do_send", smtp_down)
    draft_id = _reply_draft(store)
    draft = store.get_draft(draft_id)
    with pytest.raises(ConnectionError) as info:
        detail.approve_and_transmit(store, _settings(), draft, "Body", "bob@example.test", {}, [])
    assert store.get_draft(draft_id)["state"] == "APPROVED"
    assert detail.restore_after_failed_send(store, draft_id, "Body", {}, info.value) is True
    row = store.get_draft(draft_id)
    assert row["state"] == "PENDING" and "smtp down" in row["guardrail_flags"]

    store.update_draft_state(draft_id, "SENT")
    assert detail.restore_after_failed_send(store, draft_id, "Body", {}, info.value) is False
    assert store.get_draft(draft_id)["state"] == "SENT"


def test_override_is_recorded_on_the_draft_and_in_the_audit_log(store, monkeypatch):
    monkeypatch.setattr(detail, "_do_send", lambda *a, **k: 3)
    draft_id = _reply_draft(store, state="DRAFT")
    soft = [presend.Check("pii", "No personal details", False, "Looks like a card number.")]
    detail.approve_and_transmit(
        store, _settings(), store.get_draft(draft_id), "Body", "bob@example.test",
        {"pii": ["CREDIT_CARD"]}, soft,
    )
    flags = json.loads(store.get_draft(draft_id)["guardrail_flags"])
    assert flags["override"] == ["pii"]
    rows = [
        json.loads(r["detail_json"])
        for r in store.conn.execute("SELECT detail_json FROM audit_log").fetchall()
    ]
    assert any(d.get("action") == "guardrail_override" and d["overridden"] == ["pii"] for d in rows)


def test_no_override_audit_means_no_send(store, monkeypatch):
    calls = []
    monkeypatch.setattr(detail, "_do_send", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(
        presend, "record_override", lambda *a, **k: (_ for _ in ()).throw(OSError("audit down"))
    )
    draft_id = _reply_draft(store)
    with pytest.raises(OSError):
        detail.approve_and_transmit(
            store, _settings(), store.get_draft(draft_id), "Body", "bob@example.test", {}, []
        )
    assert calls == []


# --- compose ---------------------------------------------------------------
def test_compose_now_runs_the_content_guards(store):
    ev = compose.evaluate_compose(
        store, _settings(), None, "inbox@main.example", "carol@new.test", "Hi",
        f"The login is {FAKE_KEY}",
    )
    by = {c.id: c for c in ev.checks}
    assert by["secrets"].warning and FAKE_KEY not in by["secrets"].detail
    # a first contact is not a warning in Compose
    assert by["rule_a"].passed and by["rule_b"].passed


def test_compose_hard_gates(store):
    ev = compose.evaluate_compose(
        store, _settings(), None, "forged@elsewhere.test", "carol@new.test", "Hi", ""
    )
    by = {c.id: c for c in ev.checks}
    assert by["sender_ok"].blocking and by["body_present"].blocking


def test_compose_sent_draft_cannot_be_sent_twice(store, monkeypatch):
    calls = []
    monkeypatch.setattr(compose, "_do_send", lambda *a, **k: calls.append(1) or 2)
    s = _settings()
    did = compose.save_for_send(store, s, None, "inbox@main.example", "c@new.test", "Hi", "Body")
    compose.send_manual(store, s, did, "inbox@main.example", "c@new.test", "Hi", "Body", None, {}, [])
    compose.mark_manual_sent(store, did)
    assert store.get_manual_draft(did)["state"] == "SENT"
    with pytest.raises(RuntimeError, match="already sent"):
        compose.send_manual(store, s, did, "inbox@main.example", "c@new.test", "Hi", "Body", None, {}, [])
    assert len(calls) == 1


def test_compose_failed_send_keeps_the_draft_for_a_retry(store, monkeypatch):
    def down(*a, **k):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(compose, "_do_send", down)
    s = _settings()
    did = compose.save_for_send(store, s, None, "inbox@main.example", "c@new.test", "Hi", "Body")
    with pytest.raises(ConnectionError):
        compose.send_manual(store, s, did, "inbox@main.example", "c@new.test", "Hi", "Body", None, {}, [])
    assert store.get_manual_draft(did)["state"] == "DRAFT"
    # saving again updates the same draft instead of creating a second one
    assert compose.save_for_send(store, s, did, "inbox@main.example", "c@new.test", "Hi", "B2") == did


def test_compose_override_is_kept_on_the_draft(store, monkeypatch):
    monkeypatch.setattr(compose, "_do_send", lambda *a, **k: 2)
    s = _settings()
    did = compose.save_for_send(store, s, None, "inbox@main.example", "c@new.test", "Hi", "Body")
    soft = [presend.Check("secrets", "s", False, "Looks like it contains: AWSKeyDetector.")]
    compose.send_manual(store, s, did, "inbox@main.example", "c@new.test", "Hi", "Body", None,
                        {"secrets": ["AWSKeyDetector"]}, soft)
    assert json.loads(store.get_manual_draft(did)["guardrail_flags"])["override"] == ["secrets"]


# --- F2: a failed Sent-folder copy must surface ----------------------------
def test_failed_sent_folder_copy_surfaces(store):
    rid = store.begin_send_record("inbox@main.example", "c@new.test", "Hi", "Body", origin="ui_compose")
    store.finish_send_record(rid, "SENT", smtp_message_id="<m@x>")
    assert presend.append_warning(store, rid) == ""
    store.record_send_append(rid, "FAILED", folder=None, note="login rejected")
    msg = presend.append_warning(store, rid)
    assert msg.startswith("Sent.") and "Sent folder" in msg and "login rejected" in msg
    store.record_send_append(rid, "OK", folder="Sent", note=None)
    assert presend.append_warning(store, rid) == ""
    store.record_send_append(rid, "SKIPPED", folder=None, note="no raw bytes")
    assert "no raw bytes" in presend.append_warning(store, rid)


def test_run_send_carries_the_copy_warning(store):
    rid = store.begin_send_record("inbox@main.example", "c@new.test", "Hi", "Body", origin="ui_compose")
    store.finish_send_record(rid, "SENT", smtp_message_id="<m@x>")
    store.record_send_append(rid, "FAILED", folder=None, note="timeout")

    async def go():
        async def transmit():
            return rid

        return await presend.run_send(transmit, store=store)

    assert "timeout" in asyncio.run(go()).copy_warning


# --- no way to send that skips the dialog ----------------------------------
UI_DIR = Path(__file__).resolve().parents[2] / "src" / "mailforge" / "ui"


def _calls(tree: ast.AST, names: set[str]):
    """Calls to, or references of, the named functions (``run.io_bound(fn, ...)``
    passes the function without calling it, and must count)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name in names:
                yield node
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id in names:
                    yield arg


def _enclosing(tree: ast.AST) -> dict[int, list[str]]:
    """call node id -> names of the functions it sits inside (outermost first)."""
    out: dict[int, list[str]] = {}

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            nxt = stack + [child.name] if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else stack
            out[id(child)] = nxt
            walk(child, nxt)

    walk(tree, [])
    return out


def test_every_ui_send_goes_through_presend_confirm():
    guarded = 0
    for path in sorted(UI_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = _enclosing(tree)
        # the low-level transmitter may only be called from a page's _do_send
        for call in _calls(tree, {"transmit_and_record"}):
            assert where[id(call)][-1:] == ["_do_send"], f"{path.name}: transmit outside _do_send"
        # _do_send may only be called from the pre-send helpers presend.confirm drives
        for call in _calls(tree, {"_do_send"}):
            chain = where[id(call)]
            assert chain and chain[-1] in {"approve_and_transmit", "send_manual"}, (
                f"{path.name}: _do_send called from {chain}"
            )
        for fn in ("approve_and_transmit", "send_manual"):
            for call in _calls(tree, {fn}):
                chain = where[id(call)]
                assert "_transmit" in chain, f"{path.name}: {fn} called outside a _transmit callback"
                # ... and that callback belongs to a handler that calls presend.confirm
                handler = next(n for n in ast.walk(tree) if isinstance(n, (
                    ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == chain[0])
                confirms = [c for c in _calls(handler, {"confirm"})
                            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                            and getattr(c.func.value, "id", "") == "presend"]
                assert confirms, f"{path.name}: {chain[0]} sends without presend.confirm"
                guarded += 1
    assert guarded == 2  # detail + compose


def test_the_bridge_is_the_only_other_sender_and_it_is_not_ui():
    for path in UI_DIR.rglob("*.py"):
        assert "bridge_cli" not in path.read_text(encoding="utf-8") or path.name == "app.py"
    assert SimpleNamespace  # keep import used
