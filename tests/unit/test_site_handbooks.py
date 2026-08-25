"""Config-driven site handbooks: extraction, fail-closed gates, private writes.

Every fixture site is declared inline (no machine-local content, no real
business), so these tests describe the generic contract only.
"""

from __future__ import annotations

import os
import stat

import pytest

from emailforge.config import SiteConfig
from emailforge.knowledge.handbooks import (
    build_site_handbooks,
    read_site_handbook,
    sync_site_handbook_to_openclaw,
    sync_site_handbooks_to_openclaw,
    write_site_handbooks,
)
from emailforge.knowledge.sync import managed_sites

_POLICY = """# {NAME} Email Response Handbook

Site: https://example.com/
Canonical local source digest: `{source_digest}`

## Non-negotiable operating rules

- Produce a draft only. A human must review and send every email.
"""


def _policy_file(tmp_path, name: str) -> str:
    path = tmp_path / f"{name}-policy.md"
    path.write_text(_POLICY.replace("{NAME}", name.title()), encoding="utf-8")
    return str(path)


def _write_pages(root) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        """
        <html><body><nav>Ignore navigation</nav><main>
        <h1>Main Site</h1>
        <p>We answer questions about our published guides.</p>
        <ul><li>Guides are free for personal use.</li></ul>
        <script>secret_should_not_appear()</script>
        </main><footer>Ignore footer</footer></body></html>
        """,
        encoding="utf-8",
    )
    (root / "faq.html").write_text(
        """
        <html><body><main><h2>FAQ</h2>
        <p>Delivery dates are never promised by email.</p>
        <p>Internal DRAFT_ONLY_PLACEHOLDER must not leak.</p>
        <p>The approval deadline for an appeal is 30 days.</p>
        </main></body></html>
        """,
        encoding="utf-8",
    )
    (root / "notes.md").write_text(
        "# Notes\n\nMarkdown pages are supported too.\n\nInternal placeholder note.\n",
        encoding="utf-8",
    )


def _write_llms(root) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "llms.txt").write_text(
        "# Shop\n\n- [Materials](https://shop.example.net/materials/)\n", encoding="utf-8"
    )
    (root / "llms-full.txt").write_text(
        "# Full site\n\n## Materials\n\nTest on a small sample first.\n", encoding="utf-8"
    )


def _page_site(tmp_path, **overrides) -> SiteConfig:
    root = tmp_path / "main-src"
    _write_pages(root)
    defaults = dict(
        name="Main Site",
        knowledge_root=str(root),
        policy_file=_policy_file(tmp_path, "main"),
        knowledge_pages=["index.html", "faq.html", "notes.md"],
        knowledge_required_pages=["index.html", "faq.html"],
        knowledge_exclude_patterns=[
            "placeholder",
            r"\A(?=.*appeal)(?=.*(?:30 day|deadline))",
        ],
        site_url="https://example.com",
        openclaw_workspace="workspace-email-main",
    )
    defaults.update(overrides)
    return SiteConfig(**defaults)


def _llms_site(tmp_path, **overrides) -> SiteConfig:
    root = tmp_path / "shop-src"
    _write_llms(root)
    defaults = dict(
        name="Shop",
        knowledge_root=str(root),
        policy_file=_policy_file(tmp_path, "shop"),
        site_url="https://shop.example.net",
        openclaw_workspace="workspace-email-shop",
    )
    defaults.update(overrides)
    return SiteConfig(**defaults)


def test_page_mode_extracts_blocks_and_is_deterministic(tmp_path):
    site = _page_site(tmp_path)

    first = build_site_handbooks("main", site=site)
    second = build_site_handbooks("MAIN", site=site)

    assert first == second
    assert set(first) == {"handbook", "full_text"}
    assert first["handbook"].startswith("# Main Email Response Handbook")
    assert "## Source page: index.html" in first["full_text"]
    assert "Public URL: https://example.com/\n" in first["full_text"]
    assert "Public URL: https://example.com/faq/\n" in first["full_text"]
    assert "## Main Site" in first["full_text"]  # h1 demoted one level
    assert "- Guides are free for personal use." in first["full_text"]
    assert "Markdown pages are supported too." in first["full_text"]
    assert "Ignore navigation" not in first["full_text"]
    assert "Ignore footer" not in first["full_text"]
    assert "secret_should_not_appear" not in first["full_text"]


def test_exclude_patterns_drop_matching_blocks_case_insensitively(tmp_path):
    site = _page_site(tmp_path)

    documents = build_site_handbooks("main", site=site)

    assert "Delivery dates are never promised" in documents["full_text"]
    assert "PLACEHOLDER" not in documents["full_text"]
    assert "Internal placeholder note" not in documents["full_text"]  # markdown too
    assert "deadline for an appeal" not in documents["full_text"]


def test_no_exclusions_keeps_every_block(tmp_path):
    site = _page_site(tmp_path, knowledge_exclude_patterns=[])

    documents = build_site_handbooks("main", site=site)

    assert "DRAFT_ONLY_PLACEHOLDER" in documents["full_text"]
    assert "deadline for an appeal" in documents["full_text"]


