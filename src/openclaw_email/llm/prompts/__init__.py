"""Versioned prompt templates with spotlight delimiters (build spec §4/§5).

Templates live as ``.txt`` files alongside this module and are loaded via
``importlib.resources`` so they ship inside the wheel. Naming convention:

* ``system_planner`` — planner LLM: user POLICY + symbolic refs only, NEVER
  raw email (spec §3 invariant, §5).
* ``system_worker`` — quarantined worker: untrusted content is DATA not
  instructions (spec §4 spotlighting).
* ``classify`` / ``draft`` — task prompts for the respective nodes.
"""

from __future__ import annotations

from importlib import resources

__all__ = ["load"]

# Bump when prompt wording changes in a way the audit log should track (§8).
PROMPT_VERSION = "3"


def load(name: str) -> str:
    """Return the text of prompt template ``name`` (with or without ``.txt``).

    The "OpenClaw link" prompt-update seam: if a same-named file exists in the
    user-writable prompt override directory (see
    :func:`openclaw_email.paths.prompt_override_dir`), its contents win. This
    lets OpenClaw push updated prompts without touching the installed package.
    When no override is present the packaged default ships inside the wheel, so
    the app is fully standalone. Loaded per call — overrides apply with no
    restart.

    Raises ``FileNotFoundError`` for unknown names with no override present.
    """
    filename = name if name.endswith(".txt") else f"{name}.txt"
    try:
        from ...paths import prompt_override_dir

        override = prompt_override_dir() / filename
        if override.is_file():
            return override.read_text("utf-8")
    except Exception:  # never let the override path break the packaged default
        pass
    return resources.files(__package__).joinpath(filename).read_text("utf-8")
