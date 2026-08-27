#!/usr/bin/env python3
"""Publication gate: scan a tree for personal, infrastructure, or secret data.

Standalone (stdlib only) so it can run inside the produced public tree with no
dependencies installed. It runs six ways:

    python3 scripts/scrub_check.py src tests docs README.md  # paths
    python3 scripts/scrub_check.py --staged             # pre-commit: the INDEX
    python3 scripts/scrub_check.py --rev SHA            # pre-push: a commit TREE
    python3 scripts/scrub_check.py --message FILE       # commit-msg: one message
    python3 scripts/scrub_check.py --commit-range A..B  # pre-push: N messages
    python3 scripts/scrub_check.py --self-test          # prove the rules

Prints ``path:line: <category>: <match>`` for every hit and exits 1 when the
tree is not publishable. ``--self-test`` proves each category is still caught.

Commit messages go through the same regexes: they are published as loudly as
the code, and a message is where a path, a hostname or an assistant session URL
gets pasted without thinking. Two copies of a denylist is one stale denylist,
so the hooks in ``scripts/hooks/`` (install with ``scripts/install-hooks.sh``)
call this file rather than reimplementing it in shell.

The denylist is deliberately broader than "things that are secret": a public
release must also not leak whose machine it was built on, which sites it was
written for, or what the author's mailboxes are called.

**The rules in this file are generic on purpose.** They describe *shapes* — an
absolute home path, a tailnet hostname, a private IP, a key, an address — never
the maintainer's actual name, machines or clients. Those literals live in
``scripts/scrub-rules.local.txt``, which is git-ignored and mode 600, because a
denylist naming what must never be published *is* a publication of exactly that
list: this file ships, so anything hardcoded here ships with it, in every
revision of it that history keeps. Forks add their own without editing this
file. CI never has that file, and this script says so out loud rather than
printing "clean" for a run that could not possibly have checked a name.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

# Directories never scanned (build noise, virtualenvs, VCS internals).
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".nicegui", "node_modules", "dist", "build", ".idea", ".vscode",
}
# Binary/large artefacts: presence is checked by the publisher's allowlist, and
# scanning them produces noise, not findings.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".gz",
    ".whl", ".exe", ".db", ".sqlite", ".sqlite3", ".pyc", ".lock",
}

#: A line ending in this marker is exempt (used by this file's own rule
#: literals and by test fixtures that must contain a denylisted sample).
PRAGMA = "scrub-check: allow"
#: Everything between these markers is exempt, for blocks of rule literals.
REGION_BEGIN = "scrub-check: begin-allow"
REGION_END = "scrub-check: end-allow"

#: Substrings that make an otherwise-matching line acceptable. Keep this list
#: as small as it can possibly be — every entry is a hole in the gate.
ALLOWLIST = (
    # The single permitted attribution, in README / CHANGELOG.
    "Published by Laser Lloyd",
    "https://www.laserlloyd.com",
    # pyproject authorship — the same attribution, package-metadata form.
    'authors = [{ name = "Laser Lloyd" }]',
)

#: The one place a personal name is published ON PURPOSE. MIT requires the
#: copyright notice to survive verbatim, and the holder of a copyright is a
#: deliberate public authorship statement — the opposite of a leak. The
#: exception is therefore as narrow as it can be made:
#:
#:   * only in a file actually named LICENSE / COPYING,
#:   * only on a line that IS the copyright notice,
#:   * and only against the fork's LOCAL identifier rules.
#:
#: Every generic rule — home paths, hosts, IPs, emails, keys, secrets — still
#: applies to that line and to the rest of the file. A hostname in a LICENSE is
#: still a leak; the author's name in the copyright line is not.
LICENSE_FILENAMES = {"LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "COPYING"}
COPYRIGHT_LINE = re.compile(r"^\s*Copyright\s*\(c\)\s*\d{4}(?:[-–]\d{4})?\s+\S", re.IGNORECASE)


def _is_copyright_notice(path: Path, line: str) -> bool:
    return path.name in LICENSE_FILENAMES and bool(COPYRIGHT_LINE.match(line))


class Rule(NamedTuple):
    category: str
    pattern: re.Pattern[str]


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# scrub-check: begin-allow  (the rule literals below are patterns, not data)
RULES: tuple[Rule, ...] = (
    # --- personal ---------------------------------------------------------
    # An absolute home path names its owner. Matched by SHAPE so no account
    # name has to be written down here: /home/<user> and the Fedora Atomic
    # /var/home/<user> both leak whose machine produced the file.
    Rule("personal-path", _rx(r"/(?:var/)?home/(?!user\b|youruser\b|<)[a-z_][a-z0-9_.-]{1,31}")),
    # --- infrastructure ----------------------------------------------------
    Rule("infra-host", _rx(r"\btailscale\b|\b[a-z0-9-]+\.ts\.net\b")),
    Rule("infra-host", _rx(r"\bdreamhost\b|\b(?:imap|smtp|pop|mail)\.dreamhost\.com\b")),
    Rule("infra-host", _rx(r"\bdh_[a-z0-9]{6,}\b")),
    Rule("infra-file", _rx(r"\bopenclaw\.json\b")),
    Rule("infra-model", _rx(r"\bNSFW\b")),
    Rule("private-ip", _rx(r"\b(?:192\.168|10\.\d{1,3}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}\b")),
    # --- contact details ---------------------------------------------------
    # Any email address outside the reserved documentation namespaces:
    # example.com/.net/.org and the reserved TLDs .example/.test/.invalid/
    # .localhost (RFC 2606 / RFC 6761), plus placeholders like you@your-domain.
    Rule(
        "email-address",
        re.compile(
            r"\b[\w.+-]+@(?!your-)"
            r"(?![\w.-]*\bexample\.(?:com|net|org)\b)"
            r"(?![\w.-]*\.(?:example|test|invalid|localhost)\b)"
            r"[\w-]+\.[a-z]{2,}\b",
            re.IGNORECASE,
        ),
    ),
    # --- assistant transcripts ---------------------------------------------
    # A session URL links a private transcript of the machine this was built
    # on; the trailers announce the assistant in a public log. Both belong to
    # the commit MESSAGE far more often than to a file, which is why the
    # commit-msg hook runs these same rules.
    Rule("assistant-session", _rx(r"claude\.ai/code/session|\bClaude-Session\s*:")),
    Rule("assistant-trailer", _rx(r"^\s*Co-Authored-By:\s*(?:Claude|Fable)\b")),
    # --- secrets -----------------------------------------------------------
    Rule("secret-token", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")),
    Rule("secret-token", re.compile(r"\bAKIA[0-9A-Z]{12,}\b")),
    Rule("secret-token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    Rule("secret-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    Rule("secret-token", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    Rule(
        "secret-assignment",
        _rx(r"""(?:api_?key|password|passwd|secret|token)\s*[:=]\s*["']([^"'\s]{8,})["']"""),
    ),
    Rule("secret-blob", re.compile(r"\b[0-9a-f]{40,}\b")),
    Rule("secret-blob", re.compile(r"\b[A-Za-z0-9+/]{60,}={0,2}\b")),
)

# scrub-check: end-allow

#: The git-ignored file holding this fork's private identifiers.
LOCAL_RULES_PATH = Path(__file__).resolve().parent / "scrub-rules.local.txt"

#: Category reported for a hit on one of those identifiers. Deliberately says
#: nothing about WHICH one matched beyond the matched text itself.
LOCAL_CATEGORY = "local-identifier"

_local_rules: tuple[Rule, ...] | None = None
_local_noted = False


def load_local_rules(path: Path | None = None) -> tuple[Rule, ...]:
    """This fork's private identifiers, one case-insensitive regex per line.

    Absent is not an error — a contributor's clone and CI both run without it.
    But it is announced, once per process, on stderr: a gate that prints
    "clean" after checking only the generic shapes has overstated what it
    verified, and an overstated gate is worse than none because it is trusted.
    """
    global _local_noted
    target = LOCAL_RULES_PATH if path is None else path
    if not target.exists():
        if not _local_noted:
            _local_noted = True
            print(
                f"scrub_check: NOTE - {target.name} not found, so this fork's "
                "private identifiers are NOT loaded.\n"
                "            The generic rules (home paths, hosts, IPs, keys, "
                "emails, secrets) still ran; a name\n"
                "            or a machine name cannot be caught by this run.",
                file=sys.stderr,
            )
        return ()
    rules: list[Rule] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rules.append(Rule(LOCAL_CATEGORY, re.compile(line, re.IGNORECASE)))
        except re.error as exc:
            print(f"scrub_check: bad regex in {target.name}: {line!r} ({exc})",
                  file=sys.stderr)
            sys.exit(2)
    return tuple(rules)


def active_rules() -> tuple[Rule, ...]:
    """The generic rules plus this fork's, loaded once per process."""
    global _local_rules
    if _local_rules is None:
        _local_rules = load_local_rules()
    return RULES + _local_rules

