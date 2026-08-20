"""Shared UI theme + app shell — see docs/STYLE-GUIDE.md (the canonical guide).

Modern dark "slate + indigo" system: deep blue-slate surfaces, a single indigo
accent, Inter/JetBrains-Mono font stacks (system fallbacks only — the UI must
work fully offline), 12px radii, a labeled navigation drawer, and flat cards
with 1px borders. Change colors ONLY by editing the CSS variables below and
the matching table in docs/STYLE-GUIDE.md.

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
    ("dashboard", "Dashboard", "dashboard", "/"),
    ("mail", "Inbox", "inbox", "/mail"),
    ("drafts", "AI Review", "auto_awesome", "/drafts"),
    ("compose", "Compose", "edit", "/compose"),
    ("templates", "Templates", "content_copy", "/templates"),
    ("references", "Knowledge", "library_books", "/references"),
    ("activity", "Activity", "history", "/activity"),
    ("models", "Models", "memory", "/models"),
    ("settings", "Settings", "settings", "/settings"),
]

ACCENT = "#6366f1"

_CSS = """
:root {
  --bg-primary: #0b1121;
  --bg-secondary: #101830;
  --bg-tertiary: #16203f;
  --bg-hover: #1d2a52;
  --bg-input: #131c38;

  --text-primary: #e7ecf8;
  --text-secondary: #93a1c4;
  --text-muted: #5a6a94;

  --accent: #6366f1;
  --accent-hover: #818cf8;
  --accent-muted: rgba(99, 102, 241, 0.16);

  --border: #24304f;
  --border-light: #2f3d66;

  --user-bubble: rgba(99, 102, 241, 0.20);
  --bot-bubble: #16203f;

  --success: #34d399;
  --error: #f87171;
  --warning: #fbbf24;
  --info: #22d3ee;

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
html, body, .q-page-container, .nicegui-content { max-width: 100vw; }
.nicegui-content { padding: 18px 22px; }
.oce-menu-btn { display: none; }
.oce-header-note { white-space: nowrap; }
.oce-toolbar { gap: 12px; }
.oce-ai-drawer {
  background: var(--bg-secondary) !important;
  border-left: 1px solid var(--border);
}
.oce-chat-scroll { height: calc(100vh - 260px); overflow-y: auto; }
.oce-chat-message {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.oce-chat-message--user { background: var(--user-bubble); }
.oce-chat-message--assistant { background: var(--bot-bubble); }

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
  width: 100%; min-height: 42px;
  border-radius: var(--radius-md);
  border: 1px solid transparent;
  color: var(--text-secondary);
  transition: all 0.12s ease;
  justify-content: flex-start;
  padding: 0 12px;
}
.oce-nav-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.oce-nav-btn--active {
  background: var(--accent-muted);
  border-color: var(--accent);
  color: var(--accent-hover);
  box-shadow: 0 0 10px rgba(99, 102, 241, 0.35);
}
.oce-logo {
  width: 42px; height: 42px;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--accent) 0%, #8b5cf6 100%);
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 19px; color: white;
  user-select: none;
}
.oce-brand-name { font-weight: 800; letter-spacing: -0.02em; }
.oce-nav-btn .q-icon { margin-right: 8px; }
.oce-nav-btn .q-btn__content { width: 100%; justify-content: flex-start; }
.oce-main-column { min-width: 0; }
.oce-mail-workspace {
  display: grid;
  grid-template-columns: minmax(310px, 390px) minmax(0, 1fr);
  min-height: calc(100vh - 160px);
  overflow: hidden;
}
.oce-mail-list {
  border-right: 1px solid var(--border);
  overflow-y: auto;
  max-height: calc(100vh - 160px);
}
/* rows must scroll, never flex-compress into unreadable slivers */
.oce-mail-list > * { flex: 0 0 auto; }
.oce-mail-pane {
  overflow-y: auto;
  max-height: calc(100vh - 160px);
  padding: 16px 20px 96px; /* room for the sticky action bar */
}
.oce-row--selected { border-color: var(--accent); background: var(--accent-muted); }
.oce-reader-window {
  background: var(--bg-primary) !important;
  color: var(--text-primary);
  padding: 0 !important;
}
.oce-reader-window-header {
  position: sticky; top: 0; z-index: 2;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
}
.oce-reader-window-content { max-width: 920px; margin: 0 auto; }
.oce-markdown { overflow-wrap: anywhere; line-height: 1.62; }
.oce-markdown h1, .oce-markdown h2, .oce-markdown h3 { margin: 1em 0 .45em; }
/* email headings stay email-sized (resets removed the browser scale) */
.oce-markdown h1 { font-size: 1.45rem; font-weight: 800; }
.oce-markdown h2 { font-size: 1.25rem; font-weight: 700; }
.oce-markdown h3 { font-size: 1.1rem;  font-weight: 700; }
.oce-markdown ul { list-style: disc; padding-left: 1.5em; margin: .5em 0; }
.oce-markdown ol { list-style: decimal; padding-left: 1.5em; margin: .5em 0; }
.oce-markdown blockquote {
  border-left: 3px solid var(--border-light);
  padding-left: 12px; margin: .6em 0;
  color: var(--text-secondary);
}
.oce-markdown p { margin: .6em 0; }
.oce-markdown table { border-collapse: collapse; max-width: 100%; overflow-x: auto; }
.oce-markdown th, .oce-markdown td { border: 1px solid var(--border); padding: 7px 9px; }
.oce-sticky-actions {
  position: sticky; bottom: 8px; z-index: 2;
  background: rgba(16,24,48,.96);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 9px 12px;
}

