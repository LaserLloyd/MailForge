from __future__ import annotations

import os
import stat

from openclaw_email import __version__
from openclaw_email.config import Settings


def test_version_matches_package_metadata():
    assert __version__ == "0.3.1"


def test_saved_config_is_owner_only(tmp_path):
    target = tmp_path / "config.toml"
    Settings().save(target)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
