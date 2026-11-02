"""Agent-writable guidance: policy, references, templates (bridge ops, WP-1).

Everything runs against a throwaway application home, a temp DB and fixture
sites; nothing touches the machine's real policies, handbooks or database.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from mailforge.bridge_ops import guidance_ops as g
from mailforge.config import Settings, SiteConfig
from mailforge.db.store import open_store, register_sites
from mailforge.knowledge import guidance

POLICY_V1 = (
    "# Main Email Response Handbook\n\n"
    "Canonical local source digest: `{source_digest}`\n\n"
    "## Rules\n\n- Produce a draft only. A human sends.\n"
)
POLICY_V2 = POLICY_V1 + "- Never promise delivery dates.\n"


class _OfflineBridge:
    async def is_up_async(self) -> bool:
        return False

    def is_up(self) -> bool:
        return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAILFORGE_HOME", str(home))
    register_sites(["main", "shop"])
    policies = home / "config" / "policies"
    policies.mkdir(parents=True)
    policy = policies / "main.md"
    policy.write_text(POLICY_V1, encoding="utf-8")
    src = tmp_path / "main-src"
    src.mkdir()
    (src / "llms.txt").write_text("# Main\n\n- [Home](https://example.test/)\n", encoding="utf-8")
    (src / "llms-full.txt").write_text("# Full\n\nWe sell guides.\n", encoding="utf-8")
    site = SiteConfig(
        name="Main", knowledge_root=str(src), policy_file=str(policy), site_url="https://example.test"
    )
    shop = SiteConfig(name="Shop")
    sites = {"main": site, "shop": shop}
    settings = Settings(sites=sites)
    monkeypatch.setattr("mailforge.knowledge.sync._sites", lambda: dict(sites))
    monkeypatch.setattr("mailforge.knowledge.handbooks._configured_sites", lambda: dict(sites))
    monkeypatch.setattr(g, "_make_bridge", lambda _settings: _OfflineBridge())
    with open_store(tmp_path / "t.db") as store:
        yield type("Env", (), {
            "store": store, "settings": settings, "policy": policy, "home": home,
            "policies": policies, "tmp": tmp_path,
        })
    register_sites(["main", "shop"])


def call(env, op, data=None, site="main"):
    return g.HANDLERS[op](dict(data or {}), site, env.store, env.settings)


def sha(text: str) -> str:
    return guidance.sha256_text(text)


def audit_rows(env, tool="guidance_write"):
    return env.store.conn.execute(
        "SELECT actor, event, detail_json FROM audit_log "
        "WHERE json_extract(detail_json,'$.tool')=? ORDER BY id", (tool,)
    ).fetchall()


# ---------------------------------------------------------------- registry
def test_registry_is_classified():
    assert set(g.HANDLERS) == set(g.CLASS)
    assert set(g.CLASS.values()) == {"read", "guidance_write"}
    assert {"guidance", "guidance-history", "templates", "template", "references", "reference"} <= {
        k for k, v in g.CLASS.items() if v == "read"
    }
    assert {"policy-save", "guidance-refresh", "guidance-revert", "template-save",
            "template-delete", "reference-save", "reference-delete",
            "reference-reindex"} <= {k for k, v in g.CLASS.items() if v == "guidance_write"}


# ------------------------------------------------------------------ policy
def test_read_policy_returns_text_and_sha(env):
    out = call(env, "guidance", {"kind": "policy"})
    assert out["text"] == POLICY_V1
    assert out["sha256"] == sha(POLICY_V1)


def test_read_handbook_after_refresh(env):
    call(env, "guidance-refresh")
    out = call(env, "guidance", {"kind": "handbook"})
    assert "Main Email Response Handbook" in out["text"]
    assert call(env, "guidance", {"kind": "full-text"})["text"]


def test_policy_save_writes_regenerates_and_reports_offline_index(env):
    out = call(env, "policy-save", {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1),
                                    "author": "ds_flash"})
    assert env.policy.read_text(encoding="utf-8") == POLICY_V2
    assert out["old_sha256"] == sha(POLICY_V1)
    assert out["new_sha256"] == sha(POLICY_V2)
    assert out["regenerated"] is True
    assert out["indexed"] is False
    assert out["reason"]
    handbook = env.home / "data" / "handbooks" / "main-site-handbook.md"
    assert "Never promise delivery dates." in handbook.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(env.policy).st_mode) in (0o600, 0o644)  # not widened


def test_policy_save_writes_a_backup_of_the_old_text(env):
    call(env, "policy-save", {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1)})
    hist = call(env, "guidance-history", {"kind": "policy"})["versions"]
    assert len(hist) == 1
    assert hist[0]["sha256"] == sha(POLICY_V1)
    files = list((env.home / "data" / "guidance-history" / "main" / "policy" / "policy").iterdir())
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == POLICY_V1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600


def test_policy_save_is_audited(env):
    call(env, "policy-save", {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1),
                              "author": "ds_flash"})
    rows = audit_rows(env)
    assert len(rows) == 1
    assert rows[0]["actor"] == "agent" and rows[0]["event"] == "tool_call"
    detail = json.loads(rows[0]["detail_json"])
    assert detail["kind"] == "policy" and detail["author"] == "ds_flash"
    assert detail["new_sha"] and detail["old_sha"] and detail["bytes"] == len(POLICY_V2.encode())


def test_sha_mismatch_is_refused_with_the_current_sha(env):
    with pytest.raises(guidance.GuidanceError) as e:
        call(env, "policy-save", {"text": POLICY_V2, "expected_sha256": sha("something else")})
    assert sha(POLICY_V1) in str(e.value)
    assert env.policy.read_text(encoding="utf-8") == POLICY_V1


def test_update_without_expected_sha_is_refused(env):
    with pytest.raises(guidance.GuidanceError, match="expected_sha256"):
        call(env, "policy-save", {"text": POLICY_V2})


def test_policy_must_keep_the_digest_token(env):
    with pytest.raises(guidance.GuidanceError, match="source_digest"):
        call(env, "policy-save", {"text": "# New\n\nno token\n", "expected_sha256": sha(POLICY_V1)})


def test_symlinked_policy_is_refused(env, tmp_path):
    target = tmp_path / "elsewhere.md"
    target.write_text(POLICY_V1, encoding="utf-8")
    link = env.policies / "link.md"
    link.symlink_to(target)
    site = SiteConfig(name="Main", policy_file=str(link), knowledge_root=str(tmp_path))
    settings = Settings(sites={"main": site})
    with pytest.raises(guidance.GuidanceError, match="symlink|outside"):
        g.HANDLERS["policy-save"](
            {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1)}, "main", env.store, settings
        )
    assert target.read_text(encoding="utf-8") == POLICY_V1


def test_policy_outside_the_policies_dir_is_refused(env, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(POLICY_V1, encoding="utf-8")
    for bad in (outside, env.policies / ".." / "config.toml"):
        site = SiteConfig(name="Main", policy_file=str(bad), knowledge_root=str(tmp_path))
        settings = Settings(sites={"main": site})
        with pytest.raises(guidance.GuidanceError, match="outside"):
            g.HANDLERS["policy-save"](
                {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1)}, "main", env.store, settings
            )


def test_symlinked_directory_escape_is_refused(env, tmp_path):
    other = tmp_path / "other-dir"
    other.mkdir()
    (other / "p.md").write_text(POLICY_V1, encoding="utf-8")
    (env.policies / "sub").symlink_to(other)
    site = SiteConfig(name="Main", policy_file=str(env.policies / "sub" / "p.md"),
                      knowledge_root=str(tmp_path))
    settings = Settings(sites={"main": site})
    with pytest.raises(guidance.GuidanceError):
        g.HANDLERS["policy-save"](
            {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1)}, "main", env.store, settings
        )


def test_no_path_comes_from_input(env, tmp_path):
    evil = tmp_path / "evil.md"
    call(env, "policy-save", {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1),
                              "path": str(evil), "policy_file": str(evil), "file": str(evil)})
    assert not evil.exists()
    assert env.policy.read_text(encoding="utf-8") == POLICY_V2


def test_site_without_policy_file_is_refused(env):
    with pytest.raises(guidance.GuidanceError, match="policy"):
        call(env, "policy-save", {"text": POLICY_V2, "expected_sha256": "x"}, site="shop")


def test_secret_in_text_is_refused_and_counted(env):
    bad = POLICY_V1 + "\naws key AKIAIOSFODNN7EXAMPLE\n"  # scrub-check: allow
    with pytest.raises(guidance.GuidanceError, match="secret"):
        call(env, "policy-save", {"text": bad, "expected_sha256": sha(POLICY_V1)})
    assert env.policy.read_text(encoding="utf-8") == POLICY_V1
    blocks = env.store.conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE event='block' "
        "AND json_extract(detail_json,'$.tool')='guidance_write'"
    ).fetchone()[0]
    assert blocks == 1
    # and the secret value never reached the audit log
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(  # scrub-check: allow
        [dict(r) for r in env.store.conn.execute("SELECT * FROM audit_log").fetchall()]
    )


@pytest.mark.parametrize("bad", ["nul \x00 byte {source_digest}", "bell \x07 {source_digest}",
                                 "esc \x1b[31m {source_digest}"])
def test_control_characters_are_refused(env, bad):
    with pytest.raises(guidance.GuidanceError, match="control"):
        call(env, "policy-save", {"text": bad, "expected_sha256": sha(POLICY_V1)})


def test_policy_cap_is_64kb(env):
    big = "{source_digest}\n" + ("a b c d e f g h\n" * 5000)
    assert len(big.encode()) > 64 * 1024
    with pytest.raises(guidance.GuidanceError, match="64"):
        call(env, "policy-save", {"text": big, "expected_sha256": sha(POLICY_V1)})


def test_text_is_nfc_normalised(env):
    decomposed = POLICY_V2 + "Café policy.\n"
    out = call(env, "policy-save", {"text": decomposed, "expected_sha256": sha(POLICY_V1)})
    assert "Café policy." in env.policy.read_text(encoding="utf-8")
    assert out["new_sha256"] == sha(env.policy.read_text(encoding="utf-8"))


def test_revert_round_trip_is_itself_versioned(env):
    call(env, "policy-save", {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1)})
    v1 = call(env, "guidance-history", {"kind": "policy"})["versions"][0]
    out = call(env, "guidance-revert", {"kind": "policy", "version": v1["version"]})
    assert env.policy.read_text(encoding="utf-8") == POLICY_V1
    assert out["new_sha256"] == sha(POLICY_V1)
    shas = [v["sha256"] for v in call(env, "guidance-history", {"kind": "policy"})["versions"]]
    assert sha(POLICY_V2) in shas and sha(POLICY_V1) in shas
    # the version content can be read back
    got = call(env, "guidance-history", {"kind": "policy", "version": v1["version"]})
    assert got["text"] == POLICY_V1


def test_revert_rejects_a_forged_version_name(env):
    for bad in ("../../etc/passwd", "x", "20261102T000000000000Z-zzzzzzzz", ""):
        with pytest.raises(guidance.GuidanceError):
            call(env, "guidance-revert", {"kind": "policy", "version": bad})


def test_history_keeps_only_the_last_50(env):
    for i in range(53):
        guidance.write_history("main", "policy", "policy", POLICY_V1 + f"- rule number {i}\n")
    assert len(call(env, "guidance-history", {"kind": "policy"})["versions"]) == 50


def test_daily_write_cap(env):
    since = datetime.now(timezone.utc).isoformat()
    from mailforge.audit.log import AuditLog
    for _ in range(g.GUIDANCE_WRITES_PER_DAY):
        AuditLog(env.store).record("agent", "tool_call", detail={"tool": "guidance_write"})
    assert env.store.count_audit_tool_calls("guidance_write", since) >= 30
    with pytest.raises(guidance.GuidanceError, match="cap"):
        call(env, "policy-save", {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1)})
    # reads are unaffected
    assert call(env, "guidance", {"kind": "policy"})["text"] == POLICY_V1


def test_guidance_changes_24h_lists_agent_edits(env):
    call(env, "policy-save", {"text": POLICY_V2, "expected_sha256": sha(POLICY_V1),
                              "author": "ds_flash"})
    changes = g.guidance_changes_24h(env.store)
    assert len(changes) == 1 and changes[0]["kind"] == "policy"
    old = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    assert g.guidance_changes_24h(env.store, since_iso=old) == []


# --------------------------------------------------------------- templates
def _tpl(**over):
    d = {"name": "Thanks", "category": "General", "subject": "Thank you",
         "body": "Hello {{first_name}}, thanks for writing.", "shortcut": "ty"}
    d.update(over)
    return d


def test_template_create_read_update_delete(env):
    out = call(env, "template-save", _tpl(author="ds_flash"))
    tid = out["id"]
    listing = call(env, "templates")["templates"]
    assert tid in [t["id"] for t in listing]
    full = call(env, "template", {"id": tid})
    assert full["body"].startswith("Hello") and full["sha256"]
    upd = call(env, "template-save", _tpl(id=tid, body="Hi {{first_name}}.",
                                          expected_sha256=full["sha256"]))
    assert upd["id"] == tid
    assert call(env, "template", {"id": tid})["body"] == "Hi {{first_name}}."
    # the replaced version is in history, and revert restores it
    ver = call(env, "guidance-history", {"kind": "template", "id": tid})["versions"][0]
    call(env, "guidance-revert", {"kind": "template", "id": tid, "version": ver["version"]})
    assert call(env, "template", {"id": tid})["body"].startswith("Hello")
    call(env, "template-delete", {"id": tid})
    assert tid not in [t["id"] for t in call(env, "templates")["templates"]]
    assert len(audit_rows(env)) == 4  # create, update, revert, delete


def test_template_update_needs_matching_sha(env):
    tid = call(env, "template-save", _tpl())["id"]
    with pytest.raises(guidance.GuidanceError, match="expected_sha256"):
        call(env, "template-save", _tpl(id=tid, body="x"))
    with pytest.raises(guidance.GuidanceError, match="changed"):
        call(env, "template-save", _tpl(id=tid, body="x", expected_sha256="0" * 64))


def test_template_caps_secrets_and_placeholders(env):
    with pytest.raises(guidance.GuidanceError, match="20"):
        call(env, "template-save", _tpl(body="a" * 20001))
    with pytest.raises(guidance.GuidanceError, match="200"):
        call(env, "template-save", _tpl(subject="s" * 201))
    with pytest.raises(guidance.GuidanceError, match="secret"):
        call(env, "template-save", _tpl(body="key AKIAIOSFODNN7EXAMPLE"))  # scrub-check: allow
    with pytest.raises(ValueError, match="placeholder"):
        call(env, "template-save", _tpl(body="Hello {{not_allowed}}"))


def test_system_template_cannot_be_deleted(env):
    env.store.conn.execute(
        "INSERT INTO response_templates(site_id,name,category,subject,body,shortcut,is_system,"
        "created_at,updated_at) VALUES('main','Sys','General','s','b','',1,'t','t')"
    )
    env.store.conn.commit()
    tid = env.store.conn.execute("SELECT id FROM response_templates WHERE name='Sys'").fetchone()[0]
    with pytest.raises(guidance.GuidanceError, match="system"):
        call(env, "template-delete", {"id": tid})
    assert env.store.get_response_template(tid) is not None


def test_template_is_site_bound(env):
    tid = call(env, "template-save", _tpl())["id"]
    with pytest.raises(guidance.GuidanceError, match="not found"):
        call(env, "template", {"id": tid}, site="shop")
    with pytest.raises(guidance.GuidanceError, match="not found"):
        call(env, "template-delete", {"id": tid}, site="shop")


# -------------------------------------------------------------- references
def test_reference_create_read_update_delete(env):
    out = call(env, "reference-save", {"filename": "faq.md", "text": "# FAQ\n\nWe ship worldwide.\n",
                                       "author": "ds_flash"})
    rid = out["id"]
    assert out["indexed"] is False and out["reason"]
    row = env.store.get_reference_document(rid)
    path = row["stored_path"]
    assert path.startswith(str(env.home / "data" / "references" / "main"))
    assert os.path.basename(path).endswith("-faq.md")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    full = call(env, "reference", {"id": rid})
    assert "worldwide" in full["text"] and full["sha256"]
    listing = call(env, "references")["references"]
    assert listing[0]["id"] == rid and listing[0]["managed"] is False
    call(env, "reference-save", {"id": rid, "filename": "faq.md", "text": "# FAQ\n\nUpdated.\n",
                                 "expected_sha256": full["sha256"]})
    assert "Updated" in open(path, encoding="utf-8").read()
    hist = call(env, "guidance-history", {"kind": "reference", "id": rid})["versions"]
    assert len(hist) == 1
    call(env, "reference-delete", {"id": rid})
    assert env.store.get_reference_document(rid) is None
    # the text of the deleted document survives in history
    assert call(env, "guidance-history", {"kind": "reference", "id": rid})["versions"]
    assert len(audit_rows(env)) == 3


def test_reference_filename_is_sanitised_and_suffix_checked(env):
    out = call(env, "reference-save", {"filename": "../../etc/evil.md", "text": "hello there\n"})
    path = env.store.get_reference_document(out["id"])["stored_path"]
    assert path.startswith(str(env.home / "data" / "references" / "main"))
    assert ".." not in os.path.relpath(path, env.home)
    for bad in ("x.pdf", "x.docx", "x", "x.md.exe"):
        with pytest.raises(guidance.GuidanceError, match="\\.md"):
            call(env, "reference-save", {"filename": bad, "text": "hello\n"})


def test_reference_caps_secrets_and_sha(env):
    with pytest.raises(guidance.GuidanceError, match="200"):
        call(env, "reference-save", {"filename": "big.md", "text": "word " * 41000})
    with pytest.raises(guidance.GuidanceError, match="secret"):
        call(env, "reference-save", {"filename": "k.md",
                                     "text": "token ghp_aB3dE5gHjKmNpQsTvWyZbCdEfGhJkLmNpQrS"})  # scrub-check: allow
    rid = call(env, "reference-save", {"filename": "a.md", "text": "one\n"})["id"]
    with pytest.raises(guidance.GuidanceError, match="expected_sha256"):
        call(env, "reference-save", {"id": rid, "filename": "a.md", "text": "two\n"})
    with pytest.raises(guidance.GuidanceError, match="changed"):
        call(env, "reference-save", {"id": rid, "filename": "a.md", "text": "two\n",
                                     "expected_sha256": "1" * 64})


def test_managed_reference_cannot_be_deleted_or_overwritten(env):
    call(env, "guidance-refresh")
    managed = [r for r in call(env, "references")["references"] if r["managed"]]
    assert managed
    mid = managed[0]["id"]
    with pytest.raises(guidance.GuidanceError, match="managed"):
        call(env, "reference-delete", {"id": mid})
    with pytest.raises(guidance.GuidanceError, match="managed"):
        call(env, "reference-save", {"id": mid, "filename": "x.md", "text": "hello\n",
                                     "expected_sha256": managed[0]["sha256"]})
    assert env.store.get_reference_document(mid) is not None
    # reading a managed handbook is fine
    assert call(env, "reference", {"id": mid})["text"]


def test_reference_is_site_bound(env):
    rid = call(env, "reference-save", {"filename": "a.md", "text": "one\n"})["id"]
    for op in ("reference", "reference-delete", "reference-reindex"):
        with pytest.raises(guidance.GuidanceError, match="not found"):
            call(env, op, {"id": rid}, site="shop")


def test_reference_reindex_reports_offline(env):
    rid = call(env, "reference-save", {"filename": "a.md", "text": "one\n"})["id"]
    out = call(env, "reference-reindex", {"id": rid})
    assert out["indexed"] is False and out["reason"]


def test_refresh_returns_the_regeneration_report(env):
    out = call(env, "guidance-refresh")
    assert out["regenerated"] is True and out["indexed"] is False
    assert out["documents"]
    assert any(r["detail"] for r in [{"detail": r["detail_json"]} for r in audit_rows(env)])


def test_reference_text_endpoint_truncates(env):
    rid = call(env, "reference-save", {"filename": "a.md", "text": "word " * 5000})["id"]
    out = call(env, "reference", {"id": rid, "max_chars": 1000})
    assert out["truncated"] is True and len(out["text"]) <= 1000
