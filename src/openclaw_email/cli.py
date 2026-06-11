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

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="OpenClaw Email Agent — local-LLM email triage & auto-draft (no auto-send).",
)
service_app = typer.Typer(no_args_is_help=True, help="Install/manage the background service.")
app.add_typer(service_app, name="service")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("openclaw_email")


@app.command()
def version() -> None:
    """Print version."""
    typer.echo(f"openclaw-email {__version__}")


@app.command("setup-wizard")
def setup_wizard() -> None:
    """Interactive setup: accounts, credentials (keyring), config, DB init."""
    from . import secrets as secrets_mod
    from .config import IMAPAccount, SecuritySettings, Settings, SMTPAccount
    from .db.store import open_store

    typer.secho("\n=== OpenClaw Email Agent — Setup Wizard ===\n", fg="cyan", bold=True)
    typer.echo("Credentials are stored in your OS keyring, never on disk in plaintext.\n")

    # --- IMAP account ---
    name = typer.prompt("Account label (e.g. 'work')", default="primary")
    imap_host = typer.prompt("IMAP host", default="imap.example.com")
    imap_port = typer.prompt("IMAP port", default=993, type=int)
    username = typer.prompt("Email / username")
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
    secrets_mod.set_smtp_secret(name, smtp_secret)

    settings = Settings(
        imap_accounts=[
            IMAPAccount(
                name=name,
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
    store.upsert_account(name, "imap", imap_host, imap_port, username)
    store.upsert_account(name, "smtp", smtp_host, smtp_port, smtp_user)
    typer.secho(f"Database initialised at {store.db_path} (chmod 0600)", fg="green")
    store.close()

    typer.secho(
        "\nDone. Start the agent with:  openclaw-email serve\n"
        "Then open the localhost URL it prints to review drafts.\n",
        fg="cyan",
        bold=True,
    )


@app.command()
def serve(
    no_ui: bool = typer.Option(False, "--no-ui", help="Run listener/agent without the web UI."),
) -> None:
    """Start the mail listener, agent graph, and (by default) the web UI."""
    from .runtime import run_serve

    run_serve(start_ui=not no_ui)


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
            "UI is not running. Start it with 'openclaw-email service start' "
            "or 'openclaw-email serve', then try again.",
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
    from .config import load_settings
    from .rag.embedder import ingest_documents

    settings = load_settings()
    n = ingest_documents(settings, include_style=style, include_context=context)
    typer.secho(f"Ingested {n} chunks.", fg="green")


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
