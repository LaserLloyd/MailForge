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
import os
import queue
import threading

from .agent.graph import AgentGraph
from .audit.monitor import AnomalyMonitor
from .config import load_settings
from .db.store import open_store
from .mail.imap_listener import IMAPListener
from .security.logging_filter import install_redaction

log = logging.getLogger(__name__)
DEFERRED_RETRY_INTERVAL_S = 60


def _make_bridge(settings):
    try:
        from .llm.bridge import LMStudioBridge

        return LMStudioBridge.from_settings(settings)
    except Exception as e:  # lmstudio extra not installed
        log.warning("LLM bridge unavailable (%s) — running in DEFERRED mode.", e)
        return None


def _site_cfg(settings, site_id):
    return (getattr(settings, "sites", None) or {}).get(str(site_id or "").lower())


def _site_terms(settings, site_id):
    """Configured topical vocabulary for a site (``content_terms``)."""
    from .security.inbound_screening import terms_pattern

    cfg = _site_cfg(settings, site_id)
    return terms_pattern(getattr(cfg, "content_terms", ()) or ())


def _brand_terms(settings, site_id):
    """Names the site is known by: its display name plus ``brand_terms``."""
    from .security.inbound_screening import brand_pattern

    cfg = _site_cfg(settings, site_id)
    names = [getattr(cfg, "name", "") or "", *(getattr(cfg, "brand_terms", ()) or ())]
    return brand_pattern(names)


def run_serve(start_ui: bool = True, ui_port: int | None = None) -> None:
    settings = load_settings()
    settings.assert_invariants()
    install_redaction()

    db_path = settings.resolved_db_path()
    store = open_store(db_path)  # graph/UI connection
    bridge = _make_bridge(settings)
    graph = AgentGraph(store, settings, bridge)

    work_q: queue.Queue[int] = queue.Queue()
    stop_event = threading.Event()
    deferred_resume_lock = asyncio.Lock()

    # --- start one listener per account (each with its own DB connection) ---
    listeners: list[IMAPListener] = []
    # Demo mode configures fictional mailboxes for Compose but must never open
    # a socket to them; the CLI sets this before calling run_serve().
    accounts_to_listen = [] if os.environ.get("OPENCLAW_EMAIL_NO_LISTENERS") == "1" else list(
        settings.imap_accounts
    )
    for acct in accounts_to_listen:
        lstore = open_store(db_path, init=False)
        listener = IMAPListener(
            acct,
            lstore,
            work_q.put,
            security=settings.security,
            screening_mode=settings.site_screening_mode(acct.site_id),
            # Optional per-site screening vocabulary from config.toml
            # ([sites.<id>].content_terms); absent => packaged defaults.
            site_terms=_site_terms(settings, acct.site_id),
            brand_terms=_brand_terms(settings, acct.site_id),
            # Runs in the listener's own thread: a slow Sent-folder scan used to
            # block ALL listeners (and the UI) from starting.
            bootstrap_allowlist_on_start=True,
        )
        listeners.append(listener)

    monitor = AnomalyMonitor(store)

    async def _resume_deferred() -> None:
        # Startup recovery can take longer than the periodic retry interval.
        # Single-flight it so the timer never classifies the same deferred row
        # concurrently with the startup pass.
        async with deferred_resume_lock:
            # is_up() is a blocking HTTP probe (5 s timeout) — keep it off the
            # event loop or the UI freezes every minute while LM Studio is down.
            if bridge is None or not await asyncio.to_thread(bridge.is_up):
                return
            for d in store.deferred_drafts():
                try:
                    await graph.process_message(d["message_id"])
                except Exception as e:
                    log.error("Resume of deferred draft %s failed: %s", d["id"], e)

    async def _deferred_resumer() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(DEFERRED_RETRY_INTERVAL_S)
            if stop_event.is_set():
                return
            await _resume_deferred()

    async def _consumer() -> None:
        loop = asyncio.get_running_loop()
        # Standalone robustness: adapt to whatever models this LM Studio serves
        # so the app works on any machine, not just one with the configured ids.
        if bridge is not None:
            try:
                rep = await bridge.resolve_served_models()
                if rep.get("changed"):
                    log.info(
                        "Adapted to served models: chat=%s embed=%s",
                        rep.get("chat"),
                        rep.get("embed"),
                    )
            except Exception as e:
                log.debug("model resolution skipped: %s", e)
            if getattr(bridge, "has_heavy", False):
                log.info("OpenClaw heavy-lift link active: %s", bridge.heavy_base_url)
        for mid in store.unprocessed_message_ids(limit=200):
            work_q.put(mid)
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

    async def _retention_sweep() -> None:
        """Permanently delete Trash whose holding period is up (mail/retention).

        Runs off the event loop — a sweep logs into every mailbox it has work
        for, and that must never stall the UI or the listeners.
        """
        from .mail.retention import SWEEP_INTERVAL_S, sweep

        while not stop_event.is_set():
            try:
                report = await asyncio.to_thread(sweep, store, settings)
                if report.requested:
                    log.info("Retention sweep: %s", report.summary())
            except Exception as e:  # noqa: BLE001
                log.exception("Retention sweep failed: %s", e)
            for _ in range(int(SWEEP_INTERVAL_S)):
                if stop_event.is_set():
                    return
                await asyncio.sleep(1)

    async def _refresh_bot_knowledge() -> None:
        """Refresh canonical handbooks after core mail processing is online."""
        await asyncio.sleep(8)
        try:
            from .knowledge.sync import refresh_managed_knowledge

            report = await refresh_managed_knowledge(store, bridge)
            log.info("Managed response-bot handbooks refreshed: %s", sorted(report))
        except Exception as e:
            log.warning("Managed handbook refresh deferred: %s", e)

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
        nicegui_app.on_startup(lambda: asyncio.create_task(_deferred_resumer()))
        nicegui_app.on_startup(lambda: asyncio.create_task(_refresh_bot_knowledge()))
        nicegui_app.on_startup(lambda: asyncio.create_task(_retention_sweep()))

        def _shutdown() -> None:
            stop_event.set()
            for lst in listeners:
                lst.stop()
            monitor.stop()
            work_q.put(None)  # unblock consumer

        nicegui_app.on_shutdown(_shutdown)
        log.info("Starting UI — open the localhost URL printed below to review drafts.")
        run_ui(store, settings, bridge, port=ui_port)
    else:
        # Headless: own event loop.
        _start_background()
        try:
            async def _headless() -> None:
                await asyncio.gather(
                    _consumer(),
                    _deferred_resumer(),
                    _refresh_bot_knowledge(),
                    _retention_sweep(),
                )

            asyncio.run(_headless())
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            for lst in listeners:
                lst.stop()
            monitor.stop()
