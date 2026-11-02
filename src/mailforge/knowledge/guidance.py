"""Safe read/write primitives for agent-editable guidance.

Guidance is the text that steers the drafter: a site's response policy, its
reference documents and its response templates. This module is the one place
that decides what an agent may write and how:

* **No path ever comes from input.** The policy path comes from the site's
  configured ``policy_file`` and must resolve inside the policies directory
  (``<config_dir>/policies``) without a symlink. Reference files are created
  under ``<data_dir>/references/<site>/``.
* **Optimistic concurrency.** An update needs ``expected_sha256`` of the
  current content; a mismatch is refused with the current sha, so an agent
  never clobbers an edit the owner made in the UI.
* **Content rules.** NFC, no control characters except newline and tab, no
  secrets, a per-kind size cap.
* **Versioning.** Before every overwrite or delete the old content is written
  to ``<data_dir>/guidance-history/<site>/<kind>/<key>/<UTC>-<sha8>.md``
  (0600); the last 50 are kept.

Nothing here touches the database or the network; the bridge layer
(``bridge_ops/guidance_ops.py``) adds audit, caps and indexing.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import config_dir, data_dir
from ..security import secrets_scan

POLICY_MAX_BYTES = 64 * 1024
REFERENCE_MAX_BYTES = 200 * 1024
TEMPLATE_BODY_MAX = 20_000
TEMPLATE_SUBJECT_MAX = 200
HISTORY_KEEP = 50
DIGEST_MARKER = "{source_digest}"

_VERSION_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_KINDS = frozenset({"policy", "reference", "template"})


class GuidanceError(ValueError):
    """A guidance request that is refused (maps to ``invalid_request``)."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- content
def normalise_text(text: Any, *, cap_bytes: int, label: str, single_line: bool = False) -> str:
    """Validate and canonicalise text an agent wants to store."""
    if not isinstance(text, str):
        raise GuidanceError(f"{label} must be text")
    clean = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    if any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in clean):
        raise GuidanceError(f"{label} contains control characters")
    if single_line and "\n" in clean:
        raise GuidanceError(f"{label} must be a single line")
    if len(clean.encode("utf-8")) > cap_bytes:
        raise GuidanceError(f"{label} is too long (limit {cap_bytes // 1024} KB)")
    return clean


def check_no_secrets(text: str, label: str) -> None:
    found = secrets_scan.scan(text)
    if found:
        raise GuidanceError(
            f"{label} looks like it contains a secret ({', '.join(found)}); "
            "guidance must not hold credentials"
        )


def require_current(expected: Any, current_text: str | None, label: str) -> None:
    """Refuse an update whose ``expected_sha256`` is missing or stale."""
    if current_text is None:
        return
    current = sha256_text(current_text)
    given = str(expected or "").strip().lower()
    if not given:
        raise GuidanceError(
            f"expected_sha256 is required to change an existing {label} "
            f"(current sha256: {current}); read it first"
        )
    if given != current:
        raise GuidanceError(
            f"the {label} changed since you read it (current sha256: {current}); "
            "re-read it and apply your edit to the current text"
        )


# ------------------------------------------------------------------- files
def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:  # pragma: no cover - non-POSIX
            pass
    finally:
        tmp.unlink(missing_ok=True)


def policies_dir() -> Path:
    return config_dir() / "policies"


def policy_path(site_cfg: Any, site: str) -> Path:
    """The site's policy file, proven to be a regular path inside the policies dir."""
    declared = str(getattr(site_cfg, "policy_file", "") or "").strip()
    if not declared:
        raise GuidanceError(f"site {site!r} has no policy_file configured")
    path = Path(declared).expanduser()
    root = policies_dir().resolve()
    absolute = Path(os.path.abspath(path))
    if path.is_symlink():
        raise GuidanceError("the policy file is a symlink; refusing to write through it")
    real = absolute.resolve()
    try:
        real.relative_to(root)
    except ValueError:
        raise GuidanceError(
            "the policy file is outside the policies directory; only files in "
            f"{policies_dir()} can be edited this way"
        ) from None
    return real


def read_if_exists(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


# ----------------------------------------------------------------- history
def history_dir(site: str, kind: str, key: str) -> Path:
    if kind not in _KINDS or not _KEY_RE.match(str(key)):
        raise GuidanceError("invalid guidance address")
    if not re.match(r"^[a-z0-9_-]{1,40}$", str(site)):
        raise GuidanceError("invalid site")
    return data_dir() / "guidance-history" / site / kind / str(key)


def write_history(site: str, kind: str, key: str, old_text: str) -> str:
    """Save ``old_text`` as a new version and prune to the last 50."""
    directory = history_dir(site, kind, key)
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    version = f"{now.strftime('%Y%m%dT%H%M%S')}{now.microsecond:06d}Z-{sha256_text(old_text)[:8]}"
    atomic_write(directory / f"{version}.md", old_text)
    for stale in sorted(directory.glob("*.md"))[:-HISTORY_KEEP]:
        stale.unlink(missing_ok=True)
    return version


def list_history(site: str, kind: str, key: str) -> list[dict[str, Any]]:
    directory = history_dir(site, kind, key)
    if not directory.is_dir():
        return []
    out = []
    for f in sorted(directory.glob("*.md"), reverse=True):
        if not _VERSION_RE.match(f.stem):
            continue
        text = f.read_text(encoding="utf-8")
        out.append({
            "version": f.stem,
            "saved_at": datetime.strptime(f.stem[:21], "%Y%m%dT%H%M%S%f").replace(
                tzinfo=timezone.utc).isoformat(),
            "sha256": sha256_text(text),
            "bytes": len(text.encode("utf-8")),
        })
    return out


def read_history(site: str, kind: str, key: str, version: str) -> str:
    if not _VERSION_RE.match(str(version or "")):
        raise GuidanceError("version is not a valid history version name")
    path = history_dir(site, kind, key) / f"{version}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise GuidanceError("that history version does not exist") from None


# --------------------------------------------------------------- references
REFERENCE_SUFFIXES = (".md", ".txt")


def safe_reference_name(name: Any) -> str:
    """A storage-safe basename (no path parts) that must end in .md or .txt."""
    base = Path(str(name or "").replace("\\", "/")).name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .")[:180]
    if not stem.lower().endswith(REFERENCE_SUFFIXES):
        raise GuidanceError("filename must end in .md or .txt")
    return stem


def references_root(site: str) -> Path:
    return data_dir() / "references" / site


def inside_data_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(data_dir().resolve())
    except (ValueError, OSError):
        return False
    return True
