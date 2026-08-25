#!/usr/bin/env python3
"""Build the public edition: allowlisted copy → scrub gate → tests → zip.

    python3 scripts/publish_public.py                 # dry run: prints the plan
    python3 scripts/publish_public.py --go            # actually build it

Steps, in order, each of which aborts the build on failure:

1. **Copy an ALLOWLIST** of paths into ``--out`` (default
   ``~/Projects/siftforge-public``), skipping a DENY list of runtime,
   secret, and build artefacts. Nothing outside the allowlist can leak, because
   nothing outside it is ever read.
2. **Scrub gate** — run ``scrub_check.py`` over the produced tree.
3. **Tests** — ``uv run --extra dev pytest -q`` *inside* the produced tree, so
   the thing that ships is the thing that was tested.
4. **Zip** — deterministic ordering, fixed timestamps, one top-level
   ``siftforge-<version>/`` folder. Prints size + sha256.
5. **Report** — ``<out>-PUBLISH-REPORT.md`` (beside the tree) with file count, hash, results.

Every subprocess is invoked with a fixed argv list; there is no shell.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Only these paths are copied. Directories are copied recursively, subject to
#: DENY_PATTERNS below.
ALLOWLIST: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".gitignore",
    ".python-version",
    ".github",
    "src",
    "tests",
    "docs",
    "installer",
    "packaging",
    "scripts",
)

#: Names/globs never copied, wherever they appear.
DENY_PATTERNS: tuple[str, ...] = (
    # Maintainer-private publishing tooling: the scanner's denylist is a map of
    # exactly what must never ship, so neither it nor its tests nor this
    # script nor the private doc are part of the public tree.
    "scrub_check.py", "publish_public.py", "test_scrub_check.py", "PRIVATE-PUBLISHING.md",
    "PUBLISH-REPORT.md",
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",
    ".env", ".env.*", "*.bak", "*.bak-*", "*.orig", "*.rej", "*.log",
    "__pycache__", "*.pyc", "*.pyo",
    ".venv", "venv", ".nicegui", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".git", ".DS_Store", "dist", "build", "*.egg-info",
    "ui_port", "ui_launcher_key", "ui_storage_secret", "config.toml",
    "*.pem", "*.key", "*.p12", "id_rsa*", "*.whl", "*.tar.gz",
)

SCRUB_TARGETS: tuple[str, ...] = (
    "src", "tests", "docs", "scripts", "installer", "packaging",
    "README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml",
)

_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
#: Fixed timestamp so two builds of the same content produce the same zip.
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def read_version(repo: Path) -> str:
    match = _VERSION_RE.search((repo / "pyproject.toml").read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("could not read version from pyproject.toml")
    return match.group(1)


def denied(path: Path) -> bool:
    """True when any component of ``path`` matches the deny list."""
    return any(
        fnmatch.fnmatch(part, pattern)
        for part in path.parts
        for pattern in DENY_PATTERNS
    )


def plan_files(repo: Path) -> list[Path]:
    """Repo-relative paths that the public edition will contain."""
    selected: list[Path] = []
    for entry in ALLOWLIST:
        source = repo / entry
        if not source.exists():
            continue
        if source.is_file():
            if not denied(Path(entry)):
                selected.append(Path(entry))
            continue
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(repo)
            if not denied(relative):
                selected.append(relative)
    return sorted(set(selected))


def copy_tree(repo: Path, out: Path, files: list[Path]) -> None:
    if out.exists():
        shutil.rmtree(out)
    for relative in files:
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / relative, target)


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Fixed-argv subprocess; never a shell."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, cwd=str(cwd), capture_output=True, text=True, check=False
    )


def scrub(out: Path) -> subprocess.CompletedProcess[str]:
    # The scanner lives in the PRIVATE repo (it is deliberately not copied into
    # the public tree); run that copy against the produced tree.
    scanner = Path(__file__).resolve().parent / "scrub_check.py"
    targets = [t for t in SCRUB_TARGETS if (out / t).exists()]
    return run([sys.executable, str(scanner), *targets], out)


def test(out: Path) -> subprocess.CompletedProcess[str]:
    return run(["uv", "run", "--extra", "dev", "pytest", "-q"], out)


def build_zip(out: Path, files: list[Path], zip_path: Path, version: str) -> tuple[str, int]:
    top = f"siftforge-{version}"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(files):
            info = zipfile.ZipInfo(f"{top}/{relative.as_posix()}", date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (out / relative).read_bytes())
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    return digest, zip_path.stat().st_size


def write_report(
    out: Path, *, version: str, files: list[Path], zip_path: Path,
    digest: str, size: int, scrub_result: str, test_result: str,
) -> Path:
    # Written NEXT TO the tree, not inside it: it records the local build path.
    report = out.parent / f"{out.name}-PUBLISH-REPORT.md"
    report.write_text(
        "\n".join(
            [
                f"# SiftForge {version} — publish report",
                "",
                f"- Built (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                f"- Files: {len(files)}",
                f"- Archive: `{zip_path}`",
                f"- Size: {size:,} bytes",
                f"- sha256: `{digest}`",
                "",
                "## Scrub check",
                "",
                "```",
                scrub_result.strip() or "(no output)",
                "```",
                "",
                "## Tests",
                "",
                "```",
                test_result.strip() or "(no output)",
                "```",
                "",
                "## Contents",
                "",
                *(f"- `{f.as_posix()}`" for f in files),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="Source repository.")
    parser.add_argument(
        "--out", type=Path, default=Path.home() / "Projects" / "siftforge-public",
        help="Output tree (REPLACED on --go).",
    )
    parser.add_argument("--zip", dest="zip_path", type=Path, default=None, help="Archive path.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip step 3 (not for a release).")
    parser.add_argument("--go", action="store_true", help="Actually write. Without it: dry run.")
    args = parser.parse_args(argv)

    repo: Path = args.repo.resolve()
    out: Path = args.out.expanduser().resolve()
    version = read_version(repo)
    zip_path: Path = (
        args.zip_path.expanduser().resolve()
        if args.zip_path
        else out.parent / f"siftforge-{version}.zip"
    )
    files = plan_files(repo)

    print(f"siftforge {version}")
    print(f"  source : {repo}")
    print(f"  output : {out}")
    print(f"  archive: {zip_path}")
    print(f"  files  : {len(files)}")
    if not args.go:
        for relative in files[:20]:
            print(f"    {relative.as_posix()}")
        if len(files) > 20:
            print(f"    … and {len(files) - 20} more")
        print("\nDRY RUN — nothing written. Re-run with --go to build.")
        return 0

    print("\n[1/5] copying allowlisted files …")
    copy_tree(repo, out, files)

    print("[2/5] scrub check …")
    scrub_run = scrub(out)
    scrub_output = scrub_run.stdout + scrub_run.stderr
    print(scrub_output.rstrip())
    if scrub_run.returncode != 0:
        print("ABORT: the produced tree is not publishable.")
        return 1

    test_output = "skipped (--skip-tests)"
    if args.skip_tests:
        print("[3/5] tests SKIPPED")
    else:
        print("[3/5] tests in the produced tree …")
        test_run = test(out)
        test_output = (test_run.stdout + test_run.stderr).strip()
        print(test_output[-2000:])
        if test_run.returncode != 0:
            print("ABORT: tests failed in the produced tree.")
            return 1

    print("[4/5] zipping …")
    digest, size = build_zip(out, files, zip_path, version)
    print(f"  {zip_path}\n  {size:,} bytes\n  sha256 {digest}")

    print("[5/5] writing report …")
    report = write_report(
        out, version=version, files=files, zip_path=zip_path, digest=digest,
        size=size, scrub_result=scrub_output,
        test_result=test_output.splitlines()[-1] if test_output else "",
    )
    print(f"  {report}")
    print("\nPublic edition ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
