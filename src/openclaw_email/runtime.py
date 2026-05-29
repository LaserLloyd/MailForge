"""Runtime integrator — wires the listener, agent graph, monitor, and UI.

This is the body of ``openclaw-email serve``. It is the only place that holds
references to every subsystem at once:

    IMAP listeners (one thread per account, own DB connection)
        → work queue (thread-safe)
            → async consumer → AgentGraph.process_message (drafts only)
    AnomalyMonitor (out-of-band thread)
    NiceGUI UI (127.0.0.1) for human approval + send

If LM Studio is down, ingestion continues and drafts land as
DEFERRED_NO_LLM; a resumer re-runs the graph for them once the model returns.
No mail is ever sent here — the send path is behind a human click in the UI.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

from .agent.graph import AgentGraph
from .audit.monitor import AnomalyMonitor
from .config import load_settings
from .db.store import open_store
from .mail.imap_listener import IMAPListener, bootstrap_allowlist
from .security.logging_filter import install_redaction

log = logging.getLogger(__name__)


def _make_bridge(settings):
    try:
        from .llm.bridge import LMStudioBridge

        return LMStudioBridge.from_settings(settings)
    except Exception as e:  # lmstudio extra not installed
        log.warning("LLM bridge unavailable (%s) — running in DEFERRED mode.", e)
        return None


def run_serve(start_ui: bool = True) -> None:
    settings = load_settings()
    settings.assert_invariants()
    install_redaction()

    db_path = settings.resolved_db_path()
    store = open_store(db_path)  # graph/UI connection
    bridge = _make_bridge(settings)
    graph = AgentGraph(store, settings, bridge)

    work_q: queue.Queue[int] = queue.Queue()
    stop_event = threading.Event()

    # --- start one listener per account (each with its own DB connection) ---
    listeners: list[IMAPListener] = []
    for acct in settings.imap_accounts:
        lstore = open_store(db_path, init=False)
        try:
            n = bootstrap_allowlist(acct, lstore)
            log.info("Bootstrapped %s allowlist entries for %s.", n, acct.name)
        except Exception as e:
            log.info("Allowlist bootstrap skipped for %s: %s", acct.name, e)
        listener = IMAPListener(acct, lstore, work_q.put)
        listeners.append(listener)

    monitor = AnomalyMonitor(store)

    async def _resume_deferred() -> None:
        if bridge is None or not bridge.is_up():
            return
        for d in store.deferred_drafts():
            try:
                await graph.process_message(d["message_id"])
            except Exception as e:
                log.error("Resume of deferred draft %s failed: %s", d["id"], e)

    async def _consumer() -> None:
        loop = asyncio.get_running_loop()
        await _resume_deferred()
        while not stop_event.is_set():
            try:
                mid = await loop.run_in_executor(None, work_q.get)
            except Exception:
                break
            if mid is None:
                continue
            try:
                await graph.process_message(mid)
            except Exception as e:
                log.exception("Graph failed for message %s: %s", mid, e)

    def _start_background() -> None:
        for lst in listeners:
            lst.start()
        monitor.start()
        log.info("Listeners + monitor started (%d account(s)).", len(listeners))

    if start_ui:
        from .ui.app import nicegui_app, run_ui

        # Run listeners + consumer inside NiceGUI's event loop.
        nicegui_app.on_startup(_start_background)
        nicegui_app.on_startup(lambda: asyncio.create_task(_consumer()))

        def _shutdown() -> None:
            stop_event.set()
            for lst in listeners:
                lst.stop()
            monitor.stop()
            work_q.put(None)  # unblock consumer

        nicegui_app.on_shutdown(_shutdown)
        log.info("Starting UI — open the localhost URL printed below to review drafts.")
        run_ui(store, settings, bridge)
    else:
        # Headless: own event loop.
        _start_background()
        try:
            asyncio.run(_consumer())
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            for lst in listeners:
                lst.stop()
            monitor.stop()