def test_pages_are_auto_discovered_when_no_allowlist_is_given(tmp_path):
    site = _page_site(tmp_path, knowledge_pages=[])

    documents = build_site_handbooks("main", site=site)

    for relative in ("faq.html", "index.html", "notes.md"):
        assert f"## Source page: {relative}" in documents["full_text"]


def test_policy_header_receives_the_source_digest(tmp_path):
    site = _page_site(tmp_path)

    before = build_site_handbooks("main", site=site)
    (tmp_path / "main-src" / "index.html").write_text(
        "<html><body><main><p>Changed copy.</p></main></body></html>", encoding="utf-8"
    )
    after = build_site_handbooks("main", site=site)

    digest_line = next(
        line for line in before["handbook"].splitlines() if line.startswith("Canonical")
    )
    assert "{source_digest}" not in before["handbook"]
    assert len(digest_line.split("`")[1]) == 64
    assert before["handbook"] != after["handbook"]  # digest tracks the sources


def test_llms_mode_uses_the_generated_export(tmp_path):
    site = _llms_site(tmp_path)

    documents = build_site_handbooks("shop", site=site)

    assert "## Site index" in documents["handbook"]
    assert "[Materials](https://shop.example.net/materials/)" in documents["handbook"]
    assert "# Current Shop Site Text" in documents["full_text"]
    assert "Test on a small sample first" in documents["full_text"]
    assert "Produce a draft only" in documents["full_text"]


def test_knowledge_intro_override_is_used(tmp_path):
    site = _llms_site(tmp_path, knowledge_intro="Reference only; rules above win.")

    documents = build_site_handbooks("shop", site=site)

    assert "Reference only; rules above win." in documents["full_text"]


def test_private_atomic_write_read_and_explicit_openclaw_sync(tmp_path):
    site = _llms_site(tmp_path)
    output = tmp_path / "managed"
    workspaces = tmp_path / "agent-workspaces"

    paths = write_site_handbooks("shop", root=output, site=site)

    assert paths["handbook"].name == "shop-site-handbook.md"
    assert paths["full_text"].name == "shop-site-full-text.md"
    assert stat.S_IMODE(os.stat(paths["handbook"]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(paths["full_text"]).st_mode) == 0o600
    assert read_site_handbook("shop", root=output) == paths["handbook"].read_text()
    assert read_site_handbook("shop", root=output, full=True) == paths["full_text"].read_text()
    assert not workspaces.exists()

    target = sync_site_handbook_to_openclaw(
        "shop", handbook_root=output, workspace_root=workspaces, site=site
    )

    assert target == workspaces / "workspace-email-shop" / "SITE-HANDBOOK.md"
    assert target.read_text() == paths["handbook"].read_text()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_sync_uses_site_isolated_workspaces_and_skips_unconfigured_sites(tmp_path):
    registry = {
        "main": _page_site(tmp_path),
        "shop": _llms_site(tmp_path),
        "studio": SiteConfig(name="Studio"),  # no managed knowledge
    }
    output = tmp_path / "managed"
    workspaces = tmp_path / "agent-workspaces"
    for site_id in ("main", "shop"):
        write_site_handbooks(site_id, root=output, site=registry[site_id])

    paths = sync_site_handbooks_to_openclaw(
        handbook_root=output, workspace_root=workspaces, sites=registry
    )

    assert set(paths) == {"main", "shop"}
    assert paths["main"].parent.name == "workspace-email-main"
    assert "Main Email Response Handbook" in paths["main"].read_text()
    assert "Shop Email Response Handbook" in paths["shop"].read_text()
    assert managed_sites(registry) == ("main", "shop")


def test_site_without_workspace_is_not_copied_anywhere(tmp_path):
    site = _llms_site(tmp_path, openclaw_workspace=None)
    output = tmp_path / "managed"
    workspaces = tmp_path / "agent-workspaces"
    write_site_handbooks("shop", root=output, site=site)

    assert (
        sync_site_handbook_to_openclaw(
            "shop", handbook_root=output, workspace_root=workspaces, site=site
        )
        is None
    )
    assert not workspaces.exists()


def test_incomplete_or_unconfigured_sources_fail_closed(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ValueError, match="no knowledge_root"):
        build_site_handbooks("studio", site=SiteConfig(name="Studio"))
    with pytest.raises(FileNotFoundError, match="missing faq.html"):
        build_site_handbooks("main", site=_page_site(tmp_path), source_root=empty)
    with pytest.raises(FileNotFoundError, match="no knowledge pages found"):
        build_site_handbooks(
            "main",
            site=_page_site(tmp_path, knowledge_required_pages=[]),
            source_root=empty,
        )
    with pytest.raises(FileNotFoundError, match="no policy_file"):
        build_site_handbooks("shop", site=_llms_site(tmp_path, policy_file=None))
