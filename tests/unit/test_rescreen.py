"""`mailforge rescreen`: re-apply screening without overriding people."""

from __future__ import annotations

import pytest

from mailforge.config import Settings, SiteConfig
from mailforge.db.store import open_store, register_sites
from mailforge.rescreen import rescreen

# scrub-check: begin-allow  (public provider domain under test; local part synthetic)
GH_NOTIFY = "notifications@github.com"
# scrub-check: end-allow


@pytest.fixture(autouse=True)
def _registry():
    register_sites(["main", "shop"])
    yield
    register_sites(["main", "shop"])


def _settings():
    return Settings(
        imap_accounts=[],
        sites={
            "main": SiteConfig(name="Main"),
            "shop": SiteConfig(name="Acme Shop", screening_mode="content_only",
                               content_terms=["lasers?", "plywood"]),
        },
    )


def _add(store, uid, frm, subject, *, status="POTENTIAL_SPAM", source="AUTO_POLICY",
         trashed=0, site="shop"):
    acct = store.upsert_account(f"{site}1", "imap", "h", 993, f"in@{site}.test", site)
    mid = store.insert_message(
        account_id=acct, folder="INBOX", uid=uid, message_id=f"<{uid}@x.test>",
        thread_id=str(uid), from_addr=frm, from_name="Sender", to_addrs="", cc_addrs="",
        subject=subject, received_at="2026-07-16T00:00:00+00:00", raw_html=None,
        sanitized_text="body", has_attachments=0, link_count=0,
        screening_status=status, screening_source=source,
    )
    if trashed:
        store.set_messages_trashed([mid], True)
    return mid


def _status(store, mid):
    row = store.conn.execute(
        "SELECT m.screening_status, m.screening_source, c.category, c.model_used "
        "FROM messages m LEFT JOIN classifications c ON c.message_id=m.id WHERE m.id=?",
        (mid,),
    ).fetchone()
    return tuple(row)


def test_dry_run_reports_and_writes_nothing(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        gh = _add(store, 1, GH_NOTIFY, "[o/r] Run failed: CI")
        report = rescreen(store, _settings(), dry_run=True)
        assert report.transitions()[("POTENTIAL_SPAM", "FILED")] == 1
        assert not report.applied
        assert _status(store, gh) == ("POTENTIAL_SPAM", "AUTO_POLICY", None, None)


def test_apply_files_github_spams_cold_outreach_and_is_idempotent(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        gh = _add(store, 1, GH_NOTIFY, "[o/r] Run failed: CI")
        seo = _add(store, 2, "a@seo-agency.test", "SEO Services Proposal")
        report = rescreen(store, _settings(), dry_run=False)
        assert report.applied
        assert _status(store, gh) == ("FILED", "RULE", "GITHUB", "rule:github")
        assert _status(store, seo)[:2] == ("SPAM", "AUTO_RULE")
        again = rescreen(store, _settings(), dry_run=False)
        assert again.changes == [] and not again.applied


def test_human_and_model_decisions_are_never_overridden(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        human = _add(store, 1, GH_NOTIFY, "[o/r] Run failed: CI", status="CONTENT",
                     source="USER")
        model = _add(store, 2, "a@seo-agency.test", "Hello", status="SPAM", source="LLM")
        rescreen(store, _settings(), dry_run=False)
        assert _status(store, human)[:2] == ("CONTENT", "USER")
        assert _status(store, model)[:2] == ("SPAM", "LLM")


def test_withheld_mail_is_never_released_to_ai(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        topical = _add(store, 1, "maker@reader.test", "Plywood settings for my laser")
        report = rescreen(store, _settings(), dry_run=False)
        assert [c.message_id for c in report.kept_withheld] == [topical]
        assert _status(store, topical)[0] == "POTENTIAL_SPAM"


def test_readable_mail_moves_only_on_a_rule_verdict(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        # Screened UNSCREENED back when the site was "standard"; the
        # content_only catch-all would now say POTENTIAL_SPAM.
        old = _add(store, 1, "billing@vendor.test", "Your receipt", status="UNSCREENED")
        cold = _add(store, 2, "a@seo-agency.test", "SEO Services Proposal",
                    status="UNSCREENED")
        report = rescreen(store, _settings(), dry_run=False)
        assert [c.message_id for c in report.kept_readable] == [old]
        assert _status(store, old)[0] == "UNSCREENED"
        assert _status(store, cold)[0] == "SPAM"
        rescreen(store, _settings(), dry_run=False, all_statuses=True)
        assert _status(store, old)[0] == "POTENTIAL_SPAM"


def test_trashed_mail_is_skipped_unless_asked(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        gh = _add(store, 1, GH_NOTIFY, "[o/r] Run failed: CI", trashed=1)
        rescreen(store, _settings(), dry_run=False)
        assert _status(store, gh)[0] == "POTENTIAL_SPAM"
        rescreen(store, _settings(), dry_run=False, include_trashed=True)
        assert _status(store, gh)[0] == "FILED"


def test_rule_category_replaces_a_model_label_and_stale_rules_are_removed(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        gh = _add(store, 1, GH_NOTIFY, "[o/r] Run failed: CI", status="UNSCREENED",
                  site="main")
        store.upsert_classification(gh, category="NOTIFY", priority=1, rationale="x",
                                    injection_risk=0.0, model_used="some-model")
        rescreen(store, _settings(), dry_run=False)
        assert _status(store, gh) == ("FILED", "RULE", "GITHUB", "rule:github")
        # Removing the rule later does not release already-filed mail to AI:
        # that stays a human decision ("Mark as content").
        settings = _settings()
        settings.category_rules = []
        report = rescreen(store, settings, dry_run=False, all_statuses=True)
        assert [c.message_id for c in report.kept_withheld] == [gh]
        assert _status(store, gh)[0] == "FILED"


def test_stale_rule_classification_is_removed_when_mail_moves(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        mid = _add(store, 1, "a@seo-agency.test", "SEO Services Proposal")
        store.upsert_classification(mid, category="OLDRULE", priority=1, rationale="x",
                                    injection_risk=None, model_used="rule:old")
        rescreen(store, _settings(), dry_run=False)
        assert _status(store, mid) == ("SPAM", "AUTO_RULE", None, None)


def test_site_filter(tmp_path):
    with open_store(tmp_path / "r.db") as store:
        gh = _add(store, 1, GH_NOTIFY, "[o/r] Run failed: CI")
        rescreen(store, _settings(), site="main", dry_run=False)
        assert _status(store, gh)[0] == "POTENTIAL_SPAM"


def test_cli_dry_run_on_a_copy(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mailforge import cli

    db = tmp_path / "copy.db"
    with open_store(db) as store:
        _add(store, 1, GH_NOTIFY, "[o/r] Run failed: CI")
    monkeypatch.setattr("mailforge.config.load_settings", lambda: _settings())
    result = CliRunner().invoke(cli.app, ["rescreen", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "POTENTIAL_SPAM -> FILED: 1" in result.output
    assert "Dry run" in result.output
    with open_store(db) as store:
        assert store.conn.execute("SELECT screening_status FROM messages").fetchone()[0] == (
            "POTENTIAL_SPAM"
        )
