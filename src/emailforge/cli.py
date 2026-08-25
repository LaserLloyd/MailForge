"""Command-line interface (Typer) — build spec §2.

Subcommands: setup-wizard, serve, ingest, service {install,start,stop},
redteam, audit-verify, version. The wizard is the user-friendly entrypoint
the spec calls for: it walks the user through accounts, stores creds in the
keyring, writes the TOML config, and initialises the DB.
"""

from __future__ import annotations

import getpass
import logging
import sys

import typer

from . import __version__
from .db.store import DEFAULT_SITE_ID

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="EmailForge — local-LLM email triage & auto-draft (no auto-send).",
)
service_app = typer.Typer(no_args_is_help=True, help="Install/manage the background service.")
app.add_typer(service_app, name="service")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("emailforge")


def _infer_site(username: str, site_ids: list[str]) -> str:
    """Guess a mailbox's site from its address, else use the first site.

    A mailbox usually belongs to the site whose id appears in its domain
    (``sales@shop.example.com`` -> ``shop``). No match => the first configured
    site, which is the only sensible default on a single-site install.
    """
    domain = username.lower().rpartition("@")[2]
    for site in site_ids:
        if site and site in domain:
            return site
    return site_ids[0] if site_ids else DEFAULT_SITE_ID


@app.command()
def version() -> None:
    """Print version."""
    typer.echo(f"emailforge {__version__}")


@app.command("setup-wizard")
def setup_wizard() -> None:
    """Interactive setup: accounts, credentials (keyring), config, DB init."""
    from . import secrets as secrets_mod
    from .config import IMAPAccount, SecuritySettings, Settings, SMTPAccount
    from .db.store import open_store

    typer.secho("\n=== EmailForge — Setup Wizard ===\n", fg="cyan", bold=True)
    typer.echo("Credentials are stored in your OS keyring, never on disk in plaintext.\n")

    # --- IMAP account ---
    name = typer.prompt("Account label (e.g. 'work')", default="primary")
    imap_host = typer.prompt("IMAP host", default="imap.example.com")
    imap_port = typer.prompt("IMAP port", default=993, type=int)
    username = typer.prompt("Email / username")
    site_ids = list(Settings().sites.keys())
    inferred_site = _infer_site(username, site_ids)
    site_id = typer.prompt(f"Site [{'/'.join(site_ids)}]", default=inferred_site).strip().lower()
    if site_id not in site_ids:
        typer.secho(f"Site must be one of: {', '.join(site_ids)}.", fg="red")
        raise typer.Exit(code=2)
    auth_method = typer.prompt(
        "IMAP auth method [password/xoauth2]", default="password"
    ).strip()
    secret = getpass.getpass("IMAP password or OAuth2 token (hidden): ")
    secrets_mod.set_imap_secret(name, secret)

    # --- SMTP account ---
    typer.echo("")
    smtp_host = typer.prompt("SMTP host", default="smtp.example.com")
    smtp_port = typer.prompt("SMTP port", default=587, type=int)
    smtp_user = typer.prompt("SMTP username", default=username)
    same = typer.confirm("Use the same secret for SMTP?", default=True)
    smtp_secret = secret if same else getpass.getpass("SMTP password/token (hidden): ")
    # Send paths select an identity by SMTP username, so key the credential by
    # that same stable value (account labels are only for IMAP listener names).
    secrets_mod.set_smtp_secret(smtp_user, smtp_secret)

    settings = Settings(
        imap_accounts=[
            IMAPAccount(
                name=name,
                site_id=site_id,
                host=imap_host,
                port=imap_port,
                username=username,
                auth_method="xoauth2" if auth_method == "xoauth2" else "password",
            )
        ],
        smtp=SMTPAccount(host=smtp_host, port=smtp_port, username=smtp_user),
        security=SecuritySettings(),
    )
    settings.assert_invariants()
    cfg_path = settings.save()
    typer.secho(f"\nConfig written to {cfg_path}", fg="green")

    store = open_store(settings.resolved_db_path())
    store.upsert_account(name, "imap", imap_host, imap_port, username, site_id)
    store.upsert_account(name, "smtp", smtp_host, smtp_port, smtp_user, site_id)
    typer.secho(f"Database initialised at {store.db_path} (chmod 0600)", fg="green")
    store.close()

    typer.secho(
        "\nDone. Start the agent with:  emailforge serve\n"
        "Then open the localhost URL it prints to review drafts.\n",
        fg="cyan",
        bold=True,
    )