/* cards */
.oce-card {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: none;
  color: var(--text-primary);
  min-width: 0;
}
.oce-card .q-card__section { padding: 14px 16px; }

/* list rows (inbox) */
.oce-row {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  cursor: pointer;
  overflow: hidden;
  transition: background 0.12s ease, border-color 0.12s ease;
}
.oce-row:hover { background: var(--bg-hover); border-color: var(--border-light); }
.oce-row .col, .oce-row .q-column { min-width: 0; }
.oce-row .q-label { max-width: 100%; }

/* subjects: NEVER single-line-ellipsized — wrap up to 2 lines in lists
   (full subject in a tooltip), unlimited wrap in readers. */
.oce-subject {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  overflow-wrap: anywhere;
  white-space: normal;
  line-height: 1.35;
}
.oce-subject--full { white-space: normal; overflow-wrap: anywhere; }
.oce-clip-1 {
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

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
  border-color: rgba(99, 102, 241, 0.45);
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

/* per-inbox tiles (dashboard) — header + grid of clickable metrics */
.oce-inbox-tile { flex: 1 1 320px; min-width: 0; padding: 14px; gap: 11px; }
.oce-metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(78px, 1fr));
  gap: 7px;
  width: 100%;
}
.oce-metric {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  min-width: 0;
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease;
}
.oce-metric:hover { background: var(--bg-hover); border-color: var(--accent); }
.oce-metric .oce-metric-n { font-size: 19px; font-weight: 700; line-height: 1.15; }
/* labels wrap to a second line rather than ellipsizing: "Needs r…" tells the
   reader nothing, and these six labels are the whole point of the tile. */
.oce-metric .oce-metric-l {
  font-size: 10.5px; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.05em;
  white-space: normal; overflow-wrap: break-word; line-height: 1.25;
}
/* an empty metric still navigates, but must not compete for attention */
.oce-metric--zero { opacity: 0.66; }
.oce-metric--zero .oce-metric-n { color: var(--text-muted); }

/* health dot */
.oce-dot { width: 11px; height: 11px; border-radius: 50%; background: var(--text-muted); }
.oce-dot--up   { background: var(--success); box-shadow: 0 0 7px rgba(52,211,153,0.7); }
.oce-dot--down { background: var(--error);   box-shadow: 0 0 7px rgba(248,113,113,0.7); }
.oce-dot--warn { background: var(--warning); box-shadow: 0 0 7px rgba(251,191,36,0.7); }
.oce-dot--busy { background: var(--accent-hover); animation: oce-pulse 1s ease-in-out infinite; }
@keyframes oce-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

