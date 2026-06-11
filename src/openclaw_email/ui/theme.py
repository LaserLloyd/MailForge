"""Shared UI theme + app shell — "Dispatch" design language.

Adopts the palette/layout of the local chat app: deep
tech-dark backgrounds, a single violet accent (#7c3aed), Inter/JetBrains-Mono
font stacks (system fallbacks only — the UI must work fully offline), 12px
radii, a narrow 76px icon sidebar, and flat cards with 1px borders.

Pages wrap their content in :func:`shell`:

    with theme.shell("inbox", title="Inbox", bridge=bridge):
        ...page content...

which applies the palette, builds the icon sidebar + header (with a live
LM Studio health dot when a bridge is provided), and yields a centered
content column. Security shell (auth gating, Host guard) stays in app.py —
this module is presentation only.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from nicegui import background_tasks, ui

log = logging.getLogger(__name__)

# (key, label, material icon, route) — order = sidebar order.
NAV: list[tuple[str, str, str, str]] = [
    ("inbox", "Inbox", "inbox", "/"),
    ("activity", "Activity", "history", "/activity"),
    ("models", "Models", "memory", "/models"),
    ("settings", "Settings", "settings", "/settings"),
]

ACCENT = "#7c3aed"

_CSS = """
:root {
  --bg-primary: #0f0f1a;
  --bg-secondary: #16162a;
  --bg-tertiary: #1e1e3a;
  --bg-hover: #252545;
  --bg-input: #1a1a30;

  --text-primary: #e8e8f0;
  --text-secondary: #8888aa;
  --text-muted: #555577;

  --accent: #7c3aed;
  --accent-hover: #8b5cf6;
  --accent-muted: rgba(124, 58, 237, 0.15);

  --border: #2a2a45;
  --border-light: #333355;

  --user-bubble: rgba(124, 58, 237, 0.22);
  --bot-bubble: #1e1e3a;

  --success: #34d399;
  --error: #f87171;
  --warning: #fbbf24;

  --radius-sm: 6px;
  --radius-md: 12px;
  --radius-lg: 16px;

  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
}

body, .q-page, .nicegui-content {
  background: var(--bg-primary) !important;
  color: var(--text-primary);
  font-family: var(--font-sans);
}
.nicegui-content { padding: 18px 22px; }

/* scrollbars */
::-webkit-scrollbar { width: 7px; height: 7px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-light); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* header + sidebar chrome */
.q-header.oce-header {
  background: var(--bg-secondary) !important;
  border-bottom: 1px solid var(--border);
}
.q-drawer {
  background: var(--bg-secondary) !important;
  border-right: 1px solid var(--border);
}
.oce-nav-btn {
  width: 48px; height: 48px;
  border-radius: var(--radius-md);
  border: 1px solid transparent;
  color: var(--text-secondary);
  transition: all 0.12s ease;
}
.oce-nav-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.oce-nav-btn--active {
  background: var(--accent-muted);
  border-color: var(--accent);
  color: var(--accent-hover);
  box-shadow: 0 0 10px rgba(124, 58, 237, 0.35);
}
.oce-logo {
  width: 44px; height: 44px;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--accent) 0%, #4c1d95 100%);
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 19px; color: white;
  user-select: none;
}

/* cards */
.oce-card {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: none;
  color: var(--text-primary);
}
.oce-card .q-card__section { padding: 14px 16px; }

/* list rows (inbox) */
.oce-row {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease;
}
.oce-row:hover { background: var(--bg-hover); border-color: var(--border-light); }

/* chat-style bubbles (detail page) */
.oce-bubble {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 12px 15px;
  max-width: 100%;
}
.oce-bubble--bot { background: var(--bot-bubble); border-bottom-left-radius: 4px; }
.oce-bubble--user {
  background: var(--user-bubble);
  border-color: rgba(124, 58, 237, 0.45);
  border-bottom-right-radius: 4px;
}

/* badges */
.oce-badge {
  display: inline-flex; align-items: center;
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 11.5px; font-weight: 600;
  letter-spacing: 0.02em;
  border: 1px solid var(--border-light);
  background: var(--bg-hover);
  color: var(--text-secondary);
  white-space: nowrap;
}
.oce-badge--accent  { background: var(--accent-muted); border-color: var(--accent); color: var(--accent-hover); }
.oce-badge--success { background: rgba(52,211,153,0.12); border-color: var(--success); color: var(--success); }
.oce-badge--error   { background: rgba(248,113,113,0.12); border-color: var(--error); color: var(--error); }
.oce-badge--warning { background: rgba(251,191,36,0.12);  border-color: var(--warning); color: var(--warning); }

/* stat chips */
.oce-stat {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 10px 18px;
  min-width: 110px;
}
.oce-stat .oce-stat-n { font-size: 22px; font-weight: 700; line-height: 1.1; }
.oce-stat .oce-stat-l { font-size: 11.5px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.06em; }

/* health dot */
.oce-dot { width: 11px; height: 11px; border-radius: 50%; background: var(--text-muted); }
.oce-dot--up   { background: var(--success); box-shadow: 0 0 7px rgba(52,211,153,0.7); }
.oce-dot--down { background: var(--error);   box-shadow: 0 0 7px rgba(248,113,113,0.7); }