@app.command("account-add")
def account_add(
    name: str = typer.Option(None, "--name", help="Unique account label, e.g. 'support'."),
    username: str = typer.Option(None, "--username", help="Mailbox address."),
    site: str = typer.Option(None, "--site", help="Site id the mailbox belongs to."),
    imap_host: str = typer.Option(None, "--imap-host"),
    imap_port: int = typer.Option(None, "--imap-port"),
    auth: str = typer.Option(None, "--auth", help="password | xoauth2"),
    smtp_host: str = typer.Option(
        "", "--smtp-host", help="Per-account SMTP override; empty => global relay."
    ),
    smtp_port: int = typer.Option(587, "--smtp-port"),
    password_stdin: bool = typer.Option(
        False,
        "--password-stdin",
        help=(
            "Read the password from stdin instead of prompting: line 1 = IMAP, "
            "optional line 2 = SMTP. Keeps the secret out of argv and shell history."
        ),
    ),
    test_login: bool = typer.Option(
        False, "--test", help="Verify the credential logs in over IMAP before saving."
    ),
    no_input: bool = typer.Option(
        False, "--no-input", help="Never prompt; fail if a required value is missing."
    ),
) -> None:
    """Append one mailbox without replacing existing account configuration.

    Interactive by default. Passing ``--password-stdin`` with the other options
    makes this scriptable, which is how external credential tooling adds
    mailboxes (a GUI cannot answer getpass prompts).
    """
    from .accounts import AccountError, add_account, test_imap_login
    from .config import load_settings

    settings = load_settings()
    site_ids = list(settings.sites.keys())

    def _need(value: str | None, prompt: str, default: str | None = None) -> str:
        if value:
            return str(value).strip()
        # A default is a default in both modes — only a value with no default is
        # genuinely required, and only that should fail a scripted add.
        if default is not None:
            if no_input or password_stdin:
                return default
            return str(typer.prompt(prompt, default=default)).strip()
        if no_input or password_stdin:
            typer.secho(f"Missing required value: {prompt}.", fg="red")
            raise typer.Exit(code=2)
        return str(typer.prompt(prompt)).strip()

    if not no_input and not password_stdin:
        typer.secho("\n=== Add EmailForge mailbox ===\n", fg="cyan", bold=True)

    label = _need(name, "Unique account label")
    address = _need(username, "Email / username")
    site_id = _need(site, f"Site [{'/'.join(site_ids)}]", _infer_site(address, site_ids))
    host = _need(imap_host, "IMAP host", "imap.example.com")
    port = imap_port or (993 if no_input or password_stdin else typer.prompt("IMAP port", default=993, type=int))
    method = _need(auth, "IMAP auth method [password/xoauth2]", "password").lower()

    if password_stdin:
        lines = sys.stdin.read().split("\n")
        imap_secret = lines[0] if lines else ""
        smtp_secret = lines[1] if len(lines) > 1 and lines[1] else imap_secret
    elif no_input:
        typer.secho("--no-input requires --password-stdin.", fg="red")
        raise typer.Exit(code=2)
    else:
        imap_secret = getpass.getpass("IMAP password or OAuth2 token (hidden): ")
        smtp_secret = imap_secret
        if not typer.confirm("Use the same secret for SMTP?", default=True):
            smtp_secret = getpass.getpass("SMTP password/token (hidden): ")

    try:
        if test_login:
            test_imap_login(host, port, address, imap_secret, method)
            typer.secho(f"IMAP login OK for {address}.", fg="green")
        add_account(
            settings,
            name=label,
            username=address,
            site_id=site_id,
            imap_host=host,
            imap_port=port,
            auth_method=method,
            imap_secret=imap_secret,
            smtp_secret=smtp_secret,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
        )
    except AccountError as e:
        typer.secho(f"{e} Nothing changed.", fg="red")
        raise typer.Exit(code=2) from None

    typer.secho(f"Added {address} as '{label}' (site: {site_id}).", fg="green")
    typer.secho("Restart emailforge.service for the listener to take effect.", fg="yellow")


