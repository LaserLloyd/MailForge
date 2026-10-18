"""Test isolation: every test runs against a throwaway application home.

``config.Settings`` binds the TOML path at import time, so the override has to
be in place before ``mailforge`` is imported — a conftest at this level is
the earliest hook pytest offers. Without it the suite would read (and the
config tests could write) the machine's real config.toml.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "MAILFORGE_HOME", tempfile.mkdtemp(prefix="mailforge-tests-")
)

# Hermetic: never let an installed ``security-ml`` extra (transformers /
# huggingface_hub) reach the Hugging Face Hub from a test. With the cache absent
# the optional model simply "is unavailable" and the documented heuristic
# fallbacks run, exactly as on the CI runner that installs ``--extra dev`` only.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import _netguard  # noqa: E402

# Refuse every outbound connect except to ephemeral loopback ports a test opens
# itself (see tests/_netguard.py). Installed before anything else can connect.
_netguard.install()

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_network_attempts():
    """Fail the test that attempted a real connection, even when the code under
    test swallowed the refusal (clients here degrade to a sentinel)."""
    _netguard.VIOLATIONS.clear()
    yield
    if _netguard.VIOLATIONS:
        found = list(_netguard.VIOLATIONS)
        _netguard.VIOLATIONS.clear()
        pytest.fail("test attempted a real network connection: " + "; ".join(found),
                    pytrace=False)


@pytest.fixture(autouse=True)
def _optional_ml_tiers_off(request, monkeypatch):
    """Run every test on the documented heuristic fallbacks, whatever extras
    happen to be installed.

    With ``--all-extras`` the ``security-ml`` tier is importable, and its first
    use downloads weights (Hugging Face) and a spaCy model (GitHub). A test must
    never do that, and its result must not depend on whether a developer's cache
    is warm. Tests that really exercise the model tier carry ``@pytest.mark.ml``
    and skip themselves unless the weights are already cached.
    """
    if request.node.get_closest_marker("ml"):
        yield
        return
    from mailforge.security import guardrails, presidio, prompt_guard

    for fn in (prompt_guard._load_model, guardrails._load_scanners, presidio._load_analyzer):
        fn.cache_clear()
    monkeypatch.setattr(prompt_guard, "_load_model", lambda: None)
    monkeypatch.setattr(guardrails, "_load_scanners", lambda kind: None)
    monkeypatch.setattr(presidio, "_load_analyzer", lambda: None)
    yield


@pytest.fixture(autouse=True)
def _registered_sites():
    """Every test runs against the two example sites, restored afterwards.

    ``register_sites`` installs a process-wide registry, so a test that
    configures its own sites would otherwise leak into the next one.
    """
    from mailforge.db import store as store_mod

    previous = store_mod.VALID_SITES
    store_mod.register_sites(["main", "shop"])
    yield
    store_mod.VALID_SITES = previous


@pytest.fixture
def assert_private_mode():
    """Assert a file is owner-only, where the platform actually has modes.

    Windows has no owner/group/other bits: ``os.chmod`` there only toggles the
    read-only attribute, so ``S_IMODE`` reports 0o666/0o444 whatever the code
    asked for. Asserting 0600 on Windows tests the OS, not MailForge — so the
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