/* dashboard bulk-action strip: quiet until something is ticked */
.oce-bulkbar { transition: border-color 0.12s ease; }
.oce-bulkbar--active { border-color: var(--accent) !important; }

/* mail-sync chip (header): dot + "checked N s ago" + refresh */
.oce-sync {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 2px 4px 2px 9px; border-radius: 999px;
  border: 1px solid var(--border-light); background: var(--bg-tertiary);
}
.oce-sync .oce-sync-text {
  font-size: 11.5px; color: var(--text-secondary); white-space: nowrap;
  max-width: 260px; overflow: hidden; text-overflow: ellipsis;
}
.oce-sync--error .oce-sync-text { color: var(--error); font-weight: 600; }
.oce-sync--warn  .oce-sync-text { color: var(--warning); font-weight: 600; }
.oce-sync--new { cursor: pointer; border-color: var(--accent); }
.oce-sync--new .oce-sync-text { color: var(--accent-hover); font-weight: 700; }
@media (max-width: 760px) { .oce-sync .oce-sync-text { display: none; } }

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

@media (max-width: 760px) {
  html, body, .q-page-container, .nicegui-content { overflow-x: hidden !important; }
  .nicegui-content { padding: 12px !important; }
  .oce-menu-btn { display: inline-flex; }
  .oce-header-note { display: none; }
  .oce-toolbar { align-items: stretch !important; flex-wrap: wrap !important; }
  .oce-toolbar .oce-toolbar-grow { width: 100% !important; max-width: none !important; }
  .oce-toolbar .oce-toolbar-grow.col { flex: 1 1 100% !important; }
  .oce-toolbar .q-tabs { width: 100% !important; max-width: 100% !important; overflow: hidden; }
  .oce-toolbar .q-tabs__content {
    justify-content: flex-start !important;
    overflow-x: auto !important;
  }
  .oce-stat { min-width: 0; flex: 1 1 120px; }
  .oce-row { padding: 11px 12px; }
  .oce-ai-drawer { width: min(94vw, 440px) !important; }
  .oce-mail-workspace { display: block; min-height: 0; }
  .oce-mail-list, .oce-mail-pane { max-height: none; border-right: 0; }
  .oce-mail-workspace.has-selection .oce-mail-list { display: none; }
  .oce-mail-workspace:not(.has-selection) .oce-mail-pane { display: none; }
}
"""


def apply() -> None:
    """Apply palette + dark mode + Quasar brand colors to the current page."""
    ui.dark_mode().enable()
    ui.colors(
        primary=ACCENT,
        secondary="#16203f",
        accent="#818cf8",
        dark="#101830",
        positive="#34d399",
        negative="#f87171",
        warning="#fbbf24",
        info="#22d3ee",
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


def quarantine_badge() -> ui.html:
    return badge("QUARANTINED", "error")


def screening_badge(status: Any) -> ui.html | None:
    value = str(status or "").upper()
    labels = {
        "CONTENT": ("CONTENT", "success"),
        "POTENTIAL_SPAM": ("POTENTIAL SPAM", "warning"),
        "POTENTIAL_ISSUE": ("POTENTIAL ISSUE", "error"),
        "SPAM": ("SPAM", "error"),
    }
    item = labels.get(value)
    return badge(*item) if item else None


def clean_subject(subject: Any) -> str:
    """Display-safe subject: unfold header line breaks, collapse whitespace."""
    s = " ".join(str(subject or "").split())
    return s or "(no subject)"


def subject_label(subject: Any, *, full: bool = False, style: str = "") -> ui.label:
    """Render an email subject that is never silently truncated.

    Lists get a 2-line clamp with the full subject in a tooltip; readers
    (``full=True``) wrap without limit. Always use this instead of a raw
    ``ui.label`` + nowrap/ellipsis for subjects (STYLE-GUIDE.md §Subjects).
    """
    text = clean_subject(subject)
    label = ui.label(text).classes("oce-subject--full" if full else "oce-subject")
    if style:
        label.style(style)
    if not full:
        with label:
            ui.tooltip(text)
    return label


def _health_dot(bridge: object | None, store: object | None = None) -> None:
    """Compact live AI/retrieval health status for the app header."""
    dot = ui.element("div").classes("oce-dot")
    with dot:
        tip = ui.tooltip("checking LM Studio…")
    retrieval = "references: vector search ready" if getattr(store, "vec_enabled", False) else (
        "references: text-only until sqlite-vec is installed"
    )
    if bridge is None:
        tip.set_text(f"AI drafting unavailable; {retrieval}")
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
            model = getattr(bridge, "model_id", "configured model")
            tip.set_text(
                f"AI drafting online ({model}); {retrieval}"
                if up else f"AI drafting OFFLINE — drafts deferred; {retrieval}"
            )
        except Exception:
            pass  # page torn down while the probe was in flight

    # A plain background task (not ui.timer): a timer parented to this slot
    # raises "parent slot has been deleted" if the user navigates away before
    # it fires — the task only touches elements inside the guarded block.
    background_tasks.create(_probe(), name="lmstudio-health-probe")


def _sync_widget(store: object | None = None) -> None:
    """Mail-sync status chip + Refresh button for the app header.

    Reads the listeners' live status from :mod:`mail.sync_state` every 2 s
    (no DB hit), so the user can always see *when* mail was last pulled and
    whether a mailbox is in trouble. Refresh pokes every listener to
    reconcile now, spins until they report back (or 25 s), then reloads the
    page if anything new arrived.
    """
    import asyncio

    from ..mail.sync_state import human_age, registry

    def _describe() -> tuple[str, str, str]:
        """(dot class, chip text, tooltip)."""
        s = registry.summary()
        accounts = s["accounts"]
        if s["state"] == "none":
            return "oce-dot--down", "No mailboxes", "No IMAP account listener is running."
        lines = []
        for a in accounts:
            if a.last_error:
                lines.append(f"{a.account}: PROBLEM — {a.last_error}")
            elif a.state in {"starting", "connecting"}:
                lines.append(f"{a.account}: connecting…")
            else:
                new = f", {a.new_total} new this session" if a.new_total else ""
                lines.append(
                    f"{a.account}: {'checking' if a.state == 'checking' else 'live'}, "
                    f"checked {human_age(a.age())}{new}"
                )
        tip = "\n".join(lines) + "\n\nMail is pulled continuously (IMAP IDLE + a check every 30 s). " \
            "Refresh forces a check now."
        if s["checking"]:
            return "oce-dot--busy", "Checking mail…", tip
        if s["state"] == "error":
            first = s["errors"][0]
            return "oce-dot--down", f"Mail sync down: {first.last_error}", tip
        if s["state"] == "degraded":
            names = ", ".join(e.account for e in s["errors"])
            return "oce-dot--warn", f"Sync problem: {names}", tip
        if s["state"] == "starting":
            return "oce-dot", "Connecting to mail…", tip
        return "oce-dot--up", f"Mail checked {human_age(s['age'])}", tip

    kind, text, tip = _describe()
    # Mail that lands while this page is open is announced on the chip (the
    # list itself is a static render); clicking the chip then reloads.
    state = {"baseline": sum(a.new_total for a in registry.snapshot()), "has_new": 0}
    chip = ui.element("div").classes("oce-sync")
    with chip:
        dot = ui.element("div").classes(f"oce-dot {kind}")
        label = ui.label(text).classes("oce-sync-text")
        tooltip = ui.tooltip(tip).style("white-space: pre-line; max-width: 420px")
        btn = ui.button(icon="refresh").props("flat round dense size=sm").classes("oce-sync-btn")
        btn.tooltip("Check for new mail now")

    def _chip_click() -> None:
        if state["has_new"]:
            ui.navigate.reload()

    chip.on("click", _chip_click)

    _dot_kinds = ("oce-dot--up", "oce-dot--down", "oce-dot--warn", "oce-dot--busy")

    def _paint() -> None:
        kind, text, tip = _describe()
        gained = sum(a.new_total for a in registry.snapshot()) - state["baseline"]
        state["has_new"] = max(0, gained)
        if state["has_new"] and kind == "oce-dot--up":
            n = state["has_new"]
            text = f"{n} new message{'s' if n != 1 else ''} — click to show"
        dot.classes(remove=" ".join(_dot_kinds), add=kind)
        label.set_text(text)
        tooltip.set_text(tip)
        chip.classes(
            remove="oce-sync--error oce-sync--warn oce-sync--new",
            add=(
                "oce-sync--error" if kind == "oce-dot--down" and registry.summary()["state"] != "none"
                else "oce-sync--warn" if kind == "oce-dot--warn"
                else "oce-sync--new" if state["has_new"] else ""
            ),
        )

    async def _refresh() -> None:
        before = registry.seqs()
        if not before:
            ui.notify("No mailbox listener is running — nothing to refresh.", type="warning")
            return
        new_before = sum(a.new_total for a in registry.snapshot())
        btn.props("loading")
        _paint()
        try:
            registry.request_refresh()
            done = await asyncio.to_thread(registry.wait_for_check, before, 25.0)
        finally:
            try:
                btn.props(remove="loading")
                _paint()
            except Exception:
                pass  # page torn down mid-refresh
        new_now = sum(a.new_total for a in registry.snapshot())
        gained = max(0, new_now - new_before)
        s = registry.summary()
        if not done:
            ui.notify(
                "Mail check is taking longer than expected — still running in the background.",
                type="warning", timeout=6000,
            )
        elif s["errors"]:
            names = ", ".join(f"{e.account} ({e.last_error})" for e in s["errors"])
            ui.notify(f"Checked, but a mailbox has a problem: {names}", type="negative", timeout=9000)
        if gained:
            ui.notify(f"{gained} new message{'s' if gained != 1 else ''}.", type="positive")
            ui.navigate.reload()
        elif done and not s["errors"]:
            ui.notify("Mailboxes checked — no new mail.", type="info", timeout=2500)

    btn.on_click(_refresh)
    ui.timer(2.0, _paint)


@contextmanager
def shell(
    active: str,
    title: str,
    bridge: object | None = None,
    store: object | None = None,
    max_width: int = 1280,
) -> Iterator[None]:
    """Page chrome: palette + icon sidebar + header; yields the content column."""
    apply()

    drawer = ui.left_drawer(value=None, fixed=True).props(":breakpoint=760 :width=210")
    with drawer:
        with ui.column().classes("w-full q-pa-sm").style("gap: 10px"):
            with ui.row().classes("items-center no-wrap q-px-sm").style("gap: 10px"):
                with ui.element("div").classes("oce-logo"):
                    ui.html("&#9993;")  # envelope glyph
                ui.label("OpenClaw Email").classes("oce-brand-name")
            ui.element("div").style(
                "height:1px;width:100%;background:var(--border-light)"
            )
            for key, label, icon, route in NAV:
                btn = (
                    ui.button(label, icon=icon, on_click=lambda r=route: ui.navigate.to(r))
                    .props("flat no-caps dense")
                    .classes("oce-nav-btn")
                )
                if key == active:
                    btn.classes(add="oce-nav-btn--active")

    with ui.header(elevated=False).classes("oce-header items-center q-px-md q-py-sm"):
        ui.button(icon="menu", on_click=drawer.toggle).props("flat round dense").classes(
            "oce-menu-btn"
        )
        ui.label(title).classes("text-h6").style("font-weight:700")
        ui.space()
        ui.label("human approval — nothing sends without you").classes(
            "text-caption oce-header-note"
        ).style("color: var(--text-secondary)")
        _sync_widget(store)
        _health_dot(bridge, store)

    with ui.column().classes("w-full mx-auto oce-main-column").style(
        f"max-width: {max(760, int(max_width))}px; gap: 14px"
    ):
        yield
