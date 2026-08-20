"""Safe Markdown and plain-text viewers shared by mail, compose, and knowledge."""

from __future__ import annotations

from nicegui import ui

from ..mail.markdown import markdown_to_safe_html


def safe_markdown(
    text: str, *, classes: str = "w-full", allow_links: bool = True
) -> ui.html:
    """Render Markdown after server-side sanitization and remote-media removal."""
    return ui.html(
        markdown_to_safe_html(text, allow_links=allow_links), sanitize=True
    ).classes(
        f"oce-markdown {classes}"
    )


def plain_text(text: str, *, classes: str = "w-full") -> ui.label:
    return ui.label(text or "").classes(classes).style(
        "white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13.5px; "
        "line-height: 1.62; color: var(--text-primary)"
    )


def reader(
    text: str,
    *,
    trusted_markdown: bool,
    title: str = "Message",
    note: str = "",
    markdown_default: bool = False,
    allow_links: bool = True,
) -> None:
    """Render inline content with an optional maximized reading window.

    ``markdown_default=True`` adds a Formatted/Plain toggle and starts on the
    formatted view. Both views are safe for untrusted email content: the
    Markdown path is server-side bleach-sanitized (no scripts, no remote
    images) and re-sanitized by NiceGUI at render time.
    """
    toggleable = markdown_default and not trusted_markdown
    view = {"mode": "formatted" if (trusted_markdown or markdown_default) else "plain"}

    @ui.refreshable
    def _content() -> None:
        if view["mode"] == "formatted":
            safe_markdown(text, allow_links=allow_links)
        else:
            plain_text(text)
        if note:
            ui.label(note).style(
                "margin-top: 10px; color: var(--text-muted); font-size: 11px"
            )

    with ui.card().classes("w-full oce-card oce-reader-card").style("padding: 16px"):
        with ui.row().classes("w-full items-center").style("gap: 8px"):
            ui.label(title).style("font-weight: 700; color: var(--text-secondary)")
            ui.space()
            if toggleable:
                ui.toggle(
                    {"formatted": "Formatted", "plain": "Plain text"},
                    value=view["mode"],
                    on_change=lambda e: (view.update(mode=e.value), _content.refresh()),
                ).props("dense no-caps unelevated toggle-color=primary size=sm")
            with ui.dialog().props("maximized transition-show=slide-up") as dialog:
                with ui.card().classes("oce-reader-window"):
                    with ui.row().classes(
                        "w-full items-center q-pa-md oce-reader-window-header"
                    ):
                        ui.label(title).style("font-size: 18px; font-weight: 800")
                        ui.space()
                        ui.button(icon="close", on_click=dialog.close).props(
                            "flat round dense"
                        )
                    with ui.element("div").classes(
                        "w-full oce-reader-window-content q-pa-lg"
                    ):
                        _content()
            button = ui.button(icon="open_in_full", on_click=dialog.open).props(
                "flat round dense"
            )
            with button:
                ui.tooltip("Open full-window reader")
        _content()
