import asyncio
from pathlib import Path
from typing import Optional
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from opal_sync.auth.credentials import delete_credentials, get_credentials, store_credentials
from opal_sync.auth.session import SessionManager
from opal_sync.auth.shibboleth import login_shibboleth, ShibbolethAuthError
from opal_sync.config import UNIVERSITY_MAP, AppConfig, load_config, save_config
from opal_sync.models.course import Course
from opal_sync.scraper.courses import fetch_my_courses
from opal_sync.storage.db import Database
from opal_sync.sync.differ import SyncAction
from opal_sync.sync.engine import SyncEngine
from opal_sync.utils.logging import get_logger, setup_logger

app = typer.Typer(
    name="opal-sync",
    help="Automated synchronization client for OPAL Bildungsportal Sachsen course materials.",
    add_completion=False,
)
console = Console()
logger = get_logger()


def get_configured_engine(config_path: Optional[Path] = None) -> SyncEngine:
    config = load_config(config_path)
    setup_logger(config.general.log_level, str(config.general.log_file) if config.general.log_file else None)
    return SyncEngine(config=config)


@app.command()
def login(
    username: Optional[str] = typer.Option(None, "--username", "-u", help="Student username (e.g. s12345)"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="Password for university login"),
    university: Optional[str] = typer.Option(None, "--university", help="University key (e.g. htwk-leipzig)"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="Custom config file path"),
):
    """Authenticate with OPAL via Shibboleth and store credentials securely."""
    config = load_config(config_path)
    setup_logger(config.general.log_level)

    console.print(Panel.fit("[bold blue]OPAL Bildungsportal — Shibboleth Authentication[/bold blue]"))

    # Determine university
    if not university:
        default_uni = config.auth.university or "htwk-leipzig"
        university = typer.prompt("Select university", default=default_uni)
    config.auth.university = university.strip().lower()

    try:
        wayf_val = config.auth.wayf_value
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    # Determine username
    if not username:
        default_user = config.auth.username or ""
        username = typer.prompt("University Username (Matrikelnummer/s-Nummer)", default=default_user)
    config.auth.username = username.strip()

    # Determine password
    if not password:
        password = typer.prompt("University Password", hide_input=True)

    async def _do_login() -> bool:
        import httpx
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            return await login_shibboleth(
                client=client,
                username=config.auth.username,
                password=password,
                wayf_value=wayf_val,
            )

    try:
        with console.status("[bold green]Contacting Shibboleth IdP and verifying credentials...[/bold green]"):
            success = asyncio.run(_do_login())
    except ShibbolethAuthError as e:
        console.print(f"\n[bold red]Authentication failed:[/bold red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"\n[bold red]Network or connection error:[/bold red] {e}")
        raise typer.Exit(code=1)

    if success:
        # Save password to OS keyring
        store_credentials(config.auth.username, password)
        # Save config
        saved_path = save_config(config, config_path)

        console.print("\n[bold green]✓ Login successful![/bold green]")
        console.print(f"Credentials saved securely in OS Credential Manager for user: [cyan]{config.auth.username}[/cyan]")
        console.print(f"Configuration saved to: [italic]{saved_path}[/italic]\n")


@app.command()
def logout(
    username: Optional[str] = typer.Option(None, "--username", "-u", help="Username to clear credentials for"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="Custom config file path"),
):
    """Remove stored credentials and clear cached session cookies."""
    config = load_config(config_path)
    user = username or config.auth.username

    if user:
        delete_credentials(user)
        console.print(f"[green]✓ Removed keyring credentials for user '{user}'.[/green]")

    session_mgr = SessionManager(config.auth.session.cookie_file)
    session_mgr.clear_session()
    console.print("[green]✓ Cleared local session cookies.[/green]")


@app.command()
def courses(
    refresh: bool = typer.Option(False, "--refresh", "-r", help="Fetch live course list from OPAL"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="Custom config file path"),
):
    """List enrolled courses and their synchronization status."""
    engine = get_configured_engine(config_path)

    async def _fetch_and_store() -> list:
        import httpx
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            await engine.ensure_authenticated(client)
            courses_list = await fetch_my_courses(client)
            for c in courses_list:
                engine.db.upsert_course(c)
            return engine.db.get_courses()

    course_items = []
    if refresh:
        with console.status("[bold green]Connecting to OPAL and refreshing course catalog...[/bold green]"):
            course_items = asyncio.run(_fetch_and_store())
    else:
        course_items = engine.db.get_courses()
        if not course_items:
            console.print("[yellow]No courses cached in local database. Fetching live from OPAL...[/yellow]")
            with console.status("[bold green]Fetching courses...[/bold green]"):
                course_items = asyncio.run(_fetch_and_store())

    if not course_items:
        console.print("[yellow]No enrolled courses found.[/yellow]")
        return

    table = Table(title=f"OPAL Enrolled Courses ({len(course_items)})", header_style="bold cyan")
    table.add_column("OPAL ID", style="dim", width=12)
    table.add_column("Course Name", style="bold")
    table.add_column("Semester", style="magenta")
    table.add_column("Last Synced", style="green")
    table.add_column("Status", style="yellow")

    for c in course_items:
        synced_str = c.last_synced_at.strftime("%Y-%m-%d %H:%M") if c.last_synced_at else "Never"
        status = "[red]Excluded[/red]" if c.is_excluded else "[green]Active[/green]"
        table.add_row(c.opal_id, c.name, c.semester or "—", synced_str, status)

    console.print(table)


@app.command()
def sync(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Simulate sync without downloading files"),
    course: Optional[str] = typer.Option(None, "--course", help="Sync only a specific course (name or ID)"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-download files even if unchanged"),
    list_skipped_files: bool = typer.Option(
        False,
        "--list-skipped-files",
        help="List files skipped due to extension filters or the size limit",
    ),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="Custom config file path"),
):
    """Synchronize course files from OPAL to local disk."""
    engine = get_configured_engine(config_path)

    title = "[bold yellow]Starting OPAL Dry-Run Sync...[/bold yellow]" if dry_run else "[bold green]Starting OPAL Synchronization...[/bold green]"
    console.print(Panel.fit(title))
    console.print(f"Target directory: [cyan]{engine.config.general.sync_directory}[/cyan]\n")

    summary = asyncio.run(
        engine.run_sync(
            dry_run=dry_run,
            course_filter=course,
            force_download=force,
        )
    )

    if list_skipped_files:
        skipped_items = [
            item
            for item in summary.items
            if item.action in (SyncAction.SKIP_EXCLUDED, SyncAction.SKIP_TOO_LARGE)
        ]
        console.print("\n[bold]Skipped files:[/bold]")
        if skipped_items:
            for item in skipped_items:
                reason = (
                    "extension"
                    if item.action == SyncAction.SKIP_EXCLUDED
                    else "size"
                )
                line = Text(f"- {item.course_name}/{item.node_title}/")
                line.append(item.filename, style="cyan")
                line.append(f" ({reason}: {item.details})")
                console.print(line)
        else:
            console.print("None")

    # Print summary table
    table = Table(title="Synchronization Summary", header_style="bold blue")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="cyan")

    table.add_row("Status", f"[green]{summary.status.upper()}[/green]" if summary.status == "success" else f"[red]{summary.status.upper()}[/red]")
    table.add_row("Courses Processed", str(summary.courses_processed))
    table.add_row("New Files", str(summary.files_new))
    table.add_row("Updated Files", str(summary.files_updated))
    table.add_row("Excluded Files", str(summary.files_excluded))
    table.add_row("Too Large Files", str(summary.files_too_large))
    if summary.files_excluded:
        extensions = ", ".join(
            f"{extension} ({count})"
            for extension, count in sorted(summary.excluded_extensions.items())
        ) or "Unknown"
        table.add_row("Excluded Extensions", extensions)
    if summary.files_too_large:
        excluded_mb = summary.excluded_bytes / (1024 * 1024)
        table.add_row("Excluded Data", f"{excluded_mb:.2f} MB")
    table.add_row("Failed Files", str(summary.files_failed))
    table.add_row(
        "Removed Files",
        str(sum(change.prefix == "-" for change in summary.changes)),
    )
    mb = summary.bytes_downloaded / (1024 * 1024)
    table.add_row("Data Downloaded", f"{mb:.2f} MB")
    table.add_row("Total Sync Time", f"{summary.duration_seconds:.2f} seconds")

    console.print("\n", table)
    change_styles = {"+": "green", "~": "blue", "-": "red"}
    if summary.changes:
        console.print("\n[bold]File changes:[/bold]")
    for change in summary.changes:
        console.print(
            Text(
                f"{change.prefix} {change.path}",
                style=change_styles[change.prefix],
            )
        )
    if summary.error_message:
        console.print(
            Panel(
                summary.error_message,
                title="[bold red]Sync failed[/bold red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)


@app.command()
def status(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="Custom config file path"),
):
    """Display overall synchronization statistics and system status."""
    engine = get_configured_engine(config_path)
    stats = engine.db.get_stats()

    table = Table(title="OPAL Sync System Status", header_style="bold magenta")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Configured University", engine.config.auth.university)
    table.add_row("Username", engine.config.auth.username or "[italic red]Not set[/italic red]")

    if engine.config.auth.password:
        pass_status = "[green]Saved (Config)[/green]"
    else:
        has_keyring = bool(get_credentials(engine.config.auth.username)) if engine.config.auth.username else False
        pass_status = "[green]Saved (Keyring)[/green]" if has_keyring else "[red]Missing[/red]"
    table.add_row("Password Status", pass_status)

    sess_file = engine.config.auth.session.cookie_file
    table.add_row("Session Cache", f"[green]Present ({sess_file.name})[/green]" if sess_file.is_file() else "[dim]None[/dim]")

    table.add_row("Sync Directory", str(engine.config.general.sync_directory))
    table.add_row("Total Courses Tracked", str(stats["total_courses"]))
    table.add_row("Total Files Tracked", str(stats["total_files"]))
    total_mb = stats["total_bytes"] / (1024 * 1024)
    table.add_row("Total Downloaded Size", f"{total_mb:.2f} MB")

    last_run = stats.get("last_run")
    if last_run:
        table.add_row("Last Sync Run", f"{last_run['started_at']} ({last_run['status']})")
    else:
        table.add_row("Last Sync Run", "Never")

    console.print(table)


@app.command()
def log(
    limit: int = typer.Option(30, "--limit", "-l", help="Number of log records to show"),
    errors: bool = typer.Option(False, "--errors", "-e", help="Show only errors and warnings"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="Custom config file path"),
):
    """Display recent sync log records and error events."""
    engine = get_configured_engine(config_path)
    logs = engine.db.get_recent_logs(limit=limit, only_errors=errors)

    if not logs:
        console.print("[dim]No sync logs found in database.[/dim]")
        return

    table = Table(title=f"Recent Sync Activity (Last {len(logs)})", header_style="bold cyan")
    table.add_column("Timestamp", style="dim", width=19)
    table.add_column("Level", width=8)
    table.add_column("Action", style="bold", width=16)
    table.add_column("Course", style="cyan")
    table.add_column("Details")

    for entry in logs:
        lvl = entry["level"].upper()
        lvl_colored = f"[red]{lvl}[/red]" if lvl == "ERROR" else f"[yellow]{lvl}[/yellow]" if lvl == "WARNING" else f"[green]{lvl}[/green]"
        table.add_row(
            entry["timestamp"],
            lvl_colored,
            entry["action"],
            entry.get("course_name") or "—",
            entry.get("details") or entry.get("file_path") or "—",
        )

    console.print(table)


@app.command("config")
def show_config(
    show: bool = typer.Option(True, "--show", help="Display active configuration"),
    path: bool = typer.Option(False, "--path", help="Print configuration file path"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="Custom config file path"),
):
    """View or inspect configuration settings."""
    cfg = load_config(config_path)
    if path:
        console.print(str(cfg.config_file_path))
        return

    from rich.syntax import Syntax
    if cfg.config_file_path and cfg.config_file_path.is_file():
        with open(cfg.config_file_path, "r", encoding="utf-8") as f:
            syntax = Syntax(f.read(), "toml", theme="monokai", line_numbers=True)
            console.print(syntax)
    else:
        console.print(f"[yellow]No config file found at {cfg.config_file_path}. Showing in-memory defaults:[/yellow]")
        import tomli_w
        raw_toml = tomli_w.dumps({
            "general": {"sync_directory": str(cfg.general.sync_directory), "log_level": cfg.general.log_level},
            "auth": {"university": cfg.auth.university, "username": cfg.auth.username},
            "sync": {
                "include_extensions": cfg.sync.include_extensions,
                "exclude_extensions": cfg.sync.exclude_extensions,
                "max_file_size_mb": cfg.sync.max_file_size_mb,
                "parallel_downloads": cfg.sync.parallel_downloads,
                "request_delay_seconds": cfg.sync.request_delay_seconds,
                "delete_removed": cfg.sync.delete_removed,
            },
        })
        console.print(Syntax(raw_toml, "toml", theme="monokai"))
