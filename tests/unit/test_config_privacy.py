from __future__ import annotations

from mailforge import __version__
from mailforge.config import Settings


def test_version_matches_package_metadata():
    """__init__ and pyproject must agree — compared, not hand-copied here."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == declared


def test_saved_config_is_owner_only(tmp_path, assert_private_mode):
    target = tmp_path / "config.toml"
    Settings().save(target)
    assert_private_mode(target)