@app.command("account-list")
def account_list() -> None:
    """List configured mailboxes and whether each has a stored credential."""
    from . import secrets as secrets_mod
    from .config import load_settings

    settings = load_settings()
    if not settings.imap_accounts:
        typer.secho("No mailboxes configured. Add one with: emailforge account-add", fg="yellow")
        return
    for acct in settings.imap_accounts:
        has_imap = secrets_mod.get_imap_secret(acct.name) is not None
        has_smtp = secrets_mod.get_smtp_secret(acct.username) is not None
        # Report only presence — never the value (invariant §0.5).
        state = "ok" if (has_imap and has_smtp) else "MISSING CREDENTIAL"
        colour = "green" if (has_imap and has_smtp) else "red"
        typer.secho(
            f"{acct.name:<16} {acct.username:<32} site={acct.site_id:<12} "
            f"{acct.host}:{acct.port}  imap-secret={'yes' if has_imap else 'no'} "
            f"smtp-secret={'yes' if has_smtp else 'no'}  [{state}]",
            fg=colour,
        )


@app.command("account-remove")
def account_remove(
    name: str = typer.Argument(..., help="Account label to remove."),
    keep_secrets: bool = typer.Option(
        False, "--keep-secrets", help="Leave the keyring entries in place."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Stop listening to a mailbox. Message history is retained."""
    from .accounts import AccountError, remove_account
    from .config import load_settings

    settings = load_settings()
    if not yes and not typer.confirm(
        f"Remove mailbox '{name}'? Stored messages are kept; the listener stops.",
        default=False,
    ):
        typer.secho("Nothing changed.", fg="yellow")
        raise typer.Exit(code=1)
    try:
        account = remove_account(settings, name, purge_secrets=not keep_secrets)
    except AccountError as e:
        typer.secho(f"{e} Nothing changed.", fg="red")
        raise typer.Exit(code=2) from None
    typer.secho(f"Removed {account.username} ('{account.name}'). History retained.", fg="green")
    typer.secho("Restart emailforge.service to stop the listener.", fg="yellow")


@app.command()
def serve(
    no_ui: bool = typer.Option(False, "--no-ui", help="Run listener/agent without the web UI."),
) -> None:
    """Start the mail listener, agent graph, and (by default) the web UI."""
    from .runtime import run_serve

    run_serve(start_ui=not no_ui)


@app.command()
def demo(
    port: int = typer.Option(0, "--port", help="UI port (0 = pick a free one)."),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the launch URL instead of opening a browser."
    ),
    keep: bool = typer.Option(
        False, "--keep", help="Keep the throwaway demo directory after exit."
    ),
) -> None:
    """Run a self-contained demo on fictional data (no mailbox, no real config).

    Creates a throwaway application home in the system temp directory, seeds a
    small fictional mailbox, and starts the normal UI against it. Your real
    config and database are never opened.
    """
    import os
    import shutil
    import signal
    import tempfile
    import threading
    import time
    import webbrowser
    from pathlib import Path

    home = Path(tempfile.mkdtemp(prefix="emailforge-demo-"))

    # Point the whole app at the throwaway home BEFORE importing anything that
    # resolves paths at import time (config binds the TOML path on import).
    os.environ["EMAILFORGE_HOME"] = str(home)
    # NiceGUI's session store defaults to ./.nicegui in the CURRENT directory;
    # keep the demo's footprint entirely inside the throwaway home. (Read at
    # import time, so it must be set before nicegui is imported.)
    os.environ.setdefault("NICEGUI_STORAGE_PATH", str(home / "data" / ".nicegui"))

    from .db.store import open_store
    from .demo import build_demo_home, seed_demo
    from .paths import config_file, data_dir

    build_demo_home(home)

    # Hard guarantee: the demo may only ever touch the throwaway tree.
    if home not in data_dir().parents and data_dir() != home / "data":
        raise RuntimeError("demo refused: data directory is not the throwaway home")
    if home not in config_file().parents:
        raise RuntimeError("demo refused: config file is not in the throwaway home")

    from .config import load_settings

    settings = load_settings()
    with open_store(settings.resolved_db_path()) as store:
        counts = seed_demo(store)

    typer.secho(
        f"Demo data: {counts['messages']} messages, {counts['drafts']} drafts "
        f"in {home}",
        fg="cyan",
    )
    typer.secho(
        "No mailbox is configured, so nothing is fetched or sent. "
        "Ctrl-C to stop; the directory is deleted on exit.",
        fg="yellow",
    )

    from .demo import SimulatedMailbox
    from .runtime import run_serve
    from .ui.app import launcher_key

    ui_port = int(port) or None
    # The header sync chip / Refresh button need a mailbox to report on; the
    # configured example mailboxes must never be contacted.
    os.environ["EMAILFORGE_NO_LISTENERS"] = "1"
    SimulatedMailbox().start()

    def _open_when_up() -> None:
        from .ui.app import persistent_port

        target = ui_port or persistent_port()
        url = f"http://127.0.0.1:{target}/launch?k={launcher_key()}"
        for _ in range(60):
            time.sleep(0.25)
            import socket

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.4)
                if sock.connect_ex(("127.0.0.1", target)) == 0:
                    webbrowser.open(url)
                    return

    if not no_browser:
        threading.Thread(target=_open_when_up, daemon=True).start()

    # A plain `kill` must clean up too, not just Ctrl-C: without this the
    # throwaway directory survives every non-interactive stop.
    def _on_term(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_term)

    try:
        run_serve(start_ui=True, ui_port=ui_port)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if keep:
            typer.secho(f"Demo directory kept: {home}", fg="yellow")
        else:
            shutil.rmtree(home, ignore_errors=True)


@app.command()
def open(  # noqa: A001 - intentional CLI verb
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the URL instead of opening a browser."
    ),
) -> None:
    """Open the approval UI in your browser (used by the app-drawer launcher).

    Ensures the background service is running, then opens an authenticated
    session via the local launcher key — no copy-pasting the console token.
    """
    import socket
    import time
    import webbrowser

    from .ui.app import launcher_key, persistent_port

    # 1) Make sure something is serving the UI. Prefer the installed service.
    port = persistent_port()

    def _up() -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            return s.connect_ex(("127.0.0.1", port)) == 0

    if not _up():
        try:
            from .service import start_service

            start_service()
        except Exception as e:
            log.debug("could not start service automatically: %s", e)
        # Wait briefly for the server to bind.
        for _ in range(40):  # ~10s
            if _up():
                break
            time.sleep(0.25)

    if not _up():
        typer.secho(
            "UI is not running. Start it with 'emailforge service start' "
            "or 'emailforge serve', then try again.",
            fg="red",
        )
        raise typer.Exit(code=1)

    url = f"http://127.0.0.1:{port}/launch?k={launcher_key()}"
    if no_browser:
        typer.echo(url)
        return
    typer.secho(f"Opening {('http://127.0.0.1:%d' % port)} …", fg="cyan")
    if not webbrowser.open(url):
        typer.echo(f"Could not launch a browser. Open this URL manually:\n{url}")


@app.command()
def ingest(
    style: bool = typer.Option(True, help="Ingest configured style guides."),
    context: bool = typer.Option(True, help="Ingest configured context docs."),
) -> None:
    """Embed style guides / context docs into the vector store."""
    import asyncio

    from .config import load_settings
    from .rag.embedder import ingest_documents

    settings = load_settings()
    n = asyncio.run(
        ingest_documents(settings, include_style=style, include_context=context)
    )
    typer.secho(f"Ingested {n} chunks.", fg="green")


@app.command("references-refresh")
def references_refresh(
    site: str = typer.Option(
        "all", "--site", help="Managed handbook to refresh: all, or a site id from config [sites]."
    ),
) -> None:
    """Regenerate, index, and sync canonical response-bot site knowledge."""
    import asyncio

    from .config import load_settings
    from .db.store import open_store
    from .knowledge.sync import refresh_managed_knowledge
    from .runtime import _make_bridge

    settings = load_settings()
    site_ids = list(settings.sites.keys())
    requested = site.strip().lower()
    if requested != "all" and requested not in site_ids:
        typer.secho(f"Site must be 'all' or one of: {', '.join(site_ids)}.", fg="red")
        raise typer.Exit(code=2)
    bridge = _make_bridge(settings)
    with open_store(settings.resolved_db_path()) as store:
        report = asyncio.run(
            refresh_managed_knowledge(
                store,
                bridge,
                sites=site_ids if requested == "all" else (requested,),
            )
        )
    for site_id, site_report in report.items():
        if "documents" not in site_report:
            typer.secho(f"{site_id}: {site_report.get('skipped', 'skipped')}", fg="yellow")
            continue
        statuses = ", ".join(
            f"{kind}: {doc['status']}"
            for kind, doc in site_report["documents"].items()
        )
        typer.secho(f"{site_id}: {statuses}", fg="green")


@app.command("audit-verify")
def audit_verify() -> None:
    """Verify the chained-hash audit log integrity."""
    from .audit.log import verify_chain
    from .config import load_settings

    settings = load_settings()
    ok, n, bad = verify_chain(settings.resolved_db_path())
    if ok:
        typer.secho(f"Audit chain OK ({n} entries).", fg="green")
    else:
        typer.secho(f"AUDIT CHAIN BROKEN at entry {bad} of {n}.", fg="red")
        raise typer.Exit(code=1)


@app.command("bridge")
def bridge_command(
    action: str = typer.Argument(..., help="Machine action (status/list/get/propose/revise/weekly-summary/reference-search)."),
    site: str = typer.Option(
        ..., "--site", help="Required site boundary: site id from config [sites]."
    ),
) -> None:
    """Site-bound JSON stdin/stdout bridge for the local OpenClaw plugin."""
    from .bridge_cli import run_bridge

    raise typer.Exit(code=run_bridge(action, site))


@app.command()
def redteam(
    suite: str = typer.Option("all", help="garak | promptfoo | all"),
) -> None:
    """Run offline red-team suites against the local agent endpoint."""
    from .redteam_runner import run_redteam

    code = run_redteam(suite)
    raise typer.Exit(code=code)


@service_app.command("install")
def service_install() -> None:
    """Install the background service (systemd user unit / WinSW)."""
    from .service import install_service

    path = install_service()
    typer.secho(f"Service installed: {path}", fg="green")


@service_app.command("desktop")
def service_desktop() -> None:
    """(Re)install just the app-drawer launcher entry + icon."""
    from .service import install_desktop_entry

    path = install_desktop_entry()
    typer.secho(f"Desktop entry: {path}", fg="green")


@service_app.command("start")
def service_start() -> None:
    from .service import start_service

    start_service()
    typer.secho("Service started.", fg="green")


@service_app.command("stop")
def service_stop() -> None:
    from .service import stop_service

    stop_service()
    typer.secho("Service stopped.", fg="green")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()


@app.command("purge-trash")
def purge_trash(
    yes: bool = typer.Option(
        False, "--yes", help="Actually delete. Without it this only reports what is due."
    ),
    now: bool = typer.Option(
        False,
        "--now",
        help="Ignore the holding period and delete EVERYTHING in Trash, not just what is due.",
    ),
) -> None:
    """Permanently delete Trash whose holding period is up — locally and at the
    mail server.

    The app runs this automatically every few hours; the command exists for a
    manual sweep and for checking what is queued. Spam and scam mail has no
    holding period; everything else waits ``security.trash_retention_days``.
    """
    from .config import load_settings
    from .db.store import open_store
    from .mail.retention import delete_mode, purge_now, retention_days, sweep

    settings = load_settings()
    store = open_store(settings.resolved_db_path())
    days = retention_days(settings)
    mode = delete_mode(settings)
    queue = store.trash_queue(retention_days=days)
    due = store.trash_due_ids(retention_days=days)
    typer.echo(
        f"Trash: {len(queue)} message(s) held, {len(due)} due now "
        f"(retention {days} days, provider delete mode '{mode}')."
    )
    if not yes:
        for row in queue[:20]:
            when = row["due_at"] or "immediately (spam/scam)"
            typer.echo(f"  #{row['id']:>6}  due {when}  {str(row['subject'] or '')[:60]}")
        if len(queue) > 20:
            typer.echo(f"  … and {len(queue) - 20} more")
        typer.secho("Dry run — pass --yes to delete.", fg="yellow")
        raise typer.Exit(code=0)
    if now:
        report = purge_now(store, settings, [int(r["id"]) for r in queue])
    else:
        report = sweep(store, settings)
    typer.secho(report.summary(), fg="green" if report.ok else "yellow")
    for err in report.errors:
        typer.secho(f"  provider error — {err}", fg="red")
    raise typer.Exit(code=0 if report.ok else 1)