#: Lines that look like a secret assignment but are structurally safe: an empty
#: value, an env-var reference, or an obvious placeholder.
_SAFE_VALUE = re.compile(
    r"""(?:api_?key|password|passwd|secret|token)\s*[:=]\s*(?:["']{2}|["']?(?:\$\{?\w+|<[^>]+>|your[-_ ]|xxx|changeme|placeholder|none|null|true|false)\b)""",
    re.IGNORECASE,
)


class Hit(NamedTuple):
    path: Path
    line_no: int
    category: str
    match: str

    def render(self, root: Path) -> str:
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return f"{shown}:{self.line_no}: {self.category}: {self.match}"


def _is_local_rules(path: Path) -> bool:
    """The private identifier list is git-ignored; scanning it is all noise."""
    return path.name == LOCAL_RULES_PATH.name


def iter_files(targets: Iterable[Path]) -> Iterator[Path]:
    """Yield every scannable file under ``targets`` (files pass through)."""
    for target in targets:
        if target.is_file():
            if target.suffix.lower() not in SKIP_SUFFIXES and not _is_local_rules(target):
                yield target
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            if _is_local_rules(path):
                continue  # the private list is never published, and is all hits
            yield path


def scan_text(text: str, path: Path = Path("<text>"),
              rules: tuple[Rule, ...] | None = None) -> list[Hit]:
    """Every denylist hit in ``text``, honouring the allowlist."""
    if rules is None:
        rules = active_rules()
    hits: list[Hit] = []
    exempt_region = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        if REGION_BEGIN in line:
            exempt_region = True
            continue
        if REGION_END in line:
            exempt_region = False
            continue
        if exempt_region or PRAGMA in line:
            continue
        if any(allowed in line for allowed in ALLOWLIST):
            continue
        licence_notice = _is_copyright_notice(path, line)
        for rule in rules:
            match = rule.pattern.search(line)
            if not match:
                continue
            if rule.category == LOCAL_CATEGORY and licence_notice:
                continue  # a copyright holder is published on purpose
            if rule.category == "secret-assignment" and _SAFE_VALUE.search(line):
                continue
            hits.append(Hit(path, line_no, rule.category, match.group(0)[:80]))
    return hits


