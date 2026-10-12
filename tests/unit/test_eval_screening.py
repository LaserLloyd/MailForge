"""`mailforge eval-screening`: leave-one-out replay of human labels."""

from __future__ import annotations

import pytest

from mailforge.config import Settings, SiteConfig
from mailforge.db.store import open_store, register_sites
from mailforge.eval_screening import evaluate, format_report



def _settings():
    return Settings(
        imap_accounts=[],
        sites={"main": SiteConfig(name="Main"), "shop": SiteConfig(name="Acme Shop")},
    )


def _add(store, uid, frm, subject, *, status="UNSCREENED", source="AUTO_POLICY", site="main"):
    acct = store.upsert_account(f"{site}1", "imap", "h", 993, f"in@{site}.test", site)
    return store.insert_message(
        account_id=acct, folder="INBOX", uid=uid, message_id=f"<{uid}@x.test>",
        thread_id=str(uid), from_addr=frm, from_name="Sender", to_addrs="", cc_addrs="",
        subject=subject, received_at="2026-07-16T00:00:00+00:00", raw_html=None,
        sanitized_text="body", has_attachments=0, link_count=0,
        screening_status=status, screening_source=source,
    )


@pytest.fixture(autouse=True)
def _registry():
    register_sites(["main", "shop"])
    yield
    register_sites(["main", "shop"])


def _spam(store, mid):
    store.record_screening_feedback(mid, "SPAM", learn_similar=True)


def test_leave_one_out_removes_only_the_rows_own_example(tmp_path):
    with open_store(tmp_path / "e.db") as store:
        a = _add(store, 1, "x@spammer.test", "Cheap watches now", status="UNSCREENED", site="main")
        b = _add(store, 2, "x@spammer.test", "Totally other subject", status="UNSCREENED", site="main")
        lone = _add(store, 3, "y@elsewhere.test", "Your invoice attached", status="UNSCREENED", site="main")
        for m in (a, b, lone):
            _spam(store, m)
        report = evaluate(store, _settings())
        by_id = {r.message_id: r for r in report.replays}
        # a and b teach each other (same sender); the lone label has no other example.
        assert by_id[a].predicted == "SPAM" and by_id[b].predicted == "SPAM"
        assert by_id[lone].predicted == "UNSCREENED"
        assert report.recall("SPAM") == pytest.approx(2 / 3)
        # The live data is untouched: all three examples still exist.
        assert len(store.screening_examples("main")) == 3


def test_only_spam_labels_say_recall_only(tmp_path):
    with open_store(tmp_path / "e.db") as store:
        m = _add(store, 1, "x@spammer.test", "Cheap watches now", status="UNSCREENED", site="main")
        _spam(store, m)
        report = evaluate(store, _settings())
    text = "\n".join(format_report(report))
    assert "no negative labels" in text.lower() and "recall only" in text.lower()
    assert report.precision("SPAM") is None


def test_manual_filed_and_content_labels_give_precision(tmp_path):
    from mailforge.config import CategoryRule

    rule = CategoryRule(name="digest", label="Digests", category="DIGEST",
                        from_domains=["filed.test"])
    settings = _settings().model_copy(update={"category_rules": [rule]})
    with open_store(tmp_path / "e.db") as store:
        gh = _add(store, 1, "bot@filed.test", "Weekly digest of things",
                  status="FILED", source="USER", site="main")
        ok = _add(store, 2, "friend@example.test", "Dinner on Friday evening",
                  status="UNSCREENED", site="main")
        store.record_screening_feedback(ok, "CONTENT")
        sp = _add(store, 3, "z@spammer.test", "Cheap watches now", status="UNSCREENED", site="main")
        sp2 = _add(store, 4, "z@spammer.test", "More cheap watches", status="UNSCREENED", site="main")
        _spam(store, sp)
        _spam(store, sp2)
        report = evaluate(store, settings)
    by_id = {r.message_id: r for r in report.replays}
    assert by_id[gh].source == "manual" and by_id[gh].predicted == "FILED"
    assert by_id[ok].truth == "CONTENT"
    assert report.negatives_exist
    assert report.precision("SPAM") == 1.0 and report.recall("FILED") == 1.0
    assert "No negative labels" not in "\n".join(format_report(report))


def test_limit_takes_the_most_recent(tmp_path):
    with open_store(tmp_path / "e.db") as store:
        ids = [_add(store, i, f"s{i}@spammer.test", f"Subject number {i} here", status="UNSCREENED",
                    site="main") for i in range(1, 5)]
        for i, m in enumerate(ids):
            store.conn.execute("UPDATE messages SET received_at=? WHERE id=?",
                               (f"2026-07-{10 + i:02d}T00:00:00+00:00", m))
            _spam(store, m)
        report = evaluate(store, _settings(), limit=2)
    assert {r.message_id for r in report.replays} == set(ids[-2:])


def test_empty_db_and_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mailforge import cli

    db = tmp_path / "e.db"
    with open_store(db) as store:
        _add(store, 1, "x@spammer.test", "Cheap watches now", status="UNSCREENED", site="main")
        assert evaluate(store, _settings()).replays == []
    monkeypatch.setattr("mailforge.config.load_settings", lambda: _settings())
    r = CliRunner().invoke(cli.app, ["eval-screening", "--db", str(db)])
    assert r.exit_code == 0 and "No human labels" in r.output
