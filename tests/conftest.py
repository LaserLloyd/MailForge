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