def scan_paths(targets: Iterable[Path]) -> list[Hit]:
    hits: list[Hit] = []
    for path in iter_files(targets):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: not publishable text
        hits.extend(scan_text(text, path))
    return hits


# --------------------------------------------------------------------------- #
# Git modes. A publish exposes three different things — the working tree, the
# tree each commit carries, and each commit MESSAGE — so the hooks scan all
# three through the rules above rather than growing a second denylist in shell.
# --------------------------------------------------------------------------- #
def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _scan_blob(path_str: str, spec: str) -> list[Hit]:
    """Scan one git object (``<rev>:<path>`` or ``:<path>`` for the index)."""
    path = Path(path_str)
    if path.suffix.lower() in SKIP_SUFFIXES or any(p in SKIP_DIRS for p in path.parts):
        return []
    raw = subprocess.run(["git", "cat-file", "blob", spec], capture_output=True).stdout
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []  # binary: not publishable text
    return scan_text(text, path)


def scan_staged() -> list[Hit]:
    """The INDEX, not the working tree — staging a secret then tidying the
    working copy must not slip past the gate."""
    hits: list[Hit] = []
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    for name in filter(None, names.split("\0")):
        hits.extend(_scan_blob(name, f":{name}"))
    return hits


def scan_rev(rev: str) -> list[Hit]:
    """The TREE of one commit: what actually travels on a push."""
    hits: list[Hit] = []
    listing = _git("ls-tree", "-r", "--format=%(objectname) %(path)", rev)
    for line in listing.splitlines():
        sha, _, name = line.partition(" ")
        hits.extend(_scan_blob(name, sha))
    return hits


