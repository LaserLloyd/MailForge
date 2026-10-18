"""Explain a configured model id that the server does not serve.

The shipped defaults are LM Studio catalogue ids (``vendor/model``). A
StudioForge gateway names its models ``publisher/repo/stem`` (for example
``unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S``) and answers any other id with
HTTP 404. That used to surface only as a debug-level log line and a silently
deferred draft; these helpers turn it into one plain sentence that the Models
page, ``mailforge llm-check`` and the ``bridge status`` op can show.

Pure functions: no I/O, no behaviour change beyond the message.
"""

from __future__ import annotations


def is_studioforge_id(model_id: str) -> bool:
    """True for a ``publisher/repo/stem`` id (three or more non-empty segments)."""
    parts = str(model_id or "").split("/")
    return len(parts) >= 3 and all(parts)


def _stem(model_id: str) -> str:
    return str(model_id or "").rsplit("/", 1)[-1].lower()


def nearest_served(configured: str, served: list[str], limit: int = 3) -> list[str]:
    """Served ids that look like what the user meant (same stem or substring)."""
    want = _stem(configured)
    if not want:
        return []
    hits = [s for s in served if want in s.lower() or _stem(s) in want]
    return hits[:limit]


def describe_mismatch(configured: str, served: list[str], role: str = "chat") -> str | None:
    """One sentence explaining why ``configured`` will 404, or ``None`` when it
    is served (or nothing is known about what the server serves)."""
    configured = str(configured or "").strip()
    served = [str(s) for s in served if s]
    if not configured or not served:
        return None
    if configured in served or configured.lower() in {s.lower() for s in served}:
        return None
    msg = f"The configured {role} model '{configured}' is not served by this server."
    if any(is_studioforge_id(s) for s in served) and not is_studioforge_id(configured):
        msg += (
            " The server uses StudioForge-style ids (publisher/repo/stem); "
            "a short LM Studio id such as this one is answered with HTTP 404."
        )
    near = nearest_served(configured, served)
    if near:
        msg += " Closest served: " + ", ".join(near) + "."
    setting = "llm.chat_model" if role == "chat" else "llm.embedding_model"
    msg += f" Set {setting} in config.toml (or on the Models page) to one of the served ids."
    return msg
