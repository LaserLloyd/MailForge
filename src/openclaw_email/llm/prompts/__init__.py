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
PROMPT_VERSION = "1"


def load(name: str) -> str:
    """Return the text of prompt template ``name`` (with or without ``.txt``).

    Raises ``FileNotFoundError`` (via the resources API) for unknown names.
    """
    filename = name if name.endswith(".txt") else f"{name}.txt"
    return resources.files(__package__).joinpath(filename).read_text("utf-8")
