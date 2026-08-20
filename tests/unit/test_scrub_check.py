"""The publication gate must catch what it claims to catch.

``scripts/scrub_check.py`` is stdlib-only and lives outside the package, so it
is loaded here by path — exactly how ``publish_public.py`` runs it inside a
freshly produced tree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scrub_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("scrub_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scrub = _load()


def test_self_test_passes():
    assert scrub.self_test() == 0


@pytest.mark.parametrize("category, sample", scrub.SELF_TEST_CASES)
def test_each_category_is_caught(category, sample):
    assert category in {hit.category for hit in scrub.scan_text(sample)}


@pytest.mark.parametrize("sample", scrub.SELF_TEST_CLEAN)
def test_clean_samples_do_not_trip(sample):
    assert scrub.scan_text(sample) == []


def test_scan_reports_path_and_line(tmp_path):
    target = tmp_path / "leak.py"
    target.write_text("ok = 1\nhost = 'imap.dreamhost.com'\n", encoding="utf-8")  # scrub-check: allow
    hits = scrub.scan_paths([tmp_path])
    assert [(h.line_no, h.category) for h in hits] == [(2, "infra-host")]
    assert hits[0].render(tmp_path) == "leak.py:2: infra-host: imap.dreamhost.com"  # scrub-check: allow


def test_skips_vcs_venv_and_binaries(tmp_path):
    for name in (".git", ".venv", "__pycache__"):
        skipped = tmp_path / name
        skipped.mkdir()
        (skipped / "x.py").write_text(
            "host = 'imap.dreamhost.com'\n", encoding="utf-8")  # scrub-check: allow
    (tmp_path / "image.png").write_bytes(b"imap.dreamhost.com")  # scrub-check: allow
    assert scrub.scan_paths([tmp_path]) == []


def test_pragma_and_region_exempt_lines(tmp_path):
    target = tmp_path / "fixtures.py"
    target.write_text(
        "sample = 'imap.dreamhost.com'  # scrub-check: allow\n"
        "# scrub-check: begin-allow\n"
        "other = '/home/mallory/notes'\n"  # scrub-check: allow
        "# scrub-check: end-allow\n"
        "clean = 'imap.example.com'\n",
        encoding="utf-8",
    )
    assert scrub.scan_paths([tmp_path]) == []


def test_allowlisted_attribution_is_permitted():
    assert scrub.scan_text("Published by Laser Lloyd — https://www.laserlloyd.com") == []
    # ...but the same name anywhere else is still a hit.
    if scrub.load_local_rules():
        assert scrub.scan_text("author = 'Laser Lloyd'")  # scrub-check: allow


def test_copyright_notice_exception_is_scoped_to_the_licence_line():
    """MIT requires the copyright notice verbatim, and naming the holder is a
    deliberate public authorship statement. The exception must not widen past
    that one line in that one file."""
    if not scrub.load_local_rules():
        pytest.skip("no scrub-rules.local.txt on this machine (CI)")
    notice = "Copyright (c) 2026 Ada Lovelace"  # a name, whoever it is

    # exempt: the copyright line of a LICENSE
    assert scrub.scan_text(notice, Path("LICENSE"),
                           rules=(scrub.Rule(scrub.LOCAL_CATEGORY,
                                             scrub._rx(r"\blovelace\b")),)) == []
    # NOT exempt: the same line in any other file
    assert scrub.scan_text(notice, Path("README.md"),
                           rules=(scrub.Rule(scrub.LOCAL_CATEGORY,
                                             scrub._rx(r"\blovelace\b")),))
    # NOT exempt: any other line of the LICENSE
    assert scrub.scan_text("contact Ada Lovelace", Path("LICENSE"),
                           rules=(scrub.Rule(scrub.LOCAL_CATEGORY,
                                             scrub._rx(r"\blovelace\b")),))


def test_generic_rules_still_apply_to_the_licence_copyright_line():
    """A hostname, key or address on that line is still a leak: only the
    LOCAL identifier rules are relaxed, never the generic ones."""
    leaky = "Copyright (c) 2026 Ada Lovelace <ada@realdomain.co>"  # scrub-check: allow
    hits = scrub.scan_text(leaky, Path("LICENSE"))
    assert "email-address" in {h.category for h in hits}
    assert scrub.scan_text("Copyright (c) 2026 admin at 192.168.222.11",  # scrub-check: allow
                           Path("LICENSE"))


def test_main_exit_codes(tmp_path, capsys):
    clean = tmp_path / "clean.md"
    clean.write_text("Mail for site 'main' at imap.example.com\n", encoding="utf-8")
    assert scrub.main([str(clean)]) == 0

    dirty = tmp_path / "dirty.md"
    dirty.write_text("run this on 192.168.222.11\n", encoding="utf-8")  # scrub-check: allow
    assert scrub.main([str(dirty)]) == 1
    assert "NOT publishable" in capsys.readouterr().out


def test_self_test_flag():
    assert scrub.main(["--self-test"]) == 0


def test_repository_sources_are_publishable():
    """The tree this test ships in must itself pass the gate."""
    root = SCRIPT.parents[1]
    targets = [
        root / "src",
        root / "tests",
        root / "scripts",
        root / "docs",
        root / "installer",
        root / "packaging",
        root / "README.md",
        root / "pyproject.toml",
    ]
    hits = scrub.scan_paths([t for t in targets if t.exists()])
    assert [h.render(root) for h in hits] == []


# --------------------------------------------------------------------------- #
# The fork's private identifiers must NOT be in this repository.
# --------------------------------------------------------------------------- #
def test_rules_in_the_repository_name_no_private_identifier():
    """The denylist must describe shapes, never literals.

    This is the whole point of ``scripts/scrub-rules.local.txt``: a hardcoded
    identifier ships with the file that forbids it, in every revision history
    keeps. So the shipped source of the gate — and its self-test fixtures — are
    read here and checked against the fork's own private list, which is exactly
    the list a reader of the public repo must not be able to reconstruct.
    """
    local = scrub.load_local_rules()
    if not local:
        pytest.skip("no scrub-rules.local.txt on this machine (CI): nothing to compare")
    shipped = SCRIPT.read_text(encoding="utf-8")
    offenders = [
        f"{SCRIPT.name}:{line_no}: {rule.pattern.pattern}"
        for line_no, line in enumerate(shipped.splitlines(), start=1)
        if not any(allowed in line for allowed in scrub.ALLOWLIST)
        for rule in local
        if rule.pattern.search(line)
    ]
    assert offenders == [], offenders


def test_self_test_fixtures_are_invented():
    """Every sample the gate proves itself with must be made up."""
    local = scrub.load_local_rules()
    if not local:
        pytest.skip("no scrub-rules.local.txt on this machine (CI): nothing to compare")
    samples = [s for _, s in scrub.SELF_TEST_CASES] + list(scrub.SELF_TEST_CLEAN)
    offenders = [
        (sample, rule.pattern.pattern)
        for sample in samples
        if not any(allowed in sample for allowed in scrub.ALLOWLIST)
        for rule in local
        if rule.pattern.search(sample)
    ]
    assert offenders == [], offenders


def test_the_private_rules_file_is_never_scanned(tmp_path):
    """It is git-ignored, it is 100% denylist, and it must never be read as a
    finding — nor, obviously, published."""
    (tmp_path / scrub.LOCAL_RULES_PATH.name).write_text(
        r"\bwintermute\b" + "\n", encoding="utf-8")
    assert scrub.scan_paths([tmp_path]) == []


def test_local_rules_load_and_apply(tmp_path):
    probe = tmp_path / scrub.LOCAL_RULES_PATH.name
    probe.write_text("# a comment\n\n" + r"\bwintermute\b" + "\n", encoding="utf-8")
    rules = scrub.load_local_rules(probe)
    assert [r.category for r in rules] == [scrub.LOCAL_CATEGORY]
    assert scrub.scan_text("host = 'WINTERMUTE'", rules=rules)   # case-insensitive
    assert scrub.scan_text("host = 'other'", rules=rules) == []


def test_missing_local_rules_file_says_so_once(tmp_path, capsys, monkeypatch):
    """Silence here would let CI print 'clean' for a run that could not have
    checked a name. It has to be announced — and only once per process."""
    monkeypatch.setattr(scrub, "_local_noted", False)
    missing = tmp_path / "absent.txt"
    assert scrub.load_local_rules(missing) == ()
    first = capsys.readouterr().err
    assert "NOT loaded" in first and missing.name in first
    assert scrub.load_local_rules(missing) == ()
    assert capsys.readouterr().err == ""


def test_bad_regex_in_local_rules_is_fatal(tmp_path):
    bad = tmp_path / scrub.LOCAL_RULES_PATH.name
    bad.write_text("[unclosed\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        scrub.load_local_rules(bad)
    assert e.value.code == 2
