"""Test isolation: every test runs against a throwaway application home.

``config.Settings`` binds the TOML path at import time, so the override has to
be in place before ``siftforge`` is imported — a conftest at this level is
the earliest hook pytest offers. Without it the suite would read (and the
config tests could write) the machine's real config.toml.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "SIFTFORGE_HOME", tempfile.mkdtemp(prefix="siftforge-tests-")
)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _registered_sites():
    """Every test runs against the two example sites, restored afterwards.

    ``register_sites`` installs a process-wide registry, so a test that
    configures its own sites would otherwise leak into the next one.
    """
    from siftforge.db import store as store_mod

    previous = store_mod.VALID_SITES
    store_mod.register_sites(["main", "shop"])
    yield
    store_mod.VALID_SITES = previous


@pytest.fixture
def assert_private_mode():
    """Assert a file is owner-only, where the platform actually has modes.

    Windows has no owner/group/other bits: ``os.chmod`` there only toggles the
    read-only attribute, so ``S_IMODE`` reports 0o666/0o444 whatever the code
    asked for. Asserting 0600 on Windows tests the OS, not SiftForge — so the
    check degrades to "the file was created" there and stays strict on POSIX.
    """
    import os
    import stat
    import sys

    def check(path) -> None:
        if sys.platform == "win32":  # pragma: no cover - Windows
            assert os.path.exists(path), f"{path} was not created"
            return
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    return check
