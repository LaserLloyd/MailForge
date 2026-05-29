"""Inbox page — table of PENDING drafts (build spec §9).

Renders a ``ui.table`` of every draft in state ``PENDING`` with columns:
sender, subject, category, injection_risk, recipient. The recipient cell is
shown RED when the recipient is an external / first-time address (i.e.
``store.is_allowlisted(recipient)`` is False) — a visual cue for the §0.4
"recipients are bound, not generated" invariant.

The table is ``@ui.refreshable``; :func:`render` wires it up and the page-level
:func:`refresh` re-pulls from the DB. The runtime/listener coroutine pushes
updates by calling :func:`openclaw_email.ui.app.refresh_inbox` (NiceGUI WS).

Clicking a row navigates to ``/detail/{draft_id}``.
"""

from __future__ import annotations

import logging
from typing import Any

from nicegui import ui

log = logging.getLogger(__name__)

# Columns for the pending-drafts table (spec §9).
_COLUMNS: list[dict[str, Any]] = [
    {"name": "sender", "label": "Sender", "field": "sender", "align": "left", "sortable": True},
    {"name": "subject", "label": "Subject", "field": "subject", "align": "left", "sortable": True},
    {"name": "category", "label": "Category", "field": "category", "align": "left", "sortable": True},
    {
        "name": "injection_risk",
        "label": "Injection risk",
        "field": "injection_risk",
        "align": "right",
        "sortable": True,
    },
    {"name": "recipient", "label": "Recipient", "field": "recipient", "align": "left"},
]


def _classification(store: object, message_id: int | None) -> dict[str, Any]:
    """Read category + injection_risk for a message (read-only via store.conn).

    The store has no typed classification reader, so the UI reads directly from
    the connection (read-only). Degrades to blanks if the row/table is absent.
    """
    if message_id is None:
        return {"category": "", "injection_risk": None}
    try:
        row = store.conn.execute(  # type: ignore[attr-defined]
            "SELECT category, injection_risk FROM classifications WHERE message_id=?",
            (message_id,),
        ).fetchone()
    except Exception as e:  # table may not exist in a bare/test DB
        log.debug("classification read failed: %s", e)
        return {"category": "", "injection_risk": None}
    if not row:
        return {"category": "", "injection_risk": None}
    return {"category": row["category"] or "", "injection_risk": row["injection_risk"]}


def _row_for_draft(store: object, draft: Any) -> dict[str, Any]:
    """Build one table row dict for a PENDING draft."""
    msg = None
    try:
        msg = store.get_message(draft["message_id"])  # type: ignore[attr-defined]
    except Exception as e:
        log.debug("message read failed for draft %s: %s", draft["id"], e)

    sender = ""
    if msg is not None:
        sender = (msg["from_name"] or msg["from_addr"] or "") if msg["from_addr"] else (
            msg["from_name"] or ""
        )

    cls = _classification(store, draft["message_id"])
    recipient = draft["recipient"] or ""
    try:
        external = not store.is_allowlisted(recipient)  # type: ignore[attr-defined]
    except Exception:
        external = True  # fail safe: treat unknown as external (red)

    risk = cls["injection_risk"]
    return {
        "id": draft["id"],
        "sender": sender,
        "subject": draft["subject"] or "",
        "category": cls["category"],
        "injection_risk": "" if risk is None else f"{float(risk):.2f}",
        "recipient": recipient,
        "recipient_external": external,
    }


def pending_rows(store: object) -> list[dict[str, Any]]:
    """Return table rows for all PENDING drafts (spec §9)."""
    try:
        drafts = store.drafts_by_state("PENDING")  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("could not load PENDING drafts: %s", e)
        return []
    return [_row_for_draft(store, d) for d in drafts]


@ui.refreshable
def _inbox_table(store: object) -> None:
    """The refreshable pending-drafts table (spec §9)."""
    rows = pending_rows(store)
    if not rows:
        ui.label("No pending drafts.").classes("text-grey")
        return

    table = ui.table(
        columns=_COLUMNS,
        rows=rows,
        row_key="id",
        pagination=20,
    ).classes("w-full")

    # Render the recipient cell red when external/first-time (spec §0.4 / §9).
    table.add_slot(
        "body-cell-recipient",
        r"""
        <q-td :props="props">
          <span :class="props.row.recipient_external ? 'text-red-600 font-bold' : ''">
            {{ props.row.recipient }}
            <q-badge v-if="props.row.recipient_external" color="red" class="q-ml-sm">
              external
            </q-badge>
          </span>
        </q-td>
        """,
    )

    # Click a row -> detail page.
    table.on("rowClick", lambda e: ui.navigate.to(f"/detail/{e.args[1]['id']}"))


# Module-level handle so the page-level refresh() hook can target the
# most-recently-rendered table instance (NiceGUI binds per client).
_rendered = False


def render(store: object) -> None:
    """Render the inbox page body (spec §9).

    Wires the refreshable table. The runtime listener calls
    :func:`openclaw_email.ui.app.refresh_inbox` (which calls :func:`refresh`)
    to push new pending drafts over the WS.
    """
    global _rendered
    ui.label("Pending drafts").classes("text-h5 q-mb-md")
    ui.label("Click a row to review, approve, edit, or reject.").classes("text-grey q-mb-sm")
    _inbox_table(store)
    _rendered = True


def refresh() -> None:
    """Re-pull PENDING drafts and re-render the table (spec §9 WS push)."""
    if not _rendered:
        return
    try:
        _inbox_table.refresh()
    except Exception as e:  # no active client / not yet rendered
        log.debug("inbox refresh skipped: %s", e)