def scan_message_text(text: str, label: str) -> list[Hit]:
    return scan_text(text, Path(label))


def scan_commit_range(range_spec: str) -> list[Hit]:
    hits: list[Hit] = []
    revs = _git("log", "--format=%H", *range_spec.split()).split()
    for rev in revs:
        message = _git("log", "-1", "--format=%B", rev)
        hits.extend(scan_message_text(message, f"{rev[:12]} (message)"))
    return hits


# --------------------------------------------------------------------------- #
# Self-test: one sample per category, plus the allowlist and safe-value paths.
# --------------------------------------------------------------------------- #
# scrub-check: begin-allow  (samples must contain what they prove is caught)
#
# EVERY host, address, account and person below is INVENTED. A self-test that
# reaches for the maintainer's real endpoint to prove the endpoint is caught
# publishes the endpoint — and keeps publishing it, in every revision of this
# file that history holds. The generic rules are shape rules, so a made-up
# sample of the right shape proves them exactly as well as a real one.
SELF_TEST_CASES: tuple[tuple[str, str], ...] = (
    ("personal-path", "home = '/home/mallory/notes'"),
    ("personal-path", "root = '/var/home/mallory/projects'"),
    ("infra-host", "host = 'imap.dreamhost.com'"),
    ("infra-host", "url = 'http://workstation-7.tailnet-fictional.ts.net:9700/mcp'"),
    ("infra-host", "cmd = 'tailscale status'"),
    ("infra-host", "user = 'dh_qz84mn'"),
    ("private-ip", "endpoint = 'http://192.168.222.11:9700'"),
    ("private-ip", "peer = '100.99.88.77'"),
    ("email-address", "contact: someone@realdomain.co"),
    ("secret-token", "OPENAI_KEY = 'sk-abcdefghijklmnopqrstuvwx'"),
    ("secret-token", "aws = 'AKIAIOSFODNN7EXAMPLE'"),
    ("secret-assignment", "api_key = 'hunter2hunter2'"),
    ("secret-blob", "digest = '" + "a1" * 24 + "'"),
    ("infra-file", "cfg = '~/openclaw.json'"),
    ("email-address", "write to sales@realcompany.co.uk"),
    ("infra-model", "model = 'some-13B-NSFW-tune'"),
    ("assistant-session", "Claude-Session: https://claude.ai/code/session_0123456789"),
    ("assistant-trailer", "Co-Authored-By: Claude Opus 5 <noreply@example.com>"),
)

SELF_TEST_CLEAN: tuple[str, ...] = (
    'host = "imap.example.com"',
    "sender = 'pat@acme.example'",
    "sender = 'sam@corp.test'",
    "contact: alex.example@example.org",
    'api_key = ""',
    'api_key = "${MAILFORGE_HEAVY_KEY}"',
    "password = None",
    "Published by Laser Lloyd — https://www.laserlloyd.com",
    "site_id = 'main'",
    "install to /home/<user>/.local/share",
    "install to /home/user/.local/share",
)

#: Proves the LOCAL rule mechanism without naming a single real identifier:
#: the file under test is written by the test itself.
LOCAL_RULES_PROBE = ("wintermute", "neuromancer-3")

