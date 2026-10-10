"""Deterministic category rules: GitHub notifications get their own bucket.

Synthetic repositories and senders only; github.com is the rule's subject.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mailforge.config import (
    GITHUB_RULE,
    CategoryRule,
    Settings,
    default_category_rules,
)
from mailforge.db.store import open_store, register_sites
from mailforge.security.inbound_screening import (
    AI_WITHHELD_STATUSES,
    assess_inbound,
    match_category_rule,
)

RULES = default_category_rules()
# scrub-check: begin-allow  (public provider domains under test; local parts are synthetic)
GH_NOTIFY = "notifications@github.com"
GH_NOREPLY = "noreply@github.com"
GH_REPLY_SUB = "noreply@reply.github.com"
NOT_GH = "a@notgithub.com"
GH_A = "a@github.com"
GH_SUFFIX_TRICK = "noreply@github.com.attacker.test"
GH_TYPO = "a@github.co"
# scrub-check: end-allow

GH_AUTH = {"dkim": "pass", "dkim_pass_domains": ["github.com"], "dmarc": "pass",
           "dmarc_from": "github.com"}


@pytest.fixture(autouse=True)
def _registry():
    register_sites(["main", "shop"])
    yield
    register_sites(["main", "shop"])


def screen(subject, frm=GH_NOTIFY, mode="content_only", headers=None, **kw):
    return assess_inbound(
        mode=mode, from_addr=frm, from_name="Someone", subject=subject, text="",
        own_domains=("shop.test",), headers=headers if headers is not None else GH_AUTH,
        category_rules=RULES, site_id="shop", **kw,
    )


@pytest.mark.parametrize("mode", ["content_only", "standard"])
@pytest.mark.parametrize(
    "frm, subject",
    [
        (GH_NOTIFY, "[owner/repo] Run failed: CI - main (abc1234)"),
        (GH_NOTIFY, "Re: [owner/repo] Fix the parser (PR #12)"),
        (GH_NOREPLY, "Your free Copilot access is ready"),
        (GH_REPLY_SUB, "[owner/repo] New issue"),
    ],
)
def test_github_notifications_are_filed(mode, frm, subject):
    r = screen(subject, frm=frm, mode=mode)
    assert (r.status, r.source, r.category, r.rule, r.priority) == (
        "FILED", "RULE", "GITHUB", "github", 1
    )
    assert r.status in AI_WITHHELD_STATUSES


@pytest.mark.parametrize(
    "subject",
    [
        "[GitHub] A new public key was added to owner/repo",
        "[GitHub] A fine-grained personal access token has been added to your account",
        "[GitHub] A third-party OAuth application has been added to your account",
        "[GitHub] A first-party GitHub OAuth application has been added to your account",
        "[GitHub] Please review this sign in",
        "[GitHub] Please verify your device",
        "[GitHub] Some App is requesting updated permissions",
        "[GitHub] Your two-factor authentication recovery codes were downloaded",
        "[owner/repo] Dependabot alert: prototype pollution in a dependency",
    ],
)
def test_github_security_notices_stay_visible(subject):
    r = screen(subject, frm=GH_NOREPLY)
    assert (r.status, r.category, r.priority) == ("POTENTIAL_ISSUE", "GITHUB", 4)
    assert "verify" in r.reason


def test_security_reason_header_is_exempt():
    r = screen("[owner/repo] Something happened",
               headers={**GH_AUTH, "github_reason": "security_alert"})
    assert r.status == "POTENTIAL_ISSUE"


@pytest.mark.parametrize(
    "headers",
    [
        {"dmarc": "fail"},
        {"dkim": "pass", "dkim_pass_domains": ["attacker.test"], "dmarc": "none"},
        {"dkim": "fail"},
    ],
)
def test_forged_github_sender_is_not_filed(headers):
    r = screen("[owner/repo] Run failed: CI", headers=headers)
    assert r.status != "FILED" and r.category == ""


def test_missing_auth_facts_still_match_for_stored_mail():
    assert screen("[owner/repo] Run failed: CI", headers={}).status == "FILED"


@pytest.mark.parametrize(
    "frm", [GH_SUFFIX_TRICK, NOT_GH, GH_TYPO]
)
def test_lookalike_senders_do_not_match(frm):
    assert match_category_rule(RULES, site_id="shop", from_addr=frm, subject="x") is None


def test_list_id_matches_mail_relayed_from_github():
    hit = match_category_rule(
        RULES, site_id="shop", from_addr="bot@lists.example.test", subject="x",
        headers={"list_id": "repo.owner.github.com"},
    )
    assert hit is not None


def test_human_examples_still_apply_to_rule_mail():
    content = [{"label": "CONTENT", "sender_addr": GH_NOTIFY,
                "subject_signature": ""}]
    r = screen("[owner/repo] Run failed: CI", learned_examples=content)
    assert (r.status, r.category) == ("CONTENT", "GITHUB")
    spam = [{"label": "SPAM", "sender_addr": GH_NOTIFY,
             "subject_signature": ""}]
    r = screen("[owner/repo] Run failed: CI", learned_examples=spam)
    assert (r.status, r.category) == ("SPAM", "GITHUB")
    # ...but a learned SPAM label never buries a security notice
    r = screen("[GitHub] Please review this sign in", frm=GH_NOTIFY,
               learned_examples=spam)
    assert r.status == "POTENTIAL_ISSUE"


def test_disabled_and_site_scoped_rules():
    off = [GITHUB_RULE.model_copy(update={"enabled": False})]
    assert match_category_rule(off, site_id="shop", from_addr=GH_A, subject="x") is None
    scoped = [GITHUB_RULE.model_copy(update={"sites": ["main"]})]
    assert match_category_rule(scoped, site_id="shop", from_addr=GH_A, subject="x") is None
    assert match_category_rule(scoped, site_id="main", from_addr=GH_A, subject="x")


def test_category_rule_validation():
    with pytest.raises(ValueError):
        CategoryRule(name="x", label="X", category="SPAM", from_domains=["a.test"])
    with pytest.raises(ValueError):
        CategoryRule(name="x", label="X", category="OK", exempt_subject_regex="(")
    with pytest.raises(ValueError):
        CategoryRule(name="Bad Name", label="X", category="OK")
    rule = CategoryRule(name="ci", label="CI", category="ci_mail", from_domains=["CI.Test"])
    assert rule.category == "CI_MAIL" and rule.from_domains == ["ci.test"]


def test_settings_default_and_toml_round_trip():
    settings = Settings(imap_accounts=[])
    assert [r.name for r in settings.category_rules] == ["github"]
    assert settings.category_label("github") == "GitHub"
    assert "category_rules" not in settings.to_toml()
    custom = Settings(
        imap_accounts=[],
        category_rules=[CategoryRule(name="ci", label="CI", category="CI", from_domains=["ci.test"])],
    )
    assert "category_rules" in custom.to_toml()


# --------------------------------------------------------------------------- #
# Ingest writes the status, the rule classification and the header facts
# --------------------------------------------------------------------------- #
def _ingest_listener(store, rules=RULES):
    from mailforge.mail.imap_listener import IMAPListener

    lst = object.__new__(IMAPListener)
    lst.store = store
    lst.account = SimpleNamespace(name="shop1", site_id="shop")
    lst._account_id = store.upsert_account("shop1", "imap", "h", 993, "in@shop.test", "shop")
    lst.site_terms = None
    lst.brand_terms = None
    lst.category_rules = tuple(rules)
    lst.security = SimpleNamespace(
        quarantine_enabled=False, attachment_max_bytes=1, attachment_max_count=1,
        impersonated_brands={},
    )
    lst.screening_mode = "content_only"
    lst._own_domains = ("shop.test",)
    lst.notified = []
    lst.on_new_message = lst.notified.append
    return lst


def _msg(uid, frm, subject, auth="relay.test; dkim=pass header.d=github.com; dmarc=pass header.from=github.com"):
    return SimpleNamespace(
        uid=str(uid),
        headers={"message-id": (f"<{uid}@x.test>",), "authentication-results": (auth,),
                 "x-github-reason": ("ci_activity",)},
        subject=subject, text="Build log text", html=None,
        date=datetime.now(timezone.utc), from_=frm,
        from_values=SimpleNamespace(name="GitHub"), to=("in@shop.test",), cc=(),
        attachments=[],
    )


def test_ingest_files_github_mail_and_skips_the_model(tmp_path):
    with open_store(tmp_path / "ingest.db") as store:
        lst = _ingest_listener(store)
        assert lst._ingest_one(_msg(1, GH_NOTIFY, "[o/r] Run failed: CI"), "INBOX")
        assert lst._ingest_one(
            _msg(2, GH_NOREPLY, "[GitHub] A new public key was added to o/r"), "INBOX"
        )
        rows = {
            r["uid"]: r for r in store.conn.execute(
                "SELECT m.uid,m.screening_status,m.header_meta,c.category,c.model_used,c.priority "
                "FROM messages m LEFT JOIN classifications c ON c.message_id=m.id"
            )
        }
        assert (rows[1]["screening_status"], rows[1]["category"], rows[1]["model_used"]) == (
            "FILED", "GITHUB", "rule:github"
        )
        assert '"github_reason": "ci_activity"' in rows[1]["header_meta"]
        assert (rows[2]["screening_status"], rows[2]["category"], rows[2]["priority"]) == (
            "POTENTIAL_ISSUE", "GITHUB", 4
        )
        filed_id = lst.notified[0]
        assert not store.message_needs_processing(filed_id)

        class _NoBridge:
            def is_up(self):  # pragma: no cover
                raise AssertionError("filed mail reached the model")

        from mailforge.agent.graph import AgentGraph

        assert asyncio.run(AgentGraph(store, Settings(), _NoBridge()).process_message(filed_id)) is None
        # Picker + inbox
        counts = store.message_tag_counts()
        assert counts["category:GITHUB"] == 2
        inbox = [r["id"] for r in store.list_received()]
        assert filed_id not in inbox and len(inbox) == 1  # security notice stays visible
        assert sorted(store.message_ids_for_tag("category:GITHUB")) == sorted(lst.notified)


def test_ingest_does_not_file_github_mail_failing_dkim(tmp_path):
    with open_store(tmp_path / "forged.db") as store:
        lst = _ingest_listener(store)
        lst._ingest_one(
            _msg(1, GH_NOTIFY, "[o/r] Run failed: CI",
                 auth="relay.test; dkim=fail header.d=github.com; dmarc=fail header.from=github.com"),
            "INBOX",
        )
        row = store.conn.execute(
            "SELECT m.screening_status, c.category FROM messages m "
            "LEFT JOIN classifications c ON c.message_id=m.id"
        ).fetchone()
        assert row["screening_status"] != "FILED" and row["category"] is None


def test_marking_filed_mail_content_requeues_it_for_triage(tmp_path):
    with open_store(tmp_path / "content.db") as store:
        lst = _ingest_listener(store)
        lst._ingest_one(_msg(1, GH_NOTIFY, "[o/r] Run failed: CI"), "INBOX")
        mid = lst.notified[0]
        assert mid not in store.unprocessed_message_ids()
        store.record_screening_feedback(mid, "CONTENT")
        assert mid in store.unprocessed_message_ids()


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
def test_picker_shows_the_rule_label_with_a_count():
    from mailforge.ui.pages import mail

    mail.register_rule_categories(Settings(imap_accounts=[]))
    options = mail.filter_options({"category:GITHUB": 12, "category:NOTIFY": 3})
    assert options["tag:category:GITHUB"] == "GitHub (12)"
    assert mail.rule_category_label("category:GITHUB") == "GitHub"
    assert mail.rule_category_label("category:NOTIFY") == ""
    assert mail.resolve_view("all", "category:GITHUB", {"category:GITHUB": 12})[2] == (
        "tag:category:GITHUB"
    )