/* quasar component re-skin */
.q-field--outlined .q-field__control {
  background: var(--bg-input);
  border-radius: var(--radius-md);
}
.q-field--outlined .q-field__control:before { border-color: var(--border-light); }
.q-textarea .q-field__native, .q-field__native, .q-field__input { color: var(--text-primary); }
.q-table { background: var(--bg-tertiary); border-radius: var(--radius-md); }
.q-table th { color: var(--text-secondary); }
.q-tab { text-transform: none; font-weight: 600; }
.q-btn { text-transform: none; border-radius: var(--radius-md); font-weight: 600; }
.q-expansion-item, .q-expansion-item__container { border-radius: var(--radius-md); }
code, pre, .q-code, .oce-mono { font-family: var(--font-mono); }
.nicegui-markdown pre, .nicegui-code, .q-card pre {
  background: #0d1117 !important;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
a { color: var(--accent-hover); }
"""


def apply() -> None:
    """Apply palette + dark mode + Quasar brand colors to the current page."""
    ui.dark_mode().enable()
    ui.colors(
        primary=ACCENT,
        secondary="#1e1e3a",
        accent="#8b5cf6",
        dark="#16162a",
        positive="#34d399",
        negative="#f87171",
        warning="#fbbf24",
        info="#8888aa",
    )
    ui.add_head_html(f"<style>{_CSS}</style>")


def badge(text: str, kind: str = "") -> ui.html:
    """A small pill badge. kind in {'', 'accent', 'success', 'error', 'warning'}."""
    cls = f"oce-badge oce-badge--{kind}" if kind else "oce-badge"
    from html import escape

    return ui.html(f'<span class="{cls}">{escape(str(text))}</span>')


def state_badge(state: str) -> ui.html:
    """Color-coded draft-state pill."""
    kinds = {
        "PENDING": "accent",
        "APPROVED": "success",
        "SENT": "success",
        "REJECTED": "error",
        "BLOCKED": "error",
        "DEFERRED_NO_LLM": "warning",
        "DRAFT": "",
    }
    label = "DEFERRED" if state == "DEFERRED_NO_LLM" else (state or "?")
    return badge(label, kinds.get(state or "", ""))


def risk_badge(risk: Any) -> ui.html | None:
    """Injection-risk pill: green <0.5, amber <0.85, red above. None hides it."""
    if risk is None or risk == "":
        return None
    try:
        v = float(risk)
    except (TypeError, ValueError):
        return None
    kind = "success" if v < 0.5 else ("warning" if v < 0.85 else "error")
    return badge(f"risk {v:.2f}", kind)


def _health_dot(bridge: object | None) -> None:
    """Async LM Studio health dot — renders grey, probes off the event loop."""
    dot = ui.element("div").classes("oce-dot")
    with dot:
        tip = ui.tooltip("checking LM Studio…")
    if bridge is None:
        tip.set_text("no LM Studio bridge configured")
        return

    async def _probe() -> None:
        import asyncio

        try:
            up = await asyncio.to_thread(lambda: bool(bridge.is_up()))  # type: ignore[attr-defined]
        except Exception as e:
            log.debug("health probe failed: %s", e)
            up = False
        try:
            dot.classes(add="oce-dot--up" if up else "oce-dot--down")
            tip.set_text("LM Studio: online" if up else "LM Studio: OFFLINE — drafts deferred")
        except Exception:
            pass  # page torn down while the probe was in flight

    # A plain background task (not ui.timer): a timer parented to this slot
    # raises "parent slot has been deleted" if the user navigates away before
    # it fires — the task only touches elements inside the guarded block.
    background_tasks.create(_probe(), name="lmstudio-health-probe")


@contextmanager
def shell(active: str, title: str, bridge: object | None = None) -> Iterator[None]:
    """Page chrome: palette + icon sidebar + header; yields the content column."""
    apply()

    with ui.left_drawer(value=True, fixed=True).props(":breakpoint=0 :width=76"):
        with ui.column().classes("items-center w-full q-pt-sm").style("gap: 14px"):
            with ui.element("div").classes("oce-logo"):
                ui.html("&#9993;")  # envelope glyph
            ui.element("div").style(
                "height:1px;width:44px;background:var(--border-light)"
            )
            for key, label, icon, route in NAV:
                btn = (
                    ui.button(icon=icon, on_click=lambda r=route: ui.navigate.to(r))
                    .props("flat round dense")
                    .classes("oce-nav-btn")
                )
                if key == active:
                    btn.classes(add="oce-nav-btn--active")
                with btn:
                    ui.tooltip(label)

    with ui.header(elevated=False).classes("oce-header items-center q-px-md q-py-sm"):
        ui.label(title).classes("text-h6").style("font-weight:700")
        ui.space()
        ui.label("human approval — nothing sends without you").classes(
            "text-caption"
        ).style("color: var(--text-secondary)")
        _health_dot(bridge)

    with ui.column().classes("w-full mx-auto").style("max-width: 1080px; gap: 14px"):
        yield
