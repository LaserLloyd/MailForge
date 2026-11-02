"""Reveal a message's real links to the human, behind a warning.

Inbound links are replaced by ``[link_N]`` tokens at ingest and the agent only
ever sees tokens. Here the person can opt in, per view, to see where each one
really goes. Nothing is persisted: a reload hides them again. Anchors are built
as NiceGUI elements (props are serialised, never spliced into HTML or JS) and
only ``http``, ``https`` and ``mailto`` ever become clickable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from nicegui import ui

from . import theme

CLICKABLE_SCHEMES = frozenset({"http", "https", "mailto"})
RISKY_SCREENING = frozenset({"POTENTIAL_ISSUE", "SPAM", "POTENTIAL_SPAM"})
_TOKEN = re.compile(r"\[link_(\d+)\]")
_DOMAIN_IN_TEXT = re.compile(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", re.I)
REL = "noopener noreferrer nofollow"


@dataclass
class LinkInfo:
    symbol: str
    target: str
    display: str
    scheme: str = ""
    host: str = ""
    clickable: bool = False
    flags: list[str] = field(default_factory=list)


def _host_of(target: str) -> tuple[str, str]:
    try:
        parts = urlsplit(target.strip())
        scheme = (parts.scheme or "").lower()
        if scheme == "mailto":
            host = parts.path.rsplit("@", 1)[-1].lower() if "@" in parts.path else ""
        else:
            host = (parts.hostname or "").lower()
        return scheme, host
    except ValueError:
        return "", ""


def _is_ip(host: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host)) or ":" in host


def _same_site(a: str, b: str) -> bool:
    a, b = a.removeprefix("www."), b.removeprefix("www.")
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def analyze(symbol: str, target: str, display: str = "") -> LinkInfo:
    scheme, host = _host_of(target)
    info = LinkInfo(symbol, target, display or "", scheme, host)
    info.clickable = scheme in CLICKABLE_SCHEMES and (bool(host) or scheme == "mailto")
    if not info.clickable:
        info.flags.append("Not a web or email address, so it will not be clickable.")
        return info
    if scheme == "http":
        info.flags.append("Not encrypted (http, not https).")
    if _is_ip(host):
        info.flags.append("Goes to a bare number address instead of a name.")
    if "xn--" in host or any(ord(c) > 127 for c in host):
        info.flags.append("Look-alike address (unusual or foreign-script letters).")
    m = _DOMAIN_IN_TEXT.search(display or "")
    if m and host and not _same_site(m.group(0).lower(), host):
        info.flags.append(f"The text says {m.group(0).lower()} but the link goes to {host}.")
    return info


def load_links(store: Any, message_id: int) -> list[LinkInfo]:
    try:
        rows = store.list_links(int(message_id))
    except Exception:  # noqa: BLE001
        return []
    return [analyze(sym, tgt, disp) for sym, tgt, disp in rows]


def is_risky(screening_status: Any, quarantined: Any) -> bool:
    return bool(quarantined) or str(screening_status or "").upper() in RISKY_SCREENING


def split_body(text: str, links: list[LinkInfo]) -> list[tuple[str, Any]]:
    """Body as ``("text", str)`` / ``("link", LinkInfo)`` parts, in order."""
    by_symbol = {i.symbol.strip("[]"): i for i in links}
    parts: list[tuple[str, Any]] = []
    pos = 0
    for m in _TOKEN.finditer(text or ""):
        info = by_symbol.get(f"link_{m.group(1)}")
        if info is None:
            continue
        if m.start() > pos:
            parts.append(("text", text[pos : m.start()]))
        parts.append(("link", info))
        pos = m.end()
    if pos < len(text or ""):
        parts.append(("text", text[pos:]))
    return parts


def anchor(info: LinkInfo, text: str | None = None) -> Any:
    """A safe new-tab link for a clickable target, else a plain label."""
    shown = text if text is not None else info.target
    if not info.clickable:
        return ui.label(shown)
    link = ui.link(shown, info.target, new_tab=True)
    link._props["rel"] = REL
    link._props["referrerpolicy"] = "no-referrer"
    return link


def render_revealed(text: str, links: list[LinkInfo]) -> None:
    """Body with tokens as anchors, then the list of destinations with copy buttons."""
    with ui.element("div").classes("w-full").style(
        "white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13.5px; line-height: 1.62"
    ):
        for kind, value in split_body(text, links):
            if kind == "text":
                ui.label(value).style(
                    "display: inline; white-space: pre-wrap"
                )
            else:
                anchor(value, value.symbol).style("display: inline")
    with ui.column().classes("w-full").style("gap: 6px; margin-top: 12px"):
        ui.label("Links in this message").style("font-weight: 700")
        for info in links:
            with ui.row().classes("w-full items-center no-wrap").style("gap: 6px"):
                ui.label(info.symbol).style("font-size: 11.5px; color: var(--text-tertiary)")
                with ui.column().classes("col").style("gap: 0; min-width: 0"):
                    anchor(info).style("overflow-wrap: anywhere; font-size: 12.5px")
                    for f in info.flags:
                        ui.label(f).style("font-size: 11px; color: var(--danger-text)")
                if info.clickable:
                    copy_btn = ui.button(icon="content_copy").props("flat round dense")
                    copy_btn._props["data-copy-addr"] = info.target
                    copy_btn.on("click", js_handler=theme.COPY_ADDRESS_JS)
                    copy_btn.tooltip("Copy link")


def warning_dialog(links: list[LinkInfo], *, risky: bool, on_confirm: Callable[[], None]) -> Any:
    with ui.dialog() as dialog, ui.card().classes("bb-card").style("max-width: 640px; width: 94vw"):
        ui.label("Be careful with links").style("font-size: 17px; font-weight: 800")
        ui.label(
            "Links in email are a common way to trick people into giving away passwords or "
            "money. Look at where each one really goes before you click. When in doubt, open "
            "the company's own website yourself instead."
        )
        if risky:
            ui.label(
                "MailForge flagged this message as suspicious or unsafe. Treat every link in "
                "it as dangerous."
            ).style(
                "background: var(--danger-bg, #fdecea); color: var(--danger-text); "
                "font-weight: 800; padding: 8px 10px; border-radius: 8px"
            )
        with ui.column().classes("w-full").style("gap: 8px; max-height: 45vh; overflow: auto"):
            for info in links:
                with ui.column().style("gap: 1px"):
                    ui.label(info.host or "(no host)").style("font-weight: 800; font-size: 15px")
                    ui.label(info.target).style(
                        "font-size: 11px; color: var(--text-tertiary); overflow-wrap: anywhere"
                    )
                    if info.display:
                        ui.label(f"Text shown in the email: {info.display}").style(
                            "font-size: 11.5px; overflow-wrap: anywhere"
                        )
                    for f in info.flags:
                        ui.label(f).style(
                            "font-size: 11.5px; font-weight: 700; color: var(--danger-text)"
                        )
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button(
                "I understand, show links",
                on_click=lambda: (dialog.close(), on_confirm()),
            ).props("no-caps color=" + ("negative" if risky else "primary"))
    return dialog


def audit_revealed(store: Any, message_id: int, count: int) -> None:
    from ..audit.log import AuditLog

    AuditLog(store).record(
        "user", "links_revealed", "messages", int(message_id), {"count": int(count)}
    )


def body_with_links(
    store: Any,
    message_id: int,
    text: str,
    *,
    screening_status: Any,
    quarantined: Any,
    render_plain: Callable[[], None],
) -> None:
    """Show ``render_plain`` until the person reveals links; then anchors + list."""
    links = load_links(store, message_id)
    state = {"on": False}

    @ui.refreshable
    def _body() -> None:
        if state["on"]:
            render_revealed(text, links)
        else:
            render_plain()

    def _confirmed() -> None:
        try:
            audit_revealed(store, message_id, len(links))
        except Exception:  # noqa: BLE001
            pass
        state["on"] = True
        _body.refresh()

    if links:
        dialog = warning_dialog(
            links, risky=is_risky(screening_status, quarantined), on_confirm=_confirmed
        )
        with ui.row().classes("w-full items-center"):
            ui.button(
                f"Show links ({len(links)})", icon="link", on_click=dialog.open
            ).props("outline no-caps dense")
    _body()
