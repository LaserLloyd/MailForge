"""Every non-Python file the package ships reaches every build that ships the package.

* The frozen Windows build (packaging/pyinstaller.spec) lists package data by hand;
  ``mailforge.ui.theme`` reads ``ui/forgeui.json`` at import, so a file left out there
  is not a missing theme but a UI that cannot start.
* ``[tool.uv] cache-keys`` decides when uv rebuilds the project's own wheel (the
  Dockerfile's cached, non-editable sync); a data kind left out ships stale.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "mailforge"


def _data_files() -> list[str]:
    return sorted(
        p.relative_to(ROOT).as_posix()
        for p in PKG.rglob("*")
        if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
    )


def test_there_is_package_data_to_check() -> None:
    files = _data_files()
    assert "src/mailforge/ui/forgeui.json" in files
    assert "src/mailforge/db/schema.sql" in files


def test_the_pyinstaller_spec_bundles_every_package_data_file() -> None:
    spec = (ROOT / "packaging" / "pyinstaller.spec").read_text(encoding="utf-8")
    sources = [
        "src/mailforge/" + "/".join(re.findall(r'"([^"]+)"', m))
        for m in re.findall(r"datas \+= \[\(str\(SRC((?: / \"[^\"]+\")+)\)", spec)
    ]
    assert sources, "no SRC datas entries found in the spec"
    missing = [f for f in _data_files() if not any(f == s or f.startswith(s + "/") for s in sources)]
    assert not missing, f"add these to datas in packaging/pyinstaller.spec: {missing}"


def test_uv_cache_keys_cover_every_package_data_kind() -> None:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    globs = [k["file"] for k in cfg["tool"]["uv"]["cache-keys"] if "file" in k]
    missing = [f for f in _data_files() if not any(fnmatch.fnmatch(f, g) for g in globs)]
    assert not missing, f"add a [tool.uv] cache-keys glob for: {missing}"
