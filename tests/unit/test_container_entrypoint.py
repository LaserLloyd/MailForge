"""deploy/docker-entrypoint.sh, step 4: LLM_BASE_URL -> MAILFORGE_LLM__LM_STUDIO_HOST.

The step is cut out of the real script and run with ``sh`` (skipped where no POSIX
shell is on PATH), so the test exercises the shipped code, not a copy of it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mailforge.llm.bridge import LMStudioBridge

ENTRYPOINT = Path(__file__).resolve().parents[2] / "deploy" / "docker-entrypoint.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh on PATH")


def _llm_step() -> str:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    start = text.index("# ---- 4. LLM host")
    end = text.index("# ---- 5.")
    return text[start:end]


def _host_for(base_url: str | None, preset: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "MAILFORGE_LLM__"))}
    if base_url is not None:
        env["LLM_BASE_URL"] = base_url
    if preset is not None:
        env["MAILFORGE_LLM__LM_STUDIO_HOST"] = preset
    script = "set -eu\n" + _llm_step() + 'printf "%s" "${MAILFORGE_LLM__LM_STUDIO_HOST:-}"\n'
    out = subprocess.run(
        [SH, "-c", script], env=env, capture_output=True, text=True, timeout=30, check=True
    )
    return out.stdout


@pytest.mark.parametrize(
    ("base_url", "host", "bridge_base"),
    [
        ("http://host.docker.internal:1234/v1", "host.docker.internal:1234",
         "http://host.docker.internal:1234/v1"),
        ("https://llm.example.test:8443/v1", "https://llm.example.test:8443",
         "https://llm.example.test:8443/v1"),
        ("HTTPS://llm.example.test/v1", "https://llm.example.test", "https://llm.example.test/v1"),
        ("llm.example.test:1234/v1", "llm.example.test:1234", "http://llm.example.test:1234/v1"),
    ],
)
def test_llm_base_url_keeps_the_scheme_the_bridge_needs(base_url, host, bridge_base):
    """An https LLM_BASE_URL must reach the bridge as https: the bearer key rides on
    every request, and a plain-HTTP downgrade would send it in clear."""
    got = _host_for(base_url)
    assert got == host
    assert LMStudioBridge(got, "m", "e").base_url == bridge_base


def test_an_explicit_lm_studio_host_wins_and_unset_means_untouched():
    assert _host_for("https://a.example.test/v1", preset="b.example.test:1") == "b.example.test:1"
    assert _host_for(None) == ""


def test_the_privilege_drop_needs_no_passwd_entry_for_puid():
    """compose.puid.yml keeps read_only: true, so usermod cannot add PUID to /etc/passwd;
    `setpriv --init-groups` then aborts ("uid ... not found") for every PUID but 1000.
    Verified against the built image in reviews/MailForge.opus.md."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    drop = [ln for ln in text.splitlines() if ln.strip().startswith("exec setpriv")]
    assert len(drop) == 1, drop
    assert "--clear-groups" in drop[0] and "--init-groups" not in drop[0]
