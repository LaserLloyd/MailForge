"""Offline red-team CI runner (build spec §3 phase 10, §10).

Shells out to ``NVIDIA/garak`` and ``promptfoo redteam`` against the local
agent endpoint. Both tools are **OPTIONAL** and not installed by default
(``[redteam]`` extra). When a tool is absent we print clear install guidance
and SKIP that suite rather than crashing — so ``siftforge redteam`` is
always safe to invoke, and CI degrades visibly instead of failing opaquely.

Configs live in ``tests/redteam/`` (``garak.yaml``, ``promptfoo.yaml``).

Return code: the worst (max) of the suites that actually ran. If nothing could
run, returns 0 but prints a loud SKIPPED banner so it is obvious in CI logs.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# garak probes mandated by spec §10.
GARAK_PROBES = "promptinject,dan,encoding,latentinjection,leakreplay,pii"
# Local agent endpoint the suites target (UI binds 127.0.0.1, spec §0.7/§9).
LOCAL_ENDPOINT = "http://127.0.0.1:8000"


def _config_dir() -> Path:
    """Repo ``tests/redteam`` dir (configs are checked into the repo)."""
    return Path(__file__).resolve().parents[2] / "tests" / "redteam"


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _run(cmd: list[str]) -> int:
    """Run a suite; return its exit code (or 1 on a launch error)."""
    log.info("Running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, check=False)
        return proc.returncode
    except FileNotFoundError:
        log.warning("Executable vanished mid-run: %s", cmd[0])
        return 1


def run_garak() -> int | None:
    """Run garak with the spec probes. Returns exit code, or None if skipped."""
    if not _have("garak"):
        print(
            "[SKIP] garak not installed. Install with:\n"
            "  uv pip install garak    # or: pipx install garak\n"
            f"  then: garak --probes {GARAK_PROBES} ...\n"
            "garak is OPTIONAL (spec §1/§10); skipping."
        )
        return None
    cfg = _config_dir() / "garak.yaml"
    cmd = [
        "garak",
        "--config",
        str(cfg),
        "--probes",
        GARAK_PROBES,
    ]
    return _run(cmd)


def run_promptfoo() -> int | None:
    """Run promptfoo redteam. Returns exit code, or None if skipped."""
    if not _have("promptfoo"):
        print(
            "[SKIP] promptfoo not installed. Install with:\n"
            "  npm install -g promptfoo    # Node-based tool\n"
            "  then: promptfoo redteam run -c tests/redteam/promptfoo.yaml\n"
            "promptfoo is OPTIONAL (spec §1/§10); skipping."
        )
        return None
    cfg = _config_dir() / "promptfoo.yaml"
    cmd = ["promptfoo", "redteam", "run", "-c", str(cfg)]
    return _run(cmd)


def run_redteam(suite: str) -> int:
    """Run the requested red-team suite(s): ``garak`` | ``promptfoo`` | ``all``.

    Returns the worst exit code across suites that actually ran. If none ran
    (both tools absent), returns 0 with a SKIPPED banner.
    """
    suite = suite.lower().strip()
    if suite not in {"garak", "promptfoo", "all"}:
        print(f"Unknown suite '{suite}'. Choose: garak | promptfoo | all")
        return 2

    results: list[int] = []
    ran_any = False

    if suite in {"garak", "all"}:
        rc = run_garak()
        if rc is not None:
            ran_any = True
            results.append(rc)

    if suite in {"promptfoo", "all"}:
        rc = run_promptfoo()
        if rc is not None:
            ran_any = True
            results.append(rc)

    if not ran_any:
        print(
            "\n===== RED-TEAM SKIPPED =====\n"
            "No red-team tools were available to run. Install the optional\n"
            "'[redteam]' extra (garak + promptfoo) to exercise the suites.\n"
            "============================\n"
        )
        return 0

    worst = max(results)
    status = "PASS" if worst == 0 else "FAIL"
    print(f"\n===== RED-TEAM {status} (worst exit code {worst}) =====")
    return worst