# scrub-check: end-allow


def self_test() -> int:
    """Prove the generic rules against invented samples, and the local-rule
    machinery against a throwaway file it writes itself.

    The samples are checked against ``RULES`` alone, not ``active_rules()``:
    the fork's private identifiers must not be able to *mask* a missing
    generic rule, and a clean sample must stay clean on a machine that has no
    local file at all (CI) as well as on one that does.
    """
    failures: list[str] = []
    for category, sample in SELF_TEST_CASES:
        found = {hit.category for hit in scan_text(sample, rules=RULES)}
        if category not in found:
            failures.append(f"MISSED {category}: {sample!r} (found {sorted(found)})")
    for sample in SELF_TEST_CLEAN:
        hits = scan_text(sample, rules=RULES)
        if hits:
            failures.append(f"FALSE POSITIVE on {sample!r}: {[h.category for h in hits]}")

    # The local-rule path, proven with invented words in a temporary file.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / LOCAL_RULES_PATH.name
        probe.write_text(
            "# comment ignored\n\n" + "\n".join(rf"\b{w}\b" for w in LOCAL_RULES_PROBE),
            encoding="utf-8",
        )
        loaded = load_local_rules(probe)
        if len(loaded) != len(LOCAL_RULES_PROBE):
            failures.append(f"local rules: loaded {len(loaded)}, expected "
                            f"{len(LOCAL_RULES_PROBE)}")
        for word in LOCAL_RULES_PROBE:
            if not scan_text(f"host = {word!r}", rules=loaded):
                failures.append(f"local rules: {word!r} not caught")
        if scan_text("host = 'unrelated'", rules=loaded):
            failures.append("local rules: matched an unrelated word")

    for line in failures:
        print(line)
    covered = {category for category, _ in SELF_TEST_CASES}
    missing = {rule.category for rule in RULES} - covered
    if missing:
        print(f"NOTE: categories without a self-test sample: {sorted(missing)}")
    if failures:
        print(f"self-test FAILED ({len(failures)} problem(s))")
        return 1
    private = len(active_rules()) - len(RULES)
    print(f"self-test OK ({len(SELF_TEST_CASES)} denylist samples, "
          f"{len(SELF_TEST_CLEAN)} clean samples, "
          f"{len(LOCAL_RULES_PROBE)} local-rule samples; "
          f"{private} private identifier rule(s) loaded from "
          f"{LOCAL_RULES_PATH.name})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help="Files/directories to scan.")
    parser.add_argument("--self-test", action="store_true", help="Verify the rules and exit.")
    parser.add_argument("--selftest", dest="self_test", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--staged", action="store_true",
                        help="pre-commit: scan the git INDEX.")
    parser.add_argument("--rev", metavar="SHA", help="pre-push: scan one commit's TREE.")
    parser.add_argument("--message", metavar="FILE",
                        help="commit-msg: scan one commit message file.")
    parser.add_argument("--commit-range", metavar="A..B",
                        help="pre-push: scan the MESSAGES of a commit range.")
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="Base for relative output paths."
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    hits: list[Hit] = []
    selected = False
    if args.staged:
        selected = True
        hits += scan_staged()
    if args.rev:
        selected = True
        hits += scan_rev(args.rev)
    if args.message:
        selected = True
        text = Path(args.message).read_text(encoding="utf-8", errors="replace")
        # A commented-out line is not published, so it is not a leak.
        body = "\n".join(ln for ln in text.split("\n") if not ln.startswith("#"))
        hits += scan_message_text(body, "commit message")
    if args.commit_range:
        selected = True
        hits += scan_commit_range(args.commit_range)
    if args.paths:
        selected = True
        hits += scan_paths(args.paths)
    if not selected:
        parser.error("give at least one path, --staged/--rev/--message/--commit-range, "
                     "or --self-test")

    for hit in hits:
        print(hit.render(args.root))
    if hits:
        print(f"\n{len(hits)} hit(s) — NOT publishable.")
        return 1
    print("scrub check clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
